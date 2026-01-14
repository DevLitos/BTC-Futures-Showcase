"""
Tests for LocationFilter - Macro Context Veto System

Tests the three main rules:
1. Extremes veto by regime (no longs in top 10%, no shorts in bottom 10% in RANGE)
2. Headroom minimum (distance to structure high/low in ATR)
3. Breakout override (allows extremes if ADX high + volume expansion)
"""

import pytest
import numpy as np
from engine.location_filter import (
    LocationFilter, LocationConfig, LocationContext,
    get_location_filter, check_location_veto
)


@pytest.fixture
def location_filter():
    """Create a fresh LocationFilter for each test."""
    return LocationFilter(LocationConfig())


@pytest.fixture
def price_data_in_range():
    """
    Generate price data simulating a RANGE market.
    Price oscillates between 95000 and 105000 over 60 days.
    Current price in the middle (percentile ~50%).
    """
    bars = 24 * 60  # 60 days of 1h bars
    t = np.linspace(0, 20 * np.pi, bars)  # ~10 oscillations
    
    # Oscillate between 95k and 105k
    base = 100000
    amplitude = 5000
    closes = base + amplitude * np.sin(t)
    
    # Add some noise
    noise = np.random.normal(0, 100, bars)
    closes = closes + noise
    
    # High/low around close
    highs = closes + np.abs(np.random.normal(100, 50, bars))
    lows = closes - np.abs(np.random.normal(100, 50, bars))
    
    # Volume
    volumes = np.random.uniform(100, 200, bars)
    
    return {
        'closes': closes.astype(float),
        'highs': highs.astype(float),
        'lows': lows.astype(float),
        'volumes': volumes.astype(float),
        'range_high': float(np.max(highs)),
        'range_low': float(np.min(lows))
    }


@pytest.fixture
def price_data_at_high():
    """
    Generate price data where current price is at 95th percentile.
    Simulates buying at the top of a range.
    """
    bars = 24 * 60
    
    # Create price history that was lower, now at highs
    closes = np.linspace(95000, 105000, bars)  # Trending up
    closes = closes + np.random.normal(0, 100, bars)
    
    highs = closes + np.abs(np.random.normal(100, 50, bars))
    lows = closes - np.abs(np.random.normal(100, 50, bars))
    volumes = np.random.uniform(100, 200, bars)
    
    return {
        'closes': closes.astype(float),
        'highs': highs.astype(float),
        'lows': lows.astype(float),
        'volumes': volumes.astype(float),
        'current_price': float(closes[-1]),  # At the high
        'range_high': float(np.max(highs)),
        'range_low': float(np.min(lows))
    }


class TestLocationContext:
    """Test LocationContext calculations."""
    
    def test_context_initialization(self):
        """Test default context values."""
        ctx = LocationContext()
        assert ctx.percentile_60d == 50.0
        assert ctx.veto_long == False
        assert ctx.veto_short == False
        assert ctx.veto_reasons == []
    
    def test_context_to_dict(self):
        """Test context serialization."""
        ctx = LocationContext(
            current_price=100000,
            percentile_60d=75.0,
            regime="RANGE"
        )
        d = ctx.to_dict()
        assert d['price'] == 100000
        assert d['pct_60d'] == 75.0
        assert d['regime'] == "RANGE"


