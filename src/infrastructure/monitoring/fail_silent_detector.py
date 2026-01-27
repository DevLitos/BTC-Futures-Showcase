"""
Fail-Silent Detector - Detects silent edge erosion

CRITICAL PROBLEM:
Most dangerous failure mode:
- No errors
- No drawdown
- System still "ACTIVE"
- But edge is dead

System captures NOTHING while market moves.

This is SILENT DEGRADATION - invisible edge death.

PHILOSOPHY:
- Absence of losses ≠ presence of edge
- Must track opportunity capture, not just results
- Silent failure is worse than loud failure
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC
import numpy as np


@dataclass
class MarketOpportunity:
    """Record of a significant market move"""
    timestamp: datetime
    move_size_r: float          # Move size in R units
    duration_hours: float
    was_captured: bool          # Did strategy capture it?
    capture_percentage: float   # % of move captured (0-100)
    reason_if_missed: Optional[str]


@dataclass
class OpportunityCaptureMetrics:
    """Metrics for opportunity capture analysis"""
    total_opportunities: int
    opportunities_captured: int
    opportunities_missed: int
    avg_capture_rate: float          # Average % captured when traded
    opportunity_participation: float  # % of opportunities traded
    silent_degradation_score: float  # 0-1, higher = worse


@dataclass
class SilentDegradationAlert:
    """Alert for silent edge erosion"""
    detected_at: datetime
    severity: str  # 'warning', 'critical', 'severe'
    capture_rate: float
    missed_opportunities: int
    reasoning: List[str]
    recommended_action: str


class FailSilentDetector:
    """
    Detects silent edge degradation through opportunity capture analysis.
    
    CONCEPT:
    - Market provides opportunities (moves > threshold)
    - Strategy should capture significant % of opportunities
    - If strategy consistently misses opportunities = silent failure
    - This is INDEPENDENT of PnL (can have no losses but no edge)
    """
    
    def __init__(self):
        """Initialize fail-silent detector"""
        self.opportunities: List[MarketOpportunity] = []
        self.alerts: List[SilentDegradationAlert] = []
        
        # Detection thresholds
        self.OPPORTUNITY_THRESHOLD_R = 1.5      # Move > 1.5R = opportunity
        self.MIN_CAPTURE_RATE = 0.30            # Should capture ≥30% of opportunities
        self.MIN_PARTICIPATION_RATE = 0.40      # Should trade ≥40% of opportunities
        self.SILENT_DEGRADATION_THRESHOLD = 0.60  # Score > 0.6 = alert
        
        # Analysis windows
        self.ROLLING_WINDOW_HOURS = 168  # 7 days
        self.MIN_OPPORTUNITIES_FOR_ANALYSIS = 5
        
    def record_market_move(self,
                          timestamp: datetime,
                          move_size_r: float,
                          duration_hours: float,
                          strategy_participated: bool,
                          capture_percentage: float = 0.0,
                          miss_reason: Optional[str] = None) -> None:
        """
        Record a significant market move and strategy response.
        
        Args:
            timestamp: When move occurred
            move_size_r: Size of move in R units
            duration_hours: How long move took
            strategy_participated: Did strategy take this trade?
            capture_percentage: % of move captured (0-100)
            miss_reason: Why opportunity was missed (if applicable)
        """
        # Only track significant opportunities
        if move_size_r < self.OPPORTUNITY_THRESHOLD_R:
            return
        
        opportunity = MarketOpportunity(
            timestamp=timestamp,
            move_size_r=move_size_r,
            duration_hours=duration_hours,
            was_captured=strategy_participated and capture_percentage > 20,
            capture_percentage=capture_percentage,
            reason_if_missed=miss_reason if not strategy_participated else None
        )
        
        self.opportunities.append(opportunity)
        
        # Limit history
        if len(self.opportunities) > 200:
            self.opportunities = self.opportunities[-200:]
        
        # Check for silent degradation
        self._check_silent_degradation()
    
    def _check_silent_degradation(self) -> None:
        """Check for silent degradation in recent opportunities"""
        
        # Get recent opportunities
        cutoff = datetime.now(UTC) - timedelta(hours=self.ROLLING_WINDOW_HOURS)
        recent_opps = [
            opp for opp in self.opportunities
            if opp.timestamp >= cutoff
        ]
        
        if len(recent_opps) < self.MIN_OPPORTUNITIES_FOR_ANALYSIS:
            return  # Insufficient data
        
        # Calculate metrics
        metrics = self._calculate_capture_metrics(recent_opps)
        
        # Check for silent degradation
        if metrics.silent_degradation_score >= self.SILENT_DEGRADATION_THRESHOLD:
            self._create_alert(metrics, recent_opps)
    
    def _calculate_capture_metrics(self,
                                   opportunities: List[MarketOpportunity]
                                   ) -> OpportunityCaptureMetrics:
        """Calculate opportunity capture metrics"""
        
        if not opportunities:
            return OpportunityCaptureMetrics(
                total_opportunities=0,
                opportunities_captured=0,
                opportunities_missed=0,
                avg_capture_rate=0.0,
                opportunity_participation=0.0,
                silent_degradation_score=0.0
            )
        
        total = len(opportunities)
        captured = [opp for opp in opportunities if opp.was_captured]
        missed = [opp for opp in opportunities if not opp.was_captured]
        
        # Average capture rate (when traded)
        capture_rates = [opp.capture_percentage for opp in captured]
        avg_capture = float(np.mean(capture_rates)) if capture_rates else 0.0
        
        # Participation rate
        participation = len(captured) / total if total > 0 else 0.0
        
        # Calculate silent degradation score
        degradation_score = self._calculate_degradation_score(
            participation,
            avg_capture,
            missed
        )
        
        return OpportunityCaptureMetrics(
            total_opportunities=total,
            opportunities_captured=len(captured),
            opportunities_missed=len(missed),
            avg_capture_rate=avg_capture,
            opportunity_participation=participation,
            silent_degradation_score=degradation_score
        )
    
    def _calculate_degradation_score(self,
                                    participation: float,
                                    avg_capture: float,
                                    missed_opps: List[MarketOpportunity]) -> float:
        """
        Calculate silent degradation score (0-1).
        
        Higher score = worse silent degradation.
        
        Factors:
        1. Low participation rate
        2. Low capture rate when participating
        3. Missing large opportunities
        4. Consistent pattern of missing
        """
        score = 0.0
        
        # Factor 1: Low participation
        if participation < self.MIN_PARTICIPATION_RATE:
            participation_penalty = (self.MIN_PARTICIPATION_RATE - participation) / self.MIN_PARTICIPATION_RATE
            score += participation_penalty * 0.40
        
        # Factor 2: Low capture rate
        if avg_capture < self.MIN_CAPTURE_RATE * 100:  # avg_capture is in %
            capture_penalty = (self.MIN_CAPTURE_RATE * 100 - avg_capture) / (self.MIN_CAPTURE_RATE * 100)
            score += capture_penalty * 0.30
        
        # Factor 3: Missing large opportunities
        if missed_opps:
            large_misses = [opp for opp in missed_opps if opp.move_size_r >= 2.0]
            if large_misses:
                large_miss_ratio = len(large_misses) / len(missed_opps)
                score += large_miss_ratio * 0.20
        
        # Factor 4: Consistency of missing
        if len(missed_opps) >= 3:
            # Check if misses are clustered (recent pattern)
            recent_misses = sorted(missed_opps, key=lambda x: x.timestamp, reverse=True)[:5]
            if len(recent_misses) >= 3:
                score += 0.10
        
        return min(score, 1.0)
    
    def _create_alert(self,
                     metrics: OpportunityCaptureMetrics,
                     opportunities: List[MarketOpportunity]) -> None:
        """Create silent degradation alert"""
        
        # Determine severity
        if metrics.silent_degradation_score >= 0.80:
            severity = 'severe'
        elif metrics.silent_degradation_score >= 0.70:
            severity = 'critical'
        else:
            severity = 'warning'
        
        # Build reasoning
        reasoning = []
        
        if metrics.opportunity_participation < self.MIN_PARTICIPATION_RATE:
            reasoning.append(
                f"Low participation: {metrics.opportunity_participation:.1%} "
                f"(threshold: {self.MIN_PARTICIPATION_RATE:.1%})"
            )
        
        if metrics.avg_capture_rate < self.MIN_CAPTURE_RATE * 100:
            reasoning.append(
                f"Low capture rate: {metrics.avg_capture_rate:.1f}% "
                f"(threshold: {self.MIN_CAPTURE_RATE * 100:.1f}%)"
            )
        
        # Check for pattern in missed opportunities
        missed_reasons = [
            opp.reason_if_missed for opp in opportunities
            if not opp.was_captured and opp.reason_if_missed
        ]
        if missed_reasons:
            reason_counts = {}
            for reason in missed_reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            most_common = max(reason_counts, key=lambda k: reason_counts.get(k, 0) or 0)
            reasoning.append(f"Common miss reason: {most_common}")
        
        # Determine action
        if severity == 'severe':
            action = "IMMEDIATE_REVIEW - Edge likely dead, no opportunity capture"
        elif severity == 'critical':
            action = "DEEP_ANALYSIS - Investigate opportunity capture failure"
        else:
            action = "MONITOR - Watch for continued capture degradation"
        
        alert = SilentDegradationAlert(
            detected_at=datetime.now(UTC),
            severity=severity,
            capture_rate=metrics.avg_capture_rate,
            missed_opportunities=metrics.opportunities_missed,
            reasoning=reasoning,
            recommended_action=action
        )
        
        self.alerts.append(alert)
        
        # Limit alert history
        if len(self.alerts) > 50:
            self.alerts = self.alerts[-50:]
    
    def get_current_status(self) -> Tuple[bool, Optional[SilentDegradationAlert]]:
        """
        Get current silent degradation status.
        
        Returns:
            Tuple of (is_degraded, latest_alert)
        """
        # Get recent opportunities
        cutoff = datetime.now(UTC) - timedelta(hours=self.ROLLING_WINDOW_HOURS)
        recent_opps = [
            opp for opp in self.opportunities
            if opp.timestamp >= cutoff
        ]
        
        if len(recent_opps) < self.MIN_OPPORTUNITIES_FOR_ANALYSIS:
            return False, None
        
        # Calculate current metrics
        metrics = self._calculate_capture_metrics(recent_opps)
        
        # Check if degraded
        is_degraded = metrics.silent_degradation_score >= self.SILENT_DEGRADATION_THRESHOLD
        
        # Get latest alert
        latest_alert = self.alerts[-1] if self.alerts else None
        
        return is_degraded, latest_alert
    
    def analyze_opportunity_pattern(self,
                                   lookback_hours: int = 168
                                   ) -> Dict:
        """
        Analyze opportunity capture patterns.
        
        Args:
            lookback_hours: Hours to look back (default 7 days)
            
        Returns:
            Analysis dict with patterns and insights
        """
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        recent_opps = [
            opp for opp in self.opportunities
            if opp.timestamp >= cutoff
        ]
        
        if not recent_opps:
            return {'status': 'insufficient_data'}
        
        # Calculate metrics
        metrics = self._calculate_capture_metrics(recent_opps)
        
        # Analyze miss reasons
        missed = [opp for opp in recent_opps if not opp.was_captured]
        miss_reasons = {}
        for opp in missed:
            if opp.reason_if_missed:
                miss_reasons[opp.reason_if_missed] = miss_reasons.get(opp.reason_if_missed, 0) + 1
        
        # Analyze opportunity sizes
        captured_sizes = [opp.move_size_r for opp in recent_opps if opp.was_captured]
        missed_sizes = [opp.move_size_r for opp in recent_opps if not opp.was_captured]
        
        # Trend analysis
        trend = self._analyze_capture_trend(recent_opps)
        
        return {
            'period_hours': lookback_hours,
            'total_opportunities': len(recent_opps),
            'participation_rate': metrics.opportunity_participation,
            'capture_rate': metrics.avg_capture_rate,
            'degradation_score': metrics.silent_degradation_score,
            'is_degraded': metrics.silent_degradation_score >= self.SILENT_DEGRADATION_THRESHOLD,
            'miss_reasons': miss_reasons,
            'avg_captured_size': np.mean(captured_sizes) if captured_sizes else 0,
            'avg_missed_size': np.mean(missed_sizes) if missed_sizes else 0,
            'capture_trend': trend
        }
    
    def _analyze_capture_trend(self, opportunities: List[MarketOpportunity]) -> str:
        """Analyze if capture rate is improving, stable, or declining"""
        
        if len(opportunities) < 10:
            return 'insufficient_data'
        
        # Split into first half and second half
        mid = len(opportunities) // 2
        first_half = opportunities[:mid]
        second_half = opportunities[mid:]
        
        # Calculate capture rates for each half
        first_captured = len([o for o in first_half if o.was_captured])
        first_rate = first_captured / len(first_half) if first_half else 0
        
        second_captured = len([o for o in second_half if o.was_captured])
        second_rate = second_captured / len(second_half) if second_half else 0
        
        # Determine trend
        if second_rate > first_rate * 1.15:
            return 'improving'
        elif second_rate < first_rate * 0.85:
            return 'declining'
        else:
            return 'stable'
    
    def get_opportunity_summary(self) -> Dict:
        """Get summary of opportunity tracking"""
        
        # Recent metrics (7 days)
        analysis = self.analyze_opportunity_pattern(lookback_hours=168)
        
        # Recent alerts
        recent_alerts = self.alerts[-5:] if self.alerts else []
        
        # Current status
        is_degraded, latest_alert = self.get_current_status()
        
        return {
            'current_status': 'degraded' if is_degraded else 'healthy',
            'recent_analysis': analysis,
            'total_opportunities_tracked': len(self.opportunities),
            'recent_alerts': len(recent_alerts),
            'latest_alert': {
                'severity': latest_alert.severity,
                'capture_rate': latest_alert.capture_rate,
                'action': latest_alert.recommended_action
            } if latest_alert else None
        }
    
    def requires_intervention(self) -> Tuple[bool, str]:
        """
        Check if silent degradation requires intervention.
        
        Returns:
            Tuple of (requires_intervention, reason)
        """
        is_degraded, latest_alert = self.get_current_status()
        
        if not is_degraded:
            return False, "No silent degradation detected"
        
        if latest_alert and latest_alert.severity in ['critical', 'severe']:
            return True, f"Silent degradation: {latest_alert.severity} - {latest_alert.recommended_action}"
        
        # Check if degradation is persistent
        recent_alerts = [
            a for a in self.alerts
            if a.detected_at > datetime.now(UTC) - timedelta(days=3)
        ]
        
        if len(recent_alerts) >= 3:
            return True, "Persistent silent degradation over 3+ days"
        
        return False, "Silent degradation detected but monitoring"
