"""
Direction Orchestrator - Unified direction determination

This module consolidates ALL direction signals into a single coherent system.
No more conflicting signals - one source of truth.

HIERARCHY (highest to lowest priority):
1. EXPANSION_MODE - Strong displacement detected, direction locked
2. DISPLACEMENT - Significant price movement (>= threshold ATR)
3. FAST_REVERSAL - Single-candle reversal detected
4. MICRO_MOMENTUM - HH/HL/LL/LH scoring on recent candles
5. STRUCTURAL_BIAS - EMA-based HTF bias
6. FORBIDDEN_DIRECTION - Fallback when bias is invalidated

RULES:
- Higher priority signals OVERRIDE lower priority
- FORBIDDEN_DIRECTION can VETO any direction (except EXPANSION_MODE)
- FAST_REVERSAL can OVERRIDE MICRO_MOMENTUM (designed for this)
- All signals are logged for debugging

Created: Feb 2, 2026
Author: Trading System
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
    
    🔥 REFACTOR (Feb 05, 2026): SIMPLIFIED HIERARCHY
    
    PROBLEM: We had 8 priority levels (100, 95, 90, 85, 80, 70, 60, 50).
    Each module wanted to be the boss. Result: conflicting signals, erratic behavior.
    
    SOLUTION: Only 4 CORE signals can determine direction:
    1. DISPLACEMENT (the market MOVED - fastest, most reliable)
    2. BREAKOUT (compression broke - confirmed direction)  
    3. ANTICIPATION (100% accuracy - pre-breakout detection)
    4. ADX_TREND (sustained trend - ADX > 20 with direction)
    
    Everything else (MICRO_MOMENTUM, STRUCTURAL_BIAS, etc.) becomes ADVISORY ONLY.
    They can reduce size or add confidence, but NOT determine direction.
    
    NEW HIERARCHY:
    - CORE (100): DISPLACEMENT, EXPANSION_MODE, EDGE_BREAKOUT, ANTICIPATION → determines direction
    - CONFIRMER (50): STRUCTURAL_BIAS → confirms or warns
    - ADVISORY (10): MICRO_MOMENTUM, FAST_REVERSAL → size adjustment only
    """
    # CORE LEVEL (100) - These DETERMINE direction (only one needs to fire)
    EXPANSION_MODE = 100      # Strong displacement with lock - BYPASSES ALL
    EDGE_BREAKOUT = 100       # Same level as EXPANSION (sovereign)
    DISPLACEMENT = 100        # Same level as EXPANSION (sovereign)
    ANTICIPATION = 100        # 🔥 RESTORED: 100% accuracy = CORE signal
    
    # TRANSITION LEVEL (75) - BIAS FLIP direction overrides structural bias
    # 🔥 FIX #269: When BIAS FLIP sets transition_mode_direction, it MUST
    # override structural_bias. Otherwise the system reverts to LONG after
    # SHORT edge expires, causing infinite CONSENSUS BLOCKED spam.
    TRANSITION_MODE = 75
    
    # CONFIRMER LEVEL (50) - These CONFIRM but don't override CORE
    STRUCTURAL_BIAS = 50      # HTF EMA-based bias
    
    # ADVISORY LEVEL (10) - These ADVISE but never determine direction
    # 🔥 REFACTOR: These signals are LAGGING. They should NOT set direction.
    FAST_REVERSAL = 10        # Downgraded from 80 → advisory only
    MICRO_MOMENTUM = 10       # Downgraded from 70 → advisory only
    FORBIDDEN_FALLBACK = 10   # Last resort
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
    
    🔥 FIX (Feb 05, 2026): Added circuit breaker for consecutive direction failures.
    If 3 consecutive directions result in losses, PAUSE for N candles.
    """
    
    # Circuit breaker configuration
    CIRCUIT_BREAKER_LOSSES = 3       # Consecutive losses to trigger
    CIRCUIT_BREAKER_PAUSE_CANDLES = 10  # Candles to pause after trigger
    
    def __init__(self):
        self._last_result: Optional[OrchestrationResult] = None
        self._signal_history: List[DirectionSignal] = []
        
        # 🔥 Circuit breaker state
        self._direction_outcomes: List[bool] = []  # True=win, False=loss
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_until_tick: int = 0
        self._consecutive_losses: int = 0
    
    def record_direction_outcome(self, was_profitable: bool, tick_count: int = 0):
        """
        Record whether the last direction decision was profitable.
        
        🔥 NEW (Feb 05, 2026): Circuit breaker for consecutive failures.
        If 3 consecutive directions → loss, pause for 10 candles.
        
        Args:
            was_profitable: True if trade was profitable
            tick_count: Current tick for pause timing
        """
        self._direction_outcomes.append(was_profitable)
        
        # Keep last 20
        if len(self._direction_outcomes) > 20:
            self._direction_outcomes = self._direction_outcomes[-20:]
        
        # Update consecutive losses
        if was_profitable:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        
        # Check circuit breaker
        if self._consecutive_losses >= self.CIRCUIT_BREAKER_LOSSES:
            self._circuit_breaker_active = True
            self._circuit_breaker_until_tick = tick_count + self.CIRCUIT_BREAKER_PAUSE_CANDLES
            logger.warning(
                f"🚨 DIRECTION CIRCUIT BREAKER TRIGGERED: {self._consecutive_losses} consecutive losses | "
                f"Pausing direction decisions for {self.CIRCUIT_BREAKER_PAUSE_CANDLES} candles"
            )
    
    def is_circuit_breaker_active(self, tick_count: int = 0) -> tuple[bool, str]:
        """
        Check if circuit breaker is active.
        
        Returns:
            (is_active, reason_string)
        """
        if not self._circuit_breaker_active:
            return False, ""
        
        if tick_count >= self._circuit_breaker_until_tick:
            # Circuit breaker expired
            self._circuit_breaker_active = False
            self._consecutive_losses = 0
            logger.info(f"✅ DIRECTION CIRCUIT BREAKER RESET at tick {tick_count}")
            return False, ""
        
        remaining = self._circuit_breaker_until_tick - tick_count
        return True, f"CIRCUIT_BREAKER: {self._consecutive_losses} losses, {remaining} candles remaining"
        
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
                    # 🔥 FIX #28 (Feb 04): Added confidence for high-confidence bypass
                    anticipation_pending: bool = False,
                    anticipation_direction: Optional[str] = None,
                    anticipation_confidence: float = 0.0,
                    
                    # 🔥 FIX #269: TRANSITION_MODE direction (Priority 75)
                    # When BIAS FLIP sets transition_mode, this direction MUST
                    # override structural_bias to prevent reverting to stale HTF bias
                    transition_mode_active: bool = False,
                    transition_mode_direction: Optional[str] = None,
                    
                    # Fast reversal signals (Priority 80)
                    fast_reversal_detected: bool = False,
                    fast_reversal_direction: Optional[str] = None,
                    fast_reversal_confidence: float = 0.0,
                    
                    # Micro momentum signals (Priority 70)
                    micro_momentum: Optional[str] = None,
                    micro_momentum_strength: str = 'WEAK',
                    
                    # Structural signals (Priority 60)
                    structural_bias: Optional[str] = None,
                    
                    # Veto signals
                    forbidden_direction: Optional[str] = None,
                    
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
        # 🔥 FIX #28 (Feb 04): Use actual confidence and mark high-confidence for bypass
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
        # 🔥 FIX #269: When BIAS FLIP activates transition_mode, this direction
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
        
        # 6. FAST_REVERSAL - Single-candle reversal
        # 🔥 REFACTOR (Feb 05, 2026): ADVISORY only - cannot determine direction
        # It can only be used to CONFIRM or add confidence to an existing CORE signal
        if fast_reversal_detected and fast_reversal_direction:
            signals.append(DirectionSignal(
                source='FAST_REVERSAL',
                direction=fast_reversal_direction,
                priority=SignalPriority.FAST_REVERSAL,
                confidence=fast_reversal_confidence,
                reason="Fast reversal (advisory only)",
                metadata={'override_momentum': False, 'is_advisory': True}  # 🔥 CHANGED
            ))
        
        # 7. MICRO_MOMENTUM - HH/HL scoring
        # 🔥 REFACTOR (Feb 05, 2026): Now ADVISORY only - CANNOT determine direction
        # This is the KEY FIX. MicroMomentum is LAGGING. It should not set direction.
        # It can only modify size/confidence of an existing CORE direction.
        if micro_momentum:
            conf = 0.9 if micro_momentum_strength == 'STRONG' else 0.6
            signals.append(DirectionSignal(
                source='MICRO_MOMENTUM',
                direction=micro_momentum,
                priority=SignalPriority.MICRO_MOMENTUM,
                confidence=conf,
                reason=f"Micro momentum {micro_momentum_strength} (advisory only)",
                metadata={'strength': micro_momentum_strength, 'is_advisory': True}  # 🔥 CHANGED
            ))
        
        # 8. STRUCTURAL_BIAS - HTF EMA
        # 🔥 REFACTOR: CONFIRMER level - can confirm but not override CORE
        if structural_bias:
            signals.append(DirectionSignal(
                source='STRUCTURAL_BIAS',
                direction=structural_bias,
                priority=SignalPriority.STRUCTURAL_BIAS,
                confidence=0.5,
                reason="HTF structural bias (confirmer)",
                metadata={'is_confirmer': True}
            ))
        
        # 9. FORBIDDEN_FALLBACK - When nothing else works (10)
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
        # 🔥 REFACTOR (Feb 05, 2026): FILTER ADVISORY SIGNALS WHEN CORE EXISTS
        # Advisory signals (MICRO_MOMENTUM, FAST_REVERSAL) should NOT determine
        # direction when there are CORE signals (DISPLACEMENT, BREAKOUT, EXPANSION)
        # =====================================================================
        core_signals = [s for s in signals if s.priority.value >= 100]
        confirmer_signals = [s for s in signals if 10 < s.priority.value < 100]
        advisory_signals = [s for s in signals if s.priority.value <= 10]
        
        # If we have CORE signals, ADVISORY signals cannot determine direction
        # They can only be used for confidence/size adjustment
        if core_signals:
            # Log that we're ignoring advisory signals
            if advisory_signals:
                advisory_dirs = [f"{s.source}={s.direction}" for s in advisory_signals]
                logger.debug(
                    f"🎯 CORE SIGNAL ACTIVE: {core_signals[0].source}={core_signals[0].direction} | "
                    f"Ignoring advisory signals: {', '.join(advisory_dirs)}"
                )
        
        # =====================================================================
        # APPLY VETO LOGIC
        # =====================================================================
        winning_signal: Optional[DirectionSignal] = None
        vetoed_by: Optional[str] = None
        final_direction: Optional[str] = None
        conflict_detected = False
        conflict_details = ""
        
        for signal in signals:
            # 🔥 SKIP ADVISORY signals if we have CORE signals
            if signal.metadata.get('is_advisory') and core_signals:
                logger.debug(f"⏭️ Skipping advisory signal {signal.source}={signal.direction}")
                continue
            
            # EXPANSION_MODE/EDGE_BREAKOUT/DISPLACEMENT are all CORE (100)
            # They bypass EVERYTHING including forbidden_direction
            if signal.priority.value >= 100:
                winning_signal = signal
                final_direction = signal.direction
                break
            
            # 🔥 FIX #28 (Feb 04): High-confidence ANTICIPATION bypasses forbidden_direction
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
