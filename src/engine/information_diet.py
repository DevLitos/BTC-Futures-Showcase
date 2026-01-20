"""
Information Diet - Controlled information exposure to prevent emotional intervention

CRITICAL INSIGHT:
"The trader doesn't decide" is the stated policy.
But if the trader SEES everything, they can intervene emotionally.

Logging, monitoring, reports = back door to intervention.

PHILOSOPHY:
- Information is NOT neutral
- Transparency ≠ visibility of everything
- Trader needs operational info, not diagnostic internals
- Excess information creates intervention pressure
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Any, Optional
from datetime import datetime, UTC


class InformationTier(Enum):
    """Information access tiers"""
    OPERATOR = "operational_only"      # What trader needs to execute
    SUPERVISOR = "performance_summary" # High-level performance only
    DIAGNOSTIC = "full_internals"      # Everything (dangerous)


@dataclass
class FilteredMetrics:
    """Metrics filtered for trader consumption"""
    can_trade: bool
    position_size_multiplier: float
    trades_today: int
    current_state: str  # ACTIVE/DEGRADED/OBSERVATION/DEAD (simple)
    last_update: datetime
    
    # Deliberately excluded:
    # - Expectancy values
    # - Degradation signal counts
    # - MFE/MAE internals
    # - Window statistics
    # - Attribution details


@dataclass
class OperatorView:
    """What the trader/operator should see"""
    # Trade execution
    can_trade: bool
    reason_if_blocked: Optional[str]  # High-level only
    position_size_multiplier: float
    
    # Simple counters
    trades_today: int
    trades_this_week: int
    
    # Basic status
    system_status: str  # "ACTIVE", "REDUCED", "PAUSED", "STOPPED"
    
    # Deliberately NO:
    # - Signal counts
    # - Degradation metrics
    # - Expectancy
    # - Win rates
    # - Detailed reasons


@dataclass
class SupervisorView:
    """What a supervisor/reviewer should see (post-session)"""
    # Daily summary
    date: datetime
    trades_taken: int
    system_status: str
    
    # Aggregate only (no intraday detail)
    daily_pnl: float
    weekly_pnl: float
    
    # Trend direction only
    performance_trend: str  # "improving", "stable", "declining"
    
    # Deliberately NO:
    # - Individual trade metrics
    # - Real-time degradation signals
    # - Attribution details


class InformationDiet:
    """
    Filters system information based on access tier.
    
    PURPOSE:
    - Prevent information overload → emotional decisions
    - Separate operational needs from diagnostic data
    - Enforce discipline through controlled visibility
    
    RULES:
    1. Trader gets ONLY operational info (can_trade, size)
    2. Detailed metrics are logged but NOT displayed
    3. Diagnostic access requires explicit override (backtest only)
    """
    
    def __init__(self, default_tier: InformationTier = InformationTier.OPERATOR):
        """
        Initialize information filter.
        
        Args:
            default_tier: Default access level
        """
        self.current_tier = default_tier
        self.access_overrides: Dict[str, InformationTier] = {}
        self.exposure_log: List[Dict] = []
        
        # Safety limits
        self.MAX_DETAIL_REQUESTS_PER_DAY = 3  # Limit diagnostic access
        self.detail_requests_today = 0
        self.last_request_date = datetime.now(UTC).date()
    
    def get_operator_view(self,
                         can_trade: bool,
                         blocked_reasons: List[str],
                         size_multiplier: float,
                         trades_today: int,
                         trades_week: int,
                         system_state: str) -> OperatorView:
        """
        Get operator-level view (minimal info for execution).
        
        Args:
            System state information
            
        Returns:
            OperatorView with only execution-relevant info
        """
        # Simplify blocked reasons to high-level only
        simple_reason = None
        if not can_trade and blocked_reasons:
            # Map detailed reasons to simple categories
            simple_reason = self._simplify_blocked_reason(blocked_reasons)
        
        # Map system state to simple status
        simple_status = self._map_system_status(system_state, size_multiplier)
        
        view = OperatorView(
            can_trade=can_trade,
            reason_if_blocked=simple_reason,
            position_size_multiplier=size_multiplier,
            trades_today=trades_today,
            trades_this_week=trades_week,
            system_status=simple_status
        )
        
        # Log exposure
        self._log_exposure('operator', view.__dict__)
        
        return view
    
    def get_supervisor_view(self,
                           date: datetime,
                           trades_taken: int,
                           system_status: str,
                           daily_pnl: float,
                           weekly_pnl: float,
                           performance_metrics: Dict) -> SupervisorView:
        """
        Get supervisor-level view (daily summaries only).
        
        Args:
            Daily/weekly aggregates
            
        Returns:
            SupervisorView with summary information
        """
        # Determine trend from metrics without exposing details
        trend = self._determine_performance_trend(performance_metrics)
        
        view = SupervisorView(
            date=date,
            trades_taken=trades_taken,
            system_status=system_status,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            performance_trend=trend
        )
        
        # Log exposure
        self._log_exposure('supervisor', view.__dict__)
        
        return view
    
    def get_diagnostic_view(self,
                           full_metrics: Dict,
                           override_key: Optional[str] = None) -> Optional[Dict]:
        """
        Get diagnostic view (full internals).
        
        DANGEROUS: Only for backtesting or authorized debugging.
        
        Args:
            full_metrics: Complete system metrics
            override_key: Authorization key for live access
            
        Returns:
            Full metrics if authorized, None otherwise
        """
        # Check if diagnostic access is allowed
        if not self._authorize_diagnostic_access(override_key):
            return None
        
        # Log exposure (with warning)
        self._log_exposure('diagnostic', {'warning': 'full_access_granted'})
        
        # Increment request counter
        self._increment_detail_requests()
        
        return full_metrics
    
    def _simplify_blocked_reason(self, detailed_reasons: List[str]) -> str:
        """Convert detailed blocking reasons to simple message"""
        
        # Map detailed reasons to simple categories
        if any('degradation' in r.lower() for r in detailed_reasons):
            return "System paused for quality assessment"
        
        if any('context' in r.lower() for r in detailed_reasons):
            return "Market conditions not suitable"
        
        if any('drawdown' in r.lower() for r in detailed_reasons):
            return "Daily limit reached"
        
        if any('observation' in r.lower() for r in detailed_reasons):
            return "System in monitoring mode"
        
        return "Trading paused"
    
    def _map_system_status(self, state: str, size_multiplier: float) -> str:
        """Map detailed state to simple status"""
        
        if size_multiplier == 0.0:
            return "STOPPED"
        elif size_multiplier < 0.75:
            return "REDUCED"
        elif state.upper() == "ACTIVE":
            return "ACTIVE"
        else:
            return "PAUSED"
    
    def _determine_performance_trend(self, metrics: Dict) -> str:
        """Determine trend without exposing detailed metrics"""
        
        # Look at recent vs baseline without showing numbers
        recent_expectancy = metrics.get('recent_expectancy', 0)
        baseline_expectancy = metrics.get('baseline_expectancy', 0)
        
        if recent_expectancy > baseline_expectancy * 1.1:
            return "improving"
        elif recent_expectancy < baseline_expectancy * 0.9:
            return "declining"
        else:
            return "stable"
    
    def _authorize_diagnostic_access(self, override_key: Optional[str]) -> bool:
        """Check if diagnostic access is authorized"""
        
        # In production, this would check actual authorization
        # For now, allow only with explicit override or in backtest mode
        
        if override_key == "BACKTEST_MODE":
            return True
        
        # Check daily limit
        current_date = datetime.now(UTC).date()
        if current_date != self.last_request_date:
            self.detail_requests_today = 0
            self.last_request_date = current_date
        
        if self.detail_requests_today >= self.MAX_DETAIL_REQUESTS_PER_DAY:
            return False
        
        # Require explicit override key in production
        return override_key is not None
    
    def _increment_detail_requests(self) -> None:
        """Increment diagnostic access counter"""
        current_date = datetime.now(UTC).date()
        if current_date != self.last_request_date:
            self.detail_requests_today = 0
            self.last_request_date = current_date
        
        self.detail_requests_today += 1
    
    def _log_exposure(self, tier: str, data: Dict) -> None:
        """Log what information was exposed"""
        entry = {
            'timestamp': datetime.now(UTC),
            'tier': tier,
            'data_keys': list(data.keys()),
            'access_tier': self.current_tier.value
        }
        
        self.exposure_log.append(entry)
        
        # Limit log size
        if len(self.exposure_log) > 1000:
            self.exposure_log = self.exposure_log[-1000:]
    
    def set_access_tier(self, tier: InformationTier) -> None:
        """
        Change access tier.
        
        NOTE: Changing to DIAGNOSTIC tier should be rare and logged.
        """
        old_tier = self.current_tier
        self.current_tier = tier
        
        if tier == InformationTier.DIAGNOSTIC:
            self._log_exposure('tier_change', {
                'from': old_tier.value,
                'to': tier.value,
                'warning': 'diagnostic_access_enabled'
            })
    
    def get_safe_log_output(self, full_metrics: Dict) -> Dict:
        """
        Filter metrics for safe logging.
        
        Returns safe subset that won't trigger emotional decisions.
        
        Args:
            full_metrics: Complete metrics dict
            
        Returns:
            Filtered metrics safe for general logging
        """
        safe_metrics = {
            'timestamp': full_metrics.get('timestamp'),
            'can_trade': full_metrics.get('can_trade'),
            'size_multiplier': full_metrics.get('size_multiplier'),
            'system_state': full_metrics.get('system_state'),
            'trades_count': full_metrics.get('trades_count'),
        }
        
        # Add high-level status only
        if 'degradation_level' in full_metrics:
            # Convert numeric level to simple status
            level = full_metrics['degradation_level']
            safe_metrics['health'] = 'good' if level < 2 else 'monitoring'
        
        return safe_metrics
    
    def should_suppress_alert(self, alert_type: str, severity: str) -> bool:
        """
        Determine if alert should be suppressed for current tier.
        
        Args:
            alert_type: Type of alert (e.g., 'degradation', 'context', 'regime')
            severity: Severity level ('low', 'medium', 'high')
            
        Returns:
            True if alert should be suppressed
        """
        if self.current_tier == InformationTier.OPERATOR:
            # Operators only see critical alerts
            if severity != 'high':
                return True
            
            # And only certain types
            if alert_type not in ['system_stopped', 'critical_error']:
                return True
        
        elif self.current_tier == InformationTier.SUPERVISOR:
            # Supervisors see high and medium
            if severity == 'low':
                return True
        
        # DIAGNOSTIC tier sees everything
        return False
    
    def format_trader_message(self, can_trade: bool, state: str, size: float) -> str:
        """
        Format simple message for trader display.
        
        Returns:
            Clean, actionable message without diagnostic details
        """
        if not can_trade:
            return f"Trading paused. System status: {state}"
        
        if size < 1.0:
            return f"Trading active at {int(size * 100)}% size"
        
        return "Trading active - full size"
    
    def get_exposure_summary(self) -> Dict:
        """Get summary of information exposure"""
        recent_exposures = self.exposure_log[-100:]
        
        tier_counts = {}
        for entry in recent_exposures:
            tier = entry['tier']
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        
        return {
            'current_tier': self.current_tier.value,
            'diagnostic_requests_today': self.detail_requests_today,
            'recent_exposures': tier_counts,
            'diagnostic_limit_remaining': (
                self.MAX_DETAIL_REQUESTS_PER_DAY - self.detail_requests_today
            )
        }
