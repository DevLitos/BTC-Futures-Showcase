"""
Integration Example - Advanced Instrumentation & Risk Middleware

Este archivo demuestra cómo integrar todas las nuevas features:
1. SL Bins Classification (Caja Negra)
2. Market Regime Engine
3. Risk Manager Middleware
4. Shadow Trading
5. Edge Health Tracking
6. Grafana Metrics

Patrón de uso recomendado:
1. Al inicio del bot: inicializar componentes
2. Antes de cada señal: classify regime y process_signal_through_middleware
3. Al entrar al trade: instrument_entry
4. Al salir del trade: instrument_exit + record_trade_result
5. Periódicamente: update Grafana metrics
"""

import logging
from datetime import datetime, UTC
from typing import Dict, Any, Optional
import numpy as np

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def demo_market_regime_classification():
    """Demonstrate Market Regime classification."""
    print("\n" + "="*70)
    print("DEMO: Market Regime Classification")
    print("="*70)
    
    from engine.market_regime import (
        classify_market_regime, 
        MarketRegime,
        get_regime_classifier,
        REGIME_GUIDANCE
    )
    
    # Simulate some market data
    np.random.seed(42)
    n = 50
    
    # Create OHLCV data - trending market
    opens = 100 + np.cumsum(np.random.randn(n) * 0.5)
    closes = opens + np.random.randn(n) * 0.3 + 0.2  # Slight upward bias
    highs = np.maximum(opens, closes) + np.abs(np.random.randn(n) * 0.1)
    lows = np.minimum(opens, closes) - np.abs(np.random.randn(n) * 0.1)
    volumes = np.random.exponential(1000, n)
    
    # High ADX = trending
    regime, details = classify_market_regime(
        adx=28.0,
        current_atr=1.5,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes
    )
    
    print(f"  Regime: {regime.value}")
    print(f"  ADX: {details['adx']}")
    print(f"  ATR Ratio: {details['atr_ratio']:.2f}")
    print(f"  Guidance: {details['guidance']}")
    print(f"  Risk Multiplier: {REGIME_GUIDANCE[regime].risk_multiplier}x")
    
    # Low ADX = mean reversion
    regime2, details2 = classify_market_regime(
        adx=15.0,
        current_atr=0.8,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes
    )
    
    print(f"\n  Regime 2: {regime2.value}")
    print(f"  ADX: {details2['adx']}")
    print(f"  Guidance: {details2['guidance']}")
    

def demo_risk_middleware():
    """Demonstrate Risk Manager Middleware."""
    print("\n" + "="*70)
    print("DEMO: Risk Manager Middleware")
    print("="*70)
    
    from engine.risk_middleware import (
        RiskManagerMiddleware,
        SetupGrade,
        MarketRegime
    )
    
    # Initialize middleware
    middleware = RiskManagerMiddleware(base_risk_pct=0.8)
    
    # Process a signal
    order = middleware.process_signal(
        direction="LONG",
        base_size=0.01,  # 0.01 BTC
        entry_price=100000.0,
        stop_loss=99000.0,  # 1% SL
        setup_name="BOS_MOMENTUM",
        regime=MarketRegime.TREND_EXPANSION,
        capital=10000.0
    )
    
    print(f"  Original Size: {order.original_size:.6f}")
    print(f"  Adjusted Size: {order.adjusted_size:.6f}")
    print(f"  Risk %: {order.adjusted_risk_pct:.2f}%")
    print(f"  Setup Grade: {order.setup_grade.value}")
    print(f"  Regime: {order.regime.value}")
    print(f"  TP1 Target: {order.tp1_r}R")
    print(f"  Should Trade: {order.should_trade}")
    
    # Simulate some trades to see streak escalation
    print("\n  Simulating winning streak...")
    for i in range(6):
        middleware.record_trade_result(
            setup_name="BOS_MOMENTUM",
            pnl=150.0,  # $150 win
            mfe_r=1.8,
            base_risk_pct=0.8,
            escalated_risk_pct=0.9,
            entry_price=100000.0,
            stop_loss=99000.0
        )
    
    summary = middleware.get_summary()
    print(f"\n  Streak: {summary['streak']['current']}")
    print(f"  Escalated: {summary['streak']['is_escalated']}")
    print(f"  Current Risk %: {summary['streak']['current_risk_pct']:.2f}%")
    print(f"  Shadow PnL Real: ${summary['shadow']['pnl_real']:.2f}")
    print(f"  Shadow PnL Escalated: ${summary['shadow']['pnl_escalated']:.2f}")


