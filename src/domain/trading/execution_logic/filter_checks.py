"""
Filter Checks Module - Pure functions for entry/exit validation.

Extracted from SignalEngine to enable:
1. Unit testing of individual filters
2. Reuse across different engine implementations
3. Clear separation of concerns

All functions return (passed: bool, reason: str) tuples.
"""

from datetime import datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from src.shared.indicators import (
    calculate_ema, calculate_atr, calculate_adx, calculate_rsi
)


def check_trend_filter(
    df: pd.DataFrame,
    bias: str,
    current_price: float
) -> Tuple[bool, str]:
    """
    TREND FILTER: Only trade in direction of clear trend.
    
    LONG: Price > EMA200 AND EMA50 > EMA200
    SHORT: Price < EMA200 AND EMA50 < EMA200
    
    Args:
        df: DataFrame with OHLCV data
        bias: 'LONG' or 'SHORT'
        current_price: Current price
        
    Returns:
        (passed, reason) tuple
    """
    if len(df) < 200:
        return True, "Not enough data for trend filter"
    
    closes = df['close'].to_numpy()
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    
    if np.isnan(ema50[-1]) or np.isnan(ema200[-1]):
        return True, "EMA values not ready"
    
    allow_long = current_price > ema200[-1] and ema50[-1] > ema200[-1]
    allow_short = current_price < ema200[-1] and ema50[-1] < ema200[-1]
    
    if bias == 'LONG':
        if allow_long:
            return True, f"Trend OK: LONG aligned (P>{ema200[-1]:.0f}, EMA50>{ema200[-1]:.0f})"
        return False, f"LONG against trend: P={current_price:.0f}, EMA50={ema50[-1]:.0f}, EMA200={ema200[-1]:.0f}"
    else:
        if allow_short:
            return True, f"Trend OK: SHORT aligned (P<{ema200[-1]:.0f}, EMA50<{ema200[-1]:.0f})"
        return False, f"SHORT against trend: P={current_price:.0f}, EMA50={ema50[-1]:.0f}, EMA200={ema200[-1]:.0f}"


def check_momentum_filter(
    df: pd.DataFrame,
    bias: str
) -> Tuple[bool, str, str, float]:
    """
    MOMENTUM FILTER: RSI slope + ADX for trend confirmation.
    
    Returns momentum quality for position sizing (not hard veto).
    
    Args:
        df: DataFrame with OHLCV data
        bias: 'LONG' or 'SHORT'
        
    Returns:
        (passed, reason, momentum_quality, atr_value) tuple
        momentum_quality: 'STRONG', 'MODERATE', 'WEAK'
    """
    if len(df) < 50:
        return True, "Not enough data for momentum", "MODERATE", 100.0
    
    closes = df['close'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    
    # RSI with slope
    rsi = calculate_rsi(closes, 14)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-5]):
        return True, "RSI not ready", "MODERATE", 100.0
    
    rsi_val = rsi[-1]
    rsi_slope = (rsi[-1] - rsi[-5]) / 5  # 5-period slope
    
    # ADX for trend strength - FIXED: calculate_adx returns (adx_values, adx_slope) tuple
    adx_values, _ = calculate_adx(highs, lows, closes, 14)
    adx_val = adx_values[-1] if not np.isnan(adx_values[-1]) else 20.0
    
    # ATR for volatility context
    atr = calculate_atr(highs, lows, closes, 14)
    atr_val = atr[-1] if not np.isnan(atr[-1]) else 100.0
    
    # Momentum quality assessment
    issues = []
    momentum_quality = "STRONG"
    
    if bias == 'LONG':
        if rsi_slope < 0:
            issues.append("RSI falling")
            momentum_quality = "MODERATE"
        if rsi_val < 40:
            issues.append("RSI oversold territory")
            momentum_quality = "WEAK"
    else:  # SHORT
        if rsi_slope > 0:
            issues.append("RSI rising")
            momentum_quality = "MODERATE"
        if rsi_val > 60:
            issues.append("RSI overbought territory")
            momentum_quality = "WEAK"
    
    if adx_val < 20:
        issues.append(f"ADX weak ({adx_val:.1f})")
        momentum_quality = "WEAK"
    
    reason = f"Momentum {momentum_quality}: RSI={rsi_val:.1f} slope={rsi_slope:.1f} ADX={adx_val:.1f}"
    if issues:
        reason += f" ({', '.join(issues)})"
    
    return True, reason, momentum_quality, atr_val


