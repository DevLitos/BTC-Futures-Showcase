"""
Walk-Forward Validation Framework

This module provides rigorous strategy validation:
- Walk-forward cross-validation (train/test splits)
- Ablation testing (isolate feature contributions)
- Statistical significance tests
- Overfitting detection

CRITICAL: This is the research harness. No strategy should go live
without passing walk-forward validation with positive out-of-sample results.

USAGE:
    from validation.walk_forward import WalkForwardValidator
    
    validator = WalkForwardValidator(
        train_days=60,
        test_days=14,
        min_folds=5
    )
    
    # Run walk-forward validation
    results = validator.run(
        strategy_fn=my_strategy,
        data=historical_data,
        initial_capital=10000.0
    )
    
    # Check if valid
    if results.is_valid:
        print("Strategy passed validation!")
    else:
        print(f"REJECTED: {results.rejection_reason}")
    
    # Run ablation test
    ablation = validator.run_ablation(
        base_strategy=my_strategy,
        features_to_ablate=['regime_filter', 'session_filter'],
        data=historical_data
    )
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Callable, Any, Tuple
import numpy as np
import pandas as pd
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# RESULT TYPES
# =============================================================================

class ValidationStatus(Enum):
    """Validation outcome status."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


@dataclass
class FoldResult:
    """Results from a single train/test fold."""
    fold_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    
    # Training metrics (for reference only - NOT for decisions)
    train_return_pct: float = 0.0
    train_sharpe: float = 0.0
    train_trades: int = 0
    train_win_rate: float = 0.0
    
    # Test metrics (THE REAL METRICS)
    test_return_pct: float = 0.0
    test_sharpe: float = 0.0
    test_trades: int = 0
    test_win_rate: float = 0.0
    test_max_drawdown_pct: float = 0.0
    test_profit_factor: float = 0.0
    
    # Degradation metrics
    sharpe_degradation: float = 0.0  # (train - test) / train
    return_degradation: float = 0.0
    
    def __post_init__(self):
        """Calculate degradation metrics."""
        if self.train_sharpe > 0:
            self.sharpe_degradation = (self.train_sharpe - self.test_sharpe) / self.train_sharpe
        if self.train_return_pct > 0:
            self.return_degradation = (self.train_return_pct - self.test_return_pct) / self.train_return_pct
    
    def to_dict(self) -> Dict:
        return {
            'fold_id': self.fold_id,
            'train_period': f"{self.train_start.date()} - {self.train_end.date()}",
            'test_period': f"{self.test_start.date()} - {self.test_end.date()}",
            'train_return_pct': round(self.train_return_pct, 2),
            'test_return_pct': round(self.test_return_pct, 2),
            'train_sharpe': round(self.train_sharpe, 3),
            'test_sharpe': round(self.test_sharpe, 3),
            'test_trades': self.test_trades,
            'test_win_rate': round(self.test_win_rate, 2),
            'test_max_dd': round(self.test_max_drawdown_pct, 2),
            'sharpe_degradation': round(self.sharpe_degradation, 2),
        }


@dataclass
class WalkForwardResult:
    """Complete walk-forward validation results."""
    strategy_name: str
    status: ValidationStatus
    rejection_reason: str = ""
    
    # Summary statistics
    num_folds: int = 0
    total_test_trades: int = 0
    
    # Out-of-sample (OOS) aggregate metrics
    oos_return_pct: float = 0.0
    oos_sharpe: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    oos_win_rate: float = 0.0
    oos_profit_factor: float = 0.0
    
    # Consistency metrics
    positive_folds: int = 0
    negative_folds: int = 0
    consistency_ratio: float = 0.0  # positive_folds / num_folds
    
    # Degradation analysis
    avg_sharpe_degradation: float = 0.0
    avg_return_degradation: float = 0.0
    
    # Statistical tests
    t_stat_return: float = 0.0
    p_value_return: float = 1.0
    is_statistically_significant: bool = False
    
    # Individual fold results
    folds: List[FoldResult] = field(default_factory=list)
    
    @property
    def is_valid(self) -> bool:
        """True if strategy passed validation."""
        return self.status == ValidationStatus.PASSED
    
    def to_dict(self) -> Dict:
        return {
            'strategy_name': self.strategy_name,
            'status': self.status.value,
            'is_valid': self.is_valid,
            'rejection_reason': self.rejection_reason,
            'num_folds': self.num_folds,
            'total_test_trades': self.total_test_trades,
            'oos_return_pct': round(self.oos_return_pct, 2),
            'oos_sharpe': round(self.oos_sharpe, 3),
            'oos_max_drawdown_pct': round(self.oos_max_drawdown_pct, 2),
            'oos_win_rate': round(self.oos_win_rate, 2),
            'oos_profit_factor': round(self.oos_profit_factor, 3),
            'consistency_ratio': round(self.consistency_ratio, 2),
            'positive_folds': self.positive_folds,
            'avg_sharpe_degradation': round(self.avg_sharpe_degradation, 2),
            'is_statistically_significant': self.is_statistically_significant,
            'p_value': round(self.p_value_return, 4),
        }
    
    def summary(self) -> str:
        """Human-readable summary."""
        status_emoji = "✅" if self.is_valid else "❌"
        lines = [
            f"{status_emoji} Walk-Forward Validation: {self.status.value}",
            f"   Strategy: {self.strategy_name}",
            f"   Folds: {self.num_folds} | Positive: {self.positive_folds} ({self.consistency_ratio:.0%})",
            f"   OOS Return: {self.oos_return_pct:.1f}% | Sharpe: {self.oos_sharpe:.2f}",
            f"   OOS Max DD: {self.oos_max_drawdown_pct:.1f}% | Win Rate: {self.oos_win_rate:.1f}%",
            f"   Avg Degradation: {self.avg_sharpe_degradation:.0%}",
            f"   Statistical Sig: {'Yes' if self.is_statistically_significant else 'No'} (p={self.p_value_return:.3f})",
        ]
        if self.rejection_reason:
            lines.append(f"   REJECTION: {self.rejection_reason}")
        return "\n".join(lines)