def demo_trade_instrumentation():
    """Demonstrate Trade Instrumentation (Caja Negra)."""
    print("\n" + "="*70)
    print("DEMO: Trade Instrumentation (Caja Negra)")
    print("="*70)
    
    from engine.advanced_analytics import (
        TradeInstrumenter,
        SLSizeBin,
        classify_sl_bin
    )
    
    # Initialize instrumenter
    instrumenter = TradeInstrumenter(output_dir="logs/demo_instrumentation")
    
    # Instrument trade entry
    entry_data = instrumenter.instrument_entry(
        trade_id="TRADE_001",
        symbol="BTC/USDT",
        entry_price=100000.0,
        direction="LONG",
        position_size=0.01,
        stop_loss=99400.0,  # 600 USD SL
        take_profit_1=101000.0,
        take_profit_2=102000.0,
        atr=800.0,  # ATR = 800 USD
        adx=25.0,
        setup_type="BOS_PULLBACK",
        avg_body_ratio=0.55,
        wick_dominance=0.3,
        volume_ratio=1.2
    )
    
    print(f"  Trade ID: {entry_data.trade_id}")
    print(f"  SL Distance: ${entry_data.sl_distance:.2f}")
    print(f"  SL/ATR Ratio: {entry_data.sl_atr_ratio:.2f}")
    print(f"  SL Bin: {entry_data.sl_bin}")
    print(f"  Session: {entry_data.session}")
    print(f"  Market Regime: {entry_data.market_regime or 'Not classified'}")
    
    # Instrument trade exit
    # PnL = (exit_price - entry_price) * position_size = (101200 - 100000) * 0.01 = $12
    # Total Risk = (entry - SL) * position = (100000 - 99400) * 0.01 = $6
    # R = 12 / 6 = 2.0R
    exit_record = instrumenter.instrument_exit(
        exit_price=101200.0,
        exit_time=datetime.now(UTC),
        exit_reason="TP1_PARTIAL",
        pnl=12.0,  # Corrected: $12 profit on 0.01 BTC move of $1200
        mfe_r=1.5,
        mae_r=0.3,
        duration_minutes=45.0
    )
    
    print(f"\n  Exit Price: ${exit_record['exit_price']:.2f}")
    print(f"  PnL: ${exit_record['pnl']:.2f}")
    print(f"  R Realized: {exit_record['r_realized']:.2f}R")
    print(f"  MFE: {exit_record['mfe_r']:.2f}R")
    
    # Check SL bin analysis
    sl_analysis = instrumenter.get_sl_bin_analysis()
    print(f"\n  SL Bin Analysis:")
    for bin_name, stats in sl_analysis.items():
        print(f"    {bin_name}: {stats['count']} trades, "
              f"expectancy {stats['expectancy']:.2f}R, "
              f"should_trade: {stats['should_trade']}")


def demo_edge_health_tracking():
    """Demonstrate Edge Health Tracking."""
    print("\n" + "="*70)
    print("DEMO: Edge Health Tracking")
    print("="*70)
    
    from engine.advanced_analytics import (
        EdgeHealthTracker,
        record_trade_for_edge_health
    )
    
    # Create tracker with lower thresholds for demo
    tracker = EdgeHealthTracker(
        rolling_window=10,
        min_trades_for_historical=20,
        alert_threshold=0.5
    )
    
    # Simulate good period
    print("  Simulating good trading period...")
    for i in range(25):
        r_value = np.random.uniform(0.5, 2.0)  # Positive R values
        tracker.record_trade_r(r_value)
    
    metrics = tracker.get_health_metrics()
    print(f"  Historical Expectancy: {metrics['historical_expectancy']:.2f}R")
    print(f"  Rolling Expectancy: {metrics['rolling_expectancy']:.2f}R")
    print(f"  Health Ratio: {metrics['health_ratio']:.2f}")
    print(f"  Observation Mode: {metrics['in_observation_mode']}")
    
    # Simulate bad period (edge degradation)
    print("\n  Simulating edge degradation...")
    for i in range(12):
        r_value = np.random.uniform(-1.0, 0.2)  # Negative R values
        tracker.record_trade_r(r_value)
    
    metrics = tracker.get_health_metrics()
    print(f"  Historical Expectancy: {metrics['historical_expectancy']:.2f}R")
    print(f"  Rolling Expectancy: {metrics['rolling_expectancy']:.2f}R")
    print(f"  Health Ratio: {metrics['health_ratio']:.2f}")
    print(f"  Observation Mode: {metrics['in_observation_mode']}")
    print(f"  Risk Multiplier: {metrics['risk_multiplier']:.2f}x")


