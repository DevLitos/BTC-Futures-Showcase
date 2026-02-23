"""
Position Manager - Manages open position lifecycle.

Manages:
- Trailing stops (hybrid: tight fast + wider slow)
- Partial exits (TP1 smart close)
- BOS invalidation detection
- Exhaustion detection & exit
- Time-based/stagnation exits
- Anticipation trade timeouts
- Momentum decay checks
- Funding/time-based forced exits

This module contains:
1. Pure utility functions (no state) - calculate_current_r, check_bos_against_position, etc.
2. PositionHealthChecker class - evaluates position health
3. PositionExitChecker class - determines if position should exit
4. MomentumDecayChecker class - detects dying momentum
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List
from enum import Enum
import numpy as np
import pandas as pd

from src.shared.indicators import calculate_atr, calculate_adx, calculate_rsi

logger = logging.getLogger(__name__)


# =========================================================================
# DATA CLASSES FOR EXIT DECISIONS
# =========================================================================

class ExitDecision(Enum):
    """Exit decision types returned by checkers."""
    HOLD = "HOLD"  # Keep position
    CLOSE = "CLOSE"  # Close immediately
    TIGHTEN = "TIGHTEN"  # Tighten stop loss


@dataclass
class ExitCheckResult:
    """Result from an exit checker."""
    decision: ExitDecision = ExitDecision.HOLD
    reason: str = ""
    new_stop: float = 0.0
    cooldown_minutes: int = 0
    extra_data: Dict[str, Any] = field(default_factory=dict)


# =========================================================================
# UTILITY FUNCTIONS - Extracted from SignalEngine
# =========================================================================

def calculate_current_r(position, current_price: float) -> float:
    """
    Calculate current R for a position.
    
    R = (current - entry) / initial_risk for LONG
    R = (entry - current) / initial_risk for SHORT
    """
    initial_risk = abs(position.entry_price - position.initial_stop_loss)
    if initial_risk <= 0:
        return 0.0
    
    if position.direction == 'LONG':
        return (current_price - position.entry_price) / initial_risk
    else:  # SHORT
        return (position.entry_price - current_price) / initial_risk


def check_bos_against_position(
    df_5m: pd.DataFrame,
    direction: str,
    lookback: int = 20,
    min_bos_atr_mult: float = 0.3  # 🔧 FIX #126: Reduced from 0.5 to 0.3 for faster detection
) -> Tuple[bool, float]:
    """
    Check if there's a Break of Structure against the position direction.
    
    🔧 FIX #126 (Feb 09, 2026): Unified BOS detection with ATR filter
    - Merged logic from filter_checks.py (ATR-filtered) with position_manager.py (returns level)
    - BOS must exceed min_bos_atr_mult × ATR to avoid noise
    
    For SHORT: If 5m makes new HH above recent highs → bearish thesis broken
    For LONG: If 5m makes new LL below recent lows → bullish thesis broken
    
    Args:
        df_5m: 5m candle DataFrame
        direction: Position direction ('LONG' or 'SHORT')
        lookback: Number of candles to look back for BOS (default 20)
        min_bos_atr_mult: Minimum ATR multiple for valid BOS (default 0.3)
    
    Returns:
        (has_bos, bos_level)
    """
    if df_5m is None or len(df_5m) < lookback:
        return False, 0.0
    
    # Use last N candles
    df = df_5m.iloc[-lookback:] if len(df_5m) > lookback else df_5m
    
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    current_high = highs[-1]
    current_low = lows[-1]
    
    # Calculate ATR for minimum threshold (avoid noise)
    if len(highs) > 1:
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1])
            )
        )
        atr = np.mean(tr[-14:]) if len(tr) >= 14 else np.mean(tr)
    else:
        atr = highs[-1] - lows[-1]  # Fallback
    
    min_bos_distance = atr * min_bos_atr_mult
    
    # Find previous swing points (excluding last candle)
    prev_highs = highs[:-1]
    prev_lows = lows[:-1]
    
    if direction == 'SHORT':
        # BOS against SHORT = new higher high
        prev_swing_high = np.max(prev_highs) if len(prev_highs) > 0 else current_high
        bos_distance = current_high - prev_swing_high
        if current_high > prev_swing_high and bos_distance >= min_bos_distance:
            return True, current_high
    else:  # LONG
        # BOS against LONG = new lower low
        prev_swing_low = np.min(prev_lows) if len(prev_lows) > 0 else current_low
        bos_distance = prev_swing_low - current_low
        if current_low < prev_swing_low and bos_distance >= min_bos_distance:
            return True, current_low
    
    return False, 0.0




# =========================================================================
# ANTICIPATION TRADE CHECKER - Timeout for anticipation entries
# =========================================================================

class AnticipationChecker:
    """
    Check if anticipation trade should timeout.
    
    Anticipation trades have a time limit - if they don't show MFE > 0.3R
    within the valid_until timestamp, we close to free capital.
    """
    
    MFE_THRESHOLD_R = 0.3
    
    @staticmethod
    def check_anticipation_timeout(
        position,
        trade_tags: Dict[str, Any],
        market_time: datetime
    ) -> ExitCheckResult:
        """
        Check if anticipation trade should be closed due to timeout.
        
        Returns:
            ExitCheckResult with CLOSE if timed out without expansion
        """
        result = ExitCheckResult()
        
        is_anticipation_trade = trade_tags.get('anticipation_entry', False)
        anticipation_valid_until = trade_tags.get('anticipation_valid_until', None)
        
        if not is_anticipation_trade or not anticipation_valid_until or not position:
            return result
        
        current_ts = market_time.timestamp()
        mfe_r = position.calculate_mfe_r() if hasattr(position, 'calculate_mfe_r') else 0
        
        # Check timeout: if past valid_until AND MFE < 0.3R → exit
        if current_ts > anticipation_valid_until and mfe_r < AnticipationChecker.MFE_THRESHOLD_R:
            result.decision = ExitDecision.CLOSE
            result.reason = "ANTICIPATION_TIMEOUT"
            result.cooldown_minutes = 1  # Short cooldown
            result.extra_data = {
                'mfe_r': mfe_r,
                'expired_since_sec': current_ts - anticipation_valid_until
            }
            logger.warning(f"⏰ ANTICIPATION TIMEOUT: Trade expired without expansion (MFE={mfe_r:.2f}R < 0.3R)")
        
        return result


# =========================================================================
# MOMENTUM DECAY CHECKER - ADX-based momentum monitoring
# =========================================================================

class MomentumDecayChecker:
    """
    Monitor momentum decay after entry.
    
    After 5 minutes in trade, if ADX drops significantly from entry ADX,
    momentum is dying and we should tighten stop to lock profits.
    """
    
    MIN_TIME_IN_TRADE_MIN = 5
    ADX_DROP_THRESHOLD = 5  # Points
    MIN_R_FOR_TIGHTEN = 0.3
    LOCK_R_MULTIPLIER = 0.5  # Lock half of current profit
    MIN_LOCK_R = 0.15
    
    @staticmethod
    def check_momentum_decay(
        position,
        current_price: float,
        market_time: datetime,
        current_adx: Optional[float]
    ) -> ExitCheckResult:
        """
        Check if momentum is decaying and stop should be tightened.
        
        Returns:
            ExitCheckResult with TIGHTEN if momentum decaying while in profit
        """
        result = ExitCheckResult()
        
        if not position or current_adx is None:
            return result
        
        if not hasattr(position, 'open_time') or not position.open_time:
            return result
        
        time_in_trade_min = (market_time - position.open_time).total_seconds() / 60
        
        # Only check after 5 minutes
        if time_in_trade_min < MomentumDecayChecker.MIN_TIME_IN_TRADE_MIN:
            return result
        
        # Store entry ADX if not yet stored
        if not hasattr(position, '_entry_adx') or position._entry_adx is None:
            position._entry_adx = current_adx
            return result
        
        # Check ADX drop
        if position._entry_adx and position._entry_adx > 0:
            adx_drop = position._entry_adx - current_adx
            
            if adx_drop >= MomentumDecayChecker.ADX_DROP_THRESHOLD:
                current_r = calculate_current_r(position, current_price)
                
                # Only tighten if in profit and not already at BE
                if current_r > MomentumDecayChecker.MIN_R_FOR_TIGHTEN and not getattr(position, 'be_moved', False):
                    initial_risk = abs(position.entry_price - position.initial_stop_loss)
                    lock_r = max(MomentumDecayChecker.MIN_LOCK_R, current_r * MomentumDecayChecker.LOCK_R_MULTIPLIER)
                    
                    if position.direction == 'LONG':
                        new_stop = position.entry_price + (initial_risk * lock_r)
                        if new_stop > position.trailing_stop:
                            result.decision = ExitDecision.TIGHTEN
                            result.reason = f"MOMENTUM_DECAY (ADX dropped {adx_drop:.1f}pts)"
                            result.new_stop = new_stop
                            result.extra_data = {
                                'adx_drop': adx_drop,
                                'lock_r': lock_r,
                                'current_r': current_r
                            }
                    else:  # SHORT
                        new_stop = position.entry_price - (initial_risk * lock_r)
                        if new_stop < position.trailing_stop:
                            result.decision = ExitDecision.TIGHTEN
                            result.reason = f"MOMENTUM_DECAY (ADX dropped {adx_drop:.1f}pts)"
                            result.new_stop = new_stop
                            result.extra_data = {
                                'adx_drop': adx_drop,
                                'lock_r': lock_r,
                                'current_r': current_r
                            }
        
        return result


# =========================================================================
# BOS INVALIDATION CHECKER - Structure break detection
# =========================================================================

class BOSInvalidationChecker:
    """
    Check if Break of Structure invalidates our thesis.
    
    If SHORT and price makes Higher High, or LONG and price makes Lower Low,
    the structural thesis is invalidated. Actions:
    - Close if near BE or small loss (|R| < threshold)
    - Tighten stop by 50% if in profit
    
    🔧 FIX #124 (Feb 09, 2026): Increased tolerance
    - Grace period 5→7 min: Give trade more time to develop
    - Close threshold 0.3→0.5R: Only close if losing significantly
    - Problem: BOS appearing 1 min after entry was closing trades prematurely
    """
    
    GRACE_PERIOD_MINUTES = 7.0  # FIX #124: 5→7 min
    CLOSE_THRESHOLD_R = 0.4  # FIX #124: 0.3→0.4R (only close if |R| < this)
    TIGHTEN_PCT = 0.5  # Tighten by 50%
    
    @staticmethod
    def check_bos_invalidation(
        position,
        has_bos_against: bool,
        current_price: float,
        market_time: datetime,
        df_5m: Optional[pd.DataFrame] = None
    ) -> ExitCheckResult:
        """
        Check if BOS should trigger exit or tightening.
        
        Returns:
            ExitCheckResult with CLOSE, TIGHTEN, or HOLD
        """
        result = ExitCheckResult()
        
        if not has_bos_against or not position:
            return result
        
        # Check grace period
        # 🔧 FIX (Jan 30, 2026): Position uses 'open_time', not 'entry_time'
        # Note: BOS GRACE PERIOD log suppressed to avoid spam (logs only at debug level)
        if hasattr(position, 'open_time') and position.open_time:
            trade_age_min = (market_time - position.open_time).total_seconds() / 60.0
            if trade_age_min < BOSInvalidationChecker.GRACE_PERIOD_MINUTES:
                logger.debug(f"🛡️ BOS GRACE PERIOD: Trade age {trade_age_min:.1f}m < {BOSInvalidationChecker.GRACE_PERIOD_MINUTES}m → IGNORING BOS")
                return result
        
        current_r = calculate_current_r(position, current_price)
        
        # Get BOS level for logging
        bos_level = 0.0
        if df_5m is not None and len(df_5m) > 0:
            if position.direction == 'SHORT':
                bos_level = df_5m['high'].iloc[-1]  # HH against short
            else:
                bos_level = df_5m['low'].iloc[-1]   # LL against long
        
        # Determine action based on current profit
        if -BOSInvalidationChecker.CLOSE_THRESHOLD_R < current_r < BOSInvalidationChecker.CLOSE_THRESHOLD_R:
            # Near breakeven or small loss - close immediately
            result.decision = ExitDecision.CLOSE
            result.reason = "BOS_INVALIDATION"
            result.cooldown_minutes = 5  # 5 min cooldown to avoid churning
            result.extra_data = {
                'bos_level': bos_level,
                'current_r': current_r
            }
            logger.warning(
                f"⛔ BOS INVALIDATION: {position.direction} thesis broken "
                f"(BOS @ {bos_level:.2f}) | Current R: {current_r:.2f} → CLOSE"
            )
            
        elif current_r >= BOSInvalidationChecker.CLOSE_THRESHOLD_R:
            # In profit - tighten stop by 50%
            current_trailing_distance = abs(current_price - position.trailing_stop)
            new_trailing_distance = current_trailing_distance * BOSInvalidationChecker.TIGHTEN_PCT
            
            if position.direction == 'LONG':
                new_stop = current_price - new_trailing_distance
                if new_stop > position.trailing_stop:
                    result.decision = ExitDecision.TIGHTEN
                    result.reason = "BOS_INVALIDATION_TIGHTEN"
                    result.new_stop = new_stop
                    result.extra_data = {
                        'bos_level': bos_level,
                        'current_r': current_r
                    }
            else:  # SHORT
                new_stop = current_price + new_trailing_distance
                if new_stop < position.trailing_stop:
                    result.decision = ExitDecision.TIGHTEN
                    result.reason = "BOS_INVALIDATION_TIGHTEN"
                    result.new_stop = new_stop
                    result.extra_data = {
                        'bos_level': bos_level,
                        'current_r': current_r
                    }
        
        return result


# =========================================================================
# EXHAUSTION TIGHTENING CHECKER - ATR-based stop tightening
# =========================================================================

class ExhaustionTighteningChecker:
    """
    Handle stop tightening when exhaustion signals are detected.
    
    MODERATE exhaustion (3 signals) → Tighten trailing stop using ATR-based calculation.
    """
    
    ATR_MULTIPLIER = 0.6  # Tighten to 0.6 * ATR from current price
    FALLBACK_TIGHTEN_PCT = 0.5  # Fallback: tighten by 50%
    
    @staticmethod
    def calculate_tightened_stop(
        position,
        current_price: float,
        atr: Optional[float],
        exhaustion_tightened: bool = False
    ) -> ExitCheckResult:
        """
        Calculate tightened stop for exhaustion warning.
        
        Args:
            position: Current position
            current_price: Current price
            atr: Current ATR value (5m)
            exhaustion_tightened: Whether already tightened this trade
        
        Returns:
            ExitCheckResult with TIGHTEN if should tighten, HOLD otherwise
        """
        result = ExitCheckResult()
        
        if exhaustion_tightened or not position:
            return result
        
        current_stop = position.trailing_stop
        
        if position.direction == 'LONG':
            if atr and atr > 0:
                new_stop = max(current_stop, current_price - (atr * ExhaustionTighteningChecker.ATR_MULTIPLIER))
            else:
                new_stop = current_stop + (current_price - current_stop) * ExhaustionTighteningChecker.FALLBACK_TIGHTEN_PCT
            
            if new_stop > current_stop:
                result.decision = ExitDecision.TIGHTEN
                result.reason = "EXHAUSTION_TIGHTEN"
                result.new_stop = new_stop
                result.extra_data = {
                    'old_stop': current_stop,
                    'atr_based': atr is not None and atr > 0
                }
        else:  # SHORT
            if atr and atr > 0:
                new_stop = min(current_stop, current_price + (atr * ExhaustionTighteningChecker.ATR_MULTIPLIER))
            else:
                new_stop = current_stop - (current_stop - current_price) * ExhaustionTighteningChecker.FALLBACK_TIGHTEN_PCT
            
            if new_stop < current_stop:
                result.decision = ExitDecision.TIGHTEN
                result.reason = "EXHAUSTION_TIGHTEN"
                result.new_stop = new_stop
                result.extra_data = {
                    'old_stop': current_stop,
                    'atr_based': atr is not None and atr > 0
                }
        
        return result


