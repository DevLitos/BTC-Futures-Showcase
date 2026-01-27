#!/usr/bin/env python3
"""
PROBE_MAX MONTE CARLO ANALYSIS
==============================
Simulates multiple PROBE_MAX scenarios based on realistic market behavior
to validate the sizing and exit rules.

PROBE_MAX Criteria:
- VPOC < 0.75 ATR
- VWAP < 1.0 ATR  
- VWAP slope aligned
- EMA20 aligned
- EMA50 aligned
- BB squeeze ending
- Volume spike

This gives conviction score >= 5 → 0.65x sizing
"""

import random
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple
import statistics

# Set seed for reproducibility
random.seed(42)
np.random.seed(42)

@dataclass
class SimulatedTrade:
    """A simulated PROBE_MAX trade"""
    conviction_score: int
    entry_price: float
    atr: float
    
    # Outcomes
    max_mfe_r: float
    max_mae_r: float
    final_pnl_r: float
    time_to_mfe_sec: float
    duration_sec: float
    
    # PROBING adjustments
    would_abort_mae: bool
    would_timeout: bool
    adjusted_pnl_r: float
    
    # Labels
    scenario: str  # "WINNER", "LOSER", "SCRATCH"


def generate_probe_max_trade(scenario: str) -> SimulatedTrade:
    """
    Generate a realistic PROBE_MAX trade scenario.
    
    Based on observed market behavior for high-conviction entries:
    - PROBE_MAX should capture strong moves when all signals align
    - But can fail when market reverses against alignment
    
    Scenarios based on real RANGE market behavior:
    - 40% Strong Winner: All signals right, market follows
    - 25% Moderate Winner: Good entry, but limited move
    - 15% Scratch: Market goes nowhere
    - 20% Loser: Signals aligned but market reverses
    """
    
    entry_price = 90000.0  # Typical BTC price
    atr = 350.0  # Typical 5m ATR for BTC
    
    # Conviction score for PROBE_MAX (5-8)
    conviction_score = random.randint(5, 8)
    
    if scenario == "STRONG_WINNER":
        # All signals aligned, market follows
        max_mfe_r = random.uniform(0.8, 2.0)
        max_mae_r = random.uniform(0.0, 0.25)  # Minimal drawdown
        final_pnl_r = random.uniform(0.5, max_mfe_r * 0.9)  # Capture most of move
        time_to_mfe_sec = random.uniform(30, 120)  # Fast to profit
        duration_sec = random.uniform(180, 600)
        
    elif scenario == "MODERATE_WINNER":
        # Good entry but limited extension
        max_mfe_r = random.uniform(0.3, 0.8)
        max_mae_r = random.uniform(0.1, 0.35)  # Some chop
        final_pnl_r = random.uniform(0.15, max_mfe_r * 0.7)
        time_to_mfe_sec = random.uniform(60, 240)
        duration_sec = random.uniform(300, 900)
        
    elif scenario == "SCRATCH":
        # Market goes nowhere
        max_mfe_r = random.uniform(0.05, 0.2)
        max_mae_r = random.uniform(0.1, 0.3)
        final_pnl_r = random.uniform(-0.15, 0.1)  # Small win/loss
        time_to_mfe_sec = random.uniform(120, 400)
        duration_sec = random.uniform(400, 800)
        
    else:  # LOSER
        # Signals aligned but market reverses hard
        max_mfe_r = random.uniform(0.0, 0.25)  # Little profit opportunity
        max_mae_r = random.uniform(0.4, 1.2)  # Big drawdown
        final_pnl_r = random.uniform(-0.8, -0.3)  # Significant loss
        time_to_mfe_sec = random.uniform(0, 60)  # Quick to profit (if any)
        duration_sec = random.uniform(60, 300)  # Stopped out relatively fast
    
    # Apply PROBING exit rules
    # Rule 1: MAE > 0.5R after 60s → Abort at -0.25R
    would_abort_mae = max_mae_r > 0.5 and duration_sec > 60
    
    # Rule 2: No +0.2R after 5min → Timeout
    would_timeout = max_mfe_r < 0.2 and duration_sec > 300
    
    # Calculate adjusted PnL based on exit rules
    if would_abort_mae:
        adjusted_pnl_r = -0.25  # Exit at MAE threshold
    elif would_timeout:
        # Exit at current level (approximate)
        adjusted_pnl_r = max(-0.15, min(0.1, final_pnl_r * 0.5))
    else:
        adjusted_pnl_r = final_pnl_r
    
    return SimulatedTrade(
        conviction_score=conviction_score,
        entry_price=entry_price,
        atr=atr,
        max_mfe_r=max_mfe_r,
        max_mae_r=max_mae_r,
        final_pnl_r=final_pnl_r,
        time_to_mfe_sec=time_to_mfe_sec,
        duration_sec=duration_sec,
        would_abort_mae=would_abort_mae,
        would_timeout=would_timeout,
        adjusted_pnl_r=adjusted_pnl_r,
        scenario=scenario
    )