def demo_sl_bin_classification():
    """Demonstrate SL Bin Classification."""
    print("\n" + "="*70)
    print("DEMO: SL Bin Classification")
    print("="*70)
    
    from engine.advanced_analytics import classify_sl_bin, SLSizeBin
    
    # ATR = $500
    atr = 500.0
    
    test_cases = [
        (200.0, "Very tight SL - 0.4 ATR"),  # MICRO
        (400.0, "Moderate SL - 0.8 ATR"),    # NORMAL
        (800.0, "Wide SL - 1.6 ATR"),        # WIDE
    ]
    
    for sl_distance, description in test_cases:
        sl_bin = classify_sl_bin(sl_distance, atr)
        ratio = sl_distance / atr
        print(f"  {description}: SL=${sl_distance:.0f}, Ratio={ratio:.2f} → {sl_bin}")
    
    print("\n  Interpretation:")
    print(f"    MICRO (<0.6 ATR): High precision but high chance of wick sweep")
    print(f"    NORMAL (0.6-1.2 ATR): Sweet spot - structure usually holds")
    print(f"    WIDE (>1.2 ATR): Conservative - for trend changes")
    print(f"\n  Rule: If MICRO has negative expectancy after 100 trades, stop trading it!")


def demo_3_phase_trailing():
    """Demonstrate 3-Phase Trailing Stop."""
    print("\n" + "="*70)
    print("DEMO: 3-Phase Trailing Stop")
    print("="*70)
    
    from engine.risk_middleware import ThreePhaseTrailing, MarketRegime
    
    trailing = ThreePhaseTrailing()
    
    # Trade parameters
    entry_price = 100000.0
    stop_loss = 99000.0  # 1000 USD risk = 1R
    position_size = 0.01
    
    # Simulate price progression
    price_levels = [
        (100500.0, "0.5R"),
        (101100.0, "1.1R - Phase 1 trigger"),
        (101500.0, "1.5R"),
        (102000.0, "2.0R - Phase 2 trigger"),
        (102700.0, "2.7R - Phase 3 (trend)"),
    ]
    
    print(f"  Entry: ${entry_price:.2f}, SL: ${stop_loss:.2f}, Risk: $1000")
    print()
    
    for price, description in price_levels:
        new_stop, reason, phase = trailing.calculate_trailing_stop(
            entry_price=entry_price,
            current_price=price,
            stop_loss=stop_loss,
            position_size=position_size,
            direction="LONG",
            regime=MarketRegime.TREND_EXPANSION,
            recent_lows=[price - 200, price - 150, price - 100],
            atr=500.0
        )
        
        locked_profit = new_stop - entry_price if new_stop > entry_price else 0
        print(f"  {description}")
        print(f"    Price: ${price:.2f} | New SL: ${new_stop:.2f} | Phase: {phase}")
        print(f"    Locked Profit: ${locked_profit:.2f} | Reason: {reason}")
        print()


def demo_ppo_metric():
    """Demonstrate PPO (Profit Per Opportunity) Metric."""
    print("\n" + "="*70)
    print("DEMO: PPO (Profit Per Opportunity)")
    print("="*70)
    
    from engine.risk_middleware import PPOTracker
    
    ppo = PPOTracker()
    
    # Simulate signals and trades
    signals = [
        (True, 150.0),   # Executed, win
        (True, -80.0),   # Executed, loss
        (False, 0.0),    # Filtered
        (True, 200.0),   # Executed, win
        (True, 100.0),   # Executed, win
        (False, 0.0),    # Filtered
        (False, 0.0),    # Filtered
        (True, -90.0),   # Executed, loss
        (True, 180.0),   # Executed, win
        (True, 120.0),   # Executed, win
    ]
    
    for executed, pnl in signals:
        ppo.record_signal(executed, pnl)
    
    summary = ppo.get_summary()
    
    print(f"  Total Valid Signals: {summary['valid_signals']}")
    print(f"  Executed Trades: {summary['executed_trades']}")
    print(f"  Filtered Signals: {summary['filtered_signals']}")
    print(f"  Execution Rate: {summary['execution_rate']*100:.1f}%")
    print(f"  Total PnL: ${summary['total_pnl']:.2f}")
    print(f"  PPO: ${summary['ppo']:.2f} per opportunity")
    print(f"  Diagnosis: {summary['diagnosis']}")
    
    print("\n  Interpretation:")
    print("    PPO = Total PnL / All Valid Signals")
    print("    Low PPO + Positive PnL = Filtering too much (leaving money)")
    print("    High PPO = Maximizing every opportunity")


