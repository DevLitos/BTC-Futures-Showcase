"""Order Block Strategy.

This strategy trades Order Block zones - the last opposing candle
before a strong directional move, which acts as support/resistance.

BALANCED FILTERS:
- Requires dual EMA trend alignment
- OB must be fresh (first test only)
- Candle structure confirms rejection
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from src.domain.indicators.atr import ATRIndicator
from src.domain.indicators.ema import EMAIndicator
from src.domain.indicators.order_block_detector import OrderBlockDetector, OrderBlockType, OrderBlock
from src.domain.models.candle import Candle
from src.domain.models.signal import Signal, SignalDirection, SignalStrength, SignalType
from src.domain.strategies.base import Strategy, StrategyConfig


class OrderBlockStrategy(Strategy):
    """Order Block Strategy with strict filters.
    
    Entry conditions:
    - OB must be fresh (max 2 tests)
    - Trend confirmed by dual EMA
    - Candle shows clear rejection from OB zone
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        """Initialize Order Block strategy."""
        if config is None:
            config = StrategyConfig(
                name="ORDER_BLOCK",
                lookback_periods=50,
                risk_reward_min=2.0,
                max_risk_pct=2.0,
                params={
                    "ema_fast": 9,
                    "ema_slow": 21,
                    "atr_period": 14,
                    "risk_reward": 2.0,     # Lower RR for better WR
                    "min_move_pct": 1.0,    # Only strong moves
                    "min_move_candles": 5,  # More confirmation
                    "max_tests": 1,         # First test only
                }
            )
        super().__init__(config)
        
        ema_fast = int(self.get_param("ema_fast", 9))
        ema_slow = int(self.get_param("ema_slow", 21))
        atr_period = int(self.get_param("atr_period", 14))
        min_move_pct = float(self.get_param("min_move_pct", 0.8))
        min_move_candles = int(self.get_param("min_move_candles", 4))
        
        self._ema = EMAIndicator(periods=[ema_fast, ema_slow])
        self._atr = ATRIndicator(period=atr_period)
        self._ob_detector = OrderBlockDetector(
            min_move_pct=min_move_pct,
            min_move_candles=min_move_candles
        )
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow

    def _evaluate_impl(self, candles: Sequence[Candle]) -> list[Signal]:
        """Evaluate candles for Order Block setups."""
        if len(candles) < self.required_candles:
            return []
        
        signals: list[Signal] = []
        
        self._ob_detector.update(candles)
        
        current = candles[-1]
        current_price = current.close
        current_time = current.timestamp
        
        ema_values = self._ema.get_current(candles)
        ema_fast = ema_values.get(self._ema_fast, current_price)
        ema_slow = ema_values.get(self._ema_slow, current_price)
        atr_value = self._atr.get_current(candles)
        
        if atr_value <= 0:
            return []
        
        # Trend filters - same as FVG strategy
        is_uptrend = current_price > ema_fast and ema_fast > ema_slow
        is_downtrend = current_price < ema_fast and ema_fast < ema_slow
        
        risk_reward = float(self.get_param("risk_reward", 2.5))
        max_tests = int(self.get_param("max_tests", 2))
        
        # Only process ONE signal per evaluation
        best_signal: Signal | None = None
        
        for ob in self._ob_detector.active_obs:
            # Skip over-tested OBs
            if ob.test_count > max_tests:
                continue
            
            # Check trend alignment
            if ob.ob_type == OrderBlockType.BULLISH and not is_uptrend:
                continue
            if ob.ob_type == OrderBlockType.BEARISH and not is_downtrend:
                continue
            
            # Check price interaction
            if not self._is_price_touching_ob(current, ob):
                continue
            
            # Create signal
            if ob.ob_type == OrderBlockType.BULLISH:
                if self._has_bullish_rejection(current, ob):
                    signal = self._create_long_signal(ob, current, current_time, atr_value, risk_reward)
                    if signal and (best_signal is None or ob.strength > 0.5):
                        best_signal = signal
            else:
                if self._has_bearish_rejection(current, ob):
                    signal = self._create_short_signal(ob, current, current_time, atr_value, risk_reward)
                    if signal and (best_signal is None or ob.strength > 0.5):
                        best_signal = signal
        
        if best_signal:
            signals.append(best_signal)
        
        return signals

    def _is_price_touching_ob(self, candle: Candle, ob: OrderBlock) -> bool:
        """Check if candle touched the OB zone."""
        if ob.ob_type == OrderBlockType.BULLISH:
            return candle.low <= ob.high and candle.high >= ob.low
        else:
            return candle.high >= ob.low and candle.low <= ob.high

    def _has_bullish_rejection(self, candle: Candle, ob: OrderBlock) -> bool:
        """Check for bullish rejection at demand zone."""
        # Must be bullish candle
        if not candle.is_bullish:
            return False
        # Low must touch zone
        if candle.low > ob.high:
            return False
        # Close must be above zone
        if candle.close <= ob.high:
            return False
        return True

    def _has_bearish_rejection(self, candle: Candle, ob: OrderBlock) -> bool:
        """Check for bearish rejection at supply zone."""
        # Must be bearish candle
        if candle.is_bullish:
            return False
        # High must touch zone
        if candle.high < ob.low:
            return False
        # Close must be below zone
        if candle.close >= ob.low:
            return False
        return True

    def _create_long_signal(
        self,
        ob: OrderBlock,
        candle: Candle,
        timestamp: datetime,
        atr_value: float,
        risk_reward: float
    ) -> Signal | None:
        """Create a long signal from bullish OB."""
        entry_price = candle.close
        stop_loss = ob.low - (atr_value * 0.3)
        risk = entry_price - stop_loss
        take_profit = entry_price + (risk * risk_reward)
        
        if stop_loss <= 0 or take_profit <= entry_price or risk <= 0:
            return None
        
        strength = SignalStrength.STRONG if ob.strength > 0.7 else SignalStrength.MODERATE
        
        return Signal(
            timestamp=timestamp,
            direction=SignalDirection.LONG,
            signal_type=SignalType.ORDER_BLOCK,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            strategy_name=self.name,
            metadata={
                "ob_type": ob.ob_type.value,
                "ob_high": ob.high,
                "ob_low": ob.low,
                "ob_strength": ob.strength,
                "test_count": ob.test_count,
            }
        )

    def _create_short_signal(
        self,
        ob: OrderBlock,
        candle: Candle,
        timestamp: datetime,
        atr_value: float,
        risk_reward: float
    ) -> Signal | None:
        """Create a short signal from bearish OB."""
        entry_price = candle.close
        stop_loss = ob.high + (atr_value * 0.3)
        risk = stop_loss - entry_price
        take_profit = entry_price - (risk * risk_reward)
        
        if take_profit <= 0 or take_profit >= entry_price or risk <= 0:
            return None
        
        strength = SignalStrength.STRONG if ob.strength > 0.7 else SignalStrength.MODERATE
        
        return Signal(
            timestamp=timestamp,
            direction=SignalDirection.SHORT,
            signal_type=SignalType.ORDER_BLOCK,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            strategy_name=self.name,
            metadata={
                "ob_type": ob.ob_type.value,
                "ob_high": ob.high,
                "ob_low": ob.low,
                "ob_strength": ob.strength,
                "test_count": ob.test_count,
            }
        )

    def reset(self) -> None:
        """Reset strategy state."""
        self._ob_detector.reset()
