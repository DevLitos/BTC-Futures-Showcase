"""
Buffer Multi-Timeframe Sincronizado
REGLA CRÍTICA: Solo recalcula indicadores cuando la vela CIERRA

Includes candle validation to prevent invalid data from corrupting calculations.
"""

import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, UTC
from typing import Dict, Optional, List
import logging

from engine.indicators import (
    calculate_ema, calculate_atr, calculate_adx,
    calculate_relative_volume, calculate_cvd,
    calculate_vwap
)
from engine.core_utils import (
    safe_float, validate_candle, to_datetime
)
from engine.time_provider import get_time_provider, TimeProvider

logger = logging.getLogger(__name__)


class SynchronizedMultiTimeframeBuffer:
    """
    Buffer que respeta el ciclo de vida de las velas:
    - Tick: CVD, velocidad
    - 1m cierra: ATR, vol relativo
    - 5m cierra: Estructura, EMAs
    - 1h cierra: Bias, ADX
    
    JAMÁS recalcules indicadores con velas incompletas
    """
    
    def __init__(self, symbol: str = 'BTC/USD', max_candles: int = 500, backtest_mode: bool = False):
        self.symbol = symbol
        self.max_candles = max_candles
        self.backtest_mode = backtest_mode
        
        # Use centralized TimeProvider for consistent timestamps
        time_mode = TimeProvider.MODE_BACKTEST if backtest_mode else TimeProvider.MODE_LIVE
        self._time_provider = get_time_provider(time_mode)
        
        # Market time - THE source of truth for all time-based logic
        # Updated with each candle/tick, synced with TimeProvider
        self.current_time: datetime = datetime.now(UTC)
        
        # Validation stats
        self._validation_stats = {'valid': 0, 'invalid': 0, 'fixed': 0}
        
        # Candle stores (OHLCV + indicadores)
        self.candles = {
            '1h': deque(maxlen=max_candles),
            '5m': deque(maxlen=max_candles),
            '1m': deque(maxlen=max_candles)
        }
        
        # Tick stream (para CVD en tiempo real)
        self.ticks = deque(maxlen=10000)
        
        # Trades (para CVD real)
        self.trades = deque(maxlen=50000)
        
        # Depth (para imbalance)
        self.depth_snapshots = deque(maxlen=5000)
        
        # Indicadores precalculados (NO vuelves a calcular)
        self.indicators = {
            '1h': {'ema50': None, 'ema200': None, 'adx': None, 'adx_slope': None, 'atr': None},
            '5m': {'ema20': None, 'ema50': None, 'atr': None, 'vwap': None},
            '1m': {'atr': None, 'rel_vol': None, 'cvd': None, 'velocity': 0.0}
        }
        
        # Real-time tracking
        self.last_price = 0.0
        
        # Candle completitud
        self.completed_candles = {'1h': 0, '5m': 0, '1m': 0}
        
        logger.info(f"SynchronizedMultiTimeframeBuffer created for {symbol}")
    
    def load_historical_candles(self, candles: list, timeframe: str):
        """Load historical candles into buffer (bootstrap)"""
        if timeframe not in self.candles:
            logger.warning(f"Invalid timeframe: {timeframe}")
            return
        
        count = 0
        for candle in candles:
            # Add timeframe if not present
            if 'timeframe' not in candle:
                candle['timeframe'] = timeframe
            
            self.candles[timeframe].append(candle)
            count += 1
            
            # Update last price from most granular history
            if timeframe in ['1m', '5m'] and candle.get('close'):
                 self.last_price = float(candle['close'])
        
        self.completed_candles[timeframe] += count
        
        # Recalculate indicators for this timeframe
        self._recalculate_indicators(timeframe)
        
        logger.info(f"✓ Loaded {count} historical {timeframe} candles (total: {len(self.candles[timeframe])})")
    
    def _recalculate_indicators(self, timeframe: str):
        """Recalculate indicators after loading historical data"""
        if timeframe == '1m':
            self._recalculate_1m_indicators()
        elif timeframe == '5m':
            self._recalculate_5m_indicators()
        elif timeframe == '1h':
            self._recalculate_1h_indicators()
    
    def add_tick(self, tick: dict):
        """Procesar tick (price update)"""
        self.ticks.append(tick)
        
        # Update last price
        if 'price' in tick:
            self.last_price = float(tick['price'])
        
        # Update CVD (solo acumula)
        self._update_cvd_from_tick(tick)
        
        # Update velocity
        self._update_velocity()
    
    def add_trade(self, trade: dict):
        """Procesar trade (para CVD real)"""
        self.trades.append(trade)
        
        # Update last price from trade as well (Robustness)
        if 'price' in trade:
            self.last_price = float(trade['price'])
            
        self._recalculate_cvd()
    
    def add_depth(self, depth: dict):
        """Procesar depth snapshot (para imbalance)"""
        self.depth_snapshots.append(depth)
    
    def add_candle(self, candle: dict):
        """
        Procesar candle CERRADO (1m, 5m, 1h)
        
        CRÍTICO:
        - Solo llama esto cuando la vela está COMPLETAMENTE CERRADA
        - El callback viene de WebSocket cuando arma la siguiente vela
        - Candles se validan antes de agregar
        """
        timeframe = candle.get('timeframe')
        
        if not timeframe or timeframe not in self.candles:
            logger.warning(f"Invalid timeframe: {timeframe}")
            return
        
        # VALIDATION: Check candle data before adding
        is_valid, errors = validate_candle(candle)
        if not is_valid:
            self._validation_stats['invalid'] += 1
            
            # Try to fix minor issues
            fixed_candle = self._try_fix_candle(candle)
            if fixed_candle:
                candle = fixed_candle
                self._validation_stats['fixed'] += 1
                logger.debug(f"Fixed candle: {errors}")
            else:
                logger.warning(f"Invalid candle rejected ({timeframe}): {errors}")
                return
        else:
            self._validation_stats['valid'] += 1
        
        # Agregar candle
        self.candles[timeframe].append(candle)
        self.completed_candles[timeframe] += 1
        
        # UPDATE MARKET TIME from candle timestamp (source of truth for all time logic)
        candle_ts = candle.get('timestamp')
        if candle_ts is not None:
            self.current_time = to_datetime(candle_ts)
            # Sync with centralized TimeProvider
            self._time_provider.set_market_time(self.current_time)
        
        # Update last price from candle close if we haven't seen ticks
        if self.last_price == 0.0 and candle.get('close'):
            self.last_price = float(candle['close'])
        
        # LOG: Only log 5m/1h at INFO, 1m at DEBUG to reduce spam
        current_counts = {
            '1m': len(self.candles['1m']),
            '5m': len(self.candles['5m']),
            '1h': len(self.candles['1h'])
        }
        
        if timeframe == '1m':
            status = "✓" if current_counts['1m'] >= 20 else "⏳"
            # 1m at DEBUG level to reduce spam - only visible with DEBUG logging
            logger.debug(f"{status} [1m] {current_counts['1m']}/20: O={candle['open']:.2f} H={candle['high']:.2f} "
                       f"L={candle['low']:.2f} C={candle['close']:.2f} V={candle['volume']:.0f}")
        elif timeframe == '5m':
            status = "✓" if current_counts['5m'] >= 50 else "⏳"
            logger.info(f"{status} [5m] O={candle['open']:.0f} C={candle['close']:.0f} ({current_counts['5m']}/50)")
        elif timeframe == '1h':
            status = "✓" if current_counts['1h'] >= 50 else "⏳"
            logger.info(f"{status} [1h] O={candle['open']:.0f} C={candle['close']:.0f} ({current_counts['1h']}/50)")
        
        # Recalcular indicadores SOLO si hay suficientes velas
        if timeframe == '1m':
            self._recalculate_1m_indicators()
        elif timeframe == '5m':
            self._recalculate_5m_indicators()
        elif timeframe == '1h':
            self._recalculate_1h_indicators()
    
    def _try_fix_candle(self, candle: dict) -> Optional[dict]:
        """
        Try to fix minor candle issues.
        
        Fixes:
        - Missing volume: set to 0
        - High < Low: swap them
        - High < Close: set high = close
        - Low > Close: set low = close
        
        Returns:
            Fixed candle dict or None if unfixable
        """
        fixed = candle.copy()
        
        # Fix missing volume
        if 'volume' not in fixed or fixed['volume'] is None:
            fixed['volume'] = 0.0
        
        # Get OHLC values
        o = safe_float(fixed.get('open'))
        h = safe_float(fixed.get('high'))
        l = safe_float(fixed.get('low'))
        c = safe_float(fixed.get('close'))
        
        # Check for completely missing data
        if o <= 0 or c <= 0:
            return None
        
        # Fix high/low if needed
        if h < l:
            fixed['high'], fixed['low'] = l, h
            h, l = l, h
        
        # Ensure high >= all prices
        max_price = max(o, h, l, c)
        if fixed['high'] < max_price:
            fixed['high'] = max_price
        
        # Ensure low <= all prices
        min_price = min(o, h, l, c)
        if fixed['low'] > min_price:
            fixed['low'] = min_price
        
        # Validate fix worked
        is_valid, _ = validate_candle(fixed)
        return fixed if is_valid else None
    
    def get_validation_stats(self) -> dict:
        """Get candle validation statistics."""
        return self._validation_stats.copy()
    
    def _recalculate_1m_indicators(self):
        """Recalcular indicadores de 1m"""
        if len(self.candles['1m']) < 20:
            return
        
        df = self._get_dataframe('1m')
        
        # ATR
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        
        atr = calculate_atr(highs, lows, closes, 14)[-1]
        self.indicators['1m']['atr'] = atr
        
        # Relative Volume
        volumes = df['volume'].values
        rel_vol = calculate_relative_volume(volumes, 20)[-1]
        self.indicators['1m']['rel_vol'] = rel_vol
        
        # Price velocity
        if len(closes) >= 2:
            self.indicators['1m']['velocity'] = abs(closes[-1] - closes[-2])
    
    def _recalculate_5m_indicators(self):
        """Recalcular indicadores de 5m"""
        if len(self.candles['5m']) < 50:
            return
        
        df = self._get_dataframe('5m')
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        volumes = df['volume'].values
        
        # EMAs
        ema20 = calculate_ema(closes, 20)[-1]
        ema50 = calculate_ema(closes, 50)[-1]
        
        self.indicators['5m']['ema20'] = ema20
        self.indicators['5m']['ema50'] = ema50
        
        # ATR
        atr = calculate_atr(highs, lows, closes, 14)[-1]
        self.indicators['5m']['atr'] = atr
        
        # VWAP (daily)
        vwap = calculate_vwap(df, anchor='D').iloc[-1]
        self.indicators['5m']['vwap'] = vwap
        
        logger.debug(f"5m indicators: EMA20={ema20:.2f} EMA50={ema50:.2f} ATR={atr:.2f} VWAP={vwap:.2f}")
    
    def _recalculate_1h_indicators(self):
        """Recalcular indicadores de 1h"""
        if len(self.candles['1h']) < 50:
            logger.warning("Not enough 1h candles for indicators (need 50+)")
            return
        
        df = self._get_dataframe('1h')
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # EMAs
        ema50 = calculate_ema(closes, 50)[-1]
        ema200 = calculate_ema(closes, 200)[-1]
        
        self.indicators['1h']['ema50'] = ema50
        self.indicators['1h']['ema200'] = ema200
        
        # ADX
        adx, adx_slope = calculate_adx(highs, lows, closes, 14)
        self.indicators['1h']['adx'] = adx[-1]
        self.indicators['1h']['adx_slope'] = adx_slope[-1] if len(adx_slope) > 0 else 0
        
        # ATR
        atr = calculate_atr(highs, lows, closes, 14)[-1]
        self.indicators['1h']['atr'] = atr
        
        logger.info(f"1h indicators: EMA50={ema50:.2f} EMA200={ema200:.2f} ADX={self.indicators['1h']['adx']:.2f} "
                   f"Slope={self.indicators['1h']['adx_slope']:.4f}")
    
    def _update_cvd_from_tick(self, tick: dict):
        """Actualizar CVD desde tick (aproximación)"""
        # CVD real se calcula desde trades en _recalculate_cvd()
        # Los ticks solo actualizan precio, no tienen info de dirección
        # Por lo tanto este método es un placeholder intencional
        return
    
    def _recalculate_cvd(self):
        """Recalcular CVD REAL desde trades"""
        if len(self.trades) < 2:
            return
        
        cvd = 0.0
        for trade in self.trades:
            if trade['is_buyer_maker']:
                # Buyer es maker = seller agressor = volumen negativo
                cvd -= trade['size']
            else:
                # Seller es maker = buyer agressor = volumen positivo
                cvd += trade['size']
        
        self.indicators['1m']['cvd'] = cvd
    
    def _update_velocity(self):
        """Actualizar velocity desde ticks"""
        if len(self.ticks) >= 2:
            latest = self.ticks[-1]['price']
            previous = self.ticks[-2]['price']
            self.indicators['1m']['velocity'] = abs(latest - previous)
    
    def _get_dataframe(self, timeframe: str) -> pd.DataFrame:
        """Convertir deque a DataFrame"""
        data = list(self.candles[timeframe])
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    
    def has_sufficient_data(self) -> bool:
        """Check if we have enough data"""
        return (len(self.candles['1h']) >= 50 and
                len(self.candles['5m']) >= 50 and
                len(self.candles['1m']) >= 20)
    
    def has_complete_data(self) -> bool:
        """Alias for has_sufficient_data - check if ready for trading"""
        return self.has_sufficient_data()
    
    def get_candles(self, timeframe: str):
        """Get candles as list of dicts or DataFrame"""
        df = self._get_dataframe(timeframe)
        if df is None or df.empty:
            return []
        # Return as list of dicts for easier consumption
        return df.to_dict('records')
    
    def get_indicators(self, timeframe: str) -> Dict:
        """Get calculated indicators"""
        return self.indicators.get(timeframe, {})
    
    def get_current_price(self) -> float:
        """Get last known price (Tick or Trade)"""
        if self.last_price > 0:
            return self.last_price
        
        # Fallback to last tick if last_price somehow 0 (legacy)
        if self.ticks:
            return self.ticks[-1]['price']
            
        return 0.0
    
    def get_market_time(self) -> datetime:
        """Get current market time (from last candle/tick timestamp)
        
        This is THE source of truth for all time-based logic.
        Use this instead of datetime.now() for:
        - Cooldown timers
        - Observation mode duration
        - Position duration
        - Any time comparison
        """
        return self.current_time
    
    def get_cvd(self) -> float:
        """Get current CVD"""
        return self.indicators['1m'].get('cvd', 0.0)
    
    def get_velocity(self) -> float:
        """Get current price velocity"""
        return self.indicators['1m'].get('velocity', 0.0)
    
    def get_candle_completion_status(self) -> Dict:
        """Get status of candle completions"""
        return {
            '1h': self.completed_candles['1h'],
            '5m': self.completed_candles['5m'],
            '1m': self.completed_candles['1m']
        }
