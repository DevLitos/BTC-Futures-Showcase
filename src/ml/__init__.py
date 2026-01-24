"""
Machine Learning Module for DayTrader 2.1

This module provides ML-based enhancements to the trading system:
- Entry Score Refinement (Priority 2)
- Exit Timing Optimization (Priority 3 - future)

Architecture:
- SHADOW MODE: ML predicts but doesn't affect decisions (data collection)
- HYBRID MODE: ML adjusts rule-based scores ±1 point
- PRIMARY MODE: ML provides main scoring (requires 1000+ trades)

Usage:
    from src.ml import MLInferenceEngine, EntryMLConfig
    
    engine = MLInferenceEngine(EntryMLConfig())
    adjusted_score, ml_prob, reason = engine.get_entry_adjustment(
        rule_score=6,
        feature_snapshot=features
    )
"""

from .config import MLPhase, EntryMLConfig
from .inference.inference_engine import MLInferenceEngine

__all__ = [
    'MLPhase',
    'EntryMLConfig', 
    'MLInferenceEngine',
]
