"""
Entry Evaluation Logic - Extracted from SignalEngine (AUDIT REFACTOR)

Created: Jan 27, 2026

Consolidated entry evaluation checks extracted from _handle_idle_state.
This reduces ~500 lines from trading_app.py.

Contains:
- Pre-entry checks (exchange availability, daily halt, etc.)
- Filter validation coordination
- Anticipation engine coordination
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx
from src.config.trading_config import DailyRegimeDetector

logger = logging.getLogger(__name__)


@dataclass
class PreEntryCheckResult:
    """Result of pre-entry checks."""
    can_proceed: bool = True
    block_reason: str = ""
    should_return: bool = False
    next_state: Optional[str] = None


def check_pre_entry_conditions(
    engine: 'SignalEngine',
    market_time
) -> PreEntryCheckResult:
    """
    Run pre-entry checks before evaluating signals.
    
    Checks:
    1. Rejection cooldown
    2. Candle gate (only evaluate on new candle)
    3. Pending orders/positions (should be in different state)
    4. Daily halt
    5. Exchange availability
    6. Can trade check
    
    Returns:
        PreEntryCheckResult indicating whether to proceed
    """
    result = PreEntryCheckResult()
    
    # 1. Rejection cooldown
    in_cooldown, cooldown_reason = engine._is_in_rejection_cooldown()
    if in_cooldown:
        engine._idle_eval_count += 1
        if engine._idle_eval_count % 30 == 0:
            logger.debug(f"⏸️ {cooldown_reason}")
        result.can_proceed = False
        result.should_return = True
        return result
    
    # 2. Candle gate (skip if in WAIT_LTF)
    if engine.setup_state != "WAIT_LTF":
        if not engine._is_new_candle():
            engine._idle_eval_count += 1
            if engine._idle_eval_count % 60 == 0:
                logger.debug(f"📊 IDLE: Waiting for new candle | Tick #{engine._idle_eval_count}")
            result.can_proceed = False
            result.should_return = True
            return result
        engine._mark_candle_evaluated()
    
    # Log progress
    engine._idle_eval_count += 1
    if engine._idle_eval_count % 30 == 0:
        logger.info(f"📊 IDLE: Evaluated {engine._idle_eval_count} ticks")
    
    # Reset trade tags
    engine._current_trade_tags = {}
    
    # 3. Check for pending orders/positions (shouldn't be in IDLE)
    if engine.pending_order_id:
        logger.critical(f"⛔ IDLE STATE ABORT: Pending order exists - transitioning to WAIT_FILL")
        result.can_proceed = False
        result.should_return = True
        result.next_state = "WAIT_FILL"
        return result
    
    if engine.risk_engine.has_open_position():
        logger.critical(f"⛔ IDLE STATE ABORT: Open position exists - transitioning to MANAGE")
        result.can_proceed = False
        result.should_return = True
        result.next_state = "MANAGE"
        return result
    
    # 4. Daily halt check
    is_halted, halt_reason = engine.state_machine.check_daily_halt()
    if is_halted:
        if engine._idle_eval_count % 1000 == 0:
            logger.critical(f"🛑 DAILY HALT ACTIVE: {halt_reason}")
        result.can_proceed = False
        result.should_return = True
        return result
    
    # 5. Exchange availability
    if hasattr(engine.execution, 'is_exchange_available') and not engine.execution.is_exchange_available:
        if engine._idle_eval_count % 100 == 0:
            logger.warning("⚠️ Exchange unavailable - skipping signal evaluation")
        result.can_proceed = False
        result.should_return = True
        return result
    
    # 6. Can trade check
    can_trade, reason = engine.state_machine.can_trade()
    if not can_trade:
        if engine._idle_eval_count % 100 == 0:
            logger.info(f"⚠️ Cannot trade: {reason}")
        result.can_proceed = False
        result.should_return = True
        return result
    
    # 7. External data check
    ext_can_trade, ext_reason = engine.external_data.can_trade()
    if not ext_can_trade:
        logger.debug(f"External data blocks trading: {ext_reason}")
        result.can_proceed = False
        result.should_return = True
        return result
    
    return result


def update_daily_regime(
    engine: 'SignalEngine',
    candles_1h: list,
    market_time
) -> Optional[str]:
    """
    Update daily regime classification.
    
    This determines bot behavior for the day (TRENDING, RANGING, CHOPPY).
    
    Returns:
        New regime name if changed, None if unchanged
    """
    if not candles_1h or len(candles_1h) < 50:
        return None
    
    # Check if should update (every 1 hour)
    should_update = (
        engine._daily_regime_last_update is None or
        (market_time - engine._daily_regime_last_update).total_seconds() > 3600
    )
    
    if not should_update:
        return None
    
    # Calculate daily indicators
    highs_1h = np.array([c['high'] for c in candles_1h])
    lows_1h = np.array([c['low'] for c in candles_1h])
    closes_1h = np.array([c['close'] for c in candles_1h])
    
    # Daily range (last 24 1h candles)
    daily_high = np.max(highs_1h[-24:]) if len(highs_1h) >= 24 else np.max(highs_1h)
    daily_low = np.min(lows_1h[-24:]) if len(lows_1h) >= 24 else np.min(lows_1h)
    daily_range = daily_high - daily_low
    
    # Daily ATR
    atr_1h = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
    daily_atr = float(atr_1h[-1] * 24) if len(atr_1h) > 0 else daily_range
    
    # Daily ADX
    adx_1h_values, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
    daily_adx = float(adx_1h_values[-1]) if len(adx_1h_values) > 0 and not np.isnan(adx_1h_values[-1]) else 15.0
    
    # Classify
    new_daily_regime = DailyRegimeDetector.classify_daily_regime(
        daily_range=daily_range,
        daily_atr=daily_atr,
        daily_adx=daily_adx
    )
    
    # Update if changed
    if new_daily_regime != engine._daily_regime:
        old_regime = engine._daily_regime
        logger.info(f"🔄 DAILY REGIME TRANSITION: {old_regime} → {new_daily_regime}")
        logger.info(
            DailyRegimeDetector.log_daily_regime(
                new_daily_regime, daily_range, daily_atr, daily_adx
            )
        )
        engine._daily_regime = new_daily_regime
        engine._daily_regime_config = DailyRegimeDetector.get_daily_config(new_daily_regime)
        
        engine._current_trade_tags['daily_regime'] = new_daily_regime
        engine._current_trade_tags['daily_regime_changed_at'] = market_time.isoformat()
        
        engine._daily_regime_last_update = market_time
        return new_daily_regime
    
    engine._daily_regime_last_update = market_time
    return None


@dataclass 
class FilterCheckResult:
    """Result from running all entry filters."""
    passed: bool = False
    direction: Optional[str] = None
    score: float = 0.0
    reasons_passed: list = field(default_factory=list)
    reasons_blocked: list = field(default_factory=list)
    trade_tags: Dict[str, Any] = field(default_factory=dict)
    transition_to_wait_ltf: bool = False


def coordinate_filter_checks(
    engine: 'SignalEngine',
    current_price: float,
    atr_1m: float,
    market_time,
    candles_1m: list,
    candles_5m: list,
    candles_1h: list
) -> FilterCheckResult:
    """
    Coordinate all filter checks for entry evaluation.
    
    This is a placeholder for future full extraction.
    Currently the logic remains inline in trading_app.py but 
    can be gradually moved here.
    
    Returns:
        FilterCheckResult with pass/fail and direction
    """
    result = FilterCheckResult()
    
    # TODO: Move filter logic here gradually
    # For now, this is a stub that returns default
    
    return result
