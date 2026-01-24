"""
Training Pipeline for Entry Score ML Model

Usage:
    from src.ml.training import train_entry_model
    
    result = train_entry_model(
        trade_logs_dir='trade_logs',
        deploy=False  # Set True to save model
    )
"""

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from .data_loader import TradeDataLoader
from ..models.entry_scorer_ml import EntryScoreML
from ..config import EntryMLConfig, ML_CONFIG

logger = logging.getLogger(__name__)


def train_entry_model(
    trade_logs_dir: str = "trade_logs",
    config: Optional[EntryMLConfig] = None,
    deploy: bool = False,
    min_trades: int = 100,
) -> Dict[str, Any]:
    """
    Train entry score ML model.
    
    Args:
        trade_logs_dir: Path to trade logs directory
        config: ML configuration (uses global if not provided)
        deploy: If True, save model to serialized/
        min_trades: Minimum trades required to train
        
    Returns:
        Dict with training results and metrics
    """
    config = config or ML_CONFIG
    
    result = {
        'success': False,
        'message': '',
        'n_trades': 0,
        'train_auc': 0.0,
        'val_auc': 0.0,
        'deployed': False,
    }
    
    # 1. Load data
    logger.info("📂 Loading trade data...")
    loader = TradeDataLoader(trade_logs_dir)
    n_loaded = loader.load()
    result['n_trades'] = n_loaded
    
    if n_loaded < min_trades:
        result['message'] = f"Insufficient data: {n_loaded} trades (need {min_trades})"
        logger.warning(f"❌ {result['message']}")
        return result
    
    # Log summary
    summary = loader.get_summary()
    logger.info(f"📊 Data summary: {summary}")
    
    # 2. Split data
    X_train, y_train, X_val, y_val = loader.get_train_val_split(val_ratio=0.2)
    
    if len(X_train) == 0:
        result['message'] = "Failed to extract features from trades"
        logger.error(f"❌ {result['message']}")
        return result
    
    # 3. Train model
    logger.info("🏋️ Training model...")
    model = EntryScoreML(params=config.lgb_params)
    feature_names = loader.get_feature_names()
    
    metrics = model.train(X_train, y_train, X_val, y_val, feature_names)
    
    result['train_auc'] = metrics['train_auc']
    result['val_auc'] = metrics['val_auc']
    
    # 4. Validate model
    if metrics['val_auc'] < config.min_oos_auc:
        result['message'] = (
            f"Model failed validation: AUC {metrics['val_auc']:.3f} < {config.min_oos_auc}"
        )
        logger.warning(f"⚠️ {result['message']}")
        # Still mark as success for logging, just don't deploy
        result['success'] = True
        return result
    
    # 5. Log feature importance
    if config.log_feature_importance:
        importance = model.get_feature_importance()
        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)
        logger.info("📊 Feature Importance (top 10):")
        for feat, imp in sorted_imp[:10]:
            logger.info(f"   {feat}: {imp:.2f}")
    
    # 6. Deploy if requested
    if deploy:
        model_dir = Path(__file__).parent.parent / 'serialized'
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / 'entry_v1.pkl'
        
        model.save(str(model_path))
        result['deployed'] = True
        logger.info(f"✅ Model deployed to {model_path}")
    
    result['success'] = True
    result['message'] = f"Training complete: AUC={metrics['val_auc']:.3f}"
    
    return result


def check_training_readiness(trade_logs_dir: str = "trade_logs") -> Dict[str, Any]:
    """
    Check if we're ready to train a model.
    
    Returns status and recommendations.
    """
    loader = TradeDataLoader(trade_logs_dir)
    n_loaded = loader.load()
    summary = loader.get_summary()
    
    status = {
        'ready': n_loaded >= 100,
        'n_trades': n_loaded,
        'summary': summary,
        'recommendations': [],
    }
    
    if n_loaded < 100:
        status['recommendations'].append(
            f"Need {100 - n_loaded} more trades before training"
        )
    elif n_loaded < 300:
        status['recommendations'].append(
            f"Have {n_loaded} trades. 300+ recommended for reliable model."
        )
    
    if summary.get('win_rate') is not None:
        win_rate = summary['win_rate']
        if win_rate < 0.3 or win_rate > 0.7:
            status['recommendations'].append(
                f"Imbalanced win rate ({win_rate:.1%}). Model may be biased."
            )
    
    return status
