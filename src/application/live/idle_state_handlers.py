"""
IDLE State Handlers Module - Extracted from _handle_idle_state.

This module contains the helper functions for IDLE state processing,
extracted from trading_app.py as part of AUDIT REFACTOR Point 5.

Each handler is a focused unit that can be tested independently.
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING

from src.shared.indicators import calculate_atr, calculate_adx, calculate_ema
from src.config.trading_config import DailyRegimeDetector
from src.domain.trading.anticipation import AnticipationSignal

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

logger = logging.getLogger(__name__)


# ==============================================================================
# REJECTION COOLDOWN CHECK
# ==============================================================================

def check_rejection_cooldown(engine: "SignalEngine") -> Tuple[bool, str]:
    """
    Check if bot is in rejection cooldown.
    
    Returns:
        (in_cooldown, reason) tuple
    """
    return engine._is_in_rejection_cooldown()


# ==============================================================================
# CANDLE GATE
# ==============================================================================

def check_candle_gate(
    engine: "SignalEngine",
    setup_state: str | None,
    idle_eval_count: int,
    current_price: float,
) -> Tuple[bool, int]:
    """
    Check if we should evaluate on this tick (candle gate).
    
    Returns:
        (should_evaluate, new_eval_count) tuple
    """
    # Exception: If in WAIT_LTF state, check every tick
    if setup_state == "WAIT_LTF":
        return True, idle_eval_count + 1
    
    if not engine._is_new_candle():
        # Same candle, no need to re-evaluate
        new_count = idle_eval_count + 1
        if new_count % 60 == 0:  # Log every minute
            logger.debug(f"📊 IDLE: Waiting for new candle | Tick #{new_count} | Price: ${current_price:.2f}")
        return False, new_count
    
    # New candle - mark as evaluated and proceed
    engine._mark_candle_evaluated()
    return True, idle_eval_count + 1


# ==============================================================================
# PENDING ORDER / POSITION CHECKS
# ==============================================================================

def check_pending_state_conflicts(engine: "SignalEngine") -> Optional[str]:
    """
    Check if there's a pending order or position that conflicts with IDLE state.
    
    Returns:
        State to transition to, or None if OK to proceed
    """
    from src.domain.trading.state.state_machine import BotState
    
    if engine.pending_order_id:
        logger.critical(f"⛔ IDLE STATE ABORT: Pending order {engine.pending_order_id} exists")
        return "WAIT_FILL"
    
    if engine.risk_engine.has_open_position():
        logger.critical(f"⛔ IDLE STATE ABORT: Open position exists")
        return "MANAGE"
    
    return None


# ==============================================================================
# EXCHANGE SYNC
# ==============================================================================

async def sync_with_exchange_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    sync_interval_seconds: float = 30.0,
) -> Tuple[bool, Optional[str]]:
    """
    Sync with exchange periodically to detect orphaned positions.
    
    Returns:
        (should_return_early, transition_state) tuple
    """
    from src.domain.trading.state.state_machine import BotState
    
    if engine._last_exchange_sync is None:
        engine._last_exchange_sync = market_time
    
    time_since_sync = (market_time - engine._last_exchange_sync).total_seconds()
    if time_since_sync <= sync_interval_seconds:
        return False, None
    
    # Perform sync
    try:
        async with asyncio.timeout(10):
            async with engine._sync_lock:
                try:
                    engine._syncing_position = True
                    
                    exchange_has_position = await engine.execution.has_open_position(engine.symbol)
                    if exchange_has_position:
                        logger.critical("🔄 IDLE but exchange has position - syncing state...")
                        
                        # Get ATR for proper SL calculation
                        candles_5m = engine.market_data.get_candles('5m')
                        atr_5m = None
                        if candles_5m:
                            df_5m = pd.DataFrame(candles_5m)
                            atr_5m = engine._calculate_atr_5m(df_5m)
                        
                        exchange_position = await engine.execution.get_position(engine.symbol)
                        if exchange_position:
                            await engine.risk_engine.update_from_exchange_position(
                                exchange_position, atr=atr_5m
                            )
                        
                        return True, "MANAGE"
                    
                    engine._last_exchange_sync = market_time
                    
                except Exception as e:
                    logger.error(f"Error during exchange sync in IDLE: {e}")
                finally:
                    engine._syncing_position = False
    except asyncio.TimeoutError:
        logger.warning("Exchange sync lock acquisition timeout - will retry next cycle")
    
    return False, None


# ==============================================================================
# WAIT_LTF STATE HANDLER
# ==============================================================================

async def handle_wait_ltf_state(
    engine: "SignalEngine",
    current_price: float,
    atr_1m: float,
    market_time: datetime,
) -> Optional[str]:
    """
    Handle WAIT_LTF state - waiting for LTF trigger after HTF setup.
    
    Returns:
        - "EXECUTING" if triggered and should proceed to execute
        - "RESET" if setup invalidated or timed out
        - None to stay in WAIT_LTF
    """
    if engine.setup_state != "WAIT_LTF":
        return None
    
    if engine.setup_start_time is None:
        logger.error("WAIT_LTF state with no setup_start_time - resetting")
        engine.setup_state = "IDLE"
        return "RESET"
    
    elapsed = (market_time - engine.setup_start_time).total_seconds()
    
    # 1. Check timeout
    if elapsed > engine.ltf_wait_timeout:
        logger.warning(f"WAIT_LTF timeout after {elapsed:.0f}s - Discarding setup")
        _reset_wait_ltf_state(engine)
        return "RESET"
    
    # 2. Check invalidation conditions - EMA50 break
    candles_5m = engine.market_data.get_candles('5m')
    if candles_5m and len(candles_5m) >= 20:
        df_5m = pd.DataFrame(candles_5m)
        closes_5m = df_5m['close'].to_numpy()
        ema_50 = calculate_ema(closes_5m, 50)[-1]
        
        if engine.current_bias == "LONG" and current_price < ema_50:
            logger.warning("Setup invalidated: Price broke EMA50 down during WAIT_LTF")
            _reset_wait_ltf_state(engine)
            return "RESET"
        
        if engine.current_bias == "SHORT" and current_price > ema_50:
            logger.warning("Setup invalidated: Price broke EMA50 up during WAIT_LTF")
            _reset_wait_ltf_state(engine)
            return "RESET"
    
    # 3. Try LTF trigger
    logger.debug(f"WAIT_LTF state: Checking LTF trigger ({elapsed:.0f}s/{engine.ltf_wait_timeout}s)")
    candles_1m = engine.market_data.get_candles('1m')
    
    if candles_1m and len(candles_1m) >= 20:
        df_1m = pd.DataFrame(candles_1m)
        
        # Get regime info for volume-aware filtering
        regime_value = engine.regime_classifier.current_regime.value if engine.regime_classifier.current_regime else None
        regime_conf = engine.regime_classifier.regime_confidence
        
        if engine.current_bias is None:
            logger.warning("WAIT_LTF without bias - resetting")
            engine.setup_state = "IDLE"
            return "RESET"
        
        ltf_passed, ltf_reason = engine.ltf_filter.check(
            df_1m,
            engine.current_bias,
            {},
            regime=regime_value,
            regime_confidence=regime_conf
        )
        
        if ltf_passed:
            logger.info(f"✅ LTF TRIGGER FIRED - Executing {engine.current_bias}")
            engine.setup_state = "EXECUTING"
            return "EXECUTING"
        else:
            logger.debug(f"LTF not ready yet: {ltf_reason}")
    
    return None  # Stay in WAIT_LTF


def _reset_wait_ltf_state(engine: "SignalEngine"):
    """Reset WAIT_LTF state to IDLE."""
    engine.setup_state = "IDLE"
    engine.setup_frozen_htf = False
    engine.current_bias = None


# ==============================================================================
# ANTICIPATION ENGINE CHECK
# ==============================================================================

def check_anticipation_signal(
    engine: "SignalEngine",
    current_price: float,
    market_time: datetime,
) -> Dict[str, Any]:
    """
    Check anticipation engine for early entry signals.
    
    Returns:
        Dict of trade tags to apply if anticipation detected
    """
    tags = {}
    
    try:
        candles_5m = engine.market_data.get_candles('5m')
        if not candles_5m or len(candles_5m) < 50:
            return tags
        
        df_5m = pd.DataFrame(candles_5m)
        atr_5m_arr = calculate_atr(
            df_5m['high'].to_numpy(),
            df_5m['low'].to_numpy(),
            df_5m['close'].to_numpy(),
            14
        )
        
        # Safe ATR extraction with NaN guard
        atr_5m_val = current_price * 0.005  # Default fallback
        if len(atr_5m_arr) > 0 and not np.isnan(atr_5m_arr[-1]):
            atr_5m_val = atr_5m_arr[-1]
        
        # Get HTF bias hint
        htf_bias_hint = getattr(engine, '_htf_cache_bias', None)
        
        # Get current regime
        current_regime = None
        if engine.regime_classifier and engine.regime_classifier.current_regime:
            current_regime = engine.regime_classifier.current_regime.value
        
        anticipation = engine.anticipation_engine.evaluate(
            df_5m=df_5m,
            current_price=current_price,
            atr=atr_5m_val,
            market_time=market_time,
            htf_bias=htf_bias_hint,
            regime=current_regime
        )
        
        engine._last_anticipation_state = anticipation
        
        # Check for valid anticipation signal (confidence > 60%)
        if anticipation.signal != AnticipationSignal.NONE and anticipation.confidence >= 0.6:
            tags['anticipation_signal'] = anticipation.signal.value
            tags['anticipation_confidence'] = anticipation.confidence
            tags['anticipation_direction'] = anticipation.direction
            
            logger.info(
                f"🔮 ANTICIPATION DETECTED: {anticipation.signal.value} {anticipation.direction} "
                f"(conf={anticipation.confidence:.0%})"
            )
            if anticipation.reasons:
                for reason in anticipation.reasons[:3]:
                    logger.info(f"   → {reason}")
            
            # High confidence (>75%) -> consider early entry with probe size
            if anticipation.confidence >= 0.75 and anticipation.direction:
                tags['anticipation_entry'] = True
                if anticipation.time_valid_until:
                    tags['anticipation_valid_until'] = anticipation.time_valid_until.timestamp()
                tags['anticipation_probe_size'] = 0.30  # 30% size
                logger.info(f"🔬 ANTICIPATION PROBE: Will use 30% position size")
    
    except Exception as e:
        logger.warning(f"⚠️ Anticipation engine error (non-fatal): {e}")
    
    return tags


# ==============================================================================
# DAILY REGIME DETECTOR
# ==============================================================================

def update_daily_regime_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    update_interval_seconds: float = 3600.0,  # 1 hour
) -> Optional[Dict[str, Any]]:
    """
    Update daily regime classification if enough time has passed.
    
    Returns:
        New regime config dict if updated, None otherwise
    """
    candles_1h = engine.market_data.get_candles('1h')
    if not candles_1h or len(candles_1h) < 50:
        return None
    
    should_update = (
        engine._daily_regime_last_update is None or
        (market_time - engine._daily_regime_last_update).total_seconds() > update_interval_seconds
    )
    
    if not should_update:
        return None
    
    # Calculate daily indicators from 1h candles
    highs_1h = np.array([c['high'] for c in candles_1h])
    lows_1h = np.array([c['low'] for c in candles_1h])
    closes_1h = np.array([c['close'] for c in candles_1h])
    
    # Daily range (last 24 1h candles)
    daily_high = np.max(highs_1h[-24:]) if len(highs_1h) >= 24 else np.max(highs_1h)
    daily_low = np.min(lows_1h[-24:]) if len(lows_1h) >= 24 else np.min(lows_1h)
    daily_range = daily_high - daily_low
    
    # Daily ATR (from 1h data averaged)
    atr_1h = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
    daily_atr = float(atr_1h[-1] * 24) if len(atr_1h) > 0 else daily_range
    
    # Daily ADX
    adx_1h_values, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
    daily_adx = 15.0
    if len(adx_1h_values) > 0 and not np.isnan(adx_1h_values[-1]):
        daily_adx = float(adx_1h_values[-1])
    
    # Classify daily regime
    new_daily_regime = DailyRegimeDetector.classify_daily_regime(
        daily_range=daily_range,
        daily_atr=daily_atr,
        daily_adx=daily_adx
    )
    
    # Update state if changed
    if new_daily_regime != engine._daily_regime:
        old_regime = engine._daily_regime
        logger.info(f"🔄 DAILY REGIME TRANSITION: {old_regime} → {new_daily_regime}")
        logger.info(DailyRegimeDetector.log_daily_regime(
            new_daily_regime, daily_range, daily_atr, daily_adx
        ))
        
        engine._daily_regime = new_daily_regime
        # 🔥 FIX: Method is get_daily_config, not get_regime_config
        engine._daily_regime_config = DailyRegimeDetector.get_daily_config(new_daily_regime)
    
    engine._daily_regime_last_update = market_time
    return engine._daily_regime_config


# ==============================================================================
# PRE-TRADE CHECKS
# ==============================================================================

async def check_pre_trade_conditions(
    engine: "SignalEngine",
    market_time: datetime,
) -> Tuple[bool, str]:
    """
    Check conditions that must pass before evaluating trades.
    
    Returns:
        (can_trade, reason) tuple
    """
    # Daily halt check
    is_halted, halt_reason = engine.state_machine.check_daily_halt()
    if is_halted:
        return False, f"Daily halt: {halt_reason}"
    
    # Exchange availability
    if hasattr(engine.execution, 'is_exchange_available') and not engine.execution.is_exchange_available:
        return False, "Exchange unavailable"
    
    # State machine check
    can_trade, reason = engine.state_machine.can_trade()
    if not can_trade:
        return False, reason
    
    # External data check
    ext_can_trade, ext_reason = engine.external_data.can_trade()
    if not ext_can_trade:
        return False, f"External data: {ext_reason}"
    
    return True, "OK"


async def refresh_balance_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    refresh_interval_seconds: float = 300.0,  # 5 minutes
):
    """Refresh account balance periodically."""
    if engine._last_balance_check is None:
        engine._last_balance_check = market_time
    
    if (market_time - engine._last_balance_check).total_seconds() <= refresh_interval_seconds:
        return
    
    try:
        balance = await engine.execution.get_account_balance()
        if balance:
            await engine.risk_engine.update_capital_from_balance(balance)
        engine._last_balance_check = market_time
    except Exception as e:
        logger.warning(f"Balance check error: {e}")
