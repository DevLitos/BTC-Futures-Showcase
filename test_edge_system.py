"""
Comprehensive Test Suite for Edge Definition & Operations System

Tests all components:
- EdgeValidator
- DegradationMonitor
- StrategyStateMachine
- EdgeEnforcement

Run with: pytest test_edge_system.py -v
"""

import pytest
from datetime import datetime, timedelta
import numpy as np

from engine.edge_validator import (
    EdgeValidator, ContextValidity, DegradationMetrics,
    TradeRecord
)
from engine.degradation_monitor import DegradationMonitor
from engine.strategy_state_machine import StrategyStateMachine, StrategyState
from engine.edge_enforcement import EdgeEnforcement


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def edge_validator():
    """Initialize fresh EdgeValidator for each test"""
    return EdgeValidator()


@pytest.fixture
def degradation_monitor():
    """Initialize fresh DegradationMonitor for each test"""
    return DegradationMonitor(window_size=20)


@pytest.fixture
def state_machine():
    """Initialize fresh StrategyStateMachine for each test"""
    return StrategyStateMachine()


@pytest.fixture
def enforcement():
    """Initialize fresh EdgeEnforcement for each test"""
    return EdgeEnforcement()


# ============================================================================
# TESTS: EdgeValidator - Context Validation
# ============================================================================

class TestEdgeValidatorContext:
    """Tests for EdgeValidator.validate_context()"""
    
    def test_context_all_valid(self, edge_validator):
        """Test when all context conditions are valid"""
        context = edge_validator.validate_context(
            atr_current=150.0,
            atr_p30=120.0,           # ATR > P30 ✓
            session_range=200.0,
            session_range_avg=250.0, # Range > 0.7×avg ✓
            is_macro_event_day=False, # No macro ✓
            expansion_sessions_count=2, # ≥2 expansions ✓
            volume_trend_london_ny=0.9  # > 0.8 ✓
        )
        
        assert context.is_valid() == True
        assert context.atr_valid == True
        assert context.range_valid == True
        assert context.macro_event_valid == True
        assert context.expansion_valid == True
        assert context.volume_valid == True
    
    def test_context_atr_invalid(self, edge_validator):
        """Test when ATR is below percentile 30"""
        context = edge_validator.validate_context(
            atr_current=100.0,        # < P30
            atr_p30=120.0,
            session_range=200.0,
            session_range_avg=250.0,
            is_macro_event_day=False,
            expansion_sessions_count=2,
            volume_trend_london_ny=0.9
        )
        
        assert context.is_valid() == False
        assert context.atr_valid == False
        assert "ATR below percentile 30" in context.invalid_reasons()
    
    def test_context_range_invalid(self, edge_validator):
        """Test when session range is below 0.7×average"""
        context = edge_validator.validate_context(
            atr_current=150.0,
            atr_p30=120.0,
            session_range=100.0,      # < 0.7×250
            session_range_avg=250.0,
            is_macro_event_day=False,
            expansion_sessions_count=2,
            volume_trend_london_ny=0.9
        )
        
        assert context.is_valid() == False
        assert context.range_valid == False
    
    def test_context_macro_event_invalid(self, edge_validator):
        """Test when post-macro event without follow-through"""
        context = edge_validator.validate_context(
            atr_current=150.0,
            atr_p30=120.0,
            session_range=200.0,
            session_range_avg=250.0,
            is_macro_event_day=True,  # Post-macro shock
            expansion_sessions_count=2,
            volume_trend_london_ny=0.9
        )
        
        assert context.is_valid() == False
        assert context.macro_event_valid == False
    
    def test_context_expansion_invalid(self, edge_validator):
        """Test when < 2 consecutive expansions"""
        context = edge_validator.validate_context(
            atr_current=150.0,
            atr_p30=120.0,
            session_range=200.0,
            session_range_avg=250.0,
            is_macro_event_day=False,
            expansion_sessions_count=1,  # < 2
            volume_trend_london_ny=0.9
        )
        
        assert context.is_valid() == False
        assert context.expansion_valid == False
    
    def test_context_volume_invalid(self, edge_validator):
        """Test when volume trend is below threshold"""
        context = edge_validator.validate_context(
            atr_current=150.0,
            atr_p30=120.0,
            session_range=200.0,
            session_range_avg=250.0,
            is_macro_event_day=False,
            expansion_sessions_count=2,
            volume_trend_london_ny=0.7  # < 0.8
        )
        
        assert context.is_valid() == False
        assert context.volume_valid == False
    
    def test_context_multiple_invalid(self, edge_validator):
        """Test when multiple conditions fail"""
        context = edge_validator.validate_context(
            atr_current=100.0,        # Invalid
            atr_p30=120.0,
            session_range=100.0,      # Invalid
            session_range_avg=250.0,
            is_macro_event_day=True,  # Invalid
            expansion_sessions_count=1, # Invalid
            volume_trend_london_ny=0.7  # Invalid
        )
        
        assert context.is_valid() == False
        assert len(context.invalid_reasons()) >= 5


