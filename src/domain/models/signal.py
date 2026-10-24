"""Signal model representing trading signals from strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SignalDirection(Enum):
    """Direction of a trading signal."""
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class SignalStrength(Enum):
    """Strength/confidence of the signal."""
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class SignalType(Enum):
    """Type of signal based on strategy pattern."""
    FVG = "FVG"
    IFVG = "IFVG"
    ORDER_BLOCK = "ORDER_BLOCK"
    MEAN_REVERSION = "MEAN_REVERSION"
    TREND_CONTINUATION = "TREND_CONTINUATION"


@dataclass(frozen=True, slots=True)
class Signal:
    """Immutable trading signal from a strategy.
    
    Attributes:
        timestamp: When the signal was generated
        direction: LONG, SHORT, or FLAT
        signal_type: Type of pattern that generated the signal
        entry_price: Suggested entry price
        stop_loss: Suggested stop loss price
        take_profit: Suggested take profit price
        strength: Confidence level of the signal
        strategy_name: Name of the strategy that generated it
        metadata: Additional signal-specific data
    """
    timestamp: datetime
    direction: SignalDirection
    signal_type: SignalType
    entry_price: float
    stop_loss: float
    take_profit: float
    strength: SignalStrength = SignalStrength.MODERATE
    strategy_name: str = ""
    symbol: str = "BTC/USD"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive")
        if self.take_profit <= 0:
            raise ValueError("take_profit must be positive")
        
        if self.direction == SignalDirection.LONG:
            if self.stop_loss >= self.entry_price:
                raise ValueError("LONG signal: stop_loss must be below entry_price")
            if self.take_profit <= self.entry_price:
                raise ValueError("LONG signal: take_profit must be above entry_price")
        elif self.direction == SignalDirection.SHORT:
            if self.stop_loss <= self.entry_price:
                raise ValueError("SHORT signal: stop_loss must be above entry_price")
            if self.take_profit >= self.entry_price:
                raise ValueError("SHORT signal: take_profit must be below entry_price")

    @property
    def risk_amount(self) -> float:
        """Distance from entry to stop loss."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_amount(self) -> float:
        """Distance from entry to take profit."""
        return abs(self.take_profit - self.entry_price)

    @property
    def risk_reward_ratio(self) -> float:
        """Reward to risk ratio."""
        if self.risk_amount == 0:
            return 0.0
        return self.reward_amount / self.risk_amount

    @property
    def risk_pct(self) -> float:
        """Risk as percentage of entry price."""
        return (self.risk_amount / self.entry_price) * 100

    @property
    def reward_pct(self) -> float:
        """Reward as percentage of entry price."""
        return (self.reward_amount / self.entry_price) * 100

    def is_valid(self, min_rr: float = 1.0) -> bool:
        """Check if signal meets minimum criteria."""
        return (
            self.direction != SignalDirection.FLAT
            and self.risk_reward_ratio >= min_rr
            and self.risk_pct < 5.0  # Max 5% risk
        )


@dataclass(slots=True)
class SignalEvaluation:
    """Result of evaluating a signal against current market state."""
    signal: Signal
    is_triggered: bool
    triggered_at: datetime | None = None
    current_price: float = 0.0
    distance_to_entry_pct: float = 0.0
    notes: str = ""

    @property
    def can_enter(self) -> bool:
        """True if signal is triggered and within acceptable range."""
        return self.is_triggered and abs(self.distance_to_entry_pct) < 0.5
