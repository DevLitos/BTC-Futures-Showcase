"""
Entry Scorer - Score > Gates Architecture

Extracted from signal_engine.py (Jan 20, 2026)

4 blocks × 0-2 points = max 8. Threshold: 5
Only Structure=0 blocks entry (the only real gate)

Score blocks:
1. STRUCTURE (0-2): HTF alignment + 5m structure
2. TIMING (0-2): BOS confirmation + clean close
3. MOMENTUM (0-2): ADX + slope + volume
4. CONTEXT (0-2): Session + volatility ratio
5. SYNERGY (0-1): Non-linear bonus for perfect setups

Total possible: 9 points
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List, TYPE_CHECKING
from datetime import datetime

from src.shared.indicators import (
    calculate_ema,
    calculate_adx,
    calculate_atr,
    calculate_rsi,
)
from src.config.trading_config import OperatingMode, get_session_name

if TYPE_CHECKING:
    from src.domain.market.filters import EntryFilterPipeline
    from src.domain.market.filters.filter_interface import FilterDecision
    from src.domain.market.filters import MicroTrendState

logger = logging.getLogger(__name__)


@dataclass
class ScoreBreakdown:
    """Detailed breakdown of entry score calculation."""
    structure: int = 0
    timing: int = 0
    momentum: int = 0
    context: int = 0
    synergy: int = 0
    micro_trend_adj: float = 0.0
    filter_pipeline_adj: float = 0.0
    # 🔥 NEW (Jan 24): Late confirmation penalty
    late_confirmation_penalty: float = 0.0
    penalty_hierarchy: Dict[str, Any] = field(default_factory=dict)
    adx_values: np.ndarray = field(default_factory=lambda: np.array([]))
    
    @property
    def total(self) -> int:
        """Calculate total score."""
        base = self.structure + self.timing + self.momentum + self.context + self.synergy
        return max(0, int(base + self.micro_trend_adj + self.filter_pipeline_adj + self.late_confirmation_penalty))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            'structure': self.structure,
            'timing': self.timing,
            'momentum': self.momentum,
            'context': self.context,
            'synergy': self.synergy,
            'micro_trend_adj': self.micro_trend_adj,
            'filter_pipeline_adj': self.filter_pipeline_adj,
            'late_confirmation_penalty': self.late_confirmation_penalty,
            'total': self.total
        }


@dataclass
class TradeTags:
    """Tags collected during scoring for trade analysis."""
    score: int = 0
    mode: str = ""
    htf_aligned: bool = False
    ltf_clean: bool = False
    bos_confirmed: bool = False
    ema_reclaim: bool = False
    adx: float = 0.0
    adx_5m: float = 0.0
    adx_slope: float = 0.0
    adx_slope_bucket: str = ""
    rsi: float = 0.0
    atr: float = 0.0
    session: str = "unknown"
    session_quality: str = ""
    volatility_ratio: float = 0.0
    rel_vol: float = 1.0
    volume_penalty: float = 0.0
    slope_penalty: float = 0.0
    penalty_hierarchy_saved: float = 0.0
    worst_context_penalty: Optional[str] = None
    worst_execution_penalty: Optional[str] = None
    # 🔥 NEW (Jan 24): Late confirmation penalty tracking
    late_confirmation_penalty: float = 0.0
    candles_since_bos: int = 0
    # 🔥 NEW (Feb 2): BOS Quality tracking for post-trade analysis
    bos_quality: str = "UNKNOWN"     # 'STRONG', 'NORMAL', 'WEAK'
    bos_body_atr: float = 0.0        # BOS body size in ATR multiples
    bos_volume_ratio: float = 0.0    # BOS volume vs average
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {k: v for k, v in self.__dict__.items()}


# =============================================================================
# 🔥 LATE CONFIRMATION PENALTY (Jan 24, 2026)
# Penalizes "perfect" setups that took too long to confirm
# 
# PROBLEM: Score 7+ trades have negative expectancy because:
# - When ALL confirmations align (score 9), the move already happened
# - You enter at the TOP/BOTTOM of the move with no room left
# - MFE is low, MAE is high → losing trade
#
# SOLUTION: Penalize high scores that took many candles to confirm
# - Score 5-6 that confirms quickly → good (enters early)
# - Score 8-9 that took forever → bad (enters late)
# =============================================================================
class LateConfirmationConfig:
    """
    Configuration for late confirmation penalty.
    
    Philosophy:
    - Quick confirmation (2-3 candles) = early entry = good
    - Slow confirmation (7+ candles) = late entry = penalize
    - The more "perfect" the score, the MORE suspicious we are of lateness
    """
    
    # If all 4 components have max score AND it took > N candles, penalize
    PERFECT_SCORE_CANDLES_THRESHOLD = 5  # If perfect setup took > 5 candles
    
    # Penalty per "late" candle beyond threshold
    PENALTY_PER_LATE_CANDLE = 0.5
    
    # Maximum penalty from late confirmation
    MAX_LATE_PENALTY = 2.0
    
    # Only apply to scores above this (don't penalize already-weak setups)
    MIN_SCORE_FOR_LATE_PENALTY = 99  # 🔥 FIX (Jan 24): DISABLED - high confluence = good, not bad
    
    # Candle counting method
    # We count candles since BOS started forming (using df_1m high/low breaks)
    MAX_CANDLES_TO_CHECK = 10
    
    @classmethod
    def calculate_penalty(
        cls,
        raw_score: int,
        candles_since_bos: int,
        all_components_maxed: bool
    ) -> float:
        """
        Calculate late confirmation penalty.
        
        Args:
            raw_score: Score before this penalty
            candles_since_bos: How many candles since initial BOS signal
            all_components_maxed: True if structure=2, timing=2, momentum=2, context=2
            
        Returns:
            Penalty value (negative, to reduce score)
        """
        # Only apply to high scores
        if raw_score < cls.MIN_SCORE_FOR_LATE_PENALTY:
            return 0.0
        
        # Only apply if we detected slow confirmation
        if candles_since_bos <= cls.PERFECT_SCORE_CANDLES_THRESHOLD:
            return 0.0
        
        # Extra suspicion for "perfect" setups
        suspicion_mult = 1.5 if all_components_maxed else 1.0
        
        # Calculate penalty
        late_candles = candles_since_bos - cls.PERFECT_SCORE_CANDLES_THRESHOLD
        penalty = late_candles * cls.PENALTY_PER_LATE_CANDLE * suspicion_mult
        
        # Cap penalty
        penalty = min(penalty, cls.MAX_LATE_PENALTY)
        
        return -penalty


class EntryScorer:
    """
    Entry Score System - Score > Gates architecture
    
    Calculates entry quality score using 4 blocks.
    Only Structure=0 blocks entry (the only real gate).
    """
    
    def __init__(self):
        self._breakdown = ScoreBreakdown()
        self._trade_tags = TradeTags()
    
    @property
    def breakdown(self) -> ScoreBreakdown:
        """Get last score breakdown."""
        return self._breakdown
    
    @property
    def trade_tags(self) -> TradeTags:
        """Get last trade tags."""
        return self._trade_tags
    
    def calculate_entry_score(
        self,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        df_1m: pd.DataFrame,
        bias: str,
        current_price: float,
        market_time: datetime,
        micro_trend_state: Optional['MicroTrendState'] = None,
        filter_pipeline: Optional['EntryFilterPipeline'] = None,
        displacement_edge_active: bool = False,  # 🔥 FIX (Jan 30): Bypass structure for displacement
        expansion_mode_active: bool = False,     # 🔥 FIX #56: Expansion mode bypass
        strong_trend_adx: float = 0.0,           # 🔥 FIX #56: ADX for strong trend override
        bias_inverted: bool = False,             # 🔥 FIX #61: Bias was inverted by micro-trend
        bias_invalidated: bool = False,          # 🔥 FIX #63: Bias was flipped (opposite dir forbidden)
    ) -> Tuple[int, Dict[str, Any], bool]:
        """
        Calculate entry score using 4 blocks.
        
        Args:
            df_1h: 1-hour DataFrame with OHLCV
            df_5m: 5-minute DataFrame with OHLCV
            df_1m: 1-minute DataFrame with OHLCV
            bias: 'LONG' or 'SHORT'
            current_price: Current market price
            market_time: Current market timestamp
            micro_trend_state: Optional MicroTrendState for adjustment
            filter_pipeline: Optional filter pipeline for adjustments
            displacement_edge_active: If True, bypass STRUCTURE GATE (market already voted)
            expansion_mode_active: If True, bypass STRUCTURE GATE (expansion/trend mode)
            strong_trend_adx: ADX value for strong trend override (>50 = override)
            bias_inverted: If True, bypass STRUCTURE GATE (direction changed by micro-trend)
            bias_invalidated: If True, bypass STRUCTURE GATE (opposite direction forbidden by price action)
        
        Returns:
            (total_score, breakdown_dict, structure_blocks)
            If structure_blocks=True, DO NOT ENTER regardless of score.
        """
        # Reset state
        self._breakdown = ScoreBreakdown()
        self._trade_tags = TradeTags(mode=OperatingMode.MODE)
        
        # 1️⃣ STRUCTURE (0-2) - THE ONLY GATE
        # 🔥 FIX (Jan 30): DISPLACEMENT EDGE bypasses structure gate
        # Rationale: The market has already "voted" with size, speed and continuation.
        # Following slow EMA50_1h against a $300+ displacement is retail thinking.
        structure_score, structure_blocks, htf_clean = self._score_structure(
            df_1h, df_5m, bias, current_price
        )
        
        # 🔥 DISPLACEMENT OVERRIDE: If displacement edge is active, IGNORE structure gate
        if structure_blocks and displacement_edge_active:
            logger.warning(
                f"🌊 DISPLACEMENT STRUCTURE BYPASS: Ignoring STRUCTURE GATE for {bias} | "
                f"Market momentum > HTF structure (EMA50_1h is SLOW during fast moves)"
            )
            structure_blocks = False
            structure_score = 1  # Give partial structure credit
        
        # 🔥 FIX #56: EXPANSION MODE OVERRIDE - Market in confirmed expansion
        if structure_blocks and expansion_mode_active:
            logger.warning(
                f"📈 EXPANSION STRUCTURE BYPASS: Ignoring STRUCTURE GATE for {bias} | "
                f"Expansion mode active - trend already confirmed"
            )
            structure_blocks = False
            structure_score = 1
        
        # 🔥 FIX #56: STRONG TREND ADX OVERRIDE - Very high ADX means clear trend
        STRUCTURE_GATE_ADX_OVERRIDE = 55.0  # Very strong trend threshold
        if structure_blocks and strong_trend_adx >= STRUCTURE_GATE_ADX_OVERRIDE:
            logger.warning(
                f"🔥 STRONG TREND STRUCTURE BYPASS: Ignoring STRUCTURE GATE for {bias} | "
                f"ADX={strong_trend_adx:.1f} >= {STRUCTURE_GATE_ADX_OVERRIDE} → clear trend"
            )
            structure_blocks = False
            structure_score = 1
        
        # 🔥 FIX #61 (Feb 06): BIAS INVERSION OVERRIDE
        # When bias is inverted by strong micro-trend (BULLISH when HTF=SHORT or vice versa),
        # STRUCTURE GATE should not block because:
        # 1. The inversion already considered location extreme checks
        # 2. HTF structure (EMA50_1h) is slow and doesn't reflect current micro-trend
        # 3. Blocking creates DEADLOCK: can't SHORT (obstinacy) and can't LONG (structure)
        if structure_blocks and bias_inverted:
            logger.warning(
                f"🔄 BIAS INVERSION STRUCTURE BYPASS: Ignoring STRUCTURE GATE for {bias} | "
                f"Direction was inverted by micro-trend - structure check is stale"
            )
            structure_blocks = False
            structure_score = 1  # Partial credit - inversion means micro is aligned
        
        # 🔥 FIX #63 (Feb 06): BIAS FLIP/INVALIDATION OVERRIDE
        # When opposite direction is FORBIDDEN by price action (e.g., "SHORT forbidden, LONG enabled"),
        # the system has already confirmed direction change via price structure.
        # STRUCTURE GATE should not block because:
        # 1. Price has moved significantly against the old bias (invalidated)
        # 2. The system is now FORCED to trade the new direction
        # 3. EMA50_1h is slow and doesn't reflect the price invalidation
        # Example: Price rises $300+ from low → SHORT forbidden → LONG is the ONLY valid direction
        if structure_blocks and bias_invalidated:
            logger.warning(
                f"🔄 FIX #63 BIAS FLIP STRUCTURE BYPASS: Ignoring STRUCTURE GATE for {bias} | "
                f"Opposite direction was FORBIDDEN by price action - structure is stale"
            )
            structure_blocks = False
            structure_score = 1  # Partial credit - price has voted
        
        self._breakdown.structure = structure_score
        self._trade_tags.htf_aligned = structure_score > 0
        self._trade_tags.ltf_clean = structure_score == 2
        
        if structure_blocks:
            # Structure = 0 is the only hard gate
            return 0, self._breakdown.to_dict(), True
        
        # 2️⃣ TIMING (0-2)
        timing_score, bos_confirmed = self._score_timing(df_1m, bias)
        
        # SEMANTIC COHERENCE: Timing capped to 1 if Structure = 0
        if self._breakdown.structure == 0:
            timing_score = min(timing_score, 1)
            logger.debug("⚠️ Timing capped to 1 (Structure=0 → coherencia semántica)")
        
        self._breakdown.timing = timing_score
        self._trade_tags.bos_confirmed = bos_confirmed
        
        # 3️⃣ MOMENTUM (0-2)
        self._breakdown.momentum = self._score_momentum(df_5m, bias)
        
        # MOMENTUM COHERENCE CHECK:
        # If momentum = 0 AND structure < 2, this is a low quality setup
        if self._breakdown.momentum == 0 and self._breakdown.structure < 2:
            logger.warning(
                f"⚠️ Weak momentum (0) with partial structure "
                f"({self._breakdown.structure}) - low quality setup"
            )
            self._breakdown.structure = max(0, self._breakdown.structure - 1)
        
        # 4️⃣ CONTEXT (0-2)
        self._breakdown.context = self._score_context(df_5m, market_time)
        
        # 5️⃣ SYNERGY BONUS (0-1) - Non-linear interaction
        if htf_clean and bos_confirmed:
            self._breakdown.synergy = 1
            logger.debug("✨ Synergy bonus: HTF clean + BOS confirmed")
        
        # 6️⃣ MICRO TREND ADJUSTMENT
        if micro_trend_state is not None:
            micro_adj = micro_trend_state.score_adjustment
            if micro_adj != 0:
                self._breakdown.micro_trend_adj = micro_adj
                if micro_adj < 0:
                    logger.warning(
                        f"📉 Micro trend penalty: {micro_adj:+.1f} "
                        f"(micro={micro_trend_state.micro_trend}, "
                        f"strength={micro_trend_state.strength:.0%})"
                    )
                else:
                    logger.info(
                        f"📈 Micro trend bonus: {micro_adj:+.1f} "
                        f"(micro={micro_trend_state.micro_trend})"
                    )
        
        # 6.5️⃣ FILTER PIPELINE ADJUSTMENT with PENALTY HIERARCHY
        if filter_pipeline is not None:
            last_decision = filter_pipeline.get_last_decision()
            if last_decision and last_decision.score_adjust != 0:
                fp_adj = self._apply_penalty_hierarchy(
                    filter_pipeline,
                    last_decision,
                    self._breakdown.micro_trend_adj
                )
                self._breakdown.filter_pipeline_adj = fp_adj
                if fp_adj < 0:
                    logger.warning(f"📉 Filter pipeline penalty (hierarchical): {fp_adj:+.1f}")
                else:
                    logger.info(f"📈 Filter pipeline bonus: {fp_adj:+.1f}")
        
        # =====================================================================
        # 7️⃣ LATE CONFIRMATION PENALTY (Jan 24, 2026)
        # Penalize "perfect" setups that took too long to confirm
        # This fixes negative expectancy on score 7+ trades
        # =====================================================================
        raw_score = self._breakdown.structure + self._breakdown.timing + \
                    self._breakdown.momentum + self._breakdown.context + \
                    self._breakdown.synergy
        
        # Check if all components are maxed (perfect setup → suspicious if late)
        all_maxed = (
            self._breakdown.structure == 2 and
            self._breakdown.timing == 2 and
            self._breakdown.momentum == 2 and
            self._breakdown.context == 2
        )
        
        # Count candles since BOS started forming
        candles_since_bos = self._count_candles_since_bos(df_1m, bias)
        
        # Calculate late confirmation penalty
        late_penalty = LateConfirmationConfig.calculate_penalty(
            raw_score=raw_score,
            candles_since_bos=candles_since_bos,
            all_components_maxed=all_maxed
        )
        
        if late_penalty < 0:
            self._breakdown.late_confirmation_penalty = late_penalty
            logger.warning(
                f"⏰ LATE CONFIRMATION PENALTY: {late_penalty:+.1f} "
                f"(candles_since_bos={candles_since_bos}, all_maxed={all_maxed})"
            )
            self._trade_tags.late_confirmation_penalty = late_penalty
        
        # Update final score in tags
        self._trade_tags.score = self._breakdown.total
        
        return self._breakdown.total, self._breakdown.to_dict(), False
    
    def _count_candles_since_bos(self, df_1m: pd.DataFrame, bias: str) -> int:
        """
        Count how many candles since BOS (Break of Structure) started forming.
        
        This helps detect "late" entries where price moved a lot before we entered.
        
        Returns:
            Number of candles since the initial BOS breakout (0 if very fresh)
        """
        if len(df_1m) < 10:
            return 0
        
        max_lookback = min(LateConfirmationConfig.MAX_CANDLES_TO_CHECK, len(df_1m) - 1)
        
        # Get recent swing high/low to compare against
        lookback_data = df_1m.iloc[-(max_lookback + 5):-max_lookback] if len(df_1m) > max_lookback + 5 else df_1m.iloc[:5]
        
        if len(lookback_data) < 3:
            return 0
        
        if bias == 'LONG':
            # Find when price first broke above recent high
            reference_high = lookback_data['high'].max()
            for i in range(max_lookback, 0, -1):
                candle = df_1m.iloc[-i]
                if candle['high'] > reference_high:
                    # Found first breakout, count remaining candles
                    return i - 1  # How many candles since that breakout
        else:  # SHORT
            # Find when price first broke below recent low
            reference_low = lookback_data['low'].min()
            for i in range(max_lookback, 0, -1):
                candle = df_1m.iloc[-i]
                if candle['low'] < reference_low:
                    return i - 1
        
        return 0  # No clear BOS found in lookback period
    
    def _apply_penalty_hierarchy(
        self,
        filter_pipeline: 'EntryFilterPipeline',
        pipeline_decision: 'FilterDecision',
        micro_adjustment: float
    ) -> float:
        """
        PHASE A: Apply penalty hierarchy to prevent excessive penalty stacking.
        
        Penalty Categories:
        - CONTEXT: {regime, volatility, micro_trend, correlation, macro_trend}
        - EXECUTION: {cost, spread, timing, overtrade}
        
        Rule: Only apply WORST penalty from CONTEXT + WORST from EXECUTION
        """
        filter_decisions = filter_pipeline.get_filter_decisions() if filter_pipeline else {}
        
        context_penalties = {}
        execution_penalties = {}
        
        CONTEXT_FILTERS = {'volatility_shock', 'bb_width', 'macro_trend', 'btc_correlation', 'post_sweep'}
        EXECUTION_FILTERS = {'cost', 'overtrade'}
        
        for source, decision in filter_decisions.items():
            if decision.score_adjust < 0:
                if source in CONTEXT_FILTERS:
                    context_penalties[source] = decision.score_adjust
                elif source in EXECUTION_FILTERS:
                    execution_penalties[source] = decision.score_adjust
        
        # Add micro_trend to context penalties if present
        if micro_adjustment < 0:
            context_penalties['micro_trend'] = micro_adjustment
        
        # Pick WORST penalty from each category
        worst_context = 0.0
        worst_context_source = None
        if context_penalties:
            worst_context_source = min(context_penalties, key=lambda k: context_penalties.get(k, 0) or 0)
            worst_context = context_penalties.get(worst_context_source, 0) or 0
        
        worst_execution = 0.0
        worst_execution_source = None
        if execution_penalties:
            worst_execution_source = min(execution_penalties, key=lambda k: execution_penalties.get(k, 0) or 0)
            worst_execution = execution_penalties.get(worst_execution_source, 0) or 0
        
        total_hierarchical = worst_context + worst_execution
        
        # Store hierarchy info
        self._breakdown.penalty_hierarchy = {
            'context_penalties': context_penalties,
            'execution_penalties': execution_penalties,
            'worst_context': (worst_context_source, worst_context),
            'worst_execution': (worst_execution_source, worst_execution),
            'total_before_hierarchy': pipeline_decision.score_adjust + micro_adjustment,
            'total_after_hierarchy': total_hierarchical
        }
        
        # Log if hierarchy saved us from excessive penalties
        original_total = sum(context_penalties.values()) + sum(execution_penalties.values())
        if abs(original_total) > abs(total_hierarchical) + 0.1:
            logger.info(
                f"🛡️ PENALTY HIERARCHY: Reduced {original_total:+.1f} → {total_hierarchical:+.1f} "
                f"| CONTEXT: {worst_context_source}={worst_context:+.1f} "
                f"| EXECUTION: {worst_execution_source}={worst_execution:+.1f}"
            )
            self._trade_tags.penalty_hierarchy_saved = abs(original_total - total_hierarchical)
        
        self._trade_tags.worst_context_penalty = worst_context_source
        self._trade_tags.worst_execution_penalty = worst_execution_source
        
        # Return only the hierarchical adjustment (micro_adjustment already applied above)
        return total_hierarchical - micro_adjustment if micro_adjustment < 0 else total_hierarchical
    
    def apply_rr_score_adjustment(
        self,
        score: int,
        rr: float,
        current_regime: Optional[Any] = None
    ) -> Tuple[float, str]:
        """
        Apply RR-based score adjustment with PROPORTIONAL penalty.
        
        RR < 1.0 → Proportional penalty based on distance from ideal
        RR >= 1.5 → Add bonus point (rewards high RR)
        
        RANGE regime gets lighter penalty (high winrate compensates)
        TREND regime gets full penalty (need RR for math)
        
        Returns: (adjusted_score, adjustment_reason)
        """
        adjustment = 0.0
        reason = ""
        
        # Get regime-specific multiplier for penalty
        regime_multiplier = 1.0  # Default (TREND)
        if current_regime is not None:
            # Import here to avoid circular import
            from src.domain.market.regime.regime_detector import EntryRegime
            if current_regime in (EntryRegime.RANGE,):
                regime_multiplier = getattr(OperatingMode, 'RR_PENALTY_RANGE_MULTIPLIER', 0.5)
        
        # RR below threshold → PROPORTIONAL penalty (not fixed)
        if rr < OperatingMode.RR_PENALTY_THRESHOLD:
            rr_gap = OperatingMode.RR_PENALTY_THRESHOLD - rr
            max_penalty = getattr(OperatingMode, 'RR_PENALTY_MAX_POINTS', 2)
            
            # Proportional: RR=0.5 gets full penalty, RR=0.9 gets 20% penalty
            penalty_ratio = min(1.0, rr_gap / 0.5)
            base_penalty = max_penalty * penalty_ratio
            
            # Apply regime multiplier (RANGE = 50% penalty)
            adjustment = -base_penalty * regime_multiplier
            
            regime_note = f" (RANGE: {regime_multiplier:.0%} penalty)" if regime_multiplier < 1.0 else ""
            reason = f"RR penalty: {rr:.2f}R < {OperatingMode.RR_PENALTY_THRESHOLD}R → {adjustment:.1f}{regime_note}"
            logger.warning(f"📉 {reason}")
        # RR excellent → bonus
        elif rr >= OperatingMode.RR_EXCELLENT_THRESHOLD:
            adjustment = OperatingMode.RR_EXCELLENT_BONUS
            reason = f"RR bonus: {rr:.2f}R >= {OperatingMode.RR_EXCELLENT_THRESHOLD}R → +{OperatingMode.RR_EXCELLENT_BONUS}"
            logger.info(f"📈 {reason}")
        
        adjusted_score = max(0, score + adjustment)
        return adjusted_score, reason
    
    def _score_structure(
        self,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        bias: str,
        current_price: float
    ) -> Tuple[int, bool, bool]:
        """
        STRUCTURE SCORE (0-2)
        +2 → HTF aligned + 5m structure clean
        +1 → HTF ok but LTF structure partial
        0 → Against structure → BLOCKS ENTRY
        
        UPDATED Jan 27, 2026:
        - Added EMA50 slope tolerance for shorts during downtrends
        - Added price proximity tolerance (0.3% = ~$250 at BTC $85k)
        - Problem: EMA50_1h is slow, during fast moves it blocks valid trades
        
        Returns: (score, blocks_entry, htf_is_clean)
        """
        if len(df_1h) < 50 or len(df_5m) < 20:
            return 1, False, False  # Not enough data, allow with partial score
        
        # HTF alignment check
        closes_1h = df_1h['close'].to_numpy()
        ema50_1h = calculate_ema(closes_1h, 50)
        
        if np.isnan(ema50_1h[-1]):
            return 1, False, False
        
        # =========================================================================
        # SMART HTF ALIGNMENT (Jan 27, 2026)
        # EMA50 is slow - during fast moves, price can be "against" EMA50 briefly
        # while the trend has clearly changed. Use these exceptions:
        # 1. Slope tolerance: If EMA50 slope confirms direction, allow
        # 2. Price proximity: If within 0.3% of EMA50, allow
        # =========================================================================
        
        # Calculate EMA50 slope (change over last 3 hours)
        ema50_slope = 0.0
        if len(ema50_1h) >= 4:
            ema50_slope = (ema50_1h[-1] - ema50_1h[-4]) / ema50_1h[-4] * 100  # % change
        
        # Price proximity to EMA50 (0.3% tolerance = ~$250 at BTC $85k)
        PROXIMITY_TOLERANCE = 0.003  # 0.3%
        price_distance_pct = (current_price - ema50_1h[-1]) / ema50_1h[-1]
        is_near_ema50 = abs(price_distance_pct) <= PROXIMITY_TOLERANCE
        
        htf_aligned = False
        htf_slope_override = False
        
        if bias == 'LONG':
            htf_aligned = current_price > ema50_1h[-1]
            # Allow LONG if near EMA50 and slope is positive (recovering uptrend)
            if not htf_aligned and is_near_ema50 and ema50_slope > 0:
                htf_aligned = True
                htf_slope_override = True
                logger.info(f"📈 STRUCTURE: LONG allowed - near EMA50 ({price_distance_pct:.2%}) with positive slope ({ema50_slope:.3f}%)")
        else:
            htf_aligned = current_price < ema50_1h[-1]
            # Allow SHORT if near EMA50 and slope is negative (developing downtrend)
            if not htf_aligned and is_near_ema50 and ema50_slope < 0:
                htf_aligned = True
                htf_slope_override = True
                logger.info(f"📉 STRUCTURE: SHORT allowed - near EMA50 ({price_distance_pct:.2%}) with negative slope ({ema50_slope:.3f}%)")
            # Also allow SHORT if slope is strongly negative (fast move down)
            elif not htf_aligned and ema50_slope < -0.05:  # -0.05% = ~$45/hour drop
                htf_aligned = True
                htf_slope_override = True
                logger.info(f"📉 STRUCTURE: SHORT allowed - strong negative slope ({ema50_slope:.3f}%) indicates downtrend")
        
        if not htf_aligned:
            # 🔥 FIX #62b (Feb 06): EXTREME DISTANCE BYPASS
            # If price is >7% away from EMA50_1h, the EMA is STALE and irrelevant.
            # This happens during:
            # 1. Flash crashes (price drops -10% in hours, EMA50_1h hasn't caught up)
            # 2. V-recoveries (price recovers but EMA50 is still at crash levels)
            # 3. Gap moves (weekend gaps where EMA50 is completely wrong)
            # In these cases, blocking trades is HARMFUL - we lose huge moves.
            EXTREME_DISTANCE_THRESHOLD = 0.07  # 7% = EMA50 is stale
            if abs(price_distance_pct) > EXTREME_DISTANCE_THRESHOLD:
                logger.warning(
                    f"🔥 FIX #62b EXTREME DISTANCE BYPASS: EMA50 at {price_distance_pct:.1%} is STALE | "
                    f"{bias} allowed - EMA50_1h cannot reflect current market during crash/recovery"
                )
                # Allow trade but with partial structure score
                return 1, False, False
            
            # Against HTF → Structure = 0 → BLOCKS
            logger.warning(f"⛔ STRUCTURE GATE: {bias} against HTF (Price vs EMA50_1h) | "
                          f"Price: {current_price:.0f}, EMA50: {ema50_1h[-1]:.0f}, "
                          f"Distance: {price_distance_pct:.2%}, Slope: {ema50_slope:.3f}%")
            return 0, True, False
        
        # 5m structure check (price vs EMA20)
        closes_5m = df_5m['close'].to_numpy()
        ema20_5m = calculate_ema(closes_5m, 20)
        
        if np.isnan(ema20_5m[-1]):
            return 1, False, htf_aligned
        
        ltf_clean = False
        if bias == 'LONG':
            # Clean structure: price above EMA20 or just reclaiming
            ltf_clean = current_price >= ema20_5m[-1] * 0.998  # Within 0.2%
        else:
            ltf_clean = current_price <= ema20_5m[-1] * 1.002
        
        if htf_aligned and ltf_clean:
            return 2, False, True  # Perfect alignment, HTF is clean
        elif htf_aligned:
            # Give partial credit if override was used
            score = 1 if not htf_slope_override else 1
            return score, False, False  # HTF ok, LTF partial
        
        return 0, True, False  # Should not reach here
    
    def _score_timing(
        self,
        df_1m: pd.DataFrame,
        bias: str
    ) -> Tuple[int, bool]:
        """
        TIMING SCORE (0-2)
        +2 → BOS confirmed at CLOSE + clean reclaim + STRONG BOS
        +1 → BOS + late timing OR weak BOS
        0 → Signal intra-candle / ambiguous
        
        🔥 MEJORA (Feb 2, 2026): BOS QUALITY ASSESSMENT
        
        PROBLEMA: Antes un BOS débil (vela pequeña, bajo volumen) puntuaba igual
        que un BOS fuerte (vela grande, volumen alto). Esto mezclaba trades malos
        con buenos en el mismo bucket de score.
        
        SOLUCIÓN: Evaluar CALIDAD del BOS:
        - Body size vs ATR (grande = convincente)
        - Volume vs average (alto = institucional)
        - Close position (cerca del extremo = fuerte)
        
        BOS Quality Score:
        - STRONG: body >= 0.8 ATR AND volume >= 1.2x avg AND close in top/bottom 30%
        - NORMAL: body >= 0.4 ATR OR volume >= 1.0x avg
        - WEAK: body < 0.4 ATR AND volume < 1.0x avg
        
        Returns: (score, bos_confirmed)
        """
        if len(df_1m) < 10:
            return 1, False  # Not enough data
        
        # Check if last CLOSED candle confirmed BOS
        last_closed = df_1m.iloc[-2] if len(df_1m) >= 2 else df_1m.iloc[-1]
        prev_candles = df_1m.iloc[-7:-2] if len(df_1m) >= 7 else df_1m.iloc[:-1]
        
        if len(prev_candles) < 3:
            return 1, False
        
        recent_high = prev_candles['high'].max()
        recent_low = prev_candles['low'].min()
        
        bos_confirmed = False
        clean_close = False
        
        # =====================================================================
        # 🔥 BOS QUALITY CALCULATION
        # =====================================================================
        # Calculate ATR for reference
        ranges = (df_1m['high'] - df_1m['low']).tail(20)
        atr_1m = ranges.mean() if len(ranges) >= 10 else ranges.iloc[-1]
        
        # Calculate average volume
        volumes = df_1m['volume'].tail(20)
        avg_volume = volumes.mean() if len(volumes) >= 10 else 1.0
        
        # BOS candle metrics
        bos_body = abs(last_closed['close'] - last_closed['open'])
        bos_range = last_closed['high'] - last_closed['low']
        bos_volume = last_closed.get('volume', avg_volume)
        
        # Body ratio vs ATR (how "convincing" is the candle)
        body_atr_ratio = bos_body / atr_1m if atr_1m > 0 else 0
        
        # Volume ratio vs average
        volume_ratio = bos_volume / avg_volume if avg_volume > 0 else 1.0
        
        if bias == 'LONG':
            # BOS: Close above recent high (not just wick)
            bos_confirmed = last_closed['close'] > recent_high
            # Clean: Close near high of candle (strong)
            if bos_range > 0:
                close_position = (last_closed['close'] - last_closed['low']) / bos_range
                clean_close = close_position > 0.6  # Closed in upper 40%
        else:
            # BOS: Close below recent low
            bos_confirmed = last_closed['close'] < recent_low
            if bos_range > 0:
                close_position = (last_closed['high'] - last_closed['close']) / bos_range
                clean_close = close_position > 0.6  # Closed in lower 40%
        
        # =====================================================================
        # 🔥 BOS QUALITY CLASSIFICATION
        # =====================================================================
        bos_quality = 'WEAK'  # Default
        
        if bos_confirmed:
            # Count quality signals
            quality_signals = 0
            
            # Signal 1: Strong body (>= 0.8 ATR)
            if body_atr_ratio >= 0.8:
                quality_signals += 1
                
            # Signal 2: Above average volume
            if volume_ratio >= 1.2:
                quality_signals += 1
                
            # Signal 3: Clean close (already calculated)
            if clean_close:
                quality_signals += 1
            
            # Classify based on signals
            if quality_signals >= 3:
                bos_quality = 'STRONG'
            elif quality_signals >= 1:
                bos_quality = 'NORMAL'
            else:
                bos_quality = 'WEAK'
            
            # Log BOS quality for analysis
            logger.debug(
                f"📊 BOS QUALITY: {bos_quality} | "
                f"body_atr={body_atr_ratio:.2f}, vol_ratio={volume_ratio:.2f}, "
                f"clean_close={clean_close}, signals={quality_signals}"
            )
            
            # Store in trade tags for post-trade analysis
            self._trade_tags.bos_quality = bos_quality
            self._trade_tags.bos_body_atr = body_atr_ratio
            self._trade_tags.bos_volume_ratio = volume_ratio
        
        # =====================================================================
        # 🔥 SCORING WITH BOS QUALITY
        # =====================================================================
        if bos_confirmed and clean_close:
            if bos_quality == 'STRONG':
                return 2, True  # Perfect timing + STRONG BOS = max score
            elif bos_quality == 'NORMAL':
                return 2, True  # Good timing + decent BOS = full score
            else:  # WEAK
                # BOS confirmed but weak - reduce by 0.5 (effectively 1.5)
                logger.warning(
                    f"⚠️ WEAK BOS detected: body={body_atr_ratio:.2f}ATR, "
                    f"volume={volume_ratio:.2f}x avg | Score capped at 1"
                )
                return 1, True  # WEAK BOS = reduced score
        elif bos_confirmed:
            return 1, True  # BOS confirmed but timing not perfect
        
        # Check if current candle is forming BOS (intra-candle)
        current = df_1m.iloc[-1]
        intra_bos = False
        if bias == 'LONG':
            intra_bos = current['high'] > recent_high
        else:
            intra_bos = current['low'] < recent_low
        
        if intra_bos:
            return 1, False  # Potential BOS forming, not confirmed
        
        return 0, False  # No clear timing signal
    
    def _score_momentum(
        self,
        df_5m: pd.DataFrame,
        bias: str
    ) -> int:
        """
        MOMENTUM SCORE (0-2)
        +2 → ADX HTF strong + slope favorable + volume OK
        +1 → Neutral momentum
        0 → Weak momentum (but does NOT block!)
        
        CAPA 4: Degradation penalties (weights, not gates):
        - Volume low → -0.5 (tracked as penalty, applied via rounding)
        - ADX slope declining fast → -0.25
        """
        if len(df_5m) < 30:
            return 1
        
        closes = df_5m['close'].to_numpy()
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        
        adx, _ = calculate_adx(highs, lows, closes, 14)
        
        if np.isnan(adx[-1]):
            return 1
        
        adx_val = adx[-1]
        
        # ADX slope (momentum direction)
        adx_slope = 0.0
        if len(adx) >= 5 and not np.isnan(adx[-5]):
            adx_slope = adx[-1] - adx[-5]
        
        # Store in trade tags for analysis
        self._trade_tags.adx_5m = float(adx_val)
        self._trade_tags.adx = float(adx_val)  # Keep for backwards compat
        self._trade_tags.adx_slope = float(adx_slope)
        
        # Store ADX array for regime detection (last 10 values)
        adx_values = adx[-10:] if len(adx) >= 10 else adx
        adx_values = adx_values[~np.isnan(adx_values)]
        self._breakdown.adx_values = adx_values
        
        # ADX slope bucket for analysis
        if adx_slope < -0.5:
            self._trade_tags.adx_slope_bucket = 'declining_fast'
        elif adx_slope < -0.15:
            self._trade_tags.adx_slope_bucket = 'declining'
        elif adx_slope < 0.15:
            self._trade_tags.adx_slope_bucket = 'neutral'
        else:
            self._trade_tags.adx_slope_bucket = 'rising'
        
        # Also calculate and store RSI for tags
        rsi = calculate_rsi(closes, 14)
        if not np.isnan(rsi[-1]):
            self._trade_tags.rsi = float(rsi[-1])
        
        # Base score based on ADX strength and slope
        # 🔄 REFACTORED Feb 04: Use ADXThresholds for consistent thresholds
        from src.config.trading_config import ADXThresholds
        base_score = 0.0
        if ADXThresholds.is_strong_trend(adx_val) and adx_slope > 0:  # > 25
            base_score = 2.0  # Strong trend, strengthening
        elif ADXThresholds.is_trending(adx_val) or (adx_val > 15 and adx_slope > 0):  # > 20
            base_score = 1.0  # Moderate trend
        else:
            base_score = 0.0  # Weak (but doesn't block!)
        
        # ===== CAPA 4: Degradation penalties =====
        penalty = 0.0
        
        # Volume penalty (from trade tags if set externally)
        rel_vol = self._trade_tags.rel_vol
        if rel_vol < 0.6:  # Low volume
            penalty += 0.5
            self._trade_tags.volume_penalty = 0.5
            logger.debug(f"📊 Volume penalty: -0.5 (rel_vol={rel_vol:.2f})")
        
        # ADX slope penalty (fast decline = momentum dying)
        if adx_slope < -0.5:
            penalty += 0.25
            self._trade_tags.slope_penalty = 0.25
            logger.debug(f"📊 Slope penalty: -0.25 (slope={adx_slope:.2f})")
        
        # Apply penalty and round to integer
        final_score = max(0, base_score - penalty)
        return int(round(final_score))
    
    def _score_context(
        self,
        df_5m: pd.DataFrame,
        market_time: datetime
    ) -> int:
        """
        CONTEXT SCORE (0-2)
        +1 → Session London/NY
        +1 → Volatility healthy (ATR fast ≈ slow)
        """
        score = 0
        
        # Session check using config helper
        hour = market_time.hour
        minute = market_time.minute
        day_of_week = market_time.weekday()
        session_name = get_session_name(hour, minute, day_of_week)
        
        # Best sessions get point
        if session_name in ['LONDON', 'NY', 'LONDON_NY_OVERLAP']:
            score += 1
            self._trade_tags.session = 'active'
        elif session_name == 'ASIA':
            self._trade_tags.session = 'asia'
        else:
            self._trade_tags.session = 'quiet'
        
        self._trade_tags.session_quality = session_name
        
        # Volatility check
        if len(df_5m) >= 50:
            highs = df_5m['high'].to_numpy()
            lows = df_5m['low'].to_numpy()
            closes = df_5m['close'].to_numpy()
            
            atr_fast = calculate_atr(highs, lows, closes, 14)
            atr_slow = calculate_atr(highs, lows, closes, 42)
            
            if not np.isnan(atr_fast[-1]) and not np.isnan(atr_slow[-1]):
                # Healthy: ATR fast within 70-150% of slow
                ratio = atr_fast[-1] / atr_slow[-1] if atr_slow[-1] > 0 else 1.0
                self._trade_tags.volatility_ratio = float(ratio)
                self._trade_tags.atr = float(atr_fast[-1])
                if 0.7 <= ratio <= 1.5:
                    score += 1
        
        return score
    
    def get_score_size_multiplier(self, score: int) -> float:
        """
        Score-based position sizing multiplier.
        score=5 → 0.8x
        score=6 → 1.0x
        score>=7 → 1.2x
        """
        if score >= 7:
            return 1.2
        elif score >= 6:
            return 1.0
        else:  # score = 5
            return 0.8


# Singleton instance
_entry_scorer: Optional[EntryScorer] = None


def get_entry_scorer() -> EntryScorer:
    """Get global EntryScorer instance."""
    global _entry_scorer
    if _entry_scorer is None:
        _entry_scorer = EntryScorer()
    return _entry_scorer
