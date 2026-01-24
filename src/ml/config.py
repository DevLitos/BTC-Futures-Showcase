"""
ML Configuration - Entry Score Refinement

Centralized configuration for all ML components.
Phase-based approach ensures safe deployment.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Any


class MLPhase(Enum):
    """ML deployment phases with increasing autonomy."""
    DISABLED = auto()    # ML completely off
    SHADOW = auto()      # ML predicts and logs, but doesn't affect decisions
    HYBRID = auto()      # ML adjusts rule-based score ±1 point
    PRIMARY = auto()     # ML is the primary scoring mechanism


@dataclass
class EntryMLConfig:
    """Configuration for Entry Score ML Model."""
    
    # =========================================================================
    # PHASE CONTROL
    # =========================================================================
    phase: MLPhase = MLPhase.SHADOW  # Start in shadow mode
    
    # Thresholds to advance phases (based on accumulated trades with outcomes)
    min_trades_for_shadow: int = 0      # Can start immediately
    min_trades_for_hybrid: int = 300    # Need statistical significance
    min_trades_for_primary: int = 1000  # Need robust validation
    
    # =========================================================================
    # MODEL CONFIDENCE THRESHOLDS
    # =========================================================================
    # In HYBRID mode, ML adjusts score only when confident
    high_confidence_threshold: float = 0.65  # Boost score if P(win) > this
    low_confidence_threshold: float = 0.35   # Reduce score if P(win) < this
    
    # =========================================================================
    # SCORE ADJUSTMENT LIMITS (HYBRID MODE)
    # =========================================================================
    max_ml_boost: int = 1      # Maximum score increase from ML
    max_ml_penalty: int = 1    # Maximum score decrease from ML
    
    # Only adjust if rule-based score is in these ranges
    boost_min_rule_score: int = 4   # Don't boost scores below 4
    penalty_max_rule_score: int = 6  # Don't penalize scores above 6
    
    # =========================================================================
    # VALIDATION REQUIREMENTS
    # =========================================================================
    min_walk_forward_folds: int = 5
    min_oos_auc: float = 0.55           # Minimum out-of-sample AUC
    max_acceptable_degradation: float = 0.25  # Max sharpe degradation train→test
    min_consistency_ratio: float = 0.60  # Min ratio of positive folds
    
    # =========================================================================
    # RETRAINING
    # =========================================================================
    retrain_every_n_trades: int = 100   # Retrain after N new trades
    model_staleness_hours: int = 168    # Force retrain if model older than this (7 days)
    
    # =========================================================================
    # FEATURE CONFIGURATION
    # =========================================================================
    top_k_features: int = 15  # Limit features for small datasets (avoid overfitting)
    
    # =========================================================================
    # LIGHTGBM HYPERPARAMETERS (Conservative for small datasets)
    # =========================================================================
    lgb_params: Dict[str, Any] = field(default_factory=lambda: {
        'objective': 'binary',
        'metric': 'auc',
        'boosting_type': 'gbdt',
        'num_leaves': 8,           # Very shallow to prevent overfitting
        'max_depth': 3,            # Limit depth
        'learning_rate': 0.01,     # Slow learning
        'n_estimators': 100,       # Few trees
        'min_child_samples': 20,   # Require 20 samples per leaf
        'subsample': 0.8,
        'colsample_bytree': 0.7,
        'reg_alpha': 0.1,          # L1 regularization
        'reg_lambda': 0.1,         # L2 regularization
        'random_state': 42,
        'verbose': -1,
    })
    
    # =========================================================================
    # LOGGING
    # =========================================================================
    log_all_predictions: bool = True    # Log every prediction for analysis
    log_feature_importance: bool = True  # Log feature importances periodically
    
    def can_advance_to_hybrid(self, n_trades: int, validation_auc: float) -> bool:
        """Check if we can advance from SHADOW to HYBRID."""
        return (
            n_trades >= self.min_trades_for_hybrid and
            validation_auc >= self.min_oos_auc
        )
    
    def can_advance_to_primary(self, n_trades: int, validation_auc: float) -> bool:
        """Check if we can advance from HYBRID to PRIMARY."""
        return (
            n_trades >= self.min_trades_for_primary and
            validation_auc >= self.min_oos_auc + 0.05  # Higher bar for primary
        )


# Global config instance (can be overridden)
ML_CONFIG = EntryMLConfig()