# ============================================================================
# TESTS: EdgeValidator - Degradation Detection
# ============================================================================

class TestEdgeValidatorDegradation:
    """Tests for EdgeValidator.detect_degradation()"""
    
    def test_degradation_no_signals(self, edge_validator):
        """Test when no degradation signals are present"""
        # Add healthy trades
        for i in range(20):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=2),
                entry_price=100.0,
                exit_price=102.0,        # Winner
                mfe=300.0,               # High MFE
                mae=100.0,               # Low MAE
                reached_tp1=True,
                reached_tp2=True,
                is_be_stop=False
            )
        
        degradation = edge_validator.detect_degradation(window_size=20)
        
        assert degradation.active_signals == 0
        assert degradation.mfe_mae_ratio > 1.5
        assert degradation.be_stop_percentage < 0.4
    
    def test_degradation_mfe_mae_signal(self, edge_validator):
        """Test MFE/MAE ratio degradation signal"""
        # Add trades with low MFE/MAE ratio
        for i in range(20):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=2),
                entry_price=100.0,
                exit_price=101.0,
                mfe=100.0,               # Low MFE
                mae=300.0,               # High MAE
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=False
            )
        
        degradation = edge_validator.detect_degradation(window_size=20)
        
        assert degradation.active_signals >= 1
        assert degradation.mfe_mae_ratio < 1.5
    
    def test_degradation_be_stops_signal(self, edge_validator):
        """Test BE stops percentage degradation signal"""
        # Add trades with many BE stops (> 40%)
        for i in range(20):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=100.0,        # Break-even
                mfe=50.0,
                mae=50.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True          # BE stop
            )
        
        degradation = edge_validator.detect_degradation(window_size=20)
        
        assert degradation.active_signals >= 1
        assert degradation.be_stop_percentage > 0.4
    
    def test_degradation_multiple_signals(self, edge_validator):
        """Test when 2+ signals are detected"""
        # Add 10 trades with MFE/MAE issue + 10 trades with BE stops
        for i in range(10):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=99.0,
                mfe=50.0,
                mae=200.0,               # Bad ratio
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=False
            )
        
        for i in range(10, 20):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=100.0,
                mfe=50.0,
                mae=50.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True          # BE stop
            )
        
        degradation = edge_validator.detect_degradation(window_size=20)
        
        assert degradation.active_signals >= 2


# ============================================================================
# TESTS: EdgeValidator - Trade Recording & Reporting
# ============================================================================

class TestEdgeValidatorTradeRecording:
    """Tests for trade recording and reporting"""
    
    def test_record_trade_winner(self, edge_validator):
        """Test recording a winning trade"""
        edge_validator.record_trade(
            trade_id=1,
            entry_time=datetime.utcnow(),
            exit_time=datetime.utcnow() + timedelta(hours=2),
            entry_price=100.0,
            exit_price=103.0,
            mfe=400.0,
            mae=50.0,
            reached_tp1=True,
            reached_tp2=True,
            is_be_stop=False
        )
        
        assert edge_validator.get_trade_count() == 1
        trade = edge_validator.trade_history[0]
        assert trade.is_winner == True
        assert trade.reached_tp1 == True
        assert trade.reached_tp2 == True
    
    def test_record_trade_loser(self, edge_validator):
        """Test recording a losing trade"""
        edge_validator.record_trade(
            trade_id=1,
            entry_time=datetime.utcnow(),
            exit_time=datetime.utcnow() + timedelta(hours=1),
            entry_price=100.0,
            exit_price=98.0,
            mfe=100.0,
            mae=200.0,
            reached_tp1=False,
            reached_tp2=False,
            is_be_stop=False
        )
        
        assert edge_validator.get_trade_count() == 1
        trade = edge_validator.trade_history[0]
        assert trade.is_winner == False
    
    def test_win_rate_calculation(self, edge_validator):
        """Test win rate calculation"""
        # Add 10 trades: 6 winners, 4 losers
        for i in range(6):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=102.0,  # Winner
                mfe=200.0, mae=50.0,
                reached_tp1=True, reached_tp2=True, is_be_stop=False
            )
        
        for i in range(6, 10):
            edge_validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=98.0,   # Loser
                mfe=50.0, mae=200.0,
                reached_tp1=False, reached_tp2=False, is_be_stop=False
            )
        
        report = edge_validator.calculate_win_rate(window_size=10)
        
        assert report['win_rate'] == 0.6  # 6/10
        assert report['profit_factor'] > 0


