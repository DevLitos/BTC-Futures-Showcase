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


def calculate_smart_trailing_stop(
    position,
    current_price: float,
    atr: float,
    regime: str = 'RANGE'
) -> float:
    """
    Calculate smart trailing stop based on regime and position state.
    
    Hybrid approach:
    - EXPANSION/TREND: Wider trail (2.0 ATR) to let winners run
    - RANGE: Tighter trail (1.2 ATR) to lock in profits
    - After TP1 hit: Always tighter (0.8 ATR)
    
    Args:
        position: Current position
        current_price: Current market price
        atr: Current ATR value
        regime: Market regime ('RANGE', 'EXPANSION', 'TREND')
    
    Returns:
        New trailing stop price
    """
    if atr <= 0:
        return position.trailing_stop
    
    # Determine trail multiplier based on regime
    if regime in ('TREND_EXPANSION', 'EXPANSION', 'TRENDING'):
        trail_mult = 2.0  # Wider to let winners run
    else:
        trail_mult = 1.2  # Tighter for ranging markets
    
    # After TP1, always tighter
    if hasattr(position, 'tp1_hit') and position.tp1_hit:
        trail_mult = min(trail_mult, 0.8)
    
    # Calculate new stop
    trail_distance = atr * trail_mult
    
    if position.direction == 'LONG':
        new_stop = current_price - trail_distance
        # Never move stop backwards
        return max(new_stop, position.trailing_stop)
    else:  # SHORT
        new_stop = current_price + trail_distance
        # Never move stop backwards (for short, lower is better)
        return min(new_stop, position.trailing_stop)


def should_tighten_on_exhaustion(
    exhaustion_level: str,
    position,
    current_price: float,
    atr: float
) -> Tuple[bool, float]:
    """
    Check if trailing stop should be tightened due to exhaustion signals.
    
    MODERATE exhaustion (3 signals): Tighten to 0.6 ATR
    SEVERE exhaustion (4+ signals): Return signal to close
    
    Returns:
        (should_tighten, new_stop) - new_stop is 0 if should close
    """
    if exhaustion_level == 'SEVERE':
        return True, 0.0  # Signal to close
    
    if exhaustion_level == 'MODERATE':
        # Tighten to 0.6 ATR from current price
        tight_distance = atr * 0.6
        
        if position.direction == 'LONG':
            new_stop = max(current_price - tight_distance, position.trailing_stop)
        else:
            new_stop = min(current_price + tight_distance, position.trailing_stop)
        
        return True, new_stop
    
    return False, position.trailing_stop


def calculate_partial_close_size(
    position_size: float,
    current_r: float,
    partial_pct: float = 0.5
) -> float:
    """
    Calculate size to close for partial exit.
    
    Default: Close 50% at TP1
    
    Args:
        position_size: Current position size
        current_r: Current R multiple
        partial_pct: Percentage to close (0.0 - 1.0)
    
    Returns:
        Size to close
    """
    if current_r < 0.5:
        return 0.0  # Don't partial below 0.5R
    
    close_size = position_size * partial_pct
    
    # Ensure we don't close everything
    remaining = position_size - close_size
    min_remaining = 0.00001  # Minimum BTC to keep
    
    if remaining < min_remaining:
        close_size = position_size - min_remaining
    
    return max(close_size, 0.0)


def check_time_stagnation(
    position,
    market_time: datetime,
    atr: float,
    stagnation_threshold_r: float = 0.3,
    max_stagnation_minutes: int = 60
) -> Tuple[bool, str]:
    """
    Check if position is stagnating (not making progress).
    
    Stagnation = price stuck within threshold for too long without hitting target.
    
    Args:
        position: Current position
        market_time: Current market time
        atr: Current ATR
        stagnation_threshold_r: R threshold to consider stuck
        max_stagnation_minutes: Minutes before timing out
    
    Returns:
        (should_exit, reason)
    """
    # 🔧 FIX (Jan 30, 2026): Position uses 'open_time', not 'entry_time'
    if not hasattr(position, 'open_time') or not position.open_time:
        return False, ""
    
    # Calculate time in trade
    time_in_trade = (market_time - position.open_time).total_seconds() / 60.0
    
    if time_in_trade < max_stagnation_minutes:
        return False, ""
    
    # Check if making progress
    mfe_r = position.calculate_mfe_r() if hasattr(position, 'calculate_mfe_r') else 0.0
    
    if mfe_r < stagnation_threshold_r:
        return True, f"Stagnation: {time_in_trade:.0f}min without reaching {stagnation_threshold_r}R (MFE={mfe_r:.2f}R)"
    
    return False, ""


# =========================================================================
# POSITION LIFECYCLE CHECKS
# =========================================================================

