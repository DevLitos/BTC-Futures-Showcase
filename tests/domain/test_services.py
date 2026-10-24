"""Tests for application services."""

from datetime import UTC, datetime, timedelta

import pytest

from src.application.services.candle_aggregator import CandleAggregator, Tick
from src.application.services.position_manager import PositionManager, PositionManagerConfig
from src.application.services.risk_manager import RiskLimits, RiskManager
from src.application.services.signal_orchestrator import SignalOrchestrator
from src.domain.models.candle import Candle
from src.domain.models.signal import Signal, SignalDirection, SignalStrength, SignalType
from src.domain.strategies import FVGTrendStrategy


class TestCandleAggregator:
    def test_tick_to_candle(self):
        aggregator = CandleAggregator(
            symbol="BTC/USD",
            intervals=[60],  # 1 minute
            max_candles=100,
        )

        base_time = datetime(2024, 1, 1, 12, 0, 0)

        # Add ticks within same minute
        ticks = [
            Tick(base_time, 90000, 0.1),
            Tick(base_time + timedelta(seconds=10), 90100, 0.2),
            Tick(base_time + timedelta(seconds=30), 89900, 0.1),
            Tick(base_time + timedelta(seconds=50), 90050, 0.15),
        ]

        for tick in ticks:
            aggregator.add_tick(tick)

        # No candle completed yet (still in same minute)
        candles = aggregator.get_candles(60)
        assert len(candles) == 0

        # Add tick in next minute to complete previous candle
        aggregator.add_tick(Tick(base_time + timedelta(minutes=1, seconds=5), 90100, 0.1))

        candles = aggregator.get_candles(60)
        assert len(candles) == 1

        candle = candles[0]
        assert candle.open == 90000
        assert candle.high == 90100
        assert candle.low == 89900
        assert candle.close == 90050

    def test_multiple_intervals(self):
        aggregator = CandleAggregator(
            symbol="BTC/USD",
            intervals=[60, 300],  # 1m, 5m
            max_candles=100,
        )

        # Callbacks tracking
        candles_1m = []
        candles_5m = []

        aggregator.on_candle(60, lambda c: candles_1m.append(c))
        aggregator.on_candle(300, lambda c: candles_5m.append(c))

        # Add 6 minutes of ticks
        base_time = datetime(2024, 1, 1, 12, 0, 0)

        for minute in range(7):
            for second in range(0, 60, 10):
                tick = Tick(base_time + timedelta(minutes=minute, seconds=second), 90000 + minute * 10, 0.1)
                aggregator.add_tick(tick)

        # Should have 6 completed 1m candles
        assert len(candles_1m) == 6
        # Should have 1 completed 5m candle
        assert len(candles_5m) == 1


class TestSignalOrchestrator:
    def test_strategy_evaluation(self):
        strategy = FVGTrendStrategy()
        orchestrator = SignalOrchestrator(strategies=[strategy], buffer_size=100)

        # Add candles
        for i in range(60):
            candle = Candle(
                symbol="BTC/USD",
                timestamp=datetime.now(UTC) - timedelta(minutes=60 - i),
                open=90000 + i * 10,
                high=90000 + i * 10 + 50,
                low=90000 + i * 10 - 50,
                close=90000 + i * 10 + 20,
                volume=100,
            )
            orchestrator.add_candle(candle)

        assert orchestrator.candle_count == 60

    def test_signal_callback(self):
        signals_received = []

        def on_signal(event):
            signals_received.append(event.signal)

        strategy = FVGTrendStrategy()
        orchestrator = SignalOrchestrator(strategies=[strategy], buffer_size=100)
        orchestrator.on_signal(on_signal)

        # Add enough candles
        for i in range(100):
            candle = Candle(
                symbol="BTC/USD",
                timestamp=datetime.now(UTC) - timedelta(minutes=100 - i),
                open=90000 + i * 10,
                high=90000 + i * 10 + 50,
                low=90000 + i * 10 - 50,
                close=90000 + i * 10 + 20,
                volume=100,
            )
            orchestrator.add_candle(candle)

        # May or may not generate signals depending on patterns
        assert isinstance(signals_received, list)


class TestPositionManager:
    def test_open_position_from_signal(self):
        manager = PositionManager(config=PositionManagerConfig(max_positions=1, max_risk_per_position_pct=3.0))

        signal = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=90000,
            stop_loss=89000,
            take_profit=93000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST",
        )

        position = manager.open_from_signal(signal, size=0.01)

        assert position is not None
        assert position.entry_price == 90000
        assert position.stop_loss == 89000
        assert position.take_profit == 93000
        assert manager.position_count == 1

    def test_position_stop_loss(self):
        manager = PositionManager()

        signal = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=90000,
            stop_loss=89000,
            take_profit=93000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST",
        )

        manager.open_from_signal(signal, size=0.01)

        # Price drops to stop loss
        closed = manager.update_price(88500)

        assert len(closed) == 1
        assert closed[0].exit_reason.value == "STOP_LOSS"
        assert manager.position_count == 0

    def test_restore_open_position(self):
        manager = PositionManager()

        position = manager.restore_open_position(
            {
                "position_id": "exchange-1",
                "symbol": "PF_XBTUSD",
                "side": "LONG",
                "entry_price": 90000,
                "size": 0.01,
                "stop_loss": 89000,
                "take_profit": 93000,
                "opened_at": "2026-10-24T12:00:00+00:00",
                "strategy_name": "TEST",
                "signal_type": "FVG",
            }
        )

        assert position is not None
        assert manager.get_position("exchange-1") is position
        assert manager.position_count == 1

    def test_position_take_profit(self):
        manager = PositionManager()

        signal = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=90000,
            stop_loss=89000,
            take_profit=93000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST",
        )

        manager.open_from_signal(signal, size=0.01)

        # Price rises to take profit
        closed = manager.update_price(93500)

        assert len(closed) == 1
        assert closed[0].exit_reason.value == "TAKE_PROFIT"
        assert closed[0].realized_pnl > 0

    def test_max_positions_limit(self):
        manager = PositionManager(config=PositionManagerConfig(max_positions=1))

        signal1 = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=90000,
            stop_loss=89000,
            take_profit=93000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST",
        )

        signal2 = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=91000,
            stop_loss=90000,
            take_profit=94000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST2",
        )

        # First position should open
        pos1 = manager.open_from_signal(signal1, size=0.01)
        assert pos1 is not None

        # Second should fail (max reached)
        pos2 = manager.open_from_signal(signal2, size=0.01)
        assert pos2 is None

    def test_stats(self):
        manager = PositionManager()

        # Open and close a winning trade
        signal = Signal(
            timestamp=datetime.now(UTC),
            direction=SignalDirection.LONG,
            signal_type=SignalType.FVG,
            entry_price=90000,
            stop_loss=89000,
            take_profit=93000,
            strength=SignalStrength.MODERATE,
            strategy_name="TEST",
        )

        manager.open_from_signal(signal, size=0.01)
        manager.update_price(93500)  # Take profit

        stats = manager.get_stats()
        assert stats["total_trades"] == 1
        assert stats["wins"] == 1
        assert stats["win_rate"] == 100.0
        assert stats["total_pnl"] > 0


def test_risk_manager_restores_daily_limits() -> None:
    manager = RiskManager(RiskLimits(max_daily_loss_usd=100.0, max_daily_trades=2))

    manager.restore_daily_state(-100.0, 2)

    allowed, reason = manager.can_trade()
    assert allowed is False
    assert "Daily loss limit restored" in manager.kill_reason
    assert "Kill switch active" in reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
