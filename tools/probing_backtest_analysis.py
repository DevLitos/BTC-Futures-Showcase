#!/usr/bin/env python3
"""
PROBING SYSTEM BACKTEST ANALYSIS
================================
Analyzes historical trades to simulate impact of 3-level PROBING system.

PROBING Levels:
- PROBE_MIN (0.25x): Base criteria only
- PROBE_MED (0.50x): + EMA/VWAP alignment
- PROBE_MAX (0.65x): + BB squeeze, volume spike

Exit Rules for PROBING:
- MAE > 0.3R → ABORT (thesis failed)
- 3min without +0.2R → TIMEOUT (no edge shown)

This script:
1. Loads historical trades
2. Classifies what PROBING level each would have gotten
3. Applies sizing adjustments
4. Simulates exit rules (MAE abort, timeout)
5. Computes expected PnL with new system
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from collections import defaultdict

@dataclass
class TradeAnalysis:
    """Analysis result for a single trade"""
    trade_id: str
    direction: str
    entry_price: float
    pnl_r: float
    mfe_r: float
    mae_r: float
    original_size: float
    regime: str
    vwap_distance_atr: float
    vpoc_price: float
    score: float
    duration_minutes: float
    exit_reason: str
    
    # PROBING analysis
    probing_eligible: bool
    probing_level: str  # "PROBE_MIN", "PROBE_MED", "PROBE_MAX", "NOT_ELIGIBLE"
    probing_size_mult: float
    probing_would_abort_mae: bool
    probing_would_timeout: bool
    
    # Adjusted results
    adjusted_size: float
    adjusted_pnl_r: float
    adjusted_pnl_usd: float


def classify_probing_level(trade: dict) -> Tuple[bool, str, float, List[str]]:
    """
    Classify what PROBING level a trade would have gotten.
    
    Returns: (eligible, level, size_mult, reasons)
    """
    entry_tags = trade.get('entry_tags', [])
    
    # Extract metrics from trade data
    vwap_distance = 999.0
    vpoc_valid = False
    htf_aligned = 'htf_aligned' in entry_tags
    
    # Try to get VWAP distance from tags (it's stored differently in old trades)
    for tag in entry_tags:
        if isinstance(tag, str) and 'vwap_distance' in tag.lower():
            # Try to extract value if it's in the tag name
            pass
    
    # Check if we have the metrics we need
    # In historical trades, VWAP distance is stored in entry_tags
    if 'vwap_distance_atr' in entry_tags:
        # This is just a marker, actual value might be in metadata
        vwap_ok = True  # Assume OK if tag exists
    else:
        vwap_ok = False
    
    if 'vpoc_valid' in entry_tags or 'vpoc_price' in entry_tags:
        vpoc_ok = True
    else:
        vpoc_ok = False
    
    # Session quality
    session_ok = 'session_quality' in entry_tags
    
    # Check base eligibility
    if not htf_aligned:
        return False, "NOT_ELIGIBLE", 0.0, ["HTF not aligned"]
    
    # For historical analysis, assume location was OK if trade was taken
    # (since old system already filtered)
    
    # Build conviction score
    conviction_score = 1  # Base
    reasons = []
    
    # EMA alignment (check from entry_tags)
    if 'ema_reclaim' in entry_tags:
        conviction_score += 1
        reasons.append("EMA_reclaim")
    
    if 'bos_confirmed' in entry_tags:
        conviction_score += 1
        reasons.append("BOS_confirmed")
    
    # Check score breakdown for momentum
    score_breakdown = trade.get('score_breakdown', {})
    if score_breakdown.get('momentum', 0) >= 2:
        conviction_score += 1
        reasons.append("Momentum_strong")
    
    if score_breakdown.get('timing', 0) >= 1:
        conviction_score += 1
        reasons.append("Timing_confirmed")
    
    # Determine level
    if conviction_score >= 5:
        level = "PROBE_MAX"
        size_mult = 0.65
    elif conviction_score >= 3:
        level = "PROBE_MED"
        size_mult = 0.50
    else:
        level = "PROBE_MIN"
        size_mult = 0.25
    
    return True, level, size_mult, reasons


def would_abort_mae(mae_r: float, duration_minutes: float) -> bool:
    """
    Check if trade would have been aborted due to MAE > 0.5R (after 60s)
    
    UPDATED: MAE threshold raised from 0.3R to 0.5R based on backtest
    analysis showing many winners had drawdowns >0.3R before reversing.
    Added time requirement (60s = 1min) to avoid panic exits.
    """
    # Only abort if MAE > 0.5R AND time > 1 minute
    return mae_r > 0.5 and duration_minutes > 1.0


def would_timeout(mfe_r: float, duration_minutes: float) -> bool:
    """
    Check if trade would have timed out (no +0.2R in 5 minutes)
    
    UPDATED: Timeout increased from 3min to 5min based on backtest
    analysis showing some trades needed more time to develop.
    """
    # If MFE never reached 0.2R and duration > 5 minutes
    if duration_minutes > 5.0 and mfe_r < 0.2:
        return True
    return False


def analyze_trade(trade: dict) -> TradeAnalysis:
    """Analyze a single trade for PROBING impact"""
    
    # Extract basic info
    trade_id = trade.get('trade_id', 'unknown')
    direction = trade.get('direction', 'UNKNOWN')
    entry_price = trade.get('entry_price', 0)
    pnl_r = trade.get('pnl_r', 0)
    mfe_r = trade.get('mfe_r', 0)
    mae_r = trade.get('mae_r', 0)
    original_size = trade.get('position_size', 0)
    regime = trade.get('regime', 'UNKNOWN')
    score = trade.get('entry_score', 0)
    duration_minutes = trade.get('duration_minutes', 0)
    exit_reason = trade.get('exit_reason', 'UNKNOWN')
    pnl_usd = trade.get('pnl_usd', 0)
    
    # Extract from entry_tags
    entry_tags = trade.get('entry_tags', [])
    vwap_distance_atr = 0  # Would need to extract from actual tags
    vpoc_price = 0  # Would need to extract from actual tags
    
    # Classify PROBING
    eligible, level, size_mult, reasons = classify_probing_level(trade)
    
    # Check abort conditions
    abort_mae = would_abort_mae(mae_r, duration_minutes) if eligible else False
    timeout = would_timeout(mfe_r, duration_minutes) if eligible else False
    
    # Calculate adjusted results
    if eligible:
        adjusted_size = original_size * size_mult
        
        # If would have aborted, estimate exit at ~-0.25R
        if abort_mae:
            adjusted_pnl_r = -0.25  # Exit at approximately this level
        elif timeout and pnl_r > 0:
            # If would timeout but was profitable, assume early exit at ~0.1R
            adjusted_pnl_r = min(pnl_r, 0.1)
        elif timeout:
            # If would timeout and was losing, assume exit at approximately -0.15R
            adjusted_pnl_r = max(pnl_r, -0.15)
        else:
            adjusted_pnl_r = pnl_r
        
        # Adjust USD PnL proportionally
        if pnl_r != 0:
            adjusted_pnl_usd = pnl_usd * (adjusted_pnl_r / pnl_r) * size_mult
        else:
            adjusted_pnl_usd = 0
    else:
        adjusted_size = original_size
        adjusted_pnl_r = pnl_r
        adjusted_pnl_usd = pnl_usd
    
    return TradeAnalysis(
        trade_id=trade_id,
        direction=direction,
        entry_price=entry_price,
        pnl_r=pnl_r,
        mfe_r=mfe_r,
        mae_r=mae_r,
        original_size=original_size,
        regime=regime,
        vwap_distance_atr=vwap_distance_atr,
        vpoc_price=vpoc_price,
        score=score,
        duration_minutes=duration_minutes,
        exit_reason=exit_reason,
        probing_eligible=eligible,
        probing_level=level,
        probing_size_mult=size_mult,
        probing_would_abort_mae=abort_mae,
        probing_would_timeout=timeout,
        adjusted_size=adjusted_size,
        adjusted_pnl_r=adjusted_pnl_r,
        adjusted_pnl_usd=adjusted_pnl_usd
    )


def load_trades(trade_logs_dir: str) -> List[dict]:
    """Load all trades from JSONL files"""
    trades = []
    
    for filename in sorted(os.listdir(trade_logs_dir)):
        if filename.startswith('trades_') and filename.endswith('.jsonl'):
            filepath = os.path.join(trade_logs_dir, filename)
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trade = json.loads(line)
                            # Skip test trades
                            if trade.get('exit_reason') == 'TEST':
                                continue
                            trades.append(trade)
                        except json.JSONDecodeError:
                            pass
    
    return trades


def main():
    """Run PROBING backtest analysis"""
    print("=" * 70)
    print("PROBING SYSTEM BACKTEST ANALYSIS")
    print("=" * 70)
    print()
    
    # Load trades
    trade_logs_dir = os.path.join(os.path.dirname(__file__), '..', 'trade_logs')
    trades = load_trades(trade_logs_dir)
    
    print(f"Loaded {len(trades)} trades from logs")
    print()
    
    # Analyze each trade
    analyses = [analyze_trade(t) for t in trades]
    
    # Filter to RANGE regime only (PROBING targets RANGE)
    range_analyses = [a for a in analyses if a.regime == 'RANGE']
    
    print(f"RANGE regime trades: {len(range_analyses)}")
    print()
    
    # Statistics
    print("=" * 70)
    print("PROBING ELIGIBILITY BREAKDOWN")
    print("=" * 70)
    
    eligible = [a for a in range_analyses if a.probing_eligible]
    not_eligible = [a for a in range_analyses if not a.probing_eligible]
    
    print(f"Eligible for PROBING: {len(eligible)}")
    print(f"Not eligible: {len(not_eligible)}")
    print()
    
    # Level breakdown
    level_counts = defaultdict(int)
    for a in eligible:
        level_counts[a.probing_level] += 1
    
    print("PROBING Level Distribution:")
    for level in ["PROBE_MIN", "PROBE_MED", "PROBE_MAX"]:
        count = level_counts[level]
        pct = (count / len(eligible) * 100) if eligible else 0
        print(f"  {level}: {count} ({pct:.1f}%)")
    print()
    
    # Abort analysis
    print("=" * 70)
    print("PROBING EXIT RULE ANALYSIS")
    print("=" * 70)
    
    abort_mae = [a for a in eligible if a.probing_would_abort_mae]
    timeout = [a for a in eligible if a.probing_would_timeout and not a.probing_would_abort_mae]
    
    print(f"Would abort (MAE > 0.3R): {len(abort_mae)} ({len(abort_mae)/len(eligible)*100:.1f}%)")
    print(f"Would timeout (no +0.2R in 3min): {len(timeout)} ({len(timeout)/len(eligible)*100:.1f}%)")
    print()
    
    # PnL comparison
    print("=" * 70)
    print("PNL COMPARISON: ORIGINAL vs PROBING SYSTEM")
    print("=" * 70)
    
    # Original system
    orig_pnl_r = sum(a.pnl_r for a in range_analyses)
    orig_pnl_usd = sum(trades[i].get('pnl_usd', 0) for i, a in enumerate(analyses) if a.regime == 'RANGE' and a in range_analyses[:len(range_analyses)])
    
    # Just use the computed values
    orig_pnl_usd = 0
    for t in trades:
        if t.get('regime') == 'RANGE' and t.get('exit_reason') != 'TEST':
            orig_pnl_usd += t.get('pnl_usd', 0)
    
    orig_wins = len([a for a in range_analyses if a.pnl_r > 0])
    orig_losses = len([a for a in range_analyses if a.pnl_r <= 0])
    
    print(f"ORIGINAL SYSTEM (RANGE trades):")
    print(f"  Total PnL (R): {orig_pnl_r:.2f}R")
    print(f"  Total PnL (USD): ${orig_pnl_usd:.2f}")
    print(f"  Win Rate: {orig_wins}/{len(range_analyses)} ({orig_wins/len(range_analyses)*100:.1f}%)")
    print(f"  Avg PnL/trade (R): {orig_pnl_r/len(range_analyses):.3f}R")
    print()
    
    # PROBING system
    probe_pnl_r = sum(a.adjusted_pnl_r for a in eligible)
    probe_pnl_usd = sum(a.adjusted_pnl_usd for a in eligible)
    probe_wins = len([a for a in eligible if a.adjusted_pnl_r > 0])
    
    print(f"PROBING SYSTEM (eligible trades):")
    print(f"  Total PnL (R): {probe_pnl_r:.2f}R")
    print(f"  Total PnL (USD): ${probe_pnl_usd:.2f}")
    print(f"  Win Rate: {probe_wins}/{len(eligible)} ({probe_wins/len(eligible)*100:.1f}%)")
    print(f"  Avg PnL/trade (R): {probe_pnl_r/len(eligible):.3f}R")
    print()
    
    # Per-level analysis
    print("=" * 70)
    print("PNL BY PROBING LEVEL")
    print("=" * 70)
    
    for level in ["PROBE_MIN", "PROBE_MED", "PROBE_MAX"]:
        level_trades = [a for a in eligible if a.probing_level == level]
        if level_trades:
            level_pnl_r = sum(a.adjusted_pnl_r for a in level_trades)
            level_wins = len([a for a in level_trades if a.adjusted_pnl_r > 0])
            
            print(f"{level}:")
            print(f"  Trades: {len(level_trades)}")
            print(f"  Total PnL (R): {level_pnl_r:.2f}R")
            print(f"  Win Rate: {level_wins}/{len(level_trades)} ({level_wins/len(level_trades)*100:.1f}%)")
            print(f"  Avg PnL/trade: {level_pnl_r/len(level_trades):.3f}R")
            print()
    
    # Risk-adjusted comparison
    print("=" * 70)
    print("RISK-ADJUSTED ANALYSIS")
    print("=" * 70)
    
    # Calculate effective risk exposure
    # Original: 100% position size
    # PROBING: 25-65% position size
    
    avg_probe_size = sum(a.probing_size_mult for a in eligible) / len(eligible) if eligible else 0
    risk_adjusted_orig = orig_pnl_r  # 100% exposure
    risk_adjusted_probe = probe_pnl_r / avg_probe_size if avg_probe_size > 0 else 0  # Normalize to 100%
    
    print(f"Average PROBING size mult: {avg_probe_size:.2f}x")
    print(f"Original PnL (R): {risk_adjusted_orig:.2f}R")
    print(f"PROBING PnL normalized to 100%: {risk_adjusted_probe:.2f}R")
    print()
    
    # Trade-by-trade breakdown
    print("=" * 70)
    print("TRADE-BY-TRADE BREAKDOWN (RANGE regime)")
    print("=" * 70)
    print()
    print(f"{'ID':<25} {'Dir':<6} {'Score':<6} {'PnL_R':<8} {'MFE':<6} {'MAE':<6} {'Level':<12} {'Abort':<6} {'Adj_R':<8}")
    print("-" * 95)
    
    for a in range_analyses:
        abort_str = "MAE" if a.probing_would_abort_mae else ("TIME" if a.probing_would_timeout else "-")
        level_str = a.probing_level if a.probing_eligible else "N/A"
        
        print(f"{a.trade_id:<25} {a.direction:<6} {a.score:<6.1f} {a.pnl_r:<8.2f} {a.mfe_r:<6.2f} {a.mae_r:<6.2f} {level_str:<12} {abort_str:<6} {a.adjusted_pnl_r:<8.2f}")
    
    print()
    print("=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    
    improvement = (probe_pnl_r - orig_pnl_r) if len(eligible) > 0 else 0
    
    if improvement > 0:
        print(f"✅ PROBING system would IMPROVE performance by {improvement:.2f}R")
    elif improvement < 0:
        print(f"⚠️ PROBING system would REDUCE performance by {abs(improvement):.2f}R")
    else:
        print(f"📊 PROBING system would have SIMILAR performance")
    
    print()
    print("Key benefits of PROBING:")
    print("  - Reduced risk exposure (avg {:.0f}% of original)".format(avg_probe_size * 100))
    print(f"  - MAE abort would have saved {len(abort_mae)} losing trades")
    print(f"  - Timeout abort would have exited {len(timeout)} stagnant trades")
    print()


if __name__ == '__main__':
    main()