def check_volatility_expansion(df: pd.DataFrame) -> Tuple[bool, str]:
    """
    VOLATILITY FILTER: ATR fast vs ATR slow.
    
    Only trade when BTC moves, not in compression.
    ATR(14) >= ATR(42) * 0.9
    
    Args:
        df: DataFrame with OHLCV data
        
    Returns:
        (passed, reason) tuple
    """
    if len(df) < 50:
        return True, "Not enough data for volatility"
    
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    atr_fast = calculate_atr(highs, lows, closes, 14)
    atr_slow = calculate_atr(highs, lows, closes, 42)
    
    if np.isnan(atr_fast[-1]) or np.isnan(atr_slow[-1]):
        return True, "ATR not ready"
    
    threshold = atr_slow[-1] * 0.9
    
    if atr_fast[-1] >= threshold:
        return True, f"Volatility OK: ATR14={atr_fast[-1]:.2f} >= {threshold:.2f} (90% of ATR42)"
    
    return False, f"Low volatility: ATR14={atr_fast[-1]:.2f} < {threshold:.2f}"


def check_anti_reversal(df: pd.DataFrame, bias: str) -> Tuple[bool, str]:
    """
    ANTI-REVERSO FILTER: Detect engulfing patterns + rejection wicks.
    
    Blocks entry if:
    - Engulfing bearish detected for LONG
    - Engulfing bullish detected for SHORT
    - Rejection wick > 60% of range
    
    Args:
        df: DataFrame with OHLCV data
        bias: 'LONG' or 'SHORT'
        
    Returns:
        (passed, reason) tuple
    """
    if len(df) < 3:
        return True, "Not enough data for reversal detection"
    
    last3 = df.iloc[-3:]
    
    # Candle properties
    open1, close1 = last3.iloc[-2]['open'], last3.iloc[-2]['close']
    open2, close2 = last3.iloc[-1]['open'], last3.iloc[-1]['close']
    high2, low2 = last3.iloc[-1]['high'], last3.iloc[-1]['low']
    
    candle_range = high2 - low2
    if candle_range <= 0:
        return True, "No range"
    
    # Engulfing patterns
    bearish_engulf = (close2 < open2 and  # Current bearish
                     close2 < open1 and   # Close below prev open
                     open2 > close1)      # Open above prev close
    
    bullish_engulf = (close2 > open2 and  # Current bullish
                     close2 > open1 and   # Close above prev open
                     open2 < close1)      # Open below prev close
    
    # Rejection wicks (upper wick for LONG rejection, lower for SHORT)
    upper_wick = high2 - max(open2, close2)
    lower_wick = min(open2, close2) - low2
    
    long_reject = upper_wick > candle_range * 0.6
    short_reject = lower_wick > candle_range * 0.6
    
    if bias == 'LONG':
        if bearish_engulf:
            return False, "Bearish engulfing detected - LONG blocked"
        if long_reject:
            return False, f"Long rejection wick ({upper_wick/candle_range*100:.0f}% of range)"
        return True, "No reversal patterns for LONG"
    else:
        if bullish_engulf:
            return False, "Bullish engulfing detected - SHORT blocked"
        if short_reject:
            return False, f"Short rejection wick ({lower_wick/candle_range*100:.0f}% of range)"
        return True, "No reversal patterns for SHORT"


