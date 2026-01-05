"""
Adaptive Window Manager - Dynamic evaluation windows based on market conditions

CRITICAL PROBLEM:
All your windows are FIXED:
- 20 trades
- 50 trades
- 14 days

But markets don't respect fixed windows.

ISSUES:
- Fast markets: React too late (20 trades = 3 hours)
- Slow markets: React too early (20 trades = 3 weeks, insufficient sample)
- Volatility regime changes mid-window (mixed data)

PHILOSOPHY:
- Windows should adapt to market speed
- Minimum volatility elapsed (not just time/trades)
- Account for regime stability
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC
import numpy as np


class WindowType(Enum):
    """Types of evaluation windows"""
    DEGRADATION = "degradation_detection"
    EXPECTANCY = "expectancy_calculation"
    RECOVERY = "recovery_validation"
    REGIME = "regime_classification"


@dataclass
class WindowCriteria:
    """Criteria for window validity"""
    min_trades: int
    max_trades: int
    min_days: int
    max_days: int
    min_volatility_events: int  # Minimum ATR cycles or significant moves
    min_regime_stability: float  # 0-1, regime must be stable
    

@dataclass
class AdaptiveWindow:
    """An adaptive evaluation window"""
    window_type: WindowType
    start_time: datetime
    end_time: Optional[datetime]
    trades_count: int
    days_elapsed: float
    volatility_events_count: int
    regime_changes_count: int
    is_valid: bool
    validity_reason: str


class AdaptiveWindowManager:
    """
    Manages adaptive evaluation windows based on market conditions.
    
    CONCEPT:
    - Don't evaluate until window is statistically meaningful
    - Window size adapts to market speed
    - Volatility-normalized windows (not just trade count)
    - Regime-aware (don't evaluate across regime changes)
    """
    
    def __init__(self):
        """Initialize window manager"""
        
        # Window criteria for different purposes
        self.criteria = {
            WindowType.DEGRADATION: WindowCriteria(
                min_trades=15,
                max_trades=30,
                min_days=2,
                max_days=10,
                min_volatility_events=3,  # 3 significant ATR cycles
                min_regime_stability=0.70
            ),
            WindowType.EXPECTANCY: WindowCriteria(
                min_trades=30,
                max_trades=100,
                min_days=5,
                max_days=30,
                min_volatility_events=5,
                min_regime_stability=0.60
            ),
            WindowType.RECOVERY: WindowCriteria(
                min_trades=10,
                max_trades=25,
                min_days=3,
                max_days=14,
                min_volatility_events=2,
                min_regime_stability=0.70
            ),
            WindowType.REGIME: WindowCriteria(
                min_trades=5,
                max_trades=20,
                min_days=1,
                max_days=5,
                min_volatility_events=2,
                min_regime_stability=0.50
            )
        }
        
        # Active windows
        self.active_windows: Dict[WindowType, AdaptiveWindow] = {}
        
        # History
        self.window_history: List[AdaptiveWindow] = []
        
        # Volatility tracking
        self.atr_history: List[Tuple[datetime, float]] = []
        self.regime_history: List[Tuple[datetime, str]] = []
        
    def start_window(self, window_type: WindowType) -> AdaptiveWindow:
        """
        Start a new adaptive window.
        
        Args:
            window_type: Type of window to create
            
        Returns:
            New AdaptiveWindow instance
        """
        window = AdaptiveWindow(
            window_type=window_type,
            start_time=datetime.now(UTC),
            end_time=None,
            trades_count=0,
            days_elapsed=0,
            volatility_events_count=0,
            regime_changes_count=0,
            is_valid=False,
            validity_reason="window_started"
        )
        
        self.active_windows[window_type] = window
        return window
    
    def update_window(self,
                     window_type: WindowType,
                     trade_added: bool = False,
                     atr_value: Optional[float] = None,
                     current_regime: Optional[str] = None) -> AdaptiveWindow:
        """
        Update an active window with new data.
        
        Args:
            window_type: Which window to update
            trade_added: Whether a new trade was added
            atr_value: Current ATR value (for volatility tracking)
            current_regime: Current market regime
            
        Returns:
            Updated window
        """
        if window_type not in self.active_windows:
            raise ValueError(f"No active window of type {window_type}")
        
        window = self.active_windows[window_type]
        
        # Update trade count
        if trade_added:
            window.trades_count += 1
        
        # Update elapsed time
        window.days_elapsed = (datetime.now(UTC) - window.start_time).total_seconds() / 86400
        
        # Update volatility events
        if atr_value is not None:
            self.atr_history.append((datetime.now(UTC), atr_value))
            window.volatility_events_count = self._count_volatility_events(window.start_time)
        
        # Update regime changes
        if current_regime is not None:
            if not self.regime_history or self.regime_history[-1][1] != current_regime:
                self.regime_history.append((datetime.now(UTC), current_regime))
            window.regime_changes_count = self._count_regime_changes(window.start_time)
        
        # Check validity
        window.is_valid, window.validity_reason = self._check_window_validity(window)
        
        return window
    
    def _count_volatility_events(self, since: datetime) -> int:
        """
        Count significant volatility events since timestamp.
        
        A volatility event = significant ATR change (>15%)
        """
        recent_atr = [
            (ts, val) for ts, val in self.atr_history
            if ts >= since
        ]
        
        if len(recent_atr) < 2:
            return 0
        
        events = 0
        for i in range(1, len(recent_atr)):
            prev_atr = recent_atr[i-1][1]
            curr_atr = recent_atr[i][1]
            
            if prev_atr > 0:
                change = abs(curr_atr - prev_atr) / prev_atr
                if change > 0.15:  # 15% change = event
                    events += 1
        
        return events
    
    def _count_regime_changes(self, since: datetime) -> int:
        """Count regime changes since timestamp"""
        recent_regimes = [
            (ts, regime) for ts, regime in self.regime_history
            if ts >= since
        ]
        
        # Count transitions
        if len(recent_regimes) <= 1:
            return 0
        
        return len(recent_regimes) - 1  # N records = N-1 transitions
    
    def _check_window_validity(self, window: AdaptiveWindow) -> Tuple[bool, str]:
        """
        Check if window meets validity criteria.
        
        Returns:
            Tuple of (is_valid, reason)
        """
        criteria = self.criteria[window.window_type]
        
        # Check minimum trades
        if window.trades_count < criteria.min_trades:
            return False, f"Insufficient trades: {window.trades_count}/{criteria.min_trades}"
        
        # Check minimum days
        if window.days_elapsed < criteria.min_days:
            return False, f"Insufficient time: {window.days_elapsed:.1f}/{criteria.min_days} days"
        
        # Check minimum volatility events
        if window.volatility_events_count < criteria.min_volatility_events:
            return False, f"Insufficient volatility cycles: {window.volatility_events_count}/{criteria.min_volatility_events}"
        
        # Check regime stability
        regime_stability = self._calculate_regime_stability(window)
        if regime_stability < criteria.min_regime_stability:
            return False, f"Regime unstable: {regime_stability:.2f}/{criteria.min_regime_stability}"
        
        # Check maximum constraints
        if window.trades_count > criteria.max_trades:
            return False, f"Window too large: {window.trades_count}/{criteria.max_trades} trades"
        
        if window.days_elapsed > criteria.max_days:
            return False, f"Window too old: {window.days_elapsed:.1f}/{criteria.max_days} days"
        
        return True, "Window valid for evaluation"
    
    def _calculate_regime_stability(self, window: AdaptiveWindow) -> float:
        """
        Calculate regime stability (0-1).
        
        Stability = 1 - (regime_changes / max_expected_changes)
        High stability = few regime changes
        """
        window_days = max(window.days_elapsed, 0.1)
        
        # Expect max 1 regime change per 3 days in stable market
        max_expected = window_days / 3
        
        if max_expected == 0:
            return 1.0
        
        stability = 1.0 - min(window.regime_changes_count / max_expected, 1.0)
        
        return max(stability, 0.0)
    
    def can_evaluate(self, window_type: WindowType) -> Tuple[bool, str]:
        """
        Check if window is ready for evaluation.
        
        Args:
            window_type: Which window to check
            
        Returns:
            Tuple of (can_evaluate, reason)
        """
        if window_type not in self.active_windows:
            return False, "No active window"
        
        window = self.active_windows[window_type]
        
        if not window.is_valid:
            return False, window.validity_reason
        
        return True, "Window ready for evaluation"
    
    def close_window(self, window_type: WindowType) -> AdaptiveWindow:
        """
        Close an active window and move to history.
        
        Args:
            window_type: Which window to close
            
        Returns:
            Closed window
        """
        if window_type not in self.active_windows:
            raise ValueError(f"No active window of type {window_type}")
        
        window = self.active_windows[window_type]
        window.end_time = datetime.now(UTC)
        
        # Archive
        self.window_history.append(window)
        if len(self.window_history) > 100:
            self.window_history = self.window_history[-100:]
        
        # Remove from active
        del self.active_windows[window_type]
        
        return window
    
    def get_adaptive_sample_size(self,
                                window_type: WindowType,
                                current_volatility: float,
                                baseline_volatility: float) -> int:
        """
        Calculate adaptive sample size based on current volatility.
        
        In high volatility: Fewer trades needed (data rich)
        In low volatility: More trades needed (data sparse)
        
        Args:
            window_type: Type of window
            current_volatility: Current ATR or volatility measure
            baseline_volatility: Historical baseline
            
        Returns:
            Recommended sample size
        """
        criteria = self.criteria[window_type]
        base_size = criteria.min_trades
        
        if baseline_volatility == 0:
            return base_size
        
        # Calculate volatility ratio
        vol_ratio = current_volatility / baseline_volatility
        
        # Adjust sample size inversely to volatility
        # High vol (2x) → 0.7x trades needed
        # Low vol (0.5x) → 1.4x trades needed
        adjustment = 1.0 / np.sqrt(vol_ratio)
        
        # Clamp adjustment
        adjustment = np.clip(adjustment, 0.7, 1.5)
        
        adaptive_size = int(base_size * adjustment)
        
        # Ensure within bounds
        adaptive_size = np.clip(adaptive_size, criteria.min_trades, criteria.max_trades)
        
        return adaptive_size
    
    def get_adaptive_timeframe(self,
                              window_type: WindowType,
                              current_trade_frequency: float,
                              baseline_frequency: float) -> int:
        """
        Calculate adaptive timeframe based on trade frequency.
        
        Args:
            window_type: Type of window
            current_trade_frequency: Current trades per day
            baseline_frequency: Historical trades per day
            
        Returns:
            Recommended days for window
        """
        criteria = self.criteria[window_type]
        base_days = criteria.min_days
        
        if baseline_frequency == 0 or current_trade_frequency == 0:
            return base_days
        
        # Calculate frequency ratio
        freq_ratio = current_trade_frequency / baseline_frequency
        
        # Adjust timeframe inversely to frequency
        # High freq (2x) → 0.7x days needed
        # Low freq (0.5x) → 1.5x days needed
        adjustment = 1.0 / np.sqrt(freq_ratio)
        
        # Clamp adjustment
        adjustment = np.clip(adjustment, 0.6, 2.0)
        
        adaptive_days = int(base_days * adjustment)
        
        # Ensure within bounds
        adaptive_days = np.clip(adaptive_days, criteria.min_days, criteria.max_days)
        
        return adaptive_days
    
    def requires_window_reset(self,
                             window_type: WindowType,
                             regime_changed: bool,
                             major_volatility_shift: bool) -> Tuple[bool, str]:
        """
        Check if window should be reset due to market condition changes.
        
        Args:
            window_type: Which window to check
            regime_changed: Whether market regime changed
            major_volatility_shift: Whether volatility shifted significantly
            
        Returns:
            Tuple of (should_reset, reason)
        """
        if window_type not in self.active_windows:
            return False, "No active window"
        
        window = self.active_windows[window_type]
        criteria = self.criteria[window_type]
        
        # Reset if regime changed and window requires stability
        if regime_changed and criteria.min_regime_stability > 0.60:
            return True, "Regime changed - window invalidated"
        
        # Reset if major volatility shift
        if major_volatility_shift:
            return True, "Major volatility shift - window invalidated"
        
        # Reset if window has too many regime changes
        if window.regime_changes_count > 3:
            return True, "Excessive regime instability"
        
        return False, "Window still valid"
    
    def get_window_summary(self) -> Dict:
        """Get summary of all active windows"""
        summary = {
            'active_windows': {},
            'recent_history': []
        }
        
        # Active windows
        for wtype, window in self.active_windows.items():
            can_eval, reason = self.can_evaluate(wtype)
            summary['active_windows'][wtype.value] = {
                'trades': window.trades_count,
                'days_elapsed': round(window.days_elapsed, 1),
                'volatility_events': window.volatility_events_count,
                'regime_changes': window.regime_changes_count,
                'is_valid': window.is_valid,
                'can_evaluate': can_eval,
                'status': reason
            }
        
        # Recent history
        recent = self.window_history[-5:] if self.window_history else []
        for window in recent:
            summary['recent_history'].append({
                'type': window.window_type.value,
                'trades': window.trades_count,
                'days': round(window.days_elapsed, 1),
                'was_valid': window.is_valid
            })
        
        return summary
    
    def cleanup_old_data(self, days_to_keep: int = 30) -> None:
        """Clean up old ATR and regime history"""
        cutoff = datetime.now(UTC) - timedelta(days=days_to_keep)
        
        self.atr_history = [
            (ts, val) for ts, val in self.atr_history
            if ts >= cutoff
        ]
        
        self.regime_history = [
            (ts, regime) for ts, regime in self.regime_history
            if ts >= cutoff
        ]
