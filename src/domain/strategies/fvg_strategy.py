"""FVG (Fair Value Gap) + Trend Filter Strategy.

This strategy implements the ICT Fair Value Gap concept with trend confirmation:
1. Detects FVG patterns (imbalances in price)
2. Requires trend confirmation (EMA crossover or price position)
3. Waits for price to enter FVG zone
4. Uses FVG-based SL/TP for better risk management

BALANCED FILTERS:
- Trend confirmed by dual EMA
- FVG must be reasonably fresh (<60% fill)
- Candle structure confirms direction
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from src.domain.indicators.atr import ATRIndicator
from src.domain.indicators.ema import EMAIndicator
from src.domain.indicators.fvg_detector import FairValueGap, FVGDetector, FVGType
from src.domain.models.candle import Candle
from src.domain.models.signal import Signal, SignalDirection, SignalStrength, SignalType
from src.domain.strategies.base import Strategy, StrategyConfig

logger = logging.getLogger("trading")


class FVGTrendStrategy(Strategy):
    """FVG + Trend Filter Strategy.

    Entry conditions:
    1. FVG exists and is reasonably fresh (fill < 60%)
    2. Trend: price above/below EMA AND fast EMA above/below slow EMA
    3. Price is within or near FVG zone
    4. Current candle confirms direction

    Exit:
    - Stop Loss: Below FVG bottom (long) or above FVG top (short) with buffer
    - Take Profit: Based on risk * risk_reward
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        """Initialize FVG Trend strategy."""
        if config is None:
            config = StrategyConfig(
                name="FVG_TREND",
                lookback_periods=50,
                risk_reward_min=2.0,
                max_risk_pct=2.0,
                params={
                    "ema_fast": 9,
                    "ema_slow": 21,
                    "atr_period": 14,
                    "risk_reward": 2.5,  # Increased from 2.0
                    "min_fvg_pct": 0.02,
                    "max_fill_pct": 70,
                },
            )
        super().__init__(config)

        ema_fast = int(self.get_param("ema_fast", 9))
        ema_slow = int(self.get_param("ema_slow", 21))
        atr_period = int(self.get_param("atr_period", 14))
        min_fvg_pct = float(self.get_param("min_fvg_pct", 0.03))

        self._ema = EMAIndicator(periods=[ema_fast, ema_slow])
        self._atr = ATRIndicator(period=atr_period)
        self._fvg_detector = FVGDetector(min_gap_pct=min_fvg_pct)
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow

    def _evaluate_impl(self, candles: Sequence[Candle]) -> list[Signal]:
        """Evaluate candles for FVG + Trend setups."""
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

        # Trend filters
        is_uptrend = current_price > ema_fast and ema_fast > ema_slow
        is_downtrend = current_price < ema_fast and ema_fast < ema_slow

        # Candle confirms direction
        is_bullish_candle = current.close > current.open
        is_bearish_candle = current.close < current.open

        risk_reward = float(self.get_param("risk_reward", 2.5))
        max_fill = float(self.get_param("max_fill_pct", 60))

        # BULLISH setup
        if is_uptrend and is_bullish_candle:
            fvg = self._find_valid_fvg(current, FVGType.BULLISH, max_fill)
            if fvg:
                signal = self._create_signal(fvg, current, SignalDirection.LONG, current_time, risk_reward, atr_value)
                if signal:
                    signals.append(signal)

        # BEARISH setup
        if is_downtrend and is_bearish_candle:
            fvg = self._find_valid_fvg(current, FVGType.BEARISH, max_fill)
            if fvg:
                signal = self._create_signal(fvg, current, SignalDirection.SHORT, current_time, risk_reward, atr_value)
                if signal:
                    signals.append(signal)

        return signals

    def _find_valid_fvg(self, candle: Candle, fvg_type: FVGType, max_fill_pct: float) -> FairValueGap | None:
        """Find a valid FVG that candle interacts with."""
        active_fvgs = self._fvg_detector.get_active_fvgs(fvg_type)

        for fvg in active_fvgs:
            if fvg.fill_percentage > max_fill_pct:
                continue

            # Check if candle touched/entered FVG
            if fvg_type == FVGType.BULLISH:
                # Candle low should touch FVG, close should be above
                if candle.low <= fvg.top and candle.close >= fvg.bottom:
                    return fvg
            else:
                # Candle high should touch FVG, close should be below
                if candle.high >= fvg.bottom and candle.close <= fvg.top:
                    return fvg

        return None

    def _create_signal(
        self,
        fvg: FairValueGap,
        candle: Candle,
        direction: SignalDirection,
        timestamp: datetime,
        risk_reward: float,
        atr: float,
    ) -> Signal | None:
        """Create signal with FVG-based SL/TP."""
        entry_price = candle.close

        if direction == SignalDirection.LONG:
            # Balanced SL below FVG
            stop_loss = fvg.bottom - (atr * 0.25)
            risk = entry_price - stop_loss
            take_profit = entry_price + (risk * risk_reward)
        else:
            stop_loss = fvg.top + (atr * 0.25)
            risk = stop_loss - entry_price
            take_profit = entry_price - (risk * risk_reward)

        if stop_loss <= 0 or risk <= 0:
            return None
        if direction == SignalDirection.LONG and take_profit <= entry_price:
            return None
        if direction == SignalDirection.SHORT and take_profit >= entry_price:
            return None

        strength = SignalStrength.MODERATE
        if fvg.fill_percentage < 30:
            strength = SignalStrength.STRONG

        return Signal(
            timestamp=timestamp,
            direction=direction,
            signal_type=SignalType.FVG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            strategy_name=self.name,
            metadata={
                "fvg_top": fvg.top,
                "fvg_bottom": fvg.bottom,
                "fvg_fill_pct": fvg.fill_percentage,
                "risk_usd": risk,
            },
        )

    def reset(self) -> None:
        """Reset strategy state."""
        self._fvg_detector.reset()
