"""
Strategy State Machine - Automatic activation/deactivation logic

Manages transitions between:
- ACTIVE: Normal operation (100% position size)
- DEGRADED: Reduced operation (50% position size) 
- OBSERVATION: Post-alert monitoring (50% position size, 14 days)
- DEAD: Archived (0% position size)

State transitions are automatic based on metrics.
The trader does NOT decide transitions.

IMPORTANT: This is the ONLY component that manages state transitions.
EdgeValidator and other components only REPORT metrics - they do NOT change state.
"""

from typing import Tuple, Optional, List
from datetime import datetime, timedelta, UTC
import logging

# Import canonical state types - SINGLE SOURCE OF TRUTH
from src.engine.state.state_types import StrategyState, StateTransition, get_state_size_multiplier, can_trade_in_state


class StrategyStateMachine:
    """
    Automatic state management based on operational metrics.
    
    Core principle: The system automatically transitions based on
    objective metrics. The trader does NOT make state decisions.
    """
    
    def __init__(self, current_time: Optional[datetime] = None):
        """Initialize state machine"""
        self.current_state = StrategyState.ACTIVE
        self.state_entry_time = current_time or datetime.now(UTC)
        self.state_transitions: List[StateTransition] = []
        
        # Thresholds
        self.DEGRADATION_SIGNAL_THRESHOLD = 2  # signals to trigger degradation
        self.OBSERVATION_PERIOD_DAYS = 14
        self.CONSECUTIVE_NEGATIVE_WINDOWS = 3
        self.EXPECTANCY_THRESHOLD = 0.0
        self.DRAWDOWN_LIMIT = 0.18
        
        self.logger = logging.getLogger(__name__)
    
    # ========================================================================
    # STATE TRANSITIONS
    # ========================================================================
    
    def transition_to_degraded(self, reason: str, metrics: dict, current_time: Optional[datetime] = None) -> None:
        """
        Transition to DEGRADED state (50% position size).
        
        Triggered by 2+ degradation signals detected.
        
        Args:
            reason: Human-readable transition reason
            metrics: Dict with current metrics snapshot
            current_time: Optional timestamp for backtesting
        """
        if self.current_state == StrategyState.DEGRADED:
            return  # Already degraded
        
        if self.current_state == StrategyState.DEAD:
            return  # Cannot transition from dead
        
        now = current_time or datetime.now(UTC)
        old_state = self.current_state
        self.current_state = StrategyState.DEGRADED
        self.state_entry_time = now
        
        transition = StateTransition(
            from_state=old_state,
            to_state=StrategyState.DEGRADED,
            timestamp=now,
            reason=reason,
            metrics_snapshot=metrics
        )
        self.state_transitions.append(transition)
        
        self.logger.warning(f"[STATE] Transition ACTIVE → DEGRADED: {reason}")
        self.logger.warning(f"[STATE] Position size reduced to 50%")
    
    def transition_to_observation(self, reason: str, metrics: dict, current_time: Optional[datetime] = None) -> None:
        """
        Transition to OBSERVATION state (50% position size, 14 days).
        
        Triggered after degradation signals don't clear in DEGRADED mode.
        
        Args:
            reason: Human-readable transition reason
            metrics: Dict with current metrics snapshot
            current_time: Optional timestamp for backtesting
        """
        now = current_time or datetime.now(UTC)
        old_state = self.current_state
        self.current_state = StrategyState.OBSERVATION
        self.state_entry_time = now
        
        transition = StateTransition(
            from_state=old_state,
            to_state=StrategyState.OBSERVATION,
            timestamp=now,
            reason=reason,
            metrics_snapshot=metrics
        )
        self.state_transitions.append(transition)
        
        self.logger.warning(f"[STATE] Transition to OBSERVATION: {reason}")
        self.logger.warning(f"[STATE] Entering 14-day monitoring period")
    
    def transition_to_dead(self, reason: str, metrics: dict, current_time: Optional[datetime] = None) -> None:
        """
        Transition to DEAD state (strategy archived permanently).
        
        Triggered by:
        - 3 consecutive negative expectancy windows of 50 trades
        - Structural edge loss in all regimes
        - Irreversible degradation
        
        Args:
            reason: Human-readable transition reason
            metrics: Dict with current metrics snapshot
            current_time: Optional timestamp for backtesting
        """
        now = current_time or datetime.now(UTC)
        old_state = self.current_state
        self.current_state = StrategyState.DEAD
        self.state_entry_time = now
        
        transition = StateTransition(
            from_state=old_state,
            to_state=StrategyState.DEAD,
            timestamp=now,
            reason=reason,
            metrics_snapshot=metrics
        )
        self.state_transitions.append(transition)
        
        self.logger.error(f"[STATE] STRATEGY ARCHIVED: {reason}")
        self.logger.error(f"[STATE] This is professional. Edge is gone.")
    
    def transition_to_active(self, reason: str, metrics: dict, current_time: Optional[datetime] = None) -> None:
        """
        Transition back to ACTIVE state (recovery).
        
        From DEGRADED: If degradation signals clear (< 2)
        From OBSERVATION: If 14 days elapsed and degradation cleared
        
        Args:
            reason: Human-readable transition reason
            metrics: Dict with current metrics snapshot
            current_time: Optional timestamp for backtesting
        """
        if self.current_state == StrategyState.ACTIVE:
            return  # Already active
        
        if self.current_state == StrategyState.DEAD:
            return  # Cannot resurrect dead strategy
        
        now = current_time or datetime.now(UTC)
        old_state = self.current_state
        self.current_state = StrategyState.ACTIVE
        self.state_entry_time = now
        
        transition = StateTransition(
            from_state=old_state,
            to_state=StrategyState.ACTIVE,
            timestamp=now,
            reason=reason,
            metrics_snapshot=metrics
        )
        self.state_transitions.append(transition)
        
        self.logger.info(f"[STATE] Recovery to ACTIVE: {reason}")
        self.logger.info(f"[STATE] Position size restored to 100%")
    
    # ========================================================================
    # STATE EVALUATION
    # ========================================================================
    
    def evaluate_state_transitions(self,
                                  degradation_signals: int,
                                  context_valid: bool,
                                  expectancy: float,
                                  current_drawdown: float,
                                  recent_metrics: dict,
                                  current_time: Optional[datetime] = None) -> Tuple[StrategyState, Optional[str]]:
        """
        Evaluates current state and determines if transition is needed.
        
        This is the MAIN STATE DECISION LOGIC.
        Called every N trades or hourly.
        
        Args:
            degradation_signals: Count of active signals (0-4)
            context_valid: Whether edge context is valid
            expectancy: Current expectancy value (50+ trade window)
            current_drawdown: Current drawdown as decimal (0.15 = 15%)
            recent_metrics: Dict with recent performance metrics
            current_time: Optional timestamp for backtesting
        
        Returns:
            Tuple (new_state, transition_reason_or_none)
        """
        transition_reason = None
        new_state = self.current_state
        now = current_time or datetime.now(UTC)
        
        # ====== FROM ACTIVE ======
        if self.current_state == StrategyState.ACTIVE:
            # Active → Degraded: 2+ signals
            if degradation_signals >= self.DEGRADATION_SIGNAL_THRESHOLD:
                new_state = StrategyState.DEGRADED
                transition_reason = f"Degradation detected: {degradation_signals} signals active"
            
            # Active → Dead: Expectancy collapsed
            elif expectancy < self.EXPECTANCY_THRESHOLD and recent_metrics.get('windows_negative', 0) >= self.CONSECUTIVE_NEGATIVE_WINDOWS:
                new_state = StrategyState.DEAD
                transition_reason = f"Edge lost: {self.CONSECUTIVE_NEGATIVE_WINDOWS} windows with negative expectancy"
            
            # Active → Dead: Drawdown exceeded
            elif current_drawdown > self.DRAWDOWN_LIMIT:
                new_state = StrategyState.DEAD
                transition_reason = f"Catastrophic drawdown: {current_drawdown*100:.1f}% > {self.DRAWDOWN_LIMIT*100:.1f}%"
        
        # ====== FROM DEGRADED ======
        elif self.current_state == StrategyState.DEGRADED:
            elapsed = now - self.state_entry_time
            
            # Degraded → Observation: After 14 days in degraded mode
            if elapsed > timedelta(days=self.OBSERVATION_PERIOD_DAYS):
                new_state = StrategyState.OBSERVATION
                transition_reason = f"Observational period starting after {self.OBSERVATION_PERIOD_DAYS} days degraded"
            
            # Degraded → Active: Signals cleared quickly
            elif degradation_signals < self.DEGRADATION_SIGNAL_THRESHOLD:
                new_state = StrategyState.ACTIVE
                transition_reason = f"Degradation resolved: {degradation_signals} signals (< {self.DEGRADATION_SIGNAL_THRESHOLD})"
            
            # Degraded → Dead: Expectancy negative and persistent
            elif expectancy < self.EXPECTANCY_THRESHOLD:
                new_state = StrategyState.DEAD
                transition_reason = f"Edge collapse while degraded: negative expectancy"
        
        # ====== FROM OBSERVATION ======
        elif self.current_state == StrategyState.OBSERVATION:
            elapsed = now - self.state_entry_time
            
            # Observation → Active: Period over + degradation cleared
            if elapsed > timedelta(days=self.OBSERVATION_PERIOD_DAYS):
                if degradation_signals < self.DEGRADATION_SIGNAL_THRESHOLD:
                    new_state = StrategyState.ACTIVE
                    transition_reason = f"Observation period completed: degradation resolved"
                else:
                    new_state = StrategyState.DEAD
                    transition_reason = f"Observation period ended but degradation persists: edge is dead"
            
            # Observation → Dead: Expectancy collapsed
            elif expectancy < self.EXPECTANCY_THRESHOLD:
                new_state = StrategyState.DEAD
                transition_reason = f"Edge collapse during observation: negative expectancy confirmed"
        
        # ====== FROM DEAD ======
        elif self.current_state == StrategyState.DEAD:
            # Dead state is permanent - no transitions out
            pass
        
        # Execute transition if needed
        if new_state != self.current_state:
            self._execute_transition(new_state, transition_reason, recent_metrics, now)
        
        return new_state, transition_reason
    
    def _execute_transition(self, new_state: StrategyState, reason: str | None, metrics: dict, current_time: datetime) -> None:
        """Executes the actual state transition"""
        reason_str = reason or "unspecified"
        if new_state == StrategyState.DEGRADED:
            self.transition_to_degraded(reason_str, metrics, current_time)
        elif new_state == StrategyState.OBSERVATION:
            self.transition_to_observation(reason_str, metrics, current_time)
        elif new_state == StrategyState.DEAD:
            self.transition_to_dead(reason_str, metrics, current_time)
        elif new_state == StrategyState.ACTIVE:
            self.transition_to_active(reason_str, metrics, current_time)
    
    # ========================================================================
    # STATE INFORMATION
    # ========================================================================
    
    def get_position_size_multiplier(self) -> float:
        """
        Returns position size multiplier based on current state.
        
        Uses centralized state_types.get_state_size_multiplier() for consistency.
        
        Returns:
            1.0 = ACTIVE (normal)
            0.5 = DEGRADED (50% reduction)
            0.5 = OBSERVATION (50% reduction)
            0.0 = DEAD (no trading)
        """
        return get_state_size_multiplier(self.current_state)
    
    def can_trade(self) -> bool:
        """Returns True if trading is allowed"""
        return can_trade_in_state(self.current_state)
    
    def get_state_duration(self, current_time: Optional[datetime] = None) -> timedelta:
        """Returns time spent in current state"""
        now = current_time or datetime.now(UTC)
        return now - self.state_entry_time
    
    def is_observation_period_remaining(self, current_time: Optional[datetime] = None) -> timedelta:
        """
        Returns remaining observation period (if in OBSERVATION state).
        
        Returns:
            timedelta remaining, or timedelta(0) if not in observation
        """
        if self.current_state != StrategyState.OBSERVATION:
            return timedelta(0)
        
        elapsed = self.get_state_duration(current_time)
        remaining = timedelta(days=self.OBSERVATION_PERIOD_DAYS) - elapsed
        
        return max(remaining, timedelta(0))
    
    # ========================================================================
    # REPORTING
    # ========================================================================
    
    def get_state_report(self, current_time: Optional[datetime] = None) -> dict:
        """
        Returns comprehensive state report for logging.
        
        Returns:
            Dict with state info and duration
        """
        return {
            'current_state': self.current_state.value,
            'position_multiplier': self.get_position_size_multiplier(),
            'can_trade': self.can_trade(),
            'state_duration_hours': self.get_state_duration(current_time).total_seconds() / 3600.0,
            'observation_remaining_days': self.is_observation_period_remaining(current_time).days,
            'transitions_total': len(self.state_transitions),
            'last_transition': self.state_transitions[-1] if self.state_transitions else None
        }
    
    def get_transition_history(self, limit: int = 10) -> List[dict]:
        """
        Returns recent state transitions.
        
        Args:
            limit: Max transitions to return
        
        Returns:
            List of transition dicts
        """
        recent = self.state_transitions[-limit:]
        
        return [
            {
                'timestamp': t.timestamp.isoformat(),
                'from': t.from_state.value,
                'to': t.to_state.value,
                'reason': t.reason,
                'metrics': t.metrics_snapshot
            }
            for t in recent
        ]
    
    def log_state_changes(self) -> None:
        """Logs current state for monitoring"""
        report = self.get_state_report()
        
        if self.current_state == StrategyState.ACTIVE:
            self.logger.info(f"[STATE] 🟢 ACTIVE - Normal operation")
        elif self.current_state == StrategyState.DEGRADED:
            self.logger.warning(f"[STATE] 🟡 DEGRADED - 50% position size - Duration: {report['state_duration_hours']:.1f}h")
        elif self.current_state == StrategyState.OBSERVATION:
            remaining = report['observation_remaining_days']
            self.logger.warning(f"[STATE] 🟠 OBSERVATION - 14-day monitoring - {remaining} days remaining")
        elif self.current_state == StrategyState.DEAD:
            self.logger.error(f"[STATE] 🔴 DEAD - Strategy archived permanently")