def demo_full_integration():
    """Full integration demo showing complete workflow."""
    print("\n" + "="*70)
    print("FULL INTEGRATION DEMO: Complete Trade Workflow")
    print("="*70)
    
    from engine.market_regime import classify_market_regime, MarketRegime
    from engine.risk_middleware import get_risk_middleware, process_signal_through_middleware
    from engine.advanced_analytics import (
        get_trade_instrumenter, 
        record_trade_for_edge_health,
        classify_sl_bin
    )
    
    print("\n  1. MARKET REGIME CLASSIFICATION")
    print("  " + "-"*40)
    
    # Classify market regime
    regime, details = classify_market_regime(
        adx=27.0,
        current_atr=600.0
    )
    print(f"     Regime: {regime.value}")
    print(f"     ATR Ratio: {details['atr_ratio']:.2f}")
    
    print("\n  2. SIGNAL PROCESSING THROUGH MIDDLEWARE")
    print("  " + "-"*40)
    
    # Process signal
    order = process_signal_through_middleware(
        direction="LONG",
        base_size=0.01,
        entry_price=100000.0,
        stop_loss=99200.0,
        setup_name="BOS_RETEST",
        regime=regime,
        capital=5000.0
    )
    
    print(f"     Original Size: {order.original_size:.6f}")
    print(f"     Adjusted Size: {order.adjusted_size:.6f}")
    print(f"     Risk: {order.adjusted_risk_pct:.2f}%")
    print(f"     Should Trade: {order.should_trade}")
    
    print("\n  3. TRADE INSTRUMENTATION (Caja Negra)")
    print("  " + "-"*40)
    
    # Instrument entry
    instrumenter = get_trade_instrumenter()
    entry_data = instrumenter.instrument_entry(
        trade_id="DEMO_001",
        symbol="BTC/USDT",
        entry_price=100000.0,
        direction="LONG",
        position_size=order.adjusted_size,
        stop_loss=99200.0,
        take_profit_1=100800.0,
        atr=600.0,
        adx=27.0,
        setup_type="BOS_RETEST"
    )
    
    print(f"     SL Bin: {entry_data.sl_bin}")
    print(f"     Session: {entry_data.session}")
    
    print("\n  4. TRADE EXIT & RECORDING")
    print("  " + "-"*40)
    
    # Instrument exit
    # PnL = (100900 - 100000) * adjusted_size
    # For demo, use consistent values
    pnl_value = (100900.0 - 100000.0) * order.adjusted_size  # ~$56
    exit_record = instrumenter.instrument_exit(
        exit_price=100900.0,
        exit_time=datetime.now(UTC),
        exit_reason="TP1_HIT",
        pnl=pnl_value,
        mfe_r=1.3,
        mae_r=0.4,
        duration_minutes=35.0
    )
    
    print(f"     PnL: ${exit_record['pnl']:.2f}")
    print(f"     R Realized: {exit_record['r_realized']:.2f}R")
    
    # Record for middleware
    middleware = get_risk_middleware()
    # Risk: (entry - SL) * size = (100000 - 99200) * 0.0625 = $50
    # Escalated would be slightly more
    middleware.record_trade_result(
        setup_name="BOS_RETEST",
        pnl=pnl_value,
        mfe_r=1.3,
        base_risk_pct=0.8,
        escalated_risk_pct=0.9,
        entry_price=100000.0,
        stop_loss=99200.0
    )
    
    # Record for edge health
    health_metrics = record_trade_for_edge_health(exit_record['r_realized'])
    
    print(f"\n  5. EDGE HEALTH STATUS")
    print("  " + "-"*40)
    print(f"     Trade Count: {health_metrics['trade_count']}")
    print(f"     Observation Mode: {health_metrics['in_observation_mode']}")
    print(f"     Risk Multiplier: {health_metrics['risk_multiplier']:.2f}x")
    
    print("\n  ✅ Full integration workflow complete!")


if __name__ == "__main__":
    print("\n" + "="*70)
    print("  ADVANCED INSTRUMENTATION & RISK MIDDLEWARE DEMO")
    print("  DayTrader 2.1 - V2.1")
    print("="*70)
    
    try:
        demo_sl_bin_classification()
        demo_market_regime_classification()
        demo_risk_middleware()
        demo_trade_instrumentation()
        demo_edge_health_tracking()
        demo_3_phase_trailing()
        demo_ppo_metric()
        demo_full_integration()
        
        print("\n" + "="*70)
        print("  ALL DEMOS COMPLETED SUCCESSFULLY")
        print("="*70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
