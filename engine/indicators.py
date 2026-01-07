"""
Technical Indicators for BTC Perpetual Trading System
Implements all required indicators: EMA, ADX, ATR, VWAP, Fibonacci, Volume, CVD
"""

import logging
import numpy as np
from numba import jit
from typing import Tuple, Optional
import pandas as pd


logger = logging.getLogger(__name__)


@jit(nopython=True)
def calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return np.full(len(prices), np.nan)

    ema = np.full(len(prices), np.nan)
    ema[period - 1] = np.mean(prices[:period])
    alpha = 2.0 / (period + 1.0)

    for i in range(period, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1.0 - alpha)

    return ema


@jit(nopython=True)
def calculate_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Average True Range"""
    n = len(close)
    if n < period:
        return np.full(n, np.nan)

    tr = np.zeros(n)
    tr[0] = high[0] - low[0]

    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)

    atr = np.full(n, np.nan)
    atr[period - 1] = np.mean(tr[:period])

    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr


@jit(nopython=True)
def calculate_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
    """
    Calculate ADX (Average Directional Index) and its slope
    Returns: (adx_values, adx_slope)
    """
    n = len(close)
    if n < period + 1:
        return np.full(n, np.nan), np.full(n, np.nan)

    # Calculate True Range
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)

    # Calculate Directional Movement
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)

    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    # Smooth the indicators
    atr_smooth = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    dx = np.full(n, np.nan)
    adx = np.full(n, np.nan)

    # Initial values
    atr_smooth[period - 1] = np.mean(tr[:period])
    plus_dm_smooth = np.mean(plus_dm[:period])
    minus_dm_smooth = np.mean(minus_dm[:period])

    for i in range(period, n):
        atr_smooth[i] = (atr_smooth[i - 1] * (period - 1) + tr[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm[i]) / period

        if atr_smooth[i] > 0:
            plus_di[i] = 100 * plus_dm_smooth / atr_smooth[i]
            minus_di[i] = 100 * minus_dm_smooth / atr_smooth[i]

            di_sum = plus_di[i] + minus_di[i]
            if di_sum > 0:
                dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    # Calculate ADX
    adx_start = period * 2 - 1
    if n > adx_start:
        adx[adx_start] = np.mean(dx[period:adx_start + 1])
        for i in range(adx_start + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    # Calculate ADX slope
    adx_slope = np.full(n, np.nan)
    for i in range(1, n):
        if not np.isnan(adx[i]) and not np.isnan(adx[i - 1]):
            adx_slope[i] = adx[i] - adx[i - 1]

    return adx, adx_slope


def calculate_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate RSI (Relative Strength Index).

    Returns an array the same length as `prices`, filled with `np.nan` where
    insufficient history exists.
    """
    n = len(prices)
    if n < period + 1:
        return np.full(n, np.nan)

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    rsi = np.full(n, np.nan)

    # Wilder's smoothing
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    rs = np.inf if avg_loss == 0 else (avg_gain / avg_loss)
    rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        rs = np.inf if avg_loss == 0 else (avg_gain / avg_loss)
        rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def calculate_vwap(df: pd.DataFrame, anchor: str = 'D') -> pd.Series:
    """
    Calculate VWAP (Volume Weighted Average Price)
    anchor: 'D' for daily, 'W' for weekly
    """
    df = df.copy()
    
    # Ensure we have a proper timestamp index
    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', errors='coerce')
        df.set_index('timestamp', inplace=True)
    elif not isinstance(df.index, pd.DatetimeIndex):
        # Try to convert index to datetime if it's numeric
        try:
            df.index = pd.to_datetime(df.index, unit='ms', errors='coerce')
        except Exception as e:
            logger.warning(f"Could not convert index to datetime for VWAP calculation: {e}")
            # Fallback: return simple average price
            return pd.Series((df['high'] + df['low'] + df['close']) / 3, index=df.index)
    
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['tp_volume'] = df['typical_price'] * df['volume']

    # Group by anchor and calculate cumulative sums
    df['date'] = df.index.to_period(anchor)
    df['cumulative_tp_vol'] = df.groupby('date')['tp_volume'].cumsum()
    df['cumulative_vol'] = df.groupby('date')['volume'].cumsum()

    vwap = df['cumulative_tp_vol'] / df['cumulative_vol']
    return vwap


