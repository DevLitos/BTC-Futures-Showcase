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

import logging
import pandas as pd
from datetime import datetime
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

# =========================================================================
# 🔥 FIX #180 (Feb 11, 2026): LOG RATE LIMITING
# 
# PROBLEMA: Logs repetitivos ensucian los archivos:
#   - "FIX #171 STALE SIGNAL" cada tick (50+ veces)
#   - "FIX #147 Delta SEVERE" cada tick cuando delta es alto
#   - "FIX #176 Cross-block expired" cada tick después de timeout
#   - "PENALTY SUMMARY" cada tick con cualquier penalty
# 
# SOLUCIÓN: Rate limiter que sólo logea una vez por minuto para cada tipo.
# =========================================================================
_rate_limiter: dict[str, datetime] = {}  # log_key -> last_log_time
_RATE_LIMIT_SECONDS = 60  # Solo logear cada 60 segundos

def _should_log_rate_limited(key: str, interval_seconds: int = _RATE_LIMIT_SECONDS) -> bool:
    """Check if we should log this message or skip due to rate limiting."""
    now = datetime.utcnow()
    last_log = _rate_limiter.get(key)
    if last_log is None or (now - last_log).total_seconds() >= interval_seconds:
        _rate_limiter[key] = now
        return True
    return False
