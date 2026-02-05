"""
Entry Validators Module - Consolidated validation logic for trade entry.

This module extracts chase penalty calculation, graduated sizing, and cost validation
from the monolithic trading_app.py into focused, testable classes.

Extracted from _execute_entry() as part of AUDIT REFACTOR Point 5.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING
import logging
import numpy as np

from src.config.trading_config import (
    EntryGateConfig,
    OrderExecution,
    TrendExpansionConfig,
    OperatingMode,
)

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

logger = logging.getLogger(__name__)


# ==============================================================================
# DATA CLASSES
# ==============================================================================

@dataclass
class ChaseResult:
    """Result of chase penalty calculation."""
    chase_atr: float = 0.0
    chase_penalty: float = 0.0
    should_block: bool = False
    block_reason: str = ""
    adjusted_score: float = 0.0
    momentum_context_positive: bool = False
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DefensiveSizingResult:
    """Result of graduated sizing calculation (defensive multipliers)."""
    score_size_multiplier: float = 1.0
    rr_size_multiplier: float = 1.0
    ltf_size_multiplier: float = 1.0
    combined_defensive: float = 1.0
    entry_type: str = "full"  # probe, reduced, full
    is_blocked: bool = False
    block_reason: str = ""
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CostValidationResult:
    """Result of cost validation."""
    is_valid: bool = True
    cost_usd: float = 0.0
    expected_profit_usd: float = 0.0
    expected_profit_usd_net: float = 0.0
    expected_net_r: float = 0.0
    fee_r_estimate: float = 0.0
    cost_ratio: float = 0.0
    threshold: float = 0.0
    block_reason: str = ""
    cost_validation_failed: bool = False  # 🔥 Flag when validation errors occur


@dataclass
class RRValidationResult:
    """Result of Risk/Reward validation."""
    is_valid: bool = True
    ideal_rr: float = 0.0
    worst_case_rr: float = 0.0
    effective_floor: float = 0.0
    rr_size_multiplier: float = 1.0
    high_score_bypass: bool = False
    block_reason: str = ""
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeValidationResult:
    """Result of PROBE-specific validation."""
    is_valid: bool = True
    block_reason: str = ""
    expected_r: float = 0.0
    expected_net_profit: float = 0.0
    tp_r: float = 0.0
    fees_usd: float = 0.0


# ==============================================================================
# CHASE VALIDATOR
# ==============================================================================

class ChaseValidator:
    """
    Validates and penalizes chase distance (entering after price has moved).
    
    Chase = entering after a move has already started.
    Higher chase = higher reversion probability.
    
    Extracted from _execute_entry lines ~4170-4300.
    """
    
    def __init__(self, regime_detector, entry_threshold: float = 4.0):
        self.regime_detector = regime_detector
        self.entry_threshold = entry_threshold
    
    def calculate_chase_penalty(
        self,
        df_1m,
        atr_1m: float,
        bias: str,
        current_regime,
        score: float,
        trade_regime: str,
        has_bos: bool,
        adx_values,
        atr_5m: float,
        atr_avg: float,
        trend_expansion_active: bool,
        anti_paralysis_level: int,
        df_5m=None,
    ) -> ChaseResult:
        """
        Calculate chase penalty and determine if entry should be blocked.
        
        Returns ChaseResult with adjusted score and penalty details.
        """
        result = ChaseResult(adjusted_score=score)
        
        if not OrderExecution.ANTI_CHASING_ENABLED:
            return result
        
        # Calculate chase ATR
        result.chase_atr = self._calculate_chase_atr(df_1m, atr_1m, bias)
        result.tags['chase_atr'] = result.chase_atr
        
        # Check if regime says to block entirely (only CHAOS or extreme chase)
        should_block, block_reason = self.regime_detector.should_block_trade(
            result.chase_atr, current_regime
        )
        
        if should_block:
            result.should_block = True
            result.block_reason = block_reason
            return result
        
        # Calculate regime-aware chase penalty
        result.chase_penalty = self.regime_detector.get_chase_penalty(
            result.chase_atr, current_regime
        )
        
        # Check momentum context (if positive, may reduce penalty)
        adx_rising = len(adx_values) >= 3 and adx_values[-1] > adx_values[-3]
        candle_expansion = bool(atr_5m > atr_avg * 1.1)
        
        # Check VWAP reclaim
        vwap_reclaim = self._check_vwap_reclaim(df_5m, bias)
        
        momentum_context = self.regime_detector.get_momentum_context(
            has_bos=has_bos,
            adx_rising=adx_rising,
            candle_expansion=candle_expansion,
            vwap_reclaim=vwap_reclaim
        )
        result.momentum_context_positive = momentum_context
        
        # Apply momentum context reduction (REGIME-DEPENDENT)
        is_trend_regime = trade_regime in ['TREND', 'TREND_EXPANSION', 'BREAKOUT']
        
        if momentum_context and result.chase_penalty > 0 and is_trend_regime:
            result.chase_penalty *= 0.5
            result.tags['momentum_context_reduced'] = True
            logger.info("📊 Momentum context positive + TREND regime → chase penalty halved")
        elif momentum_context and result.chase_penalty > 0:
            result.tags['momentum_context_ignored'] = True
            logger.info("📊 Momentum context positive but RANGE regime → chase penalty NOT reduced")
        
        # TREND EXPANSION: Further reduce chase penalty
        if trend_expansion_active and result.chase_penalty > 0:
            result.chase_penalty *= TrendExpansionConfig.CHASE_PENALTY_MULT
            result.tags['expansion_chase_reduction'] = True
            logger.info(f"🚀 Trend expansion → chase penalty reduced to {TrendExpansionConfig.CHASE_PENALTY_MULT:.0%}")
        
        # NOTE: Anti-paralysis removed (Jan 27, 2026) - globally disabled
        
        # Apply penalty to score
        if result.chase_penalty > 0:
            result.adjusted_score = score - result.chase_penalty
            result.tags['chase_penalty'] = result.chase_penalty
            
            logger.info(
                f"📊 Chase penalty: {result.chase_atr:.2f} ATR → "
                f"-{result.chase_penalty:.1f} pts → adjusted score {result.adjusted_score:.1f}"
            )
            
            # Re-check threshold after penalty
            if result.adjusted_score < self.entry_threshold:
                result.should_block = True
                result.block_reason = f"Score after chase penalty ({result.adjusted_score:.1f}) < threshold ({self.entry_threshold})"
                result.tags['near_miss'] = True
                result.tags['near_miss_reason'] = 'chase_penalty'
        
        return result
    
    def _calculate_chase_atr(self, df_1m, atr_1m: float, bias: str) -> float:
        """Calculate how many ATRs price has moved since signal start."""
        if df_1m is None or len(df_1m) < 5:
            return 0.0
        
        try:
            closes = df_1m['close'].to_numpy()[-5:]
            if bias == 'LONG':
                # For longs, chase = how much price has risen
                chase_distance = closes[-1] - min(closes)
            else:
                # For shorts, chase = how much price has fallen
                chase_distance = max(closes) - closes[-1]
            
            return chase_distance / atr_1m if atr_1m > 0 else 0.0
        except Exception as e:
            logger.debug(f"Chase ATR calculation failed: {e}")
            return 0.0
    
    def _check_vwap_reclaim(self, df_5m, bias: str) -> bool:
        """Check if price has reclaimed VWAP (bullish for longs, bearish for shorts)."""
        if df_5m is None or len(df_5m) < 5:
            return False
        
        try:
            from src.shared.indicators import calculate_vwap
            vwap_series = calculate_vwap(df_5m)
            if len(vwap_series) < 5:
                return False
            
            closes_5m = df_5m['close'].to_numpy()
            
            if bias == 'LONG':
                was_below = any(closes_5m[i] < vwap_series.iloc[i] for i in range(-5, -1))
                now_above = closes_5m[-1] > vwap_series.iloc[-1]
                return was_below and now_above
            else:  # SHORT
                was_above = any(closes_5m[i] > vwap_series.iloc[i] for i in range(-5, -1))
                now_below = closes_5m[-1] < vwap_series.iloc[-1]
                return was_above and now_below
        except Exception as e:
            logger.debug(f"VWAP reclaim check failed: {e}")
            return False


# ==============================================================================
# GRADUATED SIZER
# ==============================================================================

class GraduatedSizer:
    """
    ⚠️ LEGACY SIZER - DEPRECATED (Jan 27, 2026)
    
    This class implements the old 16-multiplier sizing system.
    It is kept for comparison and rollback purposes.
    
    NEW CODE SHOULD USE: SimplifiedSizer from src.config.simplified_sizing
    
    Toggle: SimplifiedFeatureFlags.USE_SIMPLIFIED_SIZING = True → uses SimplifiedSizer
            SimplifiedFeatureFlags.USE_SIMPLIFIED_SIZING = False → uses this class
    
    Factors applied multiplicatively (LEGACY):
    - Score-based size multiplier
    - RR-based size multiplier  
    - LTF confirmation multiplier
    - Loss streak multiplier
    - Session quality multiplier
    - Day of week multiplier
    - Spread multiplier
    - Countertrend multiplier
    - Location cap
    - Gate size cap
    - Probing/Range multiplier
    - Daily regime multiplier
    
    Extracted from _execute_entry lines ~4630-4850.
    """
    
    def calculate_combined_defensive(
        self,
        score: float,
        worst_case_rr: float,
        current_regime_str: str,
        trade_tags: Dict[str, Any],
        market_time,
        consecutive_losses: int,
        spread_size_factor: float,
        gate_size_cap: float,
        black_swan_multiplier: float,
    ) -> DefensiveSizingResult:
        """
        Calculate combined defensive multiplier from all sizing factors.
        
        ⚠️ LEGACY: This uses 16 multipliers. Prefer SimplifiedSizer for 5-mult system.
        
        Returns DefensiveSizingResult with all multipliers and entry type.
        """
        result = DefensiveSizingResult()
        
        # SCORE-BASED SIZING
        result.score_size_multiplier = EntryGateConfig.get_score_size_multiplier(score)
        result.tags['score_size_multiplier'] = result.score_size_multiplier
        
        # Determine entry type
        if result.score_size_multiplier <= 0.3:
            result.entry_type = 'probe'
        elif result.score_size_multiplier < 1.0:
            result.entry_type = 'reduced'
        else:
            result.entry_type = 'full'
        
        # CHASE FORCED REDUCTION: Block full entry when chase > 1.5 ATR
        prelim_chase_atr = trade_tags.get('chase_atr', 0)
        if result.entry_type == 'full' and prelim_chase_atr > 1.5:
            logger.warning(f"⚠️ FULL → REDUCED: Chase {prelim_chase_atr:.2f} ATR > 1.5 ATR threshold")
            result.score_size_multiplier = min(result.score_size_multiplier, 0.5)
            result.entry_type = 'reduced'
            result.tags['chase_forced_reduction'] = True
        
        # NOTE (Jan 27, 2026): Removed PROBE BLOCK in poor session check.
        # HARD BLOCKS (Phase 5.5) already blocks session_quality='poor'.
        # This code would never execute anyway.
        
        # Start with black_swan and score
        result.combined_defensive = black_swan_multiplier * result.score_size_multiplier
        
        # RR-BASED SIZING
        result.rr_size_multiplier = EntryGateConfig.get_rr_size_multiplier(
            worst_case_rr, current_regime_str
        )
        result.combined_defensive *= result.rr_size_multiplier
        result.tags['rr_size_multiplier'] = result.rr_size_multiplier
        
        # LTF CONFIRMATION SIZING
        ltf_confirmations = trade_tags.get('ltf_confirmations', 2)
        result.ltf_size_multiplier = EntryGateConfig.get_ltf_size_multiplier(ltf_confirmations, total=3)
        result.combined_defensive *= result.ltf_size_multiplier
        result.tags['ltf_size_multiplier'] = result.ltf_size_multiplier
        
        # LOSS STREAK SIZING
        loss_streak_multiplier = OperatingMode.get_loss_sizing_multiplier(int(consecutive_losses))
        if consecutive_losses > 0:
            logger.info(f"📉 Loss streak sizing: {consecutive_losses} losses → {loss_streak_multiplier:.0%}")
            result.tags['loss_streak_size_factor'] = loss_streak_multiplier
        result.combined_defensive *= loss_streak_multiplier
        
        # SESSION QUALITY SIZING
        from src.config.trading_config import get_session_name
        session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
        session_multiplier = OperatingMode.get_session_multiplier(session_name)
        if session_multiplier < 1.0:
            logger.info(f"📊 Session sizing: {session_name} → {session_multiplier:.0%}")
        result.tags['session_quality'] = session_name
        result.tags['session_size_factor'] = session_multiplier
        result.combined_defensive *= session_multiplier
        
        # DAY OF WEEK SIZING
        day_multiplier = OperatingMode.get_day_multiplier(market_time.weekday())
        if day_multiplier < 1.0:
            day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            logger.info(f"📊 Day sizing: {day_names[market_time.weekday()]} → {day_multiplier:.0%}")
        result.tags['day_of_week'] = market_time.weekday()
        result.tags['day_size_factor'] = day_multiplier
        result.combined_defensive *= day_multiplier
        
        # SPREAD SIZE REDUCTION
        if spread_size_factor < 1.0:
            logger.info(f"📊 Spread sizing: elevated spread → {spread_size_factor:.0%}")
            result.tags['spread_size_factor'] = spread_size_factor
            result.combined_defensive *= spread_size_factor
        
        # COUNTERTREND SIZE REDUCTION
        ct_size_mult = trade_tags.get('ct_size_mult', 1.0)
        if ct_size_mult < 1.0:
            logger.info(f"📊 Countertrend sizing: → {ct_size_mult:.0%}")
            result.tags['ct_size_applied'] = ct_size_mult
            result.combined_defensive *= ct_size_mult
        
        # LOCATION SIZE CAP
        bias = trade_tags.get('direction', 'LONG')
        location_cap_key = 'location_size_cap_long' if bias == 'LONG' else 'location_size_cap_short'
        location_size_cap = trade_tags.get(location_cap_key, 1.0)
        if location_size_cap < 1.0:
            logger.info(f"📍 Location size cap: {location_size_cap:.0%}")
            result.tags['location_size_cap_applied'] = location_size_cap
            result.combined_defensive *= location_size_cap
        
        # ANTICIPATION PROBE SIZING
        anticipation_probe_size = trade_tags.get('anticipation_probe_size', 1.0)
        if anticipation_probe_size < 1.0:
            logger.info(f"🔬 Anticipation probe sizing: {anticipation_probe_size:.0%}")
            result.tags['anticipation_size_applied'] = anticipation_probe_size
            result.combined_defensive *= anticipation_probe_size
        
        # ENTRY QUALITY GATE SIZE CAP
        if gate_size_cap < 1.0:
            logger.info(f"🚧 Gate size cap: {gate_size_cap:.0%}")
            result.tags['gate_size_cap'] = gate_size_cap
            result.combined_defensive *= gate_size_cap
        
        # PROBING / RANGE SIZE MODIFIER
        range_size_mult = trade_tags.get('range_size_mult', 1.0)
        if range_size_mult < 1.0:
            probing_mode = trade_tags.get('probing_mode', False)
            if probing_mode:
                if range_size_mult >= 0.65:
                    level = "PROBE_MAX"
                elif range_size_mult >= 0.50:
                    level = "PROBE_MED"
                else:
                    level = "PROBE_MIN"
                logger.info(f"🔍 {level} sizing: {range_size_mult:.0%}")
            else:
                logger.info(f"📊 RANGE sizing: {range_size_mult:.0%}")
            result.tags['range_size_mult_applied'] = range_size_mult
            result.combined_defensive *= range_size_mult
        
        # 🔥 MOVE QUALITY SIZE MODIFIER (Jan 28, 2026)
        # If move is EXHAUSTING, block entry entirely
        # If move is DEVELOPING, reduce size
        move_quality = trade_tags.get('move_quality', 'UNKNOWN')
        move_quality_mult = trade_tags.get('move_quality_size_mult', 1.0)
        if move_quality == 'EXHAUSTING':
            # HARD BLOCK - do not enter exhausted moves
            logger.info(f"🚫 MOVE EXHAUSTING: Blocking entry | Wait for pullback")
            result.tags['move_quality_block'] = True
            result.combined_defensive = 0.0  # This will block the trade
        elif move_quality_mult < 1.0 and move_quality != 'UNKNOWN':
            logger.info(f"📉 MOVE QUALITY [{move_quality}]: size × {move_quality_mult:.0%}")
            result.tags['move_quality_applied'] = move_quality_mult
            result.combined_defensive *= move_quality_mult
        
        # DAILY REGIME SIZE MODIFIER
        daily_regime_config = trade_tags.get('_daily_regime_config', {})
        daily_regime_size_mult = daily_regime_config.get('size_mult', 1.0) if daily_regime_config else 1.0
        if daily_regime_size_mult < 1.0:
            logger.info(f"📅 Daily regime sizing: → {daily_regime_size_mult:.0%}")
            result.tags['daily_regime_size_mult'] = daily_regime_size_mult
            result.combined_defensive *= daily_regime_size_mult
        
        return result


# ==============================================================================
# RR VALIDATOR
# ==============================================================================

class RRValidator:
    """
    Validates Risk/Reward ratio and calculates worst-case RR.
    
    Extracted from _execute_entry lines ~4505-4600.
    """
    
    def __init__(self, slippage_model):
        self.slippage_model = slippage_model
    
    def validate_rr(
        self,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        bias: str,
        score: float,
        current_regime,
        regime_detector,
        volatility_ratio: float = 1.0,
    ) -> RRValidationResult:
        """
        Validate RR and calculate worst-case scenario.
        
        Returns RRValidationResult with validation status and sizing multiplier.
        """
        from src.infrastructure.exchanges.kraken.slippage_model import OrderType
        
        result = RRValidationResult()
        
        # Calculate ideal RR
        risk = abs(current_price - stop_loss)
        if risk <= 0:
            result.is_valid = False
            result.block_reason = "Zero risk distance"
            return result
        
        result.ideal_rr = abs(take_profit - current_price) / risk
        result.tags['rr_ideal'] = result.ideal_rr
        
        # Get regime floors
        current_regime_config = regime_detector.get_config(current_regime)
        regime_min_rr = current_regime_config.min_rr
        absolute_floor = getattr(OperatingMode, 'RR_HARD_FLOOR', 0.4)
        result.effective_floor = max(regime_min_rr, absolute_floor)
        
        result.tags['rr_regime_floor'] = regime_min_rr
        result.tags['rr_effective_floor'] = result.effective_floor
        
        # Check ideal RR against floor
        if result.ideal_rr < result.effective_floor:
            result.is_valid = False
            result.block_reason = f"RR {result.ideal_rr:.2f}R < floor {result.effective_floor:.2f}R"
            result.tags['skip_reason'] = 'rr_below_floor'
            return result
        
        # Calculate worst-case RR with slippage
        result.worst_case_rr = self.slippage_model.calculate_worst_case_rr(
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=bias,
            order_type=OrderType.LIMIT,
            volatility_ratio=volatility_ratio
        )
        
        result.tags['rr_worst_case'] = result.worst_case_rr
        result.tags['rr_slippage_impact'] = result.ideal_rr - result.worst_case_rr
        
        logger.info(
            f"📊 RR Analysis: Ideal={result.ideal_rr:.2f}R → "
            f"Worst-case={result.worst_case_rr:.2f}R "
            f"(slippage={result.ideal_rr - result.worst_case_rr:.2f}R)"
        )
        
        # RR-based sizing
        current_regime_str = current_regime.value if current_regime else "NORMAL"
        result.rr_size_multiplier = EntryGateConfig.get_rr_size_multiplier(
            result.worst_case_rr, current_regime_str
        )
        result.tags['rr_size_multiplier'] = result.rr_size_multiplier
        
        # NOTE (Jan 27, 2026): HIGH SCORE BYPASS removed.
        # HARD BLOCKS (Phase 5.5) now blocks RR < 0.7R with NO exceptions.
        # This bypass would never execute anyway since HARD BLOCKS runs first.
        
        if result.rr_size_multiplier == 0.0:
            result.is_valid = False
            result.block_reason = (
                f"Worst-case RR {result.worst_case_rr:.2f}R < floor "
                f"{result.effective_floor:.2f}R (ideal was {result.ideal_rr:.2f}R)"
            )
            result.tags['skip_reason'] = 'worst_case_rr_below_floor'
        
        return result


# ==============================================================================
# COST VALIDATOR
# ==============================================================================

class CostValidator:
    """
    Validates that trading costs don't exceed expected profit threshold.
    
    Extracted from _execute_entry lines ~5155-5235.
    """
    
    def validate_costs(
        self,
        position_size: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        bias: str,
        is_maker_entry: bool = True,
        is_maker_exit: bool = True,
    ) -> CostValidationResult:
        """
        Validate that costs don't eat expected profit.
        
        Returns CostValidationResult with validation status.
        """
        # 🔥 FIX: Correct import path for cost_model
        from src.domain.trading.cost.cost_model import get_cost_model
        
        result = CostValidationResult()
        
        try:
            cost_model = get_cost_model()
            notional_usd = position_size * current_price
            
            # Estimate roundtrip costs
            costs = cost_model.estimate_costs(
                side=bias,
                size_usd=notional_usd,
                entry_price=current_price,
                is_maker_entry=is_maker_entry,
                is_maker_exit=is_maker_exit,
            )
            
            # Risk in USD (1R)
            sl_distance = abs(current_price - stop_loss)
            risk_usd = position_size * sl_distance

            # Expected gross profit at TP (not a fixed 1.2R)
            tp_distance = abs(take_profit - current_price)
            expected_gross_profit_usd = position_size * tp_distance

            result.expected_profit_usd = expected_gross_profit_usd
            result.cost_usd = costs.total_usd

            # Fee estimate in R terms
            result.fee_r_estimate = (result.cost_usd / risk_usd) if risk_usd > 0 else 999.0

            # Expected net R (after estimated costs)
            gross_r = (expected_gross_profit_usd / risk_usd) if risk_usd > 0 else 0.0
            result.expected_net_r = gross_r - result.fee_r_estimate

            # Expected net profit in USD
            result.expected_profit_usd_net = expected_gross_profit_usd - result.cost_usd
            
            # Adaptive threshold based on maker/taker profile
            # (Kept for diagnostics; hard blocks live elsewhere.)
            if not is_maker_entry or not is_maker_exit:
                result.threshold = 0.50
            else:
                result.threshold = 0.80
            
            result.cost_ratio = (
                result.cost_usd / result.expected_profit_usd 
                if result.expected_profit_usd > 0 else 999
            )
            
            if result.cost_ratio > result.threshold:
                result.is_valid = False
                result.block_reason = (
                    f"Costs ${result.cost_usd:.4f} = {result.cost_ratio*100:.1f}% "
                    f"of expected profit ${result.expected_profit_usd:.4f} "
                    f"(threshold: {result.threshold*100:.0f}%)"
                )
                logger.warning(f"💸 COST VALIDATOR BLOCK: {result.block_reason}")
            else:
                logger.info(
                    f"✅ Cost validator PASS: ${result.cost_usd:.4f} = "
                    f"{result.cost_ratio*100:.1f}% of profit (< {result.threshold*100:.0f}%)"
                )

            logger.info(
                f"📉 Net expectancy: gross={gross_r:.2f}R | fees≈{result.fee_r_estimate:.3f}R | net≈{result.expected_net_r:.2f}R | "
                f"profit_net=${result.expected_profit_usd_net:.2f}"
            )
        
        except Exception as e:
            logger.warning(f"⚠️ Cost validator error (proceeding with caution): {e}")
            result.cost_validation_failed = True  # Flag for observability
        
        return result


# ==============================================================================
# MOMENTUM VALIDATOR
# ==============================================================================

class MomentumValidator:
    """
    Validates momentum conditions and determines order type.
    
    Extracted from _execute_entry lines ~5055-5150.
    """
    
    def determine_order_type(
        self,
        df_1m,
        atr_1m: float,
        gate_require_limit: bool,
        adx_value: float,
    ) -> Tuple[bool, float, Optional[str]]:
        """
        Determine whether to use market or limit order based on momentum.
        
        Returns:
            - use_market_order: bool
            - price_velocity: float (in ATR units)
            - skip_reason: str or None if should skip entry
        """
        use_market_order = False
        price_velocity = 0.0
        skip_reason = None
        
        candles_1m = df_1m.to_dict('records') if hasattr(df_1m, 'to_dict') else df_1m
        
        if not candles_1m or len(candles_1m) < 2:
            return use_market_order, price_velocity, skip_reason
        
        # Get last 2 candles
        last_close = candles_1m[-1]['close']
        prev_close = candles_1m[-2]['close']
        price_change = abs(last_close - prev_close)
        
        # Calculate ATR
        closes_1m = [c['close'] for c in candles_1m[-14:]]
        highs_1m = [c['high'] for c in candles_1m[-14:]]
        lows_1m = [c['low'] for c in candles_1m[-14:]]
        
        if len(closes_1m) < 14:
            return use_market_order, price_velocity, skip_reason
        
        from src.shared.indicators import calculate_atr
        atr_1m_vals = calculate_atr(
            np.array(highs_1m, dtype=float),
            np.array(lows_1m, dtype=float),
            np.array(closes_1m, dtype=float),
            14
        )
        atr_1m_current = atr_1m_vals[-1] if len(atr_1m_vals) > 0 else atr_1m
        
        # Velocity = price change / ATR
        price_velocity = price_change / atr_1m_current if atr_1m_current > 0 else 0
        
        # Check for chasing in dead market
        is_chasing_in_range = (
            price_velocity > OrderExecution.MOMENTUM_MAX_VELOCITY and
            adx_value < OrderExecution.MOMENTUM_MAX_VELOCITY_ADX_THRESHOLD
        )
        
        if is_chasing_in_range:
            skip_reason = 'chasing_in_range'
            logger.warning(
                f"🚫 CHASING IN DEAD MARKET: velocity={price_velocity:.2f}ATR > "
                f"{OrderExecution.MOMENTUM_MAX_VELOCITY}ATR + ADX={adx_value:.1f} < "
                f"{OrderExecution.MOMENTUM_MAX_VELOCITY_ADX_THRESHOLD} → SKIP"
            )
            return use_market_order, price_velocity, skip_reason
        
        # Determine order type
        use_market_order = (
            OrderExecution.USE_MARKET_ON_MOMENTUM and 
            price_velocity > OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER
        )
        
        # Gate can force limit order
        if gate_require_limit and use_market_order:
            logger.info("🚧 GATE OVERRIDE: Market order blocked → using LIMIT")
            use_market_order = False
        
        if use_market_order:
            logger.info(
                f"⚡ HIGH MOMENTUM: velocity={price_velocity:.2f}ATR > "
                f"{OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER}ATR → Using MARKET order"
            )
        else:
            logger.debug(
                f"Normal momentum: velocity={price_velocity:.2f}ATR → Using LIMIT order"
            )
        
        return use_market_order, price_velocity, skip_reason


# ==============================================================================
# FACTORY FUNCTIONS
# ==============================================================================

_chase_validator: Optional[ChaseValidator] = None
_graduated_sizer: Optional[GraduatedSizer] = None
_rr_validator: Optional[RRValidator] = None
_cost_validator: Optional[CostValidator] = None
_momentum_validator: Optional[MomentumValidator] = None


def get_chase_validator(regime_detector, entry_threshold: float = 4.0) -> ChaseValidator:
    """Get singleton chase validator instance."""
    global _chase_validator
    if _chase_validator is None:
        _chase_validator = ChaseValidator(regime_detector, entry_threshold)
    return _chase_validator


def get_graduated_sizer() -> GraduatedSizer:
    """Get singleton graduated sizer instance."""
    global _graduated_sizer
    if _graduated_sizer is None:
        _graduated_sizer = GraduatedSizer()
    return _graduated_sizer


def get_rr_validator(slippage_model) -> RRValidator:
    """Get singleton RR validator instance."""
    global _rr_validator
    if _rr_validator is None:
        _rr_validator = RRValidator(slippage_model)
    return _rr_validator


def get_cost_validator() -> CostValidator:
    """Get singleton cost validator instance."""
    global _cost_validator
    if _cost_validator is None:
        _cost_validator = CostValidator()
    return _cost_validator


def get_momentum_validator() -> MomentumValidator:
    """Get singleton momentum validator instance."""
    global _momentum_validator
    if _momentum_validator is None:
        _momentum_validator = MomentumValidator()
    return _momentum_validator


# ==============================================================================
# 🔥 HARD ENTRY BLOCKS (Jan 27, 2026)
# ==============================================================================
# These are NON-NEGOTIABLE blocks. NO bypass. NO exceptions.
# If any of these conditions are met, the trade is BLOCKED.
# ==============================================================================

@dataclass
class HardBlockResult:
    """Result of hard entry block validation."""
    is_blocked: bool = False
    block_reason: str = ""
    block_type: str = ""  # 'session_quality', 'rr_floor', etc.
    # 🔥 NEW (Jan 28): PROVISIONAL support
    is_provisional: bool = False
    provisional_reason: str = ""


class HardEntryBlockValidator:
    """
    HARD entry blocks - NO EXCEPTIONS, NO BYPASS.
    
    🔥 FIX (Jan 27, 2026): 
    The system was accepting garbage trades with:
    - session_quality = 'poor' → "entering anyway"
    - RR worst-case < 0.7R → "using probe despite"
    
    This ends NOW. These conditions BLOCK the trade. Period.
    
    A professional trader prefers 0 trades to forced trades with:
    - Bad session quality
    - Negative expectancy after costs
    
    NO HIGH SCORE BYPASS. NO ANTI-PARALYSIS OVERRIDE.
    """
    
    # 🔥 FIX (Jan 28, 2026): Dynamic RR floor based on fees
    # OLD: Fixed 0.7R floor
    # NEW: max(0.5R, 0.2R + fees_buffer)
    # This ensures we always cover costs PLUS have minimum edge
    HARD_RR_FLOOR_MIN = 0.5  # Absolute minimum
    FEES_BUFFER_R = 0.2      # Buffer above fees
    DEFAULT_FEES_R = 0.25    # Default fee estimate (~0.25R roundtrip including spread)
    
    @classmethod
    def calculate_dynamic_rr_floor(cls, estimated_fees_r: float | None = None) -> float:
        """Calculate dynamic RR floor: max(0.5R, 0.2R + fees)"""
        fees = estimated_fees_r if estimated_fees_r is not None else cls.DEFAULT_FEES_R
        return max(cls.HARD_RR_FLOOR_MIN, cls.FEES_BUFFER_R + fees)
    
    # BLOCKED SESSION QUALITIES
    BLOCKED_SESSION_QUALITIES = {'poor'}  # Block all 'poor' sessions
    
    @classmethod
    def validate_entry(
        cls,
        session_quality: str,
        worst_case_rr: float,
        adx: float | None = None,
        score: float = 0.0,
        score_timing: int | None = None,
        estimated_fees_r: float | None = None,  # 🔥 NEW: Pass estimated fees
    ) -> HardBlockResult:
        """
        Validate entry against HARD blocks.
        
        Args:
            session_quality: 'good', 'moderate', or 'poor'
            worst_case_rr: RR after slippage and fees
            adx: Current ADX (optional, for context logging)
            score: Entry score (NOT used for bypass - just for logging)
            score_timing: Timing score (0-2). If 0, entry is risky.
            estimated_fees_r: Estimated fees in R (optional)
            
        Returns:
            HardBlockResult with block decision
        """
        result = HardBlockResult()
        
        # Calculate dynamic RR floor
        rr_floor = cls.calculate_dynamic_rr_floor(estimated_fees_r)
        
        # =====================================================================
        # BLOCK 1: Session Quality = 'poor'
        # =====================================================================
        if session_quality.lower() in cls.BLOCKED_SESSION_QUALITIES:
            result.is_blocked = True
            result.block_type = 'session_quality'
            result.block_reason = (
                f"🛑 HARD BLOCK: session_quality='{session_quality}' | "
                f"Score={score:.1f} IGNORED - no entry in poor sessions. "
                f"Wait for better conditions."
            )
            logger.warning(result.block_reason)
            return result
        
        # =====================================================================
        # BLOCK 2: Worst-case RR < dynamic floor
        # 🔥 FIX (Jan 28, 2026): Using dynamic floor instead of fixed 0.7R
        # =====================================================================
        if worst_case_rr < rr_floor:
            result.is_blocked = True
            result.block_type = 'rr_floor'
            result.block_reason = (
                f"🛑 HARD BLOCK: worst_case_rr={worst_case_rr:.2f}R < {rr_floor:.2f}R floor | "
                f"Score={score:.1f} IGNORED - mathematically negative expectancy. "
                f"NO probe, NO bypass."
            )
            logger.warning(result.block_reason)
            return result
        
        # =====================================================================
        # BLOCK 3: ADX-based ranging market with low RR (combined check)
        # In ranging + low volatility, even dynamic floor may be risky
        # =====================================================================
        # Use HTFFilters.ADX_RANGE_MAX (18) for consistency across the system
        from src.config.trading_config import HTFFilters
        ADX_RANGE_THRESHOLD = HTFFilters.ADX_RANGE_MAX  # 18
        RR_RANGE_FLOOR = rr_floor + 0.15  # Add buffer in ranging markets

        if adx is not None and adx < ADX_RANGE_THRESHOLD and worst_case_rr < RR_RANGE_FLOOR:
            result.is_blocked = True
            result.block_type = 'range_rr'
            result.block_reason = (
                f"🛑 HARD BLOCK: Ranging market (ADX={adx:.1f} < {ADX_RANGE_THRESHOLD}) + "
                f"RR={worst_case_rr:.2f}R < {RR_RANGE_FLOOR:.2f}R | "
                f"In range, need higher RR to cover chop costs."
            )
            logger.warning(result.block_reason)
            return result
        
        # =====================================================================
        # 🔥 CAMBIO CLAVE #2 (Jan 28): TIMING=0 NO BLOQUEA, MARCA PROVISIONAL
        # 
        # ANTES: score_timing=0 → HARD BLOCK
        # AHORA: score_timing=0 → PROVISIONAL (marca para size reducido)
        # 
        # Esto solo ya dobla la frecuencia sin empeorar drawdown.
        # El bloqueo ahora es informativo, no hard block.
        # =====================================================================
        if score_timing is not None and score_timing == 0:
            if worst_case_rr < 1.0:
                # NO bloquear, solo marcar como provisional
                result.is_provisional = True
                result.provisional_reason = f'timing_zero (RR={worst_case_rr:.2f})'
                logger.info(
                    f"⚠️ PROVISIONAL: score_timing=0 + RR={worst_case_rr:.2f}R → "
                    f"starter position, no hard block"
                )
                # NOT blocked - continue to return non-blocked result
        
        # All checks passed
        return result
    
    @classmethod
    def get_minimum_rr_for_session(cls, session_quality: str) -> float:
        """
        Get minimum required RR for a session quality.
        
        Args:
            session_quality: 'good', 'moderate', 'poor'
            
        Returns:
            Minimum worst-case RR required
        """
        # 🔄 REFACTORED Feb 04: Use SessionQuality for consistent checks
        from src.config.trading_config import SessionQuality
        if SessionQuality.is_quality_blocked(session_quality):
            return float('inf')  # Never enter
        elif session_quality.lower() == 'moderate':
            return 0.65  # Need slightly better RR
        else:  # good
            return cls.calculate_dynamic_rr_floor()


# ==============================================================================
# PROBE VALIDATOR (Jan 28, 2026)
# ==============================================================================

class ProbeValidator:
    """
    Validates PROBE-specific entry requirements.
    
    PROBE trades have relaxed RR but MUST meet minimum profitability:
    - expected_R ≥ 0.25
    - expected_net_profit ≥ fees × 2
    - TP ≥ 0.3R
    - maker only (entrada + salida) - enforced elsewhere
    
    PROBE PROHIBIDO SI:
    - TP < spread × 2 (ruido puro)
    - SL < ATR × 0.3 (ruido puro)
    """
    
    # Constants for PROBE validation
    MIN_EXPECTED_R = 0.25
    MIN_PROFIT_FEE_MULT = 2.0  # profit >= fees * 2
    MIN_TP_R = 0.3
    MIN_TP_SPREAD_MULT = 2.0  # TP must be > spread * 2
    MIN_SL_ATR_MULT = 0.3     # SL must be > ATR * 0.3
    
    # Kraken maker fee for estimation
    MAKER_FEE_PCT = 0.02 / 100  # 0.02%
    
    @classmethod
    def validate_probe(
        cls,
        is_probe: bool,
        position_size: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        bias: str,
        atr_1m: float,
        spread_bps: float = 1.0,
    ) -> ProbeValidationResult:
        """
        Validate a PROBE trade meets minimum profitability requirements.
        
        Only applies if is_probe=True. Non-probe trades pass through.
        
        Args:
            is_probe: Whether this is a probe trade (probing_mode set)
            position_size: Size in BTC
            current_price: Current price
            stop_loss: SL price
            take_profit: TP price
            bias: 'LONG' or 'SHORT'
            atr_1m: 1-minute ATR
            spread_bps: Current spread in basis points
            
        Returns:
            ProbeValidationResult with validation status
        """
        result = ProbeValidationResult()
        
        # Non-probe trades pass through
        if not is_probe:
            return result
        
        # Calculate trade metrics
        if bias == 'LONG':
            risk_usd = (current_price - stop_loss) * position_size
            reward_usd = (take_profit - current_price) * position_size
            tp_distance = take_profit - current_price
            sl_distance = current_price - stop_loss
        else:
            risk_usd = (stop_loss - current_price) * position_size
            reward_usd = (current_price - take_profit) * position_size
            tp_distance = current_price - take_profit
            sl_distance = stop_loss - current_price
        
        # Ensure positive values
        risk_usd = abs(risk_usd)
        reward_usd = abs(reward_usd)
        tp_distance = abs(tp_distance)
        sl_distance = abs(sl_distance)
        
        # Calculate R values
        result.expected_r = reward_usd / risk_usd if risk_usd > 0 else 0
        result.tp_r = result.expected_r  # TP in R terms
        
        # Estimate fees (maker entry + maker exit)
        notional = position_size * current_price
        result.fees_usd = notional * cls.MAKER_FEE_PCT * 2  # entry + exit
        
        # Net profit
        result.expected_net_profit = reward_usd - result.fees_usd
        
        # =========================================================================
        # PROBE VALIDATION RULES
        # =========================================================================
        
        # Rule 1: expected_R ≥ 0.25
        if result.expected_r < cls.MIN_EXPECTED_R:
            result.is_valid = False
            result.block_reason = (
                f"🔬 PROBE BLOCK: expected_R={result.expected_r:.2f} < {cls.MIN_EXPECTED_R} "
                f"(need minimum edge)"
            )
            logger.warning(result.block_reason)
            return result
        
        # Rule 2: expected_net_profit ≥ fees × 2
        min_profit = result.fees_usd * cls.MIN_PROFIT_FEE_MULT
        if result.expected_net_profit < min_profit:
            result.is_valid = False
            result.block_reason = (
                f"🔬 PROBE BLOCK: net_profit=${result.expected_net_profit:.2f} < "
                f"fees×{cls.MIN_PROFIT_FEE_MULT:.0f}=${min_profit:.2f} "
                f"(fees would eat profit)"
            )
            logger.warning(result.block_reason)
            return result
        
        # Rule 3: TP ≥ 0.3R
        if result.tp_r < cls.MIN_TP_R:
            result.is_valid = False
            result.block_reason = (
                f"🔬 PROBE BLOCK: TP={result.tp_r:.2f}R < {cls.MIN_TP_R}R "
                f"(target too small)"
            )
            logger.warning(result.block_reason)
            return result
        
        # Rule 4: TP > spread × 2 (avoid noise)
        spread_usd = current_price * (spread_bps / 10000)
        min_tp_spread = spread_usd * cls.MIN_TP_SPREAD_MULT
        if tp_distance < min_tp_spread:
            result.is_valid = False
            result.block_reason = (
                f"🔬 PROBE BLOCK: TP_dist=${tp_distance:.2f} < spread×{cls.MIN_TP_SPREAD_MULT:.0f}=${min_tp_spread:.2f} "
                f"(pure noise)"
            )
            logger.warning(result.block_reason)
            return result
        
        # Rule 5: SL > ATR × 0.3 (avoid noise)
        min_sl_atr = atr_1m * cls.MIN_SL_ATR_MULT
        if sl_distance < min_sl_atr:
            result.is_valid = False
            result.block_reason = (
                f"🔬 PROBE BLOCK: SL_dist=${sl_distance:.2f} < ATR×{cls.MIN_SL_ATR_MULT}=${min_sl_atr:.2f} "
                f"(SL in noise zone)"
            )
            logger.warning(result.block_reason)
            return result
        
        # All PROBE checks passed
        logger.info(
            f"✅ PROBE VALID: R={result.expected_r:.2f} | "
            f"net_profit=${result.expected_net_profit:.2f} | "
            f"fees=${result.fees_usd:.2f}"
        )
        return result


def get_hard_entry_validator() -> type:
    """Get the HardEntryBlockValidator class."""
    return HardEntryBlockValidator


def get_probe_validator() -> type:
    """Get the ProbeValidator class."""
    return ProbeValidator
