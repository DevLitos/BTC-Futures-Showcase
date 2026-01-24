"""
Technical Indicators for BTC Perpetual Trading System
Implements all required indicators: EMA, ADX, ATR, VWAP, Fibonacci, Volume, CVD
"""

import logging
import numpy as np
from numpy.typing import NDArray
from numba import jit
from typing import Any, Tuple, Optional, Union
import pandas as pd


logger = logging.getLogger(__name__)

# =====================================================================
# TYPE DEFINITIONS
# =====================================================================
# Internal type: what we work with after normalization (CLEAN)
NumericArray = NDArray[np.float64]

# Input type: what external callers can pass (BOUNDARY)
# NOTE: InputArray includes Any to support legacy call sites using `.values`.
# The Any is "burned" immediately in to_ndarray() - it NEVER propagates to core.
# TODO: Migrate callers to pass Series/ndarray directly and remove Any.
InputArray = Union[
    NDArray[np.float64],
    np.ndarray,
    pd.Series,
    pd.DataFrame,
    Any,  # For .values, ArrayLike, ExtensionArray - normalized immediately
]


def to_ndarray(x: Optional[InputArray]) -> NumericArray:
    """
    Normalize any array-like input to np.ndarray (float64).
    
    This is the SINGLE POINT OF CONVERSION at the boundary.
    All public functions call this before internal processing.
    The Any type is "burned" here - core functions only see NumericArray.
    
    Args:
        x: Any array-like input (pandas Series, DataFrame column, ndarray, list, etc.)
           Cannot be None - will raise ValueError.
        
    Returns:
        NumericArray: Normalized float64 ndarray for internal use
        
    Raises:
        ValueError: If input is None
    """
    if x is None:
        raise ValueError("Input cannot be None")
    
    # Fast path for ndarray (most common)
    if isinstance(x, np.ndarray):
        return x.astype(np.float64, copy=False) if x.dtype != np.float64 else x
    
    # Pandas objects - use to_numpy() which guarantees ndarray
    if hasattr(x, "to_numpy"):
        return x.to_numpy(dtype=np.float64)
    
    # Last resort for ArrayLike / Unknown / .values
    return np.asarray(x, dtype=np.float64)


@jit(nopython=True)
def _jit_calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """JIT-compiled EMA calculation (internal use)."""
    if len(prices) < period:
        return np.full(len(prices), np.nan)

    ema = np.full(len(prices), np.nan)
    ema[period - 1] = np.mean(prices[:period])
    alpha = 2.0 / (period + 1.0)

    for i in range(period, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1.0 - alpha)

    return ema


def calculate_ema(prices: InputArray, period: int) -> np.ndarray:
    """Calculate Exponential Moving Average. Accepts pd.Series or array-like."""
    return _jit_calculate_ema(to_ndarray(prices), period)


@jit(nopython=True)
def _jit_calculate_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """JIT-compiled ATR calculation (internal use)."""
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


def calculate_atr(
    high: InputArray,
    low: InputArray,
    close: InputArray,
    period: int = 14
) -> np.ndarray:
    """Calculate Average True Range. Accepts pd.Series or array-like."""
    return _jit_calculate_atr(to_ndarray(high), to_ndarray(low), to_ndarray(close), period)


@jit(nopython=True)
def _jit_calculate_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
    """JIT-compiled ADX calculation (internal use)."""
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


def calculate_adx(
    high: InputArray,
    low: InputArray,
    close: InputArray,
    period: int = 14
) -> Tuple[np.ndarray, np.ndarray]:
    """Calculate ADX and slope. Accepts pd.Series or array-like."""
    return _jit_calculate_adx(to_ndarray(high), to_ndarray(low), to_ndarray(close), period)


def _base_calculate_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Base RSI calculation (internal use)."""
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


def calculate_rsi(prices: InputArray, period: int = 14) -> np.ndarray:
    """Calculate RSI. Accepts pd.Series or array-like."""
    return _base_calculate_rsi(to_ndarray(prices), period)


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
    df['date'] = df.index.to_period(anchor)  # pyright: ignore[reportAttributeAccessIssue]
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


def _base_calculate_bollinger_bands(prices: np.ndarray, period: int = 20, std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Base Bollinger Bands calculation (internal use)."""
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


