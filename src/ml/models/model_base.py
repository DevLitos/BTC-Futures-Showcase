"""
Base class for ML models in DayTrader.

Provides common interface for training, prediction, serialization.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from pathlib import Path
import numpy as np
import logging

logger = logging.getLogger(__name__)


class MLModelBase(ABC):
    """
    Abstract base class for all ML models.
    
    Ensures consistent interface across different model types.
    """
    
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.is_trained = False
        self.training_samples = 0
        self.training_auc = 0.0
        self.validation_auc = 0.0
        self.feature_names: List[str] = []
        self.created_at: Optional[str] = None
        self.last_trained_at: Optional[str] = None
    
    @abstractmethod
    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        feature_names: List[str]
    ) -> Dict[str, float]:
        """
        Train the model.
        
        Returns:
            Dict with training metrics (train_auc, val_auc, etc.)
        """
        pass
    
    @abstractmethod
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Predict probabilities.
        
        Args:
            X: Feature array of shape (n_samples, n_features)
            
        Returns:
            Array of probabilities of shape (n_samples,)
        """
        pass
    
    @abstractmethod
    def get_feature_importance(self) -> Dict[str, float]:
        """Return feature importances for interpretability."""
        pass
    
    @abstractmethod
    def save(self, path: str) -> None:
        """Save model and metadata to disk."""
        pass
    
    @abstractmethod
    def load(self, path: str) -> None:
        """Load model and metadata from disk."""
        pass
    
    def predict_single(self, features: np.ndarray) -> float:
        """
        Predict probability for a single sample.
        
        Args:
            features: 1D array of shape (n_features,)
            
        Returns:
            Probability of positive class (win)
        """
        if features.ndim == 1:
            features = features.reshape(1, -1)
        
        probs = self.predict_proba(features)
        return float(probs[0])
    
    def get_metadata(self) -> Dict[str, Any]:
        """Get model metadata for logging/storage."""
        return {
            'model_name': self.model_name,
            'is_trained': self.is_trained,
            'training_samples': self.training_samples,
            'training_auc': self.training_auc,
            'validation_auc': self.validation_auc,
            'n_features': len(self.feature_names),
            'feature_names': self.feature_names,
            'created_at': self.created_at,
            'last_trained_at': self.last_trained_at,
        }