# ============================================================================
# TESTS: DegradationMonitor - Signal Detection
# ============================================================================

class TestDegradationMonitor:
    """Tests for DegradationMonitor signal detection"""
    
    def test_mfe_mae_signal_detection(self, degradation_monitor):
        """Test MFE/MAE degradation signal detection"""
        # Add trades with bad MFE/MAE ratio AND be stops to trigger 2+ signals
        for i in range(20):
            degradation_monitor.add_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=100.05,  # Break-even or small win
                mfe=100.0,
                mae=400.0,          # Bad ratio
                duration_hours=1.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True     # Add BE stop signal
            )

        assert degradation_monitor.has_active_degradation() == True
        metrics = degradation_monitor.get_current_metrics()
        assert metrics['mfe_mae_ratio'] < 1.5
    
    def test_be_stop_signal_detection(self, degradation_monitor):
        """Test BE stops signal detection"""
        # Add trades with > 40% BE stops
        for i in range(20):
            degradation_monitor.add_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=100.0,      # Break-even
                mfe=50.0,
                mae=50.0,
                duration_hours=1.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True         # BE stop
            )
        
        assert degradation_monitor.has_active_degradation() == True
        metrics = degradation_monitor.get_current_metrics()
        assert metrics['be_stop_pct'] > 40
    
    def test_health_status_healthy(self, degradation_monitor):
        """Test health status when no signals"""
        # Add healthy trades
        for i in range(20):
            degradation_monitor.add_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=102.0,
                mfe=300.0,
                mae=50.0,
                duration_hours=2.0,
                reached_tp1=True,
                reached_tp2=True,
                is_be_stop=False
            )
        
        assert degradation_monitor.get_health_status() == "HEALTHY"
        assert degradation_monitor.has_active_degradation() == False
    
    def test_health_status_critical(self, degradation_monitor):
        """Test health status when 2+ signals active"""
        # Add trades that trigger multiple signals
        for i in range(10):
            degradation_monitor.add_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=99.0,
                mfe=100.0,
                mae=400.0,              # Signal 1
                duration_hours=1.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True         # Signal 2
            )
        
        for i in range(10, 20):
            degradation_monitor.add_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=100.0,
                mfe=50.0,
                mae=50.0,
                duration_hours=1.0,
                reached_tp1=False,
                reached_tp2=False,
                is_be_stop=True         # More BE stops
            )
        
        assert degradation_monitor.has_active_degradation() == True
        status = degradation_monitor.get_health_status()
        assert status in ["CRITICAL", "SEVERE"]


# ============================================================================
# TESTS: StrategyStateMachine - State Transitions
# ============================================================================

