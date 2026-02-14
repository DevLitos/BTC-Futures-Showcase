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

# FIX #41: Rate limiting for guard logs - don't spam same message
_last_guard_log: dict[str, datetime] = {}  # guard_name -> last_log_time
_guard_log_interval = timedelta(minutes=2)  # Only log same guard every 2 minutes

# 🔧 FIX #129 (Feb 09, 2026): Throttle TRANSITION_MODE log
_last_transition_mode_log: Optional[datetime] = None

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
from src.application.live.entry_validators import ChaseResult
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
        # GUARD 4: Delta SEVERE contradict — HARD BLOCK
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
        # GUARD 5: Session limits with Hot Hand
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
        result.threshold = 5.0  # Fixed threshold — Opción A: no mutations, no dynamic threshold
        
        return result
    
    @staticmethod
    def _calculate_threshold(engine: 'SignalEngine', ctx: EntryContext) -> float:
        """Fixed threshold — Opción A: no dynamic adjustments."""
        return 5.0

    @staticmethod
    def _get_trend_phase_adjustment(trend_phase: TrendPhase, bias: str, engine: 'SignalEngine') -> float:
        """Disabled — Opción A."""
        return 0.0
    

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
                    MIN_NET_R_IOC = 0.60
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
                    MIN_NET_R_IOC = 0.60
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
# 🚀 DISPLACEMENT FAST PATH (Jan 30, 2026)
# =========================================================================
# Este es el pipeline URGENTE para displacement.
# Bypasea TODA la ceremonia de validación normal.
# 
# Filosofía: "Prefiero entrar feo que perder el tren"
# 
# Regla brutal:
#   Primer displacement válido = entrada inmediata o cancelación total
#   Si no puedes entrar dentro de 0-1 velas, NO es trade de displacement
# =========================================================================

