"""
Direction Orchestrator - Unified direction determination

HIERARCHY (highest to lowest priority):
1. EXPANSION_MODE (100) - Strong displacement detected, direction locked
2. EDGE_BREAKOUT (100) - Edge with sovereign authority
3. DISPLACEMENT (100) - Significant price movement (>= threshold ATR)
4. ANTICIPATION (100) - Pre-breakout detection, 100% accuracy
5. TRANSITION_MODE (75) - BIAS FLIP direction override
6. STRUCTURAL_BIAS (50) - EMA-based HTF bias (needs confirmation)
7. FORBIDDEN_FALLBACK (10) - Last resort when nothing else available

RULES:
- CORE signals (100) bypass forbidden_direction
- STRUCTURAL_BIAS alone is NOT actionable (HTF_BIAS_ALONE veto)
- Location extremes veto incompatible direction unless breakout confirmed
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
import logging

from src.config.trading_config import RiskManagement

logger = logging.getLogger(__name__)


class SignalPriority(Enum):
    """
    Priority levels for direction signals.
    
    - CORE (100): DISPLACEMENT, EXPANSION_MODE, EDGE_BREAKOUT, ANTICIPATION → determines direction
    - TRANSITION (75): TRANSITION_MODE → overrides structural bias after BIAS FLIP
    - CONFIRMER (50): STRUCTURAL_BIAS → confirms but needs another signal
    - FALLBACK (10): FORBIDDEN_FALLBACK → last resort
    """
    # CORE LEVEL (100) - These DETERMINE direction (only one needs to fire)
    EXPANSION_MODE = 100      # Strong displacement with lock - BYPASSES ALL
    EDGE_BREAKOUT = 100       # Same level as EXPANSION (sovereign)
    DISPLACEMENT = 100        # Same level as EXPANSION (sovereign)
    ANTICIPATION = 100        # 🔥 RESTORED: 100% accuracy = CORE signal
    
    # TRANSITION LEVEL (75) - BIAS FLIP direction overrides structural bias
    # When BIAS FLIP sets transition_mode_direction, it MUST
    # override structural_bias. Otherwise the system reverts to LONG after
    # SHORT edge expires, causing infinite CONSENSUS BLOCKED spam.
    TRANSITION_MODE = 75
    
    # CONFIRMER LEVEL (50) - These CONFIRM but don't override CORE
    STRUCTURAL_BIAS = 50      # HTF EMA-based bias
    
    # ADVISORY LEVEL (10) - Computed but never determine direction
    FORBIDDEN_FALLBACK = 10   # Last resort when nothing else available
    NONE = 0                  # No signal


@dataclass
class DirectionSignal:
    """Represents a direction signal from any source."""
    source: str                    # Signal source name
    direction: Optional[str]       # 'LONG', 'SHORT', or None
    priority: SignalPriority       # Priority level
    confidence: float = 1.0        # 0.0 to 1.0
    reason: str = ""               # Human-readable reason
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __lt__(self, other: 'DirectionSignal') -> bool:
        """Compare by priority for sorting."""
        return self.priority.value < other.priority.value


@dataclass 
class OrchestrationResult:
    """Result of direction orchestration."""
    final_direction: Optional[str]        # The chosen direction
    winning_signal: Optional[DirectionSignal]  # Signal that won
    vetoed_by: Optional[str] = None       # If vetoed, by what
    all_signals: List[DirectionSignal] = field(default_factory=list)
    conflict_detected: bool = False       # True if signals conflicted
    conflict_details: str = ""            # Description of conflict
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags for logging."""
        tags = {
            'orchestrated_direction': self.final_direction,
            'direction_source': self.winning_signal.source if self.winning_signal else 'NONE',
            'direction_priority': self.winning_signal.priority.name if self.winning_signal else 'NONE',
            'direction_confidence': self.winning_signal.confidence if self.winning_signal else 0.0,
        }
        if self.vetoed_by:
            tags['direction_vetoed_by'] = self.vetoed_by
        if self.conflict_detected:
            tags['direction_conflict'] = True
            tags['direction_conflict_details'] = self.conflict_details
        return tags


