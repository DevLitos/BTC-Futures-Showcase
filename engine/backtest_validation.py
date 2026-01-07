"""
Backtest Validation Framework - Sistema de Métricas Robustas

PRIORIDADES (en orden):
1. Expectancy/trade (R promedio por trade)
2. Profit Factor (gross profit / gross loss)  
3. Max DD / Return ratio (risk-adjusted return)
4. Stability (varianza entre periodos)

REGLA DE ORO: Si algo sube P1 pero destroza estabilidad, SE DESCARTA.

Criterios de aceptación para cada feature:
- P3 no cae más del 15-20%
- Profit Factor no empeora
- Drawdown no sube de forma desproporcionada

Este módulo NO toca el edge, solo MIDE y VALIDA.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, UTC
from enum import Enum
import numpy as np
import json
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# VALIDATION METRICS
# =============================================================================

@dataclass
class PeriodMetrics:
    """
    Metrics for a single backtest period.
    
    These are the ONLY metrics that matter for validation.
    """
    period_name: str = ""
    
    # Core metrics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    
    # R-based metrics (THE MOST IMPORTANT)
    total_r: float = 0.0
    expectancy_r: float = 0.0  # Average R per trade
    
    # PnL metrics
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    
    # Risk metrics
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    
    # Derived metrics (calculated)
    win_rate: float = 0.0
    profit_factor: float = 0.0
    return_pct: float = 0.0
    
    # Stability metrics
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    
    def calculate_derived(self, initial_capital: float = 10000.0):
        """Calculate derived metrics from raw data."""
        # Win rate
        if self.total_trades > 0:
            self.win_rate = self.wins / self.total_trades
            self.expectancy_r = self.total_r / self.total_trades
        
        # Profit Factor
        if self.gross_loss > 0:
            self.profit_factor = self.gross_profit / abs(self.gross_loss)
        elif self.gross_profit > 0:
            self.profit_factor = float('inf')
        else:
            self.profit_factor = 0.0
        
        # Return
        self.return_pct = (self.total_pnl / initial_capital) * 100
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'period': self.period_name,
            'trades': self.total_trades,
            'wins': self.wins,
            'win_rate': f"{self.win_rate*100:.2f}%",
            'expectancy_r': f"{self.expectancy_r:.3f}R",
            'total_pnl': f"${self.total_pnl:.2f}",
            'return_pct': f"{self.return_pct:.2f}%",
            'max_dd': f"{self.max_drawdown_pct:.2f}%",
            'profit_factor': f"{self.profit_factor:.2f}",
        }


@dataclass
class ValidationResult:
    """
    Result of cross-validation.
    
    Determines if a feature should be enabled or disabled.
    """
    feature_name: str
    is_valid: bool
    reason: str
    
    # Period results
    p1_metrics: Optional[PeriodMetrics] = None
    p2_metrics: Optional[PeriodMetrics] = None
    p3_metrics: Optional[PeriodMetrics] = None  # TARGET (14-21 days ago)
    oos_metrics: Optional[PeriodMetrics] = None  # Out of sample
    
    # Comparison to baseline
    p3_change_pct: float = 0.0  # % change in P3 return
    pf_change: float = 0.0      # Change in Profit Factor
    dd_change_pct: float = 0.0  # Change in max drawdown


@dataclass
class ValidationConfig:
    """Configuration for validation criteria."""
    
    # P3 must not fall more than this %
    MAX_P3_DEGRADATION_PCT: float = 20.0  # 20% max degradation allowed
    
    # Profit Factor must not fall more than this
    MAX_PF_DEGRADATION: float = 0.15  # 15% max PF drop
    
    # Drawdown must not increase more than this %
    MAX_DD_INCREASE_PCT: float = 30.0  # 30% max DD increase
    
    # Minimum trades per period for valid comparison
    MIN_TRADES_PER_PERIOD: int = 20
    
    # Stability: max variance ratio between periods
    MAX_RETURN_VARIANCE_RATIO: float = 3.0  # P1/P3 ratio should not exceed 3x


# =============================================================================
# PERIOD ANALYZER
# =============================================================================

class PeriodAnalyzer:
    """
    Analyzes trades for a specific period.
    """
    
    def __init__(self, period_name: str, initial_capital: float = 10000.0):
        self.period_name = period_name
        self.initial_capital = initial_capital
        self.trades: List[Dict] = []
        
    def add_trade(self, trade: Dict):
        """Add a trade to the period."""
        self.trades.append(trade)
    
    def calculate_metrics(self) -> PeriodMetrics:
        """Calculate all metrics for this period."""
        metrics = PeriodMetrics(period_name=self.period_name)
        
        if not self.trades:
            return metrics
        
        metrics.total_trades = len(self.trades)
        
        # Track equity for drawdown calculation
        equity = self.initial_capital
        peak_equity = equity
        max_drawdown = 0.0
        
        r_values = []
        
        for trade in self.trades:
            pnl = trade.get('pnl', 0.0)
            r_value = trade.get('r_realized', trade.get('r', 0.0))
            
            # Count wins/losses
            if pnl > 0:
                metrics.wins += 1
                metrics.gross_profit += pnl
            else:
                metrics.losses += 1
                metrics.gross_loss += abs(pnl)
            
            # R tracking
            metrics.total_r += r_value
            r_values.append(r_value)
            
            # PnL
            metrics.total_pnl += pnl
            
            # Equity curve
            equity += pnl
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
        
        metrics.max_drawdown_pct = max_drawdown * 100
        metrics.max_drawdown_usd = self.initial_capital * max_drawdown
        
        # Calculate derived metrics
        metrics.calculate_derived(self.initial_capital)
        
        # Calculate Sharpe-like ratio (using R values)
        if r_values and len(r_values) > 1:
            r_std = np.std(r_values)
            if r_std > 0:
                metrics.sharpe_ratio = metrics.expectancy_r / r_std
            
            # Sortino (only downside deviation)
            downside = [r for r in r_values if r < 0]
            if downside:
                downside_std = np.std(downside)
                if downside_std > 0:
                    metrics.sortino_ratio = metrics.expectancy_r / downside_std
        
        return metrics


# =============================================================================
# CROSS-VALIDATOR
# =============================================================================

class CrossValidator:
    """
    Cross-validation system for feature testing.
    
    Runs backtest for:
    - P1: Last 7 days
    - P2: 7-14 days ago
    - P3: 14-21 days ago (TARGET - must not degrade)
    - OOS: Optional additional month
    """
    
    def __init__(self, config: ValidationConfig = None):
        self.config = config or ValidationConfig()
        
        # Baseline metrics (without the feature)
        self.baseline_p1: Optional[PeriodMetrics] = None
        self.baseline_p2: Optional[PeriodMetrics] = None
        self.baseline_p3: Optional[PeriodMetrics] = None
        
        # Feature metrics (with the feature)
        self.feature_p1: Optional[PeriodMetrics] = None
        self.feature_p2: Optional[PeriodMetrics] = None
        self.feature_p3: Optional[PeriodMetrics] = None
    
    def set_baseline(self, p1: PeriodMetrics, p2: PeriodMetrics, p3: PeriodMetrics):
        """Set baseline metrics (without feature)."""
        self.baseline_p1 = p1
        self.baseline_p2 = p2
        self.baseline_p3 = p3
        
        logger.info(f"📊 BASELINE SET:")
        logger.info(f"   P1: {p1.return_pct:.2f}% return, {p1.expectancy_r:.3f}R expectancy")
        logger.info(f"   P2: {p2.return_pct:.2f}% return, {p2.expectancy_r:.3f}R expectancy")
        logger.info(f"   P3: {p3.return_pct:.2f}% return, {p3.expectancy_r:.3f}R expectancy")
    
    def validate_feature(self, 
                         feature_name: str,
                         p1: PeriodMetrics, 
                         p2: PeriodMetrics, 
                         p3: PeriodMetrics,
                         oos: Optional[PeriodMetrics] = None) -> ValidationResult:
        """
        Validate a feature against baseline.
        
        Returns ValidationResult with pass/fail and reasons.
        """
        self.feature_p1 = p1
        self.feature_p2 = p2
        self.feature_p3 = p3
        
        # Check if we have baseline
        if not self.baseline_p3:
            return ValidationResult(
                feature_name=feature_name,
                is_valid=False,
                reason="NO_BASELINE: Set baseline first",
                p1_metrics=p1,
                p2_metrics=p2,
                p3_metrics=p3
            )
        
        # Check minimum trades
        if p3.total_trades < self.config.MIN_TRADES_PER_PERIOD:
            return ValidationResult(
                feature_name=feature_name,
                is_valid=False,
                reason=f"INSUFFICIENT_TRADES: P3 has {p3.total_trades} < {self.config.MIN_TRADES_PER_PERIOD}",
                p1_metrics=p1,
                p2_metrics=p2,
                p3_metrics=p3
            )
        
        # Calculate changes
        p3_change = self._calculate_change(self.baseline_p3.return_pct, p3.return_pct)
        pf_change = self._calculate_change(self.baseline_p3.profit_factor, p3.profit_factor)
        dd_change = self._calculate_change(self.baseline_p3.max_drawdown_pct, p3.max_drawdown_pct)
        
        # Check criteria
        failures = []
        
        # 1. P3 degradation check
        if p3_change < -self.config.MAX_P3_DEGRADATION_PCT:
            failures.append(f"P3_DEGRADED: {p3_change:.1f}% (max -{self.config.MAX_P3_DEGRADATION_PCT}%)")
        
        # 2. Profit Factor check (only fail if it gets worse)
        if pf_change < -self.config.MAX_PF_DEGRADATION * 100:
            failures.append(f"PF_DEGRADED: {pf_change:.1f}% (max -{self.config.MAX_PF_DEGRADATION*100}%)")
        
        # 3. Drawdown check (only fail if DD increases significantly)
        if dd_change > self.config.MAX_DD_INCREASE_PCT:
            failures.append(f"DD_INCREASED: +{dd_change:.1f}% (max +{self.config.MAX_DD_INCREASE_PCT}%)")
        
        # 4. Stability check: P1 should not be wildly different from P3
        if p1.return_pct > 0 and p3.return_pct > 0:
            variance_ratio = max(p1.return_pct, p3.return_pct) / min(p1.return_pct, p3.return_pct)
            if variance_ratio > self.config.MAX_RETURN_VARIANCE_RATIO:
                failures.append(f"UNSTABLE: P1/P3 ratio {variance_ratio:.1f}x (max {self.config.MAX_RETURN_VARIANCE_RATIO}x)")
        
        # Determine validity
        is_valid = len(failures) == 0
        
        if is_valid:
            reason = f"PASSED: P3 {p3_change:+.1f}%, PF {pf_change:+.1f}%, DD {dd_change:+.1f}%"
        else:
            reason = " | ".join(failures)
        
        result = ValidationResult(
            feature_name=feature_name,
            is_valid=is_valid,
            reason=reason,
            p1_metrics=p1,
            p2_metrics=p2,
            p3_metrics=p3,
            oos_metrics=oos,
            p3_change_pct=p3_change,
            pf_change=pf_change,
            dd_change_pct=dd_change
        )
        
        # Log result
        status = "✅ VALID" if is_valid else "❌ INVALID"
        logger.info(f"\n{'='*60}")
        logger.info(f"VALIDATION: {feature_name}")
        logger.info(f"{'='*60}")
        logger.info(f"Status: {status}")
        logger.info(f"Reason: {reason}")
        logger.info(f"")
        logger.info(f"Baseline P3: {self.baseline_p3.return_pct:.2f}% | "
                   f"PF: {self.baseline_p3.profit_factor:.2f} | "
                   f"DD: {self.baseline_p3.max_drawdown_pct:.2f}%")
        logger.info(f"Feature P3:  {p3.return_pct:.2f}% | "
                   f"PF: {p3.profit_factor:.2f} | "
                   f"DD: {p3.max_drawdown_pct:.2f}%")
        logger.info(f"Change:      {p3_change:+.1f}% | {pf_change:+.1f}% | {dd_change:+.1f}%")
        logger.info(f"{'='*60}\n")
        
        return result
    
    def _calculate_change(self, baseline: float, feature: float) -> float:
        """Calculate percentage change from baseline to feature."""
        if baseline == 0:
            return 0.0 if feature == 0 else 100.0
        return ((feature - baseline) / abs(baseline)) * 100


# =============================================================================
# FEATURE FLAGS SYSTEM
# =============================================================================

class FeatureFlag(Enum):
    """Available feature flags."""
    CAPITAL_WEIGHTING = "capital_weighting"
    SESSION_RISK = "session_risk"
    SL_DYNAMIC = "sl_dynamic"
    RUNNER_OPTIMIZATION = "runner_optimization"
    REGIME_FILTER = "regime_filter"
    SLIPPAGE_SIM = "slippage_sim"


@dataclass
class FeatureFlagState:
    """State of a single feature flag."""
    flag: FeatureFlag
    enabled: bool = False
    validation_status: str = "NOT_TESTED"
    last_validation: Optional[datetime] = None
    validation_result: Optional[ValidationResult] = None


class FeatureFlagManager:
    """
    Manages feature flags for gradual rollout.
    
    Features must pass validation before being enabled.
    """
    
    def __init__(self, state_file: str = "config/feature_flags.json"):
        self.state_file = Path(state_file)
        self.flags: Dict[FeatureFlag, FeatureFlagState] = {}
        
        # Initialize all flags as disabled
        for flag in FeatureFlag:
            self.flags[flag] = FeatureFlagState(flag=flag)
        
        # Load saved state if exists
        self._load_state()
    
    def is_enabled(self, flag: FeatureFlag) -> bool:
        """Check if a feature is enabled."""
        return self.flags.get(flag, FeatureFlagState(flag=flag)).enabled
    
    def enable(self, flag: FeatureFlag, validation_result: ValidationResult = None):
        """Enable a feature flag."""
        if flag in self.flags:
            self.flags[flag].enabled = True
            self.flags[flag].last_validation = datetime.now(UTC)
            if validation_result:
                self.flags[flag].validation_status = "PASSED" if validation_result.is_valid else "FAILED"
                self.flags[flag].validation_result = validation_result
            self._save_state()
            logger.info(f"✅ ENABLED: {flag.value}")
    
    def disable(self, flag: FeatureFlag, reason: str = ""):
        """Disable a feature flag."""
        if flag in self.flags:
            self.flags[flag].enabled = False
            self.flags[flag].validation_status = f"DISABLED: {reason}"
            self._save_state()
            logger.info(f"❌ DISABLED: {flag.value} - {reason}")
    
    def validate_and_toggle(self, 
                            flag: FeatureFlag, 
                            validation_result: ValidationResult):
        """Validate a feature and toggle accordingly."""
        if validation_result.is_valid:
            self.enable(flag, validation_result)
        else:
            self.disable(flag, validation_result.reason)
    
    def get_status_report(self) -> str:
        """Get a formatted status report of all flags."""
        lines = ["", "=" * 50, "FEATURE FLAGS STATUS", "=" * 50]
        
        for flag, state in self.flags.items():
            status = "🟢 ON" if state.enabled else "🔴 OFF"
            lines.append(f"  {flag.value:25} {status:8} ({state.validation_status})")
        
        lines.append("=" * 50)
        return "\n".join(lines)
    
    def _save_state(self):
        """Save state to JSON file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        state = {}
        for flag, flag_state in self.flags.items():
            state[flag.value] = {
                'enabled': flag_state.enabled,
                'validation_status': flag_state.validation_status,
                'last_validation': flag_state.last_validation.isoformat() if flag_state.last_validation else None
            }
        
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def _load_state(self):
        """Load state from JSON file."""
        if not self.state_file.exists():
            return
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            
            for flag_name, flag_data in state.items():
                try:
                    flag = FeatureFlag(flag_name)
                    if flag in self.flags:
                        self.flags[flag].enabled = flag_data.get('enabled', False)
                        self.flags[flag].validation_status = flag_data.get('validation_status', 'LOADED')
                except ValueError:
                    pass
        except Exception as e:
            logger.warning(f"Could not load feature flags: {e}")