async def _displacement_fast_path(
    engine: 'SignalEngine',
    ctx: 'EntryContext', 
    current_price: float,
    atr_1m: float,
    market_time: datetime
) -> Optional[EntryResult]:
    """
    FAST PATH para DISPLACEMENT - bypasea validación normal.
    
    🔥 FIX #160 (Feb 10): MODE_EXPANSION elimina frenos por barras
    En expansión de tendencia, no hay "chase block" por número de velas.
    Solo se bloquea si la ESTRUCTURA se invalida (precio cruza origen).
    
    Returns:
        EntryResult si debe ejecutar fast path
        None si debe continuar con pipeline normal
    """
    from src.domain.trading.edge_state_manager import EdgeState, EdgeType, DisplacementEdgeCriteria
    
    # =====================================================================
    # CHECK 1: ¿Hay DISPLACEMENT EDGE activo con entrada directa?
    # =====================================================================
    edge_mgr = getattr(engine, '_edge_manager', None)
    if not edge_mgr:
        return None
    
    # Solo actuar si DISPLACEMENT está ACTIVE
    if edge_mgr.state != EdgeState.EDGE_ACTIVE:
        return None
    
    current_bos = edge_mgr.current_bos
    if not current_bos:
        return None
    
    # Solo para DISPLACEMENT edge type
    if current_bos.edge_type != EdgeType.DISPLACEMENT:
        return None
    
    bars_since = current_bos.bars_since_displacement
    
    # =====================================================================
    # 🔥 FIX #186 (Feb 11, 2026): STALE + CHASE BLOCK for DISPLACEMENT
    # =====================================================================
    # PROBLEMA CRÍTICO: DISPLACEMENT bypasea TODAS las protecciones incluyendo:
    #   1. STALE SIGNAL (-1.5 penalty) → Un edge de 40 min igual que de 1 min
    #   2. CHASE detection → Precio ya subió $1,300 antes de entrar
    #   3. CONSENSUS BLOCKED → "LONG vs BEAR_STRONG" ignorado
    #
    # Caso real Feb 11: 
    #   - 14:32:05: Edge LONG @ $67,218
    #   - 14:35 → 15:10: BLOCKED por BEAR_STRONG (39 min)
    #   - 15:11:03: DISPLACEMENT detected, bypasea TODO
    #   - 15:11:04: Entry LONG @ $68,507 (top of 2% move)
    #   - Resultado: -0.32R
    #
    # SOLUCIÓN:
    #   - Edge age > 15 min → BLOCK (no solo penalty)
    #   - Price moved > 1.5 ATR en dirección del edge → CHASE BLOCK
    # =====================================================================
    
    # CHECK 1.5a: STALE BLOCK - No entrar con edge de más de 15 min
    MAX_EDGE_AGE_MINUTES = 15.0
    edge_age_min = edge_mgr.get_edge_age_minutes()
    if edge_age_min > MAX_EDGE_AGE_MINUTES:
        logger.warning(
            f"🚫 FIX #186 STALE BLOCK: DISPLACEMENT edge age {edge_age_min:.1f}min > {MAX_EDGE_AGE_MINUTES}min | "
            f"Edge too old - not chasing. FAST PATH BLOCKED."
        )
        # Expirar el edge para no seguir intentando
        edge_mgr._expire_edge(reason=f"STALE_BLOCK:age={edge_age_min:.1f}min")
        return None
    
    # CHECK 1.5b: CHASE BLOCK - No entrar si precio ya se movió > 1.5 ATR
    CHASE_THRESHOLD_ATR = 1.5
    disp_direction = current_bos.direction
    origin_price = current_bos.displacement_origin if hasattr(current_bos, 'displacement_origin') else current_bos.price
    
    if origin_price and atr_1m > 0:
        if disp_direction == 'LONG':
            price_moved = current_price - origin_price
        else:
            price_moved = origin_price - current_price
        
        chase_threshold = CHASE_THRESHOLD_ATR * atr_1m
        if price_moved > chase_threshold:
            logger.warning(
                f"🚫 FIX #186 CHASE BLOCK: Price moved ${price_moved:.0f} > ${chase_threshold:.0f} (1.5 ATR) | "
                f"Origin ${origin_price:.0f} → Current ${current_price:.0f} | "
                f"FAST PATH BLOCKED - would chase the move."
            )
            # NO expirar edge - solo bloquear fast path, dejar que pullback pipeline evalúe
            current_bos.force_direct_entry = False
            return None
    
    # =====================================================================
    # 🔥 FIX #160 (Feb 10): MODE_EXPANSION vs MODE_RANGE
    # 
    # PROBLEMA CRÍTICO: El sistema trataba TODOS los trades igual.
    # En RANGE: cooldowns, waits, chase blocks → correcto
    # En EXPANSION: los mismos frenos → DESTRUYE el edge
    #
    # SOLUCIÓN: Dos modos de operación completamente diferentes:
    #
    # MODE_RANGE (trend_expansion_active=False):
    #   - FAST_PATH_MAX_BARS = 3 (entrada rápida)
    #   - bars 4-10: Zona pullback
    #   - bars > 10: CHASE BLOCK
    #
    # MODE_EXPANSION (trend_expansion_active=True):
    #   - FAST_PATH_MAX_BARS = 10 (ventana extendida)
    #   - NO chase block por barras
    #   - Solo invalidar si ESTRUCTURA rompe (precio cruza origen)
    # =====================================================================
    trend_expansion_active = getattr(engine, '_trend_expansion_active', False)
    
    if trend_expansion_active:
        # ═══════════════════════════════════════════════════════════════════
        # MODE_EXPANSION: Ventana extendida, sin chase block por barras
        # ═══════════════════════════════════════════════════════════════════
        FAST_PATH_MAX_BARS = 10  # Ventana mucho más amplia en expansión
        
        if bars_since > FAST_PATH_MAX_BARS:
            if current_bos.force_direct_entry:
                current_bos.force_direct_entry = False
                logger.info(
                    f"🚀 MODE_EXPANSION: bars={bars_since} > {FAST_PATH_MAX_BARS} | "
                    f"Fast path cerrado pero PIPELINE NORMAL permitido (sin chase block)"
                )
                engine._current_trade_tags['expansion_mode_entry'] = True
            # En EXPANSION: NO cerramos entry_window, permitimos pipeline normal
            return None
        
        logger.info(
            f"🚀 MODE_EXPANSION FAST PATH | bars={bars_since} | "
            f"Trend expansion activo - ventana extendida"
        )
    else:
        # ═══════════════════════════════════════════════════════════════════
        # MODE_RANGE: Frenos normales, ventana corta
        # ═══════════════════════════════════════════════════════════════════
        FAST_PATH_MAX_BARS = 3
        PULLBACK_ZONE_MAX = 10  # Usar config en vez de hardcode 5
        
        if bars_since > FAST_PATH_MAX_BARS:
            if current_bos.force_direct_entry:
                current_bos.force_direct_entry = False
                logger.warning(
                    f"🚂 DISPLACEMENT FAST PATH MISSED | bars={bars_since} > {FAST_PATH_MAX_BARS} | "
                    f"El tren se fue - ahora solo PULLBACK entry permitida"
                )
                engine._current_trade_tags['displacement_fast_path_missed'] = True
                engine._current_trade_tags['displacement_missed_at_bars'] = bars_since
            
            if bars_since <= PULLBACK_ZONE_MAX:
                logger.debug(
                    f"🌊 DISPLACEMENT: bars={bars_since} - PULLBACK ZONE | "
                    f"Pipeline normal evaluará pullback entry"
                )
            else:
                # MODE_RANGE: Sí aplicar CHASE BLOCK
                logger.info(
                    f"🚫 MODE_RANGE CHASE BLOCK | bars={bars_since} > {PULLBACK_ZONE_MAX} | "
                    f"Edge expirado - no perseguir"
                )
                current_bos.entry_window_open = False
            
            return None
    
    # =====================================================================
    # 🔥 CHECK 1.5: ANTICIPATION EDGE PRIORITY (FIX #29)
    # =====================================================================
    # Si hay un BREAKOUT edge armado (de ANTICIPATION) con structure_level,
    # NO hacer DISPLACEMENT fast_path - esperar a que el precio rompa el
    # structure_level para una entrada más precisa.
    #
    # Ejemplo del log:
    # - ANTICIPATION detectó SHORT @ $76,672 (structure level)
    # - DISPLACEMENT quiere entrar @ $76,480 
    # - Mejor esperar ruptura de $76,672 para entrada más temprana
    # =====================================================================
    breakout_edge_armed = False
    breakout_has_structure = False
    
    # Get DISPLACEMENT direction from current_bos (available at this point)
    displacement_direction = current_bos.direction
    
    # Check si hay un BREAKOUT edge armado en algún edge manager
    # (puede estar pendiente de confirmación)
    structure_level_for_log: float = 0.0  # Initialize for logging
    anticipation_direction: str = ''
    anticipation_armed_time: Optional[datetime] = None
    
    if hasattr(engine, '_edge_manager') and engine._edge_manager:
        alt_edge_mgr = engine._edge_manager
        from src.domain.trading.edge_state_manager import EdgeState, EdgeType
        
        # Si el edge actual es BREAKOUT (de ANTICIPATION) y está ARMED
        if alt_edge_mgr.state == EdgeState.EDGE_ARMED:
            alt_bos = alt_edge_mgr.current_bos
            if alt_bos and alt_bos.edge_type == EdgeType.BREAKOUT:
                breakout_edge_armed = True
                anticipation_direction = alt_bos.direction
                anticipation_armed_time = alt_bos.timestamp  # timestamp = when edge was created
                # Check if it has a real structure_level (not just squeeze level)
                structure_level = alt_bos.bos_level
                squeeze_level = alt_bos.price
                if abs(structure_level - squeeze_level) > 0.3 * atr_1m:
                    breakout_has_structure = True
                    structure_level_for_log = structure_level
    
    # 🧠 SMART DEFERRAL: Solo diferir si las direcciones COINCIDEN
    # Si ANTICIPATION=SHORT y DISPLACEMENT=LONG, son setups diferentes - no diferir
    same_direction = (anticipation_direction == displacement_direction)
    
    # 🧠 TIMEOUT: No diferir si ANTICIPATION lleva más de 10 min sin confirmar
    # Después de 10 min, el edge está "stale" - mejor entrar con DISPLACEMENT
    MAX_DEFER_MINUTES = 10
    defer_timeout_exceeded = False
    minutes_armed: float = 0.0
    if anticipation_armed_time:
        minutes_armed = (market_time - anticipation_armed_time).total_seconds() / 60
        defer_timeout_exceeded = minutes_armed > MAX_DEFER_MINUTES
    
    if breakout_edge_armed and breakout_has_structure and same_direction and not defer_timeout_exceeded:
        # ANTICIPATION tiene un edge más preciso esperando Y es la misma dirección
        logger.info(
            f"⏸️ FAST PATH DEFERRED: ANTICIPATION EDGE ARMED | "
            f"BREAKOUT {anticipation_direction} structure=${structure_level_for_log:.0f} waiting for break | "
            f"DISPLACEMENT @ ${current_price:.0f} deferred"
        )
        engine._current_trade_tags['fast_path_deferred_anticipation'] = True
        engine._current_trade_tags['anticipation_structure_level'] = structure_level_for_log
        # Don't return None completely - allow pipeline to check BREAKOUT edge
        # But skip the DISPLACEMENT fast path
        return None
    elif breakout_edge_armed and breakout_has_structure and not same_direction:
        # Direcciones opuestas - log pero NO diferir
        logger.debug(
            f"🔀 ANTICIPATION {anticipation_direction} vs DISPLACEMENT {displacement_direction} | "
            f"Different directions - proceeding with DISPLACEMENT"
        )
    elif breakout_edge_armed and defer_timeout_exceeded:
        # Timeout - el ANTICIPATION edge está stale
        logger.warning(
            f"⏰ ANTICIPATION TIMEOUT: Edge armed for {minutes_armed:.1f} min > {MAX_DEFER_MINUTES} min | "
            f"Proceeding with DISPLACEMENT"
        )
        engine._current_trade_tags['anticipation_timeout'] = True
    
    # =====================================================================
    # CHECK 2: Validaciones MÍNIMAS (solo las críticas)
    # =====================================================================
    
    # 2a. Dirección debe coincidir con bias actual
    disp_direction = current_bos.direction
    if disp_direction != ctx.bias:
        logger.warning(
            f"🚫 FAST PATH SKIP: Direction mismatch | "
            f"DISPLACEMENT={disp_direction} vs BIAS={ctx.bias}"
        )
        return None
    
    # =====================================================================
    # 🔥 CHECK 2a.5: LOCATION EXTREME BLOCK (Feb 01, 2026)
    # =====================================================================
    # BUG CRÍTICO: FAST_PATH bypasaba todas las protecciones de location
    # Trade real: SHORT en percentil 3.8% (sobreventa extrema) → -0.51R
    #
    # REGLA: No shorteamos en sobreventa extrema, no longeamos en sobrecompra
    # =====================================================================
    location_ctx = getattr(engine, '_location_context', None)
    if location_ctx:
        pct_60d = location_ctx.percentile_60d
        in_extreme_low = location_ctx.in_extreme_low or pct_60d <= 10
        in_extreme_high = location_ctx.in_extreme_high or pct_60d >= 90
        
        # Block SHORT in extreme oversold
        if disp_direction == 'SHORT' and in_extreme_low:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: EXTREME OVERSOLD | "
                f"Attempting SHORT at percentile {pct_60d:.1f}% | "
                f"Don't short the bottom"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            engine._current_trade_tags['fast_path_extreme_pct'] = pct_60d
            return None
        
        # Block LONG in extreme overbought
        if disp_direction == 'LONG' and in_extreme_high:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: EXTREME OVERBOUGHT | "
                f"Attempting LONG at percentile {pct_60d:.1f}% | "
                f"Don't buy the top"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            engine._current_trade_tags['fast_path_extreme_pct'] = pct_60d
            return None
    
    # 2b. No en cooldown global
    # 🔧 FIX: El método es is_in_cooldown(), no in_cooldown()
    if engine.state_machine.is_in_cooldown():
        logger.debug("🚫 FAST PATH SKIP: In cooldown")
        return None
    
    # 2c. No ya en posición
    # 🔧 FIX: El atributo es .state (BotState enum), no .current_state
    # También importamos BotState para comparar correctamente
    from src.domain.trading.state.state_machine import BotState
    if engine.state_machine.state != BotState.IDLE:
        return None
    
    # =====================================================================
    # � FIX #91 (Feb 09, 2026): SIMPLIFICAR BLOQUEOS - ERAN CONTRAPRODUCENTES
    # =====================================================================
    # PROBLEMA: Los bloqueos creaban ciclos donde SIEMPRE llegábamos tarde:
    # 1. Detecta SHORT → forbidden_direction=LONG ✓
    # 2. Pero forbidden_direction bloquea el SIGUIENTE short también
    # 3. Porque el bias flip es tan rápido que forbidden_dir aún no se limpió
    #
    # SOLUCIÓN: Para DISPLACEMENT FAST PATH, SOLO bloquear si:
    # - direction_lock ACTIVO en dirección OPUESTA (no forbidden_direction)
    # - expansion_mode ACTIVO reciente (<5 min) en dirección OPUESTA
    #
    # El DISPLACEMENT es la señal más fuerte. Si hay displacement,
    # forbidden_direction es STALE (viejo) - ignorarlo.
    # =====================================================================
    
    # Check: Si hay direction_lock ACTIVO opuesto → BLOCK (pero con timeout)
    direction_lock = getattr(engine, '_direction_lock', None)
    lock_expires = getattr(engine, '_direction_lock_expires_at', None)
    if direction_lock and direction_lock != disp_direction:
        # Solo bloquear si el lock NO ha expirado
        if lock_expires and datetime.now() < lock_expires:
            logger.info(
                f"⚠️ FAST PATH: direction_lock={direction_lock} vs {disp_direction} | "
                f"Lock expires in {(lock_expires - datetime.now()).seconds}s - ALLOWING (new displacement stronger)"
            )
            # 🔥 CAMBIO: No bloquear, solo loguear. El nuevo displacement puede ser más fuerte.
    
    # 🔥 ELIMINADO: forbidden_direction check - era CONTRAPRODUCENTE
    # El forbidden_direction es resultado de un bias flip que ya pasó.
    # Si hay nuevo displacement, es señal FRESCA que debe tomar precedencia.
    
    # Check: Si expansion_mode ACTIVO reciente (<5 min) en dirección opuesta → BLOCK
    expansion_mode = getattr(engine, '_expansion_mode', False)
    expansion_dir = getattr(engine, '_expansion_direction', None)
    if expansion_mode and expansion_dir and expansion_dir != disp_direction:
        exp_start = getattr(engine, '_expansion_start_time', None)
        if exp_start and (datetime.now() - exp_start).total_seconds() < 300:  # 5 min (era 15)
            logger.warning(
                f"🚫 FAST PATH BLOCKED: COUNTER-EXPANSION (recent) | "
                f"{disp_direction} vs EXPANSION={expansion_dir} (started {(datetime.now() - exp_start).seconds}s ago)"
            )
            engine._current_trade_tags['fast_path_blocked_counter_momentum'] = True
            return None
        # Si expansion > 5 min, permitir - el mercado puede haber cambiado

    # =====================================================================
    # 🔥 FIX #91: ELIMINADO BTC COUNTER-TREND BLOCKER
    # =====================================================================
    # PROBLEMA: BTC trend es LAGGING. Cuando BTC trend = BEARISH,
    # el movimiento bajista ya pasó. Bloquear LONG en ese punto
    # significa perder el rebote.
    #
    # El DISPLACEMENT local es más confiable que el BTC trend agregado.
    # =====================================================================
    # 🔥 ELIMINADO: BTC counter-trend blocker - era lagging y contraproducente
    # Solo logueamos para contexto, no bloqueamos
    filter_pipeline = getattr(engine, 'filter_pipeline', None)
    btc_correlation_filter = getattr(filter_pipeline, 'btc_correlation_filter', None) if filter_pipeline else None
    if btc_correlation_filter:
        btc_feed = getattr(btc_correlation_filter, 'btc_feed', None)
        if btc_feed and btc_feed.is_ready():
            btc_context = btc_feed.get_context()
            if not btc_context.is_stale:
                btc_trend = btc_context.trend_bias
                engine._current_trade_tags['fast_path_btc_trend'] = btc_trend
                # Log but DON'T block - BTC trend is lagging

    # =====================================================================
    # 🔥 FIX #92: DELTA CONTRADICT - RELAJAR UMBRAL
    # =====================================================================
    # El delta contradict era útil pero el umbral de 2x era muy restrictivo.
    # Subimos a 5x para solo bloquear casos extremos de fakeout.
    # =====================================================================
    delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', 0)
    
    # Umbral RELAJADO: Solo bloquear si delta es >5x (era 2x)
    DELTA_CONTRADICT_THRESHOLD = 5.0  # 🔥 FIX: era 2.0
    
    if disp_direction == 'LONG' and delta_ratio < -DELTA_CONTRADICT_THRESHOLD:
        # Volumen vendedor masivo contra LONG
        logger.warning(
            f"🚫🚫🚫 FAST PATH BLOCKED: DELTA CONTRADICT | "
            f"Direction=LONG but delta={delta_ratio:.2f}x (selling pressure) | "
            f"Displacement puede ser FAKEOUT - esperando confirmación"
        )
        engine._current_trade_tags['fast_path_blocked_by_delta'] = True
        engine._current_trade_tags['fast_path_block_delta_ratio'] = delta_ratio
        return None
    
    if disp_direction == 'SHORT' and delta_ratio > DELTA_CONTRADICT_THRESHOLD:
        # Volumen comprador masivo contra SHORT
        logger.warning(
            f"🚫🚫🚫 FAST PATH BLOCKED: DELTA CONTRADICT | "
            f"Direction=SHORT but delta=+{delta_ratio:.2f}x (buying pressure) | "
            f"Displacement puede ser FAKEOUT - esperando confirmación"
        )
        engine._current_trade_tags['fast_path_blocked_by_delta'] = True
        engine._current_trade_tags['fast_path_block_delta_ratio'] = delta_ratio
        return None
    
    # =====================================================================
    # 🚨 CHECK 4: ENTRY AT EXTREME (Fix #14 - Feb 02, 2026)
    # =====================================================================
    # PROBLEMA CRÍTICO DETECTADO:
    # - LONG entró DESPUÉS de que el precio subiera $560
    # - El displacement origin era $77,658, entry fue en $78,214
    # - Esto es entrar en el TOP del movimiento → MFE 0.03R → loss
    #
    # CASO REAL (Feb 02, 2026):
    # - Displacement LONG detectado con origin $77,658
    # - Precio actual $78,214 (ya subió $560 = 2.4 ATR desde origin)
    # - Sistema entró en el EXTREMO → reversión inmediata → -0.30R
    #
    # 🔧 FIX INTELIGENTE (Fix #26): Usar displacement-ratio, no ATR
    # El problema era que ATR_1m (~$100) hacía que un disp de $400 ya fuera 4 ATR
    # Ahora: bloqueamos solo si perseguimos >50% MÁS ALLÁ del displacement inicial
    # =====================================================================
    origin_for_extreme_check = current_bos.origin_price or 0
    initial_disp_for_check = current_bos.initial_displacement_amount or 0
    
    if origin_for_extreme_check > 0 and initial_disp_for_check > 0:
        # Calcular distancia desde origin al precio actual
        if disp_direction == 'LONG':
            distance_from_origin = current_price - origin_for_extreme_check
        else:
            distance_from_origin = origin_for_extreme_check - current_price
        
        # Si retrocedimos (distance < 0), no hay chase
        if distance_from_origin > 0:
            # Ratio: 1.0 = exactamente en el displacement, 1.5 = 50% más allá
            displacement_ratio = distance_from_origin / initial_disp_for_check
            
            # BLOCK si >50% más allá del displacement inicial
            MAX_CHASE_RATIO = 1.5
            if displacement_ratio > MAX_CHASE_RATIO:
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: ENTRY AT EXTREME | "
                    f"{disp_direction} entry at {displacement_ratio:.1f}x displacement | "
                    f"Origin=${origin_for_extreme_check:.0f}, Disp=${initial_disp_for_check:.0f}, "
                    f"Current=${current_price:.0f}, Distance=${distance_from_origin:.0f} | "
                    f"Don't chase - wait for pullback"
                )
                engine._current_trade_tags['fast_path_blocked_by_extreme_entry'] = True
                engine._current_trade_tags['fast_path_displacement_ratio'] = displacement_ratio
                return None
    
    # =====================================================================
    # 🚨 CHECK 5: PRECIO YA REVIRTIÓ? (FAKEOUT DETECTOR)
    # =====================================================================
    # Si el precio ya retrocedió >40% del displacement inicial = FAKEOUT
    # 
    # Caso real:
    # - Displacement SHORT $326 detectado @ $82301
    # - 8 segundos después precio ya en $82481 (+$180 = 55% reversal)
    # - Esto era un wick/fakeout, no un displacement real
    #
    # Regla: Si precio ha revertido >40%, el displacement ya se invalidó
    # =====================================================================
    origin_price_check = current_bos.origin_price or 0
    initial_disp_check = current_bos.initial_displacement_amount or 0
    
    if initial_disp_check > 0 and origin_price_check > 0:
        if disp_direction == 'LONG':
            # En LONG: origin era el LOW, displacement subió
            # Si precio actual < origin + (disp * 0.60) → ya retrocedió >40%
            min_continuation = origin_price_check + (initial_disp_check * 0.60)
            if current_price < min_continuation:
                retracement_pct = (origin_price_check + initial_disp_check - current_price) / initial_disp_check * 100
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: FAKEOUT DETECTED | "
                    f"LONG displacement pero precio ya retrocedió {retracement_pct:.0f}% | "
                    f"Origin=${origin_price_check:.0f} Disp=${initial_disp_check:.0f} Current=${current_price:.0f}"
                )
                engine._current_trade_tags['fast_path_blocked_by_reversal'] = True
                engine._current_trade_tags['fast_path_reversal_pct'] = retracement_pct
                return None
        else:
            # En SHORT: origin era el HIGH, displacement bajó
            # Si precio actual > origin - (disp * 0.60) → ya retrocedió >40%
            max_continuation = origin_price_check - (initial_disp_check * 0.60)
            if current_price > max_continuation:
                retracement_pct = (current_price - (origin_price_check - initial_disp_check)) / initial_disp_check * 100
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: FAKEOUT DETECTED | "
                    f"SHORT displacement pero precio ya retrocedió {retracement_pct:.0f}% | "
                    f"Origin=${origin_price_check:.0f} Disp=${initial_disp_check:.0f} Current=${current_price:.0f}"
                )
                engine._current_trade_tags['fast_path_blocked_by_reversal'] = True
                engine._current_trade_tags['fast_path_reversal_pct'] = retracement_pct
                return None
    
    # =====================================================================
    # � FIX (Feb 02): SCORE INFORMATIVO PARA FAST_PATH
    # =====================================================================
    # PROBLEMA: FAST_PATH no calculaba score → score=0 en logs → imposible analizar
    # 
    # SOLUCIÓN: Calcular score INFORMATIVO (no bloquea, solo para análisis)
    # Esto permite:
    # 1. Distinguir "FAST_PATH buenos" de "FAST_PATH malos"
    # 2. Aplicar TIME_INVALIDATION correctamente basado en score real
    # 3. Análisis post-trade para mejorar criterios
    #
    # El score se calcula pero NO bloquea la entrada (FAST_PATH siempre ejecuta)
    # =====================================================================
    fast_path_score = 0.0
    fast_path_breakdown = {}
    
    try:
        # Obtener entry_scorer si está disponible
        entry_scorer = getattr(engine, '_entry_scorer', None)
        if entry_scorer and ctx.df_1h is not None and ctx.df_5m is not None and ctx.df_1m is not None:
            # 🔥 FIX #56: Get context for structure gate overrides
            expansion_mode_active = engine._current_trade_tags.get('expansion_mode_activated', False)
            effective_adx = engine._current_trade_tags.get('effective_adx', 0) or engine._current_trade_tags.get('htf_adx', 0)
            
            # 🔥 FIX #230: Get impulse age and origin price
            impulse_start_price: float | None = None
            impulse_age_bars: int = 0
            if hasattr(engine, '_edge_manager') and engine._edge_manager is not None:
                impulse_start_price = engine._edge_manager.get_edge_origin_price()
                edge_age_min = engine._edge_manager.get_edge_age_minutes(market_time)
                impulse_age_bars = int(edge_age_min / 5.0)
            
            # Calcular score con la firma correcta del método
            score, breakdown, structure_blocked = entry_scorer.calculate_entry_score(
                df_1h=ctx.df_1h,
                df_5m=ctx.df_5m,
                df_1m=ctx.df_1m,
                bias=disp_direction,
                current_price=current_price,
                market_time=market_time,
                micro_trend_state=getattr(engine, '_micro_trend_state', None),
                filter_pipeline=getattr(engine, '_entry_filter_pipeline', None),
                displacement_edge_active=True,  # FAST_PATH = displacement activo
                expansion_mode_active=expansion_mode_active,  # 🔥 FIX #56
                strong_trend_adx=effective_adx,               # 🔥 FIX #56
                impulse_start_price=impulse_start_price,      # 🔥 FIX #230
                impulse_age_bars=impulse_age_bars,            # 🔥 FIX #230
            )
            fast_path_score = score
            fast_path_breakdown = breakdown
            
            # Guardar en tags para logging y análisis
            engine._current_trade_tags['entry_score'] = fast_path_score
            engine._current_trade_tags['score_breakdown'] = fast_path_breakdown
            
            # También guardar componentes individuales para análisis
            for key, val in fast_path_breakdown.items():
                if key.startswith('score_') or key in ('structure', 'timing', 'momentum', 'context', 'synergy'):
                    engine._current_trade_tags[f'score_{key}' if not key.startswith('score_') else key] = val
            
            # Guardar en engine._entry_score para TIME_INVALIDATION fix
            engine._entry_score = fast_path_score
            engine._score_breakdown = fast_path_breakdown
            
            logger.info(
                f"📊 FAST PATH INFORMATIVE SCORE: {fast_path_score:.1f} | "
                f"breakdown={fast_path_breakdown}"
            )
        else:
            # Fallback: score = 0 porque no hay scorer o DataFrames disponibles
            missing = []
            if not entry_scorer:
                missing.append('scorer')
            if ctx.df_1h is None:
                missing.append('df_1h')
            if ctx.df_5m is None:
                missing.append('df_5m')
            if ctx.df_1m is None:
                missing.append('df_1m')
            
            engine._current_trade_tags['entry_score'] = 0
            engine._current_trade_tags['score_not_calculated'] = f"missing_{','.join(missing)}"
            engine._entry_score = 0
            engine._score_breakdown = {}
            logger.warning(
                f"⚠️ FAST PATH: Score=0 (missing: {missing}) - proceeding anyway"
            )
            
    except Exception as e:
        # Si falla el scoring, no bloqueamos FAST_PATH - solo logueamos
        logger.warning(f"⚠️ FAST PATH: Score calculation failed: {e} - continuing without score")
        engine._current_trade_tags['entry_score'] = 0
        engine._current_trade_tags['score_calculation_error'] = str(e)
        engine._entry_score = 0
        engine._score_breakdown = {}
    
    # ML inference removed from fast path — Opción A
    
    # =====================================================================
    # CHECK 2.5e: SCORE MÍNIMO PARA FAST PATH (Feb 03, 2026)
    # =====================================================================
    # 🔥 FIX #129 (Feb 09, 2026): Use centralized constant from unified_constants
    # Análisis de 49 trades FAST PATH: expectancy -0.012R, 35% TIME_INVALIDATION
    # Problema: Entraba con scores 5-6 en setups mediocres
    # Solución: Score >= 7 requiere buena estructura+timing+momentum
    #
    # 🔥 FIX #223 (Feb 12, 2026): TREND_BIRTH FAST TRACK
    # PROBLEMA: Score típico de TREND_BIRTH es 5-6, pero threshold es 7
    # Esto IMPIDE entrar al INICIO de tendencias (el mejor momento)
    # SOLUCIÓN: Reducir threshold a 5.0 cuando TrendPhase = TREND_BIRTH
    from src.config.unified_constants import SCORES
    from src.domain.market.regime import TrendPhase
    
    FAST_PATH_MIN_SCORE = SCORES.FAST_PATH_MIN_SCORE  # Default 7.0
    
    # 🔥 FIX #223: Check if we're in TREND_BIRTH
    trend_phase = getattr(engine, '_trend_phase', TrendPhase.RANGE_BOUND)
    is_trend_birth = trend_phase == TrendPhase.TREND_BIRTH
    is_trend_acceleration = trend_phase == TrendPhase.TREND_ACCELERATION
    
    if is_trend_birth:
        FAST_PATH_MIN_SCORE = 5.0  # Reduced from 7.0 - CATCH THE BIRTH!
        logger.info(
            f"🚀 FIX #223 TREND_BIRTH FAST TRACK: Threshold reduced 7.0 → 5.0 | "
            f"TrendPhase=TREND_BIRTH - OPTIMAL entry window!"
        )
    elif is_trend_acceleration:
        FAST_PATH_MIN_SCORE = 6.0  # Slightly reduced - still good
        logger.debug(
            f"📈 FIX #223 TREND_ACCELERATION: Threshold reduced 7.0 → 6.0 | "
            f"TrendPhase=TREND_ACCELERATION - momentum confirmed"
        )
    
    if fast_path_score < FAST_PATH_MIN_SCORE:
        logger.warning(
            f"🛑 FAST PATH BLOCKED: Score {fast_path_score:.1f} < {FAST_PATH_MIN_SCORE} minimum | "
            f"direction={disp_direction} | TrendPhase={trend_phase.value if hasattr(trend_phase, 'value') else trend_phase} | "
            f"breakdown={engine._score_breakdown}"
        )
        return None
    
    # =====================================================================
    # CHECK 2.5f: REAL-TIME STALE CHECK 
    # =====================================================================
    # 🔥 FIX #157 (Feb 10, 2026): ELIMINADO - STALE CHECK ERA CONTRAPRODUCENTE
    # 
    # PROBLEMA REAL (Feb 10, 2026):
    #   15:39:19 - DISPLACEMENT $919 detectado
    #   15:39:19 - STALE BLOCK porque precio cerca del low
    #   15:40-45 - CHASE BLOCK x21 veces
    #   15:50:00 - Entra tarde cuando momentum murió → PIERDE
    #
    # ANÁLISIS:
    # La regla "no vendas el low" tiene sentido en REVERSALS, no en DISPLACEMENT.
    # En un DISPLACEMENT real, el momentum está ACTIVO precisamente cuando
    # el precio está cerca del extremo. Bloquear ahí mata el trade.
    #
    # NUEVO ENFOQUE:
    # - Si hay DISPLACEMENT real (detectado), el momentum ES el extremo
    # - El FAST PATH ya tiene protección con bars_since <= MAX_BARS
    # - Si el edge expiró (bars > 5), el pipeline normal decide
    # - NO agregar otro bloqueo que contradice la señal primaria
    #
    # Este check generó más pérdidas que las que evitó porque:
    # 1. Bloqueaba entradas CON momentum
    # 2. Permitía entradas SIN momentum (después que expiró)
    # =====================================================================
    # STALE CHECK ELIMINADO - Confiamos en el displacement detection
    logger.debug(
        f"FIX #157: STALE CHECK DISABLED in FAST PATH | "
        f"Price={current_price:.0f} | direction={disp_direction} | "
        f"Trusting displacement momentum"
    )
    
    # =====================================================================
    # CHECK 2.5g: MOMENTUM == 0 -> BLOCK FAST PATH (Feb 04, 2026)
    # =====================================================================
    # Score puede ser alto (8.0) pero si momentum=0, el tren ya paro.
    # FAST PATH es para "subirse al tren en movimiento".
    # Sin momentum = sin urgencia = usar pipeline normal.
    # =====================================================================
    momentum_score = fast_path_breakdown.get('momentum', 1)
    
    if momentum_score == 0:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: MOMENTUM=0 | "
            f"Score={fast_path_score:.1f} but momentum=0 | "
            f"breakdown={fast_path_breakdown} | "
            f"No momentum = no urgency - use normal pipeline"
        )
        engine._current_trade_tags['fast_path_blocked_no_momentum'] = True
        return None

    # =====================================================================
    # FAST PATH ACTIVADO - ENTRAR AHORA
    # =====================================================================
    origin_log = current_bos.origin_price or 0
    disp_log = current_bos.initial_displacement_amount or 0
    logger.warning(
        f"🚀🚀🚀 DISPLACEMENT FAST PATH ACTIVATED 🚀🚀🚀 | "
        f"bars_since={bars_since} | direction={disp_direction} | "
        f"origin=${origin_log:.0f} | displacement=${disp_log:.0f} | "
        f"INFO_SCORE={fast_path_score:.1f}"
    )
    
    # =====================================================================
    # SIZING: Basado en score calculado (Feb 01, 2026)
    # PROBLEMA: Buenos trades tenían 0.25x sizing, malos también 0.25x
    # SOLUCIÓN: Usar el score informativo para sizing proporcional
    # =====================================================================
    base_risk_pct = 0.01  # 1% of capital - industry standard
    
    # 🔥 FIX (Feb 04, 2026): Sizing CONSERVADOR - MAX 50%
    # Análisis: FAST PATH tiene expectancy -0.012R vs Normal -0.004R
    # Reducir sizing hasta que la estrategia sea net-positive
    # BEFORE: Score 7+ = 100% (demasiado agresivo para expectancy negativa)
    # AFTER: Score 8+ = 50% MAX (reducir pérdidas mientras optimizamos)
    if fast_path_score >= 9:
        fast_path_size_mult = 0.50  # Excellent score = 50% MAX
    elif fast_path_score >= 8:
        fast_path_size_mult = 0.40  # Very good score = 40%
    elif fast_path_score >= 7:
        fast_path_size_mult = 0.30  # Good score = 30%
    else:
        fast_path_size_mult = 0.25  # Minimum = 25%
    
    # Bar 0 aún más conservador (chasing risk)
    if bars_since == 0:
        fast_path_size_mult = min(fast_path_size_mult, 0.30)
    
    engine._current_trade_tags['fast_path_size_mult'] = fast_path_size_mult
    engine._current_trade_tags['fast_path_size_reason'] = f'score={fast_path_score:.1f}'
    
    # =====================================================================
    # STOP LOSS: Estructural basado en origen del displacement
    # NO ATR tight - el SL es el ORIGEN del movimiento
    # =====================================================================
    origin_price = current_bos.origin_price
    initial_disp = current_bos.initial_displacement_amount or (ctx.atr_5m * 2)
    
    # SL más allá del origen (buffer de 10%)
    sl_buffer = initial_disp * 0.10
    
    if disp_direction == 'LONG':
        # En LONG, SL debajo del origen
        stop_loss = origin_price - sl_buffer
        # Asegurar mínimo de 1x ATR5m
        min_sl = current_price - (ctx.atr_5m * 1.5)
        stop_loss = min(stop_loss, min_sl)
    else:
        # En SHORT, SL encima del origen
        stop_loss = origin_price + sl_buffer
        # Asegurar mínimo de 1x ATR5m
        max_sl = current_price + (ctx.atr_5m * 1.5)
        stop_loss = max(stop_loss, max_sl)
    
    risk = abs(current_price - stop_loss)
    
    # =====================================================================
    # TAKE PROFIT: 2.0R mínimo para displacement (tendencias)
    # =====================================================================
    tp_r = 2.0
    if disp_direction == 'LONG':
        take_profit = current_price + (risk * tp_r)
    else:
        take_profit = current_price - (risk * tp_r)
    
    # =====================================================================
    # SIZE: Calcular basado en risk
    # =====================================================================
    # 🔧 FIX: Usar risk_engine.capital en lugar de _current_balance inexistente
    risk_engine = getattr(engine, 'risk_engine', None)
    account_balance = getattr(risk_engine, 'capital', 10000) if risk_engine else 10000
    risk_amount = account_balance * base_risk_pct * fast_path_size_mult
    
    size = risk_amount / risk if risk > 0 else 0
    size = round_size_to_exchange(size)
    
    # Validar mínimo
    if size < KRAKEN_BTC_MIN_SIZE:
        logger.warning(
            f"🚫 FAST PATH: Size {size:.6f} < min {KRAKEN_BTC_MIN_SIZE} | "
            f"risk=${risk_amount:.2f}, risk_dist=${risk:.2f}"
        )
        result = EntryResult()
        result.reason = "Size below minimum for fast path"
        return result
    
    logger.warning(
        f"🚀 FAST PATH ENTRY | {disp_direction} {size:.6f} BTC @ ${current_price:.2f} | "
        f"SL=${stop_loss:.2f} ({risk:.2f} risk) | TP=${take_profit:.2f} ({tp_r}R) | "
        f"Size mult: {fast_path_size_mult:.0%} [IOC]"
    )
    
    # =====================================================================
    # EJECUTAR: IOC/Aggressive (taker permitido)
    # Nota: momentum=0 ya fue bloqueado arriba, aquí siempre hay momentum
    # =====================================================================
    engine._current_trade_tags['entry_type'] = 'FAST_PATH_DISPLACEMENT'
    engine._current_trade_tags['fast_path_bars_since'] = bars_since
    engine._current_trade_tags['fast_path_origin'] = origin_price
    engine._current_trade_tags['fast_path_size_mult'] = fast_path_size_mult
    engine._current_trade_tags['fast_path_order_type'] = 'IOC_AGGRESSIVE'
    
    from src.domain.trading.state.state_machine import BotState
    from src.domain.trading.strategy.pullback_short import TradeType
    
    result = EntryResult()
    
    # Llamar place_aggressive_order directamente para tener acceso al fill info
    order_result = await engine.execution.place_aggressive_order(
        symbol=engine.symbol,
        side='buy' if disp_direction == 'LONG' else 'sell',
        amount=size,
        price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit
    )
    
    if order_result and order_result.get('filled', 0) > 0:
        # =====================================================================
        # 🎯 IOC FILLED IMMEDIATELY - CREATE POSITION DIRECTLY
        # =====================================================================
        result.success = True
        result.order_id = order_result.get('id')
        
        fill_price = float(order_result.get('average', current_price))
        fill_size = float(order_result.get('filled', size))
        
        logger.warning(
            f"✅ FAST PATH IOC FILLED | ID: {result.order_id} | "
            f"{fill_size:.6f} @ ${fill_price:.2f}"
        )
        
        # Marcar que usamos fast path
        current_bos.force_direct_entry = False
        
        # Store order tracking
        engine.pending_order_id = result.order_id
        engine.entry_order_time = market_time
        
        # Notify reconciler
        if result.order_id:
            engine.reconciler.notify_order_placed(result.order_id)
        
        # =====================================================================
        # 1. Record fill in position_store
        # =====================================================================
        try:
            fill_side = 'buy' if disp_direction == 'LONG' else 'sell'
            await engine._position_store.record_fill(
                order_id=result.order_id or 'fast_path_ioc',
                symbol=engine.symbol,
                side=fill_side,
                size=fill_size,
                price=fill_price,
                fee=0.0,
                fill_type='TAKER',  # IOC is taker
                fill_id=f"{result.order_id}_fill",
            )
            logger.info(f"✅ PositionStore updated: {fill_side.upper()} {fill_size:.6f} @ {fill_price:.2f}")
        except Exception as e:
            logger.warning(f"⚠️ Could not update position_store on fast path fill: {e}")
        
        # =====================================================================
        # 2. Create Position in risk_engine WITH FULL METADATA
        # =====================================================================
        trade_type = engine._current_trade_type or (
            TradeType.LONG_MAIN if disp_direction == 'LONG' else TradeType.SHORT_STRUCTURAL
        )
        
        # 🔥 CRITICAL: Pass edge_type and entry_score for TIME_INVALIDATION
        entry_score = engine._current_trade_tags.get('entry_score', None)
        edge_type = engine._current_trade_tags.get('edge_type', 'DISPLACEMENT')
        bos_quality = engine._current_trade_tags.get('bos_quality', 'NORMAL')
        is_weekend = market_time.weekday() >= 5 if market_time else False
        
        engine.risk_engine.create_position(
            symbol=engine.symbol,
            direction=disp_direction,
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
            bos_quality=bos_quality
        )
        
        logger.info(
            f"🎯 Position created: {disp_direction} {fill_size:.6f} @ {fill_price:.2f} | "
            f"edge_type={edge_type}, entry_score={entry_score}"
        )
        
        # =====================================================================
        # 3. Start exhaustion tracking and reset state
        # =====================================================================
        engine.exhaustion_detector.start_tracking(market_time)
        engine._exhaustion_tightened = False
        engine._exhaustion_partial_taken = False  # 🔥 FEB 11: Reset partial flag
        engine._gap_history = []
        engine._prev_tick_price = fill_price
        
        # Record trade open
        engine.state_machine.record_trade_open(result.order_id or "FAST_PATH")
        
        # Record trade price for duplicate zone filter
        engine._record_trade_price(fill_price)
        
        # =====================================================================
        # 4. Start trade logging
        # =====================================================================
        try:
            session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
            trade_type_name = trade_type.value if hasattr(trade_type, 'value') else str(trade_type)
            
            # 🔥 EDGE TYPE (Feb 05, 2026): Get edge_type from tags
            edge_type_for_log = engine._current_trade_tags.get('edge_type', '')
            
            engine._current_trade_id = engine.trade_logger.start_trade(
                symbol=engine.symbol,
                direction=disp_direction,
                entry_price=fill_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=fill_size,
                entry_score=entry_score or 0,
                entry_tags=list(engine._current_trade_tags.keys()),
                regime=str(ctx.volatility_regime) if ctx.volatility_regime else 'NORMAL',
                volatility_regime=str(ctx.volatility_regime) if ctx.volatility_regime else 'NORMAL',
                session=session_name,
                trade_type=trade_type_name,
                score_breakdown={},
                metadata={'fast_path': True, 'bars_since': bars_since},
                feature_snapshot=engine._build_ml_feature_snapshot() if hasattr(engine, '_build_ml_feature_snapshot') else {},
                entry_trading_mode=engine._current_trade_tags.get('trading_mode', ''),
                entry_daily_regime=getattr(engine, '_daily_regime', ''),
                edge_type=edge_type_for_log
            )
        except Exception as e:
            logger.error(f"TradeLogger.start_trade() failed: {e}")
            engine._current_trade_id = None
        
        # =====================================================================
        # 5. 🔥 CRITICAL: SET SL/TP ON EXCHANGE
        # =====================================================================
        # The position is opened but WITHOUT protective orders!
        # We MUST set them now or we have unlimited risk exposure.
        try:
            logger.info(f"📍 FAST PATH: Setting SL/TP on exchange...")
            stops_set = await engine.execution.set_position_stops(
                symbol=engine.symbol,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            if stops_set:
                logger.info(f"✅ FAST PATH SL/TP set: SL={stop_loss:.2f}, TP={take_profit:.2f}")
                engine._stops_set = True
                if engine.risk_engine.position:
                    engine.risk_engine.position.mark_reduce_only_validated()
                    engine.risk_engine.position.mark_exchange_sl_active()
            else:
                # 🚨 CRITICAL: SL failed to set - we have a position without SL!
                logger.error(f"🚨🚨🚨 FAST PATH CRITICAL: SL/TP FAILED TO SET! Position open without SL!")
                # Try one more time with just SL
                retry_sl = await engine.execution.set_position_stops(
                    symbol=engine.symbol,
                    stop_loss=stop_loss,
                    take_profit=None  # At least get SL
                )
                if retry_sl:
                    logger.warning(f"⚠️ FAST PATH: SL set on retry (no TP)")
                    engine._stops_set = True
                    if engine.risk_engine.position:
                        engine.risk_engine.position.mark_exchange_sl_active()
                else:
                    # Log critical error but don't crash - position will be managed locally
                    logger.critical(
                        f"🚨🚨🚨 FAST PATH DANGER: Position {disp_direction} {fill_size:.6f} @ {fill_price:.2f} "
                        f"HAS NO EXCHANGE SL! Local SL at {stop_loss:.2f} will be used (RISKY)"
                    )
                    engine._stops_set = False
        except Exception as e:
            logger.error(f"🚨 FAST PATH SL/TP exception: {e}")
            engine._stops_set = False
        
        # =====================================================================
        # 6. Clear pending state & transition to MANAGE
        # =====================================================================
        engine.pending_order_id = None
        engine.entry_order_time = None
        engine._last_entry_data = None
        
        engine.state_machine.transition_to(BotState.MANAGE, "Fast path IOC filled")
        logger.warning(f"🚀 FAST PATH COMPLETE → MANAGE | Position active")
        
        # =====================================================================
        # 7. Send Telegram notification
        # =====================================================================
        if engine.output_manager:
            try:
                from src.application.live.trading_app import _create_safe_task
                logger.info(f"📲 Sending entry notification: {disp_direction} @ {fill_price:.2f}")
                _create_safe_task(engine.output_manager.send_entry_notification({
                    'symbol': engine.symbol,
                    'direction': disp_direction,
                    'entry_price': fill_price,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'size': fill_size,
                    'score': entry_score,
                    'regime': getattr(engine, '_current_regime', None),
                    'risk_usd': engine._current_trade_tags.get('risk_usd'),
                    'timestamp': market_time.isoformat() if hasattr(market_time, 'isoformat') else str(market_time),
                    'entry_type': 'FAST_PATH_DISPLACEMENT'
                }), "send_entry_notification")
            except Exception as e:
                logger.warning(f"⚠️ Entry notification error: {e}")
        
    else:
        # =====================================================================
        # IOC NOT FILLED - Market moved too fast or no liquidity
        # =====================================================================
        result.success = False
        result.reason = "IOC not filled - price gap too large or no liquidity"
        current_bos.force_direct_entry = False  # Don't retry
        
        logger.warning(
            f"🚫 FAST PATH IOC NOT FILLED | No position created | "
            f"Reason: IOC expired (spread too wide or slippage)"
        )
    
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
    
    # =========================================================================
    # 🔥 NEW (Feb 04, 2026): WARMUP PERIOD CHECK
    # Prevents trading immediately after startup when no market context exists
    # =========================================================================
    from src.config.trading_config import RiskManagement
    
    if RiskManagement.WARMUP_ENABLED and not getattr(engine, '_warmup_complete', False):
        idle_count = getattr(engine, '_idle_eval_count', 0)
        candles_count = getattr(engine, '_candles_since_start', 0)
        
        if idle_count < RiskManagement.WARMUP_MIN_TICKS or candles_count < RiskManagement.WARMUP_MIN_CANDLES:
            result.reason = f"⏳ WARMUP: {idle_count}/{RiskManagement.WARMUP_MIN_TICKS} ticks, {candles_count}/{RiskManagement.WARMUP_MIN_CANDLES} candles"
            logger.warning(f"🔥 {result.reason} - waiting for market context")
            return result
        else:
            # Warmup complete - mark it
            engine._warmup_complete = True
            logger.info(f"✅ WARMUP COMPLETE: {idle_count} ticks, {candles_count} candles observed")
    
    bias = engine.current_bias
    
    if not bias:
        result.reason = "No bias set"
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
    
    # Score floor check
    score_mult = EntryGateConfig.get_score_size_multiplier(score_result.score)
    if score_mult == 0.0:
        result.reason = f"Score {score_result.score:.1f} below floor"
        return result
    
    # FADE block removed — Opción A

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
        
        # Simplified: 1.5R in trend, 1.0R in range, 2.0R in breakout
        # 🔥 NEW: Adaptive TP based on volatility (atr_expansion_ratio)
        tp_mult = SimplifiedExitConfig.get_tp_r(
            regime_str=engine._current_regime.value if engine._current_regime else "NORMAL",
            adx=ctx.adx_1h if hasattr(ctx, 'adx_1h') else None,
            edge_type=current_edge_type,
            atr_expansion_ratio=atr_expansion_ratio
        )
        atr_ratio_str = f"{atr_expansion_ratio:.2f}" if atr_expansion_ratio else "N/A"
        logger.info(f"📊 SIMPLIFIED TP: {tp_mult:.2f}R (edge={current_edge_type}, atr_ratio={atr_ratio_str})")
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
    # Simple RR floor check — Opción A
    if worst_case_rr < 0.5:
        result.reason = f"RR too low: {worst_case_rr:.2f} < 0.5"
        return result
    
    # ========== PHASE 7: SIZING ==========
    sizing_result = SizingOrchestrator.calculate_size_simplified(
        engine, ctx,
        stop_loss=stop_loss,
        worst_case_rr=worst_case_rr,
    )
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
    # ✅ PASO 3 — BLOQUEA TRADES PEQUEÑOS
    # expected_net_R < 0.6 → NO TRADE (hard block)
    # =====================================================================
    MIN_EXPECTED_NET_R = 0.6
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
    # 🔥 FIX #235 (Feb 15, 2026): Lowered MIN_PROFIT_FLOOR from $0.50 to $0.25
    # PROBLEM: With $245 capital and probe sizing (0.25×), expected profit was
    # ~$0.30 → blocked by $0.50 floor. Probe trades could NEVER pass.
    # Combined with SCORE_BLOCK_FLOOR forcing probe sizing, this was a death trap.
    # $0.25 still covers fees on Kraken Futures (maker 0.02%, taker 0.05%).
    if capital < 500:
        MIN_PROFIT_PERCENT = 0.0015  # 0.15% for small accounts (~$0.37 for $245)
    else:
        MIN_PROFIT_PERCENT = 0.0010  # 0.10% for larger accounts
    MIN_PROFIT_FLOOR = 0.25     # 🔥 FIX #235: Lowered from $0.50 — allow probe trades on small accounts
    
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
    
    # 🔥 FIX (Feb 01): Get entry_score for TIME_INVALIDATION strictness
    entry_score = engine._current_trade_tags.get('entry_score', None)
    edge_type = engine._current_trade_tags.get('edge_type', None)
    
    # 🔥 NEW (Feb 03): Get BOS quality for dynamic trailing
    bos_quality = engine._current_trade_tags.get('bos_quality', 'NORMAL')
    
    # 🔥 FIX (Feb 01): Detect weekend for stricter exits
    is_weekend = ctx.market_time.weekday() >= 5 if ctx.market_time else False
    
    engine.risk_engine.create_position(
        symbol=engine.symbol,
        direction=ctx.bias,
        entry_price=ctx.current_price,
        position_size=size,
        stop_loss=stop_loss,
        atr_1m=ctx.atr_1m,
        market_time=ctx.market_time,
        trade_type=trade_type,
        regime_mode=getattr(engine, '_current_trade_regime', 'RANGE'),  # 🔥 FIX: Default to string
        edge_type=edge_type,  # 🔥 FIX (Feb 01): Pass edge type
        entry_score=entry_score,  # 🔥 FIX (Feb 01): Pass entry score for stricter TIME_INVALIDATION
        is_weekend=is_weekend,  # 🔥 FIX (Feb 01): Pass is_weekend for stricter exits
        bos_quality=bos_quality  # 🔥 NEW (Feb 03): Pass BOS quality for dynamic trailing
    )
    
    # Start exhaustion tracking
    engine.exhaustion_detector.start_tracking(ctx.market_time)
    engine._exhaustion_tightened = False
    engine._exhaustion_partial_taken = False  # 🔥 FEB 11: Reset partial flag
    
    # 🔥 FIX (Feb 01): Reset gap history for new trade
    engine._gap_history = []
    
    # 🔥 FIX (Feb 02, 2026): Also reset _prev_tick_price to avoid false PRICE_GAP_ANOMALY
    # Bug: Gap was calculated between OLD _prev_tick_price and NEW entry price → false positives
    engine._prev_tick_price = ctx.current_price
    
    # Record trade open
    engine.state_machine.record_trade_open(engine.pending_order_id or "UNKNOWN")
    
    # Start trade logging
    try:
        session_name = ctx.session_name
        trade_type_name = trade_type.value if hasattr(trade_type, 'value') else str(trade_type)
        
        # 🔥 EDGE TYPE (Feb 05, 2026): Get edge_type from tags
        edge_type_for_log = engine._current_trade_tags.get('edge_type', '')
        
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
            entry_daily_regime=getattr(engine, '_daily_regime', ''),
            edge_type=edge_type_for_log
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
    
    # 🔥 NEW (Jan 28, 2026): Send entry notification to Telegram
    if engine.output_manager:
        try:
            from src.application.live.trading_app import _create_safe_task
            logger.info(f"📲 Sending entry notification: {ctx.bias} @ {ctx.current_price:.2f}")
            _create_safe_task(engine.output_manager.send_entry_notification({
                'symbol': engine.symbol,
                'direction': ctx.bias,
                'entry_price': ctx.current_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'size': size,
                'score': engine._entry_score,
                'regime': getattr(engine, '_current_regime', None),
                'risk_usd': engine._current_trade_tags.get('risk_usd'),
                'timestamp': ctx.market_time.isoformat() if hasattr(ctx.market_time, 'isoformat') else str(ctx.market_time)
            }), "send_entry_notification")
        except Exception as e:
            logger.warning(f"⚠️ Entry notification error: {e}")



