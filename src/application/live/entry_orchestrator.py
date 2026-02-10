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
# 🔥 HELPER: EXTRACT SETUP LEVELS FOR MISSED OPPORTUNITY TRACKING (Feb 07)
# =========================================================================

def extract_setup_levels(engine: 'SignalEngine', current_price: float, bias: str, 
                         stop_loss: float = 0.0, take_profit: float = 0.0) -> Dict[str, Any]:
    """
    Extract REAL setup levels from the engine's edge manager.
    
    This is used by MissedOpportunityTracker v2.0 to simulate blocked trades
    with REAL levels instead of arbitrary detection prices.
    
    Returns:
        Dict with: origin_price, entry_level, stop_level, tp_level, 
                   invalidation_level, edge_type
    """
    levels = {
        'origin_price': 0.0,
        'entry_level': current_price,  # Default to detection price
        'stop_level': stop_loss,
        'tp_level': take_profit,
        'invalidation_level': 0.0,
        'edge_type': 'UNKNOWN',
    }
    
    # Try to get levels from EdgeManager's current BOS
    edge_mgr = getattr(engine, '_edge_manager', None)
    if edge_mgr is not None:
        bos = getattr(edge_mgr, '_current_bos', None)
        if bos is not None:
            # Get origin price (where the setup started)
            origin = getattr(bos, 'origin_price', 0.0)
            if origin and origin > 0:
                levels['origin_price'] = origin
            
            # Get edge type
            edge_type = getattr(bos, 'edge_type', None)
            if edge_type is not None:
                levels['edge_type'] = edge_type.value if hasattr(edge_type, 'value') else str(edge_type)
            
            # Get invalidation level (bos_level is used for invalidation)
            invalidation = getattr(bos, 'bos_level', 0.0)
            if invalidation and invalidation > 0:
                levels['invalidation_level'] = invalidation
            
            # Get entry zone from BOS if available
            # For DISPLACEMENT: entry is at current price in the direction of move
            # For RANGE: entry is at sweep level
            # For TREND: entry is at BOS level pullback
            entry_zone_low = getattr(bos, 'entry_zone_low', 0.0)
            entry_zone_high = getattr(bos, 'entry_zone_high', 0.0)
            
            if entry_zone_low > 0 and entry_zone_high > 0:
                # Use midpoint of entry zone
                levels['entry_level'] = (entry_zone_low + entry_zone_high) / 2
    
    # If we still don't have origin, use bias prices as fallback
    if levels['origin_price'] <= 0:
        if bias == 'SHORT':
            # SHORT: origin is the high
            origin = getattr(engine, '_bias_high_since', 0.0)
        else:
            # LONG: origin is the low
            origin = getattr(engine, '_bias_low_since', 0.0)
        
        if origin and origin > 0:
            levels['origin_price'] = origin
    
    # Calculate invalidation if not set
    if levels['invalidation_level'] <= 0 and levels['origin_price'] > 0:
        # Invalidation is at/beyond origin for displacement trades
        levels['invalidation_level'] = levels['origin_price']
    
    # If no SL provided, estimate from ATR
    if levels['stop_level'] <= 0:
        atr = getattr(engine, '_last_momentum_atr', 100.0) or 100.0
        if bias == 'LONG':
            levels['stop_level'] = current_price - (atr * 1.5)
        else:
            levels['stop_level'] = current_price + (atr * 1.5)
    
    # If no TP provided, estimate 2R from entry
    if levels['tp_level'] <= 0 and levels['stop_level'] > 0:
        risk = abs(current_price - levels['stop_level'])
        if bias == 'LONG':
            levels['tp_level'] = current_price + (risk * 2.0)
        else:
            levels['tp_level'] = current_price - (risk * 2.0)
    
    return levels


def log_blocked_with_setup(
    engine: 'SignalEngine',
    direction: str,
    price: float,
    block_reason: str,
    block_category: str,
    score: float = 0.0,
    atr: float = 0.0,
    stop_loss: float = 0.0,
    take_profit: float = 0.0
):
    """
    Log a blocked signal with REAL setup levels for v2 tracker.
    
    This wrapper extracts setup levels from the engine and passes them
    to the tracker's new API for proper simulation.
    """
    if not hasattr(engine, 'missed_tracker') or engine.missed_tracker is None:
        return
    
    # Extract real setup levels
    levels = extract_setup_levels(engine, price, direction, stop_loss, take_profit)
    
    # Call tracker with full setup info
    engine.missed_tracker.log_blocked_signal(
        direction=direction,
        price=price,
        block_reason=block_reason,
        block_category=block_category,
        score=score,
        atr=atr,
        # NEW v2 params
        origin_price=levels['origin_price'],
        entry_level=levels['entry_level'],
        stop_level=levels['stop_level'],
        tp_level=levels['tp_level'],
        invalidation_level=levels['invalidation_level'],
        edge_type=levels['edge_type'],
    )


# =========================================================================
# GUARD ORCHESTRATOR
# =========================================================================

