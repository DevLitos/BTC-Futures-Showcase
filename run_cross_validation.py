#!/usr/bin/env python3
"""
Cross-Validation Backtest Runner

Ejecuta backtest en múltiples periodos para validar robustez:
- P1: Last 7 days (recent performance)
- P2: 7-14 days ago (intermediate)
- P3: 14-21 days ago (TARGET - must be stable)
- OOS: Optional additional month

REGLA DE ORO: Si algo sube P1 pero destroza estabilidad, SE DESCARTA.

Uso:
    python run_cross_validation.py
    python run_cross_validation.py --baseline  # Set baseline (sin features)
    python run_cross_validation.py --feature capital_weighting  # Test una feature
"""

import asyncio
import argparse
import logging
from datetime import datetime, UTC
import json
from pathlib import Path

from run_backtest_kraken import BacktestRunner, download_kraken_data
from engine.backtest_validation import (
    CrossValidator,
    PeriodAnalyzer,
    PeriodMetrics,
    ValidationConfig,
    StabilityAnalyzer,
    SlippageSimulator,
    FeatureFlag,
    get_feature_flags,
    get_validator,
    print_validation_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('CrossValidation')


# =============================================================================
# CROSS-VALIDATION RUNNER
# =============================================================================

async def run_period(symbol: str, days: int, offset: int, capital: float) -> dict:
    """Run backtest for a single period."""
    logger.info(f"\n{'='*50}")
    logger.info(f"Running period: offset={offset} days")
    logger.info(f"{'='*50}")
    
    # Download data for this period
    df_1m, df_5m, df_1h = download_kraken_data(symbol, days, offset)
    
    if len(df_1m) < 500:
        logger.warning(f"Not enough data for offset {offset}")
        return {}
    
    # Run backtest
    runner = BacktestRunner(symbol=symbol, capital=capital)
    results = await runner.run(df_1m, df_5m, df_1h)
    
    return results


def results_to_metrics(results: dict, period_name: str, initial_capital: float) -> PeriodMetrics:
    """Convert backtest results to PeriodMetrics."""
    metrics = PeriodMetrics(period_name=period_name)
    
    if not results or 'trades' not in results:
        return metrics
    
    trades = results.get('trades', [])
    
    metrics.total_trades = results.get('total_trades', 0)
    metrics.wins = results.get('wins', 0)
    metrics.losses = results.get('losses', 0)
    metrics.total_pnl = results.get('total_pnl', 0.0)
    metrics.max_drawdown_pct = results.get('max_drawdown_pct', 0.0)
    
    # Calculate gross profit/loss
    for t in trades:
        pnl = t.get('data', {}).get('pnl', 0)
        if pnl > 0:
            metrics.gross_profit += pnl
        else:
            metrics.gross_loss += abs(pnl)
    
    # Calculate R if available
    for t in trades:
        r = t.get('data', {}).get('r', 0)
        metrics.total_r += r
    
    metrics.calculate_derived(initial_capital)
    
    return metrics


def print_period_results(name: str, results: dict):
    """Print results for a single period."""
    print(f"\n---[ {name} ]---")
    print(f"Total Trades:  {results.get('total_trades', 0)}")
    print(f"Wins:          {results.get('wins', 0)}")
    print(f"Win Rate:      {results.get('win_rate', 0):.2f}%")
    print(f"Total PnL:     ${results.get('total_pnl', 0):.2f}")
    print(f"Final Equity:  ${results.get('final_equity', 10000):.2f}")
    print(f"Return:        {results.get('return_pct', 0):.2f}%")
    print(f"Max Drawdown:  {results.get('max_drawdown_pct', 0):.2f}%")


def print_comprehensive_summary(p1: PeriodMetrics, p2: PeriodMetrics, p3: PeriodMetrics):
    """Print comprehensive summary with stability analysis."""
    print("\n" + "=" * 70)
    print("📊 CROSS-VALIDATION SUMMARY")
    print("=" * 70)
    
    # Period comparison table
    print("\n" + "-" * 70)
    print(f"{'Metric':<20} {'P1 (Recent)':<15} {'P2 (Mid)':<15} {'P3 (Target)':<15}")
    print("-" * 70)
    
    metrics = [
        ('Trades', p1.total_trades, p2.total_trades, p3.total_trades),
        ('Win Rate', f"{p1.win_rate*100:.1f}%", f"{p2.win_rate*100:.1f}%", f"{p3.win_rate*100:.1f}%"),
        ('Return', f"{p1.return_pct:.2f}%", f"{p2.return_pct:.2f}%", f"{p3.return_pct:.2f}%"),
        ('Expectancy', f"{p1.expectancy_r:.3f}R", f"{p2.expectancy_r:.3f}R", f"{p3.expectancy_r:.3f}R"),
        ('Profit Factor', f"{p1.profit_factor:.2f}", f"{p2.profit_factor:.2f}", f"{p3.profit_factor:.2f}"),
        ('Max DD', f"{p1.max_drawdown_pct:.2f}%", f"{p2.max_drawdown_pct:.2f}%", f"{p3.max_drawdown_pct:.2f}%"),
    ]
    
    for row in metrics:
        print(f"{row[0]:<20} {str(row[1]):<15} {str(row[2]):<15} {str(row[3]):<15}")
    
    print("-" * 70)
    
    # Stability analysis
    stability_score, stability_desc = StabilityAnalyzer.calculate_stability_score(p1, p2, p3)
    is_overfitting, overfit_reason = StabilityAnalyzer.detect_overfitting(p1, p2, p3)
    
    print(f"\n📈 STABILITY ANALYSIS:")
    print(f"   Score: {stability_score:.1f}/100 - {stability_desc}")
    
    if is_overfitting:
        print(f"   ⚠️ OVERFITTING WARNING: {overfit_reason}")
    else:
        print(f"   ✅ No overfitting detected")
    
    # Key ratios
    print(f"\n📊 KEY RATIOS:")
    
    # P1/P3 ratio
    if p3.return_pct > 0 and p1.return_pct > 0:
        p1_p3_ratio = p1.return_pct / p3.return_pct
        status = "⚠️ HIGH" if p1_p3_ratio > 2.0 else "✅ OK"
        print(f"   P1/P3 Return Ratio: {p1_p3_ratio:.2f}x {status}")
    
    # Average expectancy across periods
    avg_expectancy = (p1.expectancy_r + p2.expectancy_r + p3.expectancy_r) / 3
    print(f"   Average Expectancy: {avg_expectancy:.3f}R")
    
    # DD/Return ratio for P3
    if p3.return_pct > 0:
        dd_return_ratio = p3.max_drawdown_pct / p3.return_pct
        print(f"   DD/Return Ratio (P3): {dd_return_ratio:.2f}")
    
    print("=" * 70 + "\n")


async def run_cross_validation(symbol: str, capital: float, save_baseline: bool = False):
    """
    Run full cross-validation across P1, P2, P3.
    """
    print("\n" + "=" * 70)
    print("🔬 CROSS-VALIDATION BACKTEST")
    print(f"   Symbol: {symbol}")
    print(f"   Capital: ${capital}")
    print(f"   Mode: {'Set Baseline' if save_baseline else 'Validate'}")
    print("=" * 70)
    
    # Run P1: Last 7 days
    results_p1 = await run_period(symbol, days=7, offset=0, capital=capital)
    print_period_results("P1: Last 7 days", results_p1)
    
    # Run P2: 7-14 days ago
    results_p2 = await run_period(symbol, days=7, offset=7, capital=capital)
    print_period_results("P2: 7-14 days ago", results_p2)
    
    # Run P3: 14-21 days ago (TARGET)
    results_p3 = await run_period(symbol, days=7, offset=14, capital=capital)
    print_period_results("P3: 14-21 days ago (TARGET)", results_p3)
    
    # Convert to metrics
    p1 = results_to_metrics(results_p1, "P1", capital)
    p2 = results_to_metrics(results_p2, "P2", capital)
    p3 = results_to_metrics(results_p3, "P3", capital)
    
    # Print comprehensive summary
    print_comprehensive_summary(p1, p2, p3)
    
    # Save baseline if requested
    if save_baseline:
        validator = get_validator()
        validator.set_baseline(p1, p2, p3)
        
        # Save to file
        baseline_file = Path("config/baseline_metrics.json")
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        
        baseline_data = {
            'timestamp': datetime.now(UTC).isoformat(),
            'symbol': symbol,
            'capital': capital,
            'p1': p1.to_dict(),
            'p2': p2.to_dict(),
            'p3': p3.to_dict(),
        }
        
        with open(baseline_file, 'w') as f:
            json.dump(baseline_data, f, indent=2)
        
        print(f"✅ Baseline saved to {baseline_file}")
    
    return p1, p2, p3


async def validate_feature(feature_name: str, symbol: str, capital: float):
    """
    Validate a feature against the saved baseline.
    """
    # Load baseline
    baseline_file = Path("config/baseline_metrics.json")
    
    if not baseline_file.exists():
        print("❌ No baseline found. Run with --baseline first.")
        return
    
    with open(baseline_file, 'r') as f:
        baseline = json.load(f)
    
    # Recreate baseline metrics
    def dict_to_metrics(d: dict, name: str) -> PeriodMetrics:
        m = PeriodMetrics(period_name=name)
        m.total_trades = d.get('trades', 0)
        m.return_pct = float(d.get('return_pct', '0%').rstrip('%'))
        m.expectancy_r = float(d.get('expectancy_r', '0R').rstrip('R'))
        m.max_drawdown_pct = float(d.get('max_dd', '0%').rstrip('%'))
        m.profit_factor = float(d.get('profit_factor', '0'))
        return m
    
    baseline_p1 = dict_to_metrics(baseline['p1'], "P1")
    baseline_p2 = dict_to_metrics(baseline['p2'], "P2")
    baseline_p3 = dict_to_metrics(baseline['p3'], "P3")
    
    # Set baseline in validator
    validator = get_validator()
    validator.set_baseline(baseline_p1, baseline_p2, baseline_p3)
    
    print(f"\n🔬 Testing feature: {feature_name}")
    print(f"   (Baseline from: {baseline.get('timestamp', 'unknown')})")
    
    # Run cross-validation with feature enabled
    # Note: Feature enabling logic would go here
    # For now, just run the same backtest (user should enable feature manually)
    
    p1, p2, p3 = await run_cross_validation(symbol, capital, save_baseline=False)
    
    # Validate
    result = validator.validate_feature(feature_name, p1, p2, p3)
    print_validation_summary(result)
    
    # Update feature flags
    try:
        flag = FeatureFlag(feature_name)
        flags = get_feature_flags()
        flags.validate_and_toggle(flag, result)
        print(flags.get_status_report())
    except ValueError:
        print(f"   (Feature '{feature_name}' not in FeatureFlag enum)")
    
    return result


async def main():
    parser = argparse.ArgumentParser(description='Cross-Validation Backtest Runner')
    parser.add_argument('--symbol', default='BTC/USD:USD', help='Trading symbol')
    parser.add_argument('--capital', type=float, default=10000, help='Initial capital')
    parser.add_argument('--baseline', action='store_true', help='Save as baseline')
    parser.add_argument('--feature', type=str, help='Feature to validate')
    args = parser.parse_args()
    
    if args.feature:
        await validate_feature(args.feature, args.symbol, args.capital)
    else:
        await run_cross_validation(args.symbol, args.capital, save_baseline=args.baseline)


if __name__ == '__main__':
    asyncio.run(main())
