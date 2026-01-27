"""
Degradation Monitor - Real-time tracking of edge health signals

Monitors the 4 early warning indicators:
1. MFE/MAE ratio decline
2. Increase in BE stops
3. Winners not extending to TP2
4. Avg trade duration increase without profit

These signals lead losses by 10-15 trades.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from datetime import datetime, timedelta, UTC
from collections import deque
import numpy as np


@dataclass
class SignalTimestamp:
    """Records when each degradation signal was detected"""
    signal_type: str  # 'mfe_mae', 'be_stops', 'no_extend', 'duration'
    detected_at: datetime
    severity: float  # 0.0-1.0
    trade_count: int


class DegradationMonitor:
    """
    Real-time monitor for edge degradation.
    
    Tracks leading indicators that precede visible PnL degradation.
    Maintains rolling window of recent trades for continuous analysis.
    
    PERFORMANCE FIX (Jan 17, 2026): Added metrics caching to avoid O(n) 
    recalculation on every query. Metrics are recalculated only when 
    trades are added, not on every has_active_degradation() call.
    """
    
    def __init__(self, window_size: int = 20):
        """
        Initialize monitor.
        
        Args:
            window_size: Rolling window of trades to analyze (typically 20)
        """
        self.window_size = window_size
        self.recent_trades: deque = deque(maxlen=window_size)
        self.signal_history: List[SignalTimestamp] = []
        self.degradation_alerts: List[Dict] = []
        
        # Thresholds (same as EdgeValidator)
        self.MFE_MAE_THRESHOLD = 1.5
        self.BE_STOP_THRESHOLD = 0.40
        self.NON_EXTEND_THRESHOLD = 0.60
        self.AVG_DURATION_THRESHOLD = 12.0  # hours
        
        # Alert configuration
        self.SIGNAL_THRESHOLD = 2  # 2+ signals trigger alert
        self.SIGNAL_RETENTION_DAYS = 30
        
        # 🔥 PERFORMANCE FIX: Cache metrics to avoid O(n) recalculation on every query
        self._cached_metrics: Dict | None = None
        self._metrics_dirty: bool = True  # Flag to indicate cache needs refresh
    
    def add_trade(self,
                  trade_id: int,
                  entry_price: float,
                  exit_price: float,
                  mfe: float,
                  mae: float,
                  duration_hours: float,
                  reached_tp1: bool,
                  reached_tp2: bool,
                  is_be_stop: bool) -> None:
        """
        Adds a completed trade to monitoring window.
        
        Args:
            trade_id: Unique identifier
            entry_price: Entry price
            exit_price: Exit price
            mfe: Max Favorable Excursion
            mae: Max Adverse Excursion
            duration_hours: Trade duration in hours
            reached_tp1: Reached first take profit
            reached_tp2: Reached second take profit
            is_be_stop: Exited at break-even
        """
        trade = {
            'id': trade_id,
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
        self.recent_trades.append(trade)
        
        # 🔥 PERFORMANCE FIX: Mark cache as dirty when trades are added
        self._metrics_dirty = True
        
        self._analyze_window()
    
    def _analyze_window(self) -> None:
        """Analyzes rolling window for degradation signals"""
        if len(self.recent_trades) < self.window_size:
            return  # Need full window
        
        trades_list = list(self.recent_trades)
        active_signals = 0
        
        # Signal 1: MFE/MAE Ratio
        if self._check_mfe_mae_degradation(trades_list):
            active_signals += 1
        
        # Signal 2: BE Stops
        if self._check_be_stop_degradation(trades_list):
            active_signals += 1
        
        # Signal 3: Non-extending winners
        if self._check_extension_degradation(trades_list):
            active_signals += 1
        
        # Signal 4: Duration increase
        if self._check_duration_degradation(trades_list):
            active_signals += 1
        
        # Alert if 2+ signals
        if active_signals >= self.SIGNAL_THRESHOLD:
            self._record_alert(active_signals)
    
    def _check_mfe_mae_degradation(self, trades: List[Dict]) -> bool:
        """
        Checks if MFE/MAE ratio has degraded.
        
        Healthy: ratio > 1.5
        Degraded: ratio <= 1.5
        """
        mfes = [t['mfe'] for t in trades if t['mfe'] > 0]
        maes = [t['mae'] for t in trades if t['mae'] > 0]
        
        if len(mfes) == 0 or len(maes) == 0:
            return False
        
        ratio = float(np.mean(mfes)) / float(np.mean(maes))
        
        if ratio <= self.MFE_MAE_THRESHOLD:
            severity = float(1.0 - min(ratio / self.MFE_MAE_THRESHOLD, 1.0))
            self._record_signal('mfe_mae', severity)
            return True
        
        return False
    
    def _check_be_stop_degradation(self, trades: List[Dict]) -> bool:
        """
        Checks if BE stops percentage has increased.
        
        Healthy: < 40% of trades
        Degraded: >= 40% of trades
        """
        be_stops = sum(1 for t in trades if t['be_stop'])
        be_stop_pct = be_stops / len(trades)
        
        if be_stop_pct >= self.BE_STOP_THRESHOLD:
            severity = min(be_stop_pct / self.BE_STOP_THRESHOLD, 1.0)
            self._record_signal('be_stops', severity)
            return True
        
        return False
    
    def _check_extension_degradation(self, trades: List[Dict]) -> bool:
        """
        Checks if winners are failing to extend to TP2.
        
        Healthy: > 40% of winners extend
        Degraded: <= 40% of winners extend
        """
        winners = [t for t in trades if t['is_winner'] and t['tp1']]
        
        if len(winners) == 0:
            return False
        
        extending = sum(1 for t in winners if t['tp2'])
        extend_pct = extending / len(winners)
        non_extend_pct = 1.0 - extend_pct
        
        if non_extend_pct >= self.NON_EXTEND_THRESHOLD:
            severity = min(non_extend_pct / self.NON_EXTEND_THRESHOLD, 1.0)
            self._record_signal('no_extend', severity)
            return True
        
        return False
    
    def _check_duration_degradation(self, trades: List[Dict]) -> bool:
        """
        Checks if average trade duration is increasing.
        
        Healthy: < 12 hours average
        Degraded: >= 12 hours average
        """
        durations = [t['duration'] for t in trades]
        avg_duration = float(np.mean(durations))
        
        if avg_duration >= self.AVG_DURATION_THRESHOLD:
            severity = float(min(avg_duration / self.AVG_DURATION_THRESHOLD, 1.0))
            self._record_signal('duration', severity)
            return True
        
        return False
    
    def _record_signal(self, signal_type: str, severity: float) -> None:
        """Records a degradation signal"""
        signal = SignalTimestamp(
            signal_type=signal_type,
            detected_at=datetime.now(UTC),
            severity=severity,
            trade_count=len(self.recent_trades)
        )
        self.signal_history.append(signal)
    
    def _record_alert(self, active_signals: int) -> None:
        """Records when alert threshold is reached"""
        alert = {
            'timestamp': datetime.now(UTC),
            'signal_count': active_signals,
            'trade_count': len(self.recent_trades),
            'window_metrics': self.get_current_metrics()
        }
        self.degradation_alerts.append(alert)
    
    def get_current_metrics(self) -> Dict:
        """
        Returns current metrics from rolling window.
        
        🔥 PERFORMANCE FIX (Jan 17, 2026): Uses caching to avoid O(n) 
        recalculation on every call. Cache is invalidated when trades are added.
        
        Returns:
            Dict with all 4 degradation metrics
        """
        # Return cached metrics if still valid
        if not self._metrics_dirty and self._cached_metrics is not None:
            return self._cached_metrics
        
        if len(self.recent_trades) == 0:
            self._cached_metrics = {
                'mfe_mae_ratio': 0.0,
                'be_stop_pct': 0.0,
                'non_extend_pct': 0.0,
                'avg_duration_h': 0.0
            }
            self._metrics_dirty = False
            return self._cached_metrics
        
        trades = list(self.recent_trades)
        
        # MFE/MAE
        mfes = [t['mfe'] for t in trades if t['mfe'] > 0]
        maes = [t['mae'] for t in trades if t['mae'] > 0]
        mfe_mae_ratio = (np.mean(mfes) / np.mean(maes)) if (len(mfes) > 0 and len(maes) > 0) else 0.0
        
        # BE Stops
        be_stops = sum(1 for t in trades if t['be_stop'])
        be_stop_pct = be_stops / len(trades)
        
        # Non-extending winners
        winners = [t for t in trades if t['is_winner'] and t['tp1']]
        non_extend_pct = 0.0
        if len(winners) > 0:
            extending = sum(1 for t in winners if t['tp2'])
            non_extend_pct = 1.0 - (extending / len(winners))
        
        # Duration
        durations = [t['duration'] for t in trades]
        avg_duration = np.mean(durations)
        
        # UNITS: All percentages returned as FRACTIONS (0.0-1.0) for consistency
        # This matches threshold comparisons and avoids confusion
        self._cached_metrics = {
            'mfe_mae_ratio': round(mfe_mae_ratio, 2),
            'be_stop_pct': round(be_stop_pct, 4),  # FRACTION (0.40 = 40%)
            'non_extend_pct': round(non_extend_pct, 4),  # FRACTION (0.60 = 60%)
            'avg_duration_h': round(avg_duration, 1)
        }
        self._metrics_dirty = False
        return self._cached_metrics
    
    def get_current_metrics_display(self) -> Dict:
        """
        Returns current metrics formatted for display (percentages as 0-100).
        
        Use this for logging/UI, use get_current_metrics() for calculations.
        """
        metrics = self.get_current_metrics()
        return {
            'mfe_mae_ratio': metrics['mfe_mae_ratio'],
            'be_stop_pct': round(metrics['be_stop_pct'] * 100, 1),  # Display as %
            'non_extend_pct': round(metrics['non_extend_pct'] * 100, 1),  # Display as %
            'avg_duration_h': metrics['avg_duration_h']
        }
    
    def get_active_signals(self) -> int:
        """
        Returns count of currently active degradation signals (0-4).
        
        All comparisons use FRACTIONS (0.0-1.0) for consistency.
        """
        signals = 0
        metrics = self.get_current_metrics()
        
        # MFE/MAE ratio: lower is worse
        if metrics['mfe_mae_ratio'] > 0 and metrics['mfe_mae_ratio'] <= self.MFE_MAE_THRESHOLD:
            signals += 1
        
        # BE stops: higher is worse (fraction comparison)
        if metrics['be_stop_pct'] >= self.BE_STOP_THRESHOLD:
            signals += 1
        
        # Non-extending winners: higher is worse (fraction comparison)
        if metrics['non_extend_pct'] >= self.NON_EXTEND_THRESHOLD:
            signals += 1
        
        # Duration: higher is worse
        if metrics['avg_duration_h'] >= self.AVG_DURATION_THRESHOLD:
            signals += 1
        
        return signals
    
    def get_health_status(self) -> str:
        """
        Returns human-readable health status.
        
        Returns:
            'HEALTHY', 'WARNING', 'CRITICAL', or 'SEVERE'
        """
        signals = self.get_active_signals()
        
        if signals == 0:
            return "HEALTHY"
        elif signals == 1:
            return "WARNING"
        elif signals == 2:
            return "CRITICAL"
        else:  # signals >= 3
            return "SEVERE"
    
    def has_active_degradation(self) -> bool:
        """
        Returns True if 2+ signals active (should trigger alert).
        """
        return self.get_active_signals() >= self.SIGNAL_THRESHOLD
    
    def get_latest_alert(self) -> Dict:
        """
        Returns most recent alert or None.
        
        Returns:
            Dict with alert details or empty dict if no alerts
        """
        # Clean old signals (> 30 days)
        cutoff = datetime.now(UTC) - timedelta(days=self.SIGNAL_RETENTION_DAYS)
        self.signal_history = [s for s in self.signal_history if s.detected_at > cutoff]
        self.degradation_alerts = [a for a in self.degradation_alerts if a['timestamp'] > cutoff]
        
        if len(self.degradation_alerts) == 0:
            return {}
        
        return self.degradation_alerts[-1]
    
    def reset_alerts(self) -> None:
        """Clears alert history (use after recovery)"""
        self.signal_history = []
        self.degradation_alerts = []
        # Invalidate cache when resetting
        self._metrics_dirty = True
        self._cached_metrics = None
    
    def get_summary_report(self) -> Dict:
        """
        Returns comprehensive monitoring summary.
        
        Returns:
            Dict with all metrics and status
        """
        return {
            'timestamp': datetime.now(UTC).isoformat(),
            'health_status': self.get_health_status(),
            'active_signals': self.get_active_signals(),
            'has_degradation': self.has_active_degradation(),
            'metrics': self.get_current_metrics(),
            'window_trades': len(self.recent_trades),
            'total_signals_recorded': len(self.signal_history),
            'total_alerts': len(self.degradation_alerts),
            'latest_alert': self.get_latest_alert()
        }
