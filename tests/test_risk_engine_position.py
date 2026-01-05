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


def test_trailing_stop_uses_three_phases():
    # Use a fixed market_time reference
    base_time = datetime.now(UTC)
    
    pos = Position("BTC/USDT:USDT", "LONG", entry_price=100.0, size=1.0, stop_loss=50.0, atr_1m=2.0, market_time=base_time)
    pos.tp1_hit = True

    # RELAXED TRAILING (Jan 4, 2026): Wider stops to let winners run
    # Phase 1 (<5min) - now uses 2.5x ATR (was 1.5x)
    pos.open_time = base_time - timedelta(minutes=2)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=120.0, atr_1m=2.0, market_time=base_time)
    assert pos.trailing_stop == pytest.approx(120.0 - (2.0 * 2.5))

    # Phase 2 (5-20min) - now uses 2.5x ATR (was 2.0x)
    pos.open_time = base_time - timedelta(minutes=10)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=120.0, atr_1m=2.0, market_time=base_time)
    assert pos.trailing_stop == pytest.approx(120.0 - (2.0 * 2.5))

    # Phase 3 (20min+) - now uses 3.0x ATR (was 2.5x)
    pos.open_time = base_time - timedelta(minutes=30)
    pos.trailing_stop = 0.0
    pos.update_trailing_stop(current_price=120.0, atr_1m=2.0, market_time=base_time)
    assert pos.trailing_stop == pytest.approx(120.0 - (2.0 * 3.0))


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
