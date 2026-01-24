"""
Feature Extraction for ML Models

Transforms trade logs and live data into feature vectors for ML training/inference.
Handles missing values gracefully with sensible defaults.
"""

import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================================================================
# FEATURE DEFINITIONS
# =============================================================================

# Features ordered by expected importance (based on trading literature)
ENTRY_FEATURES = [
    # Score components (already calculated by rule-based system)
    'score_structure',
    'score_timing', 
    'score_momentum',
    'score_context',
    'score_synergy',
    
    # Trend indicators
    'adx_1h',
    'adx_5m',
    'effective_adx',
    
    # Volatility
    'atr_ratio',
    'bb_width',
    'bb_squeeze',
    
    # Location/Context
    'percentile_60d',
    'location_headroom_atr',
    'location_in_extreme',
    
    # Momentum
    'rsi_14',
    'cvd_bullish',
    'volume_ratio',
    
    # Session (one-hot encoded)
    'session_good',
    'session_poor',
    
    # Regime (one-hot encoded)
    'trading_mode_trend',
    'trading_mode_range',
    'trading_mode_transition',
    
    # External data
    'funding_rate',
    
    # Entry quality
    'chase_atr',  # Distance from ideal entry (ATR units)
    
    # Time features (BTC has strong intraday patterns)
    'day_of_week',   # 0=Monday, 6=Sunday
    'hour_utc',      # 0-23 UTC hour
    
    # Trade history
    'consecutive_losses',
]

# Default values for missing features (neutral/safe assumptions)
FEATURE_DEFAULTS = {
    'score_structure': 1.0,
    'score_timing': 1.0,
    'score_momentum': 1.0,
    'score_context': 1.0,
    'score_synergy': 0.0,
    'adx_1h': 20.0,
    'adx_5m': 20.0,
    'effective_adx': 20.0,
    'atr_ratio': 1.0,
    'bb_width': 300.0,
    'bb_squeeze': 0.0,
    'percentile_60d': 50.0,
    'location_headroom_atr': 5.0,
    'location_in_extreme': 0.0,
    'rsi_14': 50.0,
    'cvd_bullish': 0.0,
    'volume_ratio': 1.0,
    'session_good': 0.0,
    'session_poor': 0.0,
    'trading_mode_trend': 0.0,
    'trading_mode_range': 1.0,
    'trading_mode_transition': 0.0,
    'funding_rate': 0.0,
    'chase_atr': 0.0,
    'day_of_week': 3.0,   # Wednesday (mid-week default)
    'hour_utc': 12.0,     # Noon UTC (neutral default)
    'consecutive_losses': 0.0,
}


# =============================================================================
# DATA LOADING
# =============================================================================