class PositionHealthChecker:
    """
    Utility class to check position health and suggest actions.
    
    This is a stateless checker - it doesn't manage state, just evaluates.
    """
    
    @staticmethod
    def evaluate_position(
        position,
        current_price: float,
        market_time: datetime,
        df_5m: pd.DataFrame,
        atr_5m: float,
        regime: str = 'RANGE'
    ) -> dict:
        """
        Comprehensive position health check.
        
        Returns dict with:
        - current_r: Current R multiple
        - should_close: Whether to close
        - close_reason: Reason if closing
        - should_tighten: Whether to tighten stop
        - new_stop: New stop if tightening
        - has_bos: Whether BOS detected
        """
        result = {
            'current_r': 0.0,
            'should_close': False,
            'close_reason': '',
            'should_tighten': False,
            'new_stop': position.trailing_stop,
            'has_bos': False,
            'bos_level': 0.0
        }
        
        # 1. Calculate current R
        result['current_r'] = calculate_current_r(position, current_price)
        
        # 2. Check BOS invalidation (with GRACE PERIOD - same as BOSInvalidationChecker)
        has_bos, bos_level = check_bos_against_position(df_5m, position.direction)
        result['has_bos'] = has_bos
        result['bos_level'] = bos_level
        
        # 🔧 FIX #124 (Feb 09, 2026): Apply 7min grace period BEFORE acting on BOS
        # This prevents closing on pre-existing BOS detected immediately after entry
        # 🔧 FIX #125 (Feb 09, 2026): Use BOSInvalidationChecker constants to avoid inconsistency
        GRACE_PERIOD_MINUTES = BOSInvalidationChecker.GRACE_PERIOD_MINUTES
        CLOSE_THRESHOLD_R = BOSInvalidationChecker.CLOSE_THRESHOLD_R
        trade_age_min = 0.0
        within_grace = False
        # 🔧 FIX (Jan 30, 2026): Position uses 'open_time', not 'entry_time'
        if hasattr(position, 'open_time') and position.open_time:
            trade_age_min = (market_time - position.open_time).total_seconds() / 60.0
            within_grace = trade_age_min < GRACE_PERIOD_MINUTES
            # Note: BOS GRACE PERIOD log suppressed to avoid spam (logs only on first detection)
        
        if has_bos and not within_grace and -CLOSE_THRESHOLD_R < result['current_r'] < CLOSE_THRESHOLD_R:
            # Near breakeven with BOS = close (only outside grace period)
            result['should_close'] = True
            result['close_reason'] = f'BOS_INVALIDATION (R={result["current_r"]:.2f})'
        elif has_bos and not within_grace and result['current_r'] >= CLOSE_THRESHOLD_R:
            # In profit with BOS = tighten 50%
            result['should_tighten'] = True
            trail_dist = abs(current_price - position.trailing_stop) * 0.5
            if position.direction == 'LONG':
                result['new_stop'] = max(current_price - trail_dist, position.trailing_stop)
            else:
                result['new_stop'] = min(current_price + trail_dist, position.trailing_stop)
        
        # 3. Check stagnation
        if not result['should_close']:
            should_exit, reason = check_time_stagnation(position, market_time, atr_5m)
            if should_exit:
                result['should_close'] = True
                result['close_reason'] = f'TIME_STAGNATION: {reason}'
        
        return result


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


# =========================================================================
# POSITION MANAGER - Orchestrates all exit checks
# =========================================================================

class PositionManager:
    """
    Orchestrates all position management checks.
    
    This is the main class that trading_app.py should use.
    It runs all checkers in order and returns a consolidated result.
    """
    
    def __init__(self):
        self.anticipation_checker = AnticipationChecker()
        self.momentum_decay_checker = MomentumDecayChecker()
        self.bos_checker = BOSInvalidationChecker()
        self.health_checker = PositionHealthChecker()
    
    def run_exit_checks(
        self,
        position,
        current_price: float,
        market_time: datetime,
        trade_tags: Dict[str, Any],
        current_adx: Optional[float] = None,
        has_bos_against: bool = False,
        df_5m: Optional[pd.DataFrame] = None,
        atr_5m: Optional[float] = None
    ) -> ExitCheckResult:
        """
        Run all exit checks and return first CLOSE or TIGHTEN result.
        
        Checks run in order of urgency:
        1. Anticipation trade timeout
        2. BOS invalidation
        3. Momentum decay
        4. Position health (stagnation)
        
        Returns:
            ExitCheckResult - first non-HOLD result, or HOLD if all pass
        """
        if not position:
            return ExitCheckResult()
        
        # 1. Anticipation timeout
        anticipation_result = AnticipationChecker.check_anticipation_timeout(
            position, trade_tags, market_time
        )
        if anticipation_result.decision != ExitDecision.HOLD:
            return anticipation_result
        
        # 2. BOS invalidation
        bos_result = BOSInvalidationChecker.check_bos_invalidation(
            position, has_bos_against, current_price, market_time, df_5m
        )
        if bos_result.decision != ExitDecision.HOLD:
            return bos_result
        
        # 3. Momentum decay
        momentum_result = MomentumDecayChecker.check_momentum_decay(
            position, current_price, market_time, current_adx
        )
        if momentum_result.decision != ExitDecision.HOLD:
            return momentum_result
        
        # 4. Position health (stagnation, comprehensive check)
        if atr_5m and df_5m is not None:
            health_eval = PositionHealthChecker.evaluate_position(
                position, current_price, market_time, df_5m, atr_5m
            )
            if health_eval.get('should_close'):
                result = ExitCheckResult()
                result.decision = ExitDecision.CLOSE
                result.reason = health_eval.get('close_reason', 'HEALTH_CHECK')
                result.cooldown_minutes = 2
                return result
        
        return ExitCheckResult()  # HOLD


# =========================================================================
# SINGLETON ACCESS
# =========================================================================

_position_manager: Optional[PositionManager] = None


def get_position_manager() -> PositionManager:
    """Get singleton PositionManager instance."""
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager()
    return _position_manager


_position_health_checker: Optional[PositionHealthChecker] = None


def get_position_health_checker() -> PositionHealthChecker:
    """Get singleton PositionHealthChecker instance."""
    global _position_health_checker
    if _position_health_checker is None:
        _position_health_checker = PositionHealthChecker()
    return _position_health_checker
