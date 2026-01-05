"""
Tests for core_utils and integrated fixes.

Tests the centralized utilities and integration between modules.
"""

import pytest
import numpy as np
from datetime import datetime, timedelta, UTC

from engine.core_utils import (
    safe_float, safe_int, 
    to_epoch_seconds, to_datetime, ensure_utc,
    validate_candle, get_validated_ohlcv, CandleValidationError,
    PositionSizer, get_position_sizer, reset_position_sizer,
    calculate_expectancy, calculate_expectancy_r
)
from engine.time_provider import TimeProvider, get_time_provider, reset_time_provider
from engine.state_types import (
    StrategyState, get_state_size_multiplier, 
    can_trade_in_state, compute_final_position_multiplier
)


class TestSafeConversions:
    """Test safe data conversions."""
    
    def test_safe_float_none(self):
        assert safe_float(None) == 0.0
        assert safe_float(None, 99.9) == 99.9
    
    def test_safe_float_valid(self):
        assert safe_float(123.45) == 123.45
        assert safe_float("123.45") == 123.45
        assert safe_float(100) == 100.0
    
    def test_safe_float_invalid(self):
        assert safe_float("invalid") == 0.0
        assert safe_float("invalid", -1.0) == -1.0
        assert safe_float(float('nan')) == 0.0
        assert safe_float(float('inf')) == 0.0
    
    def test_safe_int(self):
        assert safe_int(None) == 0
        assert safe_int(123.7) == 123
        assert safe_int("456") == 456
        assert safe_int("invalid", -1) == -1


class TestTimestampConversions:
    """Test timestamp standardization."""
    
    def test_to_epoch_seconds_datetime(self):
        dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        epoch = to_epoch_seconds(dt)
        assert isinstance(epoch, float)
        assert epoch > 0
    
    def test_to_epoch_seconds_milliseconds(self):
        # Milliseconds (CCXT default)
        ms = 1735732800000  # 2025-01-01 12:00:00 UTC
        epoch = to_epoch_seconds(ms)
        assert epoch == 1735732800.0
    
    def test_to_epoch_seconds_seconds(self):
        # Already in seconds
        seconds = 1735732800
        epoch = to_epoch_seconds(seconds)
        assert epoch == 1735732800.0
    
    def test_to_datetime(self):
        ms = 1735732800000
        dt = to_datetime(ms)
        assert isinstance(dt, datetime)
        assert dt.tzinfo is not None
    
    def test_ensure_utc(self):
        naive = datetime(2025, 1, 1, 12, 0, 0)
        aware = ensure_utc(naive)
        assert aware.tzinfo is not None


class TestCandleValidation:
    """Test candle validation logic."""
    
    def test_valid_candle(self):
        candle = {
            'timestamp': 1735732800000,
            'open': 100.0,
            'high': 105.0,
            'low': 98.0,
            'close': 102.0,
            'volume': 1000.0
        }
        is_valid, errors = validate_candle(candle)
        assert is_valid
        assert len(errors) == 0
    
    def test_missing_fields(self):
        candle = {'open': 100, 'high': 105}  # Missing low and close
        is_valid, errors = validate_candle(candle)
        assert not is_valid
        assert len(errors) >= 2
    
    def test_high_less_than_low(self):
        candle = {
            'open': 100.0,
            'high': 95.0,  # Invalid: high < low
            'low': 98.0,
            'close': 97.0
        }
        is_valid, errors = validate_candle(candle)
        assert not is_valid
        assert any('High' in e and 'Low' in e for e in errors)
    
    def test_high_less_than_close(self):
        candle = {
            'open': 100.0,
            'high': 95.0,  # Invalid: high < close
            'low': 90.0,
            'close': 102.0
        }
        is_valid, errors = validate_candle(candle)
        assert not is_valid
    
    def test_negative_price(self):
        candle = {
            'open': -100.0,
            'high': 105.0,
            'low': 98.0,
            'close': 102.0
        }
        is_valid, errors = validate_candle(candle)
        assert not is_valid
    
    def test_get_validated_ohlcv(self):
        candle = {'open': 100, 'high': 105, 'low': 98, 'close': 102, 'volume': 1000}
        o, h, l, c, v = get_validated_ohlcv(candle)
        assert o == 100.0
        assert h == 105.0
        assert l == 98.0
        assert c == 102.0
        assert v == 1000.0
    
    def test_get_validated_ohlcv_with_defaults(self):
        candle = {'open': 100, 'high': 105}  # Missing low, close, volume
        defaults = {'low': 99.0, 'close': 101.0, 'volume': 0.0}
        o, h, l, c, v = get_validated_ohlcv(candle, defaults)
        assert l == 99.0
        assert c == 101.0
        assert v == 0.0