def calculate_fibonacci_levels(high_price: float, low_price: float) -> dict:
    """
    Calculate Fibonacci retracement levels
    Returns dictionary with levels: 0, 23.6, 38.2, 50, 61.8, 78.6, 100
    """
    diff = high_price - low_price
    return {
        '0.0': high_price,
        '23.6': high_price - 0.236 * diff,
        '38.2': high_price - 0.382 * diff,
        '50.0': high_price - 0.500 * diff,
        '61.8': high_price - 0.618 * diff,
        '78.6': high_price - 0.786 * diff,
        '100.0': low_price
    }


@jit(nopython=True)
def calculate_relative_volume(volume: np.ndarray, period: int = 20) -> np.ndarray:
    """
    Calculate relative volume (current volume / average volume)
    """
    n = len(volume)
    if n < period:
        return np.full(n, np.nan)

    rel_vol = np.full(n, np.nan)

    for i in range(period - 1, n):
        avg_vol = np.mean(volume[i - period + 1:i + 1])
        if avg_vol > 0:
            rel_vol[i] = volume[i] / avg_vol
        else:
            rel_vol[i] = 1.0

    return rel_vol


def calculate_cvd(df: pd.DataFrame) -> pd.Series:
    """
    Calculate Cumulative Volume Delta (CVD)
    Requires: close, volume columns
    Approximation based on price direction
    """
    df = df.copy()

    # Calculate direction: 1 for up, -1 for down, 0 for unchanged
    df['direction'] = np.where(df['close'] > df['close'].shift(1), 1,
                               np.where(df['close'] < df['close'].shift(1), -1, 0))

    # Volume delta: positive when price goes up, negative when down
    df['volume_delta'] = df['direction'] * df['volume']

    # Cumulative sum
    cvd = df['volume_delta'].cumsum()

    return cvd


def calculate_orderbook_imbalance(bids: list, asks: list, depth: int = 10) -> float:
    """
    Calculate order book imbalance ratio
    Returns: bid_pressure / (bid_pressure + ask_pressure) * 100
    Example: 60 means 60% bids, 40% asks
    """
    if not bids or not asks:
        return 50.0  # neutral

    bid_volume = sum([bid[1] for bid in bids[:depth]])
    ask_volume = sum([ask[1] for ask in asks[:depth]])

    total = bid_volume + ask_volume
    if total == 0:
        return 50.0

    return (bid_volume / total) * 100


@jit(nopython=True)
def detect_break_of_structure(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, 
                               lookback: int = 5, direction: str = 'long') -> bool:
    """
    Detect Break of Structure (BOS)
    For LONG: price breaks above recent high
    For SHORT: price breaks below recent low
    """
    n = len(closes)
    if n < lookback + 1:
        return False

    if direction == 'long':
        recent_high = np.max(highs[n - lookback - 1:n - 1])
        return closes[-1] > recent_high
    else:  # short
        recent_low = np.min(lows[n - lookback - 1:n - 1])
        return closes[-1] < recent_low


def check_hl_lh_structure(highs: np.ndarray, lows: np.ndarray, direction: str, periods: int = 3) -> bool:
    """
    Check if structure maintains Higher Lows (HL) for LONG or Lower Highs (LH) for SHORT
    """
    n = len(lows)
    if n < periods + 1:
        return False

    if direction == 'long':
        # Check for Higher Lows
        recent_lows = lows[-periods:]
        for i in range(1, len(recent_lows)):
            if recent_lows[i] < recent_lows[i - 1]:
                return False
        return True
    else:
        # Check for Lower Highs
        recent_highs = highs[-periods:]
        for i in range(1, len(recent_highs)):
            if recent_highs[i] > recent_highs[i - 1]:
                return False
        return True


@jit(nopython=True)
def calculate_price_velocity(prices: np.ndarray, period: int = 1) -> float:
    """
    Calculate price velocity (rate of change over period)
    """
    n = len(prices)
    if n < period + 1:
        return 0.0

    return abs(prices[-1] - prices[-(period + 1)])


def is_price_between_emas(price: float, ema_fast: float, ema_slow: float) -> bool:
    """Check if price is between two EMAs"""
    return min(ema_fast, ema_slow) <= price <= max(ema_fast, ema_slow)


def calculate_bollinger_bands(prices: np.ndarray, period: int = 20, std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Calculate Bollinger Bands
    Returns: (upper_band, middle_band, lower_band)
    """
    n = len(prices)
    if n < period:
        return np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    
    middle = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    
    for i in range(period - 1, n):
        window = prices[i - period + 1:i + 1]
        sma = np.mean(window)
        std = np.std(window)
        
        middle[i] = sma
        upper[i] = sma + std_dev * std
        lower[i] = sma - std_dev * std
    
    return upper, middle, lower