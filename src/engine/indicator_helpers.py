"""
Indicator Helpers Module - Pure functions for technical analysis.

Extracted from SignalEngine to enable:
1. Unit testing of indicator calculations
2. Caching strategies at the module level
3. Reuse across different components

These are convenience wrappers around the core indicators module
with additional calculations specific to the trading system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any

import numpy as np
import pandas as pd

from src.engine.indicators import (
    calculate_ema, calculate_atr, calculate_adx, 
    calculate_rsi, calculate_bollinger_bands
)


@dataclass
class CachedIndicators:
    """Container for cached indicator values."""
    timestamp_1m: int = 0
    timestamp_5m: int = 0
    timestamp_1h: int = 0
    
    # DataFrames
    df_1m: Optional[pd.DataFrame] = None
    df_5m: Optional[pd.DataFrame] = None
    df_1h: Optional[pd.DataFrame] = None
    
    # Calculated values
    atr_1m: Optional[float] = None
    atr_5m: Optional[float] = None
    atr_1h: Optional[float] = None
    atr_ratio: Optional[float] = None
    
    adx_1h: Optional[float] = None
    adx_5m: Optional[float] = None
    
    ema20_5m: Optional[float] = None
    ema50_1h: Optional[float] = None
    ema200_1h: Optional[float] = None
    
    rsi_1m: Optional[float] = None
    rsi_5m: Optional[float] = None
    
    bb_width: Optional[float] = None
    bb_avg: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    
    vpoc: Optional[float] = None


def ts_to_int(ts) -> int:
    """Convert any timestamp (pd.Timestamp, int, float, datetime) to int (epoch ms)."""
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        # pd.Timestamp.value is in nanoseconds, convert to ms
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        # datetime object
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0


def calculate_atr_1m(df_1m: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR on 1-minute timeframe."""
    if df_1m is None or len(df_1m) < period + 1:
        return 0.0
    
    highs = df_1m['high'].values
    lows = df_1m['low'].values
    closes = df_1m['close'].values
    
    atr = calculate_atr(highs, lows, closes, period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0


def calculate_atr_5m(df_5m: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR on 5-minute timeframe."""
    if df_5m is None or len(df_5m) < period + 1:
        return 0.0
    
    highs = df_5m['high'].values
    lows = df_5m['low'].values
    closes = df_5m['close'].values
    
    atr = calculate_atr(highs, lows, closes, period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0


def calculate_atr_generic(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR on any timeframe DataFrame."""
    if df is None or len(df) < period + 1:
        return 0.0
    
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    
    atr = calculate_atr(highs, lows, closes, period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0


def calculate_atr_ratio(df_1m: pd.DataFrame, df_5m: pd.DataFrame) -> float:
    """
    Calculate ATR ratio (1m ATR scaled to 5m vs actual 5m ATR).
    
    Ratio > 1.0 = micro volatility higher than expected
    Ratio < 1.0 = micro volatility lower than expected
    """
    atr_1m = calculate_atr_1m(df_1m)
    atr_5m = calculate_atr_5m(df_5m)
    
    if atr_5m <= 0:
        return 1.0
    
    # Scale 1m ATR to 5m equivalent (sqrt(5) scaling for volatility)
    scaled_1m = atr_1m * np.sqrt(5)
    return scaled_1m / atr_5m


def calculate_adx_value(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ADX value from DataFrame."""
    if df is None or len(df) < period * 2:
        return 20.0  # Default neutral
    
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    
    # FIXED: calculate_adx returns (adx_values, adx_slope) tuple
    adx_values, _ = calculate_adx(highs, lows, closes, period)
    return float(adx_values[-1]) if not np.isnan(adx_values[-1]) else 20.0


def calculate_rsi_value(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate RSI value from DataFrame."""
    if df is None or len(df) < period + 1:
        return 50.0  # Default neutral
    
    closes = df['close'].values
    rsi = calculate_rsi(closes, period)
    return float(rsi[-1]) if not np.isnan(rsi[-1]) else 50.0


def calculate_ema_values(df: pd.DataFrame) -> Dict[str, float]:
    """Calculate common EMA values (20, 50, 200)."""
    result = {'ema20': None, 'ema50': None, 'ema200': None}
    
    if df is None or len(df) < 200:
        return result
    
    closes = df['close'].values
    
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    
    result['ema20'] = float(ema20[-1]) if not np.isnan(ema20[-1]) else None
    result['ema50'] = float(ema50[-1]) if not np.isnan(ema50[-1]) else None
    result['ema200'] = float(ema200[-1]) if not np.isnan(ema200[-1]) else None
    
    return result


def calculate_bb_values(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> Dict[str, float]:
    """Calculate Bollinger Band values."""
    result = {'upper': None, 'middle': None, 'lower': None, 'width': None}
    
    if df is None or len(df) < period:
        return result
    
    closes = df['close'].values
    upper, middle, lower = calculate_bollinger_bands(closes, period, std_dev)
    
    if not np.isnan(upper[-1]) and not np.isnan(lower[-1]):
        result['upper'] = float(upper[-1])
        result['middle'] = float(middle[-1])
        result['lower'] = float(lower[-1])
        
        # Width as percentage of middle
        if middle[-1] > 0:
            result['width'] = (upper[-1] - lower[-1]) / middle[-1] * 100
    
    return result


def calculate_chase_atr(
    df_1m: pd.DataFrame,
    atr_1m: float,
    bias: str
) -> float:
    """
    Calculate chase ATR for anti-chase filter.
    
    Measures how far price has moved in the trade direction
    relative to ATR (catching moves already in progress).
    
    Args:
        df_1m: 1-minute DataFrame
        atr_1m: Current 1-minute ATR
        bias: 'LONG' or 'SHORT'
        
    Returns:
        Chase ATR ratio (1.0 = moved 1 ATR in direction)
    """
    if df_1m is None or len(df_1m) < 10 or atr_1m <= 0:
        return 0.0
    
    # Last 10 candles
    recent = df_1m.tail(10)
    start_price = recent.iloc[0]['close']
    current_price = recent.iloc[-1]['close']
    
    if bias == 'LONG':
        move = current_price - start_price
    else:
        move = start_price - current_price
    
    return move / atr_1m if atr_1m > 0 else 0.0


def find_support_resistance(
    bias: str,
    df_5m: pd.DataFrame,
    lookback: int = 50
) -> float:
    """
    Find support/resistance level based on bias.
    
    For LONG: Find recent swing low (support)
    For SHORT: Find recent swing high (resistance)
    
    Args:
        bias: 'LONG' or 'SHORT'
        df_5m: 5-minute DataFrame
        lookback: Number of candles to analyze
        
    Returns:
        Support/resistance price level
    """
    if df_5m is None or len(df_5m) < lookback:
        return 0.0
    
    recent = df_5m.tail(lookback)
    
    if bias == 'LONG':
        return float(recent['low'].min())
    else:
        return float(recent['high'].max())


def find_tp2_target(
    df_5m: pd.DataFrame,
    direction: str
) -> float:
    """
    Find TP2 target based on position direction.
    
    For LONG: TP2 = highest high of recent 5m candles
    For SHORT: TP2 = lowest low of recent 5m candles
    
    Args:
        df_5m: 5-minute DataFrame
        direction: Position direction ('LONG' or 'SHORT')
        
    Returns:
        TP2 target price
    """
    if df_5m is None or len(df_5m) == 0:
        return 0.0
    
    if direction == 'LONG':
        return float(df_5m['high'].max())
    else:
        return float(df_5m['low'].min())


def get_cached_indicators(
    cache: CachedIndicators,
    candles_1m: list,
    candles_5m: list,
    candles_1h: list
) -> CachedIndicators:
    """
    Update indicator cache if candle timestamps changed.
    
    This implements lazy recalculation - only compute indicators
    when the underlying data has changed.
    
    Args:
        cache: Existing cache to update
        candles_1m: 1-minute candle list
        candles_5m: 5-minute candle list
        candles_1h: 1-hour candle list
        
    Returns:
        Updated CachedIndicators
    """
    # Get current timestamps
    ts_1m = ts_to_int(candles_1m[-1]['timestamp']) if candles_1m else 0
    ts_5m = ts_to_int(candles_5m[-1]['timestamp']) if candles_5m else 0
    ts_1h = ts_to_int(candles_1h[-1]['timestamp']) if candles_1h else 0
    
    # Update 1m indicators if changed
    if ts_1m != cache.timestamp_1m and candles_1m:
        cache.timestamp_1m = ts_1m
        cache.df_1m = pd.DataFrame(candles_1m)
        cache.atr_1m = calculate_atr_1m(cache.df_1m)
        cache.rsi_1m = calculate_rsi_value(cache.df_1m)
    
    # Update 5m indicators if changed
    if ts_5m != cache.timestamp_5m and candles_5m:
        cache.timestamp_5m = ts_5m
        cache.df_5m = pd.DataFrame(candles_5m)
        cache.atr_5m = calculate_atr_5m(cache.df_5m)
        cache.adx_5m = calculate_adx_value(cache.df_5m)
        
        emas = calculate_ema_values(cache.df_5m)
        cache.ema20_5m = emas['ema20']
        
        bb = calculate_bb_values(cache.df_5m)
        cache.bb_width = bb['width']
        cache.bb_avg = bb['middle']
        cache.bb_upper = bb['upper']
        cache.bb_lower = bb['lower']
    
    # Update 1h indicators if changed
    if ts_1h != cache.timestamp_1h and candles_1h:
        cache.timestamp_1h = ts_1h
        cache.df_1h = pd.DataFrame(candles_1h)
        cache.atr_1h = calculate_atr_generic(cache.df_1h)
        cache.adx_1h = calculate_adx_value(cache.df_1h)
        
        emas = calculate_ema_values(cache.df_1h)
        cache.ema50_1h = emas['ema50']
        cache.ema200_1h = emas['ema200']
    
    # Update ratio if both timeframes available
    if cache.df_1m is not None and cache.df_5m is not None:
        cache.atr_ratio = calculate_atr_ratio(cache.df_1m, cache.df_5m)
    
    return cache


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_indicator_cache: Optional[CachedIndicators] = None


def get_indicator_cache() -> CachedIndicators:
    """Get or create the global indicator cache."""
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = CachedIndicators()
    return _indicator_cache


def reset_indicator_cache():
    """Reset the indicator cache (for testing)."""
    global _indicator_cache
    _indicator_cache = CachedIndicators()
