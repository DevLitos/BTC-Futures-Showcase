"""
Edge Enforcement Middleware - Integration layer for execution.py

REFACTORED: Now uses Activity Regime model instead of session-based validation.

This module shows how to integrate EdgeValidator and ActivityRegimeAnalyzer
into the execution engine to enforce the Edge Definition & Operations Policy.

SIZING PRIORITY (important):
    1. StrategyStateMachine controls state-based sizing (1.0, 0.5, 0.0)
    2. EdgeScore provides edge-based scaling (0.0, 0.5, 0.75, 1.0)
    3. Final multiplier = state_multiplier * edge_multiplier

Usage:
    from src.domain.trading.edge import EdgeEnforcement
    from src.domain.market.regime.activity_regime import EnforcementMode
    
    # For backtesting (relaxed thresholds)
    enforcement = EdgeEnforcement(mode=EnforcementMode.BACKTEST)
    
    # For live trading (strict thresholds)
    enforcement = EdgeEnforcement(mode=EnforcementMode.LIVE)
    
    can_trade, blockers, edge_score = enforcement.validate_pre_trade(
        highs=price_data['high'],
        lows=price_data['low'],
        closes=price_data['close'],
        volumes=price_data['volume'],
        atr_current=atr_value,
        atr_history=atr_array,
        drawdown=current_dd
    )
    
    if not can_trade:
        logger.warning(f"Trade blocked: {blockers}")
        return
    
    # Get combined position size multiplier (state + edge)
    position_mult = enforcement.get_combined_size_multiplier(edge_score)
    
    # Proceed with order placement
"""

from typing import Tuple, List, Optional
from datetime import datetime, UTC
import logging
import numpy as np

from src.domain.trading.edge.edge_validator import EdgeValidator, ContextValidity
from src.infrastructure.monitoring.degradation_monitor import DegradationMonitor
from src.domain.trading.strategy_state_machine import StrategyStateMachine
from src.domain.trading.state.state_types import StrategyState, compute_final_position_multiplier
from src.shared.time_provider import get_time_provider, TimeProvider
from src.domain.market.regime.activity_regime import (
    ActivityRegimeAnalyzer, 
    EdgeScore, 
    EnforcementMode,
    get_analyzer
)