# =============================================================================
# STABILITY ANALYZER
# =============================================================================

class StabilityAnalyzer:
    """
    Analyzes stability across periods.
    
    A feature is stable if performance is consistent across P1, P2, P3.
    """
    
    @staticmethod
    def calculate_stability_score(p1: PeriodMetrics, 
                                  p2: PeriodMetrics, 
                                  p3: PeriodMetrics) -> Tuple[float, str]:
        """
        Calculate stability score (0-100).
        
        Higher = more stable = better.
        
        Returns:
            Tuple of (score, description)
        """
        # Collect returns
        returns = [p1.return_pct, p2.return_pct, p3.return_pct]
        
        # Handle edge cases
        if all(r == 0 for r in returns):
            return 50.0, "NO_DATA"
        
        # Calculate coefficient of variation (lower = more stable)
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        
        if mean_return == 0:
            cv = float('inf')
        else:
            cv = abs(std_return / mean_return)
        
        # Convert CV to score (0-100)
        # CV of 0 = 100 (perfect stability)
        # CV of 1 = 50 (moderate stability)
        # CV of 2+ = 0 (unstable)
        score = max(0, min(100, 100 - (cv * 50)))
        
        # Determine description
        if score >= 80:
            desc = "EXCELLENT: Very stable across periods"
        elif score >= 60:
            desc = "GOOD: Reasonably stable"
        elif score >= 40:
            desc = "MODERATE: Some variance between periods"
        else:
            desc = "POOR: High variance (overfitting risk)"
        
        return score, desc
    
    @staticmethod
    def detect_overfitting(p1: PeriodMetrics, 
                          p2: PeriodMetrics, 
                          p3: PeriodMetrics) -> Tuple[bool, str]:
        """
        Detect if results show signs of overfitting.
        
        Signs:
        - P1 >> P3 (much better on recent data)
        - Expectancy drops significantly from P1 to P3
        
        Returns:
            Tuple of (is_overfitting, reason)
        """
        # Check if P1 is much better than P3
        if p3.return_pct > 0 and p1.return_pct > 0:
            ratio = p1.return_pct / p3.return_pct
            if ratio > 2.5:
                return True, f"P1/P3 ratio too high: {ratio:.2f}x"
        
        # Check expectancy degradation
        if p3.expectancy_r > 0 and p1.expectancy_r > 0:
            exp_ratio = p1.expectancy_r / p3.expectancy_r
            if exp_ratio > 2.0:
                return True, f"Expectancy drops {exp_ratio:.2f}x from P1 to P3"
        
        # Check if P1 is positive but P3 is negative
        if p1.return_pct > 0 and p3.return_pct < 0:
            return True, "P1 positive but P3 negative - unstable edge"
        
        return False, "No overfitting detected"


