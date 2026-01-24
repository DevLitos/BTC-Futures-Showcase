"""
ML Drift Detection

Monitors model performance and detects when retraining is needed.
"""

import logging
from collections import deque
from datetime import datetime, UTC
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Optional sklearn for metrics
try:
    from sklearn.metrics import roc_auc_score
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


class MLDriftDetector:
    """
    Detect when ML model performance degrades.
    
    Monitors rolling AUC and triggers alerts/retraining when needed.
    """
    
    def __init__(
        self,
        baseline_auc: float = 0.55,
        window_size: int = 50,
        alert_threshold: float = 0.85,  # Alert if AUC drops below 85% of baseline
    ):
        self.baseline_auc = baseline_auc
        self.window_size = window_size
        self.alert_threshold = alert_threshold
        
        # Rolling window of (prediction, actual) pairs
        self._predictions: deque = deque(maxlen=window_size)
        self._actuals: deque = deque(maxlen=window_size)
        
        # Statistics
        self._total_predictions = 0
        self._alerts_triggered = 0
        self._last_alert_time: Optional[datetime] = None
        self._rolling_auc_history: List[float] = []
    
    def update(self, prediction: float, actual: int) -> Optional[str]:
        """
        Update with new prediction and outcome.
        
        Args:
            prediction: Model's predicted probability
            actual: Actual outcome (1=win, 0=loss)
            
        Returns:
            Alert message if drift detected, None otherwise
        """
        self._predictions.append(prediction)
        self._actuals.append(actual)
        self._total_predictions += 1
        
        # Check every window_size predictions
        if self._total_predictions % self.window_size == 0:
            return self._check_drift()
        
        return None
    
    def _check_drift(self) -> Optional[str]:
        """Check for model drift."""
        if not SKLEARN_AVAILABLE:
            return None
        
        if len(self._predictions) < self.window_size:
            return None
        
        preds = list(self._predictions)
        actuals = list(self._actuals)
        
        # Need both classes for AUC
        if len(set(actuals)) < 2:
            logger.debug("Cannot compute AUC: only one class in window")
            return None
        
        try:
            rolling_auc = float(roc_auc_score(actuals, preds))  # pyright: ignore[reportPossiblyUnboundVariable]
            self._rolling_auc_history.append(rolling_auc)
            
            # Check if below threshold
            threshold_auc = self.baseline_auc * self.alert_threshold
            
            if rolling_auc < threshold_auc:
                self._alerts_triggered += 1
                self._last_alert_time = datetime.now(UTC)
                
                alert_msg = (
                    f"🚨 ML DRIFT DETECTED: Rolling AUC {rolling_auc:.3f} "
                    f"< threshold {threshold_auc:.3f} "
                    f"(baseline: {self.baseline_auc:.3f})"
                )
                logger.warning(alert_msg)
                return alert_msg
            
            logger.debug(f"ML health OK: Rolling AUC {rolling_auc:.3f}")
            
        except Exception as e:
            logger.warning(f"Error computing AUC: {e}")
        
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get drift detection statistics."""
        rolling_auc = self._rolling_auc_history[-1] if self._rolling_auc_history else 0.0
        threshold = self.baseline_auc * self.alert_threshold
        drift_detected = rolling_auc < threshold if self._rolling_auc_history else False
        
        return {
            'baseline_auc': self.baseline_auc,
            'window_size': self.window_size,
            'total_predictions': self._total_predictions,
            'alerts_triggered': self._alerts_triggered,
            'last_alert': self._last_alert_time.isoformat() if self._last_alert_time else None,
            'recent_aucs': self._rolling_auc_history[-10:] if self._rolling_auc_history else [],
            'current_status': self._get_status(),
            # 🔥 NEW: Explicit drift detection fields for inference engine
            'drift_detected': drift_detected,
            'rolling_auc': rolling_auc,
            'needs_retraining': self.needs_retraining(),
        }
    
    def _get_status(self) -> str:
        """Get current model health status."""
        if not self._rolling_auc_history:
            return "UNKNOWN"
        
        recent_auc = self._rolling_auc_history[-1]
        threshold = self.baseline_auc * self.alert_threshold
        
        if recent_auc >= self.baseline_auc:
            return "EXCELLENT"
        elif recent_auc >= threshold:
            return "GOOD"
        else:
            return "DEGRADED"
    
    def needs_retraining(self) -> bool:
        """Check if model needs retraining based on drift."""
        if not self._rolling_auc_history:
            return False
        
        # Retrain if last 3 checks were below threshold
        if len(self._rolling_auc_history) >= 3:
            threshold = self.baseline_auc * self.alert_threshold
            recent = self._rolling_auc_history[-3:]
            if all(auc < threshold for auc in recent):
                return True
        
        return False
    
    def reset(self, new_baseline_auc: Optional[float] = None):
        """Reset detector, optionally with new baseline."""
        self._predictions.clear()
        self._actuals.clear()
        self._total_predictions = 0
        self._alerts_triggered = 0
        self._last_alert_time = None
        self._rolling_auc_history.clear()
        
        if new_baseline_auc is not None:
            self.baseline_auc = new_baseline_auc
        
        logger.info(f"🔄 Drift detector reset (baseline AUC: {self.baseline_auc:.3f})")