class EdgeEnforcement:
    """
    Integration point for edge validation in execution pipeline.
    
    REFACTORED: Uses Activity Regime model for 24/7 crypto markets.
    
    This class orchestrates:
    1. Activity regime analysis (replaces session validation)
    2. Continuous edge scoring (replaces binary validation)
    3. Degradation signal detection
    4. Automatic state transitions
    5. Pre-trade decision enforcement
    """
    
    def __init__(self, mode: str = EnforcementMode.FORWARD):
        """
        Initialize enforcement system.
        
        Args:
            mode: Enforcement mode - 'backtest', 'forward', or 'live'
        """
        self.mode = mode
        self.thresholds = EnforcementMode.get_thresholds(mode)
        
        # Initialize time provider with appropriate mode
        time_mode = TimeProvider.MODE_BACKTEST if mode == EnforcementMode.BACKTEST else TimeProvider.MODE_LIVE
        self.time_provider = get_time_provider(time_mode)
        
        # In backtest mode, ignore volume (Kraken data has V=0)
        ignore_volume = (mode == EnforcementMode.BACKTEST)
        
        # Initialize components with mode
        self.edge_validator = EdgeValidator(enforcement_mode=mode)
        self.activity_analyzer = get_analyzer(ignore_volume=ignore_volume)
        self.degradation_monitor = DegradationMonitor(window_size=20)
        self.state_machine = StrategyStateMachine()
        
        self.logger = logging.getLogger(__name__)
        
        # Trade counter - use time provider for consistent timestamps
        self.trades_this_session = 0
        self.session_start = self.time_provider.now()
        
        # Last edge score for reference
        self.last_edge_score: Optional[EdgeScore] = None
    
    def set_mode(self, mode: str) -> None:
        """Change enforcement mode"""
        self.mode = mode
        self.thresholds = EnforcementMode.get_thresholds(mode)
        self.edge_validator.set_enforcement_mode(mode)
        
        # Update time provider mode
        time_mode = TimeProvider.MODE_BACKTEST if mode == EnforcementMode.BACKTEST else TimeProvider.MODE_LIVE
        self.time_provider.set_mode(time_mode)
        
        # Update activity analyzer if needed
        ignore_volume = (mode == EnforcementMode.BACKTEST)
        self.activity_analyzer = get_analyzer(ignore_volume=ignore_volume)
        self.logger.info(f"[EDGE] Enforcement mode changed to: {mode}")
    
    def set_market_time(self, market_time: datetime) -> None:
        """
        Set current market time (for backtest mode).
        
        In backtest, call this with each candle's timestamp to keep
        time synchronized with historical data.
        
        Args:
            market_time: Current candle timestamp
        """
        self.time_provider.set_market_time(market_time)
    
    # ========================================================================
    # PRE-TRADE VALIDATION (REFACTORED)
    # ========================================================================
    
    def validate_pre_trade(self,
                          highs: np.ndarray,
                          lows: np.ndarray,
                          closes: np.ndarray,
                          volumes: np.ndarray,
                          atr_current: float,
                          atr_history: np.ndarray,
                          current_drawdown: float,
                          candle_minutes: int = 1) -> Tuple[bool, List[str], EdgeScore]:
        """
        Main pre-trade validation gate using Activity Regime model.
        
        REFACTORED: Replaces session-based validation with continuous edge scoring.
        
        Args:
            highs: Recent high prices array
            lows: Recent low prices array
            closes: Recent close prices array
            volumes: Recent volume array
            atr_current: Current ATR value
            atr_history: Historical ATR values
            current_drawdown: Current drawdown as decimal (0.18 = 18%)
            candle_minutes: Minutes per candle
        
        Returns:
            Tuple (can_trade: bool, block_reasons: List[str], edge_score: EdgeScore)
        """
        block_reasons = []
        
        # 1. Calculate Activity Regime and Edge Score
        metrics, edge_score = self.activity_analyzer.analyze(
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            atr_values=atr_history,
            current_atr=atr_current,
            candle_minutes=candle_minutes
        )
        
        self.last_edge_score = edge_score
        
        # 2. Get context validation from EdgeValidator
        context = self.edge_validator.validate_context(
            atr_current=atr_current,
            atr_history=atr_history,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            candle_minutes=candle_minutes
        )
        
        # 3. Get degradation status
        degradation = self.degradation_monitor.get_summary_report()
        active_signals = degradation['active_signals']
        
        # Calculate expectancy for state transitions
        expectancy = self.edge_validator.calculate_expectancy(window_size=50)
        
        # 4. Evaluate state transitions
        self.state_machine.evaluate_state_transitions(
            degradation_signals=active_signals,
            context_valid=edge_score.can_trade(self.thresholds['min_edge_score']),
            expectancy=expectancy,
            current_drawdown=current_drawdown,
            recent_metrics=degradation['metrics'],
            current_time=self.time_provider.now()
        )
        
        # 5. Check all operating conditions
        # RETUNED (Jan 19, 2026): Less aggressive blocking - trust the structural filters
        
        # Strategy state (skip in backtest - drawdown accumulates unrealistically)
        if self.mode != EnforcementMode.BACKTEST:
            if self.state_machine.current_state == StrategyState.DEAD:
                block_reasons.append("Strategy is dead (archived)")
        
        # Edge score (primary validation) - but only block if VERY low
        # If HTF/MTF/LTF filters passed, we have a setup worth considering
        min_score = self.thresholds['min_edge_score']
        if edge_score.value < min_score:
            # Only add as blocking reason if significantly below threshold
            # Small misses are acceptable when filters passed
            if edge_score.value < min_score * 0.5:  # Only block if < 50% of threshold
                block_reasons.append(f"Edge score too low: {edge_score.value:.2f} < {min_score:.2f}")
            else:
                # Log warning but don't block - filters already validated the setup
                self.logger.info(f"[EDGE] Edge score marginal: {edge_score.value:.2f} (threshold {min_score:.2f}) - allowing trade")
        
        # Degradation signals - only block on severe degradation (3+)
        # 2 signals is a warning, not a hard block
        if active_signals >= 3:
            block_reasons.append(f"Severe degradation: {active_signals} signals detected")
        elif active_signals >= 2:
            self.logger.warning(f"[EDGE] Degradation warning: {active_signals} signals - allowing trade with caution")
        
        # Drawdown limit (skip in backtest - time doesn't flow, drawdown never resets)
        if self.mode != EnforcementMode.BACKTEST:
            max_dd = self.thresholds['max_drawdown']
            if current_drawdown > max_dd:
                block_reasons.append(f"Drawdown {current_drawdown*100:.1f}% > {max_dd*100:.1f}% limit")
        
        # Observation period - still enforce this
        if self.state_machine.current_state == StrategyState.OBSERVATION:
            remaining = self.state_machine.is_observation_period_remaining(current_time=self.time_provider.now())
            block_reasons.append(f"In observation period ({remaining.days} days remaining)")
        
        can_trade = (
            len(block_reasons) == 0 and
            self.state_machine.can_trade()
        )
        
        if block_reasons:
            self.logger.warning(f"[EDGE] Trade blocked: {block_reasons}")
        else:
            self.logger.info(f"[EDGE] Trade allowed - Edge score: {edge_score.value:.2f} ({edge_score.regime})")
        
        return can_trade, block_reasons, edge_score
    
    def validate_pre_trade_simple(self,
                                 context: ContextValidity,
                                 current_drawdown: float) -> Tuple[bool, List[str]]:
        """
        Simplified pre-trade validation using pre-computed context.
        
        For backwards compatibility with existing code.
        
        Args:
            context: Pre-computed ContextValidity
            current_drawdown: Current drawdown
        
        Returns:
            Tuple (can_trade: bool, block_reasons: List[str])
        """
        block_reasons = []
        
        # Get degradation status
        degradation = self.degradation_monitor.get_summary_report()
        active_signals = degradation['active_signals']
        
        # Calculate expectancy for state transitions
        expectancy = self.edge_validator.calculate_expectancy(window_size=50)
        
        # Evaluate state transitions
        self.state_machine.evaluate_state_transitions(
            degradation_signals=active_signals,
            context_valid=context.is_valid(),
            expectancy=expectancy,
            current_drawdown=current_drawdown,
            recent_metrics=degradation['metrics'],
            current_time=self.time_provider.now()
        )
        
        # Check conditions (skip some in backtest mode)
        if self.mode != EnforcementMode.BACKTEST:
            if self.state_machine.current_state == StrategyState.DEAD:
                block_reasons.append("Strategy is dead (archived)")
        
        if not context.is_valid():
            block_reasons.extend(context.invalid_reasons())
        
        if active_signals >= 2:
            block_reasons.append(f"{active_signals} degradation signals detected")
        
        # Skip drawdown check in backtest (time doesn't reset daily)
        if self.mode != EnforcementMode.BACKTEST:
            max_dd = self.thresholds['max_drawdown']
            if current_drawdown > max_dd:
                block_reasons.append(f"Drawdown {current_drawdown*100:.1f}% > {max_dd*100:.1f}% limit")
        
        if self.mode != EnforcementMode.BACKTEST:
            if self.state_machine.current_state == StrategyState.OBSERVATION:
                remaining = self.state_machine.is_observation_period_remaining()
                block_reasons.append(f"In observation period ({remaining.days} days remaining)")
        
        can_trade = (
            len(block_reasons) == 0 and
            self.state_machine.can_trade()
        )
        
        if block_reasons:
            self.logger.warning(f"[EDGE] Trade blocked: {block_reasons}")
        
        return can_trade, block_reasons
    
    # ========================================================================
    # TRADE RECORDING & MONITORING
    # ========================================================================
    
    def record_completed_trade(self,
                              trade_id: int,
                              entry_price: float,
                              exit_price: float,
                              entry_time: datetime,
                              exit_time: datetime,
                              mfe: float,
                              mae: float,
                              reached_tp1: bool = False,
                              reached_tp2: bool = False,
                              is_be_stop: bool = False) -> None:
        """
        Records a completed trade and updates monitoring.
        
        Called when a trade exits.
        Triggers degradation analysis and state evaluation.
        
        Args:
            trade_id: Unique identifier
            entry_price: Entry price
            exit_price: Exit price
            entry_time: Entry timestamp
            exit_time: Exit timestamp
            mfe: Max Favorable Excursion
            mae: Max Adverse Excursion
            reached_tp1: Reached first take profit
            reached_tp2: Reached second take profit
            is_be_stop: Exited at break-even
        """
        # Record in edge validator
        self.edge_validator.record_trade(
            trade_id=trade_id,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            mfe=mfe,
            mae=mae,
            reached_tp1=reached_tp1,
            reached_tp2=reached_tp2,
            is_be_stop=is_be_stop
        )
        
        # Record in degradation monitor
        self.degradation_monitor.add_trade(
            trade_id=trade_id,
            entry_price=entry_price,
            exit_price=exit_price,
            mfe=mfe,
            mae=mae,
            duration_hours=(exit_time - entry_time).total_seconds() / 3600.0,
            reached_tp1=reached_tp1,
            reached_tp2=reached_tp2,
            is_be_stop=is_be_stop
        )
        
        self.trades_this_session += 1
        
        # Check degradation status
        if self.degradation_monitor.has_active_degradation():
            self.logger.warning(f"[DEGRADATION] 2+ signals active - entering DEGRADED mode")
        
        self.logger.info(f"[TRADE] #{trade_id} recorded - Total: {self.trades_this_session}")
    
    # ========================================================================
    # POSITION SIZING - UNIFIED APPROACH
    # ========================================================================
    
    def get_position_size_multiplier(self) -> float:
        """
        Returns position size multiplier for current STATE only.
        
        For combined state + edge sizing, use get_combined_size_multiplier().
        
        Returns:
            1.0 = ACTIVE (full size)
            0.5 = DEGRADED or OBSERVATION (50% size)
            0.0 = DEAD (no trading)
        """
        return self.state_machine.get_position_size_multiplier()
    
    def get_combined_size_multiplier(self, edge_score: Optional[EdgeScore] = None) -> float:
        """
        Returns COMBINED position size multiplier (state + edge).
        
        SIZING POLICY: Uses min() for safety - the more conservative wins.
        
        1. State multiplier (from StrategyStateMachine)
           - DEAD = 0.0 (blocks all trading)
           - DEGRADED/OBSERVATION = 0.5 (caps position size)
           - ACTIVE = 1.0 (full size allowed)
        
        2. Edge multiplier (from EdgeScore)
           - Based on current market activity/edge quality
           - Can reduce size even in ACTIVE state
        
        Final = min(state_multiplier, edge_multiplier)
        
        Args:
            edge_score: Optional EdgeScore (uses last_edge_score if not provided)
            
        Returns:
            Combined multiplier (0.0 to 1.0)
        """
        # Get state-based multiplier
        state_mult = self.state_machine.get_position_size_multiplier()
        
        # If state blocks trading, return 0 immediately
        if state_mult == 0.0:
            return 0.0
        
        # Get edge-based multiplier
        score = edge_score if edge_score else self.last_edge_score
        if score is None:
            edge_mult = 1.0  # Default to full if no score available
        else:
            edge_mult = score.get_size_multiplier()
        
        # Use min() policy - more conservative wins
        combined = compute_final_position_multiplier(state_mult, edge_mult)
        
        self.logger.debug(
            f"[SIZING] min(State: {state_mult:.2f}, Edge: {edge_mult:.2f}) = {combined:.2f}"
        )
        
        return combined
