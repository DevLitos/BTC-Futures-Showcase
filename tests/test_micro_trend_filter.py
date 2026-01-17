"""
Tests for MicroTrendFilter - Prevents LONGs during micro-bearish trends
"""

import pytest
import numpy as np
import pandas as pd
from engine.micro_trend_filter import (
    MicroTrendFilter, 
    MicroTrendState,
    get_micro_trend_filter,
    reset_micro_trend_filter
)


@pytest.fixture
def micro_trend_filter():
    """Create fresh MicroTrendFilter for each test"""
    reset_micro_trend_filter()
    return MicroTrendFilter(hard_block_enabled=True)


@pytest.fixture
def bearish_df_5m():
    """Create a DataFrame with bearish micro trend"""
    n = 100
    # Gradual downtrend
    base_price = 97000
    prices = []
    current = base_price
    for i in range(n):
        change = -20 + np.random.uniform(-10, 10)
        current += change
        prices.append(current)
    
    closes = np.array(prices)
    highs = closes + np.random.uniform(30, 80, n)
    lows = closes - np.random.uniform(30, 80, n)
    opens = closes + np.random.uniform(-40, 40, n)
    volumes = np.random.uniform(100, 500, n)
    
    return pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })


@pytest.fixture
def bullish_df_5m():
    """Create a DataFrame with bullish micro trend"""
    n = 100
    # Gradual uptrend
    base_price = 95000
    prices = []
    current = base_price
    for i in range(n):
        change = 20 + np.random.uniform(-10, 10)
        current += change
        prices.append(current)
    
    closes = np.array(prices)
    highs = closes + np.random.uniform(30, 80, n)
    lows = closes - np.random.uniform(30, 80, n)
    opens = closes - np.random.uniform(0, 30, n)  # Opens below closes for bullish candles
    volumes = np.random.uniform(100, 500, n)
    
    return pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })


class TestMicroTrendFilter:
    """Test the MicroTrendFilter class"""
    
    def test_detects_bearish_micro_trend(self, micro_trend_filter, bearish_df_5m):
        """Test that bearish micro trend is detected"""
        current_price = bearish_df_5m['close'].iloc[-1]
        
        # Need multiple calls to trigger hysteresis
        for _ in range(3):
            allowed, reason, state = micro_trend_filter.check(
                bearish_df_5m, None, current_price, 'LONG'
            )
        
        assert state.micro_trend == "BEARISH"
        # v2: confirmed_strength after hysteresis
        assert state.confirmed_strength > 0.3 or state.raw_strength > 0.5
        assert state.is_ema_bearish or state.is_below_ema20 or state.has_swing_ll
    
    def test_detects_bullish_micro_trend(self, micro_trend_filter, bullish_df_5m):
        """Test that bullish micro trend is detected"""
        current_price = bullish_df_5m['close'].iloc[-1]
        
        # Need multiple calls to trigger hysteresis
        for _ in range(3):
            allowed, reason, state = micro_trend_filter.check(
                bullish_df_5m, None, current_price, 'SHORT'
            )
        
        assert state.micro_trend == "BULLISH"
        # v2: raw_strength should be detected even if confirmed_strength is lower
        assert state.confirmed_strength > 0.2 or state.raw_strength > 0.3
    
    def test_long_penalized_in_bearish_micro(self, micro_trend_filter, bearish_df_5m):
        """Test that LONG gets penalized in bearish micro trend"""
        current_price = bearish_df_5m['close'].iloc[-1]
        
        # Multiple calls for hysteresis
        for _ in range(3):
            allowed, reason, state = micro_trend_filter.check(
                bearish_df_5m, None, current_price, 'LONG'
            )
        
        # Should have negative score adjustment
        assert state.score_adjustment < 0
        # Either blocked or penalized
        if state.confirmed_strength >= 0.65:
            assert allowed == False  # Hard block
        else:
            assert state.score_adjustment <= -1  # At least -1 penalty
    
    def test_short_gets_bonus_in_bearish_micro(self, micro_trend_filter, bearish_df_5m):
        """Test that SHORT gets bonus in bearish micro trend"""
        current_price = bearish_df_5m['close'].iloc[-1]
        
        allowed, reason, state = micro_trend_filter.check(
            bearish_df_5m, None, current_price, 'SHORT'
        )
        
        assert allowed == True
        assert state.score_adjustment > 0  # Should get bonus
    
    def test_long_gets_bonus_in_bullish_micro(self, micro_trend_filter, bullish_df_5m):
        """Test that LONG gets bonus in bullish micro trend"""
        current_price = bullish_df_5m['close'].iloc[-1]
        
        allowed, reason, state = micro_trend_filter.check(
            bullish_df_5m, None, current_price, 'LONG'
        )
        
        assert allowed == True
        assert state.score_adjustment > 0  # Should get bonus
    
    def test_insufficient_data_allows_entry(self, micro_trend_filter):
        """Test that insufficient data allows entry (doesn't block)"""
        # Only 10 candles
        df_5m = pd.DataFrame({
            'open': [97000] * 10,
            'high': [97100] * 10,
            'low': [96900] * 10,
            'close': [97050] * 10,
            'volume': [100] * 10
        })
        
        allowed, reason, state = micro_trend_filter.check(
            df_5m, None, 97050, 'LONG'
        )
        
        assert allowed == True
        assert "Insufficient" in reason
    
    def test_state_dict_serialization(self, micro_trend_filter, bearish_df_5m):
        """Test that state can be converted to dict"""
        current_price = bearish_df_5m['close'].iloc[-1]
        
        _, _, state = micro_trend_filter.check(
            bearish_df_5m, None, current_price, 'LONG'
        )
        
        state_dict = state.to_dict()
        
        assert 'micro_trend' in state_dict
        assert 'confirmed_strength' in state_dict  # v2: renamed from strength
        assert 'score_adjustment' in state_dict
        assert 'is_ema_bearish' in state_dict
    
    def test_hard_block_disabled(self, bearish_df_5m):
        """Test that hard block can be disabled"""
        # Create filter with hard block disabled
        filter_soft = MicroTrendFilter(hard_block_enabled=False)
        current_price = bearish_df_5m['close'].iloc[-1]
        
        # Multiple calls for hysteresis
        for _ in range(3):
            allowed, reason, state = filter_soft.check(
                bearish_df_5m, None, current_price, 'LONG'
            )
        
        # Should always be allowed (never hard block)
        assert allowed == True
        # But should still penalize
        if state.micro_trend == "BEARISH" and state.confirmed_strength >= 0.25:
            assert state.score_adjustment < 0


class TestMicroTrendFilterSingleton:
    """Test singleton pattern"""
    
    def test_get_filter_returns_same_instance(self):
        """Test singleton returns same instance"""
        reset_micro_trend_filter()
        
        filter1 = get_micro_trend_filter()
        filter2 = get_micro_trend_filter()
        
        assert filter1 is filter2
    
    def test_reset_creates_new_instance(self):
        """Test reset creates new instance"""
        filter1 = get_micro_trend_filter()
        reset_micro_trend_filter()
        filter2 = get_micro_trend_filter()
        
        assert filter1 is not filter2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