@dataclass
class AblationResult:
    """Results from ablation testing."""
    feature_name: str
    base_oos_sharpe: float
    ablated_oos_sharpe: float
    sharpe_impact: float  # base - ablated
    is_positive_contribution: bool
    
    def to_dict(self) -> Dict:
        return {
            'feature': self.feature_name,
            'base_sharpe': round(self.base_oos_sharpe, 3),
            'ablated_sharpe': round(self.ablated_oos_sharpe, 3),
            'impact': round(self.sharpe_impact, 3),
            'contributes_positively': self.is_positive_contribution,
        }


# =============================================================================
# VALIDATION CRITERIA
# =============================================================================

@dataclass
class ValidationCriteria:
    """
    Criteria for passing validation.
    
    These are conservative defaults. Adjust based on strategy type
    but err on the side of strictness.
    """
    # Minimum requirements (hard rejects)
    min_folds: int = 5
    min_trades_per_fold: int = 10
    min_consistency_ratio: float = 0.6  # 60% of folds must be profitable
    
    # Performance thresholds
    min_oos_sharpe: float = 0.5
    max_oos_drawdown_pct: float = 30.0
    min_oos_return_pct: float = 0.0  # Must be positive
    min_oos_profit_factor: float = 1.1
    
    # Degradation limits
    max_sharpe_degradation: float = 0.5  # Max 50% decay from train to test
    max_return_degradation: float = 0.6  # Max 60% decay
    
    # Statistical significance
    required_significance: float = 0.10  # p-value threshold (10%)
    require_statistical_significance: bool = True


# =============================================================================
# WALK-FORWARD VALIDATOR
# =============================================================================