# =============================================================================
# SLIPPAGE SIMULATOR
# =============================================================================

@dataclass
class SlippageConfig:
    """Configuration for slippage simulation."""
    
    # Base slippage in basis points (0.01% = 1 bp)
    BASE_SLIPPAGE_BPS: float = 2.0  # 2 bps = 0.02%
    
    # Additional slippage for market orders
    MARKET_ORDER_SLIPPAGE_BPS: float = 3.0  # 3 bps extra
    
    # Slippage multiplier during high volatility
    HIGH_VOL_MULTIPLIER: float = 2.0
    HIGH_VOL_ATR_RATIO: float = 1.5  # ATR > 1.5x baseline = high vol
    
    # Random variation (adds realism)
    RANDOM_VARIATION_PCT: float = 30.0  # ±30% random variation


class SlippageSimulator:
    """
    Simulates realistic slippage for backtest.
    
    More conservative backtests = more reliable forward performance.
    """
    
    def __init__(self, config: SlippageConfig = None):
        self.config = config or SlippageConfig()
    
    def apply_slippage(self,
                       price: float,
                       direction: str,
                       is_entry: bool,
                       is_market_order: bool = False,
                       atr_ratio: float = 1.0) -> float:
        """
        Apply slippage to a price.
        
        Args:
            price: Original price
            direction: 'LONG' or 'SHORT'
            is_entry: True for entry, False for exit
            is_market_order: True if market order (more slippage)
            atr_ratio: Current ATR vs baseline (for volatility adjustment)
            
        Returns:
            Adjusted price with slippage
        """
        # Base slippage
        slippage_bps = self.config.BASE_SLIPPAGE_BPS
        
        # Add market order slippage
        if is_market_order:
            slippage_bps += self.config.MARKET_ORDER_SLIPPAGE_BPS
        
        # Adjust for volatility
        if atr_ratio > self.config.HIGH_VOL_ATR_RATIO:
            slippage_bps *= self.config.HIGH_VOL_MULTIPLIER
        
        # Add random variation
        variation = 1.0 + (np.random.uniform(-1, 1) * self.config.RANDOM_VARIATION_PCT / 100)
        slippage_bps *= variation
        
        # Convert to price adjustment
        slippage_pct = slippage_bps / 10000  # bps to decimal
        slippage_amount = price * slippage_pct
        
        # Direction determines if slippage is adverse
        # Entry LONG: price goes UP (pay more)
        # Entry SHORT: price goes DOWN (get less)
        # Exit LONG: price goes DOWN (get less)
        # Exit SHORT: price goes UP (pay more)
        
        if direction.upper() == 'LONG':
            if is_entry:
                adjusted_price = price + slippage_amount  # Pay more
            else:
                adjusted_price = price - slippage_amount  # Get less
        else:  # SHORT
            if is_entry:
                adjusted_price = price - slippage_amount  # Get less
            else:
                adjusted_price = price + slippage_amount  # Pay more
        
        return adjusted_price


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_validator: Optional[CrossValidator] = None
_flag_manager: Optional[FeatureFlagManager] = None


