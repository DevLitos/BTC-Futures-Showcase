"""
Edge Validator - Implements formal edge definition and operating policies

REFACTORED: Now uses Activity Regime model instead of session-based validation.
This is suitable for 24/7 crypto markets where London/NY/Asia sessions are irrelevant.

IMPORTANT: EdgeValidator ONLY reports metrics and signals.
State management is handled EXCLUSIVELY by StrategyStateMachine.
This module does NOT change or manage strategy state.

Core principle: The strategy can only operate if:
1. No active degradation signals (< 2 detected)
2. Activity regime supports edge (continuous score >= threshold)
3. Operating drawdown < configured limit
4. Not in post-degradation observation period

The key change is replacing binary session validation with continuous edge scoring.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timedelta, UTC
import numpy as np
from src.config.trading_config import KillConditions, MTFFilters, IndicatorPeriods
from src.domain.market.regime.activity_regime import (
    ActivityRegimeAnalyzer, 
    ActivityMetrics, 
    EdgeScore, 
    EnforcementMode,
    get_analyzer
)

# Import canonical state type - SINGLE SOURCE OF TRUTH
# Note: EdgeValidator reports metrics but does NOT manage state
from src.domain.trading.state.state_types import StrategyState


@dataclass
class DegradationTradeEntry:
    """Record of a single trade for degradation analysis (local to edge validator)"""
    trade_id: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    mfe: float  # Maximum Favorable Excursion (in ATR or pips)
    mae: float  # Maximum Adverse Excursion (in ATR or pips)
    duration_hours: float
    is_winner: bool
    reached_tp1: bool
    reached_tp2: bool
    is_be_stop: bool  # Stopped at break-even


@dataclass
class DegradationMetrics:
    """Metrics indicating edge degradation"""
    mfe_mae_ratio: float = 0.0
    be_stop_percentage: float = 0.0
    non_extending_winners_pct: float = 0.0
    avg_trade_duration_hours: float = 0.0
    active_signals: int = 0  # Count of degradation signals
    
    def get_alert_level(self) -> str:
        """Returns alert level based on signal count"""
        if self.active_signals >= 3:
            return "SEVERE"
        elif self.active_signals == 2:
            return "CRITICAL"
        elif self.active_signals == 1:
            return "WARNING"
        return "HEALTHY"


@dataclass
class ContextValidity:
    """
    Edge context validation results.
    
    REFACTORED: Now based on Activity Regime model, not sessions.
    - activity_valid: Replaces session_range_valid
    - atr_valid: Uses percentile instead of fixed threshold
    - volume_valid: Uses ratio vs MA instead of session comparison
    """
    atr_valid: bool = True
    activity_valid: bool = True      # Replaces range_valid (was session-based)
    macro_event_valid: bool = True   # Kept for special events
    expansion_valid: bool = True     # Kept but uses activity windows
    volume_valid: bool = True        # Now ratio-based
    
    # NEW: Continuous edge score
    edge_score: float = 0.5
    edge_regime: str = "NORMAL"
    
    # Enforcement mode threshold (set by EdgeValidator)
    _min_edge_score: float = 0.25  # Default, overridden by mode
    
    def is_valid(self) -> bool:
        """
        Returns True if context supports edge.
        
        RETUNED (Jan 19, 2026): Less restrictive - trust structural filters.
        Only block on critical issues, not marginal scores.
        """
        # Only block if edge score is VERY low (less than 50% of threshold)
        # Marginal scores are acceptable when structural filters passed
        if self.edge_score < self._min_edge_score * 0.5:
            return False
        
        # Only macro event is a hard block - ATR/activity are soft signals
        return self.macro_event_valid
    
    def invalid_reasons(self) -> List[str]:
        """Returns list of invalidation reasons - now less aggressive"""
        reasons = []
        # Only report edge score if significantly below threshold
        if self.edge_score < self._min_edge_score * 0.5:
            reasons.append(f"Edge score too low: {self.edge_score:.2f} < {self._min_edge_score:.2f}")
        if not self.atr_valid:
            reasons.append("ATR below minimum percentile")
        if not self.activity_valid:
            reasons.append("Activity range below threshold vs MA")
        if not self.macro_event_valid:
            reasons.append("Post-macro event without follow-through")
        # Don't add expansion/volume as hard blockers
        # if not self.expansion_valid:
        #     reasons.append("Insufficient price expansion")
        # if not self.volume_valid:
        #     reasons.append("Volume below threshold vs MA")
        return reasons


class EdgeValidator:
    """
    Validates edge presence and operational readiness.
    
    REFACTORED: Uses Activity Regime model instead of session-based validation.
    This module ONLY reports metrics and signals - it does NOT manage state.
    State management is handled exclusively by StrategyStateMachine.
    
    No trade should be executed if can_operate() returns False.
    """
    
    def __init__(self, enforcement_mode: str = EnforcementMode.FORWARD):
        """
        Initialize validator with empty trade history.
        
        Args:
            enforcement_mode: One of 'backtest', 'forward', 'live'
        """
        self.trade_history: List[DegradationTradeEntry] = []
        
        # NOTE: State tracking removed - use StrategyStateMachine instead
        # These are kept for backwards compatibility but deprecated
        self._deprecated_degradation_start_date: Optional[datetime] = None
        
        # Activity Regime analyzer (ignore volume in backtest mode)
        ignore_volume = (enforcement_mode == EnforcementMode.BACKTEST)
        self.activity_analyzer = get_analyzer(ignore_volume=ignore_volume)
        self.enforcement_mode = enforcement_mode
        self.thresholds = EnforcementMode.get_thresholds(enforcement_mode)
        
        # Configuration thresholds (degradation detection)
        self.MFE_MAE_THRESHOLD = 1.5
        self.BE_STOP_THRESHOLD = 0.40  # 40% (fraction, not percentage)
        self.NON_EXTEND_THRESHOLD = 0.60  # 60% (fraction, not percentage)
        self.AVG_DURATION_THRESHOLD = 12  # hours
        self.DEGRADATION_THRESHOLD = 2  # signals to trigger alert
        self.OBSERVATION_PERIOD_DAYS = 14
        self.CONSECUTIVE_NEGATIVE_WINDOWS = 3
        
    def set_enforcement_mode(self, mode: str) -> None:
        """Change enforcement mode and update thresholds"""
        self.enforcement_mode = mode
        self.thresholds = EnforcementMode.get_thresholds(mode)
        # Update activity analyzer with appropriate volume setting
        ignore_volume = (mode == EnforcementMode.BACKTEST)
        self.activity_analyzer = get_analyzer(ignore_volume=ignore_volume)
    
    # ========================================================================
    # CONTEXT VALIDATION (REFACTORED - Activity Regime based)
    # ========================================================================
    
    def validate_context(self,
                        atr_current: float,
                        atr_history: np.ndarray,
                        highs: np.ndarray,
                        lows: np.ndarray,
                        closes: np.ndarray,
                        volumes: np.ndarray,
                        is_macro_event_day: bool = False,
                        candle_minutes: int = 1) -> ContextValidity:
        """
        Validates if edge context is present using Activity Regime model.
        
        REFACTORED: Replaces session-based validation with continuous activity analysis.
        
        Args:
            atr_current: Current ATR value
            atr_history: Historical ATR values for percentile calculation
            highs: Recent high prices array
            lows: Recent low prices array  
            closes: Recent close prices array
            volumes: Recent volume array
            is_macro_event_day: True if post-macro event without follow
            candle_minutes: Minutes per candle (1, 5, 60, etc.)
        
        Returns:
            ContextValidity object with validation results
        """
        # CRITICAL: Initialize thresholds FIRST to prevent UnboundLocalError
        # (Bug fix Jan 4, 2026 - variable was used before assignment in some code paths)
        thresholds = self.thresholds
        
        context = ContextValidity()
        
        # Use Activity Regime analyzer for continuous scoring
        metrics, edge_score = self.activity_analyzer.analyze(
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            atr_values=atr_history,
            current_atr=atr_current,
            candle_minutes=candle_minutes
        )
        
        # Store edge score in context
        context.edge_score = edge_score.value
        context.edge_regime = edge_score.regime
        
        # Set threshold based on enforcement mode
        context._min_edge_score = thresholds['min_edge_score']
        
        # Map component scores to context flags
        
        # ATR validation: percentile-based
        context.atr_valid = metrics.atr_percentile >= thresholds['min_atr_percentile']
        
        # Activity range validation: replaces session_range
        context.activity_valid = metrics.activity_range_ratio >= thresholds['min_activity_ratio']
        
        # Volume validation: ratio-based
        context.volume_valid = metrics.volume_ratio >= thresholds['min_volume_ratio']
        
        # Macro event check: kept as-is (special events)
        context.macro_event_valid = not is_macro_event_day
        
        # Expansion validation: based on momentum
        # Consider price is expanding if momentum is strong
        context.expansion_valid = metrics.momentum_strength >= 0.3
        
        return context
    
    def validate_context_legacy(self,
                        atr_current: float,
                        atr_p30: float,
                        session_range: float,
                        session_range_avg: float,
                        is_macro_event_day: bool,
                        expansion_sessions_count: int,
                        volume_trend_london_ny: float) -> ContextValidity:
        """
        DEPRECATED: Legacy session-based validation.
        Kept for backwards compatibility but should not be used.
        
        Use validate_context() instead with Activity Regime model.
        """
        context = ContextValidity()
        
        # Just pass everything in legacy mode - let Activity Regime handle it
        context.atr_valid = True
        context.activity_valid = True
        context.macro_event_valid = not is_macro_event_day
        context.expansion_valid = expansion_sessions_count >= 2
        context.volume_valid = volume_trend_london_ny > 0.8
        context.edge_score = 0.5  # Neutral score
        context.edge_regime = "NORMAL"
        
        return context
    
    # ========================================================================
    # DEGRADATION DETECTION (señales tempranas)
    # ========================================================================
    
    def detect_degradation(self,
                          window_size: int = 20) -> DegradationMetrics:
        """
        Detects early signs of edge degradation.
        
        Analysis window: last N trades (default 20)
        Signals: 2+ triggers alert and reduce position
        
        Args:
            window_size: Number of recent trades to analyze
        
        Returns:
            DegradationMetrics with signal count and details
        """
        metrics = DegradationMetrics()
        
        if len(self.trade_history) < window_size:
            return metrics  # Not enough data
        
        window = self.trade_history[-window_size:]
        
        # Signal 1: MFE/MAE ratio degradation
        mfes = np.array([t.mfe for t in window if t.mfe > 0])
        maes = np.array([t.mae for t in window if t.mae > 0])
        
        if len(mfes) > 0 and len(maes) > 0:
            metrics.mfe_mae_ratio = float(np.mean(mfes) / np.mean(maes))
            if metrics.mfe_mae_ratio < self.MFE_MAE_THRESHOLD:
                metrics.active_signals += 1
        
        # Signal 2: BE stops ratio degradation
        be_stops = sum(1 for t in window if t.is_be_stop)
        metrics.be_stop_percentage = be_stops / len(window)
        if metrics.be_stop_percentage > self.BE_STOP_THRESHOLD:
            metrics.active_signals += 1
        
        # Signal 3: Winners not extending to TP2
        winners = [t for t in window if t.is_winner and t.reached_tp1]
        if len(winners) > 0:
            non_extending = sum(1 for t in winners if not t.reached_tp2)
            metrics.non_extending_winners_pct = non_extending / len(winners)
            if metrics.non_extending_winners_pct > self.NON_EXTEND_THRESHOLD:
                metrics.active_signals += 1
        
        # Signal 4: Avg trade duration increasing without profit
        durations = [t.duration_hours for t in window]
        metrics.avg_trade_duration_hours = float(np.mean(durations))
        if metrics.avg_trade_duration_hours > self.AVG_DURATION_THRESHOLD:
            metrics.active_signals += 1
        
        return metrics
    
    # ========================================================================
    # EXPECTANCY EVALUATION
    # ========================================================================
    
    def calculate_expectancy(self, window_size: int = 50) -> float:
        """
        Calculates expectancy in recent window.
        
        Expectancy = (Win% × Avg Win) - (Loss% × Avg Loss)
        
        Args:
            window_size: Trades to analyze (minimum 30, typically 50)
        
        Returns:
            Expectancy value (positive = profitable edge)
        """
        if len(self.trade_history) < window_size:
            return 0.0  # Not enough data
        
        recent_trades = self.trade_history[-window_size:]
        
        # Calculate PnL from entry/exit prices
        def get_pnl(t: DegradationTradeEntry) -> float:
            return t.exit_price - t.entry_price
        
        wins = [t for t in recent_trades if get_pnl(t) > 0]
        losses = [t for t in recent_trades if get_pnl(t) <= 0]
        
        if not wins and not losses:
            return 0.0
        
        total = len(recent_trades)
        win_rate = len(wins) / total if total > 0 else 0.0
        loss_rate = len(losses) / total if total > 0 else 0.0
        
        avg_win = sum(get_pnl(t) for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(get_pnl(t) for t in losses) / len(losses)) if losses else 0.0
        
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
        return expectancy
    
    # ========================================================================
    # SIGNAL GENERATION (No state management - that's StrategyStateMachine's job)
    # ========================================================================
    
    # DEPRECATED STATE METHODS - Removed to enforce single source of truth
    # State management is ONLY done by StrategyStateMachine.
    # EdgeValidator only REPORTS signals/metrics.
    
    def should_enter_degradation(self) -> Tuple[bool, str]:
        """
        Check if degradation mode should be entered.
        
        NOTE: This method only REPORTS whether degradation is warranted.
        Actual state change must be done by StrategyStateMachine.
        
        Returns:
            Tuple (should_degrade: bool, reason: str)
        """
        degradation = self.detect_degradation(window_size=20)
        if degradation.active_signals >= self.DEGRADATION_THRESHOLD:
            return True, f"{degradation.active_signals} degradation signals active"
        return False, ""
    
    def should_kill_strategy(self, expectancy: float, windows_negative: int) -> Tuple[bool, str]:
        """
        Check if strategy should be killed.
        
        NOTE: This method only REPORTS whether kill is warranted.
        Actual state change must be done by StrategyStateMachine.
        
        Args:
            expectancy: Current expectancy value
            windows_negative: Count of consecutive negative windows
            
        Returns:
            Tuple (should_kill: bool, reason: str)
        """
        if windows_negative >= self.CONSECUTIVE_NEGATIVE_WINDOWS:
            return True, f"{windows_negative} consecutive negative expectancy windows"
        return False, ""
    
    def get_observation_status(self) -> Dict:
        """
        Get observation period status.
        
        NOTE: Observation tracking should be in StrategyStateMachine.
        This is kept for backwards compatibility but deprecated.
        
        Returns:
            Dict with observation period info
        """
        return {
            'in_observation': False,  # Deprecated - use StrategyStateMachine
            'elapsed_days': 0,
            'remaining_days': 0,
            'note': 'Use StrategyStateMachine for observation period tracking'
        }
    
    # ========================================================================
    # MAIN OPERATING DECISION
    # ========================================================================
    
    def can_operate(self,
                   context: ContextValidity,
                   degradation: DegradationMetrics,
                   current_drawdown: float) -> Tuple[bool, List[str]]:
        """
        Determines if strategy can operate (MAIN DECISION GATE).
        
        REFACTORED: Now uses mode-based thresholds and continuous edge scoring.
        
        NOTE: This evaluates EDGE CONDITIONS only.
        State-based restrictions (DEAD, OBSERVATION) should be checked
        via StrategyStateMachine.can_trade() separately.
        
        Operating rules (ALL must be true):
        1. No active degradation (signals < 2)
        2. Edge score above threshold (mode-dependent)
        3. Drawdown < mode limit
        
        Args:
            context: ContextValidity from validate_context()
            degradation: DegradationMetrics from detect_degradation()
            current_drawdown: Current drawdown as decimal (0.15 = 15%)
        
        Returns:
            Tuple (can_operate: bool, reasons_if_blocked: List[str])
        """
        reasons = []
        thresholds = self.thresholds
        
        # Check 1: Degradation
        if degradation.active_signals >= self.DEGRADATION_THRESHOLD:
            reasons.append(f"Degradation active: {degradation.active_signals} signals")
        
        # Check 2: Edge score (replaces binary context validity)
        min_score = thresholds['min_edge_score']
        if context.edge_score < min_score:
            reasons.append(f"Edge score {context.edge_score:.2f} < {min_score:.2f} ({self.enforcement_mode} mode)")
        
        # Check 3: Activity context (secondary validation)
        if not context.activity_valid and self.enforcement_mode != EnforcementMode.BACKTEST:
            reasons.append("Activity regime below threshold")
        
        # Check 4: Drawdown limit (mode-dependent)
        max_dd = thresholds['max_drawdown']
        if current_drawdown > max_dd:
            reasons.append(f"Drawdown {current_drawdown*100:.1f}% exceeds {max_dd*100:.1f}%")
        
        # NOTE: State checks (observation period, DEAD state) are now handled
        # by StrategyStateMachine. This method only evaluates edge conditions.
        
        return (len(reasons) == 0), reasons
    
    # ========================================================================
    # POSITION SIZING LOGIC (DEPRECATED - Use StrategyStateMachine)
    # ========================================================================
    
    def get_position_size_multiplier(self) -> float:
        """
        DEPRECATED: Use StrategyStateMachine.get_position_size_multiplier() instead.
        
        This method is kept for backwards compatibility but should not be used.
        EdgeValidator should not manage state or sizing - that's StrategyStateMachine's job.
        
        Returns:
            1.0 always (use StrategyStateMachine for actual multiplier)
        """
        import warnings
        warnings.warn(
            "EdgeValidator.get_position_size_multiplier() is deprecated. "
            "Use StrategyStateMachine.get_position_size_multiplier() instead.",
            DeprecationWarning,
            stacklevel=2
        )
        # Return neutral value - actual sizing is from StrategyStateMachine
        return 1.0
    
    # ========================================================================
    # TRADE RECORDING
    # ========================================================================
    
    def record_trade(self,
                    trade_id: int,
                    entry_time: datetime,
                    exit_time: datetime,
                    entry_price: float,
                    exit_price: float,
                    mfe: float,
                    mae: float,
                    reached_tp1: bool = False,
                    reached_tp2: bool = False,
                    is_be_stop: bool = False) -> None:
        """
        Records a completed trade for analysis.
        
        Args:
            trade_id: Unique trade identifier
            entry_time: Entry datetime
            exit_time: Exit datetime
            entry_price: Entry price
            exit_price: Exit price
            mfe: Maximum Favorable Excursion (in ATR)
            mae: Maximum Adverse Excursion (in ATR)
            reached_tp1: Whether trade reached first take profit
            reached_tp2: Whether trade reached second take profit
            is_be_stop: Whether exited at break-even
        """
        duration = (exit_time - entry_time).total_seconds() / 3600.0  # hours
        is_winner = exit_price > entry_price
        
        trade = DegradationTradeEntry(
            trade_id=trade_id,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            mfe=mfe,
            mae=mae,
            duration_hours=duration,
            is_winner=is_winner,
            reached_tp1=reached_tp1,
            reached_tp2=reached_tp2,
            is_be_stop=is_be_stop
        )
        
        self.trade_history.append(trade)
    
    def get_trade_count(self) -> int:
        """Returns total trades recorded"""
        return len(self.trade_history)
    
    # ========================================================================
    # REPORTING
    # ========================================================================
    
    def generate_health_report(self, window_size: int = 50) -> Dict:
        """
        Generates comprehensive health report.
        
        Returns:
            Dict with all key metrics for weekly reporting
        """
        context_sample = self.calculate_win_rate(window_size)
        degradation = self.detect_degradation(min(20, len(self.trade_history)))
        expectancy = self.calculate_expectancy(window_size)
        
        report = {
            "timestamp": datetime.now(UTC).isoformat(),
            "strategy_state": "UNKNOWN",  # NOTE: State tracking moved to StrategyStateMachine
            "sample_size": self.get_trade_count(),
            "position_multiplier": self.get_position_size_multiplier(),
            
            # Degradation
            "degradation_signals": degradation.active_signals,
            "degradation_alert": degradation.get_alert_level(),
            "mfe_mae_ratio": round(degradation.mfe_mae_ratio, 2),
            "be_stop_pct": round(degradation.be_stop_percentage * 100, 1),
            "non_extending_winners_pct": round(degradation.non_extending_winners_pct * 100, 1),
            "avg_trade_duration_h": round(degradation.avg_trade_duration_hours, 1),
            
            # Performance
            "expectancy_r": round(expectancy, 3),
            "win_rate_pct": round(context_sample["win_rate"] * 100, 1),
            "profit_factor": round(context_sample["profit_factor"], 2),
        }
        
        return report
    
    def calculate_win_rate(self, window_size: int = 50) -> Dict:
        """
        Calculates win rate and profit factor.
        
        Returns:
            Dict with win_rate and profit_factor
        """
        if len(self.trade_history) < window_size:
            window = self.trade_history
        else:
            window = self.trade_history[-window_size:]
        
        if len(window) == 0:
            return {"win_rate": 0.0, "profit_factor": 0.0}
        
        winners = [t for t in window if t.is_winner]
        win_rate = len(winners) / len(window)
        
        # Profit factor = gross_profit / gross_loss
        gross_profit = sum(t.exit_price - t.entry_price for t in winners if t.is_winner)
        gross_loss = abs(sum(t.exit_price - t.entry_price for t in window if not t.is_winner))
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
        
        return {
            "win_rate": win_rate,
            "profit_factor": profit_factor
        }
