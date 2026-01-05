"""
Metrics Calculator - Centralized calculation of trading metrics.

This module is the SINGLE SOURCE OF TRUTH for expectancy, win rate,
and other performance metrics to avoid duplicate calculations.

All components should import from here rather than calculating metrics themselves.
"""

from dataclasses import dataclass
from typing import List, Tuple, Optional, Protocol
from datetime import datetime
import numpy as np


@dataclass
class TradeForMetrics:
    """Minimal trade data needed for metrics calculation."""
    is_winner: bool
    pnl_r: float = 0.0  # PnL in R units (risk multiples)
    entry_price: float = 0.0
    exit_price: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class PerformanceMetrics:
    """Calculated performance metrics."""
    expectancy_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    sample_size: int = 0
    is_statistically_significant: bool = False


class MetricsCalculator:
    """
    Centralized metrics calculator.
    
    This is the ONLY place expectancy and related metrics should be calculated.
    All other components should use this calculator.
    """
    
    MIN_TRADES_FOR_SIGNIFICANCE = 30
    
    def __init__(self):
        """Initialize calculator with empty cache."""
        self._cache = {}
        self._cache_valid = False
    
    def calculate_expectancy(self,
                            trades: List[TradeForMetrics],
                            window_size: int = 50) -> float:
        """
        Calculate expectancy from trade list.
        
        Formula: E = (Win% × Avg Win) - (Loss% × Avg Loss)
        
        Args:
            trades: List of trades with outcome data
            window_size: Number of recent trades to use
            
        Returns:
            Expectancy in R units (positive = profitable edge)
        """
        if len(trades) < self.MIN_TRADES_FOR_SIGNIFICANCE:
            return 0.0  # Not enough data
        
        # Use most recent trades
        recent = trades[-window_size:] if len(trades) > window_size else trades
        
        winners = [t for t in recent if t.is_winner]
        losers = [t for t in recent if not t.is_winner]
        
        if len(recent) == 0:
            return 0.0
        
        win_rate = len(winners) / len(recent)
        loss_rate = len(losers) / len(recent)
        
        # Calculate average win/loss in R units
        if winners:
            avg_win = np.mean([t.pnl_r for t in winners]) if any(t.pnl_r != 0 for t in winners) else 1.0
        else:
            avg_win = 0.0
        
        if losers:
            avg_loss = abs(np.mean([t.pnl_r for t in losers])) if any(t.pnl_r != 0 for t in losers) else 1.0
        else:
            avg_loss = 0.0
        
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
        return float(round(expectancy, 4))
    
    def calculate_win_rate(self, trades: List[TradeForMetrics]) -> float:
        """
        Calculate win rate.
        
        Args:
            trades: List of trades
            
        Returns:
            Win rate as fraction (0.0 to 1.0)
        """
        if len(trades) == 0:
            return 0.0
        
        winners = sum(1 for t in trades if t.is_winner)
        return winners / len(trades)
    
    def calculate_profit_factor(self, trades: List[TradeForMetrics]) -> float:
        """
        Calculate profit factor.
        
        Formula: Gross Profit / Gross Loss
        
        Args:
            trades: List of trades
            
        Returns:
            Profit factor (>1.0 is profitable)
        """
        if len(trades) == 0:
            return 0.0
        
        gross_profit = sum(t.pnl_r for t in trades if t.is_winner and t.pnl_r > 0)
        gross_loss = abs(sum(t.pnl_r for t in trades if not t.is_winner and t.pnl_r < 0))
        
        # Fallback to price-based calculation if pnl_r not set
        if gross_profit == 0 and gross_loss == 0:
            for t in trades:
                pnl = t.exit_price - t.entry_price
                if pnl > 0:
                    gross_profit += pnl
                else:
                    gross_loss += abs(pnl)
        
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        
        return round(gross_profit / gross_loss, 2)
    
    def calculate_all(self,
                     trades: List[TradeForMetrics],
                     window_size: int = 50) -> PerformanceMetrics:
        """
        Calculate all performance metrics.
        
        Args:
            trades: List of trades
            window_size: Window for expectancy calculation
            
        Returns:
            PerformanceMetrics with all calculations
        """
        recent = trades[-window_size:] if len(trades) > window_size else trades
        
        winners = [t for t in recent if t.is_winner]
        losers = [t for t in recent if not t.is_winner]
        
        # Win rate
        win_rate = len(winners) / len(recent) if recent else 0.0
        
        # Avg win/loss
        avg_win = np.mean([t.pnl_r for t in winners]) if winners else 0.0
        avg_loss = abs(np.mean([t.pnl_r for t in losers])) if losers else 0.0
        
        # Use 1R defaults if pnl_r not provided
        if avg_win == 0.0 and winners:
            avg_win = 1.0
        if avg_loss == 0.0 and losers:
            avg_loss = 1.0
        
        return PerformanceMetrics(
            expectancy_r=self.calculate_expectancy(trades, window_size),
            win_rate=win_rate,
            profit_factor=self.calculate_profit_factor(recent),
            avg_win_r=avg_win,
            avg_loss_r=avg_loss,
            sample_size=len(recent),
            is_statistically_significant=len(trades) >= self.MIN_TRADES_FOR_SIGNIFICANCE
        )
    
    def is_edge_present(self,
                       trades: List[TradeForMetrics],
                       min_expectancy: float = 0.0,
                       min_win_rate: float = 0.0,
                       min_profit_factor: float = 1.0) -> Tuple[bool, str]:
        """
        Determine if trading edge is present.
        
        Args:
            trades: List of trades
            min_expectancy: Minimum required expectancy
            min_win_rate: Minimum required win rate
            min_profit_factor: Minimum required profit factor
            
        Returns:
            Tuple (edge_present: bool, reason: str)
        """
        if len(trades) < self.MIN_TRADES_FOR_SIGNIFICANCE:
            return True, "Insufficient sample size - allowing trades"
        
        metrics = self.calculate_all(trades)
        
        if metrics.expectancy_r < min_expectancy:
            return False, f"Expectancy {metrics.expectancy_r:.3f}R < {min_expectancy:.3f}R"
        
        if metrics.win_rate < min_win_rate:
            return False, f"Win rate {metrics.win_rate*100:.1f}% < {min_win_rate*100:.1f}%"
        
        if metrics.profit_factor < min_profit_factor:
            return False, f"Profit factor {metrics.profit_factor:.2f} < {min_profit_factor:.2f}"
        
        return True, "Edge conditions met"


