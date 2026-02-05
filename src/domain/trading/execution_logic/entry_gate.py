"""
Entry Gate System - Unified decision framework for trade entries

This module implements the hard/soft gate separation and probe size system
to fix the "missed winners" problem while maintaining proper risk control.

ARCHITECTURE:
    1. HARD GATES (must pass, non-negotiable):
       - SpreadFilter (spread anomaly)
       - Volatility shock / chaos
       - Data freshness / stale data
       - Macro trend breaker (HTF veto for direction)
       - Max trades per session / cooldown
       
    2. SOFT GATES (adjust sizing, don't block):
       - Score threshold (marginal scores = probe size)
       - RR assessment (marginal RR = probe size)
       - Micro filters (LTF confirmations)
       - Session quality
       - Funding rate (marginal = size reduction)

    3. SIZING OUTPUT:
       - Full size (1.0): All gates green
       - Probe size (0.25-0.5): Hard gates pass, soft gates marginal
       - No trade (0.0): Hard gate fails

PRINCIPLE: "edge_detected + hard gates pass = trade allowed (size varies)"

Created: January 20, 2026
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class GateType(str, Enum):
    """Gate classification."""
    HARD = "HARD"      # Must pass or trade blocked
    SOFT = "SOFT"      # Can reduce size but doesn't block


class GateCategory(str, Enum):
    """Gate categories for reason tree hierarchy."""
    TECHNICAL = "TECHNICAL"           # Spread, data, chaos - Level 1
    STRATEGIC = "STRATEGIC"           # Macro veto, max trades - Level 2
    EDGE = "EDGE"                     # Setup quality - Level 3
    SIZING = "SIZING"                 # Score, RR, session - Level 4
    EXECUTION = "EXECUTION"           # Order type, offsets - Level 5


@dataclass
class GateResult:
    """Result from a single gate evaluation."""
    name: str
    gate_type: GateType
    category: GateCategory
    passed: bool
    size_multiplier: float = 1.0      # 1.0 = full, 0.5 = half, 0.25 = probe
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        status = "✅" if self.passed else "❌"
        mult = f" ({self.size_multiplier:.0%})" if self.size_multiplier < 1.0 and self.passed else ""
        return f"{status} [{self.gate_type.value}:{self.category.value}] {self.name}{mult}: {self.reason}"


@dataclass
class EntryDecision:
    """Final entry decision with full context."""
    can_trade: bool
    size_multiplier: float
    primary_reason: str
    gate_results: List[GateResult]
    is_probe: bool = False
    
    # Detailed breakdown
    hard_gates_passed: bool = True
    soft_gates_score: float = 1.0      # 0-1, average of soft gate multipliers
    
    # Tags for analysis
    tags: Dict[str, Any] = field(default_factory=dict)
    
    def get_block_reason(self) -> Optional[str]:
        """Get the primary blocking reason if blocked."""
        if self.can_trade:
            return None
        for result in self.gate_results:
            if not result.passed and result.gate_type == GateType.HARD:
                return f"{result.name}: {result.reason}"
        return self.primary_reason
    
    def get_size_adjustments(self) -> List[str]:
        """Get list of reasons for size reduction."""
        adjustments = []
        for result in self.gate_results:
            if result.passed and result.size_multiplier < 1.0:
                adjustments.append(f"{result.name}: {result.size_multiplier:.0%}")
        return adjustments


class EntryGateEvaluator:
    """
    Evaluates all entry gates and produces a unified decision.
    
    Usage:
        evaluator = EntryGateEvaluator()
        decision = evaluator.evaluate(
            spread=current_spread,
            spread_threshold=normal_spread,
            volatility_shock=False,
            data_age_seconds=5,
            htf_aligned=True,
            session_trades=2,
            max_session_trades=5,
            score=5.5,
            score_threshold=5.0,
            rr_net=0.85,
            rr_floor=0.6,
            ltf_confirmations=2,
            ltf_required=2,
            ...
        )
        
        if decision.can_trade:
            position_size = base_size * decision.size_multiplier
            if decision.is_probe:
                logger.info("Entering with probe size")
    """
    
    # =========================================================================
    # SCORING THRESHOLDS FOR SOFT GATES
    # 🔥 FIX (Feb 01, 2026): Raised based on 62-trade empirical analysis
    # Data shows score < 4.5 has 0% win rate → should be BLOCKED
    # =========================================================================
    
    # Score-based sizing (replaces binary threshold)
    SCORE_BLOCK_FLOOR = 4.5           # 🔥 Raised from 3.5: score<4.5 = 0% win rate
    SCORE_PROBE_MAX = 5.0             # 4.5-5.0 = probe (0.25x) - marginal edge
    SCORE_REDUCED_MAX = 5.5           # 5.0-5.5 = reduced (0.5x)
    # Above 5.5 = full size
    
    # RR-based sizing
    RR_BLOCK_FLOOR = 0.5              # Below 0.5 = mathematical roulette (was 0.6)
    RR_PROBE_MAX = 0.8                # 0.5-0.8 = probe size (was 1.0)
    RR_FULL_MIN = 1.0                 # Above 1.0 = full size (was 1.2)
    
    # LTF confirmations
    LTF_MAX_REQUIRED = 2              # Never require 3/3 confirmations
    
    def __init__(self,
                 score_block_floor: float | None = None,
                 rr_block_floor: float | None = None):
        """Initialize with optional custom thresholds."""
        if score_block_floor is not None:
            self.SCORE_BLOCK_FLOOR = score_block_floor
        if rr_block_floor is not None:
            self.RR_BLOCK_FLOOR = rr_block_floor
    
    def evaluate(self,
                 # HARD GATE inputs
                 spread: float = 0,
                 spread_threshold: float = 50,        # Normal spread threshold
                 spread_extreme: float = 100,         # Extreme spread = block
                 volatility_shock: bool = False,
                 data_age_seconds: float = 0,
                 data_stale_threshold: float = 30,
                 htf_aligned: bool = True,
                 session_trades: int = 0,
                 max_session_trades: int = 5,
                 in_cooldown: bool = False,
                 
                 # SOFT GATE inputs
                 score: float = 5.0,
                 score_threshold: float = 5.0,        # Reference only
                 rr_net: float = 1.2,
                 rr_ideal: float = 1.5,
                 ltf_confirmations: int = 2,
                 ltf_total: int = 3,
                 session_quality: float = 1.0,        # 0-1
                 funding_penalty: float = 0.0,        # 0-1 size reduction
                 
                 # Context
                 edge_detected: bool = True,          # Setup was found
                 regime: str = "NORMAL",              # RANGE, TREND, CHAOS
                 
                 **kwargs) -> EntryDecision:
        """
        Evaluate all gates and return unified decision.
        
        Returns:
            EntryDecision with can_trade, size_multiplier, and detailed breakdown
        """
        results: List[GateResult] = []
        tags: Dict[str, Any] = {}
        
        # =====================================================================
        # HARD GATES (Level 1-2: TECHNICAL + STRATEGIC)
        # =====================================================================
        
        # 1. SPREAD FILTER (HARD - TECHNICAL)
        spread_result = self._evaluate_spread_gate(
            spread, spread_threshold, spread_extreme
        )
        results.append(spread_result)
        tags['spread'] = spread
        tags['spread_gate'] = spread_result.passed
        
        # 2. VOLATILITY SHOCK (HARD - TECHNICAL)
        shock_result = self._evaluate_volatility_shock_gate(volatility_shock)
        results.append(shock_result)
        tags['volatility_shock'] = volatility_shock
        
        # 3. DATA FRESHNESS (HARD - TECHNICAL)
        data_result = self._evaluate_data_freshness_gate(
            data_age_seconds, data_stale_threshold
        )
        results.append(data_result)
        tags['data_age'] = data_age_seconds
        
        # 4. HTF ALIGNMENT (HARD - STRATEGIC)
        htf_result = self._evaluate_htf_gate(htf_aligned)
        results.append(htf_result)
        tags['htf_aligned'] = htf_aligned
        
        # 5. SESSION TRADE LIMIT (HARD - STRATEGIC)
        session_result = self._evaluate_session_limit_gate(
            session_trades, max_session_trades, score  # High score can override
        )
        results.append(session_result)
        tags['session_trades'] = session_trades
        tags['max_session_trades'] = max_session_trades
        
        # 6. COOLDOWN (HARD - STRATEGIC)
        cooldown_result = self._evaluate_cooldown_gate(in_cooldown)
        results.append(cooldown_result)
        tags['in_cooldown'] = in_cooldown
        
        # Check if all hard gates passed
        hard_gates = [r for r in results if r.gate_type == GateType.HARD]
        hard_gates_passed = all(r.passed for r in hard_gates)
        
        if not hard_gates_passed:
            # Find primary blocker (first failed hard gate)
            blocker = next((r for r in hard_gates if not r.passed), None)
            return EntryDecision(
                can_trade=False,
                size_multiplier=0.0,
                primary_reason=blocker.reason if blocker else "Hard gate failed",
                gate_results=results,
                hard_gates_passed=False,
                tags=tags
            )
        
        # =====================================================================
        # SOFT GATES (Level 3-4: EDGE + SIZING)
        # =====================================================================
        
        # 7. EDGE DETECTION (SOFT - EDGE)
        edge_result = self._evaluate_edge_gate(edge_detected, regime)
        results.append(edge_result)
        tags['edge_detected'] = edge_detected
        
        # 8. SCORE (SOFT - SIZING) - Graduated sizing
        score_result = self._evaluate_score_gate(score, score_threshold, regime)
        results.append(score_result)
        tags['score'] = score
        tags['score_multiplier'] = score_result.size_multiplier
        
        # 9. RR ASSESSMENT (SOFT - SIZING) - Graduated sizing
        rr_result = self._evaluate_rr_gate(rr_net, rr_ideal, regime)
        results.append(rr_result)
        tags['rr_net'] = rr_net
        tags['rr_multiplier'] = rr_result.size_multiplier
        
        # 10. LTF CONFIRMATIONS (SOFT - SIZING)
        ltf_result = self._evaluate_ltf_gate(ltf_confirmations, ltf_total)
        results.append(ltf_result)
        tags['ltf_confirmations'] = ltf_confirmations
        tags['ltf_total'] = ltf_total
        
        # 11. SESSION QUALITY (SOFT - SIZING)
        quality_result = self._evaluate_session_quality_gate(session_quality)
        results.append(quality_result)
        tags['session_quality'] = session_quality
        
        # 12. FUNDING RATE (SOFT - SIZING)
        funding_result = self._evaluate_funding_gate(funding_penalty)
        results.append(funding_result)
        tags['funding_penalty'] = funding_penalty
        
        # =====================================================================
        # CALCULATE FINAL SIZE MULTIPLIER
        # =====================================================================
        
        soft_gates = [r for r in results if r.gate_type == GateType.SOFT]
        
        # Calculate size multiplier (product of all soft gate multipliers)
        size_multiplier = 1.0
        for gate in soft_gates:
            size_multiplier *= gate.size_multiplier
        
        # Check for soft gate blocks (score or RR below absolute floor)
        soft_block = any(not r.passed for r in soft_gates)
        if soft_block:
            # Soft gate hard block (e.g., score < 4.5 or RR < 0.6)
            blocker = next((r for r in soft_gates if not r.passed), None)
            return EntryDecision(
                can_trade=False,
                size_multiplier=0.0,
                primary_reason=blocker.reason if blocker else "Soft gate floor breached",
                gate_results=results,
                hard_gates_passed=True,
                soft_gates_score=0.0,
                tags=tags
            )
        
        # Determine if this is a probe trade
        is_probe = size_multiplier <= 0.3
        
        # Ensure minimum probe size (0.25) if trading
        if size_multiplier > 0 and size_multiplier < 0.25:
            size_multiplier = 0.25
        
        # Calculate soft gates average score for analysis
        soft_multipliers = [r.size_multiplier for r in soft_gates if r.passed]
        soft_gates_score = sum(soft_multipliers) / len(soft_multipliers) if soft_multipliers else 1.0
        
        # Primary reason (most restrictive soft gate)
        most_restrictive = min(soft_gates, key=lambda r: r.size_multiplier)
        if most_restrictive.size_multiplier < 1.0:
            primary_reason = f"Size adjusted: {most_restrictive.name} ({most_restrictive.size_multiplier:.0%})"
        else:
            primary_reason = "All gates passed - full size"
        
        return EntryDecision(
            can_trade=True,
            size_multiplier=round(size_multiplier, 2),
            primary_reason=primary_reason,
            gate_results=results,
            is_probe=is_probe,
            hard_gates_passed=True,
            soft_gates_score=round(soft_gates_score, 2),
            tags=tags
        )
    
    # =========================================================================
    # HARD GATE EVALUATORS
    # =========================================================================
    
    def _evaluate_spread_gate(
        self, 
        spread: float, 
        threshold: float, 
        extreme: float
    ) -> GateResult:
        """Evaluate spread gate - HARD."""
        if spread >= extreme:
            return GateResult(
                name="Spread",
                gate_type=GateType.HARD,
                category=GateCategory.TECHNICAL,
                passed=False,
                reason=f"Extreme spread: {spread:.1f} >= {extreme:.1f}",
                details={'spread': spread, 'extreme': extreme}
            )
        elif spread >= threshold:
            # Elevated but not extreme - reduce size
            reduction = min(0.5, (spread - threshold) / (extreme - threshold))
            multiplier = 1.0 - reduction
            return GateResult(
                name="Spread",
                gate_type=GateType.HARD,
                category=GateCategory.TECHNICAL,
                passed=True,
                size_multiplier=max(0.5, multiplier),
                reason=f"Elevated spread: {spread:.1f}",
                details={'spread': spread, 'threshold': threshold}
            )
        else:
            return GateResult(
                name="Spread",
                gate_type=GateType.HARD,
                category=GateCategory.TECHNICAL,
                passed=True,
                reason=f"Spread OK: {spread:.1f}"
            )
    
    def _evaluate_volatility_shock_gate(self, shock: bool) -> GateResult:
        """Evaluate volatility shock gate - HARD."""
        if shock:
            return GateResult(
                name="VolatilityShock",
                gate_type=GateType.HARD,
                category=GateCategory.TECHNICAL,
                passed=False,
                reason="Volatility shock detected - market chaos"
            )
        return GateResult(
            name="VolatilityShock",
            gate_type=GateType.HARD,
            category=GateCategory.TECHNICAL,
            passed=True,
            reason="No volatility shock"
        )
    
    def _evaluate_data_freshness_gate(
        self, 
        age_seconds: float, 
        threshold: float
    ) -> GateResult:
        """Evaluate data freshness gate - HARD."""
        if age_seconds > threshold:
            return GateResult(
                name="DataFreshness",
                gate_type=GateType.HARD,
                category=GateCategory.TECHNICAL,
                passed=False,
                reason=f"Stale data: {age_seconds:.0f}s > {threshold:.0f}s"
            )
        return GateResult(
            name="DataFreshness",
            gate_type=GateType.HARD,
            category=GateCategory.TECHNICAL,
            passed=True,
            reason=f"Data fresh: {age_seconds:.0f}s"
        )
    
    def _evaluate_htf_gate(self, aligned: bool) -> GateResult:
        """Evaluate HTF alignment gate - HARD."""
        if not aligned:
            return GateResult(
                name="HTFAlignment",
                gate_type=GateType.HARD,
                category=GateCategory.STRATEGIC,
                passed=False,
                reason="Trade direction against HTF bias"
            )
        return GateResult(
            name="HTFAlignment",
            gate_type=GateType.HARD,
            category=GateCategory.STRATEGIC,
            passed=True,
            reason="HTF aligned"
        )
    
    def _evaluate_session_limit_gate(
        self, 
        trades: int, 
        max_trades: int,
        score: float
    ) -> GateResult:
        """Evaluate session trade limit gate - HARD with score override."""
        if trades >= max_trades:
            # Allow override if score is excellent (>= 7)
            if score >= 7.0:
                return GateResult(
                    name="SessionLimit",
                    gate_type=GateType.HARD,
                    category=GateCategory.STRATEGIC,
                    passed=True,
                    size_multiplier=0.5,  # Reduced size on override
                    reason=f"Session limit ({trades}/{max_trades}) overridden by score {score:.1f}",
                    details={'trades': trades, 'max': max_trades, 'override_score': score}
                )
            return GateResult(
                name="SessionLimit",
                gate_type=GateType.HARD,
                category=GateCategory.STRATEGIC,
                passed=False,
                reason=f"Session limit reached: {trades}/{max_trades}"
            )
        return GateResult(
            name="SessionLimit",
            gate_type=GateType.HARD,
            category=GateCategory.STRATEGIC,
            passed=True,
            reason=f"Session trades OK: {trades}/{max_trades}"
        )
    
    def _evaluate_cooldown_gate(self, in_cooldown: bool) -> GateResult:
        """Evaluate cooldown gate - HARD."""
        if in_cooldown:
            return GateResult(
                name="Cooldown",
                gate_type=GateType.HARD,
                category=GateCategory.STRATEGIC,
                passed=False,
                reason="In cooldown period"
            )
        return GateResult(
            name="Cooldown",
            gate_type=GateType.HARD,
            category=GateCategory.STRATEGIC,
            passed=True,
            reason="No cooldown active"
        )
    
    # =========================================================================
    # SOFT GATE EVALUATORS
    # =========================================================================
    
    def _evaluate_edge_gate(self, detected: bool, regime: str) -> GateResult:
        """Evaluate edge detection gate - SOFT."""
        if not detected:
            return GateResult(
                name="EdgeDetection",
                gate_type=GateType.SOFT,
                category=GateCategory.EDGE,
                passed=True,  # Soft gate doesn't block, just reduces
                size_multiplier=0.0,  # But multiplier is 0
                reason="No edge detected"
            )
        return GateResult(
            name="EdgeDetection",
            gate_type=GateType.SOFT,
            category=GateCategory.EDGE,
            passed=True,
            reason=f"Edge detected in {regime} regime"
        )
    
    def _evaluate_score_gate(
        self, 
        score: float, 
        threshold: float,
        regime: str
    ) -> GateResult:
        """
        Evaluate score gate with graduated sizing - SOFT.
        
        Sizing (updated Jan 24, 2026):
        - score < 3.5: Block (floor breach)
        - 3.5 <= score < 4.5: Probe (0.25x)
        - 4.5 <= score < 5.5: Reduced (0.5x)
        - score >= 5.5: Full (1.0x)
        """
        if score < self.SCORE_BLOCK_FLOOR:
            return GateResult(
                name="Score",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=False,  # Hard floor breach
                reason=f"Score {score:.1f} < floor {self.SCORE_BLOCK_FLOOR}"
            )
        elif score < self.SCORE_PROBE_MAX:
            return GateResult(
                name="Score",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.25,
                reason=f"Score {score:.1f} in probe zone (3.5-4.5)",
                details={'score': score, 'zone': 'probe'}
            )
        elif score < self.SCORE_REDUCED_MAX:
            return GateResult(
                name="Score",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.5,
                reason=f"Score {score:.1f} in reduced zone (4.5-5.5)",
                details={'score': score, 'zone': 'reduced'}
            )
        else:
            return GateResult(
                name="Score",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                reason=f"Score {score:.1f} excellent (>= 5.5)",
                details={'score': score, 'zone': 'full'}
            )
    
    def _evaluate_rr_gate(
        self, 
        rr_net: float, 
        rr_ideal: float,
        regime: str
    ) -> GateResult:
        """
        Evaluate RR gate with graduated sizing - SOFT.
        
        Sizing:
        - rr_net < 0.6: Block (mathematical roulette)
        - 0.6 <= rr_net < 1.0: Probe (0.25x) + require confirmation
        - 1.0 <= rr_net < 1.2: Reduced (0.5x)
        - rr_net >= 1.2: Full (1.0x)
        
        Note: In RANGE regime, lower RR thresholds are acceptable
        due to higher winrate mean-reversion.
        """
        # Adjust floors for RANGE regime
        block_floor = self.RR_BLOCK_FLOOR
        probe_max = self.RR_PROBE_MAX
        
        if regime == "RANGE":
            # RANGE regime can accept lower RR due to high winrate
            block_floor = 0.5
            probe_max = 0.8
        
        if rr_net < block_floor:
            return GateResult(
                name="RiskReward",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=False,
                reason=f"RR {rr_net:.2f} < floor {block_floor} (mathematical roulette)"
            )
        elif rr_net < probe_max:
            return GateResult(
                name="RiskReward",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.25,
                reason=f"RR {rr_net:.2f} in probe zone ({block_floor}-{probe_max})",
                details={'rr_net': rr_net, 'zone': 'probe'}
            )
        elif rr_net < self.RR_FULL_MIN:
            return GateResult(
                name="RiskReward",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.5,
                reason=f"RR {rr_net:.2f} in reduced zone ({probe_max}-{self.RR_FULL_MIN})",
                details={'rr_net': rr_net, 'zone': 'reduced'}
            )
        else:
            return GateResult(
                name="RiskReward",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                reason=f"RR {rr_net:.2f} good (>= {self.RR_FULL_MIN})",
                details={'rr_net': rr_net, 'zone': 'full'}
            )
    
    def _evaluate_ltf_gate(
        self, 
        confirmations: int, 
        total: int
    ) -> GateResult:
        """
        Evaluate LTF confirmations gate - SOFT.
        
        Rules:
        - Never require 3/3 confirmations
        - 2/3 or better = full size
        - 1/3 = probe size
        - 0/3 = reduced size (but don't block if edge strong)
        """
        effective_required = min(self.LTF_MAX_REQUIRED, total - 1)  # Never require all
        
        if confirmations >= effective_required:
            return GateResult(
                name="LTFConfirmations",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                reason=f"LTF confirmations OK: {confirmations}/{total}",
                details={'confirmations': confirmations, 'required': effective_required}
            )
        elif confirmations >= 1:
            return GateResult(
                name="LTFConfirmations",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.5,
                reason=f"LTF partial: {confirmations}/{total} (need {effective_required})",
                details={'confirmations': confirmations, 'required': effective_required}
            )
        else:
            return GateResult(
                name="LTFConfirmations",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.25,
                reason=f"LTF weak: {confirmations}/{total}",
                details={'confirmations': confirmations, 'required': effective_required}
            )
    
    def _evaluate_session_quality_gate(self, quality: float) -> GateResult:
        """Evaluate session quality gate - SOFT."""
        if quality >= 0.9:
            return GateResult(
                name="SessionQuality",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                reason=f"Session quality excellent: {quality:.0%}"
            )
        elif quality >= 0.7:
            return GateResult(
                name="SessionQuality",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.8,
                reason=f"Session quality good: {quality:.0%}"
            )
        elif quality >= 0.5:
            return GateResult(
                name="SessionQuality",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.6,
                reason=f"Session quality fair: {quality:.0%}"
            )
        else:
            return GateResult(
                name="SessionQuality",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=0.5,
                reason=f"Session quality poor: {quality:.0%}"
            )
    
    def _evaluate_funding_gate(self, penalty: float) -> GateResult:
        """Evaluate funding rate gate - SOFT."""
        if penalty <= 0:
            return GateResult(
                name="FundingRate",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                reason="Funding favorable"
            )
        else:
            multiplier = max(0.5, 1.0 - penalty)
            return GateResult(
                name="FundingRate",
                gate_type=GateType.SOFT,
                category=GateCategory.SIZING,
                passed=True,
                size_multiplier=multiplier,
                reason=f"Funding penalty: {penalty:.0%}",
                details={'penalty': penalty}
            )


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_evaluator: Optional[EntryGateEvaluator] = None


def get_entry_gate_evaluator(**kwargs) -> EntryGateEvaluator:
    """Get or create the entry gate evaluator singleton."""
    global _evaluator
    if _evaluator is None:
        _evaluator = EntryGateEvaluator(**kwargs)
    return _evaluator


def reset_entry_gate_evaluator():
    """Reset the singleton."""
    global _evaluator
    _evaluator = None


def evaluate_entry_gates(**kwargs) -> EntryDecision:
    """Convenience function to evaluate entry gates."""
    evaluator = get_entry_gate_evaluator()
    return evaluator.evaluate(**kwargs)
