import types
from datetime import datetime as real_datetime

import numpy as np
import pandas as pd
import pytest

from tests.conftest import run_async

import engine.advanced_filters as af


class _FakeDateTime:
    """Patch target for engine.advanced_filters.datetime (imported symbol)."""

    def __init__(self, fixed: real_datetime):
        self._fixed = fixed

    def now(self):
        return self._fixed

    def __getattr__(self, name):
        # Delegate anything else (e.g., .weekday) if needed
        return getattr(real_datetime, name)


def test_check_funding_rate_safe_blocks_long(monkeypatch, fake_external_data):
    monkeypatch.setattr(af.FundingRateControl, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.FundingRateControl, "MAX_FUNDING_RATE_PCT", 0.03, raising=False)

    fake_external_data.funding_rate = 0.001  # 0.1% (decimal)

    ok, reason = run_async(af.check_funding_rate_safe(fake_external_data, "BTC/USDT:USDT", "LONG"))
    assert ok is False
    assert "Funding" in reason


def test_check_funding_rate_safe_allows_short(monkeypatch, fake_external_data):
    monkeypatch.setattr(af.FundingRateControl, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.FundingRateControl, "MAX_FUNDING_RATE_PCT", 0.03, raising=False)

    fake_external_data.funding_rate = 0.01

    ok, reason = run_async(af.check_funding_rate_safe(fake_external_data, "BTC/USDT:USDT", "SHORT"))
    assert ok is True
    assert reason == ""


def test_should_exit_on_funding_triggers(monkeypatch, fake_external_data):
    monkeypatch.setattr(af.FundingRateControl, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.FundingRateControl, "FORCE_EXIT_FUNDING_PCT", 0.05, raising=False)

    fake_external_data.funding_rate = 0.001  # 0.1%

    should_exit, reason = run_async(af.should_exit_on_funding(fake_external_data, "BTC/USDT:USDT", "LONG"))
    assert should_exit is True
    assert "CRITICAL" in reason


def test_check_session_quality_blocks_weekend(monkeypatch):
    monkeypatch.setattr(af.SessionQuality, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.SessionQuality, "WEEKEND_TRADING", False, raising=False)
    monkeypatch.setattr(af.SessionQuality, "MIN_ADX_BY_SESSION", {"london": 10, "newyork": 10, "asia": 10}, raising=False)
    monkeypatch.setattr(af.SessionQuality, "MIN_VOLATILITY_BPS", {"london": 1, "newyork": 1, "asia": 1}, raising=False)

    # Saturday 12:00 UTC
    monkeypatch.setattr(af, "datetime", _FakeDateTime(real_datetime(2026, 1, 3, 12, 0, 0)))

    ok, reason = af.check_session_quality(adx=50, volatility_bps=100)
    assert ok is False
    assert "Weekend" in reason


def test_check_session_quality_blocks_low_adx(monkeypatch):
    monkeypatch.setattr(af.SessionQuality, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.SessionQuality, "WEEKEND_TRADING", True, raising=False)
    monkeypatch.setattr(af.SessionQuality, "MIN_ADX_BY_SESSION", {"london": 25, "newyork": 25, "asia": 25}, raising=False)
    monkeypatch.setattr(af.SessionQuality, "MIN_VOLATILITY_BPS", {"london": 10, "newyork": 10, "asia": 10}, raising=False)

    # Monday 10:00 UTC (London)
    monkeypatch.setattr(af, "datetime", _FakeDateTime(real_datetime(2026, 1, 5, 10, 0, 0)))

    ok, reason = af.check_session_quality(adx=10, volatility_bps=100)
    assert ok is False
    assert "ADX" in reason


def test_check_htf_divergence_detects_bearish(monkeypatch):
    monkeypatch.setattr(af.DivergenceFilters, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.DivergenceFilters, "RSI_PERIOD", 14, raising=False)
    monkeypatch.setattr(af.DivergenceFilters, "LOOKBACK_PERIODS", 20, raising=False)

    # Create df where last high > previous highs
    highs = np.array([100 + i for i in range(40)], dtype=float)
    highs[-1] = highs[-2] + 10
    closes = highs.copy()
    lows = highs - 5
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    # Force RSI: last RSI lower than previous max
    rsi = np.linspace(70, 50, num=len(df))
    monkeypatch.setattr(af, "calculate_rsi", lambda closes, period: rsi)

    detected, reason = af.check_htf_divergence(df, "LONG")
    assert detected is True
    assert "Bearish divergence" in reason


def test_vpoc_calculate_and_validity(monkeypatch):
    monkeypatch.setattr(af.VPOCFilters, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.VPOCFilters, "VPOC_LOOKBACK", 30, raising=False)
    monkeypatch.setattr(af.VPOCFilters, "PRICE_BIN_SIZE", 10.0, raising=False)

    # Cluster volume around ~1000
    prices = np.linspace(900, 1100, 60)
    df = pd.DataFrame(
        {
            "high": prices + 5,
            "low": prices - 5,
            "close": prices,
            "volume": np.where((prices > 990) & (prices < 1010), 1000.0, 10.0),
        }
    )

    vpoc = af.calculate_vpoc(df)
    assert vpoc is not None

    ok, reason = af.check_vpoc_validity(current_price=float(vpoc) + (vpoc * 0.05), vpoc=vpoc, bias="LONG")
    assert ok is False
    assert "VPOC" in reason


def test_check_btc_trend_blocks_long(monkeypatch, fake_external_data):
    monkeypatch.setattr(af.CorrelationFilters, "ENABLED", True, raising=False)
    monkeypatch.setattr(af.CorrelationFilters, "BTC_TREND_ENABLED", True, raising=False)
    monkeypatch.setattr(af.CorrelationFilters, "BTC_TIMEFRAME", "1h", raising=False)
    monkeypatch.setattr(af.CorrelationFilters, "BTC_EMA_PERIOD", 20, raising=False)

    # Force EMA above price so trend is DOWN
    monkeypatch.setattr(af, "calculate_ema", lambda closes, period: np.array([closes[-1] + 100.0]))

    ok, reason = run_async(af.check_btc_trend(fake_external_data, "LONG"))
    assert ok is False
    assert "BTC downtrend" in reason