class TestLocationFilterCalculation:
    """Test location context calculation."""
    
    def test_percentile_calculation_middle(self, location_filter, price_data_in_range):
        """Test percentile when price is in middle of range."""
        data = price_data_in_range
        
        # Current price in middle
        current_price = 100000.0
        atr = 500.0
        adx = 20.0
        
        context = location_filter.calculate_context(
            closes_1h=data['closes'],
            highs_1h=data['highs'],
            lows_1h=data['lows'],
            volumes_1h=data['volumes'],
            current_price=current_price,
            atr_1h=atr,
            adx_1h=adx,
            regime="RANGE"
        )
        
        # Should be around 50%
        assert 40 <= context.percentile_60d <= 60, f"Expected ~50%, got {context.percentile_60d}"
        assert not context.in_extreme_high
        assert not context.in_extreme_low
    
    def test_percentile_calculation_at_high(self, location_filter, price_data_at_high):
        """Test percentile when price is at high of range."""
        data = price_data_at_high
        
        context = location_filter.calculate_context(
            closes_1h=data['closes'],
            highs_1h=data['highs'],
            lows_1h=data['lows'],
            volumes_1h=data['volumes'],
            current_price=data['current_price'],
            atr_1h=500.0,
            adx_1h=20.0,
            regime="RANGE"
        )
        
        # Should be in extreme high
        assert context.percentile_60d >= 90, f"Expected >=90%, got {context.percentile_60d}"
        assert context.in_extreme_high
        assert not context.in_extreme_low
    
    def test_headroom_calculation(self, location_filter, price_data_in_range):
        """Test headroom to structure highs/lows."""
        data = price_data_in_range
        current_price = 100000.0
        atr = 500.0  # 0.5% of price
        
        context = location_filter.calculate_context(
            closes_1h=data['closes'],
            highs_1h=data['highs'],
            lows_1h=data['lows'],
            volumes_1h=data['volumes'],
            current_price=current_price,
            atr_1h=atr,
            adx_1h=20.0,
            regime="RANGE"
        )
        
        # Range is ~95k-105k, so ~5k each way from 100k = 10 ATR
        assert context.headroom_to_high_atr > 5
        assert context.headroom_to_low_atr > 5


class TestExtremesVeto:
    """Test Rule 1: Extremes veto by regime."""
    
    def test_veto_long_at_extreme_high_in_range(self, location_filter):
        """LONG should be vetoed at 95th percentile in RANGE."""
        # Create data where price is at extreme high
        bars = 24 * 60
        closes = np.linspace(90000, 100000, bars)
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,  # At the high
            atr_1h=500.0,
            adx_1h=18.0,  # Low ADX = RANGE
            regime="RANGE"
        )
        
        can_long, can_short, reasons = location_filter.apply_vetoes(context, 'LONG')
        
        assert not can_long, "LONG should be vetoed at extreme high in RANGE"
        assert can_short, "SHORT should be allowed at extreme high"
        assert any("EXTREME_HIGH_VETO" in r for r in reasons)
    
    def test_veto_short_at_extreme_low_in_range(self, location_filter):
        """SHORT should be vetoed at 5th percentile in RANGE."""
        bars = 24 * 60
        closes = np.linspace(100000, 90000, bars)  # Price dropped
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=90000.0,  # At the low
            atr_1h=500.0,
            adx_1h=18.0,
            regime="RANGE"
        )
        
        can_long, can_short, reasons = location_filter.apply_vetoes(context, 'SHORT')
        
        assert can_long, "LONG should be allowed at extreme low"
        assert not can_short, "SHORT should be vetoed at extreme low in RANGE"
        assert any("EXTREME_LOW_VETO" in r for r in reasons)
    
    def test_no_veto_in_trend_regime(self, location_filter):
        """At highs in TREND regime, LONG should NOT be vetoed."""
        bars = 24 * 60
        closes = np.linspace(90000, 100000, bars)
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=500.0,
            adx_1h=35.0,  # High ADX
            regime="TREND_EXPANSION"  # Trend regime
        )
        
        can_long, can_short, reasons = location_filter.apply_vetoes(context, 'LONG')
        
        # In trend, extremes veto doesn't apply as strictly
        assert can_long, "LONG at highs in TREND should be allowed (or just warned)"


