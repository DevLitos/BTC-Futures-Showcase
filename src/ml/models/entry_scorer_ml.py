"""
LightGBM-based Entry Score Model

Predicts probability of a winning trade based on entry features.
Designed to work alongside the rule-based scorer.
"""

import logging
import pickle
import numpy as np
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional, Any

from .model_base import MLModelBase

logger = logging.getLogger(__name__)

# Optional LightGBM import - graceful degradation if not installed
try:
    import lightgbm as lgb
    LIGHTGBM_AVAILABLE = True
except ImportError:
    LIGHTGBM_AVAILABLE = False
    logger.warning("LightGBM not installed. ML features will be limited.")

# Optional sklearn for metrics
try:
    from sklearn.metrics import roc_auc_score
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


class EntryScoreML(MLModelBase):
    """
    LightGBM classifier for entry quality prediction.
    
    Predicts P(win | features) where win = pnl_r > 0.
    
    Phases:
    - SHADOW: Predictions logged but not used
    - HYBRID: Adjusts rule-based score ±1
    - PRIMARY: Provides main entry score
    """
    
    def __init__(self, model_path: Optional[str] = None, params: Optional[Dict] = None):
        super().__init__("entry_score_v1")
        
        self.model: Optional[Any] = None  # lgb.Booster
        self.params = params or self._default_params()
        
        if model_path and Path(model_path).exists():
            self.load(model_path)
    
    def _default_params(self) -> Dict[str, Any]:
        """Default LightGBM parameters (conservative for small datasets)."""
        return {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 8,
            'max_depth': 3,
            'learning_rate': 0.01,
            'n_estimators': 100,
            'min_child_samples': 20,
            'subsample': 0.8,
            'colsample_bytree': 0.7,
            'reg_alpha': 0.1,
            'reg_lambda': 0.1,
            'random_state': 42,
            'verbose': -1,
        }
    
    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        feature_names: List[str]
    ) -> Dict[str, float]:
        """
        Train model with early stopping on validation set.
        
        Returns:
            Dict with 'train_auc', 'val_auc', 'n_iterations'
        """
        if not LIGHTGBM_AVAILABLE:
            logger.error("Cannot train: LightGBM not installed")
            return {'train_auc': 0.0, 'val_auc': 0.0, 'n_iterations': 0}
        
        if len(X_train) < 50:
            logger.warning(f"Training with only {len(X_train)} samples - high overfitting risk")
        
        self.feature_names = feature_names
        
        # Create datasets
        train_data = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)  # pyright: ignore[reportPossiblyUnboundVariable]
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)  # pyright: ignore[reportPossiblyUnboundVariable]
        
        # Train with early stopping
        callbacks = [
            lgb.early_stopping(stopping_rounds=20, verbose=False),  # pyright: ignore[reportPossiblyUnboundVariable]
            lgb.log_evaluation(period=0),  # pyright: ignore[reportPossiblyUnboundVariable]  # Suppress logging
        ]
        
        self.model = lgb.train(  # pyright: ignore[reportPossiblyUnboundVariable]
            self.params,
            train_data,
            num_boost_round=self.params.get('n_estimators', 100),
            valid_sets=[train_data, val_data],
            valid_names=['train', 'val'],
            callbacks=callbacks,
        )
        
        # Calculate metrics
        train_preds = self.model.predict(X_train)
        val_preds = self.model.predict(X_val)
        
        if SKLEARN_AVAILABLE and len(np.unique(y_train)) > 1:
            self.training_auc = roc_auc_score(y_train, train_preds)  # pyright: ignore[reportPossiblyUnboundVariable]
            self.validation_auc = roc_auc_score(y_val, val_preds)  # pyright: ignore[reportPossiblyUnboundVariable]
        else:
            self.training_auc = 0.0
            self.validation_auc = 0.0
        
        self.training_samples = len(X_train)
        self.is_trained = True
        self.last_trained_at = datetime.now(UTC).isoformat()
        
        if self.created_at is None:
            self.created_at = self.last_trained_at
        
        logger.info(
            f"✅ Model trained: {len(X_train)} samples | "
            f"Train AUC: {self.training_auc:.3f} | Val AUC: {self.validation_auc:.3f}"
        )
        
        return {
            'train_auc': float(self.training_auc),
            'val_auc': float(self.validation_auc),
            'n_iterations': int(self.model.best_iteration) if self.model else 0,
            'n_samples': len(X_train),
        }
    
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Predict win probability for each sample.
        
        Returns 0.5 (neutral) if model not trained.
        """
        if self.model is None:
            return np.full(len(X), 0.5)
        
        try:
            return self.model.predict(X)
        except Exception as e:
            logger.warning(f"Prediction error: {e}")
            return np.full(len(X), 0.5)
    
    def get_feature_importance(self) -> Dict[str, float]:
        """Return feature importances (gain-based)."""
        if self.model is None or not self.feature_names:
            return {}
        
        try:
            importance = self.model.feature_importance(importance_type='gain')
            return dict(zip(self.feature_names, importance))
        except Exception as e:
            logger.warning(f"Error getting feature importance: {e}")
            return {}
    
    def save(self, path: str) -> None:
        """Save model and metadata."""
        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            'metadata': self.get_metadata(),
            'params': self.params,
            'model_str': self.model.model_to_string() if self.model else None,
        }
        
        with open(save_path, 'wb') as f:
            pickle.dump(data, f)
        
        logger.info(f"💾 Model saved to {path}")
    
    def load(self, path: str) -> None:
        """Load model and metadata."""
        if not LIGHTGBM_AVAILABLE:
            logger.error("Cannot load: LightGBM not installed")
            return
        
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
            
            # Restore metadata
            metadata = data.get('metadata', {})
            self.model_name = metadata.get('model_name', self.model_name)
            self.is_trained = metadata.get('is_trained', False)
            self.training_samples = metadata.get('training_samples', 0)
            self.training_auc = metadata.get('training_auc', 0.0)
            self.validation_auc = metadata.get('validation_auc', 0.0)
            self.feature_names = metadata.get('feature_names', [])
            self.created_at = metadata.get('created_at')
            self.last_trained_at = metadata.get('last_trained_at')
            
            # Restore params
            self.params = data.get('params', self._default_params())
            
            # Restore model
            model_str = data.get('model_str')
            if model_str:
                self.model = lgb.Booster(model_str=model_str)  # pyright: ignore[reportPossiblyUnboundVariable]
            
            logger.info(f"📂 Model loaded from {path} (AUC: {self.validation_auc:.3f})")
            
        except Exception as e:
            logger.error(f"Error loading model from {path}: {e}")
            self.model = None
            self.is_trained = False
