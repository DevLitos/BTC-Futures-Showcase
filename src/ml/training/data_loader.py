"""
Trade Data Loader for ML Training

Handles loading, preprocessing, and splitting trade data for ML.
"""

import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from datetime import datetime, timedelta

from ..feature_extractor import (
    load_trades_from_jsonl,
    extract_features_for_training,
    ENTRY_FEATURES,
)

logger = logging.getLogger(__name__)


class TradeDataLoader:
    """
    Load and preprocess trade data for ML training.
    
    Features:
    - Load from trade_logs/ directory
    - Filter by date range
    - Train/validation split (temporal)
    - Data quality checks
    """
    
    def __init__(self, trade_logs_dir: str = "trade_logs"):
        self.trade_logs_dir = trade_logs_dir
        self._df: Optional[pd.DataFrame] = None
    
    def load(self, min_date: Optional[str] = None, max_date: Optional[str] = None) -> int:
        """
        Load trades, optionally filtering by date.
        
        Args:
            min_date: Start date (YYYY-MM-DD), inclusive
            max_date: End date (YYYY-MM-DD), inclusive
            
        Returns:
            Number of trades loaded
        """
        df = load_trades_from_jsonl(self.trade_logs_dir)
        
        if df.empty:
            self._df = df
            return 0
        
        # Filter by date if specified
        if min_date and 'entry_time' in df.columns:
            df = df[df['entry_time'] >= min_date]  # pyright: ignore[reportAttributeAccessIssue]
        
        if max_date and 'entry_time' in df.columns:
            df = df[df['entry_time'] <= max_date]  # pyright: ignore[reportAttributeAccessIssue]
        
        self._df = df  # pyright: ignore[reportAttributeAccessIssue]
        logger.info(f"Loaded {len(df)} trades")
        return len(df)
    
    def get_train_val_split(
        self,
        val_ratio: float = 0.2,
        temporal: bool = True
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Split data into train and validation sets.
        
        Args:
            val_ratio: Fraction for validation (default 20%)
            temporal: If True, use temporal split (later trades for val)
                      If False, random split (NOT recommended for trading)
        
        Returns:
            (X_train, y_train, X_val, y_val)
        """
        if self._df is None or self._df.empty:
            return np.array([]), np.array([]), np.array([]), np.array([])
        
        X, y, features = extract_features_for_training(self._df)
        
        if len(X) == 0:
            return np.array([]), np.array([]), np.array([]), np.array([])
        
        n_val = int(len(X) * val_ratio)
        n_train = len(X) - n_val
        
        if temporal:
            # Temporal split: first N for train, last M for val
            X_train, X_val = X[:n_train], X[n_train:]
            y_train, y_val = y[:n_train], y[n_train:]
        else:
            # Random split (not recommended)
            indices = np.random.permutation(len(X))
            train_idx, val_idx = indices[:n_train], indices[n_train:]
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
        
        logger.info(
            f"Split: {len(X_train)} train ({np.mean(y_train):.1%} win), "
            f"{len(X_val)} val ({np.mean(y_val):.1%} win)"
        )
        
        return X_train, y_train, X_val, y_val
    
    def get_feature_names(self) -> list:
        """Get feature names in order."""
        return ENTRY_FEATURES.copy()
    
    def get_summary(self) -> Dict[str, Any]:
        """Get data summary statistics."""
        if self._df is None or self._df.empty:
            return {'n_trades': 0}
        
        summary = {
            'n_trades': len(self._df),
            'date_range': None,
            'win_rate': None,
            'avg_pnl_r': None,
            'directions': None,
            'regimes': None,
        }
        
        if 'entry_time' in self._df.columns:
            summary['date_range'] = (
                str(self._df['entry_time'].min().date()),
                str(self._df['entry_time'].max().date())
            )
        
        if 'pnl_r' in self._df.columns:
            summary['win_rate'] = float((self._df['pnl_r'] > 0).mean())
            summary['avg_pnl_r'] = float(self._df['pnl_r'].mean())
        
        if 'direction' in self._df.columns:
            summary['directions'] = self._df['direction'].value_counts().to_dict()
        
        if 'regime' in self._df.columns:
            summary['regimes'] = self._df['regime'].value_counts().to_dict()
        
        return summary
    
    def has_enough_data(self, min_trades: int = 100) -> bool:
        """Check if we have enough data for training."""
        return self._df is not None and len(self._df) >= min_trades