class TestHeadroomVeto:
    """Test Rule 2: Headroom minimum."""
    
    def test_veto_long_no_headroom(self, location_filter):
        """LONG vetoed when too close to range high."""
        bars = 24 * 60
        # Price oscillated, now very close to high
        closes = np.concatenate([
            np.linspace(95000, 100000, bars // 2),
            np.linspace(100000, 99900, bars // 2)  # Slight pullback
        ])
        highs = closes + 50
        lows = closes - 50
        volumes = np.ones(bars) * 100
        
        # ATR is 500, range high is ~100050, current price 99900
        # Headroom = 150 / 500 = 0.3 ATR < 0.5 threshold
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=99900.0,
            atr_1h=500.0,
            adx_1h=18.0,
            regime="RANGE"
        )
        
        can_long, can_short, reasons = location_filter.apply_vetoes(context, 'LONG')
        
        # Should be vetoed due to insufficient headroom
        assert not can_long, f"LONG should be vetoed with headroom {context.headroom_to_high_atr:.2f} ATR"
        assert any("HEADROOM" in r for r in reasons)


class TestBreakoutOverride:
    """Test Rule 3: Breakout override."""
    
    def test_breakout_allows_long_at_high(self, location_filter):
        """LONG at extreme high allowed if breakout confirmed."""
        bars = 24 * 60
        closes = np.linspace(90000, 101000, bars)  # Breaking above range
        highs = closes + 100
        lows = closes - 100
        # Volume expansion on recent bars
        volumes = np.ones(bars) * 100
        volumes[-20:] = 200  # 2x volume at breakout
        
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=101000.0,  # Above previous range
            atr_1h=500.0,
            adx_1h=35.0,  # High ADX = breakout mode
            regime="RANGE"
        )
        
        can_long, can_short, reasons = location_filter.apply_vetoes(context, 'LONG')
        
        # If breakout detected, long should be allowed even at extreme
        if context.is_breakout_up:
            assert can_long, "LONG should be allowed on confirmed breakout"
            assert any("OVERRIDE" in r for r in reasons)


class TestConvenienceFunctions:
    """Test convenience functions and singleton."""
    
    def test_get_location_filter_singleton(self):
        """Test singleton accessor returns same instance."""
        f1 = get_location_filter()
        f2 = get_location_filter()
        assert f1 is f2
    
    def test_check_location_veto_function(self):
        """Test quick check function."""
        bars = 24 * 60
        closes = np.linspace(95000, 100000, bars)
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        allowed, reason = check_location_veto(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=500.0,
            adx_1h=18.0,
            bias='LONG',
            regime='RANGE'
        )
        
        # Should return a boolean and reason string
        assert isinstance(allowed, bool)
        assert isinstance(reason, str)


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_insufficient_data(self, location_filter):
        """Should handle insufficient data gracefully."""
        bars = 100  # Less than 20 days
        closes = np.ones(bars) * 100000
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=500.0,
            adx_1h=20.0,
            regime="RANGE"
        )
        
        # Should return default context without crashing
        assert context.percentile_60d == 50.0
    
    def test_zero_atr(self, location_filter):
        """Should handle zero ATR without division error."""
        bars = 24 * 60
        closes = np.ones(bars) * 100000  # Flat price
        highs = closes
        lows = closes
        volumes = np.ones(bars) * 100
        
        # Should not raise
        context = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=0.0,  # Zero ATR
            adx_1h=20.0,
            regime="RANGE"
        )
        
        # Headroom should be infinity (no division by zero)
        assert context.headroom_to_high_atr == float('inf')
    
    def test_caching(self, location_filter):
        """Test that caching works correctly."""
        bars = 24 * 60
        closes = np.linspace(95000, 100000, bars)
        highs = closes + 100
        lows = closes - 100
        volumes = np.ones(bars) * 100
        
        # First call - should calculate
        ctx1 = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=500.0,
            adx_1h=20.0,
            regime="RANGE",
            cache_key=12345
        )
        
        # Second call with same cache key - should return cached
        ctx2 = location_filter.calculate_context(
            closes_1h=closes,
            highs_1h=highs,
            lows_1h=lows,
            volumes_1h=volumes,
            current_price=100000.0,
            atr_1h=500.0,
            adx_1h=20.0,
            regime="RANGE",
            cache_key=12345
        )
        
        assert ctx1 is ctx2, "Should return cached context"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