class GuardOrchestrator:
    """
    Runs all pre-execution guards in order.
    
    🔥 FIX #81 (Feb 07, 2026): RISK PENALTY ACCUMULATOR
    =====================================================
    Guards now ADD PENALTIES instead of hard blocking.
    Block decision made at END based on:
    - Catastrophic events (funding spike, spread explosion)
    - Accumulated size penalty < 15% (too small to trade)
    
    PHILOSOPHY:
    - Each guard returning 20% block chance
    - 6 guards → 70-80% of good setups blocked
    - NEW: Each guard returns penalty multiplier
    - final_size = base_size × Π(penalties)
    - Block only if final_size < MIN_VIABLE_SIZE
    
    Guard order:
    1. Concurrency guards (pending orders, open positions) - HARD BLOCK OK
    2. Edge enforcement
    3. Location guards (anchor levels, range boundaries)
    4. ADX coherence
    5. POST_ONLY rejection tracking - HARD BLOCK OK
    6. TP/RR config sanity - HARD BLOCK OK
    7. Funding rate - CATASTROPHIC CHECK
    8. Spread filter - PENALTY
    9-15. Direction, Fakeout, Kill Check, etc - ALL PENALTIES
    """
    
    @staticmethod
    async def run_all_guards(engine: 'SignalEngine', ctx: EntryContext) -> GuardResult:
        """Run all guards and return combined result with accumulated penalties."""
        from src.domain.trading.risk_penalty import (
            RiskPenaltyAccumulator, PenaltySource, 
            CatastrophicEventDetector, AdaptiveRRThresholds
        )
        
        result = GuardResult()
        penalties = RiskPenaltyAccumulator()
        
        # 🔥 FIX #88 (Feb 09, 2026): Set context for TREND_EXPANSION override
        # 🔥 FIX #109 (Feb 09, 2026): Set context for TRANSITION_MODE override
        trend_expansion_active = getattr(engine, '_trend_expansion_active', False)
        adx_value = getattr(engine, '_htf_cache_adx', 0.0) or 0.0
        
        # Check transition mode (and check for expiration)
        transition_mode_active = getattr(engine, '_transition_mode_active', False)
        transition_expires = getattr(engine, '_transition_mode_expires', None)
        if transition_mode_active and transition_expires:
            from datetime import datetime
            if datetime.now() > transition_expires:
                # Transition mode expired
                engine._transition_mode_active = False
                engine._transition_mode_direction = None
                engine._transition_mode_expires = None
                transition_mode_active = False
                logger.info("🏁 TRANSITION_MODE EXPIRED: Returning to normal penalty floor")
        
        penalties.set_context(
            trend_expansion_active=trend_expansion_active,
            adx_value=adx_value,
            transition_mode_active=transition_mode_active
        )
        
        # 🔥 FIX #147: Initialize absorption_contrary (will be set later in flow)
        engine._current_trade_tags['absorption_contrary'] = False
        
        # =====================================================================
        # PHASE 1: HARD BLOCKS (truly blocking conditions)
        # These are infrastructure/concurrency issues, not market conditions
        # =====================================================================
        
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
            # 🔧 FIX #130: Reduced cooldown from 5 to 2 candles - 5 minutes was too long
            engine._set_rejection_cooldown("Anchor veto", candles=2)
            return result
        
        can_enter, range_reason = LocationGuards.check_range_location(engine, ctx.bias, ctx.current_price)
        if not can_enter:
            result.passed = False
            result.reason = f"Location veto: {range_reason}"
            result.tags['block_category'] = 'location'
            engine._set_rejection_cooldown("Location veto", candles=3)
            return result
        
        # 4. ADX coherence - CONVERT TO PENALTY
        if len(ctx.df_1h) >= 20 and len(ctx.df_5m) >= 20:
            adx_1h, _ = calculate_adx(ctx.df_1h['high'].to_numpy(), ctx.df_1h['low'].to_numpy(), ctx.df_1h['close'].to_numpy(), 14)
            adx_5m, _ = calculate_adx(ctx.df_5m['high'].to_numpy(), ctx.df_5m['low'].to_numpy(), ctx.df_5m['close'].to_numpy(), 14)
            
            if len(adx_1h) > 0 and len(adx_5m) > 0:
                htf_adx = adx_1h[-1]
                ltf_adx = adx_5m[-1]
                ctx.adx_1h = htf_adx
                
                has_displacement_bypass = getattr(engine, '_displacement_bypass', False)
                htf_very_strong = htf_adx > 35
                
                from src.config.trading_config import ADXThresholds
                # 🔥 FIX #81: PENALTY instead of block
                if htf_adx > ADXThresholds.TREND and ltf_adx < 10 and not has_displacement_bypass and not htf_very_strong:
                    penalties.add_penalty(
                        source=PenaltySource.WEAK_EDGE,
                        size_mult=0.50,
                        score_adj=-1.0,
                        reason=f"ADX divergence: HTF={htf_adx:.1f} trending, LTF={ltf_adx:.1f} dead"
                    )
        
        # 5. POST_ONLY rejection tracking - HARD BLOCK OK (infrastructure issue)
        if hasattr(engine, '_consecutive_post_only_rejects') and engine._consecutive_post_only_rejects >= 3:
            result.passed = False
            result.reason = f"Market too fast: {engine._consecutive_post_only_rejects} POST_ONLY rejections"
            engine._consecutive_post_only_rejects = 0
            engine.state_machine.start_cooldown(5)
            return result
        
        # 6. TP/RR sanity - HARD BLOCK OK (config error)
        if RiskManagement.TP1_RISK_MULTIPLE < OperatingMode.RR_HARD_FLOOR:
            result.passed = False
            result.reason = f"Config error: TP1_RISK_MULTIPLE < RR_HARD_FLOOR"
            return result
        
        # =====================================================================
        # PHASE 2: CATASTROPHIC EVENT CHECK
        # Only these warrant immediate hard block
        # =====================================================================
        
        # 7. Funding rate - CATASTROPHIC CHECK
        if not engine._is_backtest:
            try:
                from src.domain.market.filters import check_funding_rate_safe
                funding_ok, funding_reason = await check_funding_rate_safe(
                    engine.external_data, engine.symbol, ctx.bias
                )
                if not funding_ok:
                    # Check if it's truly catastrophic
                    is_catastrophic, cat_reason = CatastrophicEventDetector.check(
                        funding_rate=getattr(engine, '_last_funding_rate', None)
                    )
                    if is_catastrophic:
                        result.passed = False
                        result.reason = f"CATASTROPHIC: {cat_reason}"
                        return result
                    else:
                        # Just a penalty, not catastrophic
                        penalties.add_penalty(
                            source=PenaltySource.TOXIC_COMBO,
                            size_mult=0.50,
                            score_adj=-0.5,
                            reason=f"Funding risky: {funding_reason}"
                        )
            except Exception as e:
                logger.warning(f"Could not check funding rate: {e}")
        
        # =====================================================================
        # PHASE 3: MARKET CONDITION PENALTIES (no more hard blocks!)
        # =====================================================================
        
        # 8. Spread filter - PENALTY (not block)
        spread_result = engine.spread_filter.check_entry_allowed(ctx.session_name)
        if not spread_result.allowed:
            bias_flip = engine._current_trade_tags.get('bias_invalidated', False)
            bias_inverted = engine._current_trade_tags.get('bias_inverted', False)
            
            if bias_flip or bias_inverted:
                logger.info(f"🔄 SPREAD BYPASS: bias_flip={bias_flip}, bias_inverted={bias_inverted}")
            else:
                penalties.add_penalty(
                    source=PenaltySource.HIGH_SPREAD,
                    size_mult=spread_result.size_factor if spread_result.size_factor > 0 else 0.50,
                    score_adj=spread_result.score_penalty,
                    reason=f"Spread: {spread_result.reason}"
                )
        
        result.tags['spread_penalty'] = spread_result.score_penalty
        result.tags['spread_size_factor'] = spread_result.size_factor
        
        # =====================================================================
        # 🚨 GUARD 9: DELTA CONTRADICT - 🔥 FIX #147 (Feb 10, 2026)
        # =====================================================================
        # 🔥 FIX #147: HARD BLOCK cuando delta SEVERE + ABSORPTION
        # 
        # NOTA: El combo check (delta + absorption) se hace MÁS ADELANTE,
        # después de que absorption_detector haya evaluado (línea ~800+).
        # Aquí solo registramos el estado de delta para el combo check.
        # 
        # Si solo delta → penalizar pero permitir (puede ser lagging).
        # Si delta + absorption → el combo check bloqueará más adelante.
        # =====================================================================
        delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', 0)
        
        delta_severe_contradict = (
            (ctx.bias == 'LONG' and delta_ratio < -5.0) or
            (ctx.bias == 'SHORT' and delta_ratio > 5.0)
        )
        delta_moderate_contradict = (
            (ctx.bias == 'LONG' and -5.0 <= delta_ratio < -3.0) or
            (ctx.bias == 'SHORT' and 3.0 < delta_ratio <= 5.0)
        )
        
        # 🔥 Guardar estado de delta para el combo check más adelante
        engine._current_trade_tags['delta_severe_contradict'] = delta_severe_contradict
        
        # Solo delta severe (sin absorption aún) → penalizar fuerte pero permitir
        if delta_severe_contradict:
            engine._current_trade_tags['delta_info_contradict'] = True
            engine._current_trade_tags['delta_info_ratio'] = delta_ratio
            penalties.add_penalty(
                source=PenaltySource.DELTA_CONTRADICT,
                size_mult=0.50,
                score_adj=-1.0,
                reason=f"Delta SEVERE contradict: {delta_ratio:+.2f}x vs {ctx.bias}"
            )
            logger.warning(
                f"⚠️ FIX #147: Delta SEVERE {delta_ratio:+.2f}x vs {ctx.bias} | "
                f"Size × 0.50, Score -1.0"
            )
        elif delta_moderate_contradict:
            # Moderate → penalizar moderado
            engine._current_trade_tags['delta_info_contradict'] = True
            engine._current_trade_tags['delta_info_ratio'] = delta_ratio
            penalties.add_penalty(
                source=PenaltySource.DELTA_CONTRADICT,
                size_mult=0.70,
                score_adj=-0.5,
                reason=f"Delta MODERATE contradict: {delta_ratio:+.2f}x vs {ctx.bias}"
            )
            logger.info(
                f"📊 FIX #147: Delta MODERATE {delta_ratio:+.2f}x vs {ctx.bias} | "
                f"Size × 0.70, Score -0.5"
            )
        else:
            engine._current_trade_tags['delta_info_contradict'] = False
        
        engine._current_trade_tags['delta_block'] = False
        
        # =====================================================================
        # 🚨 GUARD 10: SHORT OBSTINACY - PENALTY (not block)
        # 🔥 FIX #81: Converted to penalty system
        # =====================================================================
        if ctx.bias == 'SHORT':
            bullish_signals = engine._current_trade_tags.get('bullish_signals', 0)
            obstinacy_reason = engine._current_trade_tags.get('obstinacy_reason', '')
            
            OBSTINACY_THRESHOLD = 3
            if bullish_signals >= OBSTINACY_THRESHOLD:
                # Check for bypasses
                displacement_bypass_active = engine._current_trade_tags.get('displacement_bypass', False)
                displacement_memory_active = engine._current_trade_tags.get('displacement_memory_active', False)
                strong_trend_override = engine._current_trade_tags.get('obstinacy_strong_trend_override', False)
                
                if displacement_bypass_active:
                    logger.info(f"🔥 OBSTINACY BYPASSED by DISPLACEMENT")
                elif strong_trend_override or displacement_memory_active:
                    # Soft mode - still apply penalty
                    penalties.add_penalty(
                        source=PenaltySource.SHORT_OBSTINACY,
                        size_mult=0.50,
                        score_adj=-0.5,
                        reason=f"Obstinacy soft: {bullish_signals} bullish ({obstinacy_reason})"
                    )
                else:
                    # Heavy penalty (was hard block)
                    penalties.add_penalty(
                        source=PenaltySource.SHORT_OBSTINACY,
                        size_mult=0.25,
                        score_adj=-1.5,
                        reason=f"Obstinacy: {bullish_signals} bullish ({obstinacy_reason})"
                    )
        else:
            engine._obstinacy_block_count = 0
        
        # =====================================================================
        # 🚨 GUARD 11: DISPLACEMENT EXTREME - PENALTY (not block)
        # 🔥 FIX #81: Converted to penalty system
        # =====================================================================
        # OLD: displacement_ratio > 1.5x → HARD BLOCK
        # NEW: Graduated penalty based on how much we're chasing
        #   - 1.0-1.5x: No penalty (normal entry window)
        #   - 1.5-2.0x: Light penalty (size 80%, score -0.5)
        #   - 2.0-2.5x: Medium penalty (size 60%, score -1.0)
        #   - 2.5-3.0x: Heavy penalty (size 40%, score -1.5)
        #   - >3.0x: Still trade but tiny (size 25%, score -2.0)
        #
        # WHY: Hard blocking at 1.5x caused missed trades when:
        # - Displacement was small but valid
        # - Market continued strongly in direction
        # - Entry at 2x was still profitable
        # =====================================================================
        edge_mgr = getattr(engine, '_edge_manager', None)
        if edge_mgr is not None:
            from src.domain.trading.edge_state_manager import EdgeState, EdgeType
            is_displacement = (
                edge_mgr.state == EdgeState.EDGE_ACTIVE and
                edge_mgr.current_bos is not None and
                edge_mgr.current_bos.edge_type == EdgeType.DISPLACEMENT
            )
            if is_displacement:
                origin_price = edge_mgr.current_bos.origin_price or 0
                initial_displacement = edge_mgr.current_bos.initial_displacement_amount or 0
                
                if origin_price > 0 and initial_displacement > 0:
                    if ctx.bias == 'LONG':
                        distance = ctx.current_price - origin_price
                    else:
                        distance = origin_price - ctx.current_price
                    
                    # Si distance < 0, el precio retrocedió del origin → no hay chase
                    if distance > 0:
                        displacement_ratio = distance / initial_displacement
                        
                        # 🔥 FIX #78: Graduated penalty instead of hard block
                        chase_size_mult = 1.0
                        chase_score_penalty = 0.0
                        
                        if displacement_ratio > 3.0:
                            chase_size_mult = 0.25
                            chase_score_penalty = -2.0
                            logger.warning(
                                f"⚠️ FIX #78 CHASE EXTREME: {ctx.bias} at {displacement_ratio:.1f}x displacement | "
                                f"size×25%, score-2.0 (max penalty)"
                            )
                        elif displacement_ratio > 2.5:
                            chase_size_mult = 0.40
                            chase_score_penalty = -1.5
                            logger.warning(
                                f"⚠️ FIX #78 CHASE HEAVY: {ctx.bias} at {displacement_ratio:.1f}x displacement | "
                                f"size×40%, score-1.5"
                            )
                        elif displacement_ratio > 2.0:
                            chase_size_mult = 0.60
                            chase_score_penalty = -1.0
                            logger.info(
                                f"⚠️ FIX #78 CHASE MEDIUM: {ctx.bias} at {displacement_ratio:.1f}x displacement | "
                                f"size×60%, score-1.0"
                            )
                        elif displacement_ratio > 1.5:
                            chase_size_mult = 0.80
                            chase_score_penalty = -0.5
                            logger.info(
                                f"⚠️ FIX #78 CHASE LIGHT: {ctx.bias} at {displacement_ratio:.1f}x displacement | "
                                f"size×80%, score-0.5"
                            )
                        # else: ratio <= 1.5 → no penalty
                        
                        # 🔥 FIX #81: Use centralized penalty accumulator
                        if chase_size_mult < 1.0:
                            penalties.add_penalty(
                                source=PenaltySource.DISPLACEMENT_CHASE,
                                size_mult=chase_size_mult,
                                score_adj=chase_score_penalty,
                                reason=f"Chase {displacement_ratio:.1f}x displacement"
                            )
                            engine._current_trade_tags['chase_penalty_applied'] = True
                            engine._current_trade_tags['guard_displacement_ratio'] = displacement_ratio
                            # Continue - don't block, penalty will be aggregated
        
        # =====================================================================
        # 🚨 GUARD 12: LOCATION EXTREME → SOFT MODE (Fix #56 - Feb 06, 2026)
        # =====================================================================
        # 🔄 REFACTOR Feb 06: LOCATION_EXTREME ya NO es HARD BLOCK
        # Es SOFT MODE: reduce size, requiere confirmación, tightens stops
        # 
        # LÓGICA:
        # - Percentil <= 10% NO bloquea shorts automáticamente
        # - Detectamos si es AGOTAMIENTO (no short) vs PÁNICO (yes short)
        # - PANIC MODE = displacement + ADX alto + volumen → permite short
        # - Sin pánico = SOFT MODE (40% size, tight stops, need confirm)
        # =====================================================================
        from src.config.trading_config import LocationFilters
        location_ctx = getattr(engine, '_location_context', None)
        if location_ctx:
            pct_60d = getattr(location_ctx, 'percentile_60d', 50)
            # Use centralized thresholds instead of hardcoded 10/90
            in_extreme_low = (
                getattr(location_ctx, 'in_extreme_low', False) or 
                pct_60d <= LocationFilters.EXTREME_LOW_PERCENTILE
            )
            in_extreme_high = (
                getattr(location_ctx, 'in_extreme_high', False) or 
                pct_60d >= LocationFilters.EXTREME_HIGH_PERCENTILE
            )
            
            # 🔥 FIX #56: PANIC DETECTOR - Distinguir agotamiento vs pánico
            # PÁNICO = El mercado sigue cayendo con fuerza, hay más caída por venir
            # AGOTAMIENTO = El mercado tocó fondo, probable rebote
            is_breakout_down = getattr(location_ctx, 'is_breakout_down', False)
            adx_current = getattr(location_ctx, 'adx_current', 0)
            breakout_down_score = getattr(location_ctx, 'breakout_down_score', 0)
            
            engine_adx = getattr(engine, '_current_adx', None) or adx_current
            effective_adx = max(adx_current, engine_adx) if engine_adx else adx_current
            
            displacement_bypass = engine._current_trade_tags.get('fp_displacement_bypass', False)
            
            # Get ADX 5m for panic detection (fast momentum)
            adx_5m = engine._current_trade_tags.get('adx_5m', 0) or engine._current_trade_tags.get('adx_5m_fast', 0)
            # Get volume ratio if available  
            vol_ratio = engine._current_trade_tags.get('move_vol_ratio', 1.0)
            
            # 🔥 FIX #56: Use CrashDetector for panic detection
            from src.domain.market.crash_detector import CrashDetector
            crash_detector = CrashDetector()
            
            # Calculate displacement from price move if available
            displacement_amount = engine._current_trade_tags.get('displacement_amount', 0)
            current_price = ctx.current_price
            displacement_pct = displacement_amount / current_price if current_price > 0 else 0
            
            crash_result = crash_detector.detect(
                displacement_pct=-abs(displacement_pct) if ctx.bias == 'SHORT' else abs(displacement_pct),
                adx_5m=adx_5m,
                adx_1h=effective_adx,
                vol_ratio=vol_ratio,
                direction=ctx.bias
            )
            
            is_panic_mode = crash_result.is_panic_down if ctx.bias == 'SHORT' else crash_result.is_meltup
            engine._current_trade_tags['crash_detector_result'] = crash_result.reason
            engine._current_trade_tags['crash_detector_confidence'] = crash_result.confidence
            
            # 🔥 FIX A3 (Feb 07, 2026): Invalidar edge si crash en dirección contraria
            edge_mgr_crash = getattr(engine, '_edge_manager', None)
            if edge_mgr_crash is not None:
                crash_invalidated = edge_mgr_crash.check_crash_invalidation(
                    is_panic_down=crash_result.is_panic_down,
                    is_meltup=crash_result.is_meltup,
                    crash_confidence=crash_result.confidence
                )
                if crash_invalidated:
                    engine._current_trade_tags['edge_crash_invalidated'] = True
                    logger.warning(
                        f"🔥 FIX A3: Edge invalidated by crash detector | "
                        f"panic_down={crash_result.is_panic_down} meltup={crash_result.is_meltup}"
                    )
            
            # 🔥 FIX #56: Use AbsorptionDetector to check for reversal signals
            from src.domain.market.absorption_detector import AbsorptionDetector
            absorption_detector = AbsorptionDetector()
            
            # Get VWAP if available
            vwap_price = engine._current_trade_tags.get('vwap_price', None)
            delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', None)
            
            absorption_result = absorption_detector.detect(
                df_5m=ctx.df_5m,
                current_price=current_price,
                vwap=vwap_price,
                delta_ratio=delta_ratio,
                direction=ctx.bias
            )
            
            has_absorption = (
                absorption_result.is_bullish_absorption if ctx.bias == 'SHORT'
                else absorption_result.is_bearish_distribution
            )
            engine._current_trade_tags['absorption_detector_result'] = absorption_result.reason
            engine._current_trade_tags['absorption_detector_signals'] = absorption_result.signal_count
            
            # =================================================================
            # 🔥 FIX #143 + FIX #147: ABSORPTION/DELTA COMBO BLOCK
            # =================================================================
            # FIX #143: Absorption con conf >= 50% → BLOQUEAR
            # FIX #147: Si delta SEVERE + absorption (cualquier conf) → BLOQUEAR
            # 🔥 FIX #152 (Feb 10, 2026): DELTA MODERATE + ABSORPTION = ALSO BLOCK
            # 
            # La idea: delta moderate (3-5x) + absorption = mercado contradice
            # No es lagging, es confirmación de que el lado contrario está activo.
            # Entrar contra eso es "jugar a la lotería".
            # =================================================================
            absorption_conf = absorption_result.confidence if hasattr(absorption_result, 'confidence') else 50
            delta_severe = engine._current_trade_tags.get('delta_severe_contradict', False)
            delta_moderate = engine._current_trade_tags.get('delta_info_contradict', False) and not delta_severe
            delta_any = delta_severe or delta_moderate
            delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', 0)
            
            # 🔥 FIX #152: Delta ANY + absorption = BLOCK (not just severe)
            # Delta SEVERE + absorption (any conf) → threshold 30
            # Delta MODERATE + absorption → threshold 40 (más estricto que antes)
            # Solo absorption (no delta) → threshold 50
            if delta_severe:
                effective_absorption_threshold = 30
            elif delta_moderate:
                effective_absorption_threshold = 40  # 🔥 FIX #152: MODERATE + absorption = también peligroso
            else:
                effective_absorption_threshold = 50
            
            engine._current_trade_tags['absorption_contrary'] = has_absorption and absorption_conf >= effective_absorption_threshold
            
            if has_absorption and absorption_conf >= effective_absorption_threshold:
                log_key = 'ABSORPTION_BLOCK'
                last_log = _last_guard_log.get(log_key)
                should_log = last_log is None or (ctx.market_time - last_log) > _guard_log_interval
                
                # 🔥 FIX #152: Mensaje diferenciado para combo
                if delta_severe:
                    block_type = "DELTA_SEVERE+ABSORPTION"
                elif delta_moderate:
                    block_type = "DELTA_MODERATE+ABSORPTION"  # 🔥 FIX #152
                else:
                    block_type = "ABSORPTION_ONLY"
                
                if should_log:
                    _last_guard_log[log_key] = ctx.market_time
                    logger.warning(
                        f"🛑 FIX #152: {block_type} BLOCK | {ctx.bias} blocked by "
                        f"{'BULLISH' if ctx.bias == 'SHORT' else 'BEARISH'} absorption | "
                        f"conf={absorption_conf}% (threshold={effective_absorption_threshold}%) | "
                        f"delta={delta_ratio:+.2f}x | signals={absorption_result.signal_count} | "
                        f"reason={absorption_result.reason}"
                    )
                
                engine._current_trade_tags['absorption_blocked'] = True
                engine._current_trade_tags['absorption_block_reason'] = absorption_result.reason
                
                # HARD BLOCK - no penalización, directamente no entrar
                return result._replace(
                    can_trade=False,
                    block_reason=f"ABSORPTION_BLOCK: {absorption_result.reason}"
                )
            
            # 🔥 BREAKOUT OVERRIDE: Still allow if clear structure break
            allow_short_breakout = (
                breakout_down_score >= 3  # Multiple confirmations = structure break
            ) or (
                is_breakout_down and pct_60d > 5.0  # Breakout but NOT at floor yet
            ) or (
                is_panic_mode and not has_absorption  # 🔥 FIX #56: Panic WITHOUT absorption
            )
            
            # 🔥 FIX #56: SOFT MODE instead of HARD BLOCK
            # En extreme low, NO bloqueamos shorts - los modulamos
            if ctx.bias == 'SHORT' and in_extreme_low and not allow_short_breakout:
                # 🔥 FIX #56 + #81: Use penalty accumulator instead of just tagging
                now = ctx.market_time
                log_key = 'LOCATION_EXTREME_SOFT_SHORT'
                last_log = _last_guard_log.get(log_key)
                should_log = last_log is None or (now - last_log) > _guard_log_interval
                
                if should_log:
                    _last_guard_log[log_key] = now
                    logger.warning(
                        f"⚠️ LOCATION SOFT MODE: SHORT at extreme low ({pct_60d:.1f}%) | "
                        f"Size→40%, TightStops | ADX_5m={adx_5m:.0f} vol={vol_ratio:.1f}"
                    )
                
                # 🔥 FIX #81: Add penalty to accumulator
                penalties.add_penalty(
                    source=PenaltySource.LOCATION_EXTREME,
                    size_mult=0.40,
                    score_adj=-1.0,
                    reason=f"SHORT at extreme low ({pct_60d:.1f}%)"
                )
                engine._current_trade_tags['location_extreme_soft_mode'] = True
                engine._current_trade_tags['location_extreme_percentile'] = pct_60d
                
                # 🔥 FIX #56: NO return result - dejamos que continúe pero con soft mode
                # El size cap y tight stops se aplicarán en el sizing/risk engine
            
            # 🔥 FIX #56: Log cuando PANIC MODE o BREAKOUT permite trading normal
            if ctx.bias == 'SHORT' and in_extreme_low and allow_short_breakout:
                log_key = 'LOCATION_PANIC_OVERRIDE_SHORT'
                last_log = _last_guard_log.get(log_key)
                should_log = last_log is None or (ctx.market_time - last_log) > _guard_log_interval
                if should_log:
                    _last_guard_log[log_key] = ctx.market_time
                    mode_reason = "PANIC_MODE" if is_panic_mode else f"BREAKOUT_SCORE={breakout_down_score}"
                    logger.info(
                        f"🔥 LOCATION OVERRIDE: {mode_reason} | SHORT at low ({pct_60d:.1f}%) | "
                        f"ADX_1h={effective_adx:.0f} ADX_5m={adx_5m:.0f} | Full size allowed"
                    )
            
            # 🔥 FIX #56: Same logic for LONG at extreme high
            is_breakout_up = getattr(location_ctx, 'is_breakout_up', False)
            breakout_up_score = getattr(location_ctx, 'breakout_up_score', 0)
            
            # Check MELT-UP mode (euphoria) - use CrashDetector result
            is_meltup_mode = crash_result.is_meltup
            
            allow_long_breakout = (
                breakout_up_score >= 3  # Multiple confirmations = structure break
            ) or (
                is_breakout_up and pct_60d < 95.0  # Breakout but NOT at ceiling yet
            ) or (
                is_meltup_mode  # 🔥 FIX #56: Melt-up mode overrides location extreme
            )
            
            if ctx.bias == 'LONG' and in_extreme_high and not allow_long_breakout:
                # 🔥 FIX #56 + #81: Use penalty accumulator instead of just tagging
                now = ctx.market_time
                log_key = 'LOCATION_EXTREME_SOFT_LONG'
                last_log = _last_guard_log.get(log_key)
                should_log = last_log is None or (now - last_log) > _guard_log_interval
                
                if should_log:
                    _last_guard_log[log_key] = now
                    logger.warning(
                        f"⚠️ LOCATION SOFT MODE: LONG at extreme high ({pct_60d:.1f}%) | "
                        f"Size→40%, TightStops | ADX_5m={adx_5m:.0f} vol={vol_ratio:.1f}"
                    )
                
                # 🔥 FIX #81: Add penalty to accumulator
                penalties.add_penalty(
                    source=PenaltySource.LOCATION_EXTREME,
                    size_mult=0.40,
                    score_adj=-1.0,
                    reason=f"LONG at extreme high ({pct_60d:.1f}%)"
                )
                engine._current_trade_tags['location_extreme_soft_mode'] = True
                engine._current_trade_tags['location_extreme_percentile'] = pct_60d
                
                # 🔥 FIX #56: NO return result - dejamos que continúe pero con soft mode
            
            # Log when MELT-UP mode allows normal trading
            if ctx.bias == 'LONG' and in_extreme_high and allow_long_breakout:
                log_key = 'LOCATION_MELTUP_OVERRIDE_LONG'
                last_log = _last_guard_log.get(log_key)
                should_log = last_log is None or (ctx.market_time - last_log) > _guard_log_interval
                if should_log:
                    _last_guard_log[log_key] = ctx.market_time
                    mode_reason = "MELTUP_MODE" if is_meltup_mode else f"BREAKOUT_SCORE={breakout_up_score}"
                    logger.info(
                        f"🔥 LOCATION OVERRIDE: {mode_reason} | LONG at high ({pct_60d:.1f}%) | "
                        f"ADX_1h={effective_adx:.0f} ADX_5m={adx_5m:.0f} | Full size allowed"
                    )
        
        # =====================================================================
        # 🚨 GUARD 13: COUNTER-MOMENTUM BLOCK (Fix #17 - Feb 02, 2026)
        # 🔄 REFACTOR Feb 04: Uses DirectionStateManager.validate_direction()
        # 🔥 FIX #77 (Feb 07): REDUCE instead of BLOCK for most cases
        # SINGLE SOURCE OF TRUTH for direction validation
        # =====================================================================
        direction_mgr = getattr(engine, 'direction_state_mgr', None)
        if direction_mgr is not None:
            # Sync current state from engine to manager
            direction_mgr.sync_from_app(
                direction_lock=getattr(engine, '_direction_lock', None),
                direction_lock_reason=getattr(engine, '_direction_lock_reason', None),
                direction_lock_expires_at=getattr(engine, '_direction_lock_expires_at', None),
                direction_lock_initial_move=getattr(engine, '_direction_lock_initial_move', 0.0),
                expansion_mode=getattr(engine, '_expansion_mode', False),
                expansion_direction=getattr(engine, '_expansion_direction', None),
                expansion_start_time=getattr(engine, '_expansion_start_time', None),
                expansion_initial_displacement=getattr(engine, '_expansion_initial_displacement', 0.0),
                bias_high_since=getattr(engine, '_bias_high_since', 0.0),
                bias_low_since=getattr(engine, '_bias_low_since', 0.0),
                forbidden_direction=getattr(engine, '_forbidden_direction', None),
            )
            
            # 🔥 FIX #77: Unified direction validation returns penalties, not just blocks
            is_allowed, reason, category, dir_size_mult, dir_score_penalty = direction_mgr.validate_direction(
                proposed_direction=ctx.bias,
                is_edge_active=(edge_mgr.is_edge_active() if edge_mgr else False)
            )
            
            if not is_allowed:
                # Only truly dangerous situations (counter-expansion < 2min)
                logger.warning(
                    f"🚫🚫🚫 GUARD BLOCKED: {category.upper().replace('_', ' ')} | "
                    f"{reason}"
                )
                result.passed = False
                result.reason = reason
                result.tags['block_category'] = category
                engine._current_trade_tags[f'guard_blocked_by_{category}'] = True
                return result
            elif dir_size_mult < 1.0 or dir_score_penalty < 0:
                # 🔥 FIX #77 + #81: Use penalty accumulator instead of result.tags
                logger.info(
                    f"⚠️ FIX #77 DIRECTION PENALTY: {ctx.bias} | "
                    f"size×{dir_size_mult:.0%}, score{dir_score_penalty:+.1f} | "
                    f"Reason: {reason}"
                )
                # Determine the right penalty source
                penalty_source = PenaltySource.COUNTER_EXPANSION if 'expansion' in category else PenaltySource.DIRECTION_LOCK
                penalties.add_penalty(
                    source=penalty_source,
                    size_mult=dir_size_mult,
                    score_adj=dir_score_penalty,
                    reason=reason
                )
                engine._current_trade_tags['direction_penalty_applied'] = True
                # Continue with trade - penalties will be aggregated
        else:
            # Fallback: Legacy checks if DirectionStateManager not available
            # 🔥 FIX #77 + #81: Use penalty accumulator
            direction_lock = getattr(engine, '_direction_lock', None)
            if direction_lock and direction_lock != ctx.bias:
                logger.info(
                    f"⚠️ FIX #77 DIRECTION PENALTY (legacy): {ctx.bias} vs lock {direction_lock} | "
                    f"size×50%, score-1.5"
                )
                penalties.add_penalty(
                    source=PenaltySource.DIRECTION_LOCK,
                    size_mult=0.50,
                    score_adj=-1.5,
                    reason=f"Direction lock {direction_lock} vs {ctx.bias}"
                )
                engine._current_trade_tags['direction_penalty_applied'] = True
                # Continue - don't block
            
            forbidden_dir = getattr(engine, '_forbidden_direction', None)
            if forbidden_dir == ctx.bias:
                logger.info(
                    f"⚠️ FIX #77 FORBIDDEN PENALTY (legacy): {ctx.bias} forbidden | "
                    f"size×30%, score-2.0"
                )
                penalties.add_penalty(
                    source=PenaltySource.FORBIDDEN_DIRECTION,
                    size_mult=0.30,
                    score_adj=-2.0,
                    reason=f"Forbidden direction {ctx.bias}"
                )
                engine._current_trade_tags['forbidden_penalty_applied'] = True
                # Continue - don't block
        
        # =====================================================================
        # 🚨 GUARD 14: FAKEOUT DETECTOR (Fix #18 - Feb 02, 2026)
        # 🔥 FIX #94 (Feb 09, 2026): TODO es PENALTY, no hay hard block
        # =====================================================================
        # ANTES: >80% retrace → HARD BLOCK
        # AHORA: >80% retrace → SEVERE PENALTY pero el floor de 50% aplica
        #
        # RAZÓN: El sistema bloqueaba DEMASIADOS trades. Es mejor entrar
        # con posición pequeña que no entrar nunca.
        # =====================================================================
        if edge_mgr is not None and edge_mgr.current_bos is not None:
            bos = edge_mgr.current_bos
            origin_price = bos.origin_price or 0
            initial_disp = bos.initial_displacement_amount or 0
            
            if initial_disp > 0 and origin_price > 0:
                # Calculate retracement percentage
                if ctx.bias == 'LONG':
                    peak_price = origin_price + initial_disp
                    retracement_pct = ((peak_price - ctx.current_price) / initial_disp) * 100 if initial_disp > 0 else 0
                else:
                    bottom_price = origin_price - initial_disp
                    retracement_pct = ((ctx.current_price - bottom_price) / initial_disp) * 100 if initial_disp > 0 else 0
                
                retracement_pct = max(0, retracement_pct)
                
                # 🔥 FIX #94: TODO es penalty, el floor de 50% se aplicará después
                if retracement_pct > 80:
                    # Fakeout severo - pero NO bloqueamos, aplicamos penalty máximo
                    logger.warning(
                        f"⚠️ FAKEOUT SEVERE: {ctx.bias} retrace {retracement_pct:.0f}% | "
                        f"Aplicando penalty máximo (floor 50% aplicará)"
                    )
                    penalties.add_penalty(
                        source=PenaltySource.FAKEOUT,
                        size_mult=0.10,  # Will be floored to 50%
                        score_adj=-2.0,
                        reason=f"Fakeout severe ({retracement_pct:.0f}% retrace)"
                    )
                    engine._current_trade_tags['fakeout_severe'] = True
                elif retracement_pct > 50:
                    penalties.add_penalty(
                        source=PenaltySource.FAKEOUT,
                        size_mult=0.40,
                        score_adj=-1.0,
                        reason=f"Fakeout ({retracement_pct:.0f}% retrace)"
                    )
                    engine._current_trade_tags['fakeout_caution'] = True
                # Eliminado: penalties para 40-50% - demasiado conservador
                
                if retracement_pct > 50:
                    engine._current_trade_tags['fakeout_retracement'] = retracement_pct
        
        # =====================================================================
        # � FIX #133 (Feb 09, 2026): GUARD 15 DISABLED - LOGGING ONLY
        # =====================================================================
        # PROBLEMA IDENTIFICADO: Este guard causa DOUBLE-COUNTING de penalties.
        # 
        # EVIDENCIA: Las señales que cuenta (bias_invalidated, forbidden_direction,
        # delta_contradicts) YA aplicaron penalties individuales antes de llegar aquí.
        # Después, este guard aplica OTRA penalty encima.
        # 
        # EJEMPLO: 
        #   - delta_contradicts → -0.5 score (Guard 10)
        #   - forbidden_direction → -2.0 score + 30% size (Guard 11)
        #   - Kill Check cuenta 2 signals → -1.0 score + 50% size ADICIONAL
        #   - TOTAL: -3.5 score (cuando max lógico sería -2.5)
        #
        # SOLUCIÓN: Convertir a logging-only para diagnóstico, sin penalties.
        # El FIX #131 (MAX_TOTAL_PENALTY = -3.0) ya protege contra acumulación.
        # =====================================================================
        danger_signals = 0
        danger_reasons = []
        current_tags = getattr(engine, '_current_trade_tags', {})
        
        # Still count for logging/diagnostics
        danger_tag_checks = [
            ('bias_invalidated', 'Bias invalidated'),
            ('forbidden_direction', 'Forbidden dir'),
            ('delta_contradicts', 'Delta contra'),
            ('expansion_mode_activated', 'Expansion'),
            ('structure_break_against', 'Structure break'),
        ]
        
        for tag_name, reason_text in danger_tag_checks:
            if current_tags.get(tag_name):
                danger_signals += 1
                danger_reasons.append(reason_text)
        
        # Log for diagnostics but NO PENALTY (FIX #133)
        if danger_signals >= 2:
            logger.debug(
                f"📊 FIX #133 KILL CHECK (LOGGING ONLY): {danger_signals} signals | "
                f"{', '.join(danger_reasons)} | No penalty applied (handled by individual guards)"
            )
            engine._current_trade_tags['danger_signal_count_diagnostic'] = danger_signals
            # NO penalties.add_penalty() - individual guards already handled these
        
        # 15. Session limits (just flag - override checked later)
        # 🔥 FIX (Feb 02, 2026): Apply mode-based cap to session limit
        # 🔥 FIX #10: Hot Hand system - base guaranteed, bonus on wins after base
        session_trades = engine._session_trade_counts.get(ctx.session_name, 0)
        base_limit = SessionTradeLimits.MAX_TRADES_PER_SESSION.get(ctx.session_name, 3)
        
        # Apply MODE_TRADE_LIMIT_CAP based on current regime
        current_mode = getattr(engine, '_current_trade_regime', 'RANGE')
        mode_cap = SessionTradeLimits.MODE_TRADE_LIMIT_CAP.get(current_mode, 5)
        base_limit = min(base_limit, mode_cap)
        
        # 🔥 FIX #10: Hot Hand logic
        # - If session_trades < base_limit: ALWAYS allowed (base guaranteed)
        # - If session_trades >= base_limit: only if last trade was WIN
        from src.domain.trading.activity_tracker import get_activity_tracker
        tracker = get_activity_tracker()
        
        # Reset session state if session changed
        tracker.reset_session_state(ctx.session_name)
        
        session_limit = base_limit  # Start with base
        hot_hand_bonus = 0
        session_stopped = False
        
        if session_trades >= base_limit:
            # Beyond base - check Hot Hand
            if tracker.last_trade_was_win():
                # Last trade was WIN → allow +1 bonus
                hot_hand_bonus = 1
                session_limit = session_trades + 1  # Always allow one more after a win
                result.tags['hot_hand_bonus'] = hot_hand_bonus
            else:
                # Last trade was LOSS → check if session is still profitable
                # 🔥 FIX #82: Don't stop if session P&L is positive
                # 🔥 FIX #83: Relaxed criteria - also allow if loss was small/administrative
                session_pnl_r = tracker.get_session_pnl_r()
                last_exit = tracker.get_last_exit_reason()
                last_pnl_r = tracker.get_last_trade_pnl_r()
                
                # Allow if:
                # 1. Session P&L >= +0.5R (profitable)
                # 2. Session P&L >= -0.25R AND loss was small (< 0.25R)
                # 3. Last exit was administrative (EARLY_MFE, WTP, etc.)
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
                    # Override Hot Hand stop
                    session_limit = session_trades + 1
                    result.tags['session_profitable_override'] = True
                    result.tags['session_pnl_r'] = session_pnl_r
                    override_reason = []
                    if session_pnl_r >= 0.5:
                        override_reason.append(f"profitable ({session_pnl_r:+.2f}R)")
                    if is_small_loss:
                        override_reason.append(f"small_loss ({last_pnl_r:+.2f}R)")
                    if is_administrative_exit:
                        override_reason.append(f"admin_exit ({last_exit})")
                    logger.info(
                        f"🔥 FIX #83: Hot Hand OVERRIDE | Session P&L: {session_pnl_r:+.2f}R | "
                        f"Reasons: {', '.join(override_reason)} | Allowing trade"
                    )
                else:
                    session_stopped = True
                    result.tags['session_stopped'] = True
                    result.tags['session_limit_reason'] = 'HOT_HAND_LOSS_AFTER_BASE'
                    result.tags['session_pnl_r'] = session_pnl_r
        
        result.tags['session_at_limit'] = session_trades >= session_limit
        result.tags['session_trades'] = session_trades
        result.tags['session_limit'] = session_limit
        result.tags['session_base_limit'] = base_limit
        result.tags['session_mode_cap'] = mode_cap
        
        # 🔥 FIX #60.2 (Feb 06): Reset OBSTINACY counter if all guards passed
        # Conflicto #9: Counter se acumulaba incorrectamente si guards fallaban
        # por otras razones (spread, location). Ahora solo se acumula cuando
        # el bloqueo es específicamente por SHORT_OBSTINACY.
        if result.passed:
            engine._obstinacy_block_count = 0
        
        # =====================================================================
        # 🔥 FIX #94: FINAL PENALTY AGGREGATION (Simplificado)
        # =====================================================================
        # El sistema tenía DEMASIADOS penalties que se acumulaban y mataban
        # todos los trades. Ahora:
        # - Floor de 50% FORZADO (ver risk_penalty.py)
        # - Solo bloquear si should_block=True (catastrófico)
        # =====================================================================
        penalty_result = penalties.get_result()
        
        if penalty_result.should_block:
            # Solo bloquear si es CATASTRÓFICO (no por acumulación de penalties menores)
            logger.warning(
                f"🚫 GUARD BLOCKED: {penalty_result.block_reason}"
            )
            result.passed = False
            result.reason = penalty_result.block_reason
            result.tags['block_category'] = 'catastrophic'
            engine._current_trade_tags['guard_blocked_by_catastrophic'] = True
            return result
        
        # Apply accumulated penalties to result
        result.final_size_mult = penalty_result.final_size_mult
        result.total_score_adj = penalty_result.total_score_adj
        result.penalty_sources = [p.source.value for p in penalty_result.penalties]
        
        # Log if significant penalties applied
        if penalty_result.final_size_mult < 1.0 or penalty_result.total_score_adj < 0:
            penalty_summary = ", ".join([
                f"{p.source.value}(×{p.size_mult:.0%})" 
                for p in penalty_result.penalties
            ])
            logger.info(
                f"📊 FIX #81 PENALTY SUMMARY: {ctx.bias} | "
                f"Final size={penalty_result.final_size_mult:.0%}, "
                f"Score adj={penalty_result.total_score_adj:+.1f} | "
                f"Sources: {penalty_summary}"
            )
            engine._current_trade_tags['penalty_final_size_mult'] = penalty_result.final_size_mult
            engine._current_trade_tags['penalty_total_score_adj'] = penalty_result.total_score_adj
            engine._current_trade_tags['penalty_sources'] = [p.source.value for p in penalty_result.penalties]
        
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
        
        # 🔥 FIX #56: Get expansion mode and ADX for structure gate overrides
        expansion_mode_active = engine._current_trade_tags.get('expansion_mode_activated', False)
        effective_adx = engine._current_trade_tags.get('effective_adx', 0) or engine._current_trade_tags.get('htf_adx', 0)
        
        # 🔥 FIX #61: Check if bias was inverted by micro-trend
        bias_inverted = engine._current_trade_tags.get('bias_inverted', False)
        
        # 🔥 FIX #63: Check if bias was invalidated (opposite direction forbidden)
        # This happens when price moves significantly against the old bias
        # e.g., "SHORT invalidated → FORBID SHORT, LONG detection ENABLED"
        bias_invalidated = engine._current_trade_tags.get('bias_invalidated', False)
        
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
            displacement_edge_active=displacement_edge_active,  # 🔥 FIX: Bypass structure for displacement
            expansion_mode_active=expansion_mode_active,        # 🔥 FIX #56: Bypass for expansion
            strong_trend_adx=effective_adx,                     # 🔥 FIX #56: Bypass for strong trend
            bias_inverted=bias_inverted,                        # 🔥 FIX #61: Bypass for bias inversion
            bias_invalidated=bias_invalidated,                  # 🔥 FIX #63: Bypass for bias flip
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
        
        # 🧠 FIX #20: OBSTINACY SOFT MODE additional penalty
        # When SOFT mode allows a trade that would normally be blocked,
        # apply extra score penalty as additional risk control
        soft_mode_penalty = engine._current_trade_tags.get('obstinacy_soft_score_penalty', 0)
        if soft_mode_penalty < 0:
            result.score = max(0, result.score + soft_mode_penalty)
            result.tags['obstinacy_soft_penalty'] = soft_mode_penalty
            logger.info(f"🧠 OBSTINACY SOFT MODE: Applied score penalty {soft_mode_penalty}")
        
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
        
        # =====================================================================
        # 🔥 FIX #77-80 (Feb 07, 2026): GUARD SCORE PENALTIES
        # These graduated penalties replace hard blocks with score adjustments.
        # =====================================================================
        
        # FIX #77: Direction penalties (counter-momentum, forbidden, expansion)
        direction_score_penalty = engine._current_trade_tags.get('direction_score_penalty', 0)
        if direction_score_penalty < 0:
            result.score = max(0, result.score + direction_score_penalty)
            result.tags['direction_penalty'] = direction_score_penalty
            logger.info(f"⚠️ FIX #77 DIRECTION SCORE: Applied penalty {direction_score_penalty}")
        
        # FIX #78: Displacement chase penalty
        chase_score_penalty = engine._current_trade_tags.get('chase_score_penalty', 0)
        if chase_score_penalty < 0:
            result.score = max(0, result.score + chase_score_penalty)
            result.tags['chase_penalty'] = chase_score_penalty
            logger.info(f"⚠️ FIX #78 CHASE SCORE: Applied penalty {chase_score_penalty}")
        
        # FIX #79: Fakeout penalty
        fakeout_score_penalty = engine._current_trade_tags.get('fakeout_score_penalty', 0)
        if fakeout_score_penalty < 0:
            result.score = max(0, result.score + fakeout_score_penalty)
            result.tags['fakeout_penalty'] = fakeout_score_penalty
            logger.info(f"⚠️ FIX #79 FAKEOUT SCORE: Applied penalty {fakeout_score_penalty}")
        
        # 🔥 FIX #133 (Feb 09, 2026): Kill check disabled - was causing double-counting
        # The individual guards (delta, forbidden, etc.) already applied penalties
        # No additional kill_check_score_penalty needed
        
        # =====================================================================
        
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
        
        # 🔄 REFACTORED Feb 04: Use ADXThresholds for consistent threshold
        from src.config.trading_config import ADXThresholds
        is_trend = ADXThresholds.is_strong_trend(current_adx) or ctx.volatility_regime in [VolatilityRegime.HIGH, VolatilityRegime.SPIKE, 'HIGH', 'SPIKE']
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
        
        # 🔥 FIX (Feb 04, 2026): TRANSITION mode requires higher threshold
        # Reason: TRANSITION (0.35-0.50 score) is ambiguous territory - need more confirmation
        trading_mode = engine._current_trade_tags.get('trading_mode', '') if hasattr(engine, '_current_trade_tags') else ''
        if trading_mode == 'TRANSITION':
            transition_min = RiskManagement.TRANSITION_MIN_SCORE
            if threshold < transition_min:
                # 🔥 FIX #151: Rate-limit to avoid log spam (only debug level)
                logger.debug(f"📊 TRANSITION MODE: Raising threshold from {threshold:.1f} to {transition_min:.1f}")
                threshold = transition_min
        
        # =====================================================================
        # 🔥 FIX #102a (Feb 09, 2026): REGIME CONFLICT PENALTY
        # =====================================================================
        # PROBLEMA: trading_mode=TREND pero trend_phase=RANGE_BOUND
        # Esto es una BANDERA ROJA - estás en zona híbrida
        # 
        # SEÑAL: El sistema piensa que hay trend, pero la fase dice range
        # RESULTADO: Señales que funcionan a medias, zona de más pérdidas
        # 
        # SOLUCIÓN: Aumentar threshold cuando hay conflicto
        # - TREND mode + RANGE_BOUND phase → +1.0 threshold (exigir más)
        # - Log para transparencia
        # =====================================================================
        from src.domain.market.regime.trend_intelligence import TrendPhase
        
        is_regime_conflict = (
            trading_mode == 'TREND' and 
            ctx.trend_phase == TrendPhase.RANGE_BOUND
        )
        
        if is_regime_conflict:
            REGIME_CONFLICT_PENALTY = 1.0  # Exigir 1 punto más de score
            threshold += REGIME_CONFLICT_PENALTY
            logger.warning(
                f"⚠️ FIX #102a REGIME CONFLICT: trading_mode=TREND but phase=RANGE_BOUND | "
                f"Threshold +{REGIME_CONFLICT_PENALTY} → {threshold:.1f}"
            )
            # Store for sizing penalty later
            if hasattr(engine, '_current_trade_tags'):
                engine._current_trade_tags['regime_conflict'] = True
                engine._current_trade_tags['regime_conflict_threshold_adj'] = REGIME_CONFLICT_PENALTY
        
        # =====================================================================
        # 🔥 FIX #115 (Feb 09, 2026): TRANSITION_MODE REDUCES THRESHOLD
        #
        # PROBLEMA (P6): El sistema sube el threshold en TRANSITION mode,
        # pero TRANSITION_MODE (post-BIAS_FLIP) es cuando DEBERÍAMOS ser
        # más agresivos porque hay edge de transición.
        #
        # SOLUCIÓN: Cuando _transition_mode_active, BAJAR threshold
        # - El mercado acaba de girar (BIAS_FLIP)
        # - Tenemos ventana de 15min para capturar el edge
        # - Score más bajo permite entrar antes del movimiento
        # =====================================================================
        transition_mode_active = getattr(engine, '_transition_mode_active', False)
        transition_direction = getattr(engine, '_transition_mode_direction', None)
        
        if transition_mode_active and transition_direction:
            TRANSITION_MODE_THRESHOLD_BONUS = -1.0  # Bajar 1 punto
            original_threshold = threshold
            threshold = max(4.0, threshold + TRANSITION_MODE_THRESHOLD_BONUS)  # Floor en 4.0
            
            # Solo aplicar si la dirección coincide
            if hasattr(ctx, 'bias') and ctx.bias == transition_direction:
                # 🔧 FIX #129: Throttle log to once per 2 minutes
                global _last_transition_mode_log
                market_time = ctx.market_time if hasattr(ctx, 'market_time') else datetime.now()
                if _last_transition_mode_log is None or \
                   (market_time - _last_transition_mode_log).total_seconds() >= 120:
                    logger.info(
                        f"🚀 FIX #115 TRANSITION_MODE: Lowering threshold from {original_threshold:.1f} to {threshold:.1f} | "
                        f"Direction={transition_direction}"
                    )
                    _last_transition_mode_log = market_time
                if hasattr(engine, '_current_trade_tags'):
                    engine._current_trade_tags['transition_mode_threshold_bonus'] = TRANSITION_MODE_THRESHOLD_BONUS
        
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
                logger.info(f"📊 EDGE SIZING | Type: {edge_info} | Range: {min_mult:.2f}x-{max_mult:.2f}x | Using: {max_mult:.2f}x")
            else:
                logger.debug("📊 NO EDGE active - using conservative sizing 0.60x")
        
        # Store edge info in trade tags for analysis
        if hasattr(engine, '_current_trade_tags'):
            engine._current_trade_tags['edge_mode'] = edge_mode
            engine._current_trade_tags['edge_mode_multiplier'] = edge_mode_multiplier
            engine._current_trade_tags['edge_info'] = edge_info
        
        # 🔥 FIX (Feb 2, 2026): Use last LOSS time for cooldown (not last trade time)
        # Cooldown should wait after a loss, not after any trade
        from src.domain.trading.activity_tracker import get_last_loss_time
        last_loss_time = get_last_loss_time()
        
        # 🔥 NEW (Feb 2, 2026): Get momentum score for sizing predictor
        score_breakdown = getattr(engine, '_score_breakdown', {})
        momentum_score = float(score_breakdown.get('momentum', 1.0))  # Default to neutral
        
        # 🔥 FIX #83 (Feb 7, 2026): Get RegimeFusion alignment multiplier
        regime_fusion_mult = engine._current_trade_tags.get('regime_size_mult', 1.0)
        
        # Calculate using simplified system WITH edge mode
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
            liquidity_event=None,  # Liquidity events handled at edge level now
            # 🔥 NEW (Feb 2, 2026): Toxic combo, hour, and cooldown parameters
            session_name=ctx.session_name,
            direction=ctx.bias,  # Use ctx.bias for direction
            market_time=ctx.market_time,
            last_trade_time=last_loss_time,  # 🔥 FIX: Use last LOSS time for cooldown
            momentum_score=momentum_score,  # 🔥 NEW (Feb 2): For momentum sizing predictor
            regime_fusion_mult=regime_fusion_mult,  # 🔥 FIX #83: Macro/Micro alignment
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
        
        # =====================================================================
        # FIX #102a: REGIME CONFLICT SIZE PENALTY (RESTORED)
        # =====================================================================
        # Si hay conflicto de régimen (TREND mode + RANGE_BOUND phase),
        # reducir tamaño adicional × 0.6 (además del threshold penalty)
        # =====================================================================
        REGIME_CONFLICT_SIZE_MULT = 0.60  # 40% reduction
        regime_conflict = engine._current_trade_tags.get('regime_conflict', False)
        if regime_conflict:
            result.size *= REGIME_CONFLICT_SIZE_MULT
            result.size = round_size_to_exchange(result.size)
            result.size_usd = result.size * ctx.current_price
            logger.warning(
                f"⚠️ FIX #102a SIZE PENALTY: Regime conflict → Size × {REGIME_CONFLICT_SIZE_MULT} = {result.size:.6f}"
            )
        
        # Store multipliers for logging
        result.multipliers = {
            'rr': sizing_result.rr_mult,
            'regime': sizing_result.regime_mult,
            'loss_streak': sizing_result.loss_streak_mult,
            'session': sizing_result.session_mult,
            'edge_mode': sizing_result.edge_mult,
            'toxic_hour': sizing_result.toxic_hour_mult,
            'toxic_combo': sizing_result.toxic_combo_mult,  # Toxic combo (REDUCE mode)
            'momentum': sizing_result.momentum_mult,
            'regime_fusion': sizing_result.regime_fusion_mult,  # FIX #83: Macro/Micro alignment
            'regime_conflict': REGIME_CONFLICT_SIZE_MULT if regime_conflict else 1.0,  # FIX #102a
        }
        result.final_multiplier = sizing_result.combined_multiplier * (REGIME_CONFLICT_SIZE_MULT if regime_conflict else 1.0)
        
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
    
    @staticmethod
    def calculate_size(
        engine: 'SignalEngine',
        ctx: EntryContext,
        score_result: ScoreResult,
        guard_result: GuardResult,
        stop_loss: float,
        gate_size_cap: float = 1.0,
    ) -> OrchestrationSizingResult:
        """
        ⚠️ LEGACY SIZING - DEPRECATED (Jan 27, 2026)
        
        Uses 13+ multipliers. Prefer calculate_size_simplified() for 5-mult system.
        This method is kept for comparison and rollback.
        
        Toggle in src/config/simplified_sizing.py:
        - USE_SIMPLIFIED_SIZING = True → uses calculate_size_simplified()
        - USE_SIMPLIFIED_SIZING = False → uses this method
        """
        logger.debug("📊 LEGACY SIZING PATH (13+ multipliers)")
        
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
        # 🔄 REFACTOR Feb 04: Use centralized SessionQuality.get_quality_size_mult
        from src.config.trading_config import SessionQuality
        session_quality = engine._current_trade_tags.get('session_quality', 'moderate')
        session_mult = SessionQuality.get_quality_size_mult(session_quality)
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
        
        # 🧠 FIX #20: OBSTINACY SOFT MODE size cap
        # When SOFT mode is active, cap the size to prevent overexposure
        obstinacy_soft_cap = engine._current_trade_tags.get('obstinacy_soft_size_cap', 1.0)
        if obstinacy_soft_cap < 1.0:
            logger.info(f"🧠 OBSTINACY SOFT MODE: Applying size cap {obstinacy_soft_cap:.0%}")
        result.multipliers['obstinacy_soft'] = obstinacy_soft_cap
        
        # =====================================================================
        # 🔥 FIX #77-80 (Feb 07, 2026): GUARD PENALTY MULTIPLIERS
        # These are the graduated penalties from the refactored guards.
        # Each guard can now set a size_mult instead of hard blocking.
        # =====================================================================
        
        # FIX #77: Direction penalties (counter-momentum, forbidden, expansion)
        direction_size_mult = engine._current_trade_tags.get('direction_size_mult', 1.0)
        if direction_size_mult < 1.0:
            logger.info(f"⚠️ FIX #77 DIRECTION SIZE: Applying {direction_size_mult:.0%}")
        result.multipliers['direction_penalty'] = direction_size_mult
        
        # FIX #78: Displacement chase penalty
        chase_size_mult = engine._current_trade_tags.get('chase_size_mult', 1.0)
        if chase_size_mult < 1.0:
            logger.info(f"⚠️ FIX #78 CHASE SIZE: Applying {chase_size_mult:.0%}")
        result.multipliers['chase_penalty'] = chase_size_mult
        
        # FIX #79: Fakeout penalty
        fakeout_size_mult = engine._current_trade_tags.get('fakeout_size_mult', 1.0)
        if fakeout_size_mult < 1.0:
            logger.info(f"⚠️ FIX #79 FAKEOUT SIZE: Applying {fakeout_size_mult:.0%}")
        result.multipliers['fakeout_penalty'] = fakeout_size_mult
        
        # 🔥 FIX #133 (Feb 09, 2026): Kill check disabled - was causing double-counting
        # Individual guards already applied size penalties
        # No additional kill_check_size_cap needed
        
        # =====================================================================
        
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
                logger.warning("🚫 MAKER-ONLY ENFORCED: ignoring use_market=True")
                # Fall through to limit order
                logger.info(f"📊 Placing LIMIT post-only | {ctx.bias} {size:.6f} @ {ctx.current_price:.2f}")
                order_result = await engine.execution.place_limit_order(
                    symbol=engine.symbol,
                    side='buy' if ctx.bias == 'LONG' else 'sell',
                    amount=size,
                    price=ctx.current_price,
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
                
                # 🔥 FIX #65 (Feb 06, 2026): FALLBACK TO IOC IF POST-ONLY REJECTED
                # When price is moving fast (bias_flip, momentum), post-only fails
                # Instead of 5min cooldown, try IOC to catch the move
                if order_result is None:
                    bias_flip = engine._current_trade_tags.get('bias_invalidated', False)
                    bias_inverted = engine._current_trade_tags.get('bias_inverted', False)
                    
                    if bias_flip or bias_inverted:
                        logger.warning(f"🔄 FIX #65 POST-ONLY FALLBACK: Retrying with IOC (bias_flip={bias_flip}, bias_inverted={bias_inverted})")
                        order_result = await engine.execution.place_aggressive_order(
                            symbol=engine.symbol,
                            side='buy' if ctx.bias == 'LONG' else 'sell',
                            amount=size,
                            price=ctx.current_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit
                        )
                        if order_result:
                            logger.info(f"✅ FIX #65 IOC FALLBACK SUCCESS: Order filled as taker")
            
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
    
    # =====================================================================
    # CHECK 2.5d: ML INFERENCE FOR FAST PATH (Feb 10, 2026)
    # =====================================================================
    # Call ML inference in FAST PATH too for consistent data collection
    try:
        if hasattr(engine, '_ml_inference') and engine._ml_inference is not None:
            feature_snapshot = engine._build_ml_feature_snapshot() if hasattr(engine, '_build_ml_feature_snapshot') else {}
            ml_adjusted_score, ml_prob, ml_reason = engine._ml_inference.get_entry_adjustment(
                rule_score=int(fast_path_score),
                feature_snapshot=feature_snapshot
            )
            engine._current_trade_tags['ml_win_prob'] = round(ml_prob, 4)
            engine._current_trade_tags['ml_adjusted_score'] = ml_adjusted_score
            engine._current_trade_tags['ml_reason'] = ml_reason
            engine._current_trade_tags['ml_phase'] = engine._ml_inference.phase.name
            
            # In HYBRID/PRIMARY, adjust the score
            if engine._ml_inference.affects_decisions:
                fast_path_score = float(ml_adjusted_score)
                engine._entry_score = fast_path_score
                logger.info(f"🤖 FAST PATH ML: {engine._current_trade_tags.get('entry_score', 0):.1f}→{fast_path_score:.1f} | {ml_reason}")
    except Exception as ml_err:
        logger.debug(f"FAST PATH ML skipped: {ml_err}")
        engine._current_trade_tags['ml_win_prob'] = 0.5
    
    # =====================================================================
    # CHECK 2.5e: SCORE MÍNIMO PARA FAST PATH (Feb 03, 2026)
    # =====================================================================
    # 🔥 FIX #129 (Feb 09, 2026): Use centralized constant from unified_constants
    # Análisis de 49 trades FAST PATH: expectancy -0.012R, 35% TIME_INVALIDATION
    # Problema: Entraba con scores 5-6 en setups mediocres
    # Solución: Score >= 7 requiere buena estructura+timing+momentum
    from src.config.unified_constants import SCORES
    FAST_PATH_MIN_SCORE = SCORES.FAST_PATH_MIN_SCORE
    if fast_path_score < FAST_PATH_MIN_SCORE:
        logger.warning(
            f"🛑 FAST PATH BLOCKED: Score {fast_path_score:.1f} < {FAST_PATH_MIN_SCORE} minimum (conservative) | "
            f"direction={disp_direction} | breakdown={engine._score_breakdown}"
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
                logger.debug(f"Entry notification error: {e}")
        
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
    
    # =========================================================================
    # 🔥 FIX #77c (Feb 07, 2026): PARALLEL DIRECTION EVALUATION
    # =========================================================================
    # PROBLEMA: Sistema evalúa solo una dirección (la que dice HTFFilter).
    # Si HTF dice LONG pero el momentum real es SHORT, perdemos oportunidades.
    #
    # SOLUCIÓN: Siempre calcular p_long y p_short (probabilidad/score) en paralelo.
    # Si la dirección opuesta tiene score > original + threshold, considerar flip.
    #
    # CONDICIONES PARA FLIP:
    # 1. Score opuesto > score original + 2.0 (clara ventaja)
    # 2. +DI/-DI confirma la dirección opuesta
    # 3. No hay edge activo en dirección original (no contradecir edge manager)
    # =========================================================================
    from src.shared.indicators import calculate_adx_with_di
    
    parallel_eval_enabled = True  # Flag para poder desactivar si causa problemas
    alternative_direction = 'SHORT' if bias == 'LONG' else 'LONG'
    should_flip_direction = False
    flip_reason = ""
    
    if parallel_eval_enabled:
        # Get +DI/-DI for directional decision
        candles_1h_check = engine.market_data.get_candles('1h')
        if candles_1h_check and len(candles_1h_check) >= 30:
            df_1h_check = pd.DataFrame(candles_1h_check)
            try:
                _, _, plus_di, minus_di = calculate_adx_with_di(
                    df_1h_check['high'].to_numpy(),
                    df_1h_check['low'].to_numpy(),
                    df_1h_check['close'].to_numpy(),
                    14
                )
                plus_di_last = float(plus_di[-1]) if len(plus_di) > 0 and not np.isnan(plus_di[-1]) else 20.0
                minus_di_last = float(minus_di[-1]) if len(minus_di) > 0 and not np.isnan(minus_di[-1]) else 20.0
                
                # Check if DI contradicts the bias
                DI_CONTRADICTION_THRESHOLD = 8.0  # Require 8+ point difference to flip
                
                di_says_long = plus_di_last > minus_di_last + DI_CONTRADICTION_THRESHOLD
                di_says_short = minus_di_last > plus_di_last + DI_CONTRADICTION_THRESHOLD
                
                if bias == 'LONG' and di_says_short:
                    # HTF says LONG but DI says SHORT - consider flip
                    # Check if there's an active edge in LONG direction (don't contradict)
                    edge_mgr = getattr(engine, '_edge_manager', None)
                    has_long_edge = (edge_mgr and edge_mgr.current_bos and 
                                    edge_mgr.current_bos.direction == 'LONG')
                    
                    if not has_long_edge:
                        should_flip_direction = True
                        flip_reason = f"DI FLIP: -DI={minus_di_last:.1f} >> +DI={plus_di_last:.1f} (diff={minus_di_last-plus_di_last:.1f})"
                        logger.info(f"🔄 FIX #77c PARALLEL: {bias}→{alternative_direction} | {flip_reason}")
                
                elif bias == 'SHORT' and di_says_long:
                    # HTF says SHORT but DI says LONG - consider flip
                    edge_mgr = getattr(engine, '_edge_manager', None)
                    has_short_edge = (edge_mgr and edge_mgr.current_bos and 
                                     edge_mgr.current_bos.direction == 'SHORT')
                    
                    if not has_short_edge:
                        should_flip_direction = True
                        flip_reason = f"DI FLIP: +DI={plus_di_last:.1f} >> -DI={minus_di_last:.1f} (diff={plus_di_last-minus_di_last:.1f})"
                        logger.info(f"🔄 FIX #77c PARALLEL: {bias}→{alternative_direction} | {flip_reason}")
            
            except Exception as e:
                logger.debug(f"Parallel eval DI calculation error: {e}")
    
    # Apply flip if warranted
    if should_flip_direction:
        original_bias = bias
        bias = alternative_direction
        engine._current_trade_tags['parallel_eval_flip'] = True
        engine._current_trade_tags['parallel_eval_original_bias'] = original_bias
        engine._current_trade_tags['parallel_eval_reason'] = flip_reason
    
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
    
    # 🔥 FIX #77-80 (Feb 07, 2026): PROPAGATE GUARD PENALTIES TO TRADE TAGS
    # Guards now set penalties instead of hard blocking. These need to be
    # visible in engine._current_trade_tags for scoring and sizing.
    penalty_tags = [
        'direction_size_mult', 'direction_score_penalty',
        'chase_size_mult', 'chase_score_penalty', 
        'fakeout_size_mult', 'fakeout_score_penalty', 'fakeout_tight_sl',
        'kill_check_size_mult', 'kill_check_score_penalty',
        'retracement_pct', 'displacement_ratio',
    ]
    for tag in penalty_tags:
        if tag in guard_result.tags:
            engine._current_trade_tags[tag] = guard_result.tags[tag]
    
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
    
    # =========================================================================
    # 🔥 FIX #108 (Feb 09, 2026): DOUBLE CONFLICT BLOCK
    # =========================================================================
    # PROBLEMA: Trade entró con:
    #   - regime_is_fade = True (LONG vs macro=BEAR)  
    #   - btc_correlation = -1.0 penalty (BTC trend contra)
    #   - VOLUME DIVERGENCE -30%
    #
    # Todas estas son señales de que el trade es CONTRA el mercado.
    # Individualmente pueden ser ignoradas, pero COMBINADAS indican alto riesgo.
    #
    # SOLUCIÓN: Si hay FADE + BTC CORRELATION penalty, bloquear SALVO score >= 7
    # Score >= 7 indica setup muy fuerte que puede superar la oposición.
    # =========================================================================
    regime_is_fade = engine._current_trade_tags.get('regime_is_fade', False)
    
    # Check for BTC correlation penalty in score breakdown
    btc_corr_penalty = 0.0
    if hasattr(score_result, 'breakdown') and score_result.breakdown:
        penalty_hierarchy = score_result.breakdown.get('penalty_hierarchy', {})
        context_penalties = penalty_hierarchy.get('context_penalties', {})
        btc_corr_penalty = context_penalties.get('btc_correlation', 0.0)
    
    # Double conflict: FADE + BTC penalty (only if both are significant)
    double_conflict = regime_is_fade and btc_corr_penalty <= -0.5  # At least -0.5 penalty
    
    if double_conflict and score_result.score < 7.0:
        result.reason = (
            f"⛔ DOUBLE CONFLICT: FADE + BTC penalty ({btc_corr_penalty:+.1f}) | "
            f"Score={score_result.score:.1f} < 7.0 required"
        )
        logger.warning(
            f"🔥 FIX #108 BLOCK: {result.reason} | "
            f"regime_is_fade={regime_is_fade}, btc_penalty={btc_corr_penalty:+.1f}"
        )
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=result.reason,
            block_category='double_conflict',
            score=score_result.score,
            atr=engine._last_momentum_atr
        )
        engine._current_trade_tags['double_conflict_block'] = True
        engine._current_trade_tags['double_conflict_fade'] = regime_is_fade
        engine._current_trade_tags['double_conflict_btc'] = btc_corr_penalty
        return result

    # Session limit check (with override for high scores)
    # 🔥 FIX #10: Handle Hot Hand stopped (loss occurred)
    # 🔥 FIX #82: Improved message clarity
    if guard_result.tags.get('session_stopped', False):
        session_pnl = guard_result.tags.get('session_pnl_r', 0.0)
        result.reason = (
            f"Session STOPPED (last trade was LOSS, {guard_result.tags['session_trades']} trades done, "
            f"session P&L: {session_pnl:+.2f}R)"
        )
        engine.missed_tracker.log_blocked_signal(
            direction=bias,
            price=current_price,
            block_reason=result.reason,
            block_category='session_limit',
            score=score_result.score,
            atr=engine._last_momentum_atr
        )
        return result
    
    if guard_result.tags.get('session_at_limit', False):
        hot_hand_bonus = guard_result.tags.get('hot_hand_bonus', 0)
        if score_result.score < SessionTradeLimits.OVERRIDE_IF_SCORE_GTE:
            if hot_hand_bonus > 0:
                result.reason = f"Session limit ({guard_result.tags['session_trades']}/{guard_result.tags['session_limit']}) [+{hot_hand_bonus} hot hand]"
            else:
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
    
    # ========== PHASE 4.5: ML SCORE ADJUSTMENT (Feb 10, 2026) ==========
    # Call ML inference engine to get score adjustment.
    # In SHADOW mode: logs prediction without affecting final_score
    # In HYBRID mode: adjusts score ±1 based on ML confidence
    # In PRIMARY mode: ML drives the score directly
    try:
        if hasattr(engine, '_ml_inference') and engine._ml_inference is not None:
            # Build feature snapshot for ML
            feature_snapshot = engine._build_ml_feature_snapshot() if hasattr(engine, '_build_ml_feature_snapshot') else {}
            
            # Get ML adjustment
            ml_adjusted_score, ml_prob, ml_reason = engine._ml_inference.get_entry_adjustment(
                rule_score=int(final_score),
                feature_snapshot=feature_snapshot
            )
            
            # Store ML results in trade tags
            engine._current_trade_tags['ml_win_prob'] = round(ml_prob, 4)
            engine._current_trade_tags['ml_adjusted_score'] = ml_adjusted_score
            engine._current_trade_tags['ml_reason'] = ml_reason
            engine._current_trade_tags['ml_phase'] = engine._ml_inference.phase.name
            
            # Only apply adjustment if ML affects decisions (HYBRID or PRIMARY)
            if engine._ml_inference.affects_decisions:
                final_score = float(ml_adjusted_score)
                logger.info(f"🤖 ML ADJUSTMENT: {score_result.score:.1f}→{final_score:.1f} | {ml_reason}")
            # In SHADOW mode, just log for data collection
            elif engine._ml_inference.phase.name == 'SHADOW' and ml_prob != 0.5:
                logger.debug(f"🤖 ML SHADOW: P(win)={ml_prob:.2f} [score={int(final_score)}→{ml_adjusted_score}]")
    except Exception as ml_err:
        logger.debug(f"ML inference skipped: {ml_err}")
        engine._current_trade_tags['ml_win_prob'] = 0.5
        engine._current_trade_tags['ml_reason'] = 'ML_ERROR'
    
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
    from src.config.simplified_sizing import SimplifiedFeatureFlags, SimplifiedExitConfig
    
    if SimplifiedFeatureFlags.USE_FIXED_TP:
        # Get edge type from current trade tags
        current_edge_type = engine._current_trade_tags.get('edge_type', None)
        
        # Simplified: 1.5R in trend, 1.0R in range, 2.0R in breakout
        tp_mult = SimplifiedExitConfig.get_tp_r(
            regime_str=engine._current_regime.value if engine._current_regime else "NORMAL",
            adx=ctx.adx_1h if hasattr(ctx, 'adx_1h') else None,
            edge_type=current_edge_type
        )
        logger.info(f"📊 SIMPLIFIED TP: {tp_mult}R (edge={current_edge_type})")
    else:
        # Legacy: Dynamic TP from daily regime config
        daily_tp = engine._daily_regime_config.get('tp_r_target', AsymmetryConfig.BASE_TP_R) if hasattr(engine, '_daily_regime_config') and engine._daily_regime_config else RiskManagement.TP1_RISK_MULTIPLE
        tp_mult = daily_tp
    
    if bias == 'LONG':
        take_profit = current_price + (risk * tp_mult)
    else:
        take_profit = current_price - (risk * tp_mult)
    
    # =========================================================================
    # 🔥 NEW (Feb 04, 2026): TP CAPPED BY HISTORICAL RANGE (ANCHOR LEVELS)
    # =========================================================================
    # PROBLEM: System puts TP at 71k when price is already at 60-day lows (73k).
    #          This is IMPOSSIBLE to reach - TP becomes meaningless.
    #
    # SOLUTION: Cap TP to historical range with a small buffer:
    # - SHORT TP cannot be below (range_low_60d - 0.5% buffer)
    # - LONG TP cannot be above (range_high_60d + 0.5% buffer)
    #
    # EXCEPTION: If we're in BREAKOUT mode (breaking out of range), allow more room
    # =========================================================================
    # 🔥 FIX (Feb 05, 2026): DISABLE TP CAPPING ENTIRELY DURING CONFIRMED BREAKOUT
    # =========================================================================
    # PROBLEM: On 2026-02-05, the system detected BREAKOUT correctly (score=4)
    #          but still capped TP to stale range_low_60d, resulting in NEGATIVE R:R
    #          (R:R = -0.13R, -0.19R, -0.20R) which blocked 30+ valid entries.
    #
    # ROOT CAUSE: The buffer_pct (2% vs 0.5%) is not enough when price has already
    #             moved significantly beyond the range. The capped TP ends up
    #             ABOVE the entry price for shorts.
    #
    # SOLUTION: During CONFIRMED breakout (breakout_score >= 3), do NOT cap TP at all.
    #           The breakout IS the edge - let the TP extend into new territory.
    # =========================================================================
    location_ctx = getattr(engine, '_location_context', None)
    if location_ctx and risk > 0:
        is_breakout = location_ctx.is_breakout_up or location_ctx.is_breakout_down
        
        # 🔥 FIX: Skip TP capping entirely during confirmed breakout
        if is_breakout:
            # Get breakout score for logging
            breakout_score = location_ctx.breakout_up_score if location_ctx.is_breakout_up else location_ctx.breakout_down_score
            logger.info(
                f"🚀 BREAKOUT TP BYPASS: Confirmed breakout (score={breakout_score}) | "
                f"TP NOT capped - allowing extension beyond historical range | "
                f"TP={take_profit:.0f}, entry={current_price:.0f}, "
                f"is_breakout_up={location_ctx.is_breakout_up}, is_breakout_down={location_ctx.is_breakout_down}"
            )
            # Skip all TP capping logic - don't modify take_profit
        else:
            # Normal mode: Apply TP capping to historical range
            buffer_pct = 0.005  # 0.5% buffer
            
            if bias == 'SHORT':
                # For SHORT, TP is below current price
                # Cap at historical low minus small buffer
                historical_floor = location_ctx.range_low_60d * (1 - buffer_pct)
                
                if take_profit < historical_floor:
                    original_tp = take_profit
                    take_profit = historical_floor
                    # Recalculate actual R:R we'll get
                    actual_r = (current_price - take_profit) / risk if risk > 0 else 0
                    logger.warning(
                        f"⚠️ TP CAPPED BY RANGE: SHORT TP {original_tp:.0f} → {take_profit:.0f} | "
                        f"60d-low={location_ctx.range_low_60d:.0f} | "
                        f"Effective R:R = {actual_r:.2f}R (was {tp_mult:.2f}R) | "
                        f"pct_60d={location_ctx.percentile_60d:.1f}%"
                    )
                    engine._current_trade_tags['tp_capped_by_range'] = True
                    engine._current_trade_tags['tp_original'] = original_tp
                    engine._current_trade_tags['tp_capped_to'] = take_profit
                    engine._current_trade_tags['effective_tp_r'] = actual_r
                    
            elif bias == 'LONG':
                # For LONG, TP is above current price
                # Cap at historical high plus small buffer
                historical_ceiling = location_ctx.range_high_60d * (1 + buffer_pct)
                
                if take_profit > historical_ceiling:
                    original_tp = take_profit
                    take_profit = historical_ceiling
                    # Recalculate actual R:R we'll get
                    actual_r = (take_profit - current_price) / risk if risk > 0 else 0
                    logger.warning(
                        f"⚠️ TP CAPPED BY RANGE: LONG TP {original_tp:.0f} → {take_profit:.0f} | "
                        f"60d-high={location_ctx.range_high_60d:.0f} | "
                        f"Effective R:R = {actual_r:.2f}R (was {tp_mult:.2f}R) | "
                        f"pct_60d={location_ctx.percentile_60d:.1f}%"
                    )
                    engine._current_trade_tags['tp_capped_by_range'] = True
                    engine._current_trade_tags['tp_original'] = original_tp
                    engine._current_trade_tags['tp_capped_to'] = take_profit
                    engine._current_trade_tags['effective_tp_r'] = actual_r
    
    # =========================================================================
    # 🔥 NEW (Feb 04, 2026): BLOCK if TP cap results in R:R too low
    # =========================================================================
    # If we had to cap the TP and the effective R:R is now < 0.5R, 
    # the trade is not worth taking - no edge at historical extremes.
    MIN_EFFECTIVE_RR_WHEN_CAPPED = 0.5
    if engine._current_trade_tags.get('tp_capped_by_range', False) and location_ctx is not None:
        effective_rr = engine._current_trade_tags.get('effective_tp_r', 0)
        if effective_rr < MIN_EFFECTIVE_RR_WHEN_CAPPED:
            result.reason = (
                f"🛑 HARD BLOCK: TP capped to historical range but effective R:R={effective_rr:.2f}R < "
                f"{MIN_EFFECTIVE_RR_WHEN_CAPPED}R minimum | Price at {location_ctx.percentile_60d:.0f}% of 60d range | "
                f"No edge available at this location"
            )
            logger.warning(result.reason)
            # 🔥 FIX Feb 07: Use log_blocked_with_setup for REAL simulation
            log_blocked_with_setup(
                engine=engine,
                direction=bias,
                price=current_price,
                block_reason=result.reason,
                block_category='tp_range_cap_low_rr',
                score=final_score,
                atr=engine._last_momentum_atr,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            return result

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
        # 🔥 FIX Feb 07: Use log_blocked_with_setup for REAL simulation
        log_blocked_with_setup(
            engine=engine,
            direction=bias,
            price=current_price,
            block_reason=hard_block.block_reason,
            block_category=hard_block.block_type,
            score=final_score,
            atr=engine._last_momentum_atr,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        return result
    
    # 🔥 NEW (Jan 28): Track PROVISIONAL state from hard_block
    is_provisional_from_timing = hard_block.is_provisional
    provisional_reason = hard_block.provisional_reason
    if is_provisional_from_timing:
        engine._current_trade_tags['provisional_from_timing'] = True
        engine._current_trade_tags['provisional_reason'] = provisional_reason
        logger.info(f"⚠️ Entry marked PROVISIONAL from validator: {provisional_reason}")
    
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
    # 🔥 SIMPLIFIED MODE (Jan 27, 2026): Use 5-multiplier system if enabled
    from src.config.simplified_sizing import SimplifiedFeatureFlags
    
    if SimplifiedFeatureFlags.USE_SIMPLIFIED_SIZING:
        sizing_result = SizingOrchestrator.calculate_size_simplified(
            engine, ctx,
            stop_loss=stop_loss,
            worst_case_rr=rr_result.worst_case_rr,
        )
        logger.info(f"📊 SIMPLIFIED SIZING: {sizing_result.multipliers}")
    else:
        sizing_result = SizingOrchestrator.calculate_size(
            engine, ctx, score_result, guard_result,
            stop_loss=stop_loss,
            gate_size_cap=gate_result.max_size_multiplier,
        )
    
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
        logger.warning(
            f"🚫 HARD BLOCK: net_R={cost_result.expected_net_r:.2f} < {MIN_EXPECTED_NET_R:.2f} "
            f"| fees_R={cost_result.fee_r_estimate:.2f} | profit=${cost_result.expected_profit_usd_net:.2f}"
        )
        result.reason = (
            f"HARD BLOCK: expected_net_R={cost_result.expected_net_r:.2f} < {MIN_EXPECTED_NET_R:.2f}"
        )
        # 🔥 FIX Feb 07: Use log_blocked_with_setup for REAL simulation
        log_blocked_with_setup(
            engine=engine,
            direction=bias,
            price=current_price,
            block_reason=f"net_R={cost_result.expected_net_r:.2f} < {MIN_EXPECTED_NET_R}",
            block_category='profitability',
            score=final_score,
            atr=engine._last_momentum_atr,
            stop_loss=stop_loss,
            take_profit=take_profit
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
    # 🔥 FIX #118 (Feb 09, 2026): Escalar con capital para cuentas pequeñas
    # - Cuentas pequeñas (<$500): 0.20% del capital (~$0.50 para $250)
    # - Cuentas grandes (>$500): 0.10% del capital
    # - Floor absoluto: $0.50 mínimo para que tenga sentido el trade
    if capital < 500:
        MIN_PROFIT_PERCENT = 0.0020  # 0.20% for small accounts
    else:
        MIN_PROFIT_PERCENT = 0.0010  # 0.10% for larger accounts
    MIN_PROFIT_FLOOR = 0.50     # $0.50 minimum - si no puedes ganar esto, no vale la pena
    
    min_expected_profit = max(capital * MIN_PROFIT_PERCENT, MIN_PROFIT_FLOOR)
    
    # 🔥 FIX #102d: NO HAY BYPASS - profit mínimo es sagrado
    # Si no puedes ganar $0.50 neto, no deberías estar en el trade
    if cost_result.expected_profit_usd_net < min_expected_profit:
        rr_ratio = cost_result.expected_profit_usd_net / max(sizing_result.risk_usd, 0.01) if hasattr(sizing_result, 'risk_usd') else 0.0
        logger.warning(
            f"🚫 FIX #102d BLOCK: profit_net=${cost_result.expected_profit_usd_net:.2f} < ${min_expected_profit:.2f} | "
            f"RR={rr_ratio:.2f} (no bypass allowed) | size={sizing_result.size:.6f}"
        )
        result.reason = (
            f"FIX #102d BLOCK: expected_profit=${cost_result.expected_profit_usd_net:.2f} < ${min_expected_profit:.2f} "
            f"(minimum viable profit)"
        )
        # 🔥 FIX Feb 07: Use log_blocked_with_setup for REAL simulation
        log_blocked_with_setup(
            engine=engine,
            direction=bias,
            price=current_price,
            block_reason=f"profit_net=${cost_result.expected_profit_usd_net:.2f} < ${min_expected_profit:.2f}",
            block_category='profitability',
            score=final_score,
            atr=engine._last_momentum_atr,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        return result
    else:
        rr_ratio = cost_result.expected_profit_usd_net / max(sizing_result.risk_usd, 0.01) if hasattr(sizing_result, 'risk_usd') else 1.0
        logger.info(
            f"✅ Profit check PASS: ${cost_result.expected_profit_usd_net:.2f} >= ${min_expected_profit:.2f} | "
            f"RR={rr_ratio:.2f}"
        )
    
    # =====================================================================
    # ✅ PASO 5 — PROBE-SPECIFIC VALIDATION (Jan 28, 2026)
    # Si es PROBE, aplicar reglas más estrictas de rentabilidad mínima
    # =====================================================================
    from src.application.live.entry_validators import ProbeValidator
    
    is_probe = bool(engine._current_trade_tags.get('probing_mode', False))
    spread_bps = getattr(engine, '_last_spread_bps', 1.0)
    
    probe_result = ProbeValidator.validate_probe(
        is_probe=is_probe,
        position_size=sizing_result.size,
        current_price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        bias=bias,
        atr_1m=atr_1m,
        spread_bps=spread_bps,
    )
    
    if not probe_result.is_valid:
        result.reason = probe_result.block_reason
        # 🔥 FIX Feb 07: Use log_blocked_with_setup for REAL simulation
        log_blocked_with_setup(
            engine=engine,
            direction=bias,
            price=current_price,
            block_reason=probe_result.block_reason,
            block_category='probe_filter',
            score=final_score,
            atr=engine._last_momentum_atr,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
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
        'order_type': 'LIMIT',
        'tags': engine._current_trade_tags.copy(),
        'score': final_score,
        'score_breakdown': score_result.breakdown.copy(),
    }
    
    # 🔥 FIX #146 (Feb 10, 2026): AGGRESSIVE ENTRY FOR TREND_BIRTH
    # PROBLEM: POST-ONLY LIMIT orders fail when market moves in your favor
    #   - Example: SHORT signal, price drops, SELL LIMIT never fills (needs price to rise)
    #   - System stuck in WAIT_FILL for 30s+ with timeouts
    # SOLUTION: Use IOC (taker order) for TREND_BIRTH - these are fast-moving entries
    aggressive_execution = False
    
    # Check 1: TREND_BIRTH phase → market is moving, post-only will miss
    ti_phase = engine._current_trade_tags.get('ti_phase', '')
    if ti_phase == 'TREND_BIRTH':
        aggressive_execution = True
        logger.warning(f"🚀 FIX #146 TREND_BIRTH AGGRESSIVE: Using IOC (taker) for {bias}")
    
    # Check 2: DISPLACEMENT DIRECT ENTRY (original logic)
    if not aggressive_execution:
        edge_mgr = getattr(engine, '_edge_manager', None)
        if edge_mgr is not None:
            from src.domain.trading.edge_state_manager import EdgeState, EdgeType
            is_displacement_edge = (
                edge_mgr.state == EdgeState.EDGE_ACTIVE and 
                edge_mgr.current_bos is not None and
                edge_mgr.current_bos.edge_type == EdgeType.DISPLACEMENT
            )
            # Check if still in DIRECT ENTRY WINDOW (0-2 bars)
            if is_displacement_edge:
                bars_since_disp = edge_mgr.current_bos.bars_since_displacement
                if bars_since_disp is not None and bars_since_disp <= 2:
                    aggressive_execution = True
                    logger.warning(
                        f"🚀 DISPLACEMENT DIRECT ENTRY: bar {bars_since_disp}/2 → AGGRESSIVE EXECUTION"
                    )
    
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
            logger.debug(f"Entry notification error: {e}")


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
