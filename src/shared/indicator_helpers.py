"""
Indicator Helpers Module - Pure functions for technical analysis.

Extracted from SignalEngine to enable:
1. Unit testing of indicator calculations
2. Caching strategies at the module level
3. Reuse across different components

These are convenience wrappers around the core indicators module
with additional calculations specific to the trading system.

╔══════════════════════════════════════════════════════════════════════════════╗
║ 📝 REFACTORED - Feb 05, 2026                                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ CHANGES:                                                                     ║
║ - CachedIndicators class REMOVED (use application/live/indicator_cache.py)  ║
║ - calculate_atr_1m/calculate_atr_5m consolidated to aliases of _generic     ║
║ - get_indicator_cache() and singleton REMOVED (use app/live/indicator_cache)║
║ - ts_to_int() moved to src/shared/utils.py (SINGLE SOURCE)                  ║
║                                                                              ║
║ This module now contains ONLY pure helper functions for indicators.         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any

import numpy as np
import pandas as pd

from src.shared.indicators import (
    calculate_ema, calculate_atr, calculate_adx, 
    calculate_rsi, calculate_bollinger_bands
)

# =============================================================================
# TIMESTAMP UTILITIES (import from utils for compatibility)
# =============================================================================

def ts_to_int(ts) -> int:
    """
    Convert any timestamp (pd.Timestamp, int, float, datetime) to int (epoch ms).
    
    NOTE: This is kept here for backward compatibility.
    New code should import from src.shared.utils
    """
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        # pd.Timestamp.value is in nanoseconds, convert to ms
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        # datetime object
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0


# =============================================================================
# ATR CALCULATIONS
# =============================================================================

def calculate_atr_generic(df: pd.DataFrame, period: int = 14) -> float:
    """
    Calculate ATR on any timeframe DataFrame.
    
    This is the PRIMARY function - calculate_atr_1m and calculate_atr_5m
    are now aliases to this function for backward compatibility.
    """
    if df is None or len(df) < period + 1:
        return 0.0
    
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    atr = calculate_atr(highs, lows, closes, period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0


# Aliases for backward compatibility - both call calculate_atr_generic
calculate_atr_1m = calculate_atr_generic
calculate_atr_5m = calculate_atr_generic


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
    
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    # FIXED: calculate_adx returns (adx_values, adx_slope) tuple
    adx_values, _ = calculate_adx(highs, lows, closes, period)
    return float(adx_values[-1]) if not np.isnan(adx_values[-1]) else 20.0


def calculate_rsi_value(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate RSI value from DataFrame."""
    if df is None or len(df) < period + 1:
        return 50.0  # Default neutral
    
    closes = df['close'].to_numpy()
    rsi = calculate_rsi(closes, period)
    return float(rsi[-1]) if not np.isnan(rsi[-1]) else 50.0


def calculate_ema_values(df: pd.DataFrame) -> Dict[str, float | None]:
    """Calculate common EMA values (20, 50, 200)."""
    result: Dict[str, float | None] = {'ema20': None, 'ema50': None, 'ema200': None}
    
    if df is None or len(df) < 200:
        return result
    
    closes = df['close'].to_numpy()
    
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    
    result['ema20'] = float(ema20[-1]) if not np.isnan(ema20[-1]) else None
    result['ema50'] = float(ema50[-1]) if not np.isnan(ema50[-1]) else None
    result['ema200'] = float(ema200[-1]) if not np.isnan(ema200[-1]) else None
    
    return result


def calculate_bb_values(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> Dict[str, float | None]:
    """Calculate Bollinger Band values."""
    result: Dict[str, float | None] = {'upper': None, 'middle': None, 'lower': None, 'width': None}
    
    if df is None or len(df) < period:
        return result
    
    closes = df['close'].to_numpy()
    upper, middle, lower = calculate_bollinger_bands(closes, period, std_dev)
    
    if not np.isnan(upper[-1]) and not np.isnan(lower[-1]):
        result['upper'] = float(upper[-1])
        result['middle'] = float(middle[-1])
        result['lower'] = float(lower[-1])
        
        # Width as percentage of middle
        if middle[-1] > 0:
            result['width'] = (upper[-1] - lower[-1]) / middle[-1] * 100
    
    return result


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


# =============================================================================
# ⛔ DEPRECATED CACHE FUNCTIONS - Use application/live/indicator_cache.py
# =============================================================================
# NOTE: get_cached_indicators, get_indicator_cache, reset_indicator_cache
# have been REMOVED. Use src/application/live/indicator_cache.py instead.
# CachedIndicators class has also been removed from this file.

# For backward compatibility, import from shared/indicator_cache.py which
# will issue a DeprecationWarning.

