#!/usr/bin/env python3
"""
ML Status Check Tool

Shows current state of ML system:
- Phase (SHADOW/HYBRID/PRIMARY)
- Training data available
- Model status
- Recent predictions

Usage:
    python tools/ml_status.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ml import MLPhase
from src.ml.inference import get_ml_inference_engine
from src.ml.training import check_training_readiness


def main():
    print("=" * 60)
    print("🤖 DayTrader 2.1 - ML Status Report")
    print("=" * 60)
    
    # 1. Inference Engine Status
    print("\n📊 INFERENCE ENGINE")
    print("-" * 40)
    
    engine = get_ml_inference_engine()
    stats = engine.get_stats()
    
    print(f"   Phase: {stats['phase']}")
    print(f"   Predictions: {stats['prediction_count']}")
    print(f"   Failures: {stats['model_failures']} ({stats['failure_rate']:.1%})")
    print(f"   Avg Latency: {stats['avg_latency_ms']:.2f}ms")
    print(f"   Model Loaded: {stats['model_loaded']}")
    if stats['model_loaded']:
        print(f"   Model AUC: {stats['model_auc']:.3f}")
    
    # 2. Training Data Status
    print("\n📈 TRAINING DATA")
    print("-" * 40)
    
    status = check_training_readiness('trade_logs')
    print(f"   Total Trades: {status['n_trades']}")
    print(f"   Ready to Train: {'✅ Yes' if status['ready'] else '❌ No'}")
    
    summary = status.get('summary', {})
    if summary.get('date_range'):
        print(f"   Date Range: {summary['date_range'][0]} to {summary['date_range'][1]}")
    if summary.get('win_rate') is not None:
        print(f"   Win Rate: {summary['win_rate']:.1%}")
    if summary.get('avg_pnl_r') is not None:
        print(f"   Avg PnL (R): {summary['avg_pnl_r']:.2f}R")
    if summary.get('directions'):
        print(f"   Directions: {summary['directions']}")
    if summary.get('regimes'):
        print(f"   Regimes: {summary['regimes']}")
    
    for rec in status.get('recommendations', []):
        print(f"   ⚠️ {rec}")
    
    # 3. Phase Transitions
    print("\n🔄 PHASE TRANSITIONS")
    print("-" * 40)
    
    n_trades = status['n_trades']
    
    # Current → Next requirements
    if engine.phase == MLPhase.SHADOW:
        trades_needed = 300 - n_trades
        print(f"   Current: SHADOW (logging only)")
        print(f"   Next: HYBRID (±1 score adjustment)")
        print(f"   Requirements:")
        print(f"      - {trades_needed} more trades (need 300)")
        print(f"      - Walk-forward AUC >= 0.55")
    elif engine.phase == MLPhase.HYBRID:
        trades_needed = 1000 - n_trades
        print(f"   Current: HYBRID (±1 adjustment)")
        print(f"   Next: PRIMARY (ML-driven scoring)")
        print(f"   Requirements:")
        print(f"      - {trades_needed} more trades (need 1000)")
        print(f"      - Walk-forward AUC >= 0.60")
    else:
        print(f"   Current: {engine.phase.name}")
    
    # 4. Recent Predictions
    print("\n📋 RECENT PREDICTIONS")
    print("-" * 40)
    
    recent = engine.get_recent_predictions(10)
    if recent:
        for pred in recent[-5:]:
            print(f"   [{pred['timestamp'][:19]}] "
                  f"rule={pred['rule_score']} → adj={pred['adjusted_score']} "
                  f"(P={pred['ml_prob']:.2f}) {pred['reason'][:30]}")
    else:
        print("   No predictions yet")
    
    print("\n" + "=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
