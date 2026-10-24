"""Position Manager service: tracks and manages trading positions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from src.domain.models.position import ExitReason, Position, PositionSide, PositionStatus
from src.domain.models.signal import Signal, SignalDirection


@dataclass(slots=True)
class PositionEvent:
    """Event emitted for position state changes."""

    position: Position
    event_type: str  # "opened", "updated", "closed"
    timestamp: datetime
    details: dict = field(default_factory=dict)


PositionCallback = Callable[[PositionEvent], None]


@dataclass
class PositionManagerConfig:
    """Configuration for position manager."""

    max_positions: int = 1
    max_risk_per_position_pct: float = 2.0
    max_total_risk_pct: float = 5.0
    trailing_stop_activation_r: float = 1.0
    trailing_stop_distance_r: float = 0.5
    position_timeout_minutes: int = 240


class PositionManager:
    """Manages trading positions from signals to closure.

    Handles:
    - Position creation from signals
    - Stop loss and take profit monitoring
    - Trailing stops
    - Position limits and risk management

    Example:
        manager = PositionManager(config=PositionManagerConfig())
        manager.on_position(handle_position_event)

        position = manager.open_from_signal(signal, size=0.01)
        manager.update_price(current_price)
    """

    def __init__(self, config: PositionManagerConfig | None = None) -> None:
        """Initialize position manager.

        Args:
            config: Manager configuration
        """
        self._config = config or PositionManagerConfig()
        self._positions: dict[str, Position] = {}
        self._closed_positions: list[Position] = []
        self._callbacks: list[PositionCallback] = []

    @property
    def open_positions(self) -> list[Position]:
        """All currently open positions."""
        return [p for p in self._positions.values() if p.is_open]

    @property
    def position_count(self) -> int:
        """Number of open positions."""
        return len(self.open_positions)

    @property
    def total_risk_pct(self) -> float:
        """Total risk across all open positions."""
        return sum(p.risk_pct for p in self.open_positions)

    def on_position(self, callback: PositionCallback) -> None:
        """Register a callback for position events."""
        self._callbacks.append(callback)

    def can_open_position(self, signal: Signal) -> tuple[bool, str]:
        """Check if a new position can be opened.

        Args:
            signal: Signal to evaluate

        Returns:
            Tuple of (can_open, reason)
        """
        # Check position limit
        if self.position_count >= self._config.max_positions:
            return False, f"Max positions ({self._config.max_positions}) reached"

        # Check signal risk
        if signal.risk_pct > self._config.max_risk_per_position_pct:
            return (
                False,
                f"Signal risk ({signal.risk_pct:.2f}%) exceeds max ({self._config.max_risk_per_position_pct}%)",
            )

        # Check total risk
        new_total_risk = self.total_risk_pct + signal.risk_pct
        if new_total_risk > self._config.max_total_risk_pct:
            return False, f"Total risk ({new_total_risk:.2f}%) would exceed max ({self._config.max_total_risk_pct}%)"

        # Check for conflicting positions
        for pos in self.open_positions:
            if pos.symbol == signal.symbol:
                if signal.direction == SignalDirection.LONG and pos.side == PositionSide.SHORT:
                    return False, "Conflicting SHORT position exists"
                if signal.direction == SignalDirection.SHORT and pos.side == PositionSide.LONG:
                    return False, "Conflicting LONG position exists"

        return True, "OK"

    def open_from_signal(
        self,
        signal: Signal,
        size: float,
        position_id: str | None = None,
        entry_price: float | None = None,
    ) -> Position | None:
        """Create and open a position from a signal.

        Args:
            signal: Trading signal
            size: Position size in base currency
            position_id: Optional custom position ID

        Returns:
            Created position or None if cannot open
        """
        can_open, reason = self.can_open_position(signal)
        if not can_open:
            return None

        pos_id = position_id or f"pos_{uuid4().hex[:8]}"
        side = PositionSide.LONG if signal.direction == SignalDirection.LONG else PositionSide.SHORT

        position = Position(
            position_id=pos_id,
            symbol=signal.symbol,
            side=side,
            entry_price=entry_price or signal.entry_price,
            size=size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            status=PositionStatus.OPEN,
            opened_at=signal.timestamp,
            strategy_name=signal.strategy_name,
            signal_type=signal.signal_type.value,
            metadata=dict(signal.metadata),
        )

        self._positions[pos_id] = position
        self._emit_event(position, "opened")

        return position

    def restore_open_position(self, data: dict) -> Position | None:
        """Restore a persisted open position without emitting a new entry event."""
        try:
            opened_at = data.get("opened_at")
            position = Position(
                position_id=str(data["position_id"]),
                symbol=str(data["symbol"]),
                side=PositionSide(str(data["side"])),
                entry_price=float(data["entry_price"]),
                size=float(data["size"]),
                stop_loss=float(data["stop_loss"]),
                take_profit=float(data["take_profit"]),
                status=PositionStatus.OPEN,
                opened_at=datetime.fromisoformat(opened_at) if opened_at else None,
                strategy_name=str(data.get("strategy_name", "")),
                signal_type=str(data.get("signal_type", "")),
                metadata=dict(data.get("metadata", {})),
                trailing_stop_activated=bool(data.get("trailing_stop_activated", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid persisted position: {data!r}") from exc

        self._positions[position.position_id] = position
        return position

    def update_price(self, current_price: float, timestamp: datetime | None = None) -> list[Position]:
        """Update all positions with current price.

        Checks stop loss, take profit, and trailing stops.

        Args:
            current_price: Current market price
            timestamp: Optional current timestamp

        Returns:
            List of positions that were closed
        """
        timestamp = timestamp or datetime.now(UTC)
        closed: list[Position] = []

        for position in self.open_positions:
            # Update MFE/MAE
            position.update_price(current_price)

            # Check stop loss
            if position.should_stop_out(current_price):
                self._close_position(position, current_price, ExitReason.STOP_LOSS, timestamp)
                closed.append(position)
                continue

            # Check take profit
            if position.should_take_profit(current_price):
                self._close_position(position, current_price, ExitReason.TAKE_PROFIT, timestamp)
                closed.append(position)
                continue

            # Check trailing stop
            if self._should_trail_stop(position, current_price):
                self._update_trailing_stop(position, current_price)

            # Check timeout
            if self._is_timed_out(position, timestamp):
                self._close_position(position, current_price, ExitReason.TIME_EXIT, timestamp)
                closed.append(position)

        return closed

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        reason: ExitReason = ExitReason.MANUAL,
        timestamp: datetime | None = None,
    ) -> Position | None:
        """Manually close a position.

        Args:
            position_id: Position to close
            exit_price: Exit price
            reason: Reason for closing
            timestamp: Optional timestamp

        Returns:
            Closed position or None if not found
        """
        position = self._positions.get(position_id)
        if not position or not position.is_open:
            return None

        self._close_position(position, exit_price, reason, timestamp or datetime.now(UTC))
        return position

    def _close_position(self, position: Position, exit_price: float, reason: ExitReason, timestamp: datetime) -> None:
        """Internal position close handler."""
        position.close(exit_price, reason, timestamp)
        self._closed_positions.append(position)
        self._emit_event(
            position,
            "closed",
            {"reason": reason.value, "pnl": position.realized_pnl, "r_multiple": position.realized_r},
        )

    def _should_trail_stop(self, position: Position, current_price: float) -> bool:
        """Check if trailing stop should be activated/updated."""
        if not position.is_open:
            return False

        # Calculate current R-multiple
        current_r = position.calculate_r_multiple(current_price)

        # Activate trailing stop after reaching threshold
        return current_r >= self._config.trailing_stop_activation_r

    def _update_trailing_stop(self, position: Position, current_price: float) -> None:
        """Update trailing stop for a position."""
        risk_amount = position.risk_amount
        trail_distance = risk_amount * self._config.trailing_stop_distance_r

        if position.side == PositionSide.LONG:
            new_stop = current_price - trail_distance
            if new_stop > position.stop_loss:
                old_stop = position.stop_loss
                position.stop_loss = new_stop
                position.trailing_stop_activated = True
                self._emit_event(
                    position, "updated", {"update_type": "trailing_stop", "old_stop": old_stop, "new_stop": new_stop}
                )
        else:
            new_stop = current_price + trail_distance
            if new_stop < position.stop_loss:
                old_stop = position.stop_loss
                position.stop_loss = new_stop
                position.trailing_stop_activated = True
                self._emit_event(
                    position, "updated", {"update_type": "trailing_stop", "old_stop": old_stop, "new_stop": new_stop}
                )

    def _is_timed_out(self, position: Position, current_time: datetime) -> bool:
        """Check if position has exceeded timeout."""
        if not position.opened_at:
            return False

        elapsed = (current_time - position.opened_at).total_seconds() / 60
        return elapsed > self._config.position_timeout_minutes

    def _emit_event(self, position: Position, event_type: str, details: dict | None = None) -> None:
        """Emit a position event to callbacks."""
        event = PositionEvent(
            position=position, event_type=event_type, timestamp=datetime.now(UTC), details=details or {}
        )

        for callback in self._callbacks:
            with suppress(Exception):
                callback(event)

    def get_position(self, position_id: str) -> Position | None:
        """Get a position by ID."""
        return self._positions.get(position_id)

    def get_stats(self) -> dict:
        """Get position manager statistics."""
        closed = self._closed_positions

        if not closed:
            return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_r": 0.0}

        wins = [p for p in closed if (p.realized_pnl or 0) > 0]
        total_pnl = sum(p.realized_pnl or 0 for p in closed)
        total_r = sum(p.realized_r or 0 for p in closed)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": (len(wins) / len(closed)) * 100 if closed else 0,
            "total_pnl": total_pnl,
            "avg_r": total_r / len(closed) if closed else 0,
            "avg_pnl": total_pnl / len(closed) if closed else 0,
        }

    def reset(self) -> None:
        """Clear all positions and stats."""
        self._positions.clear()
        self._closed_positions.clear()