class TestStrategyStateMachine:
    """Tests for StrategyStateMachine state transitions"""
    
    def test_initial_state_active(self, state_machine):
        """Test initial state is ACTIVE"""
        assert state_machine.current_state == StrategyState.ACTIVE
        assert state_machine.get_position_size_multiplier() == 1.0
        assert state_machine.can_trade() == True
    
    def test_transition_to_degraded(self, state_machine):
        """Test transition from ACTIVE to DEGRADED"""
        state_machine.transition_to_degraded(
            reason="Test degradation",
            metrics={}
        )
        
        assert state_machine.current_state == StrategyState.DEGRADED
        assert state_machine.get_position_size_multiplier() == 0.5
        assert state_machine.can_trade() == True
    
    def test_transition_to_observation(self, state_machine):
        """Test transition to OBSERVATION"""
        state_machine.transition_to_observation(
            reason="Test observation",
            metrics={}
        )
        
        assert state_machine.current_state == StrategyState.OBSERVATION
        assert state_machine.get_position_size_multiplier() == 0.5
        assert state_machine.is_observation_period_remaining() > timedelta(0)
    
    def test_transition_to_dead(self, state_machine):
        """Test transition to DEAD"""
        state_machine.transition_to_dead(
            reason="Test death",
            metrics={}
        )
        
        assert state_machine.current_state == StrategyState.DEAD
        assert state_machine.get_position_size_multiplier() == 0.0
        assert state_machine.can_trade() == False
    
    def test_state_transition_history(self, state_machine):
        """Test state transition history tracking"""
        state_machine.transition_to_degraded("test1", {})
        state_machine.transition_to_active("test2", {})
        
        history = state_machine.get_transition_history(limit=5)
        
        assert len(history) >= 2
        assert history[0]['from'] == 'operating'
        assert history[0]['to'] == 'reduced_size_50pct'
    
    def test_observation_period_countdown(self, state_machine):
        """Test observation period countdown"""
        state_machine.transition_to_observation("test", {})
        
        # Check remaining is positive
        remaining1 = state_machine.is_observation_period_remaining()
        assert remaining1 > timedelta(0)
        assert remaining1.days in [13, 14]  # Should be around 14 days
        
        # Simulate time passing by modifying entry time
        from datetime import datetime, timezone
        state_machine.state_entry_time = datetime.now(timezone.utc) - timedelta(days=7)
        remaining2 = state_machine.is_observation_period_remaining()
        assert remaining2 > timedelta(0)
        assert remaining2.days in [6, 7]  # Should be around 7 days left
    
    def test_dead_is_permanent(self, state_machine):
        """Test that DEAD state cannot be transitioned from"""
        state_machine.transition_to_dead("test", {})
        
        # Try to transition - should not work
        state_machine.transition_to_active("should not work", {})
        
        assert state_machine.current_state == StrategyState.DEAD


# ============================================================================
# TESTS: EdgeEnforcement - Integration
# ============================================================================

class TestEdgeEnforcement:
    """Tests for EdgeEnforcement integration"""
    
    def test_pre_trade_validation_pass(self, enforcement):
        """Test pre-trade validation when all conditions pass"""
        context = ContextValidity(
            atr_valid=True,
            range_valid=True,
            macro_event_valid=True,
            expansion_valid=True,
            volume_valid=True
        )
        
        can_trade, blockers = enforcement.validate_pre_trade(
            context=context,
            current_drawdown=0.10
        )
        
        assert can_trade == True
        assert len(blockers) == 0
    
    def test_pre_trade_validation_fail_context(self, enforcement):
        """Test pre-trade validation fails on context invalid"""
        context = ContextValidity(
            atr_valid=False,           # Invalid
            range_valid=True,
            macro_event_valid=True,
            expansion_valid=True,
            volume_valid=True
        )
        
        can_trade, blockers = enforcement.validate_pre_trade(
            context=context,
            current_drawdown=0.10
        )
        
        assert can_trade == False
        assert len(blockers) > 0
    
    def test_pre_trade_validation_fail_drawdown(self, enforcement):
        """Test pre-trade validation fails on high drawdown"""
        context = ContextValidity(
            atr_valid=True,
            range_valid=True,
            macro_event_valid=True,
            expansion_valid=True,
            volume_valid=True
        )
        
        can_trade, blockers = enforcement.validate_pre_trade(
            context=context,
            current_drawdown=0.20  # > 18%
        )
        
        assert can_trade == False
    
    def test_position_size_multiplier_active(self, enforcement):
        """Test position sizing in ACTIVE state"""
        enforcement.state_machine.current_state = StrategyState.ACTIVE
        
        assert enforcement.get_position_size_multiplier() == 1.0
    
    def test_position_size_multiplier_degraded(self, enforcement):
        """Test position sizing in DEGRADED state"""
        enforcement.state_machine.current_state = StrategyState.DEGRADED
        
        assert enforcement.get_position_size_multiplier() == 0.5
    
    def test_position_size_multiplier_dead(self, enforcement):
        """Test position sizing in DEAD state"""
        enforcement.state_machine.current_state = StrategyState.DEAD
        
        assert enforcement.get_position_size_multiplier() == 0.0
    
    def test_record_trade_updates_all_systems(self, enforcement):
        """Test that recording a trade updates all subsystems"""
        enforcement.record_completed_trade(
            trade_id=1,
            entry_price=100.0,
            exit_price=102.0,
            entry_time=datetime.utcnow() - timedelta(hours=2),
            exit_time=datetime.utcnow(),
            mfe=300.0,
            mae=50.0,
            reached_tp1=True,
            reached_tp2=True,
            is_be_stop=False
        )
        
        # Check all systems recorded it
        assert enforcement.edge_validator.get_trade_count() == 1
        assert enforcement.degradation_monitor.get_current_metrics()
        assert enforcement.trades_this_session == 1
    
    def test_health_report_generation(self, enforcement):
        """Test health report generation"""
        # Add sample trades
        for i in range(10):
            enforcement.record_completed_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=102.0 if i % 2 == 0 else 98.0,
                entry_time=datetime.utcnow() - timedelta(hours=2),
                exit_time=datetime.utcnow(),
                mfe=200.0,
                mae=100.0,
                reached_tp1=True,
                reached_tp2=i % 2 == 0,
                is_be_stop=False
            )
        
        report = enforcement.get_health_report()
        
        assert report['trades_total'] == 10
        assert report['position_multiplier'] == 1.0
        assert 'win_rate_pct' in report
        assert 'expectancy_r' in report


