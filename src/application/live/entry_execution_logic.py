"""
Entry Execution Logic - Consolidated from SignalEngine._execute_entry
(AUDIT REFACTOR - Jan 27, 2026)

This module consolidates ALL entry execution logic from trading_app.py (~1940 lines)
into organized, testable classes and functions.

Architecture:
1. PreExecutionGuards - All guards that run BEFORE scoring
2. ScoreCalculation - Threshold calculation and adjustments
3. RiskRewardValidation - RR floor checks, slippage model
4. PositionSizing - Consolidated sizing from all factors
5. OrderExecution - Final order placement

By extracting this logic, we:
- Reduce trading_app.py from 7800 to ~4000 lines
- Make entry logic testable in isolation
- Enable A/B testing of entry rules
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx, calculate_rsi
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE
from src.config.trading_config import (
    RiskManagement, OperatingMode, DynamicScoreThreshold,
    EntryGateConfig, SessionTradeLimits, VolatilityRegime, OrderExecution
)
from src.domain.market.regime import TrendPhase

logger = logging.getLogger(__name__)


# =========================================================================
# DATA CLASSES FOR EXECUTION RESULTS
# =========================================================================

@dataclass
class GuardCheckResult:
    """Result of pre-execution guard checks."""
    can_proceed: bool = True
    block_reason: str = ""
    should_return: bool = False
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ThresholdResult:
    """Result of threshold calculation."""
    base_threshold: float = 6.0
    final_threshold: float = 6.0
    trend_adjustment: float = 0.0
    anti_paralysis_adjustment: float = 0.0
    anti_paralysis_level: int = 0
    hours_since_trade: float = 0.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionSizingResult:
    """Result of position sizing calculation for execution."""
    position_size: float = 0.0
    combined_multiplier: float = 1.0
    multipliers: Dict[str, float] = field(default_factory=dict)
    blocked: bool = False
    block_reason: str = ""


@dataclass
class ExecutionResult:
    """Final result of entry execution."""
    success: bool = False
    order_id: Optional[str] = None
    fill_price: float = 0.0
    fill_size: float = 0.0
    error: str = ""
    tags: Dict[str, Any] = field(default_factory=dict)


# =========================================================================
# PRE-EXECUTION GUARDS
# =========================================================================

class PreExecutionGuards:
    """
    All guards that must pass BEFORE scoring/sizing.
    
    Guards:
    1. Location veto (near range boundary)
    2. ADX coherence (HTF trending but LTF dead)
    3. POST_ONLY rejection limit
    4. Funding rate check
    5. Spread filter
    6. Volatility regime classification
    """
    
    @staticmethod
    async def check_location_guards(
        engine: 'SignalEngine',
        bias: str,
        current_price: float,
        atr: float
    ) -> GuardCheckResult:
        """Check location-based guards."""
        from src.application.live.entry_executor import LocationGuards
        
        result = GuardCheckResult()
        
        # Check anchor levels
        can_enter, anchor_reason = LocationGuards.check_anchor_levels(
            engine, bias, current_price, atr
        )
        if not can_enter:
            result.can_proceed = False
            result.block_reason = f"Anchor veto: {anchor_reason}"
            result.should_return = True
            return result
        
        # Check range location
        can_enter, range_reason = LocationGuards.check_range_location(
            engine, bias, current_price
        )
        if not can_enter:
            result.can_proceed = False
            result.block_reason = f"Location veto: {range_reason}"
            result.should_return = True
            return result
        
        return result
    
    @staticmethod
    def check_adx_coherence(
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame
    ) -> GuardCheckResult:
        """Check ADX coherence between HTF and LTF."""
        result = GuardCheckResult()
        
        if df_1h is None or len(df_1h) < 20:
            return result
        if df_5m is None or len(df_5m) < 20:
            return result
        
        try:
            adx_1h, _ = calculate_adx(
                df_1h['high'].to_numpy(),
                df_1h['low'].to_numpy(),
                df_1h['close'].to_numpy(),
                14
            )
            adx_5m, _ = calculate_adx(
                df_5m['high'].to_numpy(),
                df_5m['low'].to_numpy(),
                df_5m['close'].to_numpy(),
                14
            )
            
            if len(adx_1h) > 0 and len(adx_5m) > 0:
                htf_adx = adx_1h[-1]
                ltf_adx = adx_5m[-1]
                
                # HTF shows trend but LTF is dead → wait
                if htf_adx > 20 and ltf_adx < 12:
                    result.can_proceed = False
                    result.block_reason = f"ADX divergence: HTF={htf_adx:.1f} trending but LTF={ltf_adx:.1f} dead"
                    result.should_return = True
                    
        except Exception as e:
            logger.debug(f"ADX coherence check error: {e}")
        
        return result
    
    @staticmethod
    async def check_funding_guard(
        engine: 'SignalEngine',
        bias: str
    ) -> GuardCheckResult:
        """Check funding rate is safe for entry."""
        from src.domain.market.filters import check_funding_rate_safe
        
        result = GuardCheckResult()
        
        if engine._is_backtest:
            return result
        
        try:
            funding_ok, funding_reason = await check_funding_rate_safe(
                engine.external_data,
                engine.symbol,
                bias
            )
            if not funding_ok:
                result.can_proceed = False
                result.block_reason = f"Funding unsafe: {funding_reason}"
                result.should_return = True
        except Exception as e:
            logger.warning(f"Could not check funding rate: {e}")
        
        return result
    
    @staticmethod
    def check_spread_guard(
        engine: 'SignalEngine',
        market_time: datetime
    ) -> GuardCheckResult:
        """Check spread is acceptable."""
        from src.config.trading_config import get_session_name
        
        result = GuardCheckResult()
        
        session_name = get_session_name(
            market_time.hour, market_time.minute, market_time.weekday()
        )
        spread_result = engine.spread_filter.check_entry_allowed(session_name)
        
        if not spread_result.allowed:
            result.can_proceed = False
            result.block_reason = f"Spread block: {spread_result.reason}"
            result.should_return = True
        else:
            result.tags['spread_penalty'] = spread_result.score_penalty
            result.tags['spread_size_factor'] = spread_result.size_factor
        
        return result


# =========================================================================
# THRESHOLD CALCULATOR
# =========================================================================

class ThresholdCalculator:
    """
    Calculates dynamic entry threshold based on:
    - Regime (RANGE vs TREND)
    - Session quality
    - Discovery mode
    - Trend phase
    - Anti-paralysis (hours since last trade)
    """
    
    @staticmethod
    def calculate(
        engine: 'SignalEngine',
        session_name: str,
        market_time: datetime,
        current_adx: float,
        volatility_regime
    ) -> ThresholdResult:
        """Calculate dynamic entry threshold with all adjustments."""
        result = ThresholdResult()
        
        # Determine regime
        is_discovery = OperatingMode.is_discovery()
        is_trend_regime = current_adx > 25 or volatility_regime in [
            VolatilityRegime.HIGH, VolatilityRegime.SPIKE
        ]
        regime_for_threshold = 'TREND' if is_trend_regime else 'RANGE'
        
        # Base threshold from config
        result.base_threshold = DynamicScoreThreshold.get_threshold(
            regime=regime_for_threshold,
            session=session_name,
            is_discovery=is_discovery
        )
        result.final_threshold = result.base_threshold
        
        # Trend phase adjustment
        trend_phase = engine._trend_phase
        result.trend_adjustment = ThresholdCalculator._get_trend_phase_adjustment(
            trend_phase, 
            engine._trend_intelligence.state if engine._trend_intelligence else None,
            engine.current_bias
        )
        result.final_threshold += result.trend_adjustment
        result.tags['trend_phase_adj'] = result.trend_adjustment
        
        # =========================================================================
        # 🔥 FIX (Jan 27, 2026): Use centralized Activity Tracker
        # ANTI-PARALYSIS IS NOW DISABLED
        # 
        # REASON: Anti-paralysis was:
        # 1. Using incorrect source of truth for last trade time
        # 2. Triggering "24h without trade" when trade happened minutes ago
        # 3. Lowering thresholds and accepting garbage trades
        # 4. Creating fear of inactivity instead of waiting for good setups
        # 
        # A professional trader prefers 0 trades to forced trades with bad RR.
        # =========================================================================
        from src.domain.trading.activity_tracker import get_activity_tracker
        
        tracker = get_activity_tracker()
        result.hours_since_trade = tracker.get_hours_since_last_trade(market_time)
        
        # ANTI-PARALYSIS DISABLED - Always return 0 adjustment
        result.anti_paralysis_adjustment = 0.0
        result.anti_paralysis_level = 0
        
        # Log activity status for monitoring (but NO threshold adjustment)
        activity_status = tracker.get_activity_status(market_time)
        result.tags['activity_level'] = activity_status.level.value
        result.tags['hours_since_trade'] = round(result.hours_since_trade, 2)
        
        # Log warning if inactivity is high (for debugging, not for action)
        if result.hours_since_trade >= 6:
            logger.warning(
                f"⏱️ Activity Monitor: {result.hours_since_trade:.1f}h since last trade | "
                f"Level: {activity_status.level.value} | "
                f"NO THRESHOLD ADJUSTMENT (anti-paralysis disabled)"
            )
        
        return result
    
    @staticmethod
    def _get_trend_phase_adjustment(
        trend_phase: TrendPhase,
        trend_state,
        bias: str
    ) -> float:
        """Get threshold adjustment based on trend phase."""
        if trend_phase == TrendPhase.TREND_BIRTH:
            return -1.0  # Easiest
        elif trend_phase == TrendPhase.TREND_ACCELERATION:
            return -0.5
        elif trend_phase == TrendPhase.TREND_EXHAUSTION:
            return +1.5
        elif trend_phase == TrendPhase.DISTRIBUTION:
            return +2.0
        elif trend_phase == TrendPhase.TREND_DEATH:
            # Reversal opportunity check
            if trend_state:
                direction = trend_state.direction.value if trend_state.direction else None
                if (direction == 'BULLISH' and bias == 'SHORT') or \
                   (direction == 'BEARISH' and bias == 'LONG'):
                    return -0.5  # Reversal opportunity
            return +2.0  # Wrong direction
        return 0.0
    
    @staticmethod
    def _get_anti_paralysis_adjustment(hours_since_trade: float) -> Tuple[float, int]:
        """
        Get anti-paralysis reduction and level.
        
        🔥 DISABLED (Jan 27, 2026): Always returns 0.
        
        REASON: Anti-paralysis was causing the system to accept garbage trades
        when it incorrectly thought it had been inactive for hours.
        """
        # DISABLED - Always return 0
        return 0.0, 0


# =========================================================================
# POSITION SIZING AGGREGATOR
# =========================================================================

class PositionSizingAggregator:
    """
    Consolidates ALL position sizing multipliers into one calculation.
    
    Multipliers:
    - Score-based
    - RR-based
    - LTF confirmation
    - Loss streak
    - Session quality
    - Day of week
    - Spread
    - Countertrend
    - Location cap
    - Anticipation probe
    - Gate size cap
    - Range/probing mode
    - Daily regime
    - Funding (for live)
    - Black swan
    - Edge enforcement
    """
    
    @staticmethod
    def calculate_all_multipliers(
        engine: 'SignalEngine',
        score: float,
        worst_case_rr: float,
        market_time: datetime,
        trade_tags: Dict[str, Any]
    ) -> ExecutionSizingResult:
        """Calculate combined sizing from all factors."""
        result = ExecutionSizingResult()
        result.combined_multiplier = 1.0
        
        # Get current regime string
        current_regime_str = engine._current_regime.value if engine._current_regime else "NORMAL"
        
        # 1. Black swan multiplier
        black_swan_mult = engine.black_swan_filter.get_size_multiplier(market_time)
        result.multipliers['black_swan'] = black_swan_mult
        result.combined_multiplier *= black_swan_mult
        
        # 2. Score-based sizing
        score_mult = EntryGateConfig.get_score_size_multiplier(score)
        result.multipliers['score'] = score_mult
        result.combined_multiplier *= score_mult
        
        # 3. RR-based sizing
        rr_mult = EntryGateConfig.get_rr_size_multiplier(worst_case_rr, current_regime_str)
        
        # High score bypass
        if rr_mult == 0.0 and score >= 7.0:
            rr_mult = 0.25
            trade_tags['high_score_rr_bypass'] = True
        
        if rr_mult == 0.0:
            result.blocked = True
            result.block_reason = f"Worst-case RR {worst_case_rr:.2f}R below floor"
            return result
        
        result.multipliers['rr'] = rr_mult
        result.combined_multiplier *= rr_mult
        
        # 4. LTF confirmation sizing
        ltf_confirmations = trade_tags.get('ltf_confirmations', 2)
        ltf_mult = EntryGateConfig.get_ltf_size_multiplier(ltf_confirmations, total=3)
        result.multipliers['ltf'] = ltf_mult
        result.combined_multiplier *= ltf_mult
        
        # 5. Loss streak sizing
        loss_mult = OperatingMode.get_loss_sizing_multiplier(int(engine._consecutive_losses))
        result.multipliers['loss_streak'] = loss_mult
        result.combined_multiplier *= loss_mult
        
        # 6. Session quality sizing
        from src.config.trading_config import get_session_name
        session_name = get_session_name(
            market_time.hour, market_time.minute, market_time.weekday()
        )
        session_mult = OperatingMode.get_session_multiplier(session_name)
        result.multipliers['session'] = session_mult
        result.combined_multiplier *= session_mult
        
        # 7. Day of week sizing
        day_mult = OperatingMode.get_day_multiplier(market_time.weekday())
        result.multipliers['day'] = day_mult
        result.combined_multiplier *= day_mult
        
        # 8. Spread sizing
        spread_mult = trade_tags.get('spread_size_factor', 1.0)
        result.multipliers['spread'] = spread_mult
        result.combined_multiplier *= spread_mult
        
        # 9. Countertrend sizing
        ct_mult = trade_tags.get('ct_size_mult', 1.0)
        result.multipliers['countertrend'] = ct_mult
        result.combined_multiplier *= ct_mult
        
        # 10. Location size cap
        bias = trade_tags.get('direction', 'LONG')
        location_cap_key = 'location_size_cap_long' if bias == 'LONG' else 'location_size_cap_short'
        location_mult = trade_tags.get(location_cap_key, 1.0)
        result.multipliers['location'] = location_mult
        result.combined_multiplier *= location_mult
        
        # 11. Anticipation probe sizing
        antic_mult = trade_tags.get('anticipation_probe_size', 1.0)
        result.multipliers['anticipation'] = antic_mult
        result.combined_multiplier *= antic_mult
        
        # 12. Gate size cap
        gate_mult = trade_tags.get('gate_size_cap', 1.0)
        result.multipliers['gate'] = gate_mult
        result.combined_multiplier *= gate_mult
        
        # 13. Range/probing mode
        range_mult = trade_tags.get('range_size_mult', 1.0)
        result.multipliers['range'] = range_mult
        result.combined_multiplier *= range_mult
        
        # 14. Daily regime sizing
        daily_config = trade_tags.get('_daily_regime_config', {})
        daily_mult = daily_config.get('size_mult', 1.0) if daily_config else 1.0
        result.multipliers['daily_regime'] = daily_mult
        result.combined_multiplier *= daily_mult
        
        return result


# =========================================================================
# ORDER EXECUTION HELPER
# =========================================================================

class OrderExecutor:
    """
    Handles final order placement with proper error handling.
    """
    
    @staticmethod
    async def execute_entry(
        engine: 'SignalEngine',
        bias: str,
        position_size: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        use_market_order: bool,
        market_time: datetime
    ) -> ExecutionResult:
        """Execute the entry order."""
        result = ExecutionResult()
        
        side = 'buy' if bias == 'LONG' else 'sell'
        order_type = 'market' if use_market_order else 'limit'
        
        try:
            # Determine limit price if using limit order
            limit_price = None
            if not use_market_order:
                offset_mult = OrderExecution.LIMIT_OFFSET_ATR_MULT
                atr_for_offset = engine._last_momentum_atr or 100.0
                offset = atr_for_offset * offset_mult
                
                if bias == 'LONG':
                    limit_price = current_price + offset
                else:
                    limit_price = current_price - offset
            
            # Place order
            if use_market_order:
                order = await engine.execution.place_market_order(
                    symbol=engine.symbol,
                    side=side,
                    amount=position_size
                )
            else:
                order = await engine.execution.place_limit_order(
                    symbol=engine.symbol,
                    side=side,
                    amount=position_size,
                    price=limit_price,
                    post_only=True
                )
            
            if order:
                result.success = True
                result.order_id = order.get('id')
                result.fill_price = order.get('price', current_price)
                result.fill_size = order.get('filled', position_size)
                
                logger.info(
                    f"✅ Order placed: {result.order_id} | {side.upper()} "
                    f"{result.fill_size:.6f} @ {result.fill_price:.2f}"
                )
            else:
                result.error = "Order response was None"
                logger.error(f"❌ Order failed: {result.error}")
                
        except Exception as e:
            result.error = str(e)
            logger.error(f"❌ Order execution error: {e}")
        
        return result


# =========================================================================
# MAIN ENTRY EXECUTION ORCHESTRATOR
# =========================================================================

async def execute_entry_orchestrated(
    engine: 'SignalEngine',
    current_price: float,
    atr_1m: float,
    market_time: datetime
) -> bool:
    """
    Orchestrated entry execution - consolidates all logic from _execute_entry.
    
    This function replaces ~1940 lines in _execute_entry with a clean flow.
    
    Returns:
        True if entry was successful, False otherwise
    """
    from src.application.live.entry_executor import ExecutionGuards
    
    bias = engine.current_bias
    if not bias:
        logger.warning("No bias set - cannot execute entry")
        return False
    
    # ========== PHASE 1: PRE-EXECUTION GUARDS ==========
    
    # 1. Check execution guards (pending orders, open positions, state)
    guard_result = await ExecutionGuards.check_all_guards(engine, current_price)
    if not guard_result.can_execute:
        if guard_result.needs_state_transition:
            from src.domain.trading.state.state_machine import BotState
            if guard_result.transition_state == "WAIT_FILL":
                engine.state_machine.transition_to(BotState.WAIT_FILL, guard_result.transition_reason)
            elif guard_result.transition_state == "MANAGE":
                engine.state_machine.transition_to(BotState.MANAGE, guard_result.transition_reason)
        return False
    
    # 2. Check edge enforcement
    can_trade, edge_mult, block_reason = await ExecutionGuards.check_edge_enforcement(engine, current_price)
    if not can_trade:
        return False
    
    # 3. Get market data
    candles_5m = engine.market_data.get_candles('5m')
    candles_1m = engine.market_data.get_candles('1m')
    candles_1h = engine.market_data.get_candles('1h')
    
    if not candles_5m or len(candles_5m) < 20:
        logger.warning("Not enough 5m data for entry")
        return False
    
    df_5m = pd.DataFrame(candles_5m)
    df_1m = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 14 else None
    df_1h = pd.DataFrame(candles_1h) if candles_1h and len(candles_1h) >= 50 else None
    
    # 4. Location guards
    atr_for_guards = engine._last_momentum_atr or 235.0
    location_result = await PreExecutionGuards.check_location_guards(
        engine, bias, current_price, atr_for_guards
    )
    if not location_result.can_proceed:
        logger.warning(f"🚫 {location_result.block_reason}")
        engine._set_rejection_cooldown(location_result.block_reason, candles=3)
        return False
    
    # 5. ADX coherence
    adx_result = PreExecutionGuards.check_adx_coherence(df_1h, df_5m)
    if not adx_result.can_proceed:
        logger.warning(f"🚫 {adx_result.block_reason}")
        return False
    
    # 6. Funding guard
    funding_result = await PreExecutionGuards.check_funding_guard(engine, bias)
    if not funding_result.can_proceed:
        logger.warning(f"🚫 {funding_result.block_reason}")
        return False
    
    # 7. Spread guard
    spread_result = PreExecutionGuards.check_spread_guard(engine, market_time)
    if not spread_result.can_proceed:
        logger.warning(f"🚫 {spread_result.block_reason}")
        engine._set_rejection_cooldown("Spread too wide", candles=1)
        return False
    engine._current_trade_tags.update(spread_result.tags)
    
    # ========== PHASE 2: THRESHOLD & SCORE ==========
    
    # The rest of the logic (scoring, RR validation, sizing) 
    # remains in trading_app.py for now, but uses extracted helpers
    # This is a gradual refactor - the full extraction happens in Phase 2
    
    return True  # Placeholder - actual execution continues in trading_app.py


# =========================================================================
# UTILITY FUNCTIONS
# =========================================================================

def calculate_stop_loss_and_tp(
    engine: 'SignalEngine',
    entry_price: float,
    bias: str,
    df_5m: pd.DataFrame,
    atr_for_sl: float,
    regime_sl_mult: float,
    daily_regime_config: dict
) -> Tuple[float, float, float]:
    """
    Calculate stop loss and take profit.
    
    Returns:
        (stop_loss, take_profit, risk)
    """
    # Find support/resistance
    support_resistance = engine._find_support_resistance(bias, df_5m)
    
    # Calculate ATR ratio for dynamic SL sizing
    atr_ratio = None
    if len(df_5m) >= 14:
        atr_values = calculate_atr(
            df_5m['high'].to_numpy(),
            df_5m['low'].to_numpy(),
            df_5m['close'].to_numpy(),
            14
        )
        if len(atr_values) >= 10:
            current_atr = atr_values[-1]
            avg_atr = sum(atr_values[-10:]) / 10
            if avg_atr > 0:
                atr_ratio = current_atr / avg_atr
    
    # Calculate stop loss
    stop_loss = engine.risk_engine.calculate_stop_loss(
        entry_price,
        bias,
        support_resistance,
        atr_for_sl,
        atr_ratio=atr_ratio,
        regime_atr_mult=regime_sl_mult
    )
    
    # Calculate risk
    risk = abs(entry_price - stop_loss)
    
    # Get TP multiplier from daily regime
    from src.config.trading_config import AsymmetryConfig
    tp_multiplier = daily_regime_config.get('tp_r_target', AsymmetryConfig.BASE_TP_R)
    
    # Calculate take profit
    if bias == 'LONG':
        take_profit = entry_price + (risk * tp_multiplier)
    else:
        take_profit = entry_price - (risk * tp_multiplier)
    
    return stop_loss, take_profit, risk


def log_sizing_breakdown(
    sizing_result: ExecutionSizingResult,
    position_size: float
):
    """Log detailed sizing breakdown."""
    parts = []
    for name, mult in sizing_result.multipliers.items():
        if mult != 1.0:
            parts.append(f"{name}={mult:.2f}")
    
    if parts:
        logger.info(f"📊 Sizing: {' × '.join(parts)} = {position_size:.6f}")