def check_volume_delta(df: pd.DataFrame) -> Tuple[bool, str]:
    """
    DELTA VOLUME FILTER: Entry needs directional volume (intention).
    
    Delta = Buy Volume - Sell Volume (approximated from candle structure)
    If |delta| < threshold → NO TRADE (no clear direction)
    
    Args:
        df: DataFrame with OHLCV data (must include 'volume')
        
    Returns:
        (passed, reason) tuple
    """
    if len(df) < 25 or 'volume' not in df.columns:
        return True, "Not enough data for volume filter"
    
    volumes = df['volume'].to_numpy()
    opens = df['open'].to_numpy()
    closes = df['close'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    
    # Current candle delta
    current_vol = volumes[-1]
    current_range = highs[-1] - lows[-1]
    current_body = closes[-1] - opens[-1]  # Positive = bullish, negative = bearish
    
    if current_range <= 0:
        return True, "Zero range candle"
    
    # Delta = volume * (body / range) * sign
    body_ratio = abs(current_body) / current_range
    delta = current_vol * body_ratio * (1 if current_body > 0 else -1)
    
    # Average volume for normalization
    avg_vol_20 = float(np.mean(np.asarray(volumes[-21:-1])))
    if avg_vol_20 <= 0:
        return True, "Zero average volume"
    
    # Normalized delta (as ratio of average volume)
    norm_delta = abs(delta) / avg_vol_20
    
    # Threshold: need at least 0.3x average volume as clear delta
    DELTA_THRESHOLD = 0.3
    
    if norm_delta >= DELTA_THRESHOLD:
        direction = "BUY" if delta > 0 else "SELL"
        return True, f"Delta OK: {norm_delta:.2f}x avg ({direction})"
    
    return False, f"No intention: delta {norm_delta:.2f}x < {DELTA_THRESHOLD}x (indecision)"


def check_loss_cooldown(
    consecutive_losses: int,
    cooldown_until: Optional[datetime],
    market_time: datetime,
    max_consecutive_losses: int = 2,
    cooldown_minutes_tier1: int = 30,
    cooldown_minutes_tier2: int = 60
) -> Tuple[bool, str]:
    """
    LOSS COOLDOWN: Protection against chop regimes.
    
    - 2 consecutive losses → 30 min cooldown
    - 3+ consecutive losses → 60 min cooldown
    
    Args:
        consecutive_losses: Number of consecutive losses
        cooldown_until: Datetime when cooldown ends (or None)
        market_time: Current market time
        max_consecutive_losses: Losses to trigger first cooldown
        cooldown_minutes_tier1: Minutes for first tier cooldown
        cooldown_minutes_tier2: Minutes for second tier cooldown
        
    Returns:
        (passed, reason) tuple
    """
    # Check if in cooldown
    if cooldown_until and market_time < cooldown_until:
        remaining = (cooldown_until - market_time).total_seconds() / 60
        return False, f"Loss cooldown active: {remaining:.0f} min remaining ({consecutive_losses} consecutive losses)"
    
    return True, f"Loss cooldown OK ({consecutive_losses} consecutive losses)"


def check_atr_regime(
    df_5m: pd.DataFrame,
    min_threshold: float = 50.0,
    max_threshold: float = 500.0
) -> Tuple[bool, str]:
    """
    ATR REGIME FILTER: Block extremes (dead market or too volatile).
    
    Args:
        df_5m: 5-minute DataFrame with OHLCV data
        min_threshold: Minimum ATR to allow entry
        max_threshold: Maximum ATR to allow entry
        
    Returns:
        (passed, reason) tuple
    """
    if len(df_5m) < 20:
        return True, "Not enough data for ATR regime"
    
    highs = df_5m['high'].to_numpy()
    lows = df_5m['low'].to_numpy()
    closes = df_5m['close'].to_numpy()
    
    atr = calculate_atr(highs, lows, closes, 14)
    if np.isnan(atr[-1]):
        return True, "ATR not ready"
    
    atr_val = atr[-1]
    
    if atr_val < min_threshold:
        return False, f"Market too quiet: ATR5m={atr_val:.1f} < {min_threshold}"
    
    if atr_val > max_threshold:
        return False, f"Market too volatile: ATR5m={atr_val:.1f} > {max_threshold}"
    
    return True, f"ATR regime OK: {atr_val:.1f}"


def check_expected_rr(
    current_price: float,
    sl_distance: float,
    bias: str,
    df_1h: pd.DataFrame,
    min_rr: float = 1.5
) -> Tuple[bool, str]:
    """
    EXPECTED R:R FILTER: Only enter if target offers good R:R.
    
    Uses recent swing high/low as target estimation.
    
    Args:
        current_price: Current price
        sl_distance: Stop loss distance (absolute)
        bias: 'LONG' or 'SHORT'
        df_1h: 1-hour DataFrame for target estimation
        min_rr: Minimum required R:R ratio
        
    Returns:
        (passed, reason) tuple
    """
    if len(df_1h) < 20 or sl_distance <= 0:
        return True, "Not enough data for R:R calculation"
    
    # Estimate target from recent swing
    lookback = 20
    if bias == 'LONG':
        target = df_1h['high'].iloc[-lookback:].max()
        potential_reward = target - current_price
    else:
        target = df_1h['low'].iloc[-lookback:].min()
        potential_reward = current_price - target
    
    if potential_reward <= 0:
        return False, f"No upside for {bias}: target={target:.0f}, price={current_price:.0f}"
    
    expected_rr = potential_reward / sl_distance
    
    if expected_rr >= min_rr:
        return True, f"R:R OK: {expected_rr:.2f} (target={target:.0f}, SL_dist={sl_distance:.0f})"
    
    return False, f"Poor R:R: {expected_rr:.2f} < {min_rr} (target={target:.0f})"


def check_bos_against_position(
    direction: str,
    candles_5m: list,
    lookback: int = 20,
    min_bos_atr_mult: float = 0.5  # 🔥 FIX (Jan 27, 2026): BOS mínimo = 0.5 ATR
) -> bool:
    """
    Detect Break of Structure (BOS) against position.
    
    BOS against LONG: Price makes new Lower Low (breaks support)
    BOS against SHORT: Price makes new Higher High (breaks resistance)
    
    🔥 FIX (Jan 27, 2026): Added minimum ATR threshold to avoid triggering
    on noise. A true BOS should be at least 0.5 ATR beyond the previous swing.
    
    Args:
        direction: Position direction ('LONG' or 'SHORT')
        candles_5m: List of 5-minute candles
        lookback: Number of candles to analyze
        min_bos_atr_mult: Minimum ATR multiple for BOS to be valid (default 0.5)
        
    Returns:
        True if BOS detected against position
    """
    if not candles_5m or len(candles_5m) < lookback:
        return False
    
    recent = candles_5m[-lookback:]
    df = pd.DataFrame(recent)
    
    current_low = df['low'].iloc[-1]
    current_high = df['high'].iloc[-1]
    
    # 🔥 FIX: Calculate ATR for minimum threshold
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    # Simple ATR calculation (True Range average)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    atr = np.mean(tr[-14:]) if len(tr) >= 14 else np.mean(tr)  # 14-period ATR
    min_bos_distance = atr * min_bos_atr_mult
    
    # Find previous swing points (excluding last candle)
    prev_lows = df['low'].iloc[:-1]
    prev_highs = df['high'].iloc[:-1]
    
    if direction == 'LONG':
        # BOS against LONG = new lower low
        prev_swing_low = prev_lows.min()
        bos_distance = prev_swing_low - current_low
        # 🔥 FIX: Only valid BOS if distance > min_bos_distance
        return current_low < prev_swing_low and bos_distance >= min_bos_distance
    else:
        # BOS against SHORT = new higher high
        prev_swing_high = prev_highs.max()
        bos_distance = current_high - prev_swing_high
        # 🔥 FIX: Only valid BOS if distance > min_bos_distance
        return current_high > prev_swing_high and bos_distance >= min_bos_distance


def check_atr_regime_allows_entry(df_5m: pd.DataFrame) -> Tuple[bool, str]:
    """
    ATR REGIME FILTER: Block entries when ATR is already expanded.
    
    We want to enter during CONTRACTION, not EXPANSION.
    If ATR is above 50th percentile of recent ATR, the move already happened.
    """
    if df_5m is None or len(df_5m) < 50:
        return True, "Not enough data for ATR regime"
    
    highs = df_5m['high'].to_numpy()
    lows = df_5m['low'].to_numpy()
    closes = df_5m['close'].to_numpy()
    
    # Calculate True Range for each candle
    atrs = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        atrs.append(max(hl, hc, lc))
    
    if len(atrs) < 30:
        return True, "Not enough ATR history"
    
    current_atr = atrs[-1]
    lookback = min(50, len(atrs))
    recent_atrs = atrs[-lookback:]
    
    # Calculate 50th percentile (median)
    sorted_atrs = sorted(recent_atrs)
    p50_idx = int(len(sorted_atrs) * 0.50)
    atr_p50 = sorted_atrs[p50_idx]
    
    if current_atr > atr_p50:
        return False, f"ATR regime HIGH: {current_atr:.2f} > P50 {atr_p50:.2f} (move already happened)"
    
    return True, f"ATR regime OK: {current_atr:.2f} <= P50 {atr_p50:.2f}"


def check_volatility_minimum(df_1m: pd.DataFrame, min_ratio: float = 0.75) -> Tuple[bool, str]:
    """
    VOLATILITY MINIMUM FILTER: Block trades in low volatility regimes.
    
    Compares current ATR to its 200-period SMA.
    If atr_ratio < min_ratio, we're in a low-vol regime where sweeps don't expand.
    """
    if df_1m is None or len(df_1m) < 200:
        return True, "Not enough data for volatility filter"
    
    highs = df_1m['high'].to_numpy()
    lows = df_1m['low'].to_numpy()
    closes = df_1m['close'].to_numpy()
    
    # Calculate True Range for each candle
    atrs = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        atrs.append(max(hl, hc, lc))
    
    if len(atrs) < 200:
        return True, "Not enough ATR samples"
    
    # Current ATR (last 14 periods average)
    current_atr = sum(atrs[-14:]) / 14
    
    # SMA of ATR over last 200 periods
    atr_sma_200 = sum(atrs[-200:]) / 200
    
    if atr_sma_200 <= 0:
        return True, "ATR SMA is zero"
    
    atr_ratio = current_atr / atr_sma_200
    
    if atr_ratio < min_ratio:
        return False, f"LOW VOL REGIME: ATR ratio {atr_ratio:.2f} < {min_ratio} (chop zone)"
    
    return True, f"Volatility OK: ATR ratio {atr_ratio:.2f} >= {min_ratio}"


def check_not_post_impulse(
    df_1m: pd.DataFrame, 
    atr_1m: float,
    impulse_threshold: float = 1.2,
    lookback: int = 3
) -> Tuple[bool, str]:
    """
    ANTI-IMPULSE FILTER: Block entries after impulsive candles.
    
    If recent candles are > impulse_threshold ATR, we're entering AFTER the move.
    We want pullback/absorption, not chasing.
    """
    if df_1m is None or len(df_1m) < 5:
        return True, "Not enough 1m data"
    
    if atr_1m <= 0:
        return True, "Invalid ATR"
    
    for i in range(-lookback, 0):
        candle_range = abs(df_1m['close'].iloc[i] - df_1m['open'].iloc[i])
        if candle_range > atr_1m * impulse_threshold:
            return False, f"Post-impulse: Candle {i} range {candle_range:.2f} > {atr_1m * impulse_threshold:.2f} ({impulse_threshold}x ATR)"
    
    return True, "No recent impulse detected"


def check_sweep_occurred(
    df_5m: pd.DataFrame,
    bias: str,
    lookback: int = 10
) -> Tuple[bool, str]:
    """
    SWEEP FILTER: Only enter if there was a liquidity sweep recently.
    
    A sweep occurs when:
    - For LONG: Price went below recent lows (swept stops) then recovered
    - For SHORT: Price went above recent highs (swept stops) then recovered
    """
    if df_5m is None or len(df_5m) < lookback + 5:
        return True, "Not enough data for sweep detection"
    
    # Look at candles before the most recent ones
    prior_candles = df_5m.iloc[-(lookback+5):-5]
    recent_candles = df_5m.iloc[-5:]
    
    if bias == 'LONG':
        # For LONG sweep: recent low went below prior lows, then recovered
        prior_low = prior_candles['low'].min()
        sweep_low = recent_candles['low'].min()
        current_close = recent_candles['close'].iloc[-1]
        
        if sweep_low < prior_low and current_close > prior_low:
            return True, f"Sweep OK: Low {sweep_low:.2f} swept {prior_low:.2f}, recovered to {current_close:.2f}"
        return False, f"No LONG sweep: Low {sweep_low:.2f} vs prior {prior_low:.2f}"
    else:
        # For SHORT sweep: recent high went above prior highs, then recovered
        prior_high = prior_candles['high'].max()
        sweep_high = recent_candles['high'].max()
        current_close = recent_candles['close'].iloc[-1]
        
        if sweep_high > prior_high and current_close < prior_high:
            return True, f"Sweep OK: High {sweep_high:.2f} swept {prior_high:.2f}, recovered to {current_close:.2f}"
        return False, f"No SHORT sweep: High {sweep_high:.2f} vs prior {prior_high:.2f}"


# =============================================================================
# SINGLETON PATTERN
# =============================================================================

_filter_checks_instance = None


def get_filter_checks():
    """Get the filter checks module (singleton pattern for compatibility)."""
    global _filter_checks_instance
    if _filter_checks_instance is None:
        # Return module reference - all functions are module-level
        import src.domain.trading.execution_logic.filter_checks as module
