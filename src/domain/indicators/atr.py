"""ATR (Average True Range) indicator for volatility measurement."""

from __future__ import annotations

from typing import Sequence

from src.domain.models.candle import Candle


def calculate_true_range(candles: Sequence[Candle]) -> list[float]:
    """Calculate True Range for each candle.
    
    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    
    Args:
        candles: Sequence of candles
        
    Returns:
        List of TR values (first value uses high-low only)
    """
    if not candles:
        return []
    
    n = len(candles)
    result = [0.0] * n
    
    # First candle: just high - low
    result[0] = candles[0].high - candles[0].low
    
    # Rest: true range calculation
    for i in range(1, n):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        result[i] = max(tr1, tr2, tr3)
    
    return result


def calculate_atr(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Calculate ATR (Average True Range).
    
    Uses Wilder's smoothing method (similar to EMA).
    
    Args:
        candles: Sequence of candles
        period: ATR period (default 14)
        
    Returns:
        List of ATR values
        
    Raises:
        ValueError: If period < 1
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    if not candles:
        return []
    
    tr_values = calculate_true_range(candles)
    n = len(tr_values)
    
    if n == 0:
        return []
    
    result = [0.0] * n
    
    if n < period:
        # Not enough data, use simple average
        avg = sum(tr_values) / n
        return [avg] * n
    
    # First ATR is SMA of TR
    first_atr = sum(tr_values[:period]) / period
    result[period - 1] = first_atr
    
    # Wilder's smoothing: ATR = ((prev_ATR * (period-1)) + current_TR) / period
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + tr_values[i]) / period
    
    # Fill early values
    for i in range(period - 1):
        result[i] = result[period - 1]
    
    return result


def calculate_atr_percent(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Calculate ATR as percentage of close price.
    
    Useful for comparing volatility across different price levels.
    
    Args:
        candles: Sequence of candles
        period: ATR period
        
    Returns:
        List of ATR% values
    """
    if not candles:
        return []
    
    atr_values = calculate_atr(candles, period)
    return [
        (atr / c.close) * 100 if c.close > 0 else 0.0
        for atr, c in zip(atr_values, candles)
    ]


class ATRIndicator:
    """ATR indicator with volatility-based utilities.
    
    Provides ATR calculation plus volatility-derived values
    useful for position sizing and stop placement.
    
    Example:
        atr = ATRIndicator(period=14)
        current_atr = atr.get_current(candles)
        stop_distance = atr.get_stop_distance(candles, multiplier=1.5)
    """

    def __init__(self, period: int = 14) -> None:
        """Initialize ATR indicator.
        
        Args:
            period: ATR period
        """
        if period < 1:
            raise ValueError("period must be at least 1")
        self._period = period

    @property
    def period(self) -> int:
        """ATR period."""
        return self._period

    def calculate(self, candles: Sequence[Candle]) -> list[float]:
        """Calculate ATR for all candles.
        
        Args:
            candles: Input candle data
            
        Returns:
            List of ATR values
        """
        return calculate_atr(candles, self._period)

    def get_current(self, candles: Sequence[Candle]) -> float:
        """Get the most recent ATR value.
        
        Args:
            candles: Input candle data
            
        Returns:
            Current ATR value
        """
        values = self.calculate(candles)
        return values[-1] if values else 0.0

    def get_current_percent(self, candles: Sequence[Candle]) -> float:
        """Get current ATR as percentage of price.
        
        Args:
            candles: Input candle data
            
        Returns:
            ATR as percentage
        """
        if not candles:
            return 0.0
        
        atr = self.get_current(candles)
        close = candles[-1].close
        return (atr / close) * 100 if close > 0 else 0.0

    def get_stop_distance(self, candles: Sequence[Candle], multiplier: float = 1.5) -> float:
        """Calculate stop loss distance based on ATR.
        
        Args:
            candles: Input candle data
            multiplier: ATR multiplier for stop distance
            
        Returns:
            Stop distance in price units
        """
        return self.get_current(candles) * multiplier

    def get_take_profit_distance(
        self,
        candles: Sequence[Candle],
        risk_reward: float = 2.0,
        stop_multiplier: float = 1.5
    ) -> float:
        """Calculate take profit distance based on R:R ratio.
        
        Args:
            candles: Input candle data
            risk_reward: Target risk:reward ratio
            stop_multiplier: ATR multiplier for stop distance
            
        Returns:
            Take profit distance in price units
        """
        stop_distance = self.get_stop_distance(candles, stop_multiplier)
        return stop_distance * risk_reward

    def is_high_volatility(self, candles: Sequence[Candle], threshold_pct: float = 2.0) -> bool:
        """Check if current volatility is high.
        
        Args:
            candles: Input candle data
            threshold_pct: ATR% threshold for high volatility
            
        Returns:
            True if ATR% exceeds threshold
        """
        return self.get_current_percent(candles) > threshold_pct

    def is_low_volatility(self, candles: Sequence[Candle], threshold_pct: float = 0.5) -> bool:
        """Check if current volatility is low.
        
        Args:
            candles: Input candle data
            threshold_pct: ATR% threshold for low volatility
            
        Returns:
            True if ATR% is below threshold
        """
        return self.get_current_percent(candles) < threshold_pct

    def volatility_regime(self, candles: Sequence[Candle]) -> str:
        """Categorize current volatility regime.
        
        Args:
            candles: Input candle data
            
        Returns:
            "low", "normal", or "high"
        """
        atr_pct = self.get_current_percent(candles)
        
        if atr_pct < 0.5:
            return "low"
        elif atr_pct > 2.0:
            return "high"
        return "normal"
