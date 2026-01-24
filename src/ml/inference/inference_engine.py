"""
Real-time ML Inference Engine

Manages ML predictions in production with phase-aware behavior.
Provides safe fallbacks and comprehensive logging.
"""

import logging
import time
from datetime import datetime, UTC
from typing import Optional, Tuple, Dict, Any, List
from pathlib import Path

from ..config import MLPhase, EntryMLConfig, ML_CONFIG
from ..models.entry_scorer_ml import EntryScoreML
from ..feature_extractor import extract_features_live, ENTRY_FEATURES
from ..monitoring.drift_detector import MLDriftDetector

logger = logging.getLogger(__name__)


class MLInferenceEngine:
    """
    Manages ML inference in production.
    
    Integrates with existing rule-based system:
    - Phase-aware behavior (SHADOW/HYBRID/PRIMARY)
    - Fallback to rules if ML fails
    - Latency monitoring
    - Comprehensive logging for analysis
    """
    
    def __init__(self, config: Optional[EntryMLConfig] = None):
        self.config = config or ML_CONFIG
        
        # Model management
        self._entry_model: Optional[EntryScoreML] = None
        self._model_path: Optional[str] = None
        
        # Statistics
        self._prediction_count = 0
        self._model_failures = 0
        self._total_latency_ms = 0.0
        self._predictions_log: List[Dict[str, Any]] = []  # Keep last N for analysis
        self._max_log_size = 1000
        
        # 🔥 Drift Detection (Jan 24, 2026)
        # Monitor model performance degradation over time
        self._drift_detector = MLDriftDetector(
            window_size=100,  # Last 100 trades for drift calculation
            baseline_auc=0.55  # Expected minimum AUC
        )
        
        # Load model if exists
        self._try_load_model()
    
    def _try_load_model(self) -> bool:
        """Try to load saved model."""
        model_dir = Path(__file__).parent.parent / 'serialized'
        model_path = model_dir / 'entry_v1.pkl'
        
        if model_path.exists():
            try:
                self._entry_model = EntryScoreML(str(model_path))
                self._model_path = str(model_path)
                logger.info(f"🤖 ML model loaded: {model_path}")
                return True
            except Exception as e:
                logger.warning(f"Failed to load model: {e}")
        
        # No model found - that's OK for SHADOW mode
        self._entry_model = EntryScoreML()  # Empty model returns 0.5
        return False
    
    @property
    def phase(self) -> MLPhase:
        """Current ML phase."""
        return self.config.phase
    
    @property
    def is_active(self) -> bool:
        """True if ML is providing predictions (not DISABLED)."""
        return self.config.phase != MLPhase.DISABLED
    
    @property
    def affects_decisions(self) -> bool:
        """True if ML can modify trading decisions."""
        return self.config.phase in (MLPhase.HYBRID, MLPhase.PRIMARY)
    
    def get_entry_adjustment(
        self,
        rule_score: int,
        feature_snapshot: Dict[str, Any]
    ) -> Tuple[int, float, str]:
        """
        Get ML-based score adjustment.
        
        This is the main entry point called by signal_engine.
        
        Args:
            rule_score: Score from rule-based EntryScorer (0-9)
            feature_snapshot: Features from _build_ml_feature_snapshot()
            
        Returns:
            (adjusted_score, ml_probability, reason_string)
        """
        start_time = time.perf_counter()
        
        # DISABLED: No-op
        if self.config.phase == MLPhase.DISABLED:
            return rule_score, 0.5, "ML_DISABLED"
        
        # Get prediction
        ml_prob = self._predict_safe(feature_snapshot)
        
        # Track latency
        latency_ms = (time.perf_counter() - start_time) * 1000
        self._total_latency_ms += latency_ms
        self._prediction_count += 1
        
        # Determine adjustment based on phase
        adjusted_score = rule_score
        reason = ""
        
        if self.config.phase == MLPhase.SHADOW:
            # SHADOW: Log but don't adjust
            reason = f"SHADOW: P(win)={ml_prob:.2f} [logged only]"
            
        elif self.config.phase == MLPhase.HYBRID:
            # HYBRID: Adjust based on confidence
            adjusted_score, reason = self._apply_hybrid_adjustment(rule_score, ml_prob)
            
        elif self.config.phase == MLPhase.PRIMARY:
            # PRIMARY: ML drives score directly
            adjusted_score = int(round(9 * ml_prob))
            reason = f"ML_PRIMARY: P(win)={ml_prob:.2f}→score={adjusted_score}"
        
        # Log prediction for analysis
        self._log_prediction(rule_score, adjusted_score, ml_prob, reason, feature_snapshot)
        
        return adjusted_score, ml_prob, reason
    
    def _predict_safe(self, feature_snapshot: Dict[str, Any]) -> float:
        """
        Predict with comprehensive error handling.
        
        Returns 0.5 (neutral) on any error.
        """
        try:
            if self._entry_model is None:
                return 0.5
            
            features = extract_features_live(feature_snapshot)
            prob = self._entry_model.predict_single(features)
            
            # Sanity check
            if not (0.0 <= prob <= 1.0):
                logger.warning(f"Invalid probability {prob}, using 0.5")
                return 0.5
            
            return prob
            
        except Exception as e:
            self._model_failures += 1
            logger.warning(f"⚠️ ML prediction failed ({self._model_failures} total): {e}")
            return 0.5
    
    def _apply_hybrid_adjustment(self, rule_score: int, ml_prob: float) -> Tuple[int, str]:
        """
        Apply HYBRID mode adjustment.
        
        Only adjusts when:
        - ML is confident (above/below thresholds)
        - Rule score is in adjustable range
        """
        adjusted = rule_score
        reason_parts = [f"HYBRID: P(win)={ml_prob:.2f}"]
        
        # High confidence boost
        if ml_prob > self.config.high_confidence_threshold:
            if rule_score >= self.config.boost_min_rule_score:
                boost = min(self.config.max_ml_boost, 9 - rule_score)  # Cap at 9
                if boost > 0:
                    adjusted = rule_score + boost
                    reason_parts.append(f"+{boost} (high confidence)")
            else:
                reason_parts.append("no boost (score too low)")
        
        # Low confidence penalty
        elif ml_prob < self.config.low_confidence_threshold:
            if rule_score <= self.config.penalty_max_rule_score:
                penalty = min(self.config.max_ml_penalty, rule_score)  # Don't go below 0
                if penalty > 0:
                    adjusted = rule_score - penalty
                    reason_parts.append(f"-{penalty} (low confidence)")
            else:
                reason_parts.append("no penalty (score too high)")
        
        else:
            reason_parts.append("neutral zone")
        
        return adjusted, " | ".join(reason_parts)
    
    def _log_prediction(
        self,
        rule_score: int,
        adjusted_score: int,
        ml_prob: float,
        reason: str,
        feature_snapshot: Dict[str, Any]
    ) -> None:
        """Log prediction for later analysis."""
        if not self.config.log_all_predictions:
            return
        
        log_entry = {
            'timestamp': datetime.now(UTC).isoformat(),
            'phase': self.config.phase.name,
            'rule_score': rule_score,
            'adjusted_score': adjusted_score,
            'ml_prob': round(ml_prob, 4),
            'adjustment': adjusted_score - rule_score,
            'reason': reason,
            # Store key features for analysis
            'features': {
                'adx_1h': feature_snapshot.get('adx_1h', 0),
                'adx_5m': feature_snapshot.get('adx_5m', 0),
                'rsi_14': feature_snapshot.get('rsi_14', 50),
                'percentile_60d': feature_snapshot.get('percentile_60d', 50),
                'consecutive_losses': feature_snapshot.get('consecutive_losses', 0),
            }
        }
        
        self._predictions_log.append(log_entry)
        
        # Trim log to max size
        if len(self._predictions_log) > self._max_log_size:
            self._predictions_log = self._predictions_log[-self._max_log_size:]
        
        # Log to file
        logger.info(
            f"🤖 ML [{self.config.phase.name}]: "
            f"rule={rule_score}→adj={adjusted_score} | "
            f"P(win)={ml_prob:.2f} | {reason}"
        )
        
        # 🔥 NEW: Log ML-Rules correlation warnings
        self._check_ml_rules_correlation(rule_score, ml_prob)
    
    def _check_ml_rules_correlation(self, rule_score: int, ml_prob: float):
        """
        Check and log when ML and rules strongly disagree.
        
        This is valuable diagnostic info:
        - If rules say STRONG (7+) but ML says weak (<0.40), investigate why
        - If rules say WEAK (<4) but ML says strong (>0.65), investigate why
        
        Patterns of disagreement may reveal:
        1. Rules missing important features that ML captures
        2. ML overfitting to noise that rules correctly ignore
        3. Market regime changes where one system adapts faster
        """
        # Strong disagreement thresholds
        RULES_HIGH_SCORE = 6
        RULES_LOW_SCORE = 4
        ML_HIGH_PROB = 0.60
        ML_LOW_PROB = 0.42
        
        # Rules bullish, ML bearish
        if rule_score >= RULES_HIGH_SCORE and ml_prob < ML_LOW_PROB:
            logger.warning(
                f"⚠️ ML-RULES DIVERGENCE (RULES_BULLISH): "
                f"Rules={rule_score} says ENTER but ML P(win)={ml_prob:.2f} says SKIP. "
                f"ML may see risk rules miss."
            )
        
        # Rules bearish, ML bullish  
        elif rule_score <= RULES_LOW_SCORE and ml_prob > ML_HIGH_PROB:
            logger.warning(
                f"⚠️ ML-RULES DIVERGENCE (ML_BULLISH): "
                f"Rules={rule_score} says SKIP but ML P(win)={ml_prob:.2f} says ENTER. "
                f"Rules may be over-filtering."
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get inference statistics."""
        avg_latency = self._total_latency_ms / max(1, self._prediction_count)
        
        # Combine with drift stats
        drift_stats = self._drift_detector.get_stats()
        
        # 🔥 IMPROVED: More robust model status detection
        model_loaded = (
            self._entry_model is not None and 
            hasattr(self._entry_model, 'is_trained') and 
            self._entry_model.is_trained
        )
        model_auc = 0.0
        if model_loaded and self._entry_model is not None and hasattr(self._entry_model, 'validation_auc'):
            model_auc = self._entry_model.validation_auc or 0.0
        
        return {
            'phase': self.config.phase.name,
            'prediction_count': self._prediction_count,
            'model_failures': self._model_failures,
            'failure_rate': self._model_failures / max(1, self._prediction_count),
            'avg_latency_ms': round(avg_latency, 2),
            'model_loaded': model_loaded,
            'model_auc': model_auc,
            # Drift monitoring - use explicit fields from get_stats()
            'drift_detected': drift_stats.get('drift_detected', False),
            'rolling_auc': drift_stats.get('rolling_auc', 0.0),
            'drift_window_size': drift_stats.get('window_size', 50),
            'drift_status': drift_stats.get('current_status', 'UNKNOWN'),
            'needs_retraining': drift_stats.get('needs_retraining', False),
        }
    
    def get_recent_predictions(self, n: int = 50) -> List[Dict[str, Any]]:
        """Get last N predictions for analysis."""
        return self._predictions_log[-n:]
    
    def reload_model(self) -> bool:
        """Reload model from disk (e.g., after retraining)."""
        logger.info("🔄 Reloading ML model...")
        return self._try_load_model()
    
    def record_outcome(self, trade_id: str, ml_prob: float, actual_win: bool) -> Dict[str, Any]:
        """
        Record trade outcome for drift detection.
        
        🔥 Called by signal_engine.complete_trade() to track model accuracy.
        
        Args:
            trade_id: Trade ID for logging
            ml_prob: ML prediction probability at entry
            actual_win: True if trade was profitable (P&L > 0)
            
        Returns:
            Drift stats including alerts if drift detected
        """
        actual = 1.0 if actual_win else 0.0
        alert_msg = self._drift_detector.update(ml_prob, int(actual))
        drift_stats = self._drift_detector.get_stats()
        
        if drift_stats.get('drift_detected', False):
            logger.warning(
                f"⚠️ ML DRIFT DETECTED for trade {trade_id}: "
                f"rolling_auc={drift_stats.get('rolling_auc', 0):.3f} < "
                f"baseline={drift_stats.get('baseline_auc', 0.55):.3f}"
            )
        
        return drift_stats
    
    def get_drift_stats(self) -> Dict[str, Any]:
        """Get current drift detector statistics."""
        return self._drift_detector.get_stats()


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_ml_engine: Optional[MLInferenceEngine] = None


def get_ml_inference_engine() -> MLInferenceEngine:
    """Get global ML inference engine instance."""
    global _ml_engine
    if _ml_engine is None:
        _ml_engine = MLInferenceEngine()
    return _ml_engine