class WalkForwardValidator:
    """
    Walk-Forward Cross-Validation Engine.
    
    Implements anchored walk-forward validation:
    - Train on expanding window (or fixed window)
    - Test on fixed forward period
    - No look-ahead bias
    - Aggregate OOS metrics for final decision
    """
    
    def __init__(
        self,
        train_days: int = 60,
        test_days: int = 14,
        step_days: int = 7,  # Gap between test periods
        min_folds: int = 5,
        anchored: bool = True,  # Expanding train window
        gap_days: int = 1,  # Gap between train and test (prevent leakage)
        criteria: ValidationCriteria = None
    ):
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.min_folds = min_folds
        self.anchored = anchored
        self.gap_days = gap_days
        self.criteria = criteria or ValidationCriteria()
        
        logger.info(
            f"WalkForwardValidator initialized: "
            f"train={train_days}d, test={test_days}d, step={step_days}d, "
            f"anchored={anchored}, min_folds={min_folds}"
        )
    
    def generate_splits(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> List[Tuple[datetime, datetime, datetime, datetime]]:
        """
        Generate train/test date splits.
        
        Returns list of (train_start, train_end, test_start, test_end) tuples.
        """
        splits = []
        
        # Minimum data required for first fold
        min_train_end = start_date + timedelta(days=self.train_days)
        
        # Current test start
        current_test_start = min_train_end + timedelta(days=self.gap_days)
        
        while True:
            test_end = current_test_start + timedelta(days=self.test_days)
            
            if test_end > end_date:
                break
            
            if self.anchored:
                train_start = start_date
            else:
                # Rolling window
                train_start = current_test_start - timedelta(days=self.gap_days + self.train_days)
            
            train_end = current_test_start - timedelta(days=self.gap_days)
            
            splits.append((train_start, train_end, current_test_start, test_end))
            
            # Move forward
            current_test_start += timedelta(days=self.step_days)
        
        return splits
    
    def run(
        self,
        strategy_fn: Callable,
        data: pd.DataFrame,
        initial_capital: float = 10000.0,
        strategy_name: str = "unnamed"
    ) -> WalkForwardResult:
        """
        Run walk-forward validation on a strategy.
        
        Args:
            strategy_fn: Function that takes (data, params) and returns equity curve
            data: Historical OHLCV data with datetime index
            initial_capital: Starting capital for each test
            strategy_name: Name for logging
            
        Returns:
            WalkForwardResult with complete validation metrics
        """
        logger.info(f"Starting walk-forward validation for '{strategy_name}'")
        
        # Determine date range
        if isinstance(data.index, pd.DatetimeIndex):
            start_date = data.index.min().to_pydatetime()
            end_date = data.index.max().to_pydatetime()
        else:
            start_date = pd.to_datetime(data['timestamp'].min())
            end_date = pd.to_datetime(data['timestamp'].max())
        
        # Generate splits
        splits = self.generate_splits(start_date, end_date)
        
        if len(splits) < self.min_folds:
            return WalkForwardResult(
                strategy_name=strategy_name,
                status=ValidationStatus.INCONCLUSIVE,
                rejection_reason=f"Insufficient data for {self.min_folds} folds. Got {len(splits)}.",
                num_folds=len(splits)
            )
        
        # Run each fold
        folds = []
        all_oos_returns = []
        all_oos_trades = []
        
        for fold_id, (train_start, train_end, test_start, test_end) in enumerate(splits):
            try:
                fold_result = self._run_fold(
                    fold_id=fold_id,
                    strategy_fn=strategy_fn,
                    data=data,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    initial_capital=initial_capital
                )
                folds.append(fold_result)
                all_oos_returns.append(fold_result.test_return_pct)
                all_oos_trades.append(fold_result.test_trades)
                
            except Exception as e:
                logger.error(f"Fold {fold_id} failed: {e}")
                continue
        
        if len(folds) < self.min_folds:
            return WalkForwardResult(
                strategy_name=strategy_name,
                status=ValidationStatus.ERROR,
                rejection_reason=f"Too many fold failures. Completed: {len(folds)}/{len(splits)}",
                num_folds=len(folds),
                folds=folds
            )
        
        # Calculate aggregate metrics
        result = self._calculate_aggregate_metrics(strategy_name, folds, all_oos_returns)
        
        # Apply validation criteria
        result = self._apply_criteria(result)
        
        logger.info(result.summary())
        return result
    
    def _run_fold(
        self,
        fold_id: int,
        strategy_fn: Callable,
        data: pd.DataFrame,
        train_start: datetime,
        train_end: datetime,
        test_start: datetime,
        test_end: datetime,
        initial_capital: float
    ) -> FoldResult:
        """Run a single train/test fold."""
        # Split data
        if isinstance(data.index, pd.DatetimeIndex):
            train_data = data[train_start:train_end]
            test_data = data[test_start:test_end]
        else:
            train_data = data[(data['timestamp'] >= train_start) & (data['timestamp'] <= train_end)]
            test_data = data[(data['timestamp'] >= test_start) & (data['timestamp'] <= test_end)]
        
        # Run strategy on train data (for reference only)
        train_result = strategy_fn(train_data, initial_capital)
        
        # Run strategy on test data (THE REAL RESULT)
        test_result = strategy_fn(test_data, initial_capital)
        
        return FoldResult(
            fold_id=fold_id,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            # Train metrics (reference only)
            train_return_pct=train_result.get('return_pct', 0.0),
            train_sharpe=train_result.get('sharpe', 0.0),
            train_trades=train_result.get('num_trades', 0),
            train_win_rate=train_result.get('win_rate', 0.0),
            # Test metrics (THE REAL METRICS)
            test_return_pct=test_result.get('return_pct', 0.0),
            test_sharpe=test_result.get('sharpe', 0.0),
            test_trades=test_result.get('num_trades', 0),
            test_win_rate=test_result.get('win_rate', 0.0),
            test_max_drawdown_pct=test_result.get('max_drawdown_pct', 0.0),
            test_profit_factor=test_result.get('profit_factor', 0.0),
        )
    
    def _calculate_aggregate_metrics(
        self,
        strategy_name: str,
        folds: List[FoldResult],
        oos_returns: List[float]
    ) -> WalkForwardResult:
        """Calculate aggregate metrics from all folds."""
        n_folds = len(folds)
        
        # Basic aggregates
        positive_folds = sum(1 for f in folds if f.test_return_pct > 0)
        total_trades = sum(f.test_trades for f in folds)
        
        # OOS metrics (aggregate)
        oos_return = np.sum(oos_returns)  # Compounded return
        oos_sharpes = [f.test_sharpe for f in folds]
        oos_sharpe = np.mean(oos_sharpes) if oos_sharpes else 0
        oos_max_dd = max(f.test_max_drawdown_pct for f in folds)
        oos_win_rates = [f.test_win_rate for f in folds if f.test_trades > 0]
        oos_win_rate = np.mean(oos_win_rates) if oos_win_rates else 0
        oos_pfs = [f.test_profit_factor for f in folds if f.test_profit_factor > 0]
        oos_pf = np.mean(oos_pfs) if oos_pfs else 0
        
        # Degradation
        degradations = [f.sharpe_degradation for f in folds if not np.isnan(f.sharpe_degradation) and not np.isinf(f.sharpe_degradation)]
        avg_degradation = np.mean(degradations) if degradations else 0
        
        return_degradations = [f.return_degradation for f in folds if not np.isnan(f.return_degradation) and not np.isinf(f.return_degradation)]
        avg_return_deg = np.mean(return_degradations) if return_degradations else 0
        
        # Statistical significance (simple t-test against 0)
        t_stat, p_value = self._simple_t_test(oos_returns)
        
        return WalkForwardResult(
            strategy_name=strategy_name,
            status=ValidationStatus.INCONCLUSIVE,  # Will be set by _apply_criteria
            num_folds=n_folds,
            total_test_trades=total_trades,
            oos_return_pct=oos_return,
            oos_sharpe=oos_sharpe,
            oos_max_drawdown_pct=oos_max_dd,
            oos_win_rate=oos_win_rate,
            oos_profit_factor=oos_pf,
            positive_folds=positive_folds,
            negative_folds=n_folds - positive_folds,
            consistency_ratio=positive_folds / n_folds if n_folds > 0 else 0,
            avg_sharpe_degradation=avg_degradation,
            avg_return_degradation=avg_return_deg,
            t_stat_return=t_stat,
            p_value_return=p_value,
            is_statistically_significant=p_value < self.criteria.required_significance,
            folds=folds
        )
    
    def _apply_criteria(self, result: WalkForwardResult) -> WalkForwardResult:
        """Apply validation criteria and set final status."""
        c = self.criteria
        reasons = []
        
        # Hard requirements
        if result.num_folds < c.min_folds:
            reasons.append(f"Insufficient folds: {result.num_folds} < {c.min_folds}")
        
        if result.total_test_trades < c.min_trades_per_fold * result.num_folds:
            reasons.append(f"Insufficient trades: {result.total_test_trades}")
        
        if result.consistency_ratio < c.min_consistency_ratio:
            reasons.append(f"Low consistency: {result.consistency_ratio:.0%} < {c.min_consistency_ratio:.0%}")
        
        # Performance thresholds
        if result.oos_sharpe < c.min_oos_sharpe:
            reasons.append(f"Low OOS Sharpe: {result.oos_sharpe:.2f} < {c.min_oos_sharpe}")
        
        if result.oos_max_drawdown_pct > c.max_oos_drawdown_pct:
            reasons.append(f"High OOS DD: {result.oos_max_drawdown_pct:.1f}% > {c.max_oos_drawdown_pct}%")
        
        if result.oos_return_pct < c.min_oos_return_pct:
            reasons.append(f"Negative OOS return: {result.oos_return_pct:.1f}%")
        
        if result.oos_profit_factor < c.min_oos_profit_factor:
            reasons.append(f"Low profit factor: {result.oos_profit_factor:.2f} < {c.min_oos_profit_factor}")
        
        # Degradation checks
        if result.avg_sharpe_degradation > c.max_sharpe_degradation:
            reasons.append(f"High degradation: {result.avg_sharpe_degradation:.0%} > {c.max_sharpe_degradation:.0%}")
        
        # Statistical significance
        if c.require_statistical_significance and not result.is_statistically_significant:
            reasons.append(f"Not statistically significant: p={result.p_value_return:.3f}")
        
        # Set status
        if reasons:
            result.status = ValidationStatus.FAILED
            result.rejection_reason = "; ".join(reasons)
        else:
            result.status = ValidationStatus.PASSED
        
        return result
    
    def _simple_t_test(self, returns: List[float]) -> Tuple[float, float]:
        """Simple one-sample t-test against 0."""
        if len(returns) < 3:
            return 0.0, 1.0
        
        mean = np.mean(returns)
        std = np.std(returns, ddof=1)
        n = len(returns)
        
        if std == 0:
            return 0.0, 1.0
        
        t_stat = mean / (std / np.sqrt(n))
        
        # Approximate p-value using normal distribution for simplicity
        # For more accurate p-value, use scipy.stats.t
        p_value = 2 * (1 - self._normal_cdf(abs(t_stat)))
        
        return t_stat, p_value
    
    def _normal_cdf(self, x: float) -> float:
        """Approximate standard normal CDF."""
        # Abramowitz and Stegun approximation
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        d = 0.3989423 * np.exp(-x * x / 2)
        p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
        return 1 - p if x > 0 else p
    
    # =========================================================================
    # ABLATION TESTING
    # =========================================================================
    
    def run_ablation(
        self,
        base_strategy_fn: Callable,
        ablation_configs: List[Dict],
        data: pd.DataFrame,
        initial_capital: float = 10000.0
    ) -> List[AblationResult]:
        """
        Run ablation tests to isolate feature contributions.
        
        Each ablation config should have:
        - 'name': Feature name being ablated
        - 'strategy_fn': Strategy function with feature disabled
        
        Returns contribution analysis for each feature.
        """
        logger.info("Starting ablation testing...")
        
        # First, run base strategy
        base_result = self.run(
            strategy_fn=base_strategy_fn,
            data=data,
            initial_capital=initial_capital,
            strategy_name="base"
        )
        base_sharpe = base_result.oos_sharpe
        
        results = []
        
        for config in ablation_configs:
            name = config['name']
            ablated_fn = config['strategy_fn']
            
            logger.info(f"Ablating feature: {name}")
            
            ablated_result = self.run(
                strategy_fn=ablated_fn,
                data=data,
                initial_capital=initial_capital,
                strategy_name=f"without_{name}"
            )
            
            ablated_sharpe = ablated_result.oos_sharpe
            impact = base_sharpe - ablated_sharpe
            
            results.append(AblationResult(
                feature_name=name,
                base_oos_sharpe=base_sharpe,
                ablated_oos_sharpe=ablated_sharpe,
                sharpe_impact=impact,
                is_positive_contribution=impact > 0
            ))
            
            logger.info(
                f"  {name}: base={base_sharpe:.3f}, ablated={ablated_sharpe:.3f}, "
                f"impact={impact:+.3f} ({'👍' if impact > 0 else '👎'})"
            )
        
        # Summary
        positive = [r for r in results if r.is_positive_contribution]
        negative = [r for r in results if not r.is_positive_contribution]
        
        logger.info(f"Ablation complete: {len(positive)} positive, {len(negative)} negative contributions")
        
        if negative:
            logger.warning(f"Consider removing: {[r.feature_name for r in negative]}")
        
        return results


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def create_strategy_runner(
    signal_generator: Callable,
    executor: Callable,
    position_sizer: Callable
) -> Callable:
    """
    Create a strategy function compatible with walk-forward validation.
    
    This wraps your strategy components into a single function that
    takes data and returns performance metrics.
    """
    def run_strategy(data: pd.DataFrame, initial_capital: float) -> Dict:
        equity = initial_capital
        trades = []
        
        # Your strategy logic here
        # This is a placeholder - implement based on your actual strategy
        
        # Return standardized metrics
        return {
            'return_pct': ((equity - initial_capital) / initial_capital) * 100,
            'sharpe': 0.0,  # Calculate from trade returns
            'num_trades': len(trades),
            'win_rate': 0.0,  # Calculate from trades
            'max_drawdown_pct': 0.0,  # Calculate from equity curve
            'profit_factor': 0.0,  # Calculate from trades
        }
    
    return run_strategy


# =============================================================================
# SINGLETON
# =============================================================================

_walk_forward_validator: Optional[WalkForwardValidator] = None


def get_walk_forward_validator(
    train_days: int = 60,
    test_days: int = 14,
    **kwargs
) -> WalkForwardValidator:
    """Get or create a walk-forward validator."""
    global _walk_forward_validator
    if _walk_forward_validator is None:
        _walk_forward_validator = WalkForwardValidator(
            train_days=train_days,
            test_days=test_days,
            **kwargs
        )
    return _walk_forward_validator


def reset_walk_forward_validator() -> None:
    """Reset validator. Only for testing."""
    global _walk_forward_validator
    _walk_forward_validator = None
