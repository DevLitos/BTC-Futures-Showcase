"""
Entry Orchestrator - Consolidates _execute_entry logic.

Created: Jan 28, 2026 (AUDIT REFACTOR - Reduce trading_app.py)

This module orchestrates the entire entry execution flow, replacing ~1500 lines
of inline code in _execute_entry with clean, testable components.

The orchestrator:
1. Runs all pre-execution guards (concurrency, edge, location)
2. Calculates entry score with all adjustments
3. Applies spread, delta, obstinacy, location penalties
4. Runs entry quality gate
5. Validates chase conditions
6. Calculates position size with all multipliers
7. Validates RR and costs
8. Executes order and handles state transitions
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx, calculate_rsi
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE
from src.config.trading_config import (
    RiskManagement, OperatingMode, EntryGateConfig, DynamicScoreThreshold,
    SessionTradeLimits, VolatilityRegime, BiasStrengthConfig,
    get_session_name, OrderExecution, AsymmetryConfig
)
from src.domain.market.regime import TrendPhase
from src.domain.trading.state.state_machine import BotState
from src.domain.market.regime import EntryRegime
from src.infrastructure.exchanges.kraken.slippage_model import OrderType
from src.application.live.entry_executor import ExecutionGuards, LocationGuards
from src.application.live.entry_validators import ChaseResult
from src.domain.trading.execution_logic.entry_quality import GateDecision

logger = logging.getLogger(__name__)


# =========================================================================
# DATA CLASSES
# =========================================================================

@dataclass
class EntryContext:
    """All context needed for entry execution."""
    current_price: float
    atr_1m: float
    market_time: datetime
    bias: str
    df_5m: pd.DataFrame
    df_1m: Optional[pd.DataFrame]
    df_1h: pd.DataFrame
    session_name: str
    # Calculated values
    atr_5m: float = 0.0
    atr_for_sl: float = 0.0
    adx_1h: float = 20.0
    has_bos: bool = False
    # State
    regime: EntryRegime = EntryRegime.NORMAL
    volatility_regime: str = "NORMAL"  # 🔥 FIX: Use string, not class
    trend_phase: TrendPhase = TrendPhase.RANGE_BOUND


@dataclass
class GuardResult:
    """Result of all guard checks."""
    passed: bool = True
    reason: str = ""
    edge_size_mult: float = 1.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass 
class ScoreResult:
    """Result of score calculation."""
    score: float = 0.0
    breakdown: Dict[str, Any] = field(default_factory=dict)
    structure_blocks: bool = False
    threshold: float = 5.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestrationSizingResult:
    """Result of position sizing in orchestration."""
    size: float = 0.0
    size_usd: float = 0.0
    multipliers: Dict[str, float] = field(default_factory=dict)
    final_multiplier: float = 1.0
    min_size_ok: bool = True


@dataclass
class EntryResult:
    """Final result of entry attempt."""
    success: bool = False
    order_id: Optional[str] = None
    reason: str = ""
    should_transition: bool = False
    transition_to: str = ""
    tags: Dict[str, Any] = field(default_factory=dict)


# =========================================================================
# GUARD ORCHESTRATOR
# =========================================================================

class GuardOrchestrator:
    """
    Runs all pre-execution guards in order.
    
    Guard order:
    1. Concurrency guards (pending orders, open positions)
    2. Edge enforcement
    3. Location guards (anchor levels, range boundaries)
    4. ADX coherence
    5. POST_ONLY rejection tracking
    6. TP/RR config sanity
    7. Funding rate
    8. Spread filter
    9. Session limits
    """
    
    @staticmethod
    async def run_all_guards(engine: 'SignalEngine', ctx: EntryContext) -> GuardResult:
        """Run all guards and return combined result."""
        result = GuardResult()
        
        # 1. Concurrency guards
        guard_result = await ExecutionGuards.check_all_guards(engine, ctx.current_price)
        if not guard_result.can_execute:
            result.passed = False
            result.reason = guard_result.block_reason
            return result
        
        # 2. Edge enforcement
        can_trade, edge_mult, block_reason = await ExecutionGuards.check_edge_enforcement(engine, ctx.current_price)
        if not can_trade:
            result.passed = False
            result.reason = block_reason
            return result
        result.edge_size_mult = edge_mult
        
        # 3. Location guards (early check)
        atr_for_headroom = engine._last_momentum_atr or 235.0
        can_enter, anchor_reason = LocationGuards.check_anchor_levels(engine, ctx.bias, ctx.current_price, atr_for_headroom)
        if not can_enter:
            result.passed = False
            result.reason = f"Anchor veto: {anchor_reason}"
            engine._set_rejection_cooldown("Anchor veto", candles=5)
            return result
        
        can_enter, range_reason = LocationGuards.check_range_location(engine, ctx.bias, ctx.current_price)
        if not can_enter:
            result.passed = False
            result.reason = f"Location veto: {range_reason}"
            result.tags['block_category'] = 'location'
            engine._set_rejection_cooldown("Location veto", candles=3)
            return result
        
        # 4. ADX coherence
        if len(ctx.df_1h) >= 20 and len(ctx.df_5m) >= 20:
            adx_1h, _ = calculate_adx(ctx.df_1h['high'].to_numpy(), ctx.df_1h['low'].to_numpy(), ctx.df_1h['close'].to_numpy(), 14)
            adx_5m, _ = calculate_adx(ctx.df_5m['high'].to_numpy(), ctx.df_5m['low'].to_numpy(), ctx.df_5m['close'].to_numpy(), 14)
            
            if len(adx_1h) > 0 and len(adx_5m) > 0:
                htf_adx = adx_1h[-1]
                ltf_adx = adx_5m[-1]
                ctx.adx_1h = htf_adx
                
                if htf_adx > 20 and ltf_adx < 12:
                    result.passed = False
                    result.reason = f"ADX divergence: HTF={htf_adx:.1f} trending, LTF={ltf_adx:.1f} dead"
                    return result
        
        # 5. POST_ONLY rejection tracking
        if hasattr(engine, '_consecutive_post_only_rejects') and engine._consecutive_post_only_rejects >= 3:
            result.passed = False
            result.reason = f"Market too fast: {engine._consecutive_post_only_rejects} POST_ONLY rejections"
            engine._consecutive_post_only_rejects = 0
            engine.state_machine.start_cooldown(5)
            return result
        
        # 6. TP/RR sanity
        if RiskManagement.TP1_RISK_MULTIPLE < OperatingMode.RR_HARD_FLOOR:
            result.passed = False
            result.reason = f"Config error: TP1_RISK_MULTIPLE < RR_HARD_FLOOR"
            return result
        
        # 7. Funding rate
        if not engine._is_backtest:
            try:
                from src.domain.market.filters import check_funding_rate_safe
                funding_ok, funding_reason = await check_funding_rate_safe(
                    engine.external_data, engine.symbol, ctx.bias
                )
                if not funding_ok:
                    result.passed = False
                    result.reason = f"Funding unsafe: {funding_reason}"
                    return result
            except Exception as e:
                logger.warning(f"Could not check funding rate: {e}")
        
        # 8. Spread filter
        spread_result = engine.spread_filter.check_entry_allowed(ctx.session_name)
        if not spread_result.allowed:
            result.passed = False
            result.reason = f"Spread block: {spread_result.reason}"
            result.tags['block_category'] = 'spread'
            engine._set_rejection_cooldown("Spread too wide", candles=1)
            return result
        
        result.tags['spread_penalty'] = spread_result.score_penalty
        result.tags['spread_size_factor'] = spread_result.size_factor
        
        # 9. Session limits (just flag - override checked later)
        session_trades = engine._session_trade_counts.get(ctx.session_name, 0)
        session_limit = SessionTradeLimits.MAX_TRADES_PER_SESSION.get(ctx.session_name, 5)
        result.tags['session_at_limit'] = session_trades >= session_limit
        result.tags['session_trades'] = session_trades
        result.tags['session_limit'] = session_limit
        
        return result


# =========================================================================
# SCORE ORCHESTRATOR
# =========================================================================

class ScoreOrchestrator:
    """
    Calculates entry score with all adjustments.
    
    Flow:
    1. Calculate base score from 4 blocks
    2. Apply spread penalty
    3. Apply delta volume bonus
    4. Apply short obstinacy penalty
    5. Apply location penalty
    6. Apply anticipation bonus
    7. Apply with-trend bonus
    8. Check structure gate
    9. Calculate dynamic threshold
    10. Check floor and entry type
    """
    
    @staticmethod
    def calculate_score(
        engine: 'SignalEngine',
        ctx: EntryContext,
        guard_result: GuardResult
    ) -> ScoreResult:
        """Calculate entry score with all adjustments."""
        result = ScoreResult()
        
        # Handle None df_1m
        df_1m_safe = ctx.df_1m if ctx.df_1m is not None else pd.DataFrame()
        
        # 1. Base score from scorer
        score, breakdown, structure_blocks = engine._entry_scorer.calculate_entry_score(
            df_1h=ctx.df_1h,
            df_5m=ctx.df_5m,
            df_1m=df_1m_safe,
            bias=ctx.bias,
            current_price=ctx.current_price,
            market_time=ctx.market_time,
            micro_trend_state=getattr(engine, '_micro_trend_state', None),
            filter_pipeline=getattr(engine, 'filter_pipeline', None),
        )
        
        result.score = float(score)
        result.breakdown = breakdown
        result.structure_blocks = structure_blocks
        
        # 2. Spread penalty
        spread_penalty = guard_result.tags.get('spread_penalty', 0)
        if spread_penalty > 0:
            result.score = max(0, result.score - spread_penalty)
            result.tags['spread_adjusted'] = True
        
        # 3. Delta volume bonus
        delta_bonus = engine._current_trade_tags.get('delta_score_bonus', 0)
        if delta_bonus > 0:
            result.score = min(9, result.score + delta_bonus)
            result.tags['delta_adjusted'] = True
        
        # 4. Short obstinacy penalty
        obstinacy_penalty = engine._current_trade_tags.get('short_obstinacy_penalty', 0)
        if obstinacy_penalty < 0:
            result.score = max(0, result.score + obstinacy_penalty)
            result.tags['obstinacy_adjusted'] = True
        
        # 5. Location penalty
        location_penalty = engine._current_trade_tags.get('location_penalty_applied', 0)
        if location_penalty < 0:
            result.score = max(0, result.score + location_penalty)
            result.tags['location_adjusted'] = True
        
        # 6. Anticipation bonus
        if engine._current_trade_tags.get('anticipation_entry', False):
            antic_dir = engine._current_trade_tags.get('anticipation_direction')
            antic_conf = engine._current_trade_tags.get('anticipation_confidence', 0)
            if antic_dir == ctx.bias and antic_conf >= 0.6:
                antic_bonus = min(1.0, 0.5 + (antic_conf - 0.6) * 1.25)
                result.score = min(9, result.score + antic_bonus)
                result.tags['anticipation_bonus'] = antic_bonus
        
        # 7. With-trend bonus
        is_countertrend = engine._current_trade_tags.get('is_countertrend', False)
        bias_strength = getattr(engine, '_current_bias_strength', None)
        if not is_countertrend and bias_strength and bias_strength.value in ['BULL_STRONG', 'BEAR_STRONG']:
            result.score += BiasStrengthConfig.WITHTREND_SCORE_RELAXATION
            result.tags['withtrend_bonus'] = BiasStrengthConfig.WITHTREND_SCORE_RELAXATION
        
        # Calculate dynamic threshold
        result.threshold = ScoreOrchestrator._calculate_threshold(engine, ctx)
        
        return result
    
    @staticmethod
    def _calculate_threshold(engine: 'SignalEngine', ctx: EntryContext) -> float:
        """Calculate dynamic entry threshold."""
        is_discovery = OperatingMode.is_discovery()
        
        # Detect regime
        adx_vals = engine._score_breakdown.get('adx_values', [20.0]) if hasattr(engine, '_score_breakdown') else [20.0]
        try:
            current_adx = float(adx_vals[-1]) if len(adx_vals) > 0 else 20.0
        except (TypeError, ValueError, IndexError):
            current_adx = 20.0
        
        is_trend = current_adx > 25 or ctx.volatility_regime in [VolatilityRegime.HIGH, VolatilityRegime.SPIKE]
        regime_str = 'TREND' if is_trend else 'RANGE'
        
        # Base threshold
        threshold = DynamicScoreThreshold.get_threshold(
            regime=regime_str,
            session=ctx.session_name,
            is_discovery=is_discovery
        )
        
        # Trend phase adjustment
        trend_adj = ScoreOrchestrator._get_trend_phase_adjustment(ctx.trend_phase, ctx.bias, engine)
        threshold += trend_adj
        
        # 🔥 FIX (Jan 27, 2026): Anti-paralysis DISABLED
        # The system was accepting garbage trades due to incorrect activity tracking
        # NO threshold reduction based on inactivity
        
        return threshold
    
    @staticmethod
    def _get_trend_phase_adjustment(trend_phase: TrendPhase, bias: str, engine: 'SignalEngine') -> float:
        """Get threshold adjustment based on trend phase."""
        if trend_phase == TrendPhase.TREND_BIRTH:
            return -1.0
        elif trend_phase == TrendPhase.TREND_ACCELERATION:
            return -0.5
        elif trend_phase == TrendPhase.TREND_EXHAUSTION:
            return 1.5
        elif trend_phase == TrendPhase.DISTRIBUTION:
            return 2.0
        elif trend_phase == TrendPhase.TREND_DEATH:
            trend_state = engine._trend_intelligence.state if hasattr(engine, '_trend_intelligence') else None
            if trend_state:
                is_reversal = (
                    (trend_state.direction.value == 'BULLISH' and bias == 'SHORT') or
                    (trend_state.direction.value == 'BEARISH' and bias == 'LONG')
                )
                return -0.5 if is_reversal else 2.0
        return 0.0
    
    @staticmethod
    def _get_anti_paralysis_adjustment(engine: 'SignalEngine', market_time: datetime) -> float:
        """
        Get threshold reduction based on inactivity.
        
        🔥 DISABLED (Jan 27, 2026): Always returns 0.
        
        REASON: Anti-paralysis was causing the system to:
        1. Think it hadn't traded in 24h when it had traded minutes ago
        2. Lower thresholds and accept garbage trades
        3. Create "fear of inactivity" instead of waiting for quality setups
        
        A professional trader prefers 0 trades to forced trades with bad RR.
        """
        # DISABLED - Always return 0
        return 0.0


# =========================================================================
# SIZING ORCHESTRATOR
# =========================================================================

class SizingOrchestrator:
    """
    Calculates final position size with all multipliers.
    
    Multipliers:
    1. Edge enforcement
    2. Score-based
    3. Regime-based
    4. Volatility
    5. Session quality
    6. Loss streak
    7. Spread factor
    8. Countertrend factor
    9. Day-of-week
    10. Anticipation probe
    11. RR-based
    12. Gate size cap
    13. Intelligent filter
    """
    
    @staticmethod
    def calculate_size(
        engine: 'SignalEngine',
        ctx: EntryContext,
        score_result: ScoreResult,
        guard_result: GuardResult,
        stop_loss: float,
        gate_size_cap: float = 1.0,
    ) -> OrchestrationSizingResult:
        """Calculate final position size."""
        result = OrchestrationSizingResult()
        result.multipliers = {}
        
        # Base size from risk engine
        # 🔥 FIX: Pass hour and minute from market_time
        base_size = engine.risk_engine.calculate_position_size(
            ctx.current_price, stop_loss, ctx.market_time.hour, ctx.market_time.minute
        )
        
        # 1. Edge enforcement
        result.multipliers['edge'] = guard_result.edge_size_mult
        
        # 2. Score-based multiplier
        score_mult = EntryGateConfig.get_score_size_multiplier(score_result.score)
        result.multipliers['score'] = score_mult
        
        # 3. Regime-based
        regime_config = engine.regime_detector.get_config(ctx.regime) if hasattr(engine, 'regime_detector') else None
        regime_mult = regime_config.size_multiplier if regime_config else 1.0
        result.multipliers['regime'] = regime_mult
        
        # 4. Volatility
        # 🔥 FIX: vol_regime is a string, use SIZE_MULTIPLIER dict
        vol_mult = VolatilityRegime.SIZE_MULTIPLIER.get(ctx.volatility_regime, 1.0)
        result.multipliers['volatility'] = vol_mult
        
        # 5. Session quality
        session_quality = engine._current_trade_tags.get('session_quality', 'moderate')
        session_mult = {'good': 1.0, 'moderate': 0.85, 'poor': 0.7}.get(session_quality, 0.85)
        result.multipliers['session'] = session_mult
        
        # 6. Loss streak
        loss_streak = getattr(engine, '_consecutive_losses', 0)
        loss_mult = max(0.5, 1.0 - (loss_streak * 0.1)) if loss_streak >= 2 else 1.0
        result.multipliers['loss_streak'] = loss_mult
        engine._current_trade_tags['loss_streak_size_factor'] = loss_mult
        
        # 7. Spread factor
        spread_mult = guard_result.tags.get('spread_size_factor', 1.0)
        result.multipliers['spread'] = spread_mult
        
        # 8. Countertrend
        ct_mult = engine._current_trade_tags.get('ct_size_mult', 1.0)
        result.multipliers['countertrend'] = ct_mult
        
        # 9. Day-of-week
        dow = ctx.market_time.weekday()
        dow_mult = engine._daily_regime_config.get('dow_size_mult', {}).get(dow, 1.0) if hasattr(engine, '_daily_regime_config') and engine._daily_regime_config else 1.0
        result.multipliers['day_of_week'] = dow_mult
        
        # 10. Anticipation probe
        antic_mult = 1.0
        if engine._current_trade_tags.get('anticipation_entry', False):
            antic_mult = engine._current_trade_tags.get('anticipation_probe_size', 0.30)
        result.multipliers['anticipation'] = antic_mult
        
        # 11. RR-based (if available)
        rr_mult = engine._current_trade_tags.get('rr_size_mult', 1.0)
        result.multipliers['rr'] = rr_mult
        
        # 12. Gate size cap
        result.multipliers['gate_cap'] = gate_size_cap
        
        # Calculate final
        final_mult = 1.0
        for name, mult in result.multipliers.items():
            final_mult *= mult
        
        result.final_multiplier = final_mult
        result.size = base_size * final_mult
        result.size = round_size_to_exchange(result.size)
        result.size_usd = result.size * ctx.current_price
        result.min_size_ok = result.size >= KRAKEN_BTC_MIN_SIZE
        
        return result


# =========================================================================
# ORDER EXECUTOR
# =========================================================================

class OrderExecutor:
    """Handles order placement and state transitions."""
    
    @staticmethod
    async def place_order(
        engine: 'SignalEngine',
        ctx: EntryContext,
        size: float,
        stop_loss: float,
        take_profit: float,
        use_market: bool = False,
        entry_data: Optional[Dict[str, Any]] = None,  # 🔥 FIX: Use Optional
    ) -> EntryResult:
        """Place order and handle result."""
        result = EntryResult()
        
        try:
            if use_market:
                logger.info(f"🚀 Placing MARKET order | {ctx.bias} {size:.6f} @ market")
                order_result = await engine.execution.place_market_order(
                    symbol=engine.symbol,
                    side='buy' if ctx.bias == 'LONG' else 'sell',
                    amount=size,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
            else:
                logger.info(f"📊 Placing LIMIT post-only | {ctx.bias} {size:.6f} @ {ctx.current_price:.2f}")
                order_result = await engine.execution.place_limit_order(
                    symbol=engine.symbol,
                    side='buy' if ctx.bias == 'LONG' else 'sell',
                    amount=size,
                    price=ctx.current_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
            
            if order_result:
                result.success = True
                result.order_id = order_result.get('id')
                
                # Store pending order info
                engine.pending_order_id = result.order_id
                engine.entry_order_time = ctx.market_time
                
                # Notify reconciler
                if result.order_id:
                    engine.reconciler.notify_order_placed(result.order_id)
                
                # Store entry data for fill handling
                engine._last_entry_data = entry_data or {
                    'bias': ctx.bias,
                    'entry_price': ctx.current_price,
                    'position_size': size,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'atr_1m': ctx.atr_1m,
                    'order_type': 'MARKET' if use_market else 'LIMIT',
                    'tags': engine._current_trade_tags.copy(),
                }
                
                # Check for instant fill (backtest only)
                is_instant = order_result.get('status') in ('filled', 'closed') or order_result.get('filled', 0) > 0
                
                if engine._is_backtest and is_instant:
                    result.should_transition = True
                    result.transition_to = "MANAGE"
                else:
                    result.should_transition = True
                    result.transition_to = "WAIT_FILL"
                
            else:
                result.reason = "Order placement returned None"
                engine.state_machine.start_cooldown(5)
                
        except Exception as e:
            result.reason = str(e)
            logger.error(f"Order execution error: {e}")
            engine.state_machine.start_cooldown(5)
        
        return result


# =========================================================================
# MAIN ORCHESTRATOR
# =========================================================================

async def orchestrate_entry(engine: 'SignalEngine', current_price: float, atr_1m: float, market_time: datetime) -> EntryResult:
    """
    Main entry orchestration function.
    
    Replaces ~1500 lines of _execute_entry with clean orchestration flow.
    
    Returns:
        EntryResult with success status and any required state transitions
    """
    result = EntryResult()
    bias = engine.current_bias
    
    if not bias:
        result.reason = "No bias set"
        return result
    
    # Get market data
    candles_5m = engine.market_data.get_candles('5m')
    candles_1m = engine.market_data.get_candles('1m')
    candles_1h = engine.market_data.get_candles('1h')
    
    if not candles_5m or len(candles_5m) < 20:
        result.reason = "Not enough 5m data"
        return result
    
    if not candles_1h or len(candles_1h) < 50:
        result.reason = "Not enough 1h data"
        return result
    
    # Build context
    df_5m = pd.DataFrame(candles_5m)
    df_1m = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 200 else None
    df_1h = pd.DataFrame(candles_1h)
    session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
    
    ctx = EntryContext(
        current_price=current_price,
        atr_1m=atr_1m,
        market_time=market_time,
        bias=bias,
        df_5m=df_5m,
        df_1m=df_1m,
        df_1h=df_1h,
        session_name=session_name,
    )
    
    # Calculate ATRs
    ctx.atr_5m = engine._calculate_atr_5m(df_5m) if len(df_5m) >= 14 else atr_1m * 2
    ctx.atr_for_sl = ctx.atr_5m if ctx.atr_5m > 0 else atr_1m * 2.5
    
    # Get regime and volatility info
    ctx.regime = getattr(engine, '_current_regime', EntryRegime.NORMAL)
    ctx.volatility_regime = getattr(engine, '_current_volatility_regime', VolatilityRegime.NORMAL)  # Already a string
    ctx.trend_phase = getattr(engine, '_trend_phase', TrendPhase.RANGE_BOUND)
    
    # ========== PHASE 1: GUARDS ==========
    guard_result = await GuardOrchestrator.run_all_guards(engine, ctx)
    if not guard_result.passed:
        result.reason = guard_result.reason
        # Log blocked signal
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=guard_result.reason,
            block_category=guard_result.tags.get('block_category', 'guard'),
            score=0,
            atr=engine._last_momentum_atr
        )
        return result
    
    # ========== PHASE 2: SCORING ==========
    score_result = ScoreOrchestrator.calculate_score(engine, ctx, guard_result)
    
    # Structure gate
    if score_result.structure_blocks:
        result.reason = f"Structure gate ({score_result.breakdown.get('structure', 0)}/2)"
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=result.reason,
            block_category='filter',
            score=score_result.score,
            atr=engine._last_momentum_atr
        )
        engine._set_rejection_cooldown("Structure against entry", candles=2)
        return result
    
    # Score floor check
    score_mult = EntryGateConfig.get_score_size_multiplier(score_result.score)
    if score_mult == 0.0:
        result.reason = f"Score {score_result.score:.1f} below floor"
        return result
    
    # Session limit check (with override for high scores)
    if guard_result.tags.get('session_at_limit', False):
        if score_result.score < SessionTradeLimits.OVERRIDE_IF_SCORE_GTE:
            result.reason = f"Session limit ({guard_result.tags['session_trades']}/{guard_result.tags['session_limit']})"
            engine.missed_tracker.log_blocked_signal(
                direction=bias,
                price=current_price,
                block_reason=result.reason,
                block_category='session_limit',
                score=score_result.score,
                atr=engine._last_momentum_atr
            )
            return result
    
    # Store score
    engine._entry_score = score_result.score
    engine._score_breakdown = score_result.breakdown
    
    # ========== PHASE 3: QUALITY GATE ==========
    # Calculate preliminary chase for gate
    prelim_chase_atr = 0.0
    if ctx.df_1m is not None and OrderExecution.ANTI_CHASING_ENABLED:
        # 🔥 FIX: Import correct function name
        from src.application.live.market_analysis import calculate_chase_atr as calculate_chase_atr_helper
        prelim_chase_atr = calculate_chase_atr_helper(ctx.df_1m, atr_1m, bias)
    
    # Run entry quality gate
    micro_trend_str = 'NEUTRAL'
    micro_strength = 0.0
    if engine._micro_trend_state:
        micro_trend_str = engine._micro_trend_state.micro_trend or 'NEUTRAL'
        micro_strength = engine._micro_trend_state.strength or 0.0
    
    gate_result = engine._entry_gate.check(
        regime=ctx.regime.value if ctx.regime else "NORMAL",
        chase_atr=prelim_chase_atr,
        micro_trend=micro_trend_str,
        micro_strength=micro_strength,
        entry_direction=bias,
        session_quality=engine._current_trade_tags.get('session_quality', 'moderate'),
        htf_adx=ctx.adx_1h,
        has_rsi_divergence=False,
        near_vpoc=False,
        near_anchor_level=False,
    )
    
    if gate_result.decision == GateDecision.BLOCK:
        result.reason = f"Quality gate: {gate_result.reason}"
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=result.reason,
            block_category='quality_gate',
            score=score_result.score,
            atr=engine._last_momentum_atr
        )
        return result
    
    # ========== PHASE 4: CHASE VALIDATION ==========
    chase_result = engine._chase_validator.calculate_chase_penalty(
        df_1m=ctx.df_1m,
        atr_1m=atr_1m,
        bias=bias,
        current_regime=ctx.regime,
        score=score_result.score,
        trade_regime=getattr(engine, '_current_trade_regime', 'RANGE'),  # 🔥 FIX: Default to string
        has_bos=ctx.has_bos,
        adx_values=score_result.breakdown.get('adx_values', [20.0]),
        atr_5m=ctx.atr_5m,
        atr_avg=ctx.atr_5m,
        trend_expansion_active=getattr(engine, '_trend_expansion_active', False),
        anti_paralysis_level=getattr(engine, '_anti_paralysis_level', 0),
        df_5m=ctx.df_5m,
    )
    
    if chase_result.should_block:
        result.reason = f"Chase block: {chase_result.block_reason}"
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=result.reason,
            block_category='chase_penalty',
            score=score_result.score,
            atr=engine._last_momentum_atr
        )
        return result
    
    # Update score with chase penalty
    final_score = chase_result.adjusted_score
    
    # ========== PHASE 5: SL/TP CALCULATION ==========
    # Find support/resistance
    support_resistance = engine._find_support_resistance(bias, df_5m)
    
    # Get regime SL multiplier
    regime_config = engine.regime_detector.get_config(ctx.regime) if hasattr(engine, 'regime_detector') else None
    regime_sl_mult = regime_config.sl_atr_multiplier if regime_config else 2.0
    
    stop_loss = engine.risk_engine.calculate_stop_loss(
        current_price, bias, support_resistance, ctx.atr_for_sl,
        atr_ratio=getattr(engine, '_last_atr_ratio', None),
        regime_atr_mult=regime_sl_mult
    )
    
    risk = abs(current_price - stop_loss)
    
    # Get TP target
    daily_tp = engine._daily_regime_config.get('tp_r_target', AsymmetryConfig.BASE_TP_R) if hasattr(engine, '_daily_regime_config') and engine._daily_regime_config else RiskManagement.TP1_RISK_MULTIPLE
    tp_mult = daily_tp
    
    if bias == 'LONG':
        take_profit = current_price + (risk * tp_mult)
    else:
        take_profit = current_price - (risk * tp_mult)
    
    # ========== PHASE 5.5: HARD ENTRY BLOCKS (Jan 27, 2026) ==========
    # These are NON-NEGOTIABLE. NO bypass. NO exceptions.
    # Must be checked BEFORE RR validation which allows bypasses.
    from src.application.live.entry_validators import HardEntryBlockValidator
    
    # Get session quality
    session_quality = engine._current_trade_tags.get('session_quality', 'moderate')
    
    # Calculate worst-case RR for hard block check
    if hasattr(engine, 'slippage_model') and engine.slippage_model:
        from src.infrastructure.exchanges import OrderType
        worst_case_rr = engine.slippage_model.calculate_worst_case_rr(
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=bias,
            order_type=OrderType.LIMIT,
            volatility_ratio=getattr(engine, '_last_atr_ratio', 1.0)
        )
    else:
        # Fallback: estimate worst-case as 80% of ideal
        ideal_rr = abs(take_profit - current_price) / risk if risk > 0 else 0
        worst_case_rr = ideal_rr * 0.8
    
    # Get current ADX for range detection
    current_adx = ctx.adx_1h if hasattr(ctx, 'adx_1h') else None
    
    # Get timing score for HARD BLOCK check (Jan 27 analysis: timing=0 = high failure)
    score_timing = engine._current_trade_tags.get('score_timing', None)
    if score_timing is None:
        # Try to get from score breakdown
        score_breakdown = getattr(engine, '_score_breakdown', {})
        score_timing = score_breakdown.get('timing', None)
    
    # Apply HARD BLOCKS
    hard_block = HardEntryBlockValidator.validate_entry(
        session_quality=session_quality,
        worst_case_rr=worst_case_rr,
        adx=current_adx,
        score=final_score,
        score_timing=score_timing,
    )
    
    if hard_block.is_blocked:
        result.reason = hard_block.block_reason
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=hard_block.block_reason,
            block_category=hard_block.block_type,
            score=final_score,
            atr=engine._last_momentum_atr
        )
        return result
    
    # Store worst_case_rr for later use
    engine._current_trade_tags['rr_worst_case'] = worst_case_rr
    
    # ========== PHASE 6: RR VALIDATION ==========
    # NOTE: RR validation is now largely redundant since HARD BLOCKS already
    # validated worst_case_rr >= 0.7R. This provides sizing multiplier only.
    rr_result = engine._rr_validator.validate_rr(
        current_price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        bias=bias,
        score=final_score,
        current_regime=ctx.regime,
        regime_detector=engine.regime_detector,
        volatility_ratio=getattr(engine, '_last_atr_ratio', 1.0),
    )
    
    if not rr_result.is_valid:
        result.reason = f"RR invalid: {rr_result.block_reason}"
        return result
    
    # Apply RR size modifier (correct attribute name)
    rr_size_mult = rr_result.rr_size_multiplier
    engine._current_trade_tags['rr_size_mult'] = rr_size_mult
    
    # ========== PHASE 7: SIZING ==========
    sizing_result = SizingOrchestrator.calculate_size(
        engine, ctx, score_result, guard_result,
        stop_loss=stop_loss,
        gate_size_cap=gate_result.max_size_multiplier,
    )
    
    if not sizing_result.min_size_ok:
        result.reason = f"Size {sizing_result.size:.6f} below minimum"
        return result
    
    # ========== PHASE 8: COST VALIDATION ==========
    # Determine order type
    use_market = False
    if ctx.df_1m is not None and len(ctx.df_1m) >= 2:
        # 🔥 FIX: Use local calculate_momentum_velocity function
        velocity = calculate_momentum_velocity(ctx.df_1m, atr_1m)
        use_market = OrderExecution.USE_MARKET_ON_MOMENTUM and velocity > OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER
        
        if gate_result.require_limit and use_market:
            use_market = False
    
    cost_result = engine._cost_validator.validate_costs(
        position_size=sizing_result.size,
        current_price=current_price,
        stop_loss=stop_loss,
        bias=bias,
        use_market_order=use_market,
    )
    
    if not cost_result.is_valid:
        result.reason = f"Cost too high: {cost_result.block_reason}"
        return result
    
    # ========== PHASE 9: EXECUTE ==========
    logger.info(f"📊 ENTRY: {bias} | Score={final_score:.1f} | Size={sizing_result.size:.6f} | SL={stop_loss:.2f} | TP={take_profit:.2f}")
    
    entry_data = {
        'bias': bias,
        'entry_price': current_price,
        'position_size': sizing_result.size,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'atr_1m': atr_1m,
        'order_type': 'MARKET' if use_market else 'LIMIT',
        'tags': engine._current_trade_tags.copy(),
        'score': final_score,
        'score_breakdown': score_result.breakdown.copy(),
    }
    
    order_result = await OrderExecutor.place_order(
        engine, ctx, sizing_result.size, stop_loss, take_profit,
        use_market=use_market,
        entry_data=entry_data,
    )
    
    if order_result.success:
        # Handle state transition
        if order_result.transition_to == "MANAGE" and engine._is_backtest:
            # Instant fill in backtest - handle inline
            await _handle_instant_fill(engine, ctx, sizing_result.size, stop_loss, take_profit, final_score, score_result.breakdown)
        elif order_result.transition_to == "WAIT_FILL":
            engine.state_machine.start_entry_timer()
            engine.state_machine.transition_to(BotState.WAIT_FILL, "Order placed")
        
        # Update session trade count
        engine._session_trade_counts[session_name] = engine._session_trade_counts.get(session_name, 0) + 1
        
        # 🔥 FIX (Jan 27, 2026): Use CENTRALIZED activity tracker
        from src.domain.trading.activity_tracker import record_trade_executed
        record_trade_executed(market_time)
        engine._last_trade_time = market_time  # Keep local copy for backwards compat
    
    result.success = order_result.success
    result.order_id = order_result.order_id
    result.reason = order_result.reason
    result.tags = engine._current_trade_tags.copy()
    
    return result


async def _handle_instant_fill(
    engine: 'SignalEngine',
    ctx: EntryContext,
    size: float,
    stop_loss: float,
    take_profit: float,
    score: float,
    breakdown: Dict[str, Any],
):
    """Handle instant fill in backtest mode."""
    from src.domain.trading.strategy.pullback_short import TradeType
    
    # Record fill in position store
    try:
        fill_side = 'buy' if ctx.bias == 'LONG' else 'sell'
        await engine._position_store.record_fill(
            order_id=engine.pending_order_id or 'bt_order',
            symbol=engine.symbol,
            side=fill_side,
            size=size,
            price=ctx.current_price,
            fee=0.0,
            fill_type='MAKER',
            fill_id=f"{engine.pending_order_id}_fill",
        )
    except Exception as e:
        logger.warning(f"Could not update position_store on backtest fill: {e}")
    
    # Create position in risk engine
    trade_type = engine._current_trade_type or (TradeType.LONG_MAIN if ctx.bias == 'LONG' else TradeType.SHORT_STRUCTURAL)
    engine.risk_engine.create_position(
        symbol=engine.symbol,
        direction=ctx.bias,
        entry_price=ctx.current_price,
        position_size=size,
        stop_loss=stop_loss,
        atr_1m=ctx.atr_1m,
        market_time=ctx.market_time,
        trade_type=trade_type,
        regime_mode=getattr(engine, '_current_trade_regime', 'RANGE')  # 🔥 FIX: Default to string
    )
    
    # Start exhaustion tracking
    engine.exhaustion_detector.start_tracking(ctx.market_time)
    engine._exhaustion_tightened = False
    
    # Record trade open
    engine.state_machine.record_trade_open(engine.pending_order_id or "UNKNOWN")
    
    # Start trade logging
    try:
        session_name = ctx.session_name
        trade_type_name = trade_type.value if hasattr(trade_type, 'value') else str(trade_type)
        
        engine._current_trade_id = engine.trade_logger.start_trade(
            symbol=engine.symbol,
            direction=ctx.bias,
            entry_price=ctx.current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=size,
            entry_score=score,
            entry_tags=list(engine._current_trade_tags.keys()),
            regime=str(ctx.volatility_regime),
            volatility_regime=str(ctx.volatility_regime),
            session=session_name,
            trade_type=trade_type_name,
            score_breakdown=breakdown.copy(),
            metadata={},
            feature_snapshot=engine._build_ml_feature_snapshot() if hasattr(engine, '_build_ml_feature_snapshot') else {},
            entry_trading_mode=engine._current_trade_tags.get('trading_mode', ''),
            entry_daily_regime=getattr(engine, '_daily_regime', '')
        )
    except Exception as e:
        logger.error(f"TradeLogger.start_trade() failed: {e}")
        engine._current_trade_id = None
    
    # Clear pending state
    engine.pending_order_id = None
    engine.entry_order_time = None
    engine._last_entry_data = None
    
    # Record trade price
    engine._record_trade_price(ctx.current_price)
    
    # Transition to MANAGE
    engine.state_machine.transition_to(BotState.MANAGE, "Instant fill (backtest)")
    
    logger.info(f"🎯 Position opened: {ctx.bias} {size:.6f} @ {ctx.current_price:.2f}")


# =========================================================================
# HELPER FUNCTIONS
# =========================================================================

def calculate_momentum_velocity(df_1m: pd.DataFrame, atr_1m: float) -> float:
    """Calculate price velocity relative to ATR."""
    if len(df_1m) < 2:
        return 0.0
    
    last_close = df_1m['close'].iloc[-1]
    prev_close = df_1m['close'].iloc[-2]
    price_change = abs(last_close - prev_close)
    
    if atr_1m > 0:
        return price_change / atr_1m
    return 0.0