# Global singleton instance (for backwards compatibility)
# Prefer explicit instantiation for new code
_calculator: Optional[MetricsCalculator] = None

def get_metrics_calculator(instance: Optional[MetricsCalculator] = None) -> MetricsCalculator:
    """
    Get or create MetricsCalculator instance.
    
    Args:
        instance: Optional explicit instance to use (DI pattern)
        
    Returns:
        MetricsCalculator instance
    """
    global _calculator
    
    # DI: si se pasa instancia explícita, usarla
    if instance is not None:
        return instance
    
    # Singleton fallback
    if _calculator is None:
        _calculator = MetricsCalculator()
    return _calculator


def reset_metrics_calculator():
    """Reset singleton (for testing)"""
    global _calculator
    _calculator = None


def calculate_expectancy(trades: List, window_size: int = 50) -> float:
    """
    Convenience function to calculate expectancy.
    
    This is the CANONICAL function for expectancy calculation.
    All code should use this instead of implementing their own.
    
    Args:
        trades: List of trade records (must have is_winner attribute)
        window_size: Recent trades to analyze
        
    Returns:
        Expectancy in R units
    """
    # Convert to TradeForMetrics if needed
    if trades and not isinstance(trades[0], TradeForMetrics):
        converted = []
        for t in trades:
            converted.append(TradeForMetrics(
                is_winner=getattr(t, 'is_winner', t.exit_price > t.entry_price if hasattr(t, 'exit_price') else False),
                pnl_r=getattr(t, 'pnl_r', 0.0),
                entry_price=getattr(t, 'entry_price', 0.0),
                exit_price=getattr(t, 'exit_price', 0.0),
                mfe=getattr(t, 'mfe', 0.0),
                mae=getattr(t, 'mae', 0.0)
            ))
        trades = converted
    
    return get_metrics_calculator().calculate_expectancy(trades, window_size)