def calculate_bollinger_bands(
    prices: InputArray,
    period: int = 20,
    std_dev: float = 2.0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calculate Bollinger Bands. Accepts pd.Series or array-like."""
    return _base_calculate_bollinger_bands(to_ndarray(prices), period, std_dev)


# ============================================================================
# PULLBACK SHORT ENGINE - New Indicators (Jan 14, 2026)
# ============================================================================

def calculate_extension_z(
    current_price: float,
    ema_values: np.ndarray,
    atr_values: np.ndarray
) -> float:
    """
    Calculate Z-score of price extension from EMA in ATR units.
    
    extension_z = (price - EMA) / ATR
    
    This measures how "stretched" price is from the mean.
    - Positive = price above EMA (bullish extension)
    - Negative = price below EMA (bearish extension)
    - Values > 1.8 indicate significant extension
    
    Args:
        current_price: Current market price
        ema_values: EMA values array (uses last value)
        atr_values: ATR values array (uses last value)
    
    Returns:
        Extension Z-score (can be positive or negative)
    """
    if len(ema_values) == 0 or len(atr_values) == 0:
        return 0.0
    
    ema = ema_values[-1]
    atr = atr_values[-1]
    
    if np.isnan(ema) or np.isnan(atr) or atr <= 0:
        return 0.0
    
    return (current_price - ema) / atr


def detect_swing_high(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    lookback: int = 3
) -> Tuple[bool, float, int]:
    """
    Detect a confirmed swing high in price data.
    
    A swing high is a local maximum where:
    - The high is greater than the N highs before it
    - The high is greater than the N highs after it (confirmation)
    
    Args:
        highs: Array of high prices
        lows: Array of low prices  
        closes: Array of close prices
        lookback: Number of candles to look left and right
    
    Returns:
        Tuple of (is_confirmed, swing_high_price, candle_index)
        - is_confirmed: True if swing high detected and confirmed
        - swing_high_price: The high price at the swing
        - candle_index: Index of the swing high candle (from end)
    """
    n = len(highs)
    
    # Need at least lookback*2 + 1 candles for confirmation
    if n < lookback * 2 + 1:
        return False, 0.0, -1
    
    # Check each potential swing high (excluding edges)
    for i in range(lookback, n - lookback):
        candidate_high = highs[i]
        
        # Check left side (N candles before)
        left_valid = all(highs[j] < candidate_high for j in range(i - lookback, i))
        
        # Check right side (N candles after)
        right_valid = all(highs[j] < candidate_high for j in range(i + 1, i + lookback + 1))
        
        if left_valid and right_valid:
            # Swing high confirmed
            candles_from_end = n - 1 - i
            return True, candidate_high, candles_from_end
    
    return False, 0.0, -1


def detect_swing_low(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    lookback: int = 3
) -> Tuple[bool, float, int]:
    """
    Detect a confirmed swing low in price data.
    
    A swing low is a local minimum where:
    - The low is less than the N lows before it
    - The low is less than the N lows after it (confirmation)
    
    Args:
        highs: Array of high prices
        lows: Array of low prices
        closes: Array of close prices
        lookback: Number of candles to look left and right
    
    Returns:
        Tuple of (is_confirmed, swing_low_price, candle_index)
    """
    n = len(lows)
    
    if n < lookback * 2 + 1:
        return False, 0.0, -1
    
    for i in range(lookback, n - lookback):
        candidate_low = lows[i]
        
        left_valid = all(lows[j] > candidate_low for j in range(i - lookback, i))
        right_valid = all(lows[j] > candidate_low for j in range(i + 1, i + lookback + 1))
        
        if left_valid and right_valid:
            candles_from_end = n - 1 - i
            return True, candidate_low, candles_from_end
    
    return False, 0.0, -1


def detect_breakout_impulse(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    atr: float,
    body_atr_ratio: float = 0.6,
    close_near_high_ratio: float = 0.8,
    volume_expansion_ratio: float = 1.5,
    volume_ma_period: int = 20
) -> Tuple[bool, dict]:
    """
    Detect a breakout impulse candle that would VETO a pullback short.
    
    A breakout impulse is characterized by:
    1. Large body relative to ATR (momentum)
    2. Close near the high (bullish strength)
    3. Volume expansion (conviction)
    
    If detected, pullback shorts should be PROHIBITED for N candles.
    
    Args:
        opens: Array of open prices
        highs: Array of high prices
        lows: Array of low prices
        closes: Array of close prices
        volumes: Array of volumes
        atr: Current ATR value
        body_atr_ratio: Minimum body/ATR ratio for impulse
        close_near_high_ratio: Close must be in top X% of range
        volume_expansion_ratio: Volume must be X times SMA
        volume_ma_period: Period for volume SMA
    
    Returns:
        Tuple of (is_breakout_impulse, details_dict)
    """
    if len(closes) < 1 or atr <= 0:
        return False, {}
    
    # Current candle values
    open_price = opens[-1]
    high = highs[-1]
    low = lows[-1]
    close = closes[-1]
    volume = volumes[-1] if len(volumes) > 0 else 0
    
    # Calculate metrics
    body = abs(close - open_price)
    candle_range = high - low
    
    if candle_range == 0:
        return False, {}
    
    # 1. Body size check (impulse)
    body_ratio = body / atr
    is_impulse = body_ratio >= body_atr_ratio
    
    # 2. Close near high check (bullish strength)
    # For bullish impulse: close should be near high
    # distance_from_high = (high - close) / candle_range
    close_position = (close - low) / candle_range  # 0 = at low, 1 = at high
    is_bullish_close = close_position >= close_near_high_ratio
    
    # 3. Volume expansion check
    is_volume_expansion = False
    volume_ratio = 0.0
    if len(volumes) >= volume_ma_period and volume > 0:
        volume_ma = np.mean(volumes[-volume_ma_period:])
        if volume_ma > 0:
            volume_ratio = volume / volume_ma
            is_volume_expansion = volume_ratio >= volume_expansion_ratio
    
    # All conditions must be true for breakout impulse
    is_breakout = is_impulse and is_bullish_close and is_volume_expansion
    
    details = {
        'body_ratio': body_ratio,
        'close_position': close_position,
        'volume_ratio': volume_ratio,
        'is_impulse': is_impulse,
        'is_bullish_close': is_bullish_close,
        'is_volume_expansion': is_volume_expansion,
        'body': body,
        'range': candle_range,
        'atr': atr
    }
    
    return is_breakout, details  # pyright: ignore[reportReturnType]


def calculate_activity_score(
    atr_current: float,
    atr_avg: float,
    volume_current: float,
    volume_avg: float,
    velocity: float = 0.0,
    velocity_avg: float = 0.0
) -> float:
    """
    Calculate market activity score for adaptive TP targeting.
    
    Score ranges from 0.0 (dead market) to 1.0 (very active market).
    
    Components:
    - ATR ratio: current ATR vs average
    - Volume ratio: current volume vs average
    - Velocity (optional): price movement speed
    
    Args:
        atr_current: Current ATR value
        atr_avg: Average ATR value
        volume_current: Current volume
        volume_avg: Average volume
        velocity: Current price velocity (optional)
        velocity_avg: Average velocity (optional)
    
    Returns:
        Activity score between 0.0 and 1.0
    """
    scores = []
    
    # ATR component (0 to 1, capped at 2x average)
    if atr_avg > 0:
        atr_ratio = min(atr_current / atr_avg, 2.0) / 2.0
        scores.append(atr_ratio)
    
    # Volume component
    if volume_avg > 0:
        volume_ratio = min(volume_current / volume_avg, 2.0) / 2.0
        scores.append(volume_ratio)
    
    # Velocity component (optional)
    if velocity_avg > 0 and velocity > 0:
        velocity_ratio = min(velocity / velocity_avg, 2.0) / 2.0
        scores.append(velocity_ratio)
    
    if not scores:
        return 0.5  # Default to moderate activity
    
    # Average of components
    return sum(scores) / len(scores)


# =============================================================================
# Aliases for backwards compatibility - the main functions now accept ArrayLike
# =============================================================================

safe_calculate_ema = calculate_ema
safe_calculate_atr = calculate_atr
safe_calculate_adx = calculate_adx
safe_calculate_rsi = calculate_rsi
safe_calculate_bollinger_bands = calculate_bollinger_bands