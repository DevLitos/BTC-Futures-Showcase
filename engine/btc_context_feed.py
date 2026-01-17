"""
BTC Market Context Feed - External async feed for BTC trend data

Provides:
1. EMA20/EMA50 on 1h timeframe for trend direction
2. Recent price data for correlation analysis
3. Async polling with configurable interval

Use Case:
- BTC correlation filter for non-BTC symbols
- Market context for all trades (even BTC itself uses higher TF context)

Created: January 15, 2026
"""

import asyncio
import logging
import aiohttp
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, UTC
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BTCMarketContext:
    """Current BTC market context snapshot"""
    
    # Price data
    last_price: float = 0.0
    ema20_1h: float = 0.0
    ema50_1h: float = 0.0
    ema200_1h: float = 0.0
    
    # Trend signals
    trend_bias: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    trend_strength: float = 0.0  # 0.0 to 1.0
    ema_slope_1h: float = 0.0    # EMA20 slope (% change over 5 candles)
    
    # Volatility
    atr_1h: float = 0.0
    volatility_regime: str = "NORMAL"  # LOW, NORMAL, HIGH, EXTREME
    
    # Timing
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_stale: bool = False  # True if data > 10 min old
    
    def to_dict(self) -> dict:
        return {
            'last_price': round(self.last_price, 2),
            'ema20_1h': round(self.ema20_1h, 2),
            'ema50_1h': round(self.ema50_1h, 2),
            'ema200_1h': round(self.ema200_1h, 2),
            'trend_bias': self.trend_bias,
            'trend_strength': round(self.trend_strength, 2),
            'ema_slope_1h': round(self.ema_slope_1h * 100, 4),  # As percentage
            'atr_1h': round(self.atr_1h, 2),
            'volatility_regime': self.volatility_regime,
            'last_update': self.last_update.isoformat(),
            'is_stale': self.is_stale,
        }


class BTCMarketContextFeed:
    """
    Async feed for BTC market context data.
    
    Polls external source (exchange or aggregator) for BTC 1h candles,
    calculates EMAs, trend direction, and volatility metrics.
    
    Usage:
        feed = BTCMarketContextFeed(exchange='kraken')
        await feed.start()  # Runs in background
        
        # Get current context
        context = feed.get_context()
        if context.trend_bias == 'BEARISH':
            # Avoid LONGs
    """
    
    # Poll interval (seconds)
    POLL_INTERVAL = 300  # 5 minutes
    STALE_THRESHOLD = 600  # 10 minutes
    
    # EMA periods
    EMA_SHORT = 20
    EMA_MID = 50
    EMA_LONG = 200
    
    # ATR period
    ATR_PERIOD = 14
    
    # Candles to fetch
    CANDLE_LOOKBACK = 220  # Enough for EMA200 + some buffer
    
    def __init__(self,
                 exchange: str = 'kraken',
                 symbol: str = 'BTC/USD',
                 enabled: bool = True):
        """
        Initialize BTC context feed.
        
        Args:
            exchange: Exchange to fetch from ('kraken', 'binance')
            symbol: Symbol to track (default BTC/USD)
            enabled: Enable/disable the feed
        """
        self.exchange = exchange.lower()
        self.symbol = symbol
        self.enabled = enabled
        self.is_running = False
        
        # Current context
        self._context = BTCMarketContext()
        
        # Historical data cache
        self._candles_1h: pd.DataFrame = pd.DataFrame()
        
        # HTTP session
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Supported exchanges
        SUPPORTED = ['kraken', 'binance', 'krakenfutures']
        if self.exchange not in SUPPORTED:
            logger.warning(f"Unsupported exchange: {exchange}. Using kraken.")
            self.exchange = 'kraken'
        
        logger.info(f"BTCMarketContextFeed initialized: {symbol} on {exchange}")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create reusable HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def _close_session(self):
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def start(self):
        """Start background polling."""
        if not self.enabled:
            logger.info("BTCMarketContextFeed disabled, not starting")
            return
        
        self.is_running = True
        logger.info("BTCMarketContextFeed starting...")
        
        try:
            # Initial fetch
            await self._poll_data()
            
            while self.is_running:
                try:
                    await asyncio.sleep(self.POLL_INTERVAL)
                    await self._poll_data()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error polling BTC context: {e}")
                    await asyncio.sleep(60)  # Retry in 1 min
        finally:
            await self._close_session()
            logger.info("BTCMarketContextFeed stopped")
    
    async def stop(self):
        """Stop background polling."""
        logger.info("Stopping BTCMarketContextFeed...")
        self.is_running = False
        await self._close_session()
    
    async def _poll_data(self):
        """Fetch latest data and update context."""
        try:
            candles = await self._fetch_candles()
            
            if candles is not None and len(candles) > 0:
                self._candles_1h = candles
                self._update_context()
                logger.debug(f"BTC context updated: {self._context.trend_bias} (strength={self._context.trend_strength:.2f})")
            else:
                # Mark as stale
                self._check_stale()
                
        except Exception as e:
            logger.error(f"Error in _poll_data: {e}")
            self._check_stale()
    
    async def _fetch_candles(self) -> Optional[pd.DataFrame]:
        """Fetch 1h candles from exchange."""
        try:
            if self.exchange in ['kraken', 'krakenfutures']:
                return await self._fetch_kraken_candles()
            elif self.exchange == 'binance':
                return await self._fetch_binance_candles()
        except Exception as e:
            logger.error(f"Error fetching candles: {e}")
        return None
    
    async def _fetch_kraken_candles(self) -> Optional[pd.DataFrame]:
        """Fetch from Kraken Futures."""
        try:
            session = await self._get_session()
            
            # Kraken Futures uses different symbol format
            symbol_map = {
                'BTC/USD': 'PI_XBTUSD',
                'BTCUSD': 'PI_XBTUSD',
                'BTC/USD:USD': 'PI_XBTUSD',
            }
            kraken_symbol = symbol_map.get(self.symbol, 'PI_XBTUSD')
            
            # Get 1h candles
            # since = timestamp in seconds
            since = int((datetime.now(UTC) - timedelta(hours=self.CANDLE_LOOKBACK)).timestamp())
            
            url = f"https://futures.kraken.com/api/charts/v1/trade/{kraken_symbol}/1h?from={since}"
            
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candles = data.get('candles', [])
                    
                    if candles:
                        df = pd.DataFrame(candles)
                        # Kraken format: time, open, high, low, close, volume
                        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df = df.sort_values('timestamp').reset_index(drop=True)
                        
                        # Convert to float
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            df[col] = df[col].astype(float)
                        
                        logger.debug(f"Fetched {len(df)} candles from Kraken")
                        return df
                else:
                    logger.warning(f"Kraken API returned {resp.status}")
            
        except Exception as e:
            logger.error(f"Error fetching Kraken candles: {e}")
        
        return None
    
    async def _fetch_binance_candles(self) -> Optional[pd.DataFrame]:
        """Fetch from Binance Futures."""
        try:
            session = await self._get_session()
            
            symbol_binance = self.symbol.replace('/', '').upper() + 'T'  # BTCUSDT
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol_binance}&interval=1h&limit={self.CANDLE_LOOKBACK}"
            
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if data:
                        df = pd.DataFrame(data, columns=[
                            'timestamp', 'open', 'high', 'low', 'close', 'volume',
                            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                            'taker_buy_quote', 'ignore'
                        ])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                        
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            df[col] = df[col].astype(float)
                        
                        logger.debug(f"Fetched {len(df)} candles from Binance")
                        return df
                        
        except Exception as e:
            logger.error(f"Error fetching Binance candles: {e}")
        
        return None
    
    def _update_context(self):
        """Update BTCMarketContext from candle data."""
        df = self._candles_1h
        
        if df is None or len(df) < self.EMA_LONG:
            logger.warning(f"Insufficient candle data: {len(df) if df is not None else 0}")
            return
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # Calculate EMAs
        ema20 = self._ema(closes, self.EMA_SHORT)
        ema50 = self._ema(closes, self.EMA_MID)
        ema200 = self._ema(closes, self.EMA_LONG)
        
        # Calculate ATR
        atr = self._atr(highs, lows, closes, self.ATR_PERIOD)
        
        # Latest values
        last_price = closes[-1]
        ema20_val = ema20[-1]
        ema50_val = ema50[-1]
        ema200_val = ema200[-1]
        atr_val = atr[-1] if len(atr) > 0 else 0
        
        # Calculate EMA slope (% change over last 5 candles)
        if len(ema20) >= 6:
            ema_slope = (ema20[-1] - ema20[-6]) / ema20[-6]
        else:
            ema_slope = 0.0
        
        # Determine trend bias
        trend_bias, trend_strength = self._determine_trend(
            last_price, ema20_val, ema50_val, ema200_val, ema_slope
        )
        
        # Determine volatility regime
        vol_regime = self._determine_volatility_regime(atr_val, last_price)
        
        # Update context
        self._context = BTCMarketContext(
            last_price=last_price,
            ema20_1h=ema20_val,
            ema50_1h=ema50_val,
            ema200_1h=ema200_val,
            trend_bias=trend_bias,
            trend_strength=trend_strength,
            ema_slope_1h=ema_slope,
            atr_1h=atr_val,
            volatility_regime=vol_regime,
            last_update=datetime.now(UTC),
            is_stale=False
        )
    
    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Calculate EMA."""
        if len(data) < period:
            return np.array([data[-1]] if len(data) > 0 else [0])
        
        alpha = 2 / (period + 1)
        ema = np.zeros(len(data))
        ema[0] = data[0]
        
        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
        
        return ema
    
    def _atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
        """Calculate ATR."""
        if len(closes) < 2:
            return np.array([0])
        
        # True Range
        tr = np.zeros(len(closes))
        tr[0] = highs[0] - lows[0]
        
        for i in range(1, len(closes)):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
        
        # ATR as EMA of TR
        return self._ema(tr, period)
    
    def _determine_trend(self, price: float, ema20: float, ema50: float, ema200: float,
                        slope: float) -> Tuple[str, float]:
        """
        Determine trend bias and strength.
        
        Returns:
            (bias, strength) where bias is BULLISH/BEARISH/NEUTRAL
            and strength is 0.0 to 1.0
        """
        score = 0.0
        
        # Price vs EMAs
        if price > ema20:
            score += 0.25
        if price > ema50:
            score += 0.25
        if price > ema200:
            score += 0.25
        
        # EMA alignment
        if ema20 > ema50 > ema200:
            score += 0.25
        elif ema20 < ema50 < ema200:
            score -= 0.25
        
        # Slope bonus
        if slope > 0.001:  # +0.1%
            score += 0.1
        elif slope < -0.001:
            score -= 0.1
        
        # Convert to bias
        if score >= 0.5:
            return "BULLISH", min(score, 1.0)
        elif score <= -0.25:
            return "BEARISH", min(abs(score), 1.0)
        else:
            return "NEUTRAL", 0.5 - abs(score)
    
    def _determine_volatility_regime(self, atr: float, price: float) -> str:
        """Determine volatility regime from ATR/price ratio."""
        if price <= 0:
            return "NORMAL"
        
        atr_pct = (atr / price) * 100  # ATR as % of price
        
        if atr_pct < 0.5:
            return "LOW"
        elif atr_pct < 1.5:
            return "NORMAL"
        elif atr_pct < 3.0:
            return "HIGH"
        else:
            return "EXTREME"
    
    def _check_stale(self):
        """Check if data is stale."""
        age = (datetime.now(UTC) - self._context.last_update).total_seconds()
        if age > self.STALE_THRESHOLD:
            self._context.is_stale = True
            logger.warning(f"BTC context stale: {age:.0f}s since last update")
    
    def get_context(self) -> BTCMarketContext:
        """Get current BTC market context."""
        self._check_stale()
        return self._context
    
    def get_trend_bias(self) -> str:
        """Get current trend bias (BULLISH/BEARISH/NEUTRAL)."""
        return self._context.trend_bias
    
    def is_aligned_with(self, bias: str) -> bool:
        """
        Check if BTC trend is aligned with given bias.
        
        Args:
            bias: 'LONG' or 'SHORT'
        
        Returns:
            True if aligned or neutral, False if opposing
        """
        if self._context.is_stale:
            return True  # Don't block on stale data
        
        btc_trend = self._context.trend_bias
        
        if btc_trend == "NEUTRAL":
            return True
        
        if bias.upper() == "LONG":
            return btc_trend != "BEARISH"
        elif bias.upper() == "SHORT":
            return btc_trend != "BULLISH"
        
        return True
    
    def is_ready(self) -> bool:
        """Check if feed has valid data."""
        return (
            self._context.last_price > 0 and
            not self._context.is_stale
        )


# Singleton instance
_btc_context_feed: Optional[BTCMarketContextFeed] = None


def get_btc_context_feed(exchange: str = 'kraken',
                         symbol: str = 'BTC/USD',
                         enabled: bool = True) -> BTCMarketContextFeed:
    """Get or create singleton BTCMarketContextFeed."""
    global _btc_context_feed
    
    if _btc_context_feed is None:
        _btc_context_feed = BTCMarketContextFeed(
            exchange=exchange,
            symbol=symbol,
            enabled=enabled
        )
    
    return _btc_context_feed


def reset_btc_context_feed():
    """Reset singleton for testing."""
    global _btc_context_feed
    _btc_context_feed = None
