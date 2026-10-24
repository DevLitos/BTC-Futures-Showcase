"""Backtest engine for strategy evaluation with historical data."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from src.application.backtest.data_fetcher import HistoricalDataConfig, KrakenHistoricalData
from src.application.services.position_manager import PositionManager, PositionManagerConfig
from src.application.services.signal_orchestrator import OrchestratorConfig, SignalOrchestrator
from src.domain.models.candle import Candle
from src.domain.models.position import Position
from src.domain.models.signal import Signal, SignalDirection
from src.domain.strategies import FVGTrendStrategy, IFVGStrategy, Strategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Backtest configuration.

    REALISTIC settings to match LIVE trading:
    - slippage_pct: 0.05% (conservative estimate for BTC futures)
    - Entries on NEXT candle open (simulates execution latency)
    - SL/TP evaluation considers which triggered first within candle
    """

    initial_capital: float = 10000.0
    position_size_btc: float = 0.1
    commission_pct: float = 0.04  # Kraken taker fee
    slippage_pct: float = 0.05  # Realistic slippage for BTC futures
    days: int = 30
    timeframe: str = "5m"
    # Match LIVE settings exactly
    min_signal_interval_seconds: int = 300  # Same as LIVE


@dataclass
class BacktestResult:
    """Results of a backtest run."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    sharpe_ratio: float = 0.0
    expectancy: float = 0.0
    final_capital: float = 0.0
    start_date: datetime | None = None
    end_date: datetime | None = None
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)

    def summary(self) -> str:
        """Generate summary string."""
        return f"""
========================================
        BACKTEST RESULTS
