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

from src.engine.indicators import (
    calculate_ema,
    calculate_adx,
    calculate_atr,
    calculate_rsi,
)
from src.engine.config import OperatingMode, get_session_name

if TYPE_CHECKING:
    from src.engine.filters import EntryFilterPipeline
    from src.engine.filters.filter_interface import FilterDecision
    from src.engine.filters import MicroTrendState

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
    penalty_hierarchy: Dict[str, Any] = field(default_factory=dict)
    adx_values: np.ndarray = field(default_factory=lambda: np.array([]))
    
    @property
    def total(self) -> int:
        """Calculate total score."""
        base = self.structure + self.timing + self.momentum + self.context + self.synergy
        return max(0, int(base + self.micro_trend_adj + self.filter_pipeline_adj))
    
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
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {k: v for k, v in self.__dict__.items()}


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
        
        Returns:
            (total_score, breakdown_dict, structure_blocks)
            If structure_blocks=True, DO NOT ENTER regardless of score.
        """
        # Reset state
        self._breakdown = ScoreBreakdown()
        self._trade_tags = TradeTags(mode=OperatingMode.MODE)
        
        # 1️⃣ STRUCTURE (0-2) - THE ONLY GATE
        structure_score, structure_blocks, htf_clean = self._score_structure(
            df_1h, df_5m, bias, current_price
        )
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
        
        # Update final score in tags
        self._trade_tags.score = self._breakdown.total
        
        return self._breakdown.total, self._breakdown.to_dict(), False
    
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
            worst_context_source = min(context_penalties, key=context_penalties.get)
            worst_context = context_penalties[worst_context_source]
        
        worst_execution = 0.0
        worst_execution_source = None
        if execution_penalties:
            worst_execution_source = min(execution_penalties, key=execution_penalties.get)
            worst_execution = execution_penalties[worst_execution_source]
        
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
            from src.engine.regime.regime_detector import EntryRegime
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
        
        Returns: (score, blocks_entry, htf_is_clean)
        """
        if len(df_1h) < 50 or len(df_5m) < 20:
            return 1, False, False  # Not enough data, allow with partial score
        
        # HTF alignment check
        closes_1h = df_1h['close'].values
        ema50_1h = calculate_ema(closes_1h, 50)
        
        if np.isnan(ema50_1h[-1]):
            return 1, False, False
        
        htf_aligned = False
        if bias == 'LONG':
            htf_aligned = current_price > ema50_1h[-1]
        else:
            htf_aligned = current_price < ema50_1h[-1]
        
        if not htf_aligned:
            # Against HTF → Structure = 0 → BLOCKS
            logger.warning(f"⛔ STRUCTURE GATE: {bias} against HTF (Price vs EMA50_1h)")
            return 0, True, False
        
        # 5m structure check (price vs EMA20)
        closes_5m = df_5m['close'].values
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
            return 1, False, False  # HTF ok, LTF partial
        
        return 0, True, False  # Should not reach here
    
    def _score_timing(
        self,
        df_1m: pd.DataFrame,
        bias: str
    ) -> Tuple[int, bool]:
        """
        TIMING SCORE (0-2)
        +2 → BOS confirmed at CLOSE + clean reclaim
        +1 → BOS + late timing
        0 → Signal intra-candle / ambiguous
        
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
        
        if bias == 'LONG':
            # BOS: Close above recent high (not just wick)
            bos_confirmed = last_closed['close'] > recent_high
            # Clean: Close near high of candle (strong)
            candle_range = last_closed['high'] - last_closed['low']
            if candle_range > 0:
                close_position = (last_closed['close'] - last_closed['low']) / candle_range
                clean_close = close_position > 0.6  # Closed in upper 40%
        else:
            # BOS: Close below recent low
            bos_confirmed = last_closed['close'] < recent_low
            candle_range = last_closed['high'] - last_closed['low']
            if candle_range > 0:
                close_position = (last_closed['high'] - last_closed['close']) / candle_range
                clean_close = close_position > 0.6  # Closed in lower 40%
        
        if bos_confirmed and clean_close:
            return 2, True  # Perfect timing, BOS confirmed
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
        
        closes = df_5m['close'].values
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        
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
        base_score = 0.0
        if adx_val > 25 and adx_slope > 0:
            base_score = 2.0  # Strong trend, strengthening
        elif adx_val > 20 or (adx_val > 15 and adx_slope > 0):
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
            highs = df_5m['high'].values
            lows = df_5m['low'].values
            closes = df_5m['close'].values
            
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
