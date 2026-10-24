"""Abstract base class for trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from src.domain.models.candle import Candle
from src.domain.models.signal import Signal


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Configuration parameters for a strategy.
    
    Attributes:
        name: Strategy identifier
        enabled: Whether strategy is active
        risk_reward_min: Minimum R:R ratio to generate signal
        max_risk_pct: Maximum risk per trade as % of entry
        lookback_periods: Number of candles needed for analysis
        params: Strategy-specific parameters
    """
    name: str
    enabled: bool = True
    risk_reward_min: float = 2.0
    max_risk_pct: float = 2.0
    lookback_periods: int = 50
    params: dict[str, float | int | str | bool] = field(default_factory=dict)


class StrategyProtocol(Protocol):
    """Protocol defining the strategy interface.
    
    Strategies are stateless: they receive candle data and return signals.
    They do NOT maintain internal state between calls.
    """

    @property
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @property
    def config(self) -> StrategyConfig:
        """Strategy configuration."""
        ...

    @property
    def required_candles(self) -> int:
        """Minimum candles needed to generate signals."""
        ...

    def evaluate(self, candles: Sequence[Candle]) -> list[Signal]:
        """Analyze candles and generate trading signals.
        
        Args:
            candles: Sequence of candles (oldest first)
            
        Returns:
            List of signals (may be empty if no setups found)
        """
        ...


class Strategy(ABC):
    """Abstract base class for trading strategies.
    
    Subclasses implement specific pattern detection logic.
    All strategies must be:
    - Stateless: No internal state between evaluate() calls
    - Pure: Same input produces same output
    - Independent: No external dependencies or side effects
    
    Example:
        class MyStrategy(Strategy):
            def _evaluate_impl(self, candles):
                # Pattern detection logic
                if pattern_found:
                    return [Signal(...)]
                return []
    """

    def __init__(self, config: StrategyConfig) -> None:
        self._config = config
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate strategy configuration. Override for custom validation."""
        if self._config.risk_reward_min <= 0:
            raise ValueError("risk_reward_min must be positive")
        if self._config.max_risk_pct <= 0 or self._config.max_risk_pct > 10:
            raise ValueError("max_risk_pct must be between 0 and 10")
        if self._config.lookback_periods < 1:
            raise ValueError("lookback_periods must be at least 1")

    @property
    def name(self) -> str:
        """Strategy identifier."""
        return self._config.name

    @property
    def config(self) -> StrategyConfig:
        """Strategy configuration."""
        return self._config

    @property
    def required_candles(self) -> int:
        """Minimum candles needed for analysis."""
        return self._config.lookback_periods

    def evaluate(self, candles: Sequence[Candle]) -> list[Signal]:
        """Analyze candles and generate signals.
        
        Validates input and delegates to _evaluate_impl.
        
        Args:
            candles: Sequence of candles (oldest first)
            
        Returns:
            List of signals or empty list
        """
        if not self._config.enabled:
            return []

        if len(candles) < self.required_candles:
            return []

        signals = self._evaluate_impl(candles)
        return self._filter_signals(signals)

    @abstractmethod
    def _evaluate_impl(self, candles: Sequence[Candle]) -> list[Signal]:
        """Implementation of pattern detection.
        
        Override in subclasses with specific logic.
        
        Args:
            candles: Validated sequence of candles
            
        Returns:
            List of potential signals
        """
        ...

    def _filter_signals(self, signals: list[Signal]) -> list[Signal]:
        """Filter signals based on strategy criteria."""
        return [
            s for s in signals
            if s.risk_reward_ratio >= self._config.risk_reward_min
            and s.risk_pct <= self._config.max_risk_pct
        ]

    def get_param(self, key: str, default: float | int | str | bool) -> float | int | str | bool:
        """Get a strategy-specific parameter."""
        return self._config.params.get(key, default)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, enabled={self._config.enabled})"
