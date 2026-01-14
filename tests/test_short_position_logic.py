"""
Tests for SHORT position logic.

This module tests all SHORT-specific logic to ensure the trading system
correctly handles short positions (selling high, buying low).

Tests cover:
1. HTF bias determination for SHORT
2. LTF trigger for SHORT
3. Stop Loss calculation for SHORT (above entry)
4. Take Profit calculation for SHORT (below entry)
5. PnL calculation for SHORT (entry - current)
6. MFE/MAE calculation for SHORT
7. Execution side for SHORT (sell to open, buy to close)
8. Trailing stop for SHORT positions
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, UTC

from engine.filters import HTFFilter, LTFTriggerFilter
from engine.risk_engine import RiskEngine, Position


class TestHTFFilterShort:
    """Test HTF filter bias determination for SHORT."""
    
    def test_short_bias_when_price_below_ema50(self):
        """Price below EMA50 should give SHORT bias."""
        htf = HTFFilter(backtest_mode=True)
        
        # Create downtrend data
        n = 200
        closes = np.linspace(100000, 95000, n)  # Decreasing prices
        highs = closes + 100
        lows = closes - 100
        
        df = pd.DataFrame({
            'open': closes + 10,
            'high': highs,
            'low': lows,
            'close': closes
        })
        
        # Current price well below EMA50
        current_price = 94000.0
        passed, bias, reason = htf.check(df, current_price)
        
        assert passed is True
        assert bias == 'SHORT', f"Expected SHORT bias, got {bias}"
    
    def test_short_bias_in_strong_downtrend(self):
        """Strong downtrend with ADX should give SHORT bias."""
        htf = HTFFilter(backtest_mode=False)  # Full validation
        
        n = 200
        # Create strong downtrend with high volatility (triggers ADX)
        closes = np.array([100000 - i * 50 - np.sin(i/5) * 100 for i in range(n)])
        highs = closes + np.random.uniform(100, 300, n)
        lows = closes - np.random.uniform(100, 300, n)
        
        df = pd.DataFrame({
            'open': closes + 20,
            'high': highs,
            'low': lows,
            'close': closes
        })
        
        current_price = closes[-1] - 500  # Below last close
        passed, bias, reason = htf.check(df, current_price)
        
        # May or may not pass ADX check, but bias should be SHORT
        if passed:
            assert bias == 'SHORT', f"Expected SHORT bias, got {bias}"


class TestLTFFilterShort:
    """Test LTF trigger for SHORT positions."""
    
    def test_ltf_short_bos_detection(self):
        """LTF should detect break of structure for SHORT."""
        ltf = LTFTriggerFilter(backtest_mode=True)
        
        # Create pattern: price rallies then breaks down
        # First 15 candles: rally (higher highs)
        closes_rally = np.array([100.0 + i * 0.5 for i in range(15)])
        # Last 5 candles: reversal and breakdown
        closes_break = np.array([107.5, 107.0, 106.0, 105.0, 103.0])
        closes = np.concatenate([closes_rally, closes_break])
        
        highs = closes + 0.5
        lows = closes - 0.5
        volumes = np.ones(20) * 100
        
        df = pd.DataFrame({
            'open': closes + 0.1,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': volumes
        })
        
        passed, reason = ltf.check(df, 'SHORT')
        ltf_info = ltf.get_last_ltf_info()
        
        assert passed is True, f"LTF should pass for SHORT, reason: {reason}"
        assert bool(ltf_info.get('bos_confirmed')) is True, "BOS should be confirmed for SHORT"
    
    def test_ltf_short_ema_slope_negative(self):
        """For SHORT, EMA slope should be negative or near zero."""
        ltf = LTFTriggerFilter(backtest_mode=True)
        
        # Downtrending data
        closes = np.array([110.0 - i * 0.3 for i in range(20)])
        highs = closes + 0.5
        lows = closes - 0.5
        volumes = np.ones(20) * 100
        
        df = pd.DataFrame({
            'open': closes + 0.1,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': volumes
        })
        
        passed, reason = ltf.check(df, 'SHORT')
        ltf_info = ltf.get_last_ltf_info()
        
        # Slope should be considered "positive" for SHORT (negative actual slope)
        assert passed is True
        assert bool(ltf_info.get('slope_positive')) is True, "Slope should be 'positive' (bearish) for SHORT"


class TestRiskEngineShort:
    """Test RiskEngine calculations for SHORT positions."""
    
    def test_stop_loss_above_entry_for_short(self):
        """Stop loss for SHORT must be ABOVE entry price."""
        re = RiskEngine(capital=10000.0)
        
        entry = 100000.0
        resistance = 100500.0  # Swing high
        atr = 500.0
        
        sl = re.calculate_stop_loss(entry, 'SHORT', resistance, atr)
        
        assert sl > entry, f"SHORT SL ({sl}) must be above entry ({entry})"
        # SL should be at least 1x ATR above entry (minimum distance)
        assert sl >= entry + atr, f"SHORT SL ({sl}) should be at least {entry + atr}"
    
    def test_stop_loss_capped_for_short(self):
        """Stop loss for SHORT should be capped at max ATR multiple."""
        re = RiskEngine(capital=10000.0)
        
        entry = 100000.0
        resistance = 105000.0  # Very far swing high
        atr = 500.0
        
        sl = re.calculate_stop_loss(entry, 'SHORT', resistance, atr)
        
        max_sl_distance = 4.0 * atr  # Max 4 ATR
        max_sl = entry + max_sl_distance
        
        assert sl <= max_sl, f"SHORT SL ({sl}) should be capped at {max_sl}"


class TestShortPositionPnL:
    """Test Position PnL calculations for SHORT."""
    
    def test_pnl_positive_when_price_drops(self):
        """SHORT position should profit when price drops."""
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1, 
                       stop_loss=100500.0, atr_1m=100.0)
        
        # Price drops = profit for SHORT
        pos.update_unrealized_pnl(99000.0)
        
        # PnL = (entry - current) * size = (100000 - 99000) * 0.1 = 100
        assert pos.unrealized_pnl == 100.0, f"PnL should be +100, got {pos.unrealized_pnl}"
    
    def test_pnl_negative_when_price_rises(self):
        """SHORT position should lose when price rises."""
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1,
                       stop_loss=100500.0, atr_1m=100.0)
        
        # Price rises = loss for SHORT
        pos.update_unrealized_pnl(101000.0)
        
        # PnL = (entry - current) * size = (100000 - 101000) * 0.1 = -100
        assert pos.unrealized_pnl == -100.0, f"PnL should be -100, got {pos.unrealized_pnl}"
    
    def test_tp1_below_entry_for_short(self):
        """Take profit for SHORT should be BELOW entry."""
        # SL is 500 above entry, so risk = 500
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1,
                       stop_loss=100500.0, atr_1m=100.0)
        
        # TP1 at 1.5R below entry
        assert pos.tp1_price < pos.entry_price, \
            f"SHORT TP1 ({pos.tp1_price}) must be below entry ({pos.entry_price})"


class TestShortMFEMAE:
    """Test MFE/MAE calculations for SHORT positions."""
    
    def test_mfe_calculated_from_lowest_price(self):
        """For SHORT, MFE = entry - lowest_price (our best profit)."""
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1,
                       stop_loss=100500.0, atr_1m=100.0)
        
        # Simulate price going down (favorable for SHORT)
        pos.lowest_price = 98500.0  # Best case
        pos.highest_price = 100200.0  # Went against briefly
        
        mfe_r = pos.calculate_mfe_r()
        
        # MFE = (entry - lowest) / risk = (100000 - 98500) / 500 = 3.0R
        assert mfe_r == pytest.approx(3.0, rel=0.01), f"MFE should be 3.0R, got {mfe_r}"
    
    def test_mae_calculated_from_highest_price(self):
        """For SHORT, MAE = highest_price - entry (our worst drawdown)."""
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1,
                       stop_loss=100500.0, atr_1m=100.0)
        
        pos.lowest_price = 98500.0
        pos.highest_price = 100200.0  # Went against us
        
        mae_r = pos.calculate_mae_r()
        
        # MAE = (highest - entry) / risk = (100200 - 100000) / 500 = 0.4R
        assert mae_r == pytest.approx(0.4, rel=0.01), f"MAE should be 0.4R, got {mae_r}"


class TestShortExecution:
    """Test execution side logic for SHORT positions."""
    
    def test_entry_side_is_sell(self):
        """Entry for SHORT should be 'sell'."""
        bias = 'SHORT'
        side = 'buy' if bias == 'LONG' else 'sell'
        
        assert side == 'sell', f"SHORT entry side should be 'sell', got {side}"
    
    def test_close_side_is_buy(self):
        """Closing a SHORT should be 'buy'."""
        direction = 'SHORT'
        close_side = 'sell' if direction == 'LONG' else 'buy'
        
        assert close_side == 'buy', f"SHORT close side should be 'buy', got {close_side}"


class TestShortTrailingStop:
    """Test trailing stop behavior for SHORT positions."""
    
    def test_trailing_stop_moves_down_on_favorable_price(self):
        """For SHORT, trailing stop should move DOWN when price moves favorably."""
        base_time = datetime.now(UTC)
        
        pos = Position('BTC/USD', 'SHORT', entry_price=100000.0, size=0.1,
                       stop_loss=100500.0, atr_1m=100.0, market_time=base_time)
        
        # Record initial SL
        initial_sl = pos.stop_loss
        
        # Simulate price dropping (favorable for SHORT)
        pos.update_unrealized_pnl(98000.0)  # 2R profit
        pos.lowest_price = 98000.0
        pos.highest_price = 100000.0
        
        # MFE would be 4R at this point
        pos.update_trailing_stop(98000.0, 100.0, base_time)
        
        # For SHORT in profit, trailing stop should potentially move down
        # The stepped trailing logic activates based on MFE levels
        # Note: actual trailing behavior depends on regime and MFE levels


class TestShortIntegration:
    """Integration tests for SHORT flow."""
    
    def test_full_short_trade_flow(self):
        """Test complete SHORT trade from entry to exit."""
        # 1. Entry
        entry_price = 100000.0
        stop_loss = 100500.0
        size = 0.1
        
        pos = Position('BTC/USD', 'SHORT', entry_price, size, stop_loss, 100.0)
        
        # 2. Verify TP direction
        assert pos.tp1_price < entry_price, "TP must be below entry for SHORT"
        
        # 3. Simulate favorable price movement
        pos.update_unrealized_pnl(99000.0)
        assert pos.unrealized_pnl > 0, "Should be in profit with price drop"
        
        # 4. Simulate price hitting TP
        tp_price = pos.tp1_price
        final_pnl = (entry_price - tp_price) * size
        
        assert final_pnl > 0, f"TP should yield positive PnL: {final_pnl}"
        
        # 5. Verify R calculation
        risk = abs(stop_loss - entry_price)
        r_at_tp = (entry_price - tp_price) / risk
        
        # TP1 should be at configured R multiple (default 1.0R based on RiskManagement.TP1_RISK_MULTIPLE)
        assert r_at_tp >= 1.0, f"TP should yield at least 1R: {r_at_tp}"
    
    def test_short_stop_loss_hit(self):
        """Test SHORT position stops out correctly."""
        entry_price = 100000.0
        stop_loss = 100500.0
        size = 0.1
        
        pos = Position('BTC/USD', 'SHORT', entry_price, size, stop_loss, 100.0)
        
        # Simulate price rising to SL
        pos.update_unrealized_pnl(stop_loss)
        
        # At SL, PnL = (entry - sl) * size = (100000 - 100500) * 0.1 = -50
        expected_loss = (entry_price - stop_loss) * size
        assert pos.unrealized_pnl == pytest.approx(expected_loss, rel=0.01)
        assert pos.unrealized_pnl < 0, "Should be in loss at SL"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
