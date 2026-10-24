"""Signal Orchestrator service: evaluates strategies and coordinates signals."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from src.domain.models.candle import Candle, CandleBuffer
from src.domain.models.signal import Signal, SignalDirection, SignalStrength
from src.domain.strategies.base import Strategy

logger = logging.getLogger("trading")


@dataclass(slots=True)
class SignalEvent:
    """Event emitted when a signal is generated."""

    signal: Signal
    candles_used: int
    evaluation_time_ms: float


SignalCallback = Callable[[SignalEvent], None]


@dataclass
class OrchestratorConfig:
    """Configuration for the signal orchestrator."""

    max_signals_per_eval: int = 3
    min_signal_interval_seconds: int = 60
    require_confirmation: bool = False
    allowed_directions: list[SignalDirection] = field(
        default_factory=lambda: [SignalDirection.LONG, SignalDirection.SHORT]
    )
    min_strength: SignalStrength = SignalStrength.WEAK


class SignalOrchestrator:
    """Orchestrates strategy evaluation and signal generation.

    Maintains candle buffers, evaluates registered strategies,
    filters/ranks signals, and emits events.

    Example:
        orchestrator = SignalOrchestrator(
            strategies=[FVGTrendStrategy(), IFVGStrategy()],
            buffer_size=100
        )
        orchestrator.on_signal(handle_signal)
        orchestrator.add_candle(candle)
    """

    def __init__(
        self, strategies: list[Strategy], buffer_size: int = 200, config: OrchestratorConfig | None = None
    ) -> None:
        """Initialize signal orchestrator.

        Args:
            strategies: List of strategies to evaluate
            buffer_size: Maximum candles to keep in buffer
            config: Orchestrator configuration
        """
        if not strategies:
            raise ValueError("At least one strategy required")

        self._strategies = strategies
        self._buffer = CandleBuffer(max_size=buffer_size)
        self._config = config or OrchestratorConfig()
        self._callbacks: list[SignalCallback] = []
        self._last_signal_time: dict[str, datetime] = {}
        self._signal_history: list[Signal] = []

    @property
    def strategies(self) -> list[Strategy]:
        """Registered strategies."""
        return self._strategies.copy()

    @property
    def candle_count(self) -> int:
        """Number of candles in buffer."""
        return len(self._buffer)

    def on_signal(self, callback: SignalCallback) -> None:
        """Register a callback for signal events."""
        self._callbacks.append(callback)

    def add_candle(self, candle: Candle) -> list[Signal]:
        """Add a candle and evaluate strategies.

        Args:
            candle: New candle to process

        Returns:
            List of signals generated
        """
        self._buffer.append(candle)
        return self._evaluate_all()

    def add_candles(self, candles: Sequence[Candle]) -> list[Signal]:
        """Add multiple candles and evaluate after all are added."""
        for candle in candles:
            self._buffer.append(candle)
        return self._evaluate_all()

    def _evaluate_all(self) -> list[Signal]:
        """Evaluate all strategies and return filtered signals."""
        if len(self._buffer) == 0:
            return []

        candles = self._buffer.to_list()
        all_signals: list[Signal] = []

        import time

        for strategy in self._strategies:
            if not strategy.config.enabled:
                continue

            start_time = time.perf_counter()

            try:
                signals = strategy.evaluate(candles)
            except Exception as e:
                logger.warning("[STRATEGY] %s error: %s", strategy.name, e)
                continue

            eval_time_ms = (time.perf_counter() - start_time) * 1000

            # Log strategy evaluation results for debugging
            if signals:
                logger.info(
                    "[STRATEGY] %s generated %d signals (eval=%.1fms)", strategy.name, len(signals), eval_time_ms
                )

            for signal in signals:
                if self._is_signal_valid(signal):
                    all_signals.append(signal)

                    # Emit event
                    event = SignalEvent(signal=signal, candles_used=len(candles), evaluation_time_ms=eval_time_ms)
                    self._emit_signal(event)
                else:
                    logger.debug(
                        "[STRATEGY] %s signal rejected: %s %.2f",
                        strategy.name,
                        signal.direction.value,
                        signal.entry_price,
                    )

        # Apply global filtering and ranking
        filtered = self._filter_signals(all_signals)
        ranked = self._rank_signals(filtered)

        # Limit number of signals
        result = ranked[: self._config.max_signals_per_eval]

        # Update history
        self._signal_history.extend(result)
        if len(self._signal_history) > 100:
            self._signal_history = self._signal_history[-100:]

        return result

    def _is_signal_valid(self, signal: Signal) -> bool:
        """Check if a signal passes basic validation."""
        # Check direction
        if signal.direction not in self._config.allowed_directions:
            return False

        # Check strength
        strength_order = {SignalStrength.WEAK: 0, SignalStrength.MODERATE: 1, SignalStrength.STRONG: 2}
        if strength_order[signal.strength] < strength_order[self._config.min_strength]:
            return False

        # Check minimum interval
        strategy_name = signal.strategy_name
        last_time = self._last_signal_time.get(strategy_name)
        if last_time:
            elapsed = (signal.timestamp - last_time).total_seconds()
            if elapsed < self._config.min_signal_interval_seconds:
                return False

        self._last_signal_time[strategy_name] = signal.timestamp
        return True

    def _filter_signals(self, signals: list[Signal]) -> list[Signal]:
        """Apply additional filtering rules."""
        if not signals:
            return []

        # Remove conflicting signals (same strategy, opposite direction)
        seen_strategies: dict[str, SignalDirection] = {}
        filtered: list[Signal] = []

        for signal in signals:
            key = signal.strategy_name
            if key in seen_strategies:
                # Keep only if same direction
                if seen_strategies[key] == signal.direction:
                    filtered.append(signal)
            else:
                seen_strategies[key] = signal.direction
                filtered.append(signal)

        return filtered

    def _rank_signals(self, signals: list[Signal]) -> list[Signal]:
        """Rank signals by quality."""
        if not signals:
            return []

        def score(s: Signal) -> float:
            # Higher score = better signal
            strength_scores = {SignalStrength.WEAK: 1, SignalStrength.MODERATE: 2, SignalStrength.STRONG: 3}

            score = strength_scores[s.strength] * 10
            score += s.risk_reward_ratio * 2
            score -= s.risk_pct  # Penalize higher risk

            return score

        return sorted(signals, key=score, reverse=True)

    def _emit_signal(self, event: SignalEvent) -> None:
        """Emit signal event to all callbacks."""
        for callback in self._callbacks:
            with suppress(Exception):
                callback(event)

    def get_recent_signals(self, count: int = 10) -> list[Signal]:
        """Get recent signals from history."""
        return self._signal_history[-count:]

    def reset(self) -> None:
        """Clear buffer and reset all strategies."""
        self._buffer.clear()
        self._last_signal_time.clear()
        self._signal_history.clear()

        for strategy in self._strategies:
            if hasattr(strategy, "reset"):
                strategy.reset()
