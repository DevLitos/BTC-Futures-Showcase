"""Candle Aggregator service: converts tick data to OHLCV candles."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.domain.models.candle import Candle


@dataclass(slots=True)
class Tick:
    """Single price tick from market data."""
    timestamp: datetime
    price: float
    volume: float = 0.0
    side: str = ""  # "buy" or "sell"


@dataclass(slots=True)
class CandleBuilder:
    """Builds a single candle from ticks."""
    symbol: str
    interval_seconds: int
    period_start: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = float('inf')
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0
    
    def add_tick(self, tick: Tick) -> None:
        """Add a tick to the current candle."""
        if self.tick_count == 0:
            self.open = tick.price
            self.high = tick.price
            self.low = tick.price
        else:
            if tick.price > self.high:
                self.high = tick.price
            if tick.price < self.low:
                self.low = tick.price
        
        self.close = tick.price
        self.volume += tick.volume
        self.tick_count += 1
    
    def build(self) -> Candle:
        """Build the final candle."""
        if self.tick_count == 0:
            raise ValueError("Cannot build candle with no ticks")
        
        return Candle(
            symbol=self.symbol,
            timestamp=self.period_start,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume
        )
    
    def reset(self, new_period_start: datetime) -> None:
        """Reset for a new candle period."""
        self.period_start = new_period_start
        self.open = 0.0
        self.high = 0.0
        self.low = float('inf')
        self.close = 0.0
        self.volume = 0.0
        self.tick_count = 0


CandleCallback = Callable[[Candle], None]


class CandleAggregator:
    """Aggregates tick data into OHLCV candles.
    
    Supports multiple timeframes and provides callbacks when candles complete.
    
    Example:
        aggregator = CandleAggregator(
            symbol="BTC/USD",
            intervals=[60, 300, 900],  # 1m, 5m, 15m
            max_candles=100
        )
        aggregator.on_candle(60, handle_1m_candle)
        aggregator.add_tick(tick)
    """

    def __init__(
        self,
        symbol: str,
        intervals: list[int],
        max_candles: int = 500
    ) -> None:
        """Initialize candle aggregator.
        
        Args:
            symbol: Trading pair symbol
            intervals: List of candle intervals in seconds
            max_candles: Maximum candles to keep per interval
        """
        if not intervals:
            raise ValueError("At least one interval required")
        
        self._symbol = symbol
        self._intervals = sorted(intervals)
        self._max_candles = max_candles
        
        # Builders for each interval
        self._builders: dict[int, CandleBuilder] = {}
        
        # Completed candles for each interval
        self._candles: dict[int, deque[Candle]] = {
            interval: deque(maxlen=max_candles)
            for interval in intervals
        }
        
        # Callbacks for each interval
        self._callbacks: dict[int, list[CandleCallback]] = {
            interval: [] for interval in intervals
        }
        
        self._last_tick_time: datetime | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def intervals(self) -> list[int]:
        return self._intervals.copy()

    def on_candle(self, interval: int, callback: CandleCallback) -> None:
        """Register a callback for completed candles.
        
        Args:
            interval: Candle interval in seconds
            callback: Function to call with completed candle
        """
        if interval not in self._callbacks:
            raise ValueError(f"Unknown interval: {interval}")
        self._callbacks[interval].append(callback)

    def add_tick(self, tick: Tick) -> list[Candle]:
        """Add a tick and return any completed candles.
        
        Args:
            tick: Price tick to process
            
        Returns:
            List of newly completed candles
        """
        completed: list[Candle] = []
        
        for interval in self._intervals:
            candle = self._process_tick_for_interval(tick, interval)
            if candle:
                completed.append(candle)
                self._candles[interval].append(candle)
                
                # Fire callbacks
                for callback in self._callbacks[interval]:
                    callback(candle)
        
        self._last_tick_time = tick.timestamp
        return completed

    def add_ticks(self, ticks: list[Tick]) -> list[Candle]:
        """Add multiple ticks, return all completed candles."""
        all_completed: list[Candle] = []
        for tick in ticks:
            completed = self.add_tick(tick)
            all_completed.extend(completed)
        return all_completed

    def get_candles(self, interval: int, count: int | None = None) -> list[Candle]:
        """Get completed candles for an interval.
        
        Args:
            interval: Candle interval in seconds
            count: Optional limit (returns most recent)
            
        Returns:
            List of candles (oldest first)
        """
        if interval not in self._candles:
            raise ValueError(f"Unknown interval: {interval}")
        
        candles = list(self._candles[interval])
        if count is not None:
            candles = candles[-count:]
        return candles

    def get_current_candle(self, interval: int) -> Candle | None:
        """Get the in-progress candle for an interval.
        
        Args:
            interval: Candle interval in seconds
            
        Returns:
            Current building candle or None
        """
        builder = self._builders.get(interval)
        if builder and builder.tick_count > 0:
            return builder.build()
        return None

    def _process_tick_for_interval(self, tick: Tick, interval: int) -> Candle | None:
        """Process a tick for a specific interval.
        
        Returns completed candle if period ended.
        """
        period_start = self._get_period_start(tick.timestamp, interval)
        
        builder = self._builders.get(interval)
        
        if builder is None:
            # First tick for this interval
            builder = CandleBuilder(
                symbol=self._symbol,
                interval_seconds=interval,
                period_start=period_start
            )
            self._builders[interval] = builder
            builder.add_tick(tick)
            return None
        
        # Check if we've moved to a new period
        if period_start > builder.period_start:
            # Complete the current candle
            completed = None
            if builder.tick_count > 0:
                completed = builder.build()
            
            # Start new candle
            builder.reset(period_start)
            builder.add_tick(tick)
            return completed
        else:
            # Same period, just add tick
            builder.add_tick(tick)
            return None

    def _get_period_start(self, timestamp: datetime, interval: int) -> datetime:
        """Get the start of the candle period containing the timestamp."""
        # Ensure timestamp is timezone-aware (UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        
        # Round down to interval boundary using Unix timestamp
        unix_ts = timestamp.timestamp()
        period_start_ts = (int(unix_ts) // interval) * interval
        return datetime.fromtimestamp(period_start_ts, tz=timezone.utc)

    def flush(self) -> list[Candle]:
        """Force complete all in-progress candles.
        
        Returns:
            List of flushed candles
        """
        completed: list[Candle] = []
        
        for interval, builder in self._builders.items():
            if builder.tick_count > 0:
                candle = builder.build()
                completed.append(candle)
                self._candles[interval].append(candle)
                
                for callback in self._callbacks[interval]:
                    callback(candle)
                
                # Reset builder
                if self._last_tick_time:
                    next_period = self._get_period_start(
                        self._last_tick_time + timedelta(seconds=interval),
                        interval
                    )
                    builder.reset(next_period)
        
        return completed

    def reset(self) -> None:
        """Clear all candles and reset builders."""
        self._builders.clear()
        for candles in self._candles.values():
            candles.clear()
        self._last_tick_time = None
