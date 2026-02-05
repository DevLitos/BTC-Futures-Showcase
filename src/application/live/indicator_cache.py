"""
Indicator Cache - Extracted from trading_app.py (Feb 04, 2026)

Caches technical indicators and only recalculates when candles close.
Dramatically reduces CPU usage by avoiding redundant calculations.
"""
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any, List
import logging

# 🔄 Fixed Feb 04: Use correct import paths from src.shared.indicators
from src.shared.indicators import (
    calculate_atr,
    calculate_adx,
    calculate_rsi,
    calculate_ema,
    calculate_bollinger_bands
)

logger = logging.getLogger(__name__)


class IndicatorCache:
    """
    Caches technical indicators with smart invalidation.
    
    Only recalculates indicators when the corresponding candle closes.
    Uses candles[-2] (last closed) as cache key, not candles[-1] (forming).
    """
    
    def __init__(self):
        """Initialize cache with empty values."""
        self._cache: Dict[str, Any] = {
            'last_candle_ts_1m': 0,
            'last_candle_ts_5m': 0,
            'last_candle_ts_1h': 0,
            'df_1m': None,
            'df_5m': None,
            'df_1h': None,
            'adx_1h': None,
            'atr_1m': None,
            'atr_ratio': None,
            'ema20_5m': None,
            'rsi_1m': None,
            'bb_width': None,
            'bb_avg': None,
            'vpoc': None,
        }
    
    def get_cached_indicators(
        self,
        candles_1m: Optional[List[dict]],
        candles_5m: Optional[List[dict]],
        candles_1h: Optional[List[dict]]
    ) -> Dict[str, Any]:
        """
        Get indicators with caching - only recalculate when candle CLOSES.
        
        Args:
            candles_1m: List of 1-minute candles
            candles_5m: List of 5-minute candles
            candles_1h: List of 1-hour candles
        
        Returns:
            Dict with: adx_1h, atr_ratio, ema20_5m, rsi_1m, bb_width, bb_avg, vpoc
            Plus df_1m, df_5m, df_1h DataFrames for further processing.
        """
        needs_1m_update = False
        needs_5m_update = False
        needs_1h_update = False
        
        # Check if LAST CLOSED candle changed (candles[-2] is the last closed one)
        if candles_1m and len(candles_1m) >= 2:
            closed_ts_1m = candles_1m[-2].get('timestamp', 0)
            if closed_ts_1m != self._cache['last_candle_ts_1m']:
                self._cache['last_candle_ts_1m'] = closed_ts_1m
                self._cache['df_1m'] = pd.DataFrame(candles_1m)
                needs_1m_update = True
        
        if candles_5m and len(candles_5m) >= 2:
            closed_ts_5m = candles_5m[-2].get('timestamp', 0)
            if closed_ts_5m != self._cache['last_candle_ts_5m']:
                self._cache['last_candle_ts_5m'] = closed_ts_5m
                self._cache['df_5m'] = pd.DataFrame(candles_5m)
                needs_5m_update = True
        
        if candles_1h and len(candles_1h) >= 2:
            closed_ts_1h = candles_1h[-2].get('timestamp', 0)
            if closed_ts_1h != self._cache['last_candle_ts_1h']:
                self._cache['last_candle_ts_1h'] = closed_ts_1h
                self._cache['df_1h'] = pd.DataFrame(candles_1h)
                needs_1h_update = True
        
        # Update 1h indicators (ADX)
        if needs_1h_update:
            self._update_1h_indicators()
        
        # Update 1m indicators (ATR, RSI, BB, VPOC)
        if needs_1m_update:
            self._update_1m_indicators()
        
        # Update 5m indicators (EMA20)
        if needs_5m_update:
            self._update_5m_indicators()
        
        return {
            'adx_1h': self._cache.get('adx_1h'),
            'atr_ratio': self._cache.get('atr_ratio'),
            'ema20_5m': self._cache.get('ema20_5m'),
            'rsi_1m': self._cache.get('rsi_1m'),
            'bb_width': self._cache.get('bb_width'),
            'bb_avg': self._cache.get('bb_avg'),
            'vpoc': self._cache.get('vpoc'),
            'df_1m': self._cache.get('df_1m'),
            'df_5m': self._cache.get('df_5m'),
            'df_1h': self._cache.get('df_1h'),
        }
    
    def _update_1h_indicators(self):
        """Update 1-hour indicators (ADX)."""
        df_1h = self._cache.get('df_1h')
        if df_1h is None or len(df_1h) < 20:
            return
        
        try:
            adx_arr, _ = calculate_adx(
                df_1h['high'].to_numpy(),
                df_1h['low'].to_numpy(),
                df_1h['close'].to_numpy()
            )
            if len(adx_arr) > 0 and not np.isnan(adx_arr[-1]):
                self._cache['adx_1h'] = float(adx_arr[-1])
                # 🔄 Added Feb 04: Store array for slope calculations
                self._cache['adx_1h_arr'] = adx_arr
        except Exception as e:
            logger.debug(f"ADX 1h calculation failed: {e}")
    
    def _update_1m_indicators(self):
        """Update 1-minute indicators (ATR, RSI, BB, VPOC)."""
        df_1m = self._cache.get('df_1m')
        if df_1m is None:
            return
        
        # ATR and ratio
        if len(df_1m) >= 20:
            try:
                atr_values = calculate_atr(
                    df_1m['high'].to_numpy(),
                    df_1m['low'].to_numpy(),
                    df_1m['close'].to_numpy()
                )
                if len(atr_values) >= 10:
                    self._cache['atr_1m'] = float(atr_values[-1])
                    avg_atr = float(np.mean(atr_values[-10:]))
                    self._cache['atr_ratio'] = self._cache['atr_1m'] / avg_atr if avg_atr > 0 else 1.0
            except Exception as e:
                logger.debug(f"ATR/ratio calculation failed: {e}")
        
        # RSI
        if len(df_1m) >= 14:
            try:
                closes = df_1m['close'].to_numpy()
                rsi_values = calculate_rsi(closes, 14)
                if len(rsi_values) > 0:
                    self._cache['rsi_1m'] = float(rsi_values[-1])
            except Exception as e:
                logger.debug(f"RSI calculation failed: {e}")
        
        # Bollinger Bands
        if len(df_1m) >= 20:
            try:
                closes = df_1m['close'].to_numpy()
                bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, period=20, std_dev=2)
                if len(bb_upper) > 0 and len(bb_lower) > 0:
                    self._cache['bb_width'] = float(bb_upper[-1] - bb_lower[-1])
                    if len(bb_upper) >= 20:
                        bb_widths = bb_upper[-20:] - bb_lower[-20:]
                        self._cache['bb_avg'] = float(np.mean(bb_widths))
                    else:
                        self._cache['bb_avg'] = self._cache['bb_width']
            except Exception as e:
                logger.debug(f"Bollinger Bands calculation failed: {e}")
        
        # VPOC
        if len(df_1m) >= 50:
            try:
                from src.domain.market.filters import calculate_vpoc
                df_for_vpoc = df_1m.tail(100) if len(df_1m) >= 100 else df_1m
                self._cache['vpoc'] = calculate_vpoc(df_for_vpoc)
            except Exception as e:
                logger.debug(f"VPOC calculation failed: {e}")
                self._cache['vpoc'] = None
    
    def _update_5m_indicators(self):
        """Update 5-minute indicators (EMA20, ADX)."""
        df_5m = self._cache.get('df_5m')
        if df_5m is None or len(df_5m) < 20:
            return
        
        try:
            closes_5m = df_5m['close'].to_numpy()
            ema_20 = calculate_ema(closes_5m, 20)
            if len(ema_20) > 0:
                self._cache['ema20_5m'] = float(ema_20[-1])
        except Exception as e:
            logger.debug(f"EMA20 5m calculation failed: {e}")
        
        # 🔄 Added Feb 04: Cache ADX 5m as well
        try:
            adx_5m_arr, _ = calculate_adx(
                df_5m['high'].to_numpy(),
                df_5m['low'].to_numpy(),
                df_5m['close'].to_numpy()
            )
            if len(adx_5m_arr) > 0 and not np.isnan(adx_5m_arr[-1]):
                self._cache['adx_5m'] = float(adx_5m_arr[-1])
                # Also store array for slope calculations
                self._cache['adx_5m_arr'] = adx_5m_arr
        except Exception as e:
            logger.debug(f"ADX 5m calculation failed: {e}")
    
    def get_atr_1m(self) -> Optional[float]:
        """Get cached ATR 1m value."""
        return self._cache.get('atr_1m')
    
    def get_adx_1h(self) -> Optional[float]:
        """Get cached ADX 1h value."""
        return self._cache.get('adx_1h')
    
    def get_adx_5m(self) -> Optional[float]:
        """Get cached ADX 5m value."""
        return self._cache.get('adx_5m')
    
    def get_adx_values(self, timeframe: str = '1h') -> Optional[np.ndarray]:
        """
        Get full ADX array for a timeframe.
        
        Useful for slope calculations and historical analysis.
        """
        key = f'adx_{timeframe}_arr'
        return self._cache.get(key)
    
    def get_all_adx(self) -> Dict[str, Optional[float]]:
        """
        Get all cached ADX values.
        
        Returns dict with adx_1h, adx_5m for easy access.
        Avoids recalculating ADX in multiple places.
        """
        return {
            'adx_1h': self._cache.get('adx_1h'),
            'adx_5m': self._cache.get('adx_5m'),
        }
    
    def invalidate(self):
        """Force invalidation of all cached values."""
        self._cache['last_candle_ts_1m'] = 0
        self._cache['last_candle_ts_5m'] = 0
        self._cache['last_candle_ts_1h'] = 0


# =============================================================================
# Singleton instance for global access
# =============================================================================
_indicator_cache: Optional[IndicatorCache] = None

def get_indicator_cache() -> IndicatorCache:
    """Get singleton instance of IndicatorCache."""
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = IndicatorCache()
    return _indicator_cache