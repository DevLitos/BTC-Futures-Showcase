"""Position model representing open trading positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class PositionSide(Enum):
    """Side of the position."""

    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(Enum):
    """Current status of a position."""

    PENDING = "PENDING"  # Signal received, waiting for entry
    OPEN = "OPEN"  # Position is active
    CLOSING = "CLOSING"  # Close order sent
    CLOSED = "CLOSED"  # Position fully closed


class ExitReason(Enum):
    """Reason for closing a position."""

    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    SIGNAL_INVALIDATED = "SIGNAL_INVALIDATED"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"


@dataclass(slots=True)
class Position:
    """Represents an open or closed trading position.

    Attributes:
        position_id: Unique identifier
        symbol: Trading pair
        side: LONG or SHORT
        entry_price: Average entry price
        size: Position size in base currency
        stop_loss: Current stop loss price
        take_profit: Current take profit price
        status: Current position status
    """

    position_id: str
    symbol: str
    side: PositionSide
    entry_price: float
    size: float
    stop_loss: float
    take_profit: float
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    strategy_name: str = ""
    signal_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # Tracking fields
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    trailing_stop_activated: bool = False

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive")
        if self.take_profit <= 0:
            raise ValueError("take_profit must be positive")

    @property
    def is_open(self) -> bool:
        """True if position is still active."""
        return self.status in (PositionStatus.OPEN, PositionStatus.PENDING)

    @property
    def is_closed(self) -> bool:
        """True if position is closed."""
        return self.status == PositionStatus.CLOSED

    @property
    def notional_value(self) -> float:
        """Position value in USD at entry."""
        return self.size * self.entry_price

    @property
    def risk_amount(self) -> float:
        """Distance from entry to stop loss."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk_pct(self) -> float:
        """Risk as percentage of entry."""
        return (self.risk_amount / self.entry_price) * 100

    def update_price(self, current_price: float) -> None:
        """Update MFE/MAE based on current price."""
        if self.side == PositionSide.LONG:
            favorable = current_price - self.entry_price
            adverse = self.entry_price - current_price
        else:
            favorable = self.entry_price - current_price
            adverse = current_price - self.entry_price

        if favorable > self.max_favorable_excursion:
            self.max_favorable_excursion = favorable
        if adverse > self.max_adverse_excursion:
            self.max_adverse_excursion = adverse

    def should_stop_out(self, current_price: float) -> bool:
        """Check if current price hits stop loss."""
        if self.side == PositionSide.LONG:
            return current_price <= self.stop_loss
        else:
            return current_price >= self.stop_loss

    def should_take_profit(self, current_price: float) -> bool:
        """Check if current price hits take profit."""
        if self.side == PositionSide.LONG:
            return current_price >= self.take_profit
        else:
            return current_price <= self.take_profit

    def calculate_pnl(self, exit_price: float) -> float:
        """Calculate PnL for a given exit price."""
        if self.side == PositionSide.LONG:
            return (exit_price - self.entry_price) * self.size
        else:
            return (self.entry_price - exit_price) * self.size

    def calculate_pnl_pct(self, exit_price: float) -> float:
        """Calculate PnL as percentage of entry."""
        if self.side == PositionSide.LONG:
            return ((exit_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - exit_price) / self.entry_price) * 100

    def calculate_r_multiple(self, exit_price: float) -> float:
        """Calculate PnL in R multiples (risk units)."""
        if self.risk_amount == 0:
            return 0.0
        pnl = self.calculate_pnl(exit_price)
        risk_value = self.risk_amount * self.size
        return pnl / risk_value

    def close(self, exit_price: float, reason: ExitReason, closed_at: datetime | None = None) -> None:
        """Mark position as closed."""
        self.exit_price = exit_price
        self.exit_reason = reason
        self.closed_at = closed_at or datetime.now(UTC)
        self.status = PositionStatus.CLOSED

    @property
    def realized_pnl(self) -> float | None:
        """PnL if position is closed."""
        if not self.is_closed or self.exit_price is None:
            return None
        return self.calculate_pnl(self.exit_price)

    @property
    def realized_r(self) -> float | None:
        """R-multiple if position is closed."""
        if not self.is_closed or self.exit_price is None:
            return None
        return self.calculate_r_multiple(self.exit_price)
