"""
Trade Statistics Persistence - Persist and analyze trade-level metrics

PRINCIPLE (Jan 20, 2026):
Every trade must record critical metrics for post-hoc analysis:
- MFE_R: Maximum Favorable Excursion (in R multiples)
- MAE_R: Maximum Adverse Excursion (in R multiples)
- Slippage: Actual vs expected slippage
- Spread at entry: Market conditions at entry
- Regime at entry: What regime was active
- Score at entry: Entry score that was used

These metrics allow:
1. Detecting if we're leaving money on the table (MFE analysis)
2. Detecting if stops are too tight (MAE analysis)
3. Calibrating slippage model (actual vs expected)
4. Validating regime classification (performance by regime)
5. Tuning score thresholds (performance by score)
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import statistics

logger = logging.getLogger(__name__)


@dataclass
class TradeMetrics:
    """
    Core metrics recorded for every trade.
    """
    # Identity
    trade_id: str
    timestamp: str                    # ISO format
    
    # Direction and result
    direction: str                    # 'LONG' or 'SHORT'
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    pnl_usd: float
    pnl_r: float                      # P&L in R multiples
    is_winner: bool
    exit_reason: str                  # 'TP', 'SL', 'TRAILING', 'MANUAL', etc.
    
    # Risk metrics
    risk_usd: float                   # Dollar risk at entry
    risk_bps: float                   # Risk in basis points
    
    # MFE/MAE (Most critical!)
    mfe_price: float                  # Highest/lowest price reached
    mfe_r: float                      # MFE in R multiples
    mae_price: float                  # Worst price reached
    mae_r: float                      # MAE in R multiples
    
    # Execution quality
    expected_entry_slippage_bps: float
    actual_entry_slippage_bps: float
    expected_exit_slippage_bps: float
    actual_exit_slippage_bps: float
    spread_at_entry_bps: float
    spread_at_exit_bps: float
    
    # Market context at entry
    regime_at_entry: str
    regime_confidence: float
    entry_score: float
    volatility_ratio: float
    atr_at_entry: float
    
    # Size info
    position_size: float
    size_multiplier: float            # Actual multiplier used
    entry_type: str                   # 'FULL', 'REDUCED', 'PROBE'
    
    # Timing
    hold_duration_seconds: int
    bars_held: int
    
    # Additional context
    tags: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'TradeMetrics':
        """Create from dictionary."""
        return cls(**data)


class TradeStatisticsStore:
    """
    Persistent storage and analysis of trade metrics.
    
    Stores trades in JSON files organized by date.
    Provides analysis functions for:
    - MFE/MAE distribution
    - Slippage calibration
    - Regime performance
    - Score performance
    
    Usage:
        store = TradeStatisticsStore('/path/to/data')
        
        # Record a trade
        store.record_trade(metrics)
        
        # Analyze
        mfe_analysis = store.analyze_mfe()
        slippage_stats = store.get_slippage_stats()
    """
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            # Default to ./data/trades/
            data_dir = os.path.join(os.getcwd(), 'data', 'trades')
        
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache for current day
        self._today_trades: List[TradeMetrics] = []
        self._today_date: Optional[str] = None
    
    def _get_file_path(self, date_str: str) -> Path:
        """Get file path for a given date."""
        return self.data_dir / f"trades_{date_str}.json"
    
    def record_trade(self, metrics: TradeMetrics) -> None:
        """
        Record a trade to persistent storage.
        """
        # Extract date from timestamp
        try:
            dt = datetime.fromisoformat(metrics.timestamp.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y%m%d')
        except Exception as e:
            logger.warning(f"Could not parse timestamp '{metrics.timestamp}': {e}, using current date")
            date_str = datetime.now(UTC).strftime('%Y%m%d')
        
        # Load existing trades for the day
        file_path = self._get_file_path(date_str)
        trades = []
        
        if file_path.exists():
            try:
                with open(file_path, 'r') as f:
                    trades = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"Could not read {file_path}, starting fresh")
                trades = []
        
        # Add new trade
        trades.append(metrics.to_dict())
        
        # Save
        with open(file_path, 'w') as f:
            json.dump(trades, f, indent=2)
        
        # Update cache
        if self._today_date != date_str:
            self._today_date = date_str
            self._today_trades = []
        self._today_trades.append(metrics)
        
        logger.info(f"📝 Trade recorded: {metrics.trade_id} | {metrics.direction} | "
                   f"P&L: {metrics.pnl_r:.2f}R | MFE: {metrics.mfe_r:.2f}R | MAE: {metrics.mae_r:.2f}R")
    
    def get_trades(self, 
                   date_from: str = None, 
                   date_to: str = None,
                   direction: str = None,
                   regime: str = None,
                   min_score: float = None) -> List[TradeMetrics]:
        """
        Retrieve trades with optional filters.
        
        Args:
            date_from: Start date (YYYYMMDD format)
            date_to: End date (YYYYMMDD format)
            direction: 'LONG' or 'SHORT'
            regime: Regime filter
            min_score: Minimum entry score
        
        Returns:
            List of TradeMetrics
        """
        trades = []
        
        # Get all trade files
        for file_path in sorted(self.data_dir.glob('trades_*.json')):
            file_date = file_path.stem.replace('trades_', '')
            
            # Date filtering
            if date_from and file_date < date_from:
                continue
            if date_to and file_date > date_to:
                continue
            
            try:
                with open(file_path, 'r') as f:
                    day_trades = json.load(f)
                    for trade_data in day_trades:
                        metrics = TradeMetrics.from_dict(trade_data)
                        
                        # Apply filters
                        if direction and metrics.direction != direction:
                            continue
                        if regime and metrics.regime_at_entry != regime:
                            continue
                        if min_score and metrics.entry_score < min_score:
                            continue
                        
                        trades.append(metrics)
            except Exception as e:
                logger.warning(f"Error reading {file_path}: {e}")
        
        return trades
    
    def analyze_mfe(self, 
                    trades: List[TradeMetrics] = None,
                    last_n_days: int = 30) -> Dict:
        """
        Analyze MFE (Maximum Favorable Excursion) distribution.
        
        KEY INSIGHT: If MFE >> actual profit, we're leaving money on table.
        - High MFE_R with low actual P&L = take profit too early
        - Low MFE_R with loss = bad entries, price never went our way
        
        Returns:
            Dict with MFE statistics and insights
        """
        if trades is None:
            date_from = (datetime.now(UTC) - 
                        __import__('datetime').timedelta(days=last_n_days)).strftime('%Y%m%d')
            trades = self.get_trades(date_from=date_from)
        
        if not trades:
            return {'error': 'No trades found'}
        
        mfe_values = [t.mfe_r for t in trades]
        pnl_values = [t.pnl_r for t in trades]
        
        # Calculate "left on table" ratio
        left_on_table = [t.mfe_r - t.pnl_r for t in trades if t.is_winner]
        
        analysis = {
            'total_trades': len(trades),
            'mfe_mean': statistics.mean(mfe_values) if mfe_values else 0,
            'mfe_median': statistics.median(mfe_values) if mfe_values else 0,
            'mfe_p75': sorted(mfe_values)[int(len(mfe_values) * 0.75)] if len(mfe_values) > 3 else 0,
            'mfe_p90': sorted(mfe_values)[int(len(mfe_values) * 0.90)] if len(mfe_values) > 9 else 0,
            'avg_pnl_r': statistics.mean(pnl_values) if pnl_values else 0,
            'avg_left_on_table_r': statistics.mean(left_on_table) if left_on_table else 0,
            'winners_with_mfe_gt_2r': len([t for t in trades if t.mfe_r >= 2.0 and t.is_winner]),
            'winners_captured_lt_50pct_mfe': len([t for t in trades 
                                                   if t.is_winner and t.mfe_r > 0 
                                                   and t.pnl_r / t.mfe_r < 0.5])
        }
        
        # Insight generation
        if analysis['avg_left_on_table_r'] > 1.0:
            analysis['insight'] = "⚠️ Leaving >1R on table on average. Consider adjusting TP or trailing."
        elif analysis['winners_captured_lt_50pct_mfe'] > len(trades) * 0.3:
            analysis['insight'] = "⚠️ >30% of winners captured <50% of MFE. TP may be too tight."
        else:
            analysis['insight'] = "✅ MFE capture looks reasonable."
        
        return analysis
    
    def analyze_mae(self, 
                    trades: List[TradeMetrics] = None,
                    last_n_days: int = 30) -> Dict:
        """
        Analyze MAE (Maximum Adverse Excursion) distribution.
        
        KEY INSIGHT: MAE shows how much pain we endured before exit.
        - High MAE_R on winners = stops may be too tight (getting stopped out unnecessarily)
        - Low MAE_R on losers = stops working correctly
        
        Returns:
            Dict with MAE statistics and insights
        """
        if trades is None:
            date_from = (datetime.now(UTC) - 
                        __import__('datetime').timedelta(days=last_n_days)).strftime('%Y%m%d')
            trades = self.get_trades(date_from=date_from)
        
        if not trades:
            return {'error': 'No trades found'}
        
        mae_values = [t.mae_r for t in trades]
        winner_mae = [t.mae_r for t in trades if t.is_winner]
        loser_mae = [t.mae_r for t in trades if not t.is_winner]
        
        # Trades that went against us significantly before winning
        close_calls = [t for t in trades if t.is_winner and t.mae_r >= 0.7]
        
        analysis = {
            'total_trades': len(trades),
            'mae_mean': statistics.mean(mae_values) if mae_values else 0,
            'mae_median': statistics.median(mae_values) if mae_values else 0,
            'winner_mae_mean': statistics.mean(winner_mae) if winner_mae else 0,
            'loser_mae_mean': statistics.mean(loser_mae) if loser_mae else 0,
            'close_call_winners': len(close_calls),
            'close_call_pct': len(close_calls) / len([t for t in trades if t.is_winner]) * 100 
                              if [t for t in trades if t.is_winner] else 0,
            'trades_with_mae_gt_1r': len([t for t in trades if t.mae_r >= 1.0])
        }
        
        # Insight generation
        if analysis['close_call_pct'] > 25:
            analysis['insight'] = "⚠️ >25% of winners had MAE >0.7R. Stops may be too tight."
        elif analysis['loser_mae_mean'] > 0.95:
            analysis['insight'] = "✅ Losers hit stop loss cleanly (MAE ~1R)."
        else:
            analysis['insight'] = "📊 MAE distribution within expected range."
        
        return analysis
    
    def get_slippage_stats(self, 
                           trades: List[TradeMetrics] = None,
                           last_n_days: int = 30) -> Dict:
        """
        Analyze actual vs expected slippage for model calibration.
        
        Returns:
            Dict with slippage statistics
        """
        if trades is None:
            date_from = (datetime.now(UTC) - 
                        __import__('datetime').timedelta(days=last_n_days)).strftime('%Y%m%d')
            trades = self.get_trades(date_from=date_from)
        
        if not trades:
            return {'error': 'No trades found'}
        
        entry_expected = [t.expected_entry_slippage_bps for t in trades]
        entry_actual = [t.actual_entry_slippage_bps for t in trades]
        exit_expected = [t.expected_exit_slippage_bps for t in trades]
        exit_actual = [t.actual_exit_slippage_bps for t in trades]
        
        entry_diff = [a - e for a, e in zip(entry_actual, entry_expected)]
        exit_diff = [a - e for a, e in zip(exit_actual, exit_expected)]
        
        analysis = {
            'total_trades': len(trades),
            'entry_slippage': {
                'expected_mean': statistics.mean(entry_expected) if entry_expected else 0,
                'actual_mean': statistics.mean(entry_actual) if entry_actual else 0,
                'actual_p95': sorted(entry_actual)[int(len(entry_actual) * 0.95)] if len(entry_actual) > 19 else 0,
                'model_error_mean': statistics.mean(entry_diff) if entry_diff else 0
            },
            'exit_slippage': {
                'expected_mean': statistics.mean(exit_expected) if exit_expected else 0,
                'actual_mean': statistics.mean(exit_actual) if exit_actual else 0,
                'actual_p95': sorted(exit_actual)[int(len(exit_actual) * 0.95)] if len(exit_actual) > 19 else 0,
                'model_error_mean': statistics.mean(exit_diff) if exit_diff else 0
            }
        }
        
        # Calibration insight
        entry_err = analysis['entry_slippage']['model_error_mean']
        if entry_err > 2:
            analysis['calibration_insight'] = f"⚠️ Model underestimates entry slippage by {entry_err:.1f}bps. Consider increasing."
        elif entry_err < -2:
            analysis['calibration_insight'] = f"✅ Model overestimates entry slippage by {abs(entry_err):.1f}bps. Conservative but OK."
        else:
            analysis['calibration_insight'] = "✅ Slippage model well calibrated."
        
        return analysis
    
    def get_regime_performance(self, 
                               trades: List[TradeMetrics] = None,
                               last_n_days: int = 30) -> Dict:
        """
        Analyze performance by regime.
        
        Returns:
            Dict with performance stats by regime
        """
        if trades is None:
            date_from = (datetime.now(UTC) - 
                        __import__('datetime').timedelta(days=last_n_days)).strftime('%Y%m%d')
            trades = self.get_trades(date_from=date_from)
        
        if not trades:
            return {'error': 'No trades found'}
        
        # Group by regime
        by_regime: Dict[str, List[TradeMetrics]] = {}
        for t in trades:
            regime = t.regime_at_entry
            if regime not in by_regime:
                by_regime[regime] = []
            by_regime[regime].append(t)
        
        analysis = {}
        for regime, regime_trades in by_regime.items():
            wins = [t for t in regime_trades if t.is_winner]
            pnl_r_values = [t.pnl_r for t in regime_trades]
            
            analysis[regime] = {
                'total_trades': len(regime_trades),
                'win_rate': len(wins) / len(regime_trades) * 100,
                'avg_pnl_r': statistics.mean(pnl_r_values) if pnl_r_values else 0,
                'total_pnl_r': sum(pnl_r_values),
                'avg_mfe_r': statistics.mean([t.mfe_r for t in regime_trades]),
                'avg_mae_r': statistics.mean([t.mae_r for t in regime_trades])
            }
        
        # Best/worst regime
        if analysis:
            best = max(analysis.keys(), key=lambda r: analysis[r]['avg_pnl_r'])
            worst = min(analysis.keys(), key=lambda r: analysis[r]['avg_pnl_r'])
            analysis['_summary'] = {
                'best_regime': best,
                'worst_regime': worst,
                'insight': f"Best: {best} ({analysis[best]['avg_pnl_r']:.2f}R avg), "
                          f"Worst: {worst} ({analysis[worst]['avg_pnl_r']:.2f}R avg)"
            }
        
        return analysis
    
    def get_score_performance(self, 
                              trades: List[TradeMetrics] = None,
                              last_n_days: int = 30) -> Dict:
        """
        Analyze performance by entry score buckets.
        
        Returns:
            Dict with performance stats by score bucket
        """
        if trades is None:
            date_from = (datetime.now(UTC) - 
                        __import__('datetime').timedelta(days=last_n_days)).strftime('%Y%m%d')
            trades = self.get_trades(date_from=date_from)
        
        if not trades:
            return {'error': 'No trades found'}
        
        # Score buckets
        buckets = {
            '4.5-5.5': (4.5, 5.5),
            '5.5-6.5': (5.5, 6.5),
            '6.5-7.5': (6.5, 7.5),
            '7.5+': (7.5, 10.0)
        }
        
        analysis = {}
        for bucket_name, (low, high) in buckets.items():
            bucket_trades = [t for t in trades if low <= t.entry_score < high]
            
            if bucket_trades:
                wins = [t for t in bucket_trades if t.is_winner]
                pnl_r_values = [t.pnl_r for t in bucket_trades]
                
                analysis[bucket_name] = {
                    'total_trades': len(bucket_trades),
                    'win_rate': len(wins) / len(bucket_trades) * 100,
                    'avg_pnl_r': statistics.mean(pnl_r_values),
                    'total_pnl_r': sum(pnl_r_values)
                }
        
        # Validate score threshold
        if analysis:
            # Check if lower scores are actually profitable
            low_score_bucket = analysis.get('4.5-5.5', {})
            if low_score_bucket and low_score_bucket.get('avg_pnl_r', 0) > 0:
                analysis['_insight'] = "✅ Probe entries (4.5-5.5 score) are profitable. Threshold validated."
            elif low_score_bucket and low_score_bucket.get('avg_pnl_r', 0) < -0.3:
                analysis['_insight'] = "⚠️ Probe entries losing money. Consider raising minimum score."
            else:
                analysis['_insight'] = "📊 Score performance within expected range."
        
        return analysis
    
    def get_today_summary(self) -> Dict:
        """Get summary of today's trades."""
        if not self._today_trades:
            return {'trades': 0, 'pnl_r': 0, 'pnl_usd': 0, 'win_rate': 0}
        
        wins = [t for t in self._today_trades if t.is_winner]
        pnl_r = sum(t.pnl_r for t in self._today_trades)
        pnl_usd = sum(t.pnl_usd for t in self._today_trades)
        
        return {
            'trades': len(self._today_trades),
            'wins': len(wins),
            'losses': len(self._today_trades) - len(wins),
            'win_rate': len(wins) / len(self._today_trades) * 100,
            'pnl_r': pnl_r,
            'pnl_usd': pnl_usd,
            'avg_mfe_r': statistics.mean([t.mfe_r for t in self._today_trades]),
            'avg_mae_r': statistics.mean([t.mae_r for t in self._today_trades])
        }


# =============================================================================
# SINGLETON
# =============================================================================

_trade_store: Optional[TradeStatisticsStore] = None


def get_trade_store(data_dir: str = None) -> TradeStatisticsStore:
    """Get or create the trade store singleton."""
    global _trade_store
    if _trade_store is None:
        _trade_store = TradeStatisticsStore(data_dir)
    return _trade_store


def reset_trade_store():
    """Reset the singleton."""
    global _trade_store
    _trade_store = None


def record_trade(metrics: TradeMetrics) -> None:
    """Convenience function to record a trade."""
    get_trade_store().record_trade(metrics)
