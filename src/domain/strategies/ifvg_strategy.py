"""IFVG (Inverse Fair Value Gap) Strategy.

This strategy trades FVG zones that have been filled and are now
acting as support/resistance (IFVG concept from ICT).

When an FVG gets filled:
- Bullish FVG becomes support zone
- Bearish FVG becomes resistance zone

BALANCED FILTERS:
- Dual EMA trend alignment required
- Strong rejection candle at IFVG zone
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from src.domain.indicators.atr import ATRIndicator
from src.domain.indicators.ema import EMAIndicator
from src.domain.indicators.fvg_detector import FVGDetector, FVGType, InverseFVG
from src.domain.models.candle import Candle
from src.domain.models.signal import Signal, SignalDirection, SignalStrength, SignalType
from src.domain.strategies.base import Strategy, StrategyConfig


class IFVGStrategy(Strategy):
    """Inverse FVG Strategy with trend filters.
    
    Entry conditions:
    - IFVG zone exists (filled FVG)
    - Trend confirmed by dual EMA
    - Strong rejection candle pattern
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        """Initialize IFVG strategy."""
        if config is None:
            config = StrategyConfig(
                name="IFVG",
                lookback_periods=50,
                risk_reward_min=2.0,
                max_risk_pct=2.0,
                params={
                    "ema_fast": 9,
                    "ema_slow": 21,
                    "atr_period": 14,
                    "risk_reward": 2.5,
                    "min_fvg_pct": 0.05,
                    "ifvg_min_fill_pct": 70.0,  # Only well-filled FVGs
                }
            )
        super().__init__(config)
        
        ema_fast = int(self.get_param("ema_fast", 9))
        ema_slow = int(self.get_param("ema_slow", 21))
        atr_period = int(self.get_param("atr_period", 14))
        min_fvg_pct = float(self.get_param("min_fvg_pct", 0.05))
        ifvg_min_fill = float(self.get_param("ifvg_min_fill_pct", 70.0))
        
        self._ema = EMAIndicator(periods=[ema_fast, ema_slow])
        self._atr = ATRIndicator(period=atr_period)
        self._fvg_detector = FVGDetector(
            min_gap_pct=min_fvg_pct,
            ifvg_min_fill_pct=ifvg_min_fill
        )
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow

    def _evaluate_impl(self, candles: Sequence[Candle]) -> list[Signal]:
        """Evaluate candles for IFVG setups."""
        if len(candles) < self.required_candles:
            return []
        
        signals: list[Signal] = []
        
        self._fvg_detector.update(candles)
        
        current = candles[-1]
        current_price = current.close
        current_time = current.timestamp
        
        ema_values = self._ema.get_current(candles)
        ema_fast = ema_values.get(self._ema_fast, current_price)
        ema_slow = ema_values.get(self._ema_slow, current_price)
        atr_value = self._atr.get_current(candles)
        
        if atr_value <= 0:
            return []
        
        # Trend filters - same as other strategies
        is_uptrend = current_price > ema_fast and ema_fast > ema_slow
        is_downtrend = current_price < ema_fast and ema_fast < ema_slow
        
        risk_reward = float(self.get_param("risk_reward", 2.5))
        
        for ifvg in self._fvg_detector.active_ifvgs:
            if not ifvg.is_price_in_zone(current_price):
                continue
            
            # Bullish IFVG (support) - only in uptrend
            if ifvg.fvg_type == FVGType.BULLISH and is_uptrend:
                if self._has_rejection(current, ifvg, is_long=True):
                    signal = self._create_long_signal(
                        ifvg, current, current_time, atr_value, risk_reward
                    )
                    if signal:
                        signals.append(signal)
                        break  # One signal per eval
            
            # Bearish IFVG (resistance) - only in downtrend
            elif ifvg.fvg_type == FVGType.BEARISH and is_downtrend:
                if self._has_rejection(current, ifvg, is_long=False):
                    signal = self._create_short_signal(
                        ifvg, current, current_time, atr_value, risk_reward
                    )
                    if signal:
                        signals.append(signal)
                        break
        
        return signals

    def _has_rejection(self, candle: Candle, ifvg: InverseFVG, is_long: bool) -> bool:
        """Check for rejection candle at IFVG zone."""
        if is_long:
            # Bullish candle with wick into zone
            has_wick = candle.low <= ifvg.reaction_zone_top
            closed_above = candle.close > ifvg.reaction_zone_top
            return candle.is_bullish and has_wick and closed_above
        else:
            # Bearish candle with wick into zone
            has_wick = candle.high >= ifvg.reaction_zone_bottom
            closed_below = candle.close < ifvg.reaction_zone_bottom
            return not candle.is_bullish and has_wick and closed_below

    def _create_long_signal(
        self,
        ifvg: InverseFVG,
        candle: Candle,
        timestamp: datetime,
        atr_value: float,
        risk_reward: float
    ) -> Signal | None:
        """Create a long signal from bullish IFVG."""
        entry_price = candle.close
        stop_loss = ifvg.reaction_zone_bottom - (atr_value * 0.3)
        risk = entry_price - stop_loss
        take_profit = entry_price + (risk * risk_reward)
        
        if stop_loss <= 0 or take_profit <= entry_price or risk <= 0:
            return None
        
        return Signal(
            timestamp=timestamp,
            direction=SignalDirection.LONG,
            signal_type=SignalType.IFVG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=SignalStrength.MODERATE,
            strategy_name=self.name,
            metadata={
                "ifvg_type": ifvg.fvg_type.value,
                "zone_top": ifvg.reaction_zone_top,
                "zone_bottom": ifvg.reaction_zone_bottom,
            }
        )

    def _create_short_signal(
        self,
        ifvg: InverseFVG,
        candle: Candle,
        timestamp: datetime,
        atr_value: float,
        risk_reward: float
    ) -> Signal | None:
        """Create a short signal from bearish IFVG."""
        entry_price = candle.close
        stop_loss = ifvg.reaction_zone_top + (atr_value * 0.3)
        risk = stop_loss - entry_price
        take_profit = entry_price - (risk * risk_reward)
        
        if take_profit <= 0 or take_profit >= entry_price or risk <= 0:
            return None
        
        return Signal(
            timestamp=timestamp,
            direction=SignalDirection.SHORT,
            signal_type=SignalType.IFVG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=SignalStrength.MODERATE,
            strategy_name=self.name,
            metadata={
                "ifvg_type": ifvg.fvg_type.value,
                "zone_top": ifvg.reaction_zone_top,
                "zone_bottom": ifvg.reaction_zone_bottom,
            }
        )

    def reset(self) -> None:
        """Reset strategy state."""
        self._fvg_detector.reset()
