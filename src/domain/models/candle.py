"""Candle model representing OHLCV data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Self


@dataclass(frozen=True, slots=True)
class Candle:
    """Immutable OHLCV candle representation.

    Attributes:
        timestamp: Candle open time (UTC)
        open: Opening price
        high: Highest price during period
        low: Lowest price during period
        close: Closing price
        volume: Total volume traded
        symbol: Trading pair symbol
        timeframe_minutes: Candle duration in minutes
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    symbol: str = "BTC/USD"
    timeframe_minutes: int = 60

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"high ({self.high}) must be >= low ({self.low})")
        if self.high < max(self.open, self.close):
            raise ValueError("high must be >= open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= open and close")
        if self.open <= 0 or self.close <= 0:
            raise ValueError("prices must be positive")

    @property
    def is_bullish(self) -> bool:
        """True if close > open."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """True if close < open."""
        return self.close < self.open

    @property
    def body_size(self) -> float:
        """Absolute size of candle body."""
        return abs(self.close - self.open)

    @property
    def body_pct(self) -> float:
        """Body size as percentage of open price."""
        return (self.body_size / self.open) * 100

    @property
    def range_size(self) -> float:
        """Total range (high - low)."""
        return self.high - self.low

    @property
    def range_pct(self) -> float:
        """Range as percentage of low price."""
        return (self.range_size / self.low) * 100 if self.low > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        """Size of upper wick."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Size of lower wick."""
        return min(self.open, self.close) - self.low

    @property
    def midpoint(self) -> float:
        """Midpoint of the candle range."""
        return (self.high + self.low) / 2

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """Create Candle from dictionary."""
        return cls(
            timestamp=data["timestamp"]
            if isinstance(data["timestamp"], datetime)
            else datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data.get("volume", 0)),
            symbol=data.get("symbol", "BTC/USD"),
            timeframe_minutes=int(data.get("timeframe_minutes", 60)),
        )


@dataclass(slots=True)
class CandleBuffer:
    """Rolling buffer of candles for indicator calculation.

    Maintains a fixed-size buffer of recent candles.
    """

    max_size: int
    _candles: list[Candle]

    def __init__(self, max_size: int = 200) -> None:
        self.max_size = max_size
        self._candles = []

    def add(self, candle: Candle) -> None:
        """Add a candle to the buffer."""
        self._candles.append(candle)
        if len(self._candles) > self.max_size:
            self._candles.pop(0)

    # Alias for compatibility
    append = add

    def get_last(self, n: int = 1) -> list[Candle]:
        """Get last N candles."""
        return self._candles[-n:] if n <= len(self._candles) else self._candles[:]

    def get_all(self) -> list[Candle]:
        """Get all candles in buffer."""
        return self._candles[:]

    @property
    def closes(self) -> list[float]:
        """Get all close prices."""
        return [c.close for c in self._candles]

    @property
    def highs(self) -> list[float]:
        """Get all high prices."""
        return [c.high for c in self._candles]

    @property
    def lows(self) -> list[float]:
        """Get all low prices."""
        return [c.low for c in self._candles]

    def __len__(self) -> int:
        return len(self._candles)

    def __getitem__(self, index: int) -> Candle:
        return self._candles[index]

    def to_list(self) -> list[Candle]:
        """Return a copy of all candles as a list."""
        return self._candles[:]

    def clear(self) -> None:
        """Clear all candles from buffer."""
        self._candles.clear()