def run_monte_carlo_simulation(n_trades: int = 100) -> List[SimulatedTrade]:
    """
    Run Monte Carlo simulation of PROBE_MAX trades.
    
    Distribution based on observed high-conviction entries in ranging markets:
    - 40% Strong Winners
    - 25% Moderate Winners  
    - 15% Scratch
    - 20% Losers
    """
    
    scenarios = (
        ["STRONG_WINNER"] * 40 +
        ["MODERATE_WINNER"] * 25 +
        ["SCRATCH"] * 15 +
        ["LOSER"] * 20
    )
    
    trades = []
    for i in range(n_trades):
        scenario = random.choice(scenarios)
        trade = generate_probe_max_trade(scenario)
        trades.append(trade)
    
    return trades


def analyze_results(trades: List[SimulatedTrade]) -> dict:
    """Analyze simulation results"""
    
    # Original system (100% size, no special exits)
    orig_pnl = [t.final_pnl_r for t in trades]
    orig_wins = len([p for p in orig_pnl if p > 0])
    
    # PROBE_MAX system (65% size, with exit rules)
    probe_pnl = [t.adjusted_pnl_r for t in trades]
    probe_wins = len([p for p in probe_pnl if p > 0])
    
    # Risk-adjusted (normalize to 100% exposure)
    probe_pnl_normalized = [p / 0.65 for p in probe_pnl]
    
    # Scenario breakdown
    scenario_results = {}
    for scenario in ["STRONG_WINNER", "MODERATE_WINNER", "SCRATCH", "LOSER"]:
        scenario_trades = [t for t in trades if t.scenario == scenario]
        if scenario_trades:
            scenario_results[scenario] = {
                'count': len(scenario_trades),
                'orig_avg': statistics.mean([t.final_pnl_r for t in scenario_trades]),
                'probe_avg': statistics.mean([t.adjusted_pnl_r for t in scenario_trades]),
                'abort_count': len([t for t in scenario_trades if t.would_abort_mae]),
                'timeout_count': len([t for t in scenario_trades if t.would_timeout])
            }
    
    return {
        'total_trades': len(trades),
        'original': {
            'total_pnl': sum(orig_pnl),
            'avg_pnl': statistics.mean(orig_pnl),
            'std_pnl': statistics.stdev(orig_pnl) if len(orig_pnl) > 1 else 0,
            'win_rate': orig_wins / len(trades),
            'wins': orig_wins,
            'losses': len(trades) - orig_wins
        },
        'probing': {
            'total_pnl': sum(probe_pnl),
            'avg_pnl': statistics.mean(probe_pnl),
            'std_pnl': statistics.stdev(probe_pnl) if len(probe_pnl) > 1 else 0,
            'win_rate': probe_wins / len(trades),
            'wins': probe_wins,
            'losses': len(trades) - probe_wins,
            'total_pnl_normalized': sum(probe_pnl_normalized),
            'avg_pnl_normalized': statistics.mean(probe_pnl_normalized)
        },
        'exit_rules': {
            'mae_aborts': len([t for t in trades if t.would_abort_mae]),
            'timeouts': len([t for t in trades if t.would_timeout])
        },
        'scenarios': scenario_results
    }


