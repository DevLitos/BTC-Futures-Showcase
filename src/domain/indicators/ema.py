"""EMA (Exponential Moving Average) indicator."""

from __future__ import annotations

from collections.abc import Sequence

from src.domain.models.candle import Candle


def calculate_ema(values: Sequence[float], period: int) -> list[float]:
    """Calculate EMA for a sequence of values.

    Args:
        values: Sequence of numeric values
        period: EMA period

    Returns:
        List of EMA values (same length as input, first `period-1` are SMA)

    Raises:
        ValueError: If period < 1 or values is empty
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    if not values:
        return []

    n = len(values)
    if n == 0:
        return []

    result = [0.0] * n
    multiplier = 2.0 / (period + 1)

    # Start with SMA for first `period` values
    if n < period:
        # Not enough data, use SMA of available
        avg = sum(values) / n
        return [avg] * n

    # Calculate initial SMA
    sma = sum(values[:period]) / period
    result[period - 1] = sma

    # Calculate EMA for remaining values
    for i in range(period, n):
        result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]

    # Fill in early values with the first calculated EMA/SMA
    for i in range(period - 1):
        result[i] = result[period - 1]

    return result


def calculate_ema_from_candles(candles: Sequence[Candle], period: int, source: str = "close") -> list[float]:
    """Calculate EMA from candle data.

    Args:
        candles: Sequence of candles
        period: EMA period
        source: Price source ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4")

    Returns:
        List of EMA values

    Raises:
        ValueError: Invalid period or source
    """
    if not candles:
        return []

    values = _extract_values(candles, source)
    return calculate_ema(values, period)


def _extract_values(candles: Sequence[Candle], source: str) -> list[float]:
    """Extract price values from candles based on source type."""
    if source == "close":
        return [c.close for c in candles]
    elif source == "open":
        return [c.open for c in candles]
    elif source == "high":
        return [c.high for c in candles]
    elif source == "low":
        return [c.low for c in candles]
    elif source == "hl2":
        return [(c.high + c.low) / 2 for c in candles]
    elif source == "hlc3":
        return [(c.high + c.low + c.close) / 3 for c in candles]
    elif source == "ohlc4":
        return [(c.open + c.high + c.low + c.close) / 4 for c in candles]
    else:
        raise ValueError(f"Unknown source: {source}. Use close, open, high, low, hl2, hlc3, or ohlc4")


class EMAIndicator:
    """EMA indicator with multiple period support.

    Provides efficient EMA calculation caching for repeated calls
    with the same data. Useful when strategy needs multiple EMAs.

    Example:
        ema = EMAIndicator(periods=[20, 50, 200])
        result = ema.calculate(candles)
        ema_20 = result[20]
        ema_50 = result[50]
    """

    def __init__(self, periods: list[int], source: str = "close") -> None:
        """Initialize EMA indicator.

        Args:
            periods: List of EMA periods to calculate
            source: Price source for calculation
        """
        if not periods:
            raise ValueError("At least one period required")
        for p in periods:
            if p < 1:
                raise ValueError(f"Invalid period: {p}")

        self._periods = sorted(periods)
        self._source = source

    @property
    def periods(self) -> list[int]:
        """Configured EMA periods."""
        return self._periods.copy()

    @property
    def max_period(self) -> int:
        """Longest EMA period."""
        return self._periods[-1]

    def calculate(self, candles: Sequence[Candle]) -> dict[int, list[float]]:
        """Calculate all configured EMAs.

        Args:
            candles: Input candle data

        Returns:
            Dict mapping period to EMA values list
        """
        if not candles:
            return {p: [] for p in self._periods}

        values = _extract_values(candles, self._source)

        return {period: calculate_ema(values, period) for period in self._periods}

    def get_current(self, candles: Sequence[Candle]) -> dict[int, float]:
        """Get the most recent EMA value for each period.

        Args:
            candles: Input candle data

        Returns:
            Dict mapping period to current EMA value
        """
        result = self.calculate(candles)
        return {period: values[-1] if values else 0.0 for period, values in result.items()}

    def is_above(self, candles: Sequence[Candle], fast: int, slow: int) -> bool:
        """Check if fast EMA is above slow EMA.

        Args:
            candles: Input candle data
            fast: Fast EMA period
            slow: Slow EMA period

        Returns:
            True if fast EMA > slow EMA
        """
        current = self.get_current(candles)
        return current.get(fast, 0) > current.get(slow, 0)

    def trend_direction(self, candles: Sequence[Candle], period: int) -> int:
        """Determine trend direction based on EMA slope.

        Args:
            candles: Input candle data
            period: EMA period to check

        Returns:
            1 for uptrend, -1 for downtrend, 0 for sideways
        """
        if len(candles) < period + 5:
            return 0

        result = self.calculate(candles)
        ema_values = result.get(period, [])

        if len(ema_values) < 5:
            return 0

        # Compare current vs 5 periods ago
        current = ema_values[-1]
        previous = ema_values[-5]

        threshold = current * 0.001  # 0.1% threshold

        if current > previous + threshold:
            return 1
        elif current < previous - threshold:
            return -1
        return 0