def load_trades_from_jsonl(trade_logs_dir: str) -> pd.DataFrame:
    """
    Load all trade logs from JSONL files into a DataFrame.
    
    Args:
        trade_logs_dir: Path to trade_logs directory
        
    Returns:
        DataFrame with all trades, sorted by entry_time
    """
    trades = []
    logs_path = Path(trade_logs_dir)
    
    for jsonl_file in sorted(logs_path.glob("trades_*.jsonl")):
        try:
            with open(jsonl_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trade = json.loads(line)
                        trades.append(trade)
        except Exception as e:
            logger.warning(f"Error reading {jsonl_file}: {e}")
    
    if not trades:
        logger.warning(f"No trades found in {trade_logs_dir}")
        return pd.DataFrame()
    
    df = pd.DataFrame(trades)
    
    # Parse timestamps
    if 'entry_time' in df.columns:
        df['entry_time'] = pd.to_datetime(df['entry_time'])
        df = df.sort_values('entry_time').reset_index(drop=True)
    
    logger.info(f"Loaded {len(df)} trades from {trade_logs_dir}")
    return df


def extract_features_from_trade(trade: Dict[str, Any]) -> Dict[str, float]:
    """
    Extract ML features from a single trade record.
    
    Combines data from feature_snapshot, score_breakdown, and metadata.
    """
    features = {}
    
    # 1. Feature snapshot (if available - new trades)
    snapshot = trade.get('feature_snapshot', {})
    for feat in ENTRY_FEATURES:
        if feat in snapshot:
            features[feat] = float(snapshot[feat])
    
    # 2. Score breakdown
    breakdown = trade.get('score_breakdown', {})
    if breakdown:
        features['score_structure'] = float(breakdown.get('structure', FEATURE_DEFAULTS['score_structure']))
        features['score_timing'] = float(breakdown.get('timing', FEATURE_DEFAULTS['score_timing']))
        features['score_momentum'] = float(breakdown.get('momentum', FEATURE_DEFAULTS['score_momentum']))
        features['score_context'] = float(breakdown.get('context', FEATURE_DEFAULTS['score_context']))
        features['score_synergy'] = float(breakdown.get('synergy', FEATURE_DEFAULTS['score_synergy']))
    
    # 3. Metadata (legacy trades might have data here)
    metadata = trade.get('metadata', {})
    for feat in ENTRY_FEATURES:
        if feat not in features and feat in metadata:
            features[feat] = float(metadata[feat])
    
    # 4. Fill missing with defaults
    for feat in ENTRY_FEATURES:
        if feat not in features:
            features[feat] = FEATURE_DEFAULTS[feat]
    
    return features


def extract_features_for_training(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """
    Extract X (features) and y (target: win/loss) from trades DataFrame.
    
    Target: pnl_r > 0 (binary classification)
    
    Returns:
        X: (n_samples, n_features) array
        y: (n_samples,) array with 1=win, 0=loss
        feature_names: list of feature names
    """
    if df.empty:
        return np.array([]), np.array([]), []
    
    # Extract features for each trade
    feature_rows = []
    valid_indices = []
    
    for idx, trade in df.iterrows():
        try:
            features = extract_features_from_trade(trade.to_dict())
            feature_row = [features.get(f, FEATURE_DEFAULTS[f]) for f in ENTRY_FEATURES]
            feature_rows.append(feature_row)
            valid_indices.append(idx)
        except Exception as e:
            logger.warning(f"Error extracting features for trade {idx}: {e}")
    
    if not feature_rows:
        return np.array([]), np.array([]), []
    
    X = np.array(feature_rows, dtype=np.float32)
    
    # Target: win (pnl_r > 0) or loss
    y = (df.loc[valid_indices, 'pnl_r'] > 0).astype(int).values
    
    return X, y, ENTRY_FEATURES


def extract_features_live(feature_snapshot: Dict[str, Any]) -> np.ndarray:
    """
    Convert live feature_snapshot dict to model input array.
    
    Handles missing values with sensible defaults.
    Used during real-time inference.
    
    Args:
        feature_snapshot: Dict from _build_ml_feature_snapshot()
        
    Returns:
        1D numpy array of shape (n_features,)
    """
    features = []
    
    for feat in ENTRY_FEATURES:
        value = feature_snapshot.get(feat, FEATURE_DEFAULTS.get(feat, 0.0))
        
        # Handle numpy types
        if hasattr(value, 'item'):
            value = value.item()
        
        # Handle non-numeric
        if not isinstance(value, (int, float)):
            value = FEATURE_DEFAULTS.get(feat, 0.0)
        
        # Handle NaN/inf
        if not np.isfinite(value):
            value = FEATURE_DEFAULTS.get(feat, 0.0)
        
        features.append(float(value))
    
    return np.array(features, dtype=np.float32)


# =============================================================================
# FEATURE STATISTICS
# =============================================================================

def compute_feature_stats(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """
    Compute statistics for each feature (for normalization/analysis).
    """
    X, y, feature_names = extract_features_for_training(df)
    
    if len(X) == 0:
        return {}
    
    stats = {}
    for i, feat in enumerate(feature_names):
        values = X[:, i]
        stats[feat] = {
            'mean': float(np.mean(values)),
            'std': float(np.std(values)),
            'min': float(np.min(values)),
            'max': float(np.max(values)),
            'median': float(np.median(values)),
        }
    
    return stats