class TestPositionSizer:
    """Test centralized position sizing."""
    
    @pytest.fixture(autouse=True)
    def reset_sizer(self):
        """Reset global sizer before each test."""
        reset_position_sizer()
        yield
        reset_position_sizer()
    
    def test_calculate_base_size_risk_based(self):
        # Disable dynamic risk to test explicit risk_pct
        sizer = PositionSizer(default_risk_pct=0.01, max_position_pct=1.0, leverage=10, use_dynamic_risk=False)
        
        # Capital: $10,000, Risk: 1% = $100
        # Entry: $50,000, SL: $49,000, Risk per unit: $1,000
        # Size: $100 / $1,000 = 0.1 BTC
        base = sizer.calculate_base_size(
            capital=10000,
            entry_price=50000,
            stop_loss=49000
        )
        assert abs(base - 0.1) < 0.001
    
    def test_calculate_base_size_margin_limited(self):
        # Disable dynamic risk to test explicit risk_pct, use leverage=10
        sizer = PositionSizer(default_risk_pct=0.5, max_position_pct=0.10, leverage=10, use_dynamic_risk=False)
        
        # High risk % but margin limits it
        # Capital: $10,000, Leverage: 10x, Max position: 10%
        # Max notional: $10,000 * 10 * 0.10 = $10,000
        # Size: $10,000 / $50,000 = 0.2 BTC
        base = sizer.calculate_base_size(
            capital=10000,
            entry_price=50000,
            stop_loss=40000  # Wide SL would allow larger risk-based size
        )
        assert base == 0.2  # Margin limited
    
    def test_apply_multipliers(self):
        sizer = PositionSizer()
        
        final, breakdown = sizer.apply_multipliers(
            base_size=1.0,
            state_multiplier=0.5,
            edge_multiplier=0.75,
            defensive_multiplier=1.0
        )
        
        assert final == 0.375  # 1.0 * 0.5 * 0.75 * 1.0
        assert breakdown['state'] == 0.5
        assert breakdown['edge'] == 0.75
        assert breakdown['blocked_by'] is None
    
    def test_apply_multipliers_blocked_by_state(self):
        sizer = PositionSizer()
        
        final, breakdown = sizer.apply_multipliers(
            base_size=1.0,
            state_multiplier=0.0,  # DEAD state
            edge_multiplier=1.0,
            defensive_multiplier=1.0
        )
        
        assert final == 0.0
        assert breakdown['blocked_by'] == 'state'
    
    def test_calculate_position_size_full_flow(self):
        sizer = get_position_sizer()
        
        size, breakdown = sizer.calculate_position_size(
            capital=10000,
            entry_price=50000,
            stop_loss=49000,
            state_multiplier=1.0,
            edge_multiplier=0.75,
            defensive_multiplier=1.0,
            leverage=10
        )
        
        assert size > 0
        assert breakdown['combined'] == 0.75
        assert breakdown['final'] == size


class TestExpectancyCalculation:
    """Test expectancy formulas."""
    
    def test_calculate_expectancy_profitable(self):
        # Win 50%, average win 100, average loss 50
        # E = (0.5 * 100) - (0.5 * 50) = 50 - 25 = 25
        exp = calculate_expectancy(0.5, 100, 50)
        assert exp == 25.0
    
    def test_calculate_expectancy_break_even(self):
        # Win 50%, average win 50, average loss 50
        # E = (0.5 * 50) - (0.5 * 50) = 0
        exp = calculate_expectancy(0.5, 50, 50)
        assert exp == 0.0
    
    def test_calculate_expectancy_negative(self):
        # Win 30%, average win 50, average loss 50
        # E = (0.3 * 50) - (0.7 * 50) = 15 - 35 = -20
        exp = calculate_expectancy(0.3, 50, 50)
        assert exp == -20.0
    
    def test_calculate_expectancy_r(self):
        trades = [
            {'pnl_r': 2.0},  # 2R win
            {'pnl_r': -1.0},  # 1R loss
            {'pnl_r': 1.5},  # 1.5R win
            {'pnl_r': -1.0},  # 1R loss
        ]
        exp = calculate_expectancy_r(trades)
        # Average: (2.0 - 1.0 + 1.5 - 1.0) / 4 = 1.5 / 4 = 0.375R
        assert abs(exp - 0.375) < 0.001