def print_results(results: dict):
    """Print formatted results"""
    
    print("=" * 70)
    print("PROBE_MAX MONTE CARLO SIMULATION")
    print("=" * 70)
    print(f"Total simulated trades: {results['total_trades']}")
    print()
    
    print("=" * 70)
    print("SCENARIO DISTRIBUTION")
    print("=" * 70)
    for scenario, data in results['scenarios'].items():
        print(f"{scenario}:")
        print(f"  Count: {data['count']} ({data['count']/results['total_trades']*100:.0f}%)")
        print(f"  Original Avg: {data['orig_avg']:.3f}R")
        print(f"  PROBING Avg: {data['probe_avg']:.3f}R")
        print(f"  MAE Aborts: {data['abort_count']}")
        print(f"  Timeouts: {data['timeout_count']}")
        print()
    
    print("=" * 70)
    print("OVERALL COMPARISON")
    print("=" * 70)
    
    orig = results['original']
    probe = results['probing']
    
    print(f"{'Metric':<25} {'Original':>15} {'PROBE_MAX':>15} {'Normalized':>15}")
    print("-" * 70)
    print(f"{'Total PnL (R)':<25} {orig['total_pnl']:>15.2f} {probe['total_pnl']:>15.2f} {probe['total_pnl_normalized']:>15.2f}")
    print(f"{'Avg PnL/trade (R)':<25} {orig['avg_pnl']:>15.3f} {probe['avg_pnl']:>15.3f} {probe['avg_pnl_normalized']:>15.3f}")
    print(f"{'Std Dev (R)':<25} {orig['std_pnl']:>15.3f} {probe['std_pnl']:>15.3f} {'-':>15}")
    print(f"{'Win Rate':<25} {orig['win_rate']*100:>14.1f}% {probe['win_rate']*100:>14.1f}% {'-':>15}")
    print(f"{'Wins/Losses':<25} {orig['wins']:>7}/{orig['losses']:<7} {probe['wins']:>7}/{probe['losses']:<7} {'-':>15}")
    print()
    
    print("=" * 70)
    print("EXIT RULES EFFECTIVENESS")
    print("=" * 70)
    rules = results['exit_rules']
    print(f"MAE Aborts (>0.5R after 60s): {rules['mae_aborts']} ({rules['mae_aborts']/results['total_trades']*100:.1f}%)")
    print(f"Timeouts (no +0.2R in 5min): {rules['timeouts']} ({rules['timeouts']/results['total_trades']*100:.1f}%)")
    print()
    
    # Calculate Sharpe-like ratio
    orig_sharpe = orig['avg_pnl'] / orig['std_pnl'] if orig['std_pnl'] > 0 else 0
    probe_sharpe = probe['avg_pnl'] / probe['std_pnl'] if probe['std_pnl'] > 0 else 0
    
    print("=" * 70)
    print("RISK-ADJUSTED METRICS")
    print("=" * 70)
    print(f"Sharpe-like ratio (Avg/Std):")
    print(f"  Original: {orig_sharpe:.3f}")
    print(f"  PROBE_MAX: {probe_sharpe:.3f}")
    print(f"  Improvement: {(probe_sharpe - orig_sharpe) / abs(orig_sharpe) * 100 if orig_sharpe != 0 else 0:.1f}%")
    print()
    
    # Expectancy comparison
    orig_expectancy = orig['avg_pnl']
    probe_expectancy = probe['avg_pnl_normalized']  # Normalized to compare fairly
    
    print(f"Expectancy (per trade):")
    print(f"  Original: {orig_expectancy:.3f}R")
    print(f"  PROBE_MAX (normalized): {probe_expectancy:.3f}R")
    
    improvement = probe_expectancy - orig_expectancy
    if improvement > 0:
        print(f"  ✅ IMPROVEMENT: +{improvement:.3f}R per trade")
    else:
        print(f"  ⚠️ DEGRADATION: {improvement:.3f}R per trade")
    print()
    
    print("=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    
    # Risk-adjusted PnL comparison
    # PROBE_MAX uses 65% of capital, so normalize
    if probe['total_pnl_normalized'] > orig['total_pnl']:
        print(f"✅ PROBE_MAX shows BETTER risk-adjusted performance")
        print(f"   Original: {orig['total_pnl']:.2f}R at 100% exposure")
        print(f"   PROBING: {probe['total_pnl']:.2f}R at 65% exposure")
        print(f"   (Normalized: {probe['total_pnl_normalized']:.2f}R)")
    else:
        print(f"⚠️ PROBE_MAX shows WORSE risk-adjusted performance")
        print(f"   Original: {orig['total_pnl']:.2f}R at 100% exposure")
        print(f"   PROBING: {probe['total_pnl']:.2f}R at 65% exposure")
        print(f"   (Normalized: {probe['total_pnl_normalized']:.2f}R)")
    
    # Key benefits
    print()
    print("Key characteristics of PROBE_MAX system:")
    print(f"  - Reduced exposure: 65% of original size")
    print(f"  - Tighter risk control via MAE/timeout rules")
    print(f"  - Std Dev reduction: {(1 - probe['std_pnl']/orig['std_pnl'])*100:.1f}%" if orig['std_pnl'] > 0 else "  - Std Dev: N/A")


def run_multiple_simulations(n_sims: int = 10, trades_per_sim: int = 50):
    """Run multiple simulations to get confidence interval"""
    
    print("=" * 70)
    print(f"RUNNING {n_sims} SIMULATIONS ({trades_per_sim} trades each)")
    print("=" * 70)
    print()
    
    all_orig_totals = []
    all_probe_totals = []
    all_probe_normalized = []
    
    for i in range(n_sims):
        trades = run_monte_carlo_simulation(trades_per_sim)
        results = analyze_results(trades)
        
        all_orig_totals.append(results['original']['total_pnl'])
        all_probe_totals.append(results['probing']['total_pnl'])
        all_probe_normalized.append(results['probing']['total_pnl_normalized'])
    
    print(f"{'Metric':<30} {'Mean':>12} {'Std':>12} {'Min':>12} {'Max':>12}")
    print("-" * 78)
    print(f"{'Original Total PnL (R)':<30} {statistics.mean(all_orig_totals):>12.2f} {statistics.stdev(all_orig_totals):>12.2f} {min(all_orig_totals):>12.2f} {max(all_orig_totals):>12.2f}")
    print(f"{'PROBE_MAX Total PnL (R)':<30} {statistics.mean(all_probe_totals):>12.2f} {statistics.stdev(all_probe_totals):>12.2f} {min(all_probe_totals):>12.2f} {max(all_probe_totals):>12.2f}")
    print(f"{'PROBE_MAX Normalized (R)':<30} {statistics.mean(all_probe_normalized):>12.2f} {statistics.stdev(all_probe_normalized):>12.2f} {min(all_probe_normalized):>12.2f} {max(all_probe_normalized):>12.2f}")
    print()
    
    # Win rate across simulations
    wins = sum(1 for o, p in zip(all_orig_totals, all_probe_normalized) if p > o)
    print(f"PROBE_MAX outperforms Original in {wins}/{n_sims} simulations ({wins/n_sims*100:.0f}%)")
    print()


def main():
    """Main entry point"""
    
    # Single detailed simulation
    print("\n" + "=" * 70)
    print("SINGLE DETAILED SIMULATION (100 trades)")
    print("=" * 70 + "\n")
    
    trades = run_monte_carlo_simulation(100)
    results = analyze_results(trades)
    print_results(results)
    
    # Multiple simulations for confidence
    print("\n")
    run_multiple_simulations(n_sims=20, trades_per_sim=50)


if __name__ == '__main__':
    main()
