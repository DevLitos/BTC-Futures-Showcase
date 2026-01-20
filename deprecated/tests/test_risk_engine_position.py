from datetime import datetime, timedelta, UTC

import pytest

from engine.risk_engine import Position
import engine.risk_engine as re


def test_stop_hit_sweep_detection_blocks_close(monkeypatch):
    monkeypatch.setattr(re.SweepFilter, "ENABLED", True, raising=False)
    monkeypatch.setattr(re.SweepFilter, "MAX_SWEEP_DEPTH_ATR", 0.5, raising=False)

    pos = Position("BTC/USDT:USDT", "LONG", entry_price=100.0, size=1.0, stop_loss=95.0, atr_1m=1.0)

    # Price hits SL, but wick below SL is small (sweep)
    assert pos.check_stop_hit(current_price=94.9, current_low=94.8, atr_1m=1.0) is False

    # Deeper wick should count as real SL
    assert pos.check_stop_hit(current_price=94.9, current_low=94.0, atr_1m=1.0) is True


def test_trailing_stop_uses_stepped_mfe():
    """
    Test STEPPED TRAILING based on MFE levels.
    
    NOTE: With hybrid trailing enabled (default), the trailing stop uses
    a combination of R-stepped locks and ATR-adaptive trailing.
    This test disables hybrid trailing to verify legacy stepped behavior.
    
    Steps (based on MFE reached, not time or current price):
    - MFE >= 0.8R → stop = entry (breakeven)
    - MFE >= 1.0R → stop = +0.4R
    - MFE >= 1.5R → stop = +0.9R
    - MFE >= 2.2R → stop = +1.6R
    """
    # Use a fixed market_time reference
    base_time = datetime.now(UTC)
    
    # Entry at 100, SL at 50, so risk = 50 per unit
    pos = Position("BTC/USDT:USDT", "LONG", entry_price=100.0, size=1.0, stop_loss=50.0, atr_1m=2.0, market_time=base_time)
    
    # Disable hybrid trailing to test legacy stepped behavior
    pos.use_hybrid_trailing = False
    
    # MFE at 0.5R (below activation threshold) - trailing should NOT activate
    pos.highest_price = 100.0 + (50.0 * 0.5)  # 125 (0.5R MFE)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=125.0, atr_1m=2.0, market_time=base_time)
    assert pos.trailing_stop == 0.0, "Trailing should not activate below 0.8R MFE"
    
    # MFE at 1.0R - should lock +0.4R
    pos.highest_price = 100.0 + (50.0 * 1.0)  # 150 (1.0R MFE)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=150.0, atr_1m=2.0, market_time=base_time)
    expected_stop = 100.0 + (50.0 * 0.4)  # entry + 0.4R = 120
    assert pos.trailing_stop == pytest.approx(expected_stop), f"At 1.0R MFE, stop should be +0.4R = {expected_stop}"
    
    # MFE at 1.5R - should lock +0.9R
    pos.highest_price = 100.0 + (50.0 * 1.5)  # 175 (1.5R MFE)
    pos.trailing_stop = 0.0  # Reset to check if it sets to new level
    pos.update_trailing_stop(current_price=175.0, atr_1m=2.0, market_time=base_time)
    expected_stop = 100.0 + (50.0 * 0.9)  # entry + 0.9R = 145
    assert pos.trailing_stop == pytest.approx(expected_stop), f"At 1.5R MFE, stop should be +0.9R = {expected_stop}"
    
    # MFE at 2.2R - should lock +1.6R
    pos.highest_price = 100.0 + (50.0 * 2.2)  # 210 (2.2R MFE)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=210.0, atr_1m=2.0, market_time=base_time)
    expected_stop = 100.0 + (50.0 * 1.6)  # entry + 1.6R = 180
    assert pos.trailing_stop == pytest.approx(expected_stop), f"At 2.2R MFE, stop should be +1.6R = {expected_stop}"


def test_should_force_exit_by_time_max_hold(monkeypatch):
    monkeypatch.setattr(re.TimeBasedExits, "ENABLED", True, raising=False)
    monkeypatch.setattr(re.TimeBasedExits, "MAX_HOLD_MINUTES", 1, raising=False)
    monkeypatch.setattr(re.TimeBasedExits, "AVOID_OVERNIGHT", False, raising=False)
    monkeypatch.setattr(re.TimeBasedExits, "STAGNATION_EXIT_ENABLED", False, raising=False)  # Disable stagnation check

    # Use a fixed market_time reference
    base_time = datetime.now(UTC)
    
    pos = Position("BTC/USDT:USDT", "LONG", entry_price=100.0, size=1.0, stop_loss=90.0, atr_1m=1.0, market_time=base_time - timedelta(minutes=5))
    
    # Position opened 5 minutes ago, MAX_HOLD is 1 minute -> should exit
    should_exit, reason = pos.should_force_exit_by_time(market_time=base_time)
    assert should_exit is True
    assert "max hold" in reason.lower()  # Updated to match new message format