========================================
Period: {self.start_date.date() if self.start_date else "N/A"} to {self.end_date.date() if self.end_date else "N/A"}
Trades: {self.total_trades} (W:{self.winning_trades} L:{self.losing_trades})
Win Rate: {self.win_rate:.1f}%
PnL: ${self.total_pnl:,.2f}
Final Capital: ${self.final_capital:,.2f}
Max Drawdown: ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)
Profit Factor: {self.profit_factor:.2f}
Avg Win: ${self.avg_win:.2f} | Avg Loss: ${self.avg_loss:.2f}
Expectancy: ${self.expectancy:.2f} per trade
Sharpe Ratio: {self.sharpe_ratio:.2f}
========================================
"""


class BacktestEngine:
    """Run backtest with ICT strategies on historical data.

    Usage:
        engine = BacktestEngine()
        result = await engine.run(days=30)
        print(result.summary())
    """

    def __init__(self, config: BacktestConfig | None = None, strategies: list[Strategy] | None = None) -> None:
        self._config = config or BacktestConfig()

        # Only strategies with positive or near-positive expectancy
        self._strategies = strategies or [
            FVGTrendStrategy(),
            IFVGStrategy(),
            # OrderBlockStrategy() - disabled: 18% WR with negative expectancy
        ]

        self._data_fetcher = KrakenHistoricalData(
            HistoricalDataConfig(timeframe=self._config.timeframe, days_back=self._config.days)
        )

        self._orchestrator = SignalOrchestrator(
            strategies=self._strategies,
            buffer_size=200,
            config=OrchestratorConfig(
                max_signals_per_eval=1,
                min_signal_interval_seconds=self._config.min_signal_interval_seconds,  # Match LIVE
            ),
        )

        self._position_manager = PositionManager(
            config=PositionManagerConfig(
                max_positions=1,
                max_risk_per_position_pct=2.0,
                trailing_stop_activation_r=1.0,
                position_timeout_minutes=240,
            )
        )

        # State
        self._capital = self._config.initial_capital
        self._peak_capital = self._capital
        self._max_drawdown = 0.0
        self._trades: list[dict] = []
        self._equity_curve: list[tuple[datetime, float]] = []
        self._current_position: Position | None = None
        self._pending_signal: Signal | None = None  # Signal waiting for next candle

    async def run(
        self, days: int | None = None, progress_callback: Callable[[int, int], None] | None = None
    ) -> BacktestResult:
        """Run the backtest.

        REALISTIC execution:
        - Signals generated after candle close
        - Entry executed on NEXT candle open (simulates latency)
        - SL/TP checked with proper order (no lookahead)

        Args:
            days: Override days from config
            progress_callback: Called with (current, total) candles

        Returns:
            BacktestResult with all metrics
        """
        days = days or self._config.days

        logger.info("[BACKTEST] Starting %d-day REALISTIC backtest with %d strategies", days, len(self._strategies))
        logger.info(
            "[BACKTEST] Config: slippage=%.2f%%, min_interval=%ds, commission=%.2f%%",
            self._config.slippage_pct,
            self._config.min_signal_interval_seconds,
            self._config.commission_pct,
        )

        # Fetch data
        candles = await self._data_fetcher.fetch(days=days, timeframe=self._config.timeframe)
        if not candles:
            logger.error("[BACKTEST] No data fetched")
            return BacktestResult()

        await self._data_fetcher.close()

        logger.info(
            "[BACKTEST] Processing %d candles from %s to %s", len(candles), candles[0].timestamp, candles[-1].timestamp
        )

        # Reset state
        self._capital = self._config.initial_capital
        self._peak_capital = self._capital
        self._max_drawdown = 0.0
        self._trades = []
        self._equity_curve = []
        self._current_position = None
        self._pending_signal: Signal | None = None  # Signal waiting for execution
        self._orchestrator.reset()

        # Process candles
        total = len(candles)
        for i, candle in enumerate(candles):
            self._process_candle_realistic(candle, i, candles)

            # Track equity
            self._equity_curve.append((candle.timestamp, self._capital))

            # Update drawdown
            if self._capital > self._peak_capital:
                self._peak_capital = self._capital
            drawdown = self._peak_capital - self._capital
            if drawdown > self._max_drawdown:
                self._max_drawdown = drawdown

            if progress_callback and i % 100 == 0:
                progress_callback(i, total)

        # Close any open position at end
        if self._current_position:
            self._close_position(candles[-1].close, "backtest_end", candles[-1].timestamp)

        # Calculate results
        return self._calculate_results(candles)

    def _process_candle_realistic(self, candle: Candle, index: int, all_candles: list[Candle]) -> None:
        """Process candle with REALISTIC execution.

        Order of operations (matches real trading):
        1. Execute pending signal at candle OPEN (simulates latency)
        2. Check SL/TP within candle (conservative order)
        3. Generate signals for NEXT candle
        """
        # 1. Execute pending signal at THIS candle's open
        if self._pending_signal and self._current_position is None:
            self._execute_pending_signal(self._pending_signal, candle)
            self._pending_signal = None

        # 2. Check existing position for SL/TP
        if self._current_position:
            self._check_position_exit(candle)

        # 3. Generate signals for NEXT candle (only if no position)
        if self._current_position is None and self._pending_signal is None:
            signals = self._orchestrator.add_candle(candle)
            if signals:
                self._pending_signal = signals[0]  # Queue for next candle

    def _execute_pending_signal(self, signal: Signal, candle: Candle) -> None:
        """Execute signal with limit order simulation.

        Uses signal's entry_price if reachable within candle, otherwise skip.
        This simulates placing a limit order at the desired price.
        """
        desired_entry = signal.entry_price

        # Check if desired entry is reachable within this candle
        if signal.direction == SignalDirection.LONG:
            # For long, we want price to dip to our entry
            if candle.low <= desired_entry <= candle.high:
                entry_price = desired_entry * (1 + self._config.slippage_pct / 100)
            else:
                # Entry not reachable, skip this signal
                logger.debug(
                    "[BACKTEST] LONG entry %.2f not reached (candle: %.2f-%.2f)", desired_entry, candle.low, candle.high
                )
                return
        else:  # SHORT
            # For short, we want price to rise to our entry
            if candle.low <= desired_entry <= candle.high:
                entry_price = desired_entry * (1 - self._config.slippage_pct / 100)
            else:
                logger.debug(
                    "[BACKTEST] SHORT entry %.2f not reached (candle: %.2f-%.2f)",
                    desired_entry,
                    candle.low,
                    candle.high,
                )
                return

        # Commission
        commission = entry_price * self._config.position_size_btc * (self._config.commission_pct / 100)
        self._capital -= commission

        from src.domain.models.position import PositionSide

        # Recalculate SL/TP based on actual entry
        original_risk = abs(signal.entry_price - signal.stop_loss)
        original_rr = abs(signal.take_profit - signal.entry_price) / original_risk if original_risk > 0 else 2.0

        if signal.direction == SignalDirection.LONG:
            stop_loss = entry_price - original_risk
            take_profit = entry_price + (original_risk * original_rr)
        else:
            stop_loss = entry_price + original_risk
            take_profit = entry_price - (original_risk * original_rr)

        self._current_position = Position(
            position_id=f"bt_{len(self._trades)}",
            symbol=candle.symbol,
            side=PositionSide.LONG if signal.direction == SignalDirection.LONG else PositionSide.SHORT,
            entry_price=entry_price,
            size=self._config.position_size_btc,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=candle.timestamp,
            strategy_name=signal.strategy_name,
        )

        logger.debug(
            "[BACKTEST] Opened %s @ %.2f (open) SL=%.2f TP=%.2f",
            self._current_position.side.value,
            entry_price,
            stop_loss,
            take_profit,
        )

    def _check_position_exit(self, candle: Candle) -> None:
        """Check SL/TP with conservative order (no lookahead).

        For LONG:
        - If LOW touches SL: exit at SL (assume worst case)
        - Elif HIGH touches TP: exit at TP

        This is CONSERVATIVE - assumes adverse move happens first when
        both SL and TP are touched in same candle.
        """
        pos = self._current_position
        if not pos:
            return

        if pos.side.value == "LONG":
            sl_hit = candle.low <= pos.stop_loss
            tp_hit = candle.high >= pos.take_profit

            if sl_hit and tp_hit:
                # Both hit - conservative: SL first
                self._close_position(pos.stop_loss, "stop_loss", candle.timestamp)
            elif sl_hit:
                self._close_position(pos.stop_loss, "stop_loss", candle.timestamp)
            elif tp_hit:
                self._close_position(pos.take_profit, "take_profit", candle.timestamp)
        else:  # SHORT
            sl_hit = candle.high >= pos.stop_loss
            tp_hit = candle.low <= pos.take_profit

            if sl_hit and tp_hit:
                # Both hit - conservative: SL first
                self._close_position(pos.stop_loss, "stop_loss", candle.timestamp)
            elif sl_hit:
                self._close_position(pos.stop_loss, "stop_loss", candle.timestamp)
            elif tp_hit:
                self._close_position(pos.take_profit, "take_profit", candle.timestamp)

    def _close_position(self, exit_price: float, reason: str, timestamp: datetime) -> None:
        """Close the current position."""
        if not self._current_position:
            return

        pos = self._current_position

        # Apply slippage
        if pos.side.value == "LONG":
            exit_price *= 1 - self._config.slippage_pct / 100
        else:
            exit_price *= 1 + self._config.slippage_pct / 100

        # Calculate PnL
        if pos.side.value == "LONG":
            pnl = (exit_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - exit_price) * pos.size

        # Commission
        commission = exit_price * pos.size * (self._config.commission_pct / 100)
        pnl -= commission

        # Calculate R-multiple
        risk = abs(pos.entry_price - pos.stop_loss) * pos.size
        r_multiple = pnl / risk if risk > 0 else 0

        # Update capital
        self._capital += pnl

        # Record trade
        trade = {
            "position_id": pos.position_id,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "size": pos.size,
            "pnl": pnl,
            "r_multiple": r_multiple,
            "reason": reason,
            "strategy": pos.strategy_name,
            "opened_at": pos.opened_at,
            "closed_at": timestamp,
            "duration_minutes": (timestamp - pos.opened_at).total_seconds() / 60 if pos.opened_at else 0,
        }
        self._trades.append(trade)

        logger.debug(
            "[BACKTEST] Closed %s @ %.2f reason=%s pnl=$%.2f R=%.2fR",
            pos.side.value,
            exit_price,
            reason,
            pnl,
            r_multiple,
        )

        self._current_position = None

    def _calculate_results(self, candles: list[Candle]) -> BacktestResult:
        """Calculate final backtest metrics."""
        if not self._trades:
            return BacktestResult(
                final_capital=self._capital,
                start_date=candles[0].timestamp if candles else None,
                end_date=candles[-1].timestamp if candles else None,
                equity_curve=self._equity_curve,
            )

        wins = [t for t in self._trades if t["pnl"] > 0]
        losses = [t for t in self._trades if t["pnl"] <= 0]

        total_wins = sum(t["pnl"] for t in wins)
        total_losses = abs(sum(t["pnl"] for t in losses))

        avg_win = total_wins / len(wins) if wins else 0
        avg_loss = total_losses / len(losses) if losses else 0

        profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # Calculate Sharpe ratio (simplified)
        returns = [t["pnl"] for t in self._trades]
        if len(returns) > 1:
            import statistics

            mean_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            sharpe = (mean_return / std_return) * (252**0.5) if std_return > 0 else 0
        else:
            sharpe = 0

        total_pnl = sum(returns)
        win_rate = (len(wins) / len(self._trades)) * 100 if self._trades else 0
        expectancy = total_pnl / len(self._trades) if self._trades else 0

        max_dd_pct = (self._max_drawdown / self._peak_capital) * 100 if self._peak_capital > 0 else 0

        result = BacktestResult(
            total_trades=len(self._trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            total_pnl=total_pnl,
            max_drawdown=self._max_drawdown,
            max_drawdown_pct=max_dd_pct,
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_win=avg_win,
            avg_loss=avg_loss,
            sharpe_ratio=sharpe,
            expectancy=expectancy,
            final_capital=self._capital,
            start_date=candles[0].timestamp if candles else None,
            end_date=candles[-1].timestamp if candles else None,
            trades=self._trades,
            equity_curve=self._equity_curve,
        )

        logger.info(result.summary())
        return result


async def run_backtest(days: int = 30) -> BacktestResult:
    """Convenience function to run backtest.

    Example:
        result = await run_backtest(days=30)
        print(result.summary())
    """
    engine = BacktestEngine(BacktestConfig(days=days))
    return await engine.run()