# ============================================================================
# INTEGRATION TESTS: Complete Scenarios
# ============================================================================

class TestCompleteScenarios:
    """Integration tests with complete trading scenarios"""
    
    def test_scenario_healthy_trading(self):
        """Scenario: Healthy trading with no degradation"""
        enforcement = EdgeEnforcement()
        
        # Valid context
        context = ContextValidity(
            atr_valid=True, range_valid=True,
            macro_event_valid=True, expansion_valid=True,
            volume_valid=True
        )
        
        # Execute 10 trades (6 winners, 4 losers)
        for i in range(10):
            # Pre-trade validation
            can_trade, _ = enforcement.validate_pre_trade(context, 0.05)
            assert can_trade == True
            
            # Record trade
            is_winner = i < 6
            enforcement.record_completed_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=102.0 if is_winner else 98.0,
                entry_time=datetime.utcnow() - timedelta(hours=1),
                exit_time=datetime.utcnow(),
                mfe=300.0 if is_winner else 100.0,
                mae=50.0 if is_winner else 200.0,
                reached_tp1=is_winner,
                reached_tp2=is_winner,
                is_be_stop=False
            )
        
        # Verify state
        assert enforcement.state_machine.current_state == StrategyState.ACTIVE
        report = enforcement.get_health_report()
        assert report['win_rate_pct'] == 60.0
        assert report['degradation_health'] == 'HEALTHY'
    
    def test_scenario_degradation_detection(self):
        """Scenario: Degradation is detected and state changes"""
        enforcement = EdgeEnforcement()
        context = ContextValidity(
            atr_valid=True,
            range_valid=True,
            macro_event_valid=True,
            expansion_valid=True,
            volume_valid=True
        )
        
        # Add trades with degradation signals
        for i in range(20):
            is_winner = i < 10
            enforcement.record_completed_trade(
                trade_id=i,
                entry_price=100.0,
                exit_price=102.0 if is_winner else 99.0,
                entry_time=datetime.utcnow() - timedelta(hours=1),
                exit_time=datetime.utcnow(),
                mfe=100.0,               # Low MFE
                mae=300.0,               # High MAE
                reached_tp1=is_winner,
                reached_tp2=False,       # Not extending
                is_be_stop=not is_winner # BE stops
            )
        
        # Check degradation detected
        degradation = enforcement.degradation_monitor.get_summary_report()
        assert degradation['active_signals'] >= 2
        
        # Verify state transition happened
        can_trade, _ = enforcement.validate_pre_trade(context, 0.05)
        # State machine should have processed the degradation
    
    def test_scenario_edge_death(self):
        """Scenario: Edge dies permanently"""
        validator = EdgeValidator()
        
        # Add 3 windows of 50 trades each with negative expectancy
        for window in range(3):
            for i in range(50):
                # Mostly losing trades
                is_winner = i < 15  # Only 30% win rate (below 45%)
                validator.record_trade(
                    trade_id=window * 50 + i,
                    entry_time=datetime.utcnow(),
                    exit_time=datetime.utcnow() + timedelta(hours=1),
                    entry_price=100.0,
                    exit_price=102.0 if is_winner else 98.5,
                    mfe=200.0 if is_winner else 50.0,
                    mae=50.0 if is_winner else 150.0,
                    reached_tp1=is_winner,
                    reached_tp2=False,
                    is_be_stop=False
                )
        
        # Check expectancy is negative
        expectancy = validator.calculate_expectancy(window_size=50)
        assert expectancy < 0  # Negative expectancy