class TestTimeProvider:
    """Test centralized time provider."""
    
    @pytest.fixture(autouse=True)
    def reset_provider(self):
        reset_time_provider()
        yield
        reset_time_provider()
    
    def test_live_mode_returns_current_time(self):
        tp = get_time_provider(TimeProvider.MODE_LIVE)
        now = tp.now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None
        # Should be close to actual current time
        diff = abs((datetime.now(UTC) - now).total_seconds())
        assert diff < 2  # Within 2 seconds
    
    def test_backtest_mode_returns_set_time(self):
        tp = get_time_provider(TimeProvider.MODE_BACKTEST)
        test_time = datetime(2025, 6, 15, 14, 30, 0, tzinfo=UTC)
        tp.set_market_time(test_time)
        
        assert tp.now() == test_time
    
    def test_elapsed_since(self):
        tp = get_time_provider(TimeProvider.MODE_BACKTEST)
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        current = datetime(2025, 1, 1, 12, 30, 0, tzinfo=UTC)
        tp.set_market_time(current)
        
        elapsed = tp.elapsed_since(start)
        assert elapsed.total_seconds() == 1800  # 30 minutes
    
    def test_is_expired(self):
        tp = get_time_provider(TimeProvider.MODE_BACKTEST)
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        current = datetime(2025, 1, 1, 12, 10, 0, tzinfo=UTC)
        tp.set_market_time(current)
        
        assert tp.is_expired(start, timedelta(minutes=5))
        assert not tp.is_expired(start, timedelta(minutes=15))


class TestStateTypes:
    """Test state type definitions and functions."""
    
    def test_get_state_size_multiplier(self):
        assert get_state_size_multiplier(StrategyState.ACTIVE) == 1.0
        assert get_state_size_multiplier(StrategyState.DEGRADED) == 0.5
        assert get_state_size_multiplier(StrategyState.OBSERVATION) == 0.5
        assert get_state_size_multiplier(StrategyState.DEAD) == 0.0
    
    def test_can_trade_in_state(self):
        assert can_trade_in_state(StrategyState.ACTIVE)
        assert can_trade_in_state(StrategyState.DEGRADED)
        assert can_trade_in_state(StrategyState.OBSERVATION)
        assert not can_trade_in_state(StrategyState.DEAD)
    
    def test_compute_final_position_multiplier_active(self):
        # ACTIVE + good edge = use edge multiplier
        mult = compute_final_position_multiplier(1.0, 0.75)
        assert mult == 0.75
    
    def test_compute_final_position_multiplier_degraded(self):
        # DEGRADED (0.5) + good edge (0.75) = min(0.5, 0.75) = 0.5
        mult = compute_final_position_multiplier(0.5, 0.75)
        assert mult == 0.5
    
    def test_compute_final_position_multiplier_poor_edge(self):
        # ACTIVE (1.0) + poor edge (0.3) = min(1.0, 0.3) = 0.3
        mult = compute_final_position_multiplier(1.0, 0.3)
        assert mult == 0.3
    
    def test_compute_final_position_multiplier_dead(self):
        # DEAD always returns 0
        mult = compute_final_position_multiplier(0.0, 1.0)
        assert mult == 0.0


class TestIntegration:
    """Integration tests between modules."""
    
    def test_state_machine_uses_time_provider(self):
        from engine.state_machine import StateMachine, BotState
        
        reset_time_provider()
        sm = StateMachine(backtest_mode=True)
        
        test_time = datetime(2025, 3, 1, 10, 0, 0, tzinfo=UTC)
        sm.set_market_time(test_time)
        
        # Should use the set time
        assert sm._get_time() == test_time
    
    def test_market_data_validates_candles(self):
        from engine.market_data import SynchronizedMultiTimeframeBuffer
        
        buffer = SynchronizedMultiTimeframeBuffer('BTC/USD', backtest_mode=True)
        
        # Add valid candle
        valid = {
            'timeframe': '1m',
            'timestamp': datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            'open': 100, 'high': 105, 'low': 98, 'close': 102, 'volume': 1000
        }
        buffer.add_candle(valid)
        
        stats = buffer.get_validation_stats()
        assert stats['valid'] >= 1
    
    def test_position_sizer_in_risk_engine(self):
        """Verify RiskEngine can work with PositionSizer."""
        from engine.risk_engine import RiskEngine
        
        re = RiskEngine(capital=10000)
        
        # RiskEngine still has its own calculate_position_size
        # but it should be consistent with PositionSizer
        size = re.calculate_position_size(
            entry_price=50000,
            stop_loss=49000,
            hour=14,
            minute=30,
            leverage=10
        )
        
        assert size > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