def get_validator() -> CrossValidator:
    """Get global CrossValidator instance."""
    global _validator
    if _validator is None:
        _validator = CrossValidator()
    return _validator


def get_feature_flags() -> FeatureFlagManager:
    """Get global FeatureFlagManager instance."""
    global _flag_manager
    if _flag_manager is None:
        _flag_manager = FeatureFlagManager()
    return _flag_manager


def is_feature_enabled(flag: FeatureFlag) -> bool:
    """Quick check if a feature is enabled."""
    return get_feature_flags().is_enabled(flag)


def print_validation_summary(result: ValidationResult):
    """Print a formatted validation summary."""
    print("\n" + "=" * 60)
    print(f"VALIDATION RESULT: {result.feature_name}")
    print("=" * 60)
    
    status = "✅ PASSED" if result.is_valid else "❌ FAILED"
    print(f"Status: {status}")
    print(f"Reason: {result.reason}")
    
    if result.p1_metrics and result.p3_metrics:
        print("\nPeriod Comparison:")
        print(f"  P1 (recent):  {result.p1_metrics.return_pct:.2f}% return, "
              f"{result.p1_metrics.expectancy_r:.3f}R, PF {result.p1_metrics.profit_factor:.2f}")
        print(f"  P3 (target):  {result.p3_metrics.return_pct:.2f}% return, "
              f"{result.p3_metrics.expectancy_r:.3f}R, PF {result.p3_metrics.profit_factor:.2f}")
        
        print(f"\nChanges from baseline:")
        print(f"  Return: {result.p3_change_pct:+.1f}%")
        print(f"  Profit Factor: {result.pf_change:+.1f}%")
        print(f"  Max Drawdown: {result.dd_change_pct:+.1f}%")
    
    print("=" * 60 + "\n")