# ============================================================================
# EDGE CASE TESTS
# ============================================================================

class TestEdgeCases:
    """Tests for edge cases and boundary conditions"""
    
    def test_empty_trade_history(self, edge_validator):
        """Test with no trades recorded"""
        assert edge_validator.get_trade_count() == 0
        
        degradation = edge_validator.detect_degradation(window_size=20)
        assert degradation.active_signals == 0
    
    def test_insufficient_trades_for_window(self, edge_validator):
        """Test when trades < window_size"""
        edge_validator.record_trade(
            trade_id=1,
            entry_time=datetime.utcnow(),
            exit_time=datetime.utcnow() + timedelta(hours=1),
            entry_price=100.0,
            exit_price=102.0,
            mfe=200.0, mae=50.0,
            reached_tp1=True, reached_tp2=True,
            is_be_stop=False
        )
        
        # Should not crash with < 20 trades
        degradation = edge_validator.detect_degradation(window_size=20)
        assert degradation.active_signals == 0
    
    def test_division_by_zero_protection(self, degradation_monitor):
        """Test protection against division by zero"""
        # Add a single trade
        degradation_monitor.add_trade(
            trade_id=1,
            entry_price=100.0,
            exit_price=100.0,
            mfe=0.0,           # Zero MFE
            mae=0.0,           # Zero MAE
            duration_hours=1.0,
            reached_tp1=False,
            reached_tp2=False,
            is_be_stop=True
        )
        
        # Should handle gracefully
        metrics = degradation_monitor.get_current_metrics()
        assert metrics['mfe_mae_ratio'] == 0.0
    
    def test_very_large_sample(self):
        """Test with very large sample of trades"""
        validator = EdgeValidator()
        
        # Add 500 trades
        for i in range(500):
            is_winner = i % 3 < 2  # ~66% win rate
            validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=102.0 if is_winner else 98.0,
                mfe=200.0 if is_winner else 50.0,
                mae=50.0 if is_winner else 200.0,
                reached_tp1=is_winner,
                reached_tp2=is_winner,
                is_be_stop=False
            )
        
        assert validator.get_trade_count() == 500
        
        # Calculate metrics
        expectancy = validator.calculate_expectancy(window_size=100)
        assert expectancy > 0  # Should be positive with 66% win rate
    
    def test_all_winners_scenario(self):
        """Test edge case with 100% win rate"""
        validator = EdgeValidator()
        
        for i in range(30):
            validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=103.0,  # Always winner
                mfe=300.0, mae=0.0,
                reached_tp1=True, reached_tp2=True,
                is_be_stop=False
            )
        
        report = validator.calculate_win_rate(window_size=30)
        assert report['win_rate'] == 1.0
        
        expectancy = validator.calculate_expectancy(window_size=30)
        assert expectancy == 1.0  # Max expectancy
    
    def test_all_losers_scenario(self):
        """Test edge case with 0% win rate"""
        validator = EdgeValidator()
        
        for i in range(30):
            validator.record_trade(
                trade_id=i,
                entry_time=datetime.utcnow(),
                exit_time=datetime.utcnow() + timedelta(hours=1),
                entry_price=100.0,
                exit_price=97.0,   # Always loser
                mfe=0.0, mae=300.0,
                reached_tp1=False, reached_tp2=False,
                is_be_stop=False
            )
        
        report = validator.calculate_win_rate(window_size=30)
        assert report['win_rate'] == 0.0
        
        expectancy = validator.calculate_expectancy(window_size=30)
        assert expectancy == -1.0  # Min expectancy


# ============================================================================
# PYTEST CONFIGURATION
# ============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
