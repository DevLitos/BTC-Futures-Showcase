"""Fetch historical OHLCV data from Kraken Futures API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt

from src.domain.models.candle import Candle

logger = logging.getLogger(__name__)


@dataclass
class HistoricalDataConfig:
    """Configuration for historical data fetching."""
    symbol: str = "BTC/USD:USD"
    exchange: str = "krakenfutures"
    timeframe: str = "5m"
    days_back: int = 30
    cache_dir: str = "storage/historical"


class KrakenHistoricalData:
    """Fetch and cache historical OHLCV data from Kraken Futures.
    
    Usage:
        fetcher = KrakenHistoricalData()
        candles = await fetcher.fetch(days=30, timeframe="5m")
    """
    
    TIMEFRAME_SECONDS = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400
    }
    
    def __init__(self, config: HistoricalDataConfig | None = None) -> None:
        self._config = config or HistoricalDataConfig()
        self._exchange: Any | None = None
        self._cache_dir = Path(self._config.cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
    
    async def _get_exchange(self) -> Any:
        """Get or create exchange instance."""
        if self._exchange is None:
            self._exchange = getattr(ccxt, self._config.exchange)({
                "enableRateLimit": True,
            })
        return self._exchange
    
    async def close(self) -> None:
        """Close exchange connection."""
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
    
    def _cache_path(self, timeframe: str, start: datetime, end: datetime) -> Path:
        """Generate cache file path."""
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")
        return self._cache_dir / f"{self._config.symbol.replace('/', '_')}_{timeframe}_{start_str}_{end_str}.csv"
    
    def _load_from_cache(self, path: Path) -> list[Candle] | None:
        """Load candles from cache file."""
        if not path.exists():
            return None
        
        candles: list[Candle] = []
        try:
            with open(path) as f:
                next(f)  # Skip header
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) >= 6:
                        ts = datetime.fromtimestamp(int(parts[0]) / 1000, tz=timezone.utc)
                        candles.append(Candle(
                            timestamp=ts,
                            open=float(parts[1]),
                            high=float(parts[2]),
                            low=float(parts[3]),
                            close=float(parts[4]),
                            volume=float(parts[5]),
                            symbol=self._config.symbol,
                            timeframe_minutes=self.TIMEFRAME_SECONDS.get(self._config.timeframe, 300) // 60
                        ))
            logger.info("[CACHE] Loaded %d candles from %s", len(candles), path)
            return candles if candles else None
        except Exception as e:
            logger.warning("[CACHE] Failed to load %s: %s", path, e)
            return None
    
    def _save_to_cache(self, path: Path, candles: list[Candle]) -> None:
        """Save candles to cache file."""
        try:
            with open(path, "w") as f:
                f.write("timestamp,open,high,low,close,volume\n")
                for c in candles:
                    f.write(f"{int(c.timestamp.timestamp() * 1000)},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
            logger.info("[CACHE] Saved %d candles to %s", len(candles), path)
        except Exception as e:
            logger.warning("[CACHE] Failed to save: %s", e)
    
    async def fetch(
        self,
        days: int | None = None,
        timeframe: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        use_cache: bool = True
    ) -> list[Candle]:
        """Fetch historical candle data.
        
        Args:
            days: Number of days back to fetch (default from config)
            timeframe: Candle timeframe (default from config)
            start: Start datetime (overrides days)
            end: End datetime (default: now)
            use_cache: Whether to use cached data
            
        Returns:
            List of Candle objects sorted by timestamp
        """
        days = days or self._config.days_back
        timeframe = timeframe or self._config.timeframe
        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=days))
        
        # Check cache first
        cache_path = self._cache_path(timeframe, start, end)
        if use_cache:
            cached = self._load_from_cache(cache_path)
            if cached:
                return cached
        
        logger.info("[FETCH] Downloading %s %s from %s to %s", 
                   self._config.symbol, timeframe, start.date(), end.date())
        
        exchange = await self._get_exchange()
        
        all_candles: list[Candle] = []
        current_since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        tf_minutes = self.TIMEFRAME_SECONDS.get(timeframe, 300) // 60
        
        while current_since < end_ms:
            try:
                ohlcv = await exchange.fetch_ohlcv(
                    self._config.symbol,
                    timeframe,
                    since=current_since,
                    limit=500  # Max per request
                )
                
                if not ohlcv:
                    break
                
                for row in ohlcv:
                    ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
                    if ts > end:
                        break
                    all_candles.append(Candle(
                        timestamp=ts,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        symbol=self._config.symbol,
                        timeframe_minutes=tf_minutes
                    ))
                
                # Move to next batch
                last_ts = ohlcv[-1][0]
                if last_ts <= current_since:
                    break
                current_since = last_ts + 1
                
                logger.info("[FETCH] Downloaded %d candles, latest: %s", 
                           len(all_candles), all_candles[-1].timestamp if all_candles else "none")
                
                # Rate limit pause
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error("[FETCH] Error: %s", e)
                await asyncio.sleep(2)
                continue
        
        # Sort by timestamp
        all_candles.sort(key=lambda c: c.timestamp)
        
        # Remove duplicates
        seen = set()
        unique_candles = []
        for c in all_candles:
            key = c.timestamp.timestamp()
            if key not in seen:
                seen.add(key)
                unique_candles.append(c)
        
        # Cache results
        if unique_candles and use_cache:
            self._save_to_cache(cache_path, unique_candles)
        
        logger.info("[FETCH] Complete: %d candles from %s to %s",
                   len(unique_candles),
                   unique_candles[0].timestamp if unique_candles else "N/A",
                   unique_candles[-1].timestamp if unique_candles else "N/A")
        
        return unique_candles


async def fetch_historical_data(days: int = 30, timeframe: str = "5m") -> list[Candle]:
    """Convenience function to fetch historical data.
    
    Example:
        candles = await fetch_historical_data(days=30, timeframe="5m")
    """
    fetcher = KrakenHistoricalData()
    try:
        return await fetcher.fetch(days=days, timeframe=timeframe)
    finally:
        await fetcher.close()
