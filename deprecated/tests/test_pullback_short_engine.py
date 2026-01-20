"""
Tests for Pullback Short Engine

Tests the tactical short system for capturing corrections in bullish markets.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch

from engine.pullback_short_engine import (
    PullbackShortEngine,
    PullbackShortSignal,
    PullbackGate,
    GateResult,
    get_pullback_short_engine,
    reset_pullback_short_engine
)
from engine.config import PullbackShortConfig, TradeType
from engine.indicators import (
    calculate_extension_z,
    detect_swing_high,
    detect_swing_low,
    detect_breakout_impulse,
    calculate_activity_score
)


@pytest.fixture
def engine():
    """Create a fresh PullbackShortEngine for each test."""
    reset_pullback_short_engine()
    return PullbackShortEngine()


@pytest.fixture
def sample_1h_data():
    """Generate sample 1H OHLCV data with uptrend (extension scenario)."""
    np.random.seed(42)
    n = 100
    
    # Create uptrending price action
    base_price = 90000
    trend = np.linspace(0, 8000, n)  # Strong uptrend
    noise = np.random.randn(n) * 200
    
    closes = base_price + trend + noise
    highs = closes + np.abs(np.random.randn(n) * 300)
    lows = closes - np.abs(np.random.randn(n) * 300)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volumes = np.random.uniform(100, 500, n)
    
    return pd.DataFrame({
        'timestamp': pd.date_range(start='2026-01-01', periods=n, freq='1h'),
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })


@pytest.fixture
def sample_5m_data():
    """Generate sample 5M OHLCV data with pullback scenario."""
    np.random.seed(42)
    n = 60
    
    # Create price that goes up then pulls back
    base_price = 97000
    up_phase = np.linspace(0, 1500, n // 2)
    down_phase = np.linspace(1500, 800, n // 2)
    trend = np.concatenate([up_phase, down_phase])
    noise = np.random.randn(n) * 50
    
    closes = base_price + trend + noise
    highs = closes + np.abs(np.random.randn(n) * 100)
    lows = closes - np.abs(np.random.randn(n) * 100)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volumes = np.random.uniform(50, 200, n)
    
    return pd.DataFrame({
        'timestamp': pd.date_range(start='2026-01-14 00:00', periods=n, freq='5min'),
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })


class TestIndicators:
    """Test the new indicator functions."""
    
    def test_calculate_extension_z_positive(self):
        """Test extension Z when price is above EMA."""
        ema = np.array([95000, 95500, 96000, 96500, 97000])
        atr = np.array([500, 500, 500, 500, 500])
        current_price = 98000
        
        ext_z = calculate_extension_z(current_price, ema, atr)
        
        # (98000 - 97000) / 500 = 2.0
        assert ext_z == pytest.approx(2.0, rel=0.01)
    
    def test_calculate_extension_z_negative(self):
        """Test extension Z when price is below EMA."""
        ema = np.array([97000])
        atr = np.array([500])
        current_price = 95500
        
        ext_z = calculate_extension_z(current_price, ema, atr)
        
        # (95500 - 97000) / 500 = -3.0
        assert ext_z == pytest.approx(-3.0, rel=0.01)
    
    def test_calculate_extension_z_handles_empty(self):
        """Test extension Z with empty arrays."""
        ext_z = calculate_extension_z(100000, np.array([]), np.array([]))
        assert ext_z == 0.0
    
    def test_detect_swing_high_found(self):
        """Test swing high detection when present."""
        highs = np.array([100, 105, 110, 108, 103, 101, 99])
        lows = np.array([95, 100, 105, 103, 98, 96, 94])
        closes = np.array([98, 103, 108, 105, 100, 98, 96])
        
        found, swing_high, candles_ago = detect_swing_high(highs, lows, closes, lookback=2)
        
        assert found == True
        assert swing_high == 110
        assert candles_ago == 4  # 4 candles from end
    
    def test_detect_swing_high_not_found(self):
        """Test swing high detection when not present (monotonic)."""
        highs = np.array([100, 102, 104, 106, 108, 110, 112])
        lows = highs - 5
        closes = highs - 2
        
        found, swing_high, candles_ago = detect_swing_high(highs, lows, closes, lookback=2)
        
        assert found == False
        assert swing_high == 0.0
    
    def test_detect_breakout_impulse_detected(self):
        """Test breakout impulse detection."""
        # Create a strong bullish candle with clear volume expansion
        # Need at least 20 candles for volume MA calculation
        n = 25
        base_price = 97000
        
        opens = np.full(n, base_price)
        highs = np.full(n, base_price + 200)
        lows = np.full(n, base_price - 100)
        closes = np.full(n, base_price + 100)
        volumes = np.full(n, 100.0)
        
        # Last candle is a strong breakout
        opens[-1] = 97000
        highs[-1] = 97800
        lows[-1] = 96900
        closes[-1] = 97750  # Close near high (750 of 900 range = 83%)
        volumes[-1] = 300   # Volume 3x average
        
        atr = 500  # Body = 750, which is > 0.6 * 500 = 300
        
        is_breakout, details = detect_breakout_impulse(
            opens, highs, lows, closes, volumes, atr,
            body_atr_ratio=0.6,
            close_near_high_ratio=0.8,
            volume_expansion_ratio=1.5
        )
        
        # Body = 750 > 300 ✓
        # Close position = (97750 - 96900) / 900 = 0.944 > 0.8 ✓
        # Volume = 300, avg of last 20 ≈ 110 → ratio ≈ 2.7 > 1.5 ✓
        assert is_breakout == True
        assert details['is_impulse'] == True
        assert details['is_bullish_close'] == True
        assert details['is_volume_expansion'] == True
    
    def test_detect_breakout_impulse_not_detected(self):
        """Test no breakout on normal candle."""
        opens = np.array([97000, 97050, 97100])
        highs = np.array([97100, 97150, 97200])
        lows = np.array([96950, 97000, 97050])
        closes = np.array([97050, 97100, 97120])  # Small body
        volumes = np.array([100, 105, 110])  # Normal volume
        atr = 500
        
        is_breakout, details = detect_breakout_impulse(
            opens, highs, lows, closes, volumes, atr
        )
        
        assert is_breakout == False
    
    def test_calculate_activity_score(self):
        """Test activity score calculation."""
        # High activity scenario
        score = calculate_activity_score(
            atr_current=600,
            atr_avg=400,
            volume_current=200,
            volume_avg=100
        )
        
        # ATR ratio: 600/400 = 1.5 → 0.75 (capped at 2)
        # Volume ratio: 200/100 = 2.0 → 1.0 (capped)
        # Average ≈ 0.875
        assert 0.5 < score <= 1.0
    
    def test_calculate_activity_score_dead_market(self):
        """Test activity score in dead market."""
        score = calculate_activity_score(
            atr_current=100,
            atr_avg=400,
            volume_current=50,
            volume_avg=200
        )
        
        # Low ratios should give low score
        assert score < 0.5


class TestPullbackShortEngine:
    """Test the PullbackShortEngine."""
    
    def test_is_enabled_when_disabled(self, engine):
        """Test engine reports disabled when config is off."""
        # PullbackShortConfig.ENABLED is False by default
        is_enabled, reason = engine.is_enabled()
        
        assert is_enabled == False
        assert "ENABLED is False" in reason
    
    def test_is_enabled_respects_daily_limit(self, engine):
        """Test daily trade limit enforcement."""
        # Force enable
        with patch.object(PullbackShortConfig, 'ENABLED', True):
            engine._daily_trade_count = 10
            
            market_time = datetime.now(UTC)
            is_enabled, reason = engine.is_enabled(market_time=market_time)
            
            assert is_enabled == False
            assert "Daily limit" in reason
    
    def test_is_enabled_respects_consecutive_losses(self, engine):
        """Test consecutive losses limit."""
        with patch.object(PullbackShortConfig, 'ENABLED', True):
            engine._consecutive_losses = 5
            
            is_enabled, reason = engine.is_enabled()
            
            assert is_enabled == False
            assert "Consecutive losses" in reason
    
    def test_evaluate_requires_long_htf_bias(self, engine, sample_1h_data, sample_5m_data):
        """Test that evaluation requires HTF bias LONG."""
        with patch.object(PullbackShortConfig, 'ENABLED', True):
            signal = engine.evaluate(
                current_price=97500,
                df_1h=sample_1h_data,
                df_5m=sample_5m_data,
                df_1m=None,
                htf_bias='SHORT',  # Not LONG
                regime='RANGE',
                market_time=datetime.now(UTC)
            )
            
            assert signal.valid == False
            assert "not LONG" in signal.block_reason
    
    def test_record_trade_result_win(self, engine):
        """Test recording a winning trade."""
        market_time = datetime.now(UTC)
        engine._last_trade_date = market_time.date()
        
        engine.record_trade_result(pnl=100.0, is_winner=True, market_time=market_time)
        
        assert engine._daily_trade_count == 1
        assert engine._consecutive_losses == 0
    
    def test_record_trade_result_loss(self, engine):
        """Test recording a losing trade increments loss counter."""
        market_time = datetime.now(UTC)
        engine._last_trade_date = market_time.date()
        
        engine.record_trade_result(pnl=-50.0, is_winner=False, market_time=market_time)
        
        assert engine._daily_trade_count == 1
        assert engine._consecutive_losses == 1
    
    def test_daily_reset(self, engine):
        """Test daily counter reset."""
        market_time = datetime.now(UTC)
        engine._daily_trade_count = 5
        engine._consecutive_losses = 2
        
        engine.reset_daily(market_time)
        
        assert engine._daily_trade_count == 0
        assert engine._consecutive_losses == 0
    
    def test_get_status(self, engine):
        """Test status reporting."""
        market_time = datetime.now(UTC)
        engine._daily_trade_count = 2
        engine._consecutive_losses = 1
        engine._last_swing_high = 97500.0
        
        status = engine.get_status(market_time)
        
        assert status['daily_trades'] == 2
        assert status['consecutive_losses'] == 1
        assert status['last_swing_high'] == 97500.0


class TestGateResults:
    """Test gate result dataclass."""
    
    def test_gate_result_creation(self):
        """Test creating a gate result."""
        result = GateResult(
            passed=True,
            gate=PullbackGate.EXTENSION,
            reason="ext_z=2.1 vs min=1.8",
            data={'extension_z': 2.1}
        )
        
        assert result.passed == True
        assert result.gate == PullbackGate.EXTENSION
        assert result.data['extension_z'] == 2.1


class TestPullbackShortSignal:
    """Test signal dataclass."""
    
    def test_signal_defaults(self):
        """Test signal has correct defaults."""
        signal = PullbackShortSignal(
            valid=False,
            entry_price=0.0,
            stop_loss=0.0,
            tp1_price=0.0,
            tp2_price=None,
            size_factor=0.3,
            swing_high=0.0,
            swing_high_index=0,
            confirm_level=0.0,
            extension_z=0.0,
            trigger_type="",
            activity_score=0.0
        )
        
        assert signal.valid == False
        assert signal.trade_type == TradeType.SHORT_PULLBACK
        assert signal.size_factor == 0.3
        assert signal.entry_mode == "RETEST"
        assert signal.confirm_level == 0.0
        assert len(signal.gate_results) == 0


class TestConfig:
    """Test configuration helpers."""
    
    def test_get_extension_z_min_trend(self):
        """Test extension Z threshold for trend regime."""
        threshold = PullbackShortConfig.get_extension_z_min("TREND")
        assert threshold == 2.3
    
    def test_get_extension_z_min_range(self):
        """Test extension Z threshold for range regime."""
        threshold = PullbackShortConfig.get_extension_z_min("RANGE")
        assert threshold == 1.8
    
    def test_get_tp_k_dead_market(self):
        """Test TP k-factor in dead market."""
        k = PullbackShortConfig.get_tp_k(activity_score=0.0, is_tp2=False)
        assert k == PullbackShortConfig.TP1_K_ATR_DEAD
    
    def test_get_tp_k_active_market(self):
        """Test TP k-factor in active market."""
        k = PullbackShortConfig.get_tp_k(activity_score=1.0, is_tp2=False)
        assert k == PullbackShortConfig.TP1_K_ATR_ACTIVE
    
    def test_get_tp_k_interpolation(self):
        """Test TP k-factor interpolation."""
        k = PullbackShortConfig.get_tp_k(activity_score=0.5, is_tp2=False)
        expected = (PullbackShortConfig.TP1_K_ATR_DEAD + PullbackShortConfig.TP1_K_ATR_ACTIVE) / 2
        assert k == pytest.approx(expected, rel=0.01)


class TestSingleton:
    """Test singleton pattern."""
    
    def test_get_pullback_short_engine_singleton(self):
        """Test singleton returns same instance."""
        reset_pullback_short_engine()
        
        engine1 = get_pullback_short_engine()
        engine2 = get_pullback_short_engine()
        
        assert engine1 is engine2
    
    def test_reset_pullback_short_engine(self):
        """Test reset creates new instance."""
        engine1 = get_pullback_short_engine()
        reset_pullback_short_engine()
        engine2 = get_pullback_short_engine()
        
        assert engine1 is not engine2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
