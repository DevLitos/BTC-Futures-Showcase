"""
Indicator Cache - Centralized caching for technical indicators

Created: Jan 27, 2026 (AUDIT REFACTOR - Point 3)

Problem: ATR, ADX, RSI, EMA calculated 7+ times per tick instead of once.
Solution: Cache by timeframe, invalidate only when NEW candle CLOSES.

Key insight: Use candles[-2] timestamp (last CLOSED candle) as cache key,
not candles[-1] (still forming candle).
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from src.shared.indicators import (
    calculate_atr, calculate_adx, calculate_ema, calculate_rsi
)

logger = logging.getLogger(__name__)


@dataclass
class CachedIndicators:
    """Cached indicator values for a single timeframe"""
    last_closed_ts: int = 0  # Timestamp of last CLOSED candle (cache key)
    
    # ATR
    atr: float = 0.0
    atr_values: Optional[np.ndarray] = None
    
    # ADX
    adx: float = 20.0
    adx_slope: float = 0.0
    adx_values: Optional[np.ndarray] = None
    
    # EMAs
    ema9: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    
    # RSI
    rsi: float = 50.0
    rsi_slope: float = 0.0
    rsi_values: Optional[np.ndarray] = None
    
    # Bollinger Bands
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0
    
    # DataFrame cache (avoid repeated pd.DataFrame() calls)
    df: Optional[pd.DataFrame] = None


class IndicatorCache:
    """
    Centralized indicator caching system.
    
    Only recalculates when a new candle CLOSES (not forms).
    Key: Use candles[-2] timestamp as cache key (last CLOSED candle).
    
    Usage:
        cache = get_indicator_cache()
        atr_5m = cache.get_atr('5m', candles_5m)
        adx_1h, adx_slope = cache.get_adx('1h', candles_1h)
    """
    
    def __init__(self):
        self._cache: Dict[str, CachedIndicators] = {
            '1m': CachedIndicators(),
            '5m': CachedIndicators(),
            '15m': CachedIndicators(),
            '1h': CachedIndicators(),
            '4h': CachedIndicators(),
        }
        self._hit_count = 0
        self._miss_count = 0
    
    def _get_closed_ts(self, candles: list) -> int:
        """Get timestamp of last CLOSED candle (candles[-2])"""
        if not candles or len(candles) < 2:
            return 0
        # candles[-1] is still forming, candles[-2] is last closed
        return int(candles[-2].get('timestamp', 0) or 0)
    
    def _is_cache_valid(self, timeframe: str, candles: list) -> bool:
        """Check if cache is still valid for this timeframe"""
        closed_ts = self._get_closed_ts(candles)
        return closed_ts == self._cache[timeframe].last_closed_ts and closed_ts > 0
    
    def _update_cache_ts(self, timeframe: str, candles: list):
        """Update cache timestamp"""
        self._cache[timeframe].last_closed_ts = self._get_closed_ts(candles)
    
    def _get_df(self, timeframe: str, candles: list) -> pd.DataFrame:
        """Get or create DataFrame for timeframe"""
        if self._is_cache_valid(timeframe, candles) and self._cache[timeframe].df is not None:
            return self._cache[timeframe].df
        
        df = pd.DataFrame(candles)
        self._cache[timeframe].df = df
        return df
    
    # =========================================================================
    # ATR
    # =========================================================================
    
    def get_atr(self, timeframe: str, candles: list, period: int = 14) -> float:
        """
        Get ATR for timeframe, recalculating only if needed.
        
        Args:
            timeframe: '1m', '5m', '1h', etc.
            candles: List of candle dicts with 'high', 'low', 'close'
            period: ATR period (default 14)
        
        Returns:
            ATR value (float)
        """
        if timeframe not in self._cache:
            self._cache[timeframe] = CachedIndicators()
        
        if not candles or len(candles) < period + 1:
            return 0.0
        
        # Check cache
        if self._is_cache_valid(timeframe, candles) and self._cache[timeframe].atr > 0:
            self._hit_count += 1
            return self._cache[timeframe].atr
        
        self._miss_count += 1
        
        # Recalculate
        df = self._get_df(timeframe, candles)
        try:
            atr_values = calculate_atr(
                df['high'].to_numpy(),
                df['low'].to_numpy(),
                df['close'].to_numpy(),
                period
            )
            
            atr = float(atr_values[-1]) if len(atr_values) > 0 and not np.isnan(atr_values[-1]) else 0.0
            
            # Update cache
            self._cache[timeframe].atr = atr
            self._cache[timeframe].atr_values = atr_values
            self._update_cache_ts(timeframe, candles)
            
            return atr
            
        except Exception as e:
            logger.debug(f"ATR calculation error for {timeframe}: {e}")
            return 0.0
    
    def get_atr_values(self, timeframe: str, candles: list, period: int = 14) -> np.ndarray:
        """Get full ATR array (cached)"""
        # Ensure ATR is calculated
        self.get_atr(timeframe, candles, period)
        return self._cache[timeframe].atr_values if self._cache[timeframe].atr_values is not None else np.array([])
    
    # =========================================================================
    # ADX
    # =========================================================================
    
    def get_adx(self, timeframe: str, candles: list, period: int = 14) -> Tuple[float, float]:
        """
        Get ADX and slope for timeframe.
        
        Returns:
            (adx_value, adx_slope)
        """
        if timeframe not in self._cache:
            self._cache[timeframe] = CachedIndicators()
        
        if not candles or len(candles) < period + 1:
            return 20.0, 0.0
        
        # Check cache
        if self._is_cache_valid(timeframe, candles) and self._cache[timeframe].adx_values is not None:
            self._hit_count += 1
            return self._cache[timeframe].adx, self._cache[timeframe].adx_slope
        
        self._miss_count += 1
        
        # Recalculate
        df = self._get_df(timeframe, candles)
        try:
            adx_values, _ = calculate_adx(
                df['high'].to_numpy(),
                df['low'].to_numpy(),
                df['close'].to_numpy(),
                period
            )
            
            adx = float(adx_values[-1]) if len(adx_values) > 0 and not np.isnan(adx_values[-1]) else 20.0
            adx_slope = float(adx_values[-1] - adx_values[-2]) if len(adx_values) > 1 and not np.isnan(adx_values[-2]) else 0.0
            
            # Update cache
            self._cache[timeframe].adx = adx
            self._cache[timeframe].adx_slope = adx_slope
            self._cache[timeframe].adx_values = adx_values
            self._update_cache_ts(timeframe, candles)
            
            return adx, adx_slope
            
        except Exception as e:
            logger.debug(f"ADX calculation error for {timeframe}: {e}")
            return 20.0, 0.0
    
    # =========================================================================
    # EMAs
    # =========================================================================
    
    def get_emas(self, timeframe: str, candles: list) -> Tuple[float, float, float, float]:
        """
        Get EMA9, EMA20, EMA50, EMA200 for timeframe.
        
        Returns:
            (ema9, ema20, ema50, ema200)
        """
        if timeframe not in self._cache:
            self._cache[timeframe] = CachedIndicators()
        
        if not candles or len(candles) < 20:
            return 0.0, 0.0, 0.0, 0.0
        
        # Check cache - EMAs are valid if ema20 is set
        if self._is_cache_valid(timeframe, candles) and self._cache[timeframe].ema20 > 0:
            self._hit_count += 1
            return (
                self._cache[timeframe].ema9,
                self._cache[timeframe].ema20,
                self._cache[timeframe].ema50,
                self._cache[timeframe].ema200
            )
        
        self._miss_count += 1
        
        # Recalculate
        df = self._get_df(timeframe, candles)
        try:
            closes = df['close'].to_numpy()
            
            ema9 = float(calculate_ema(closes, 9)[-1]) if len(closes) >= 9 else 0.0
            ema20 = float(calculate_ema(closes, 20)[-1]) if len(closes) >= 20 else 0.0
            ema50 = float(calculate_ema(closes, 50)[-1]) if len(closes) >= 50 else 0.0
            ema200 = float(calculate_ema(closes, 200)[-1]) if len(closes) >= 200 else 0.0
            
            # Update cache
            self._cache[timeframe].ema9 = ema9
            self._cache[timeframe].ema20 = ema20
            self._cache[timeframe].ema50 = ema50
            self._cache[timeframe].ema200 = ema200
            self._update_cache_ts(timeframe, candles)
            
            return ema9, ema20, ema50, ema200
            
        except Exception as e:
            logger.debug(f"EMA calculation error for {timeframe}: {e}")
            return 0.0, 0.0, 0.0, 0.0
    
    def get_ema(self, timeframe: str, candles: list, period: int) -> float:
        """Get specific EMA period"""
        ema9, ema20, ema50, ema200 = self.get_emas(timeframe, candles)
        if period == 9:
            return ema9
        elif period == 20:
            return ema20
        elif period == 50:
            return ema50
        elif period == 200:
            return ema200
        else:
            # Calculate custom period
            if not candles or len(candles) < period:
                return 0.0
            df = self._get_df(timeframe, candles)
            closes = df['close'].to_numpy()
            return float(calculate_ema(closes, period)[-1])
    
    # =========================================================================
    # RSI
    # =========================================================================
    
    def get_rsi(self, timeframe: str, candles: list, period: int = 14) -> Tuple[float, float]:
        """
        Get RSI and slope for timeframe.
        
        Returns:
            (rsi_value, rsi_slope)
        """
        if timeframe not in self._cache:
            self._cache[timeframe] = CachedIndicators()
        
        if not candles or len(candles) < period + 5:
            return 50.0, 0.0
        
        # Check cache
        if self._is_cache_valid(timeframe, candles) and self._cache[timeframe].rsi_values is not None:
            self._hit_count += 1
            return self._cache[timeframe].rsi, self._cache[timeframe].rsi_slope
        
        self._miss_count += 1
        
        # Recalculate
        df = self._get_df(timeframe, candles)
        try:
            rsi_values = calculate_rsi(df['close'].to_numpy(), period)
            
            rsi = float(rsi_values[-1]) if len(rsi_values) > 0 and not np.isnan(rsi_values[-1]) else 50.0
            rsi_slope = float(rsi_values[-1] - rsi_values[-5]) if len(rsi_values) >= 5 and not np.isnan(rsi_values[-5]) else 0.0
            
            # Update cache
            self._cache[timeframe].rsi = rsi
            self._cache[timeframe].rsi_slope = rsi_slope
            self._cache[timeframe].rsi_values = rsi_values
            self._update_cache_ts(timeframe, candles)
            
            return rsi, rsi_slope
            
        except Exception as e:
            logger.debug(f"RSI calculation error for {timeframe}: {e}")
            return 50.0, 0.0
    
    # =========================================================================
    # Utility
    # =========================================================================
    
    def invalidate(self, timeframe: str | None = None):
        """
        Invalidate cache (force recalculation).
        
        Args:
            timeframe: Specific timeframe to invalidate, or None for all.
        """
        if timeframe:
            if timeframe in self._cache:
                self._cache[timeframe] = CachedIndicators()
        else:
            for tf in self._cache:
                self._cache[tf] = CachedIndicators()
    
    def get_stats(self) -> dict:
        """Get cache hit/miss statistics"""
        total = self._hit_count + self._miss_count
        hit_rate = (self._hit_count / total * 100) if total > 0 else 0
        return {
            'hits': self._hit_count,
            'misses': self._miss_count,
            'total': total,
            'hit_rate_pct': hit_rate
        }
    
    def reset_stats(self):
        """Reset hit/miss counters"""
        self._hit_count = 0
        self._miss_count = 0


# =========================================================================
# Singleton
# =========================================================================

_indicator_cache: Optional[IndicatorCache] = None


def get_indicator_cache() -> IndicatorCache:
    """Get singleton IndicatorCache instance"""
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = IndicatorCache()
    return _indicator_cache