class DirectionOrchestrator:
    """
    Central orchestrator for all direction signals.
    
    Collects signals from multiple sources, applies priority rules,
    and outputs a single coherent direction decision.
    
    HIERARCHY:
    - CORE (100): EXPANSION_MODE, EDGE_BREAKOUT, DISPLACEMENT, ANTICIPATION
    - TRANSITION (75): TRANSITION_MODE (from BIAS FLIP)
    - CONFIRMER (50): STRUCTURAL_BIAS
    - FALLBACK (10): FORBIDDEN_FALLBACK (only when no other signal)
    """
    
    def __init__(self):
        self._last_result: Optional[OrchestrationResult] = None
        self._signal_history: List[DirectionSignal] = []
    
    def orchestrate(self,
                    # Expansion mode signals (Priority 100 - HIGHEST)
                    expansion_mode: bool = False,
                    expansion_direction: Optional[str] = None,
                    direction_lock: Optional[str] = None,
                    
                    # Edge signals (Priority 95 - SOVEREIGN AUTHORITY)
                    edge_active: bool = False,
                    edge_direction: Optional[str] = None,
                    edge_type: Optional[str] = None,  # 'BREAKOUT', 'DISPLACEMENT', 'TREND'
                    edge_confidence: float = 1.0,
                    
                    # Displacement signals (Priority 90)
                    displacement_detected: bool = False,
                    displacement_direction: Optional[str] = None,
                    strong_displacement: bool = False,
                    
                    # Anticipation signals (Priority 85)
                    # Added confidence for high-confidence bypass
                    anticipation_pending: bool = False,
                    anticipation_direction: Optional[str] = None,
                    anticipation_confidence: float = 0.0,
                    
                    # TRANSITION_MODE direction (Priority 75)
                    # When BIAS FLIP sets transition_mode, this direction MUST
                    # override structural_bias to prevent reverting to stale HTF bias
                    transition_mode_active: bool = False,
                    transition_mode_direction: Optional[str] = None,
                    
                    # Structural signals (Priority 50)
                    structural_bias: Optional[str] = None,
                    
                    # Veto signals
                    forbidden_direction: Optional[str] = None,
                    
                    # 🔥 REFACTOR: Location context for extreme veto (moved from _get_effective_direction)
                    location_context: Optional[Any] = None,
                    
                    # ADX context for trend strength assessment
                    htf_adx: float = 0.0,
                    
                    # Macro context for preventing dead-edge deadlocks
                    macro_bias_strength: Optional[str] = None,
                    
                    # Context
                    tick_count: int = 0) -> OrchestrationResult:
        """
        Orchestrate all direction signals into a single decision.
        
        Returns:
            OrchestrationResult with final direction and metadata
        """
        signals: List[DirectionSignal] = []
        
        # =====================================================================
        # COLLECT ALL SIGNALS
        # =====================================================================
        
        # 1. EXPANSION_MODE - Highest priority (100)
        if expansion_mode and expansion_direction:
            signals.append(DirectionSignal(
                source='EXPANSION_MODE',
                direction=expansion_direction,
                priority=SignalPriority.EXPANSION_MODE,
                confidence=1.0,
                reason=f"Expansion mode active with direction lock",
                metadata={'direction_lock': direction_lock}
            ))
        
        # 2. EDGE_BREAKOUT - Sovereign authority (95)
        # Edges have "sovereign authority" - only EXPANSION can override
        if edge_active and edge_direction:
            signals.append(DirectionSignal(
                source='EDGE_BREAKOUT',
                direction=edge_direction,
                priority=SignalPriority.EDGE_BREAKOUT,
                confidence=edge_confidence,
                reason=f"Edge {edge_type or 'ACTIVE'} with sovereign authority",
                metadata={'edge_type': edge_type, 'sovereign': True}
            ))
        
        # 3. DISPLACEMENT - Large price movement (90)
        if displacement_detected and displacement_direction:
            conf = 1.0 if strong_displacement else 0.8
            signals.append(DirectionSignal(
                source='DISPLACEMENT',
                direction=displacement_direction,
                priority=SignalPriority.DISPLACEMENT,
                confidence=conf,
                reason=f"{'Strong' if strong_displacement else 'Normal'} displacement detected",
                metadata={'strong': strong_displacement}
            ))
        
        # 4. ANTICIPATION - CORE signal (100) - 100% accuracy confirmed by user
        # Use actual confidence and mark high-confidence for bypass
        if anticipation_pending and anticipation_direction:
            actual_conf = anticipation_confidence if anticipation_confidence > 0 else 0.55
            is_high_confidence = actual_conf >= 0.70
            signals.append(DirectionSignal(
                source='ANTICIPATION',
                direction=anticipation_direction,
                priority=SignalPriority.ANTICIPATION,
                confidence=actual_conf,
                reason=f"Anticipation {actual_conf:.0%} conf" + (" [HIGH]" if is_high_confidence else ""),
                metadata={'pending': True, 'high_confidence': is_high_confidence, 'can_bypass_forbidden': is_high_confidence}
            ))
        
        # 5. TRANSITION_MODE - BIAS FLIP direction (Priority 75)
        # When BIAS FLIP activates transition_mode, this direction
        # MUST override structural_bias. Without this, after SHORT edge expires,
        # structural_bias=LONG takes over → arm_consensus_edge(LONG) → CONSENSUS
        # BLOCKED by macro=BEAR_STRONG → infinite spam, system paralyzed.
        if transition_mode_active and transition_mode_direction:
            signals.append(DirectionSignal(
                source='TRANSITION_MODE',
                direction=transition_mode_direction,
                priority=SignalPriority.TRANSITION_MODE,
                confidence=0.75,
                reason=f"BIAS FLIP transition → {transition_mode_direction}",
                metadata={'from_bias_flip': True}
            ))
        
        # 6. STRUCTURAL_BIAS - HTF EMA (CONFIRMER level)
        if structural_bias:
            signals.append(DirectionSignal(
                source='STRUCTURAL_BIAS',
                direction=structural_bias,
                priority=SignalPriority.STRUCTURAL_BIAS,
                confidence=0.5,
                reason="HTF structural bias (confirmer)"
            ))
        
        # 7. FORBIDDEN_FALLBACK - When nothing else works (10)
        if not signals and forbidden_direction:
            # If forbidden is LONG, fallback to SHORT
            fallback_dir = 'SHORT' if forbidden_direction == 'LONG' else 'LONG'
            signals.append(DirectionSignal(
                source='FORBIDDEN_FALLBACK',
                direction=fallback_dir,
                priority=SignalPriority.FORBIDDEN_FALLBACK,
                confidence=0.3,
                reason=f"Fallback: {forbidden_direction} forbidden, using {fallback_dir}",
                metadata={'forbidden': forbidden_direction}
            ))
        
        # =====================================================================
        # SORT BY PRIORITY (highest first)
        # =====================================================================
        signals.sort(reverse=True)
        
        # =====================================================================
        # APPLY VETO LOGIC
        # =====================================================================
        winning_signal: Optional[DirectionSignal] = None
        vetoed_by: Optional[str] = None
        final_direction: Optional[str] = None
        conflict_detected = False
        conflict_details = ""
        
        core_signals = [s for s in signals if s.priority.value >= 100]
        
        # Pre-compute macro incoherence for deadlock prevention
        # LOG EVIDENCE (Feb 23): EDGE_BREAKOUT=LONG (priority 100) bypassed
        # forbidden_direction=SHORT, but REGIME FUSION blocked ALL LONG entries.
        # Result: 12-min DEAD_EDGE deadlock per cycle, 7+ cycles = hours wasted.
        _macro_would_block_long = macro_bias_strength == 'BEAR_STRONG'
        _macro_would_block_short = macro_bias_strength == 'BULL_STRONG'
        
        for signal in signals:
            # CORE signals (EXPANSION/EDGE/DISPLACEMENT/ANTICIPATION) bypass forbidden_direction
            # UNLESS the direction contradicts strong macro (REGIME FUSION would block anyway)
            if signal.priority.value >= 100:
                direction_blocked_by_macro = (
                    (signal.direction == 'LONG' and _macro_would_block_long) or
                    (signal.direction == 'SHORT' and _macro_would_block_short)
                )
                if direction_blocked_by_macro and forbidden_direction == signal.direction:
                    # Don't bypass forbidden — REGIME FUSION would block this trade
                    if not vetoed_by:
                        vetoed_by = f"MACRO_{macro_bias_strength}"
                        conflict_detected = True
                        conflict_details = (
                            f"{signal.source}={signal.direction} forbidden AND "
                            f"macro={macro_bias_strength} contradicts — no bypass"
                        )
                    continue
                winning_signal = signal
                final_direction = signal.direction
                break
            
            # High-confidence ANTICIPATION bypasses forbidden_direction
            # Rationale: ANTICIPATION detected compression breakout BEFORE the move
            # The forbidden_direction is stale/lagging - ANTICIPATION has early info
            if signal.source == 'ANTICIPATION' and signal.metadata.get('can_bypass_forbidden'):
                logger.info(
                    f"🔮 ANTICIPATION HIGH CONFIDENCE BYPASS: {signal.direction} | "
                    f"conf={signal.confidence:.0%} bypasses FORBIDDEN={forbidden_direction}"
                )
                winning_signal = signal
                final_direction = signal.direction
                break
            
            # Check if this direction is forbidden
            if forbidden_direction and signal.direction == forbidden_direction:
                # This signal is vetoed, try next one
                if not vetoed_by:
                    vetoed_by = f"FORBIDDEN_{forbidden_direction}"
                    conflict_detected = True
                    conflict_details = f"{signal.source}={signal.direction} vetoed by forbidden_direction={forbidden_direction}"
                continue
            
            # This signal wins
            winning_signal = signal
            final_direction = signal.direction
            break
        
        # =====================================================================
        # DETECT CONFLICTS (for logging)
        # =====================================================================
        if len(signals) >= 2:
            unique_directions = set(s.direction for s in signals if s.direction)
            if len(unique_directions) > 1:
                conflict_detected = True
                conflict_sources = [f"{s.source}={s.direction}" for s in signals[:3]]
                conflict_details = f"Conflicting signals: {', '.join(conflict_sources)}"
        
        # =====================================================================
        # 🔥 REFACTOR: LOCATION EXTREME VETO (moved from _get_effective_direction)
        # If price is at extreme location, block incompatible direction
        # UNLESS breakout is confirmed in same direction
        # =====================================================================
        if final_direction and location_context:
            min_breakout_score = 3
            
            if final_direction == 'SHORT' and getattr(location_context, 'in_extreme_low', False):
                # At bottom — block SHORT unless breaking down
                breakout_override = (
                    getattr(location_context, 'is_breakout_down', False) and
                    getattr(location_context, 'breakout_down_score', 0) >= min_breakout_score
                )
                if breakout_override:
                    logger.info(
                        f"🌊 BE WATER: SHORT in extreme low ALLOWED - breakout down "
                        f"(score={location_context.breakout_down_score})"
                    )
                else:
                    if not vetoed_by:
                        vetoed_by = 'LOCATION_EXTREME_LOW'
                    final_direction = None
                    winning_signal = None
            
            elif final_direction == 'LONG' and getattr(location_context, 'in_extreme_high', False):
                # At top — block LONG unless breaking up
                breakout_override = (
                    getattr(location_context, 'is_breakout_up', False) and
                    getattr(location_context, 'breakout_up_score', 0) >= min_breakout_score
                )
                if breakout_override:
                    logger.info(
                        f"🌊 BE WATER: LONG in extreme high ALLOWED - breakout up "
                        f"(score={location_context.breakout_up_score})"
                    )
                else:
                    if not vetoed_by:
                        vetoed_by = 'LOCATION_EXTREME_HIGH'
                    final_direction = None
                    winning_signal = None
        
        # =====================================================================
        # HTF_BIAS STALENESS GUARD

        # If STRUCTURAL_BIAS is the winning signal with no CORE/TRANSITION
        # confirmation → veto ONLY in RANGE markets (low ADX).

        # ROOT CAUSE FIX
        # PROBLEM: In TREND mode (ADX >= 25), structural bias IS the signal.
        # The guard was designed for RANGE markets where HTF bias is noise.
        # But it was applied unconditionally, causing Active=None for 20+ min
        # while the market moved 0.89% in the structural direction.
        # LOG EVIDENCE: ADX=31.1, -DI=34.8 → clear SHORT trend but
        # Active=None because no CORE signal confirmed structural bias.

        # FIX: When htf_adx >= 25 (STRONG_TREND), structural bias alone
        # IS a valid actionable signal. Only veto in RANGE (ADX < 25).
        # Advisory signals (MICRO_MOMENTUM, FAST_REVERSAL) do NOT confirm.
        # =====================================================================
        if winning_signal and winning_signal.source == 'STRUCTURAL_BIAS':
            # In strong trends, structural bias IS the signal — not stale
            trend_confirms_bias = htf_adx >= 25
            
            has_real_confirmation = trend_confirms_bias or any(
                s.source != 'STRUCTURAL_BIAS' and s.priority.value >= 50
                for s in signals if not s.metadata.get('is_advisory')
            )
            
            if not has_real_confirmation:
                logger.debug(
                    f"📊 HTF_BIAS={final_direction} is ALONE | "
                    f"ADX={htf_adx:.1f} < 25 (RANGE) | No confirmation | Returning None"
                )
                final_direction = None
                winning_signal = None
                if not vetoed_by:
                    vetoed_by = 'HTF_BIAS_ALONE'
            elif trend_confirms_bias and tick_count % 60 == 1:
                logger.info(
                    f"📊 HTF_BIAS={final_direction} CONFIRMED by STRONG_TREND | "
                    f"ADX={htf_adx:.1f} ≥ 25 | Actionable"
                )
        
        # =====================================================================
        # CREATE RESULT
        # =====================================================================
        result = OrchestrationResult(
            final_direction=final_direction,
            winning_signal=winning_signal,
            vetoed_by=vetoed_by,
            all_signals=signals,
            conflict_detected=conflict_detected,
            conflict_details=conflict_details
        )
        
        # Log if there's a conflict
        if conflict_detected and tick_count % 30 == 1:
            logger.info(
                f"🎭 DIRECTION CONFLICT: {conflict_details} | "
                f"Winner: {winning_signal.source if winning_signal else 'NONE'}={final_direction}"
            )
        
        self._last_result = result
        return result
    
    def get_direction(self, **kwargs) -> Optional[str]:
        """Convenience method to just get the direction."""
        result = self.orchestrate(**kwargs)
        return result.final_direction


# Singleton
_orchestrator: Optional[DirectionOrchestrator] = None

def get_direction_orchestrator() -> DirectionOrchestrator:
    """Get singleton instance of DirectionOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DirectionOrchestrator()
    return _orchestrator