from src.infrastructure.exchanges.kraken.slippage_model import OrderType
from src.application.live.entry_executor import ExecutionGuards, LocationGuards
from src.domain.trading.execution_logic.entry_quality import GateDecision
from src.application.live.direction_state_manager import DirectionStateManager

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
    """Result of all guard checks.
    
    🔥 FIX #81 (Feb 07, 2026): Now uses RiskPenaltyAccumulator
    - Guards add penalties instead of hard blocking
    - Block decision made at end based on accumulated penalties
    - passed=False only for catastrophic events or size < 15% base
    """
    passed: bool = True
    reason: str = ""
    edge_size_mult: float = 1.0
    tags: Dict[str, Any] = field(default_factory=dict)
    
    # 🔥 FIX #81: Accumulated penalties
    final_size_mult: float = 1.0  # Product of all size penalties
    total_score_adj: float = 0.0  # Sum of all score adjustments
    penalty_sources: list = field(default_factory=list)  # Which guards penalized


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
    # 🔥 NEW (Jan 28): PROVISIONAL support
    is_provisional: bool = False
    provisional_reason: str = ''
    provisional_tp_r: float = 0.5
    provisional_time_stop_min: int = 5


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
    Essential pre-execution guards only — Opción A radical simplification.
    
    KEEP: Infrastructure guards + delta severe + session limits
    REMOVED: Location, ADX coherence, trend phase, funding, spread, obstinacy,
             displacement chase, location extreme, absorption, direction validation,
             fakeout, kill check, signal contradiction, penalty accumulator.
    """
    
    @staticmethod
    async def run_all_guards(engine: 'SignalEngine', ctx: EntryContext) -> GuardResult:
        """Run essential guards only — no penalty accumulator."""
        result = GuardResult()
        
        # =====================================================================
        # GUARD 0: HARD DAILY TRADE LIMIT — NO OVERRIDE POSSIBLE
        # Data Feb 17-19: 29 trades in 3 days, 13W/16L = -$4.34
        # Most losses come from overtrading. 2-3 quality trades > 10 mediocre.
        # =====================================================================
        HARD_DAILY_LIMIT = 5  # Maximum trades per day, NO exceptions
        daily_trades = getattr(engine.state_machine, 'daily_trades', 0)
        if daily_trades >= HARD_DAILY_LIMIT:
            result.passed = False
            result.reason = f"DAILY LIMIT: {daily_trades}/{HARD_DAILY_LIMIT} trades today (HARD CAP)"
            return result
        
        # =====================================================================
        # GUARD 1: Concurrency (pending orders, open positions) — HARD BLOCK
        # =====================================================================
        guard_result = await ExecutionGuards.check_all_guards(engine, ctx.current_price)
        if not guard_result.can_execute:
            result.passed = False
            result.reason = guard_result.block_reason
            return result
        
        # =====================================================================
        # GUARD 2: Edge enforcement — HARD BLOCK
        # =====================================================================
        can_trade, edge_mult, block_reason = await ExecutionGuards.check_edge_enforcement(engine, ctx.current_price)
        if not can_trade:
            result.passed = False
            result.reason = block_reason
            return result
        result.edge_size_mult = edge_mult
        
        # =====================================================================
        # GUARD 3: POST_ONLY rejection tracking — HARD BLOCK (infrastructure)
        # =====================================================================
        if hasattr(engine, '_consecutive_post_only_rejects') and engine._consecutive_post_only_rejects >= 3:
            result.passed = False
            result.reason = f"Market too fast: {engine._consecutive_post_only_rejects} POST_ONLY rejections"
            engine._consecutive_post_only_rejects = 0
            engine.state_machine.start_cooldown(5)
            return result
        
        # =====================================================================
        # GUARD 4: MACRO COHERENCE — Only block EXTREME macro mismatch
        # Only BEAR_STRONG / BULL_STRONG are vetoed (not BEAR / BULL).
        # Momentum override (displacement/expansion) bypasses this guard
        # because price action has proven the macro is breaking.
        # =====================================================================
        macro_strength = getattr(engine, '_current_bias_strength', None)
        macro_str = macro_strength.value if macro_strength else None
        has_momentum = getattr(engine, 'has_momentum_override', False)
        if macro_str and not has_momentum:
            is_macro_incoherent = (
                (macro_str == 'BEAR_STRONG' and ctx.bias == 'LONG') or
                (macro_str == 'BULL_STRONG' and ctx.bias == 'SHORT')
            )
            if is_macro_incoherent:
                result.passed = False
                result.reason = f"MACRO VETO: {ctx.bias} blocked by macro={macro_str}"
                # Also expire the stale edge that shouldn't exist
                if hasattr(engine, '_edge_manager'):
                    edge_mgr = engine._edge_manager
                    if edge_mgr.state.name in ('EDGE_ACTIVE', 'EDGE_ARMED'):
                        from src.domain.trading.edge_state_manager import EdgeState
                        edge_mgr._transition_to(
                            EdgeState.EDGE_EXPIRED,
                            f"MACRO VETO: {ctx.bias} vs macro={macro_str}",
                            ctx.market_time
                        )
                        logger.warning(
                            f"🚫 MACRO VETO: Expired stale {ctx.bias} edge | macro={macro_str}"
                        )
                return result

        # =====================================================================
        # GUARD 5: Delta SEVERE contradict — HARD BLOCK
        # Only block on extreme delta (>5x) against direction
        # =====================================================================
        delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', 0)
        delta_severe = (
            (ctx.bias == 'LONG' and delta_ratio < -5.0) or
            (ctx.bias == 'SHORT' and delta_ratio > 5.0)
        )
        if delta_severe:
            result.passed = False
            result.reason = f"Delta SEVERE: {delta_ratio:+.2f}x vs {ctx.bias}"
            return result
        
        # =====================================================================
        # CONTEXT: Calculate ADX for scorer and sizing
        # =====================================================================
        if len(ctx.df_1h) >= 20:
            adx_1h, _ = calculate_adx(
                ctx.df_1h['high'].to_numpy(),
                ctx.df_1h['low'].to_numpy(),
                ctx.df_1h['close'].to_numpy(), 14
            )
            if len(adx_1h) > 0:
                ctx.adx_1h = float(adx_1h[-1])
        
        # =====================================================================
        # GUARD 6: Session limits with Hot Hand
        # =====================================================================
        session_trades = engine._session_trade_counts.get(ctx.session_name, 0)
        base_limit = SessionTradeLimits.MAX_TRADES_PER_SESSION.get(ctx.session_name, 3)
        current_mode = getattr(engine, '_current_trade_regime', 'RANGE')
        mode_cap = SessionTradeLimits.MODE_TRADE_LIMIT_CAP.get(current_mode, 5)
        base_limit = min(base_limit, mode_cap)
        
        from src.domain.trading.activity_tracker import get_activity_tracker
        tracker = get_activity_tracker()
        tracker.reset_session_state(ctx.session_name)
        
        session_limit = base_limit
        hot_hand_bonus = 0
        session_stopped = False
        
        if session_trades >= base_limit:
            if tracker.last_trade_was_win():
                hot_hand_bonus = 1
                session_limit = session_trades + 1
                result.tags['hot_hand_bonus'] = hot_hand_bonus
            else:
                session_pnl_r = tracker.get_session_pnl_r()
                last_exit = tracker.get_last_exit_reason()
                last_pnl_r = tracker.get_last_trade_pnl_r()
                
                is_administrative_exit = last_exit and any(x in last_exit for x in [
                    'EARLY_MFE_EXIT', 'WORST_TRADE_PREVENTION', 'TIME_STOP', 'SYNC_CLOSE'
                ])
                is_small_loss = last_pnl_r is not None and last_pnl_r > -0.25
                allow_despite_loss = (
                    session_pnl_r >= 0.5 or
                    (session_pnl_r >= -0.25 and is_small_loss) or
                    is_administrative_exit
                )
                
                if allow_despite_loss:
                    session_limit = session_trades + 1
                    result.tags['session_profitable_override'] = True
                    result.tags['session_pnl_r'] = session_pnl_r
                else:
                    session_stopped = True
                    result.tags['session_stopped'] = True
                    result.tags['session_pnl_r'] = session_pnl_r
        
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
        
        # 🔥 FIX (Jan 30) + FIX #62 (Feb 06): Check if DISPLACEMENT is active for structure bypass
        # FIX #62: The edge_type check alone misses cases where:
        # 1. Displacement was detected → edge becomes TREND with CONSENSUS_DIRECT
        # 2. The _displacement_bypass flag is still True
        # 3. But EdgeType is TREND, not DISPLACEMENT
        # Solution: Also check _displacement_bypass from trading_app state
        displacement_edge_active = False
        edge_mgr = getattr(engine, '_edge_manager', None)
        if edge_mgr is not None:
            from src.domain.trading.edge_state_manager import EdgeState, EdgeType
            displacement_edge_active = (
                edge_mgr.state == EdgeState.EDGE_ACTIVE and 
                edge_mgr.current_bos is not None and
                edge_mgr.current_bos.edge_type == EdgeType.DISPLACEMENT
            )
        
        # 🔥 FIX #62: Also check trading_app's displacement_bypass flag
        # This catches cases where DISPLACEMENT was detected but edge_type is TREND
        if not displacement_edge_active:
            displacement_bypass_flag = engine._current_trade_tags.get('displacement_bypass', False)
            trend_displacement = engine._current_trade_tags.get('trend_displacement', False)
            if displacement_bypass_flag or trend_displacement:
                displacement_edge_active = True
                logger.debug(
                    f"🔥 FIX #62: DISPLACEMENT detected via trading_app flags | "
                    f"displacement_bypass={displacement_bypass_flag} trend_displacement={trend_displacement}"
                )
        
        expansion_mode_active = engine._current_trade_tags.get('expansion_mode_activated', False)
        effective_adx = engine._current_trade_tags.get('effective_adx', 0) or engine._current_trade_tags.get('htf_adx', 0)
        
        # 🔥 FIX #260c: Suppress ADX bypass during post-BIAS-FLIP stabilization
        # After BIAS FLIP, high ADX reflects the OLD trend direction, not the new one.
        # ADX=72 after SHORT→LONG flip means SHORT was strong, not that LONG will be.
        if hasattr(engine, '_post_bias_flip_cooldown_until') and engine._post_bias_flip_cooldown_until:
            from datetime import datetime as _dt
            if _dt.now() < engine._post_bias_flip_cooldown_until:
                effective_adx = 0
        
        # Base score from 4-block scorer — no mutations (Opción A)
        score, breakdown, structure_blocks = engine._entry_scorer.calculate_entry_score(
            df_1h=ctx.df_1h,
            df_5m=ctx.df_5m,
            df_1m=df_1m_safe,
            bias=ctx.bias,
            current_price=ctx.current_price,
            market_time=ctx.market_time,
            micro_trend_state=getattr(engine, '_micro_trend_state', None),
            filter_pipeline=getattr(engine, 'filter_pipeline', None),
            displacement_edge_active=displacement_edge_active,
            expansion_mode_active=expansion_mode_active,
            strong_trend_adx=effective_adx,
        )
        
        result.score = float(score)
        result.breakdown = breakdown
        result.structure_blocks = structure_blocks
        result.threshold = 5.0
        
        return result
    

# =========================================================================
# SIZING ORCHESTRATOR
# =========================================================================

class SizingOrchestrator:
    """
    Calculates final position size with all multipliers.
    
    SIMPLIFIED MODE (Jan 27, 2026):
    Only 5 multipliers: RR, REGIME, LOSS_STREAK, SESSION + base_risk
    Everything else becomes HARD BLOCK or 1.0
    
    LEGACY MODE (for comparison):
    All 13+ multipliers active
    
    🔥 FIX #60.3 (Feb 06): SIZING ORDER DOCUMENTATION
    =================================================
    Size caps are applied as MULTIPLIERS, meaning they are multiplicative
    with other factors, not absolute caps. The order is:
    
    1. Base size = capital * risk_pct / price_risk
    2. Edge multiplier (1.0-1.5x for institutional, 0.15-0.30x for intraday)
    3. Regime multiplier (0.5-1.0x based on ADX)
    4. Session multiplier (0.6-1.0x based on session quality)
    5. Loss streak multiplier (0.5-1.0x if consecutive losses)
    6. Spread multiplier (0.5-1.0x if spread is wide)
    7. Countertrend multiplier (0.3-0.5x for CT trades)
    8. CAPS APLICADOS ÚLTIMOS (siempre son min con final):
       - obstinacy_soft_size_cap (0.5x when SOFT MODE active)
       - kill_check_size_cap (0.5x when kill switch partial)
       - gate_size_cap (0.5x when guards detect risk)
    
    IMPORTANTE: Los caps son MULTIPLICATIVOS, no ABSOLUTOS.
    Si obstinacy_soft_cap = 0.5 y el size calculado es 0.001 BTC,
    el final será 0.0005 BTC, NO 0.5 BTC.
    """
    
    @staticmethod
    def calculate_size_simplified(
        engine: 'SignalEngine',
        ctx: EntryContext,
        stop_loss: float,
        worst_case_rr: float,
    ) -> OrchestrationSizingResult:
        """
        SIMPLIFIED SIZING: 5 multipliers only.
        
        Philosophy: Stop reducing damage, start exploiting edge.
        
        🔥 NEW (Jan 28, 2026): Edge Mode Integration
        - INSTITUTIONAL edge (BREAKOUT/TREND with high conviction): 1.0x-1.5x
        - INTRADAY edge (micro-inefficiencies): 0.15x-0.30x
        """
        from src.config.simplified_sizing import SimplifiedSizer, SimplifiedFeatureFlags
        
        result = OrchestrationSizingResult()
        
        # Get values needed for simplified sizing
        capital = engine.risk_engine.capital  # Direct access to capital attribute
        consecutive_losses = getattr(engine, '_consecutive_losses', 0)
        session_quality = engine._current_trade_tags.get('session_quality', 'moderate')
        regime_str = engine._current_regime.value if engine._current_regime else "NORMAL"
        adx = ctx.adx_1h if hasattr(ctx, 'adx_1h') else None
        
        # =====================================================================
        # 🔥 REFACTORED (Feb 05, 2026): Simplified Edge Sizing
        # 
        # Uses EdgeType-based sizing from get_edge_size_multiplier()
        # Replaces old INSTITUTIONAL/INTRADAY mode logic
        # =====================================================================
        edge_mode = 'EDGE_BASED'  # New unified mode
        edge_mode_multiplier = 0.60  # Default conservative
        edge_info = 'NO_EDGE'
        
        if hasattr(engine, '_edge_manager') and engine._edge_manager is not None:
            edge_manager = engine._edge_manager
            
            # Get sizing from new unified API
            min_mult, max_mult, edge_info = edge_manager.get_edge_size_multiplier(
                session_quality=session_quality
            )
            
            # Use max multiplier as the edge_mode_multiplier
            edge_mode_multiplier = max_mult
            edge_mode = edge_info  # DISPLACEMENT, BREAKOUT, RANGE, TREND_BOS, TREND_CONSENSUS, NO_EDGE
            
            if edge_info != 'NO_EDGE':
                if _should_log_rate_limited("edge_sizing", 120):
                    logger.info(f"📊 EDGE SIZING | Type: {edge_info} | Range: {min_mult:.2f}x-{max_mult:.2f}x | Using: {max_mult:.2f}x")
            else:
                logger.debug("📊 NO EDGE active - using conservative sizing 0.60x")
        
        # Store edge info in trade tags for analysis
        if hasattr(engine, '_current_trade_tags'):
            engine._current_trade_tags['edge_mode'] = edge_mode
            engine._current_trade_tags['edge_mode_multiplier'] = edge_mode_multiplier
            engine._current_trade_tags['edge_info'] = edge_info
        
        # Get last LOSS time for cooldown
        from src.domain.trading.activity_tracker import get_last_loss_time
        last_loss_time = get_last_loss_time()
        
        # Calculate using simplified 5-multiplier system — Opción A
        sizing_result = SimplifiedSizer.calculate(
            capital=capital,
            entry_price=ctx.current_price,
            stop_loss=stop_loss,
            worst_case_rr=worst_case_rr,
            regime_str=regime_str,
            adx=adx,
            consecutive_losses=consecutive_losses,
            session_quality=session_quality,
            edge_mode=edge_mode,
            edge_mode_multiplier=edge_mode_multiplier,
            session_name=ctx.session_name,
            direction=ctx.bias,
            market_time=ctx.market_time,
            last_trade_time=last_loss_time,
        )
        
        # Map to OrchestrationSizingResult
        if not sizing_result.should_trade:
            result.size = 0.0
            result.min_size_ok = False
            result.multipliers = {}  # Empty dict for blocked trades
            result.final_multiplier = 0.0
            logger.warning(f"🚫 SIMPLIFIED SIZING BLOCK: {sizing_result.block_reason}")
            return result
        
        result.size = sizing_result.final_size
        result.size = round_size_to_exchange(result.size)
        result.size_usd = result.size * ctx.current_price
        result.min_size_ok = result.size >= KRAKEN_BTC_MIN_SIZE
        
        # Store multipliers for logging — Opción A: 5 only
        result.multipliers = {
            'rr': sizing_result.rr_mult,
            'regime': sizing_result.regime_mult,
            'loss_streak': sizing_result.loss_streak_mult,
            'session': sizing_result.session_mult,
            'edge_mode': sizing_result.edge_mult,
        }
        result.final_multiplier = sizing_result.combined_multiplier
        
        # 🔥 NEW (Jan 28): Track PROVISIONAL state from sizing
        if sizing_result.is_provisional:
            result.is_provisional = True
            result.provisional_reason = sizing_result.provisional_reason
            result.provisional_tp_r = sizing_result.provisional_tp_r
            result.provisional_time_stop_min = sizing_result.provisional_time_stop_min
            engine._current_trade_tags['provisional'] = True
            engine._current_trade_tags['provisional_reason'] = sizing_result.provisional_reason
            engine._current_trade_tags['provisional_tp_r'] = sizing_result.provisional_tp_r
            engine._current_trade_tags['provisional_time_stop_min'] = sizing_result.provisional_time_stop_min
            logger.info(
                f"⚠️ PROVISIONAL ENTRY: {sizing_result.provisional_reason} | "
                f"Size: {result.size:.6f} | TP: {sizing_result.provisional_tp_r}R | "
                f"Time stop: {sizing_result.provisional_time_stop_min}min"
            )
        
        # Tag for tracking
        engine._current_trade_tags['simplified_sizing'] = True
        engine._current_trade_tags['simplified_multipliers'] = result.multipliers
        engine._current_trade_tags['edge_mode'] = edge_mode  # 🔥 NEW: Track edge mode
        
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
        aggressive_execution: bool = False,  # 🔥 FIX (Jan 30): Allow IOC for displacement
    ) -> EntryResult:
        """Place order and handle result."""
        result = EntryResult()
        
        try:
            # 🔥 FIX (Jan 30): DISPLACEMENT AGGRESSIVE EXECUTION
            # During DIRECT ENTRY WINDOW, post-only is counterproductive
            # The market is moving fast - either fill NOW or miss the trade
            if aggressive_execution:
                logger.warning(f"🚀 DISPLACEMENT AGGRESSIVE ENTRY: Using IOC (taker allowed) for {ctx.bias}")
                order_result = await engine.execution.place_aggressive_order(
                    symbol=engine.symbol,
                    side='buy' if ctx.bias == 'LONG' else 'sell',
                    amount=size,
                    price=ctx.current_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
            elif use_market:
                logger.warning("🚫 MAKER-ONLY ENFORCED: ignoring use_market=True → limit post-only first")
                logger.info(f"📊 Placing LIMIT post-only | {ctx.bias} {size:.6f} @ {ctx.current_price:.2f}")
                order_result = await engine.execution.place_limit_order(
                    symbol=engine.symbol,
                    side='buy' if ctx.bias == 'LONG' else 'sell',
                    amount=size,
                    price=ctx.current_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
                # If POST-ONLY rejected and system wanted market anyway → IOC fallback
                if order_result is None:
                    # 🔥 Re-validate costs with TAKER fees before IOC fallback
                    MIN_NET_R_IOC = 0.10
                    taker_cost = engine._cost_validator.validate_costs(
                        position_size=size, current_price=ctx.current_price,
                        stop_loss=stop_loss, take_profit=take_profit,
                        bias=ctx.bias, is_maker_entry=False, is_maker_exit=True,
                    )
                    if taker_cost.expected_net_r < MIN_NET_R_IOC:
                        logger.warning(
                            f"🚫 IOC BLOCKED: taker fees net_R={taker_cost.expected_net_r:.2f} < {MIN_NET_R_IOC} | "
                            f"Post-only rejected but IOC not viable (use_market path)"
                        )
                    else:
                        logger.warning(f"🔄 POST-ONLY REJECTED (use_market path) → IOC FALLBACK")
                        order_result = await engine.execution.place_aggressive_order(
                            symbol=engine.symbol,
                            side='buy' if ctx.bias == 'LONG' else 'sell',
                            amount=size,
                            price=ctx.current_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit
                        )
                        if order_result:
                            logger.info(f"✅ IOC FALLBACK SUCCESS (use_market path): Filled as taker | net_R={taker_cost.expected_net_r:.2f}")
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
                
                # 🔥 FIX #65+: ALWAYS FALLBACK TO IOC IF POST-ONLY REJECTED
                # Taker fee diff ~0.05% ($0.03 on $60 position) vs losing the trade entirely
                # Score 8.0 trade was lost to POST-ONLY reject with no retry — unacceptable
                if order_result is None:
                    # 🔥 Re-validate costs with TAKER fees before IOC fallback
                    # TP floor (0.88R) gives net=0.655R with maker, but with taker: net=0.587R < 0.60
                    MIN_NET_R_IOC = 0.10
                    taker_cost = engine._cost_validator.validate_costs(
                        position_size=size, current_price=ctx.current_price,
                        stop_loss=stop_loss, take_profit=take_profit,
                        bias=ctx.bias, is_maker_entry=False, is_maker_exit=True,
                    )
                    if taker_cost.expected_net_r < MIN_NET_R_IOC:
                        logger.warning(
                            f"🚫 IOC BLOCKED: taker fees net_R={taker_cost.expected_net_r:.2f} < {MIN_NET_R_IOC} | "
                            f"Post-only rejected but IOC not viable"
                        )
                    else:
                        logger.warning(f"🔄 POST-ONLY REJECTED → IOC FALLBACK: {ctx.bias} {size:.6f} @ {ctx.current_price:.2f}")
                        order_result = await engine.execution.place_aggressive_order(
                            symbol=engine.symbol,
                            side='buy' if ctx.bias == 'LONG' else 'sell',
                            amount=size,
                            price=ctx.current_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit
                        )
                        if order_result:
                            logger.info(f"✅ IOC FALLBACK SUCCESS: Filled as taker | net_R={taker_cost.expected_net_r:.2f}")
            
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
                    'order_type': 'LIMIT',
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
                # 🔥 FIX #79 (Feb 07): POST-ONLY reject is NOT a real failure
                # It just means price moved - try again in 1 minute, not 5
                result.reason = "Order placement returned None (likely POST-ONLY reject)"
                engine.state_machine.start_cooldown(1)  # Reduced from 5 to 1 minute
                
        except Exception as e:
            result.reason = str(e)
            logger.error(f"Order execution error: {e}")
            engine.state_machine.start_cooldown(1)  # Reduced from 5 to 1 minute
        
        return result


# =========================================================================
# SHARED: Post-fill state transitions (used by fast path + backtest)
# =========================================================================

async def _finalize_entry(
    engine: 'SignalEngine',
    direction: str,
    fill_price: float,
    fill_size: float,
    stop_loss: float,
    take_profit: float,
    atr_1m: float,
    market_time: datetime,
    score: float,
    breakdown: Dict[str, Any],
    order_id: Optional[str] = None,
    fill_type: str = 'MAKER',
    set_exchange_stops: bool = False,
    transition_reason: str = "Entry filled",
    entry_type: str = '',
    metadata: Optional[Dict] = None,
):
    """Shared post-fill state transitions for all entry paths."""
    from src.domain.trading.state.state_machine import BotState
    from src.domain.trading.strategy.pullback_short import TradeType
    from src.config.trading_config import get_session_name

    # 1. Record fill in position_store
    try:
        fill_side = 'buy' if direction == 'LONG' else 'sell'
        await engine._position_store.record_fill(
            order_id=order_id or 'unknown',
            symbol=engine.symbol,
            side=fill_side,
            size=fill_size,
            price=fill_price,
            fee=0.0,
            fill_type=fill_type,
            fill_id=f"{order_id}_fill",
        )
    except Exception as e:
        logger.warning(f"⚠️ position_store.record_fill failed: {e}")

    # 2. Create position in risk_engine
    trade_type = engine._current_trade_type or (
        TradeType.LONG_MAIN if direction == 'LONG' else TradeType.SHORT_STRUCTURAL
    )
    entry_score = engine._current_trade_tags.get('entry_score', score)
    edge_type = engine._current_trade_tags.get('edge_type', 'DISPLACEMENT')
    bos_quality = engine._current_trade_tags.get('bos_quality', 'NORMAL')
    is_weekend = market_time.weekday() >= 5 if market_time else False

    engine.risk_engine.create_position(
        symbol=engine.symbol,
        direction=direction,
        entry_price=fill_price,
        position_size=fill_size,
        stop_loss=stop_loss,
        atr_1m=atr_1m,
        market_time=market_time,
        trade_type=trade_type,
        regime_mode=getattr(engine, '_current_trade_regime', 'RANGE'),
        edge_type=edge_type,
        entry_score=entry_score,
        is_weekend=is_weekend,
        bos_quality=bos_quality,
    )

    # 3. Start exhaustion tracking + reset state
    engine.exhaustion_detector.start_tracking(market_time)
    engine._exhaustion_tightened = False
    engine._exhaustion_partial_taken = False
    engine._gap_history = []
    engine._prev_tick_price = fill_price

    # 4. Record trade open + price
    engine.state_machine.record_trade_open(order_id or "UNKNOWN")
    engine._record_trade_price(fill_price)

    # 5. Start trade logging
    try:
        session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
        trade_type_name = trade_type.value if hasattr(trade_type, 'value') else str(trade_type)
        edge_type_for_log = engine._current_trade_tags.get('edge_type', '')
        volatility_regime = getattr(engine, '_current_volatility_regime', 'NORMAL')

        engine._current_trade_id = engine.trade_logger.start_trade(
            symbol=engine.symbol,
            direction=direction,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=fill_size,
            entry_score=entry_score or 0,
            entry_tags=list(engine._current_trade_tags.keys()),
            regime=str(volatility_regime),
            volatility_regime=str(volatility_regime),
            session=session_name,
            trade_type=trade_type_name,
            score_breakdown=breakdown.copy() if breakdown else {},
            metadata=metadata or {},
            feature_snapshot=engine._build_ml_feature_snapshot() if hasattr(engine, '_build_ml_feature_snapshot') else {},
            entry_trading_mode=engine._current_trade_tags.get('trading_mode', ''),
            entry_daily_regime=getattr(engine, '_daily_regime', ''),
            edge_type=edge_type_for_log,
        )
    except Exception as e:
        logger.error(f"TradeLogger.start_trade() failed: {e}")
        engine._current_trade_id = None

    # 6. Set SL/TP on exchange (live only)
    if set_exchange_stops:
        try:
            stops_set = await engine.execution.set_position_stops(
                symbol=engine.symbol,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            if stops_set:
                logger.info(f"✅ SL/TP set: SL={stop_loss:.2f}, TP={take_profit:.2f}")
                engine._stops_set = True
                if engine.risk_engine.position:
                    engine.risk_engine.position.mark_reduce_only_validated()
                    engine.risk_engine.position.mark_exchange_sl_active()
            else:
                logger.error(f"🚨 SL/TP FAILED - retrying SL only")
                retry = await engine.execution.set_position_stops(
                    symbol=engine.symbol, stop_loss=stop_loss, take_profit=None,
                )
                if retry:
                    engine._stops_set = True
                    if engine.risk_engine.position:
                        engine.risk_engine.position.mark_exchange_sl_active()
                else:
                    logger.critical(
                        f"🚨 DANGER: Position {direction} {fill_size:.6f} @ {fill_price:.2f} HAS NO EXCHANGE SL!"
                    )
                    engine._stops_set = False
        except Exception as e:
            logger.error(f"🚨 SL/TP exception: {e}")
            engine._stops_set = False

    # 7. Clear pending state + transition to MANAGE
    engine.pending_order_id = None
    engine.entry_order_time = None
    engine._last_entry_data = None
    engine.state_machine.transition_to(BotState.MANAGE, transition_reason)

    logger.info(f"🎯 Position opened: {direction} {fill_size:.6f} @ {fill_price:.2f} → MANAGE")

    # 8. Telegram notification
    if engine.output_manager:
        try:
            from src.application.live.trading_app import _create_safe_task
            _create_safe_task(engine.output_manager.send_entry_notification({
                'symbol': engine.symbol,
                'direction': direction,
                'entry_price': fill_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'size': fill_size,
                'score': entry_score,
                'regime': getattr(engine, '_current_regime', None),
                'risk_usd': engine._current_trade_tags.get('risk_usd'),
                'timestamp': market_time.isoformat() if hasattr(market_time, 'isoformat') else str(market_time),
                'entry_type': entry_type,
            }), "send_entry_notification")
        except Exception as e:
            logger.warning(f"⚠️ Entry notification error: {e}")
# Displacement fast path extracted to displacement_fast_path.py
from src.application.live.displacement_fast_path import _displacement_fast_path



# =========================================================================
# MAIN ORCHESTRATOR
# =========================================================================

async def orchestrate_entry(engine: 'SignalEngine', current_price: float, atr_1m: float, market_time: datetime) -> EntryResult:
    """
    Main entry orchestration function.

    Returns:
        EntryResult with success status and any required state transitions
    """
    result = EntryResult()

    # Warmup check
    from src.config.trading_config import RiskManagement

    if RiskManagement.WARMUP_ENABLED and not getattr(engine, '_warmup_complete', False):
        idle_count = getattr(engine, '_idle_eval_count', 0)
        candles_count = getattr(engine, '_candles_since_start', 0)

        if idle_count < RiskManagement.WARMUP_MIN_TICKS or candles_count < RiskManagement.WARMUP_MIN_CANDLES:
            result.reason = f"⏳ WARMUP: {idle_count}/{RiskManagement.WARMUP_MIN_TICKS} ticks, {candles_count}/{RiskManagement.WARMUP_MIN_CANDLES} candles"
            return result
        else:
            engine._warmup_complete = True
            logger.info(f"✅ WARMUP COMPLETE: {idle_count} ticks, {candles_count} candles observed")
    
    bias = engine.current_bias
    
    if not bias:
        result.reason = "No bias set"
        return result
    
    # =====================================================================
    # 🔥 DIRECTION MEMORY: Block same-direction normal entry for 30 minutes
    # Prevents chasing the same move after a trade already captured it.
    # =====================================================================
    _SAME_DIR_BLOCK_NORMAL = 1800  # 30 minutes
    _last_dir_n = getattr(engine, '_last_trade_direction', None)
    _last_exit_n = getattr(engine, '_last_trade_exit_time', None)
    if _last_dir_n and _last_exit_n and _last_dir_n == bias:
        from datetime import datetime as _dt_n
        _elapsed_n = (_dt_n.now() - _last_exit_n).total_seconds()
        if _elapsed_n < _SAME_DIR_BLOCK_NORMAL:
            _remaining_n = _SAME_DIR_BLOCK_NORMAL - _elapsed_n
            if _should_log_rate_limited("direction_memory_normal", 120):
                logger.info(
                    f"🚫 DIRECTION MEMORY: {bias} blocked — last trade was {_last_dir_n} "
                    f"{_elapsed_n:.0f}s ago | Wait {_remaining_n:.0f}s"
                )
            result.reason = f"DIRECTION MEMORY: {bias} blocked for {_remaining_n:.0f}s after last {_last_dir_n}"
            return result
    
    # =====================================================================
    # 🔥 LOCATION GATE (Feb 20, 2026) — THE #1 FIX
    # Data: System was buying at the TOP and selling at the BOTTOM.
    # Example: LONG @ $68868 when SwingH=$68448 (116% of range!) = instant loss
    # Example: SHORT @ $65962 when SwingL=$65826 (5% of range!) = instant loss
    #
    # RULE: In RANGE market, only trade at favorable locations:
    #   - LONG only when price < 70% of swing range
    #   - SHORT only when price > 30% of swing range
    # DISPLACEMENT edge bypasses this (genuine momentum breakout).
    # =====================================================================
    swing_high = getattr(engine, '_last_swing_high', 0)
    swing_low = getattr(engine, '_last_swing_low', 0)
    current_edge_type_for_location = engine._current_trade_tags.get('edge_type', None)
    
    if swing_high > 0 and swing_low > 0 and swing_high > swing_low:
        swing_range = swing_high - swing_low
        range_position = (current_price - swing_low) / swing_range  # 0.0 = bottom, 1.0 = top
        engine._current_trade_tags['range_position_pct'] = round(range_position * 100, 1)
        
        # Only apply location gate for NON-displacement entries
        if current_edge_type_for_location != 'DISPLACEMENT':
            if bias == 'LONG' and range_position > 0.70:
                result.reason = (
                    f"LOCATION GATE: LONG blocked at {range_position:.0%} of range "
                    f"(>{70}%) | price={current_price:.0f} SwH={swing_high:.0f} SwL={swing_low:.0f}"
                )
                if _should_log_rate_limited("location_gate", 120):
                    logger.warning(f"🚫 {result.reason}")
                return result
            
            if bias == 'SHORT' and range_position < 0.30:
                result.reason = (
                    f"LOCATION GATE: SHORT blocked at {range_position:.0%} of range "
                    f"(<{30}%) | price={current_price:.0f} SwH={swing_high:.0f} SwL={swing_low:.0f}"
                )
                if _should_log_rate_limited("location_gate", 120):
                    logger.warning(f"🚫 {result.reason}")
                return result
    
    # Parallel DI eval removed — Opción A
    
    # =========================================================================
    # 🔥🔥🔥 PRICE DIRECTION VALIDATION - BACKUP CHECK (Feb 04, 2026) 🔥🔥🔥
    # =========================================================================
    # NOTE: La validación PRINCIPAL está ahora en trading_app.py _price_confirms_direction()
    # que bloquea CONSENSUS entries cuando el precio contradice la dirección.
    #
    # =========================================================================
    # 🔥 FIX #90 (Feb 09, 2026): ELIMINADO PRICE_DIRECTION_BLOCK
    # 
    # PROBLEMA: Este bloqueo era CONTRAPRODUCENTE porque:
    # 1. Detecta displacement SHORT (precio cayó $500)
    # 2. Pero bloquea porque "price DOWN 0.5%" → ¡Exactamente lo que queremos!
    # 3. Resultado: Nunca entra, siempre llega tarde
    #
    # El displacement YA valida la dirección correcta. Este bloqueo duplicado
    # solo servía para crear ciclos donde siempre llegamos tarde.
    # =========================================================================
    # REMOVED: price_direction_block logic - was killing valid entries
    
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
    # 🔧 FIX (Feb 03): Reducido de 200 a 30 velas - el scorer solo necesita ~20-30
    # PROBLEMA: df_1m=None causaba Score=0 en FAST PATH → sizing mínimo en buenos trades
    df_1m = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 30 else None
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
    
    # =========================================================================
    # 🚀🚀🚀 DISPLACEMENT FAST PATH (Jan 30, 2026) 🚀🚀🚀
    # =========================================================================
    # PROBLEMA: El sistema trata DISPLACEMENT como "setup a evaluar"
    # REALIDAD: DISPLACEMENT es un EVENTO URGENTE - o entras ahora o no entras
    #
    # FAST PATH bypasea TODA la ceremonia cuando:
    # 1. Edge DISPLACEMENT está ACTIVE
    # 2. bars_since_displacement <= 1 (VENTANA DURA: 0-1 velas, NO MÁS)
    # 3. Volume no colapsó
    #
    # El objetivo NO es el mejor precio
    # El objetivo es NO PERDER EL MOVIMIENTO
    # =========================================================================
    
    fast_path_result = await _displacement_fast_path(engine, ctx, current_price, atr_1m, market_time)
    if fast_path_result is not None:
        return fast_path_result
    
    # ========== PHASE 1: GUARDS ==========
    guard_result = await GuardOrchestrator.run_all_guards(engine, ctx)
    if not guard_result.passed:
        result.reason = guard_result.reason
        return result
    
    # Guard penalty propagation removed — Opción A (no penalty system)
    
    # ========== PHASE 2: SCORING ==========
    score_result = ScoreOrchestrator.calculate_score(engine, ctx, guard_result)
    
    # Structure gate
    if score_result.structure_blocks:
        result.reason = f"Structure gate ({score_result.breakdown.get('structure', 0)}/2)"
        engine._set_rejection_cooldown("Structure against entry", candles=2)
        return result
    
    # Tag weak momentum for sizing (replaces the removed -2.0 coherence penalty)
    momentum_score = score_result.breakdown.get('momentum', 0)
    if momentum_score == 0:
        engine._current_trade_tags['weak_momentum_size_mult'] = 0.70
    
    # =====================================================================
    # 🔥 TIMING GATE: Only enter at BIRTH of moves (BOS confirmed)
    # Data Feb 16-20: timing=0 trades had 14% WR (worst performer).
    # timing≥1 (BOS confirmed) = 60%+ WR.
    # Without confirmed BOS, entry is random noise, not a genuine move.
    # User demand: "SOLO COJA OPORTUNIDADES BUENAS EN EL NACIMIENTO"
    # =====================================================================
    timing_score = score_result.breakdown.get('timing', 0)
    if timing_score == 0:
        result.reason = f"TIMING GATE: No BOS confirmed (timing=0) — not a birth of a move"
        return result
    
    # Score floor check
    score_mult = EntryGateConfig.get_score_size_multiplier(score_result.score)
    if score_mult == 0.0:
        result.reason = f"Score {score_result.score:.1f} below floor"
        return result
    
    # =====================================================================
    # 🔥 TREND EDGE BLOCK (Feb 20, 2026)
    # Data (Feb 11-20): TREND edge = 28/41 trades, 18% WR, -$10.60 (83% of ALL losses)
    # 18/28 TREND trades die in TIME_INVALIDATION with avg MFE 0.11R (never moved)
    # The edge detection for TREND finds BOS in ranging market = noise.
    # ONLY allow DISPLACEMENT and RANGE edges which have actual signal.
    # =====================================================================
    current_edge_type = engine._current_trade_tags.get('edge_type', None)
    if current_edge_type == 'TREND':
        result.reason = f"TREND edge BLOCKED (Score={score_result.score:.1f}) — 83% of losses, 18% WR in data"
        return result

    # =====================================================================
    # 🔥 DISPLACEMENT CONSUMED GUARD — NORMAL PIPELINE (Feb 20, 2026)
    # Same guard as fast path: block all entries for 300s after any exit
    # to prevent re-entering on recycled edge data.
    # =====================================================================
    # Direction memory already handles same-direction blocking (30min) earlier.
    # This is a fallback: block ALL entries for 3 minutes after any exit
    # regardless of direction (market needs time to establish new equilibrium).
    last_exit_normal = getattr(engine, '_last_trade_exit_time', None)
    if last_exit_normal is not None:
        from datetime import datetime
        elapsed_since_exit = (datetime.now() - last_exit_normal).total_seconds()
        if elapsed_since_exit < 180:  # 3 minutes minimum after any exit
            result.reason = f"POST-EXIT COOLDOWN: {elapsed_since_exit:.0f}s < 180s since last exit"
            return result

    # Session limit check (with override for high scores)
    # 🔥 FIX #10: Handle Hot Hand stopped (loss occurred)
    # 🔥 FIX #82: Improved message clarity
    
    # Fusion gate removed — Opción A

    if guard_result.tags.get('session_stopped', False):
        session_pnl = guard_result.tags.get('session_pnl_r', 0.0)
        result.reason = (
            f"Session STOPPED (last trade was LOSS, {guard_result.tags['session_trades']} trades done, "
            f"session P&L: {session_pnl:+.2f}R)"
        )
        return result
    
    if guard_result.tags.get('session_at_limit', False):
        hot_hand_bonus = guard_result.tags.get('hot_hand_bonus', 0)
        if score_result.score < SessionTradeLimits.OVERRIDE_IF_SCORE_GTE:
            if hot_hand_bonus > 0:
                result.reason = f"Session limit ({guard_result.tags['session_trades']}/{guard_result.tags['session_limit']}) [+{hot_hand_bonus} hot hand]"
            else:
                result.reason = f"Session limit ({guard_result.tags['session_trades']}/{guard_result.tags['session_limit']})"
            return result
    
    # Store score
    engine._entry_score = score_result.score
    engine._score_breakdown = score_result.breakdown
    
    # Quality gate removed — Opción A
    
    # ========== PHASE 4: CHASE VALIDATION ==========
    # Chase validation removed — Opción A
    final_score = score_result.score
    
    # ========== PHASE 4.5: ML SCORE ADJUSTMENT (Feb 10, 2026) ==========
    # ML adjustment removed — Opción A
    
    # ========== PHASE 5: SL/TP CALCULATION ==========
    # Find support/resistance
    support_resistance = engine._find_support_resistance(bias, df_5m)
    
    # Get regime SL multiplier
    regime_config = engine.regime_detector.get_config(ctx.regime) if hasattr(engine, 'regime_detector') else None
    regime_sl_mult = regime_config.sl_atr_multiplier if regime_config else 2.0
    
    # 🔥 FIX #56: TIGHT STOPS in LOCATION EXTREME SOFT MODE
    # When trading in extreme location, use tighter stops to limit risk
    location_tight_stops = engine._current_trade_tags.get('location_extreme_tight_stops', False)
    if location_tight_stops:
        # Reduce SL multiplier by 30% for tighter stops
        original_mult = regime_sl_mult
        regime_sl_mult = max(1.0, regime_sl_mult * 0.7)  # At least 1.0 ATR
        logger.warning(
            f"⚠️ TIGHT STOPS: Location extreme → SL mult {original_mult:.1f} → {regime_sl_mult:.1f}"
        )
        engine._current_trade_tags['tight_stop_applied'] = True
        engine._current_trade_tags['tight_stop_original_mult'] = original_mult
        engine._current_trade_tags['tight_stop_new_mult'] = regime_sl_mult
    
    stop_loss = engine.risk_engine.calculate_stop_loss(
        current_price, bias, support_resistance, ctx.atr_for_sl,
        atr_ratio=getattr(engine, '_last_atr_ratio', None),
        regime_atr_mult=regime_sl_mult
    )
    
    risk = abs(current_price - stop_loss)
    
    # 🔥 SIMPLIFIED TP (Jan 27, 2026): Fixed TP based on regime only
    # 🔥 UPDATE (Jan 28, 2026): Added edge_type for BREAKOUT 2.0R minimum
    # 🔥 UPDATE (Feb 11, 2026): Added atr_expansion_ratio for ADAPTIVE TP
    from src.config.simplified_sizing import SimplifiedFeatureFlags, SimplifiedExitConfig
    
    if SimplifiedFeatureFlags.USE_FIXED_TP:
        # Get edge type from current trade tags
        current_edge_type = engine._current_trade_tags.get('edge_type', None)
        atr_expansion_ratio = engine._current_trade_tags.get('atr_expansion_ratio', None)
        
        # 🔥 FIX (Feb 20, 2026): Use trading_mode ('RANGE'/'TREND') as canonical regime
        # instead of engine._current_regime.value which returns raw classifier strings
        # like "low_participation_chop" that don't match get_tp_r()'s 'RANGE' check.
        # This ensures RANGE exchange TP = 0.30R (safety net if bot crashes).
        trading_mode_for_tp = engine._current_trade_tags.get('trading_mode', 'TREND')
        tp_mult = SimplifiedExitConfig.get_tp_r(
            regime_str=trading_mode_for_tp,
            adx=ctx.adx_1h if hasattr(ctx, 'adx_1h') else None,
            edge_type=current_edge_type,
            atr_expansion_ratio=atr_expansion_ratio
        )
        atr_ratio_str = f"{atr_expansion_ratio:.2f}" if atr_expansion_ratio else "N/A"
        logger.info(f"📊 SIMPLIFIED TP: {tp_mult:.2f}R (mode={trading_mode_for_tp}, edge={current_edge_type}, atr_ratio={atr_ratio_str})")
    else:
        # Legacy: Dynamic TP from daily regime config
        daily_tp = engine._daily_regime_config.get('tp_r_target', AsymmetryConfig.BASE_TP_R) if hasattr(engine, '_daily_regime_config') and engine._daily_regime_config else RiskManagement.TP1_RISK_MULTIPLE
        tp_mult = daily_tp
    
    if bias == 'LONG':
        take_profit = current_price + (risk * tp_mult)
    else:
        take_profit = current_price - (risk * tp_mult)
    
    # TP capping by historical range removed — Opción A

    # ========== PHASE 5.5: HARD ENTRY BLOCKS (Jan 27, 2026) ==========
    # Simplified worst-case RR estimate — Opción A
    ideal_rr = abs(take_profit - current_price) / risk if risk > 0 else 0
    worst_case_rr = ideal_rr * 0.8
    engine._current_trade_tags['rr_worst_case'] = worst_case_rr
    
    # ========== PHASE 6: RR VALIDATION ==========
    # RR floor — lowered for RANGE 0.30R TP (effective RR = 0.30/0.15 = 2.0)
    if worst_case_rr < 0.20:
        result.reason = f"RR too low: {worst_case_rr:.2f} < 0.20"
        return result
    
    # 🔥 FIX (Feb 19, 2026): ENFORCE REGIME RR MINIMUM FOR FADES
    # ROOT CAUSE: RegimeFusion correctly sets rr_min=2.0 for counter-trend
    # entries, but nobody checked it. Result: LONG entries at the top of
    # bounces in BEAR macro with 1.2R TP → guaranteed SL hit.
    # Now: if is_fade AND ideal_rr < rr_min → BLOCK.
    # 🔥 FIX (Feb 20, 2026): RANGE mean-reversion EXEMPT from fade RR check.
    # RANGE buys at support / sells at resistance with 0.30R TP — this is NOT
    # a directional fade. rr_min=2.0 was designed for TREND entries going
    # against macro. RANGE trades profit from many small wins at extremes.
    regime_is_fade = engine._current_trade_tags.get('regime_is_fade', False)
    regime_rr_min = engine._current_trade_tags.get('regime_rr_min', 1.0)
    current_trading_mode = engine._current_trade_tags.get('trading_mode', '')
    is_range_mode = current_trading_mode == 'RANGE'
    
    if regime_is_fade and ideal_rr < regime_rr_min and not is_range_mode:
        logger.warning(
            f"🚫 FADE RR BLOCK: ideal_rr={ideal_rr:.2f} < regime_rr_min={regime_rr_min:.1f} | "
            f"Counter-trend entry needs {regime_rr_min:.1f}R but can only achieve {ideal_rr:.2f}R | "
            f"macro={engine._current_trade_tags.get('regime_macro', '?')}, micro={bias}"
        )
        result.reason = f"FADE RR BLOCK: {ideal_rr:.2f}R < {regime_rr_min:.1f}R minimum for counter-trend"
        return result
    elif regime_is_fade and is_range_mode:
        logger.info(
            f"↔️ RANGE FADE PASS: ideal_rr={ideal_rr:.2f} | rr_min={regime_rr_min:.1f} bypassed | "
            f"RANGE mean-reversion exempt from fade RR check"
        )
    
    # ========== PHASE 7: SIZING ==========
    sizing_result = SizingOrchestrator.calculate_size_simplified(
        engine, ctx,
        stop_loss=stop_loss,
        worst_case_rr=worst_case_rr,
    )
    
    # Apply pipeline penalty tags as size multipliers
    penalty_tags = [
        ('weak_momentum_size_mult', 'WEAK_MOM'),
        ('micro_range_penalty', 'MICRO_RANGE'),
        ('low_atr_penalty', 'LOW_ATR'),
        ('low_volume_penalty', 'LOW_VOL'),
    ]
    applied_penalties = []
    for tag_name, label in penalty_tags:
        mult = engine._current_trade_tags.get(tag_name)
        if mult is not None and mult < 1.0:
            sizing_result.size *= mult
            applied_penalties.append(f"{label}={mult:.2f}")
    if applied_penalties:
        sizing_result.size = round_size_to_exchange(sizing_result.size)
        logger.info(f"📊 SIZE PENALTIES: {' × '.join(applied_penalties)} → {sizing_result.size:.6f}")
    
    if _should_log_rate_limited("sizing_info", 120):
        logger.info(f"📊 SIMPLIFIED SIZING: {sizing_result.multipliers}")
    
    if not sizing_result.min_size_ok:
        result.reason = f"Size {sizing_result.size:.6f} below minimum"
        return result
    
    # ========== PHASE 8: COST VALIDATION ==========
    # PASO 2 — MAKER ONLY REAL (SIN EXCEPCIONES)
    # Entrada: SOLO LIMIT + post-only
    # (No market entries, no chasing.)
    use_market = False
    
    cost_result = engine._cost_validator.validate_costs(
        position_size=sizing_result.size,
        current_price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        bias=bias,
        is_maker_entry=True,
        is_maker_exit=True,
    )
    
    if not cost_result.is_valid:
        result.reason = f"Cost too high: {cost_result.block_reason}"
        return result

    # =====================================================================
    # ✅ PASO 3 — BLOQUEA TRADES CON NET NEGATIVO
    # Lowered for RANGE 0.30R TP: net_R = 0.30 - fees(~0.10) = 0.20
    # =====================================================================
    MIN_EXPECTED_NET_R = 0.10
    if cost_result.expected_net_r < MIN_EXPECTED_NET_R:
        if _should_log_rate_limited("hard_block_net_r", 120):
            logger.warning(
                f"🚫 HARD BLOCK: net_R={cost_result.expected_net_r:.2f} < {MIN_EXPECTED_NET_R:.2f} "
                f"| fees_R={cost_result.fee_r_estimate:.2f} | profit=${cost_result.expected_profit_usd_net:.2f}"
            )
        result.reason = (
            f"HARD BLOCK: expected_net_R={cost_result.expected_net_r:.2f} < {MIN_EXPECTED_NET_R:.2f}"
        )
        return result

    # =====================================================================
    # ✅ PASO 4 — TAMAÑO MÍNIMO DE POSICIÓN (FIX #102d - Feb 09)
    # 🔥 REFACTORED: Eliminamos el "bypass por RR" que era autoengaño
    # 
    # PROBLEMA IDENTIFICADO:
    # - FIX #98 bypass permitía trades con profit $1.17 porque "RR >= 1.0"
    # - Esto es AUTOENGAÑO: ganas poco, pero permitido porque "matemáticamente" rentable
    # - Con slippage, fees reales, y ejecución imperfecta → break-even o pérdida
    # 
    # SOLUCIÓN FIX #102d:
    # 1. Mantener umbrales mínimos para cuentas pequeñas
    # 2. PERO exigir profit mínimo ABSOLUTO de $0.50 (cubre fees + buffer)
    # 3. Si profit < $0.50, BLOQUEAR sin excepciones
    # 4. Log para transparencia
    # =====================================================================
    capital = engine.risk_engine.capital if hasattr(engine, 'risk_engine') else 500.0
    
    # Umbrales de profit mínimo
    if capital < 500:
        MIN_PROFIT_PERCENT = 0.0004  # 0.04% for small accounts — was 0.15% which blocked ALL range entries
    else:
        MIN_PROFIT_PERCENT = 0.0010  # 0.10% for larger accounts
    MIN_PROFIT_FLOOR = 0.08     # Absolute floor — was $0.25, blocked $230 account completely
    
    min_expected_profit = max(capital * MIN_PROFIT_PERCENT, MIN_PROFIT_FLOOR)
    
    # 🔥 FIX #102d: NO HAY BYPASS - profit mínimo es sagrado
    # Si no puedes ganar $0.50 neto, no deberías estar en el trade
    if cost_result.expected_profit_usd_net < min_expected_profit:
        if _should_log_rate_limited("fix102d_block", 120):
            logger.warning(
                f"🚫 FIX #102d BLOCK: profit_net=${cost_result.expected_profit_usd_net:.2f} < ${min_expected_profit:.2f} | "
                f"net_R={cost_result.expected_net_r:.2f} | gross_R={ideal_rr:.2f} | size={sizing_result.size:.6f}"
            )
        result.reason = (
            f"FIX #102d BLOCK: expected_profit=${cost_result.expected_profit_usd_net:.2f} < ${min_expected_profit:.2f} "
            f"(minimum viable profit)"
        )
        return result
    else:
        logger.info(
            f"✅ Profit check PASS: ${cost_result.expected_profit_usd_net:.2f} >= ${min_expected_profit:.2f} | "
            f"net_R={cost_result.expected_net_r:.2f} | gross_R={ideal_rr:.2f}"
        )
    
    # Probe validation removed — Opción A
    
    # ========== PHASE 9: EXECUTE ==========
    logger.info(f"📊 ENTRY: {bias} | Score={final_score:.1f} | Size={sizing_result.size:.6f} | SL={stop_loss:.2f} | TP={take_profit:.2f}")
    
    entry_data = {
        'bias': bias,
        'entry_price': current_price,
        'position_size': sizing_result.size,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'atr_1m': atr_1m,
        'order_type': 'LIMIT',
        'tags': engine._current_trade_tags.copy(),
        'score': final_score,
        'score_breakdown': score_result.breakdown.copy(),
    }
    
    # Aggressive execution for displacement only — Opción A
    aggressive_execution = False
    edge_mgr = getattr(engine, '_edge_manager', None)
    if edge_mgr is not None:
        from src.domain.trading.edge_state_manager import EdgeState, EdgeType
        is_displacement_edge = (
            edge_mgr.state == EdgeState.EDGE_ACTIVE and 
            edge_mgr.current_bos is not None and
            edge_mgr.current_bos.edge_type == EdgeType.DISPLACEMENT
        )
        if is_displacement_edge:
            bars_since_disp = edge_mgr.current_bos.bars_since_displacement
            if bars_since_disp is not None and bars_since_disp <= 2:
                aggressive_execution = True
                logger.warning(f"🚀 DISPLACEMENT DIRECT ENTRY: bar {bars_since_disp}/2 → AGGRESSIVE")
    
    order_result = await OrderExecutor.place_order(
        engine, ctx, sizing_result.size, stop_loss, take_profit,
        use_market=False,
        entry_data=entry_data,
        aggressive_execution=aggressive_execution,  # 🔥 FIX: IOC for displacement
    )
    
    if order_result.success:
        # Handle state transition
        if order_result.transition_to == "MANAGE" and engine._is_backtest:
            # Instant fill in backtest - handle inline
            await _handle_instant_fill(engine, ctx, sizing_result.size, stop_loss, take_profit, final_score, score_result.breakdown)
        elif order_result.transition_to == "WAIT_FILL":
            engine.state_machine.start_entry_timer()
            engine.state_machine.transition_to(BotState.WAIT_FILL, "Order placed")
        
        # 🔥 FIX #196 (Feb 12, 2026): REMOVED DUPLICATE session_trade_counts increment
        # The counter is already incremented in trading_app.py line ~6926 when fill is confirmed.
        # Having it here too caused DOUBLE COUNTING: 2 real trades → 4 counted → session_limit blocked
        # See: "Session STOPPED (4 trades done)" but Health said "trades_today=2"
        
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
    """Handle instant fill in backtest mode — delegates to shared _finalize_entry."""
    await _finalize_entry(
        engine=engine,
        direction=ctx.bias,
        fill_price=ctx.current_price,
        fill_size=size,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_1m=ctx.atr_1m,
        market_time=ctx.market_time,
        score=score,
        breakdown=breakdown,
        order_id=engine.pending_order_id,
        fill_type='MAKER',
        set_exchange_stops=False,
        transition_reason="Instant fill (backtest)",
        entry_type='BACKTEST',
    )
