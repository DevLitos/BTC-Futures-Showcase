"""
Edge Recovery Protocol - Formal rules for edge recovery validation

CRITICAL PROBLEM:
Your state machine knows how to:
- Degrade
- Observe
- Kill

But it doesn't know how to RECOVER properly.
Current logic: "signals cleared" = back to active

That's too naive and dangerous.

PHILOSOPHY:
- Recovery is NOT automatic
- Recovery requires PROOF, not just absence of problems
- Recovery criteria must be explicit and measurable
- Premature recovery kills capital
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC
import numpy as np


class RecoveryPhase(Enum):
    """Phases of edge recovery"""
    NONE = "no_recovery_active"
    INITIAL = "initial_stabilization"      # Signals cleared but not proven
    VALIDATION = "performance_validation"   # Proving edge is back
    CONFIRMED = "recovery_confirmed"        # Edge validated, ready to activate
    FAILED = "recovery_failed"              # Recovery attempt failed


@dataclass
class RecoveryRequirements:
    """Requirements for edge recovery"""
    min_trades: int = 10                    # Minimum trades to validate
    min_win_rate: float = 0.45              # Minimum WR (baseline - margin)
    min_mfe_mae_ratio: float = 1.3          # Minimum MFE/MAE
    min_extension_rate: float = 0.35        # Minimum % reaching TP2
    max_be_stop_rate: float = 0.45          # Maximum BE stop rate
    max_duration_hours: float = 13.0        # Maximum avg duration
    min_clean_extensions: int = 2           # Minimum clean TP2 extensions
    min_expectancy: float = 0.3             # Minimum expectancy in R


@dataclass
class RecoveryProgress:
    """Tracks recovery progress"""
    phase: RecoveryPhase
    started_at: datetime
    trades_completed: int
    trades_required: int
    current_metrics: Dict
    requirements_met: Dict[str, bool]
    confidence: float  # 0-1
    estimated_completion_trades: int


@dataclass
class RecoveryAttempt:
    """Records a recovery attempt"""
    attempt_number: int
    started_at: datetime
    ended_at: Optional[datetime]
    final_phase: RecoveryPhase
    trades_in_attempt: int
    success: bool
    failure_reason: Optional[str]
    metrics_at_end: Dict


class EdgeRecoveryProtocol:
    """
    Manages formal edge recovery validation.
    
    RULES:
    1. Recovery requires PROOF through metrics
    2. Recovery is gradual: stabilization → validation → confirmation
    3. Failed recovery extends observation period
    4. Multiple failed recoveries → permanent kill
    """
    
    def __init__(self, baseline_metrics: Dict):
        """
        Initialize recovery protocol.
        
        Args:
            baseline_metrics: Historical baseline for comparison
        """
        self.baseline_metrics = baseline_metrics
        
        # Recovery state
        self.current_phase = RecoveryPhase.NONE
        self.recovery_started: Optional[datetime] = None
        self.recovery_trades: List[Dict] = []
        self.recovery_attempts: List[RecoveryAttempt] = []
        self.current_attempt_number = 0
        
        # Requirements (can be adjusted based on baseline)
        self.requirements = self._build_requirements(baseline_metrics)
        
        # Limits
        self.MAX_RECOVERY_ATTEMPTS = 3
        self.RECOVERY_TIMEOUT_DAYS = 21
        self.FAILED_ATTEMPT_COOLDOWN_DAYS = 7
        
    def _build_requirements(self, baseline: Dict) -> RecoveryRequirements:
        """Build recovery requirements based on baseline performance"""
        
        # Recovery WR should be at least baseline - 5%
        baseline_wr = baseline.get('win_rate', 0.50)
        min_wr = max(baseline_wr - 0.05, 0.40)
        
        # Recovery expectancy at least baseline - 0.2R
        baseline_exp = baseline.get('expectancy', 0.5)
        min_exp = max(baseline_exp - 0.2, 0.3)
        
        return RecoveryRequirements(
            min_trades=10,
            min_win_rate=min_wr,
            min_mfe_mae_ratio=1.3,
            min_extension_rate=0.35,
            max_be_stop_rate=0.45,
            max_duration_hours=13.0,
            min_clean_extensions=2,
            min_expectancy=min_exp
        )
    
    def start_recovery(self) -> bool:
        """
        Start a recovery attempt.
        
        Returns:
            True if recovery attempt started, False if blocked
        """
        # Check if we can attempt recovery
        if not self._can_attempt_recovery():
            return False
        
        # Start new attempt
        self.current_attempt_number += 1
        self.current_phase = RecoveryPhase.INITIAL
        self.recovery_started = datetime.now(UTC)
        self.recovery_trades = []
        
        return True
    
    def _can_attempt_recovery(self) -> bool:
        """Check if recovery attempt is allowed"""
        
        # Check attempt limit
        if len(self.recovery_attempts) >= self.MAX_RECOVERY_ATTEMPTS:
            return False
        
        # Check cooldown from last failed attempt
        if self.recovery_attempts:
            last_attempt = self.recovery_attempts[-1]
            if not last_attempt.success and last_attempt.ended_at:
                cooldown_end = last_attempt.ended_at + timedelta(
                    days=self.FAILED_ATTEMPT_COOLDOWN_DAYS
                )
                if datetime.now(UTC) < cooldown_end:
                    return False
        
        return True
    
    def add_recovery_trade(self,
                          entry_price: float,
                          exit_price: float,
                          mfe: float,
                          mae: float,
                          duration_hours: float,
                          reached_tp1: bool,
                          reached_tp2: bool,
                          is_be_stop: bool) -> RecoveryProgress:
        """
        Add a trade to recovery validation.
        
        Args:
            Trade metrics (same as normal trade recording)
            
        Returns:
            RecoveryProgress with current status
        """
        if self.current_phase == RecoveryPhase.NONE:
            raise ValueError("No recovery in progress")
        
        # Record trade
        trade = {
            'entry': entry_price,
            'exit': exit_price,
            'mfe': mfe,
            'mae': mae,
            'duration': duration_hours,
            'tp1': reached_tp1,
            'tp2': reached_tp2,
            'be_stop': is_be_stop,
            'is_winner': exit_price > entry_price,
            'timestamp': datetime.now(UTC)
        }
        self.recovery_trades.append(trade)
        
        # Calculate current metrics
        metrics = self._calculate_recovery_metrics()
        
        # Check phase transition
        self._update_recovery_phase(metrics)
        
        # Check for completion or failure
        self._check_recovery_outcome(metrics)
        
        # Build progress report
        requirements_met = self._check_requirements(metrics)
        
        progress = RecoveryProgress(
            phase=self.current_phase,
            started_at=self.recovery_started,
            trades_completed=len(self.recovery_trades),
            trades_required=self.requirements.min_trades,
            current_metrics=metrics,
            requirements_met=requirements_met,
            confidence=self._calculate_recovery_confidence(metrics, requirements_met),
            estimated_completion_trades=self._estimate_completion_trades(requirements_met)
        )
        
        return progress
    
    def _calculate_recovery_metrics(self) -> Dict:
        """Calculate metrics from recovery trades"""
        if not self.recovery_trades:
            return {}
        
        trades = self.recovery_trades
        
        # Win rate
        winners = [t for t in trades if t['is_winner']]
        win_rate = len(winners) / len(trades) if trades else 0
        
        # MFE/MAE
        mfe_mae_ratios = [t['mfe'] / t['mae'] if t['mae'] > 0 else 0 for t in trades]
        avg_mfe_mae = np.mean(mfe_mae_ratios) if mfe_mae_ratios else 0
        
        # BE stops
        be_stops = [t for t in trades if t['be_stop']]
        be_stop_rate = len(be_stops) / len(trades) if trades else 0
        
        # Extension rate
        extended = [t for t in trades if t['tp2']]
        extension_rate = len(extended) / len(trades) if trades else 0
        
        # Clean extensions
        clean_extensions = len([t for t in extended if t['is_winner']])
        
        # Avg duration
        avg_duration = np.mean([t['duration'] for t in trades])
        
        # Expectancy (simplified)
        avg_win = np.mean([t['exit'] - t['entry'] for t in winners]) if winners else 0
        losers = [t for t in trades if not t['is_winner']]
        avg_loss = np.mean([t['entry'] - t['exit'] for t in losers]) if losers else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        
        return {
            'win_rate': win_rate,
            'mfe_mae_ratio': avg_mfe_mae,
            'be_stop_rate': be_stop_rate,
            'extension_rate': extension_rate,
            'clean_extensions': clean_extensions,
            'avg_duration': avg_duration,
            'expectancy': expectancy,
            'sample_size': len(trades)
        }
    
    def _check_requirements(self, metrics: Dict) -> Dict[str, bool]:
        """Check which requirements are met"""
        if not metrics:
            return {}
        
        req = self.requirements
        
        return {
            'min_trades': metrics['sample_size'] >= req.min_trades,
            'win_rate': metrics['win_rate'] >= req.min_win_rate,
            'mfe_mae': metrics['mfe_mae_ratio'] >= req.min_mfe_mae_ratio,
            'extension_rate': metrics['extension_rate'] >= req.min_extension_rate,
            'be_stop_rate': metrics['be_stop_rate'] <= req.max_be_stop_rate,
            'duration': metrics['avg_duration'] <= req.max_duration_hours,
            'clean_extensions': metrics['clean_extensions'] >= req.min_clean_extensions,
            'expectancy': metrics['expectancy'] >= req.min_expectancy
        }
    
    def _update_recovery_phase(self, metrics: Dict) -> None:
        """Update recovery phase based on progress"""
        if self.current_phase == RecoveryPhase.INITIAL:
            # Move to VALIDATION if we have enough trades and no major issues
            if metrics.get('sample_size', 0) >= 5:
                requirements = self._check_requirements(metrics)
                critical_failures = sum([
                    not requirements.get('win_rate', False),
                    not requirements.get('expectancy', False),
                    metrics.get('be_stop_rate', 1.0) > 0.60
                ])
                
                if critical_failures == 0:
                    self.current_phase = RecoveryPhase.VALIDATION
        
        elif self.current_phase == RecoveryPhase.VALIDATION:
            # Move to CONFIRMED if all requirements met
            if metrics.get('sample_size', 0) >= self.requirements.min_trades:
                requirements = self._check_requirements(metrics)
                if all(requirements.values()):
                    self.current_phase = RecoveryPhase.CONFIRMED
    
    def _check_recovery_outcome(self, metrics: Dict) -> None:
        """Check if recovery succeeded or failed"""
        
        # Check timeout
        if self.recovery_started:
            days_elapsed = (datetime.now(UTC) - self.recovery_started).days
            if days_elapsed > self.RECOVERY_TIMEOUT_DAYS:
                self._fail_recovery("Recovery timeout exceeded")
                return
        
        # Check for early failure signals
        if metrics.get('sample_size', 0) >= self.requirements.min_trades:
            requirements = self._check_requirements(metrics)
            
            # Critical requirements that must be met
            critical_reqs = ['win_rate', 'expectancy', 'mfe_mae']
            critical_failures = sum([not requirements.get(r, False) for r in critical_reqs])
            
            if critical_failures >= 2:
                self._fail_recovery("Failed critical requirements")
                return
        
        # Check for confirmed recovery
        if self.current_phase == RecoveryPhase.CONFIRMED:
            self._complete_recovery(metrics)
    
    def _calculate_recovery_confidence(self,
                                      metrics: Dict,
                                      requirements_met: Dict) -> float:
        """Calculate confidence in recovery (0-1)"""
        if not metrics or not requirements_met:
            return 0.0
        
        # Base confidence on requirements met
        req_score = sum(requirements_met.values()) / len(requirements_met)
        
        # Adjust for sample size
        sample_size = metrics.get('sample_size', 0)
        size_factor = min(sample_size / self.requirements.min_trades, 1.0)
        
        # Adjust for metric quality
        quality_score = 0.0
        if metrics.get('win_rate', 0) > self.requirements.min_win_rate:
            quality_score += 0.2
        if metrics.get('expectancy', 0) > self.requirements.min_expectancy:
            quality_score += 0.2
        if metrics.get('mfe_mae_ratio', 0) > self.requirements.min_mfe_mae_ratio:
            quality_score += 0.2
        
        confidence = (req_score * 0.6) + (size_factor * 0.2) + (quality_score * 0.2)
        
        return min(confidence, 1.0)
    
    def _estimate_completion_trades(self, requirements_met: Dict) -> int:
        """Estimate how many more trades needed for recovery"""
        current_trades = len(self.recovery_trades)
        
        if current_trades >= self.requirements.min_trades:
            if all(requirements_met.values()):
                return 0  # Already complete
            else:
                return 5  # Need more trades to prove improvement
        
        return self.requirements.min_trades - current_trades
    
    def _complete_recovery(self, final_metrics: Dict) -> None:
        """Mark recovery as successful"""
        attempt = RecoveryAttempt(
            attempt_number=self.current_attempt_number,
            started_at=self.recovery_started,
            ended_at=datetime.now(UTC),
            final_phase=RecoveryPhase.CONFIRMED,
            trades_in_attempt=len(self.recovery_trades),
            success=True,
            failure_reason=None,
            metrics_at_end=final_metrics
        )
        
        self.recovery_attempts.append(attempt)
        self._reset_recovery_state()
    
    def _fail_recovery(self, reason: str) -> None:
        """Mark recovery as failed"""
        final_metrics = self._calculate_recovery_metrics()
        
        attempt = RecoveryAttempt(
            attempt_number=self.current_attempt_number,
            started_at=self.recovery_started,
            ended_at=datetime.now(UTC),
            final_phase=RecoveryPhase.FAILED,
            trades_in_attempt=len(self.recovery_trades),
            success=False,
            failure_reason=reason,
            metrics_at_end=final_metrics
        )
        
        self.recovery_attempts.append(attempt)
        self.current_phase = RecoveryPhase.FAILED
    
    def _reset_recovery_state(self) -> None:
        """Reset recovery state after completion"""
        self.current_phase = RecoveryPhase.NONE
        self.recovery_started = None
        self.recovery_trades = []
    
    def is_recovery_confirmed(self) -> bool:
        """Check if recovery is confirmed and strategy can reactivate"""
        return self.current_phase == RecoveryPhase.CONFIRMED
    
    def is_recovery_failed(self) -> bool:
        """Check if recovery failed"""
        return self.current_phase == RecoveryPhase.FAILED
    
    def can_retry_recovery(self) -> Tuple[bool, str]:
        """
        Check if another recovery attempt is allowed.
        
        Returns:
            Tuple of (can_retry, reason)
        """
        if len(self.recovery_attempts) >= self.MAX_RECOVERY_ATTEMPTS:
            return False, "Maximum recovery attempts exhausted"
        
        if self.recovery_attempts:
            last_attempt = self.recovery_attempts[-1]
            if not last_attempt.success and last_attempt.ended_at:
                cooldown_end = last_attempt.ended_at + timedelta(
                    days=self.FAILED_ATTEMPT_COOLDOWN_DAYS
                )
                if datetime.now(UTC) < cooldown_end:
                    days_left = (cooldown_end - datetime.now(UTC)).days
                    return False, f"Cooldown active: {days_left} days remaining"
        
        return True, "Recovery attempt allowed"
    
    def get_recovery_summary(self) -> Dict:
        """Get summary of recovery status"""
        return {
            'current_phase': self.current_phase.value,
            'trades_in_recovery': len(self.recovery_trades),
            'recovery_started': self.recovery_started.isoformat() if self.recovery_started else None,
            'attempt_number': self.current_attempt_number,
            'total_attempts': len(self.recovery_attempts),
            'attempts_remaining': self.MAX_RECOVERY_ATTEMPTS - len(self.recovery_attempts),
            'can_retry': self.can_retry_recovery()[0],
            'recent_attempts': [
                {
                    'attempt': a.attempt_number,
                    'success': a.success,
                    'trades': a.trades_in_attempt,
                    'failure_reason': a.failure_reason
                }
                for a in self.recovery_attempts[-3:]
            ]
        }
