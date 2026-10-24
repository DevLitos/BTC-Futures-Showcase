"""Order Block detector for ICT strategy patterns."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.domain.models.candle import Candle


class OrderBlockType(Enum):
    """Type of Order Block."""

    BULLISH = "BULLISH"  # Last down candle before up move - demand zone
    BEARISH = "BEARISH"  # Last up candle before down move - supply zone


class OrderBlockStatus(Enum):
    """Current status of an Order Block."""

    ACTIVE = "ACTIVE"  # Valid and untested
    TESTED = "TESTED"  # Price has touched but not broken
    BROKEN = "BROKEN"  # Price has traded through
    EXPIRED = "EXPIRED"  # Too old or too far from price


@dataclass(slots=True)
class OrderBlock:
    """Represents an Order Block in price action.

    An Order Block is the last opposing candle before a strong move:
    - Bullish OB: Last bearish candle before bullish move (demand)
    - Bearish OB: Last bullish candle before bearish move (supply)

    Attributes:
        ob_type: BULLISH or BEARISH
        high: Upper boundary of the OB
        low: Lower boundary of the OB
        formed_at: Timestamp when OB was formed
        status: Current validity status
        strength: Number of candles in the move following formation
    """

    ob_type: OrderBlockType
    high: float
    low: float
    formed_at: datetime
    formation_candle_idx: int
    status: OrderBlockStatus = OrderBlockStatus.ACTIVE
    strength: int = 1
    test_count: int = 0

    @property
    def midpoint(self) -> float:
        """Middle of the OB zone."""
        return (self.high + self.low) / 2

    @property
    def size(self) -> float:
        """OB zone size in price units."""
        return self.high - self.low

    @property
    def is_valid(self) -> bool:
        """True if OB is still tradeable."""
        return self.status in (OrderBlockStatus.ACTIVE, OrderBlockStatus.TESTED)

    def contains_price(self, price: float) -> bool:
        """Check if price is within OB zone."""
        return self.low <= price <= self.high

    def update_status(self, current_candle: Candle) -> None:
        """Update OB status based on current price action."""
        if self.status in (OrderBlockStatus.BROKEN, OrderBlockStatus.EXPIRED):
            return

        if self.ob_type == OrderBlockType.BULLISH:
            # Bullish OB should act as support
            if current_candle.close < self.low:
                # Closed below = broken
                self.status = OrderBlockStatus.BROKEN
            elif current_candle.low <= self.high:
                # Price touched the zone
                if self.status == OrderBlockStatus.ACTIVE:
                    self.status = OrderBlockStatus.TESTED
                    self.test_count = 1
                else:
                    self.test_count += 1
        else:
            # Bearish OB should act as resistance
            if current_candle.close > self.high:
                # Closed above = broken
                self.status = OrderBlockStatus.BROKEN
            elif current_candle.high >= self.low:
                # Price touched the zone
                if self.status == OrderBlockStatus.ACTIVE:
                    self.status = OrderBlockStatus.TESTED
                    self.test_count = 1
                else:
                    self.test_count += 1


def detect_order_blocks(
    candles: Sequence[Candle], min_move_pct: float = 0.5, min_move_candles: int = 3
) -> list[OrderBlock]:
    """Detect Order Blocks in candle data.

    Looks for the last opposing candle before a strong directional move.

    Args:
        candles: Sequence of candles (oldest first)
        min_move_pct: Minimum move size as % of price
        min_move_candles: Minimum candles in the following move

    Returns:
        List of detected Order Blocks
    """
    if len(candles) < min_move_candles + 2:
        return []

    order_blocks: list[OrderBlock] = []

    # Scan for potential OBs
    for i in range(len(candles) - min_move_candles - 1):
        potential_ob = candles[i]

        # Check for bullish OB (bearish candle before bullish move)
        if not potential_ob.is_bullish:
            # Check if following candles make bullish move
            bullish_count, move_size = _analyze_move(candles[i + 1 : i + 1 + min_move_candles + 2], is_bullish=True)

            move_pct = (move_size / potential_ob.close) * 100 if potential_ob.close > 0 else 0

            if bullish_count >= min_move_candles and move_pct >= min_move_pct:
                ob = OrderBlock(
                    ob_type=OrderBlockType.BULLISH,
                    high=potential_ob.high,
                    low=potential_ob.low,
                    formed_at=potential_ob.timestamp,
                    formation_candle_idx=i,
                    strength=bullish_count,
                )
                order_blocks.append(ob)

        # Check for bearish OB (bullish candle before bearish move)
        elif potential_ob.is_bullish:
            # Check if following candles make bearish move
            bearish_count, move_size = _analyze_move(candles[i + 1 : i + 1 + min_move_candles + 2], is_bullish=False)

            move_pct = (move_size / potential_ob.close) * 100 if potential_ob.close > 0 else 0

            if bearish_count >= min_move_candles and move_pct >= min_move_pct:
                ob = OrderBlock(
                    ob_type=OrderBlockType.BEARISH,
                    high=potential_ob.high,
                    low=potential_ob.low,
                    formed_at=potential_ob.timestamp,
                    formation_candle_idx=i,
                    strength=bearish_count,
                )
                order_blocks.append(ob)

    return order_blocks


def _analyze_move(candles: Sequence[Candle], is_bullish: bool) -> tuple[int, float]:
    """Analyze a sequence of candles for directional move.

    Args:
        candles: Candles to analyze
        is_bullish: True to check for bullish move

    Returns:
        Tuple of (consecutive candle count, total move size)
    """
    if not candles:
        return 0, 0.0

    count = 0
    start_price = candles[0].open
    end_price = candles[0].close

    for c in candles:
        if is_bullish:
            if c.is_bullish:
                count += 1
                end_price = c.close
            else:
                break
        else:
            if not c.is_bullish:
                count += 1
                end_price = c.close
            else:
                break

    move_size = abs(end_price - start_price)
    return count, move_size


class OrderBlockDetector:
    """Order Block detection and tracking service.

    Maintains a list of active Order Blocks, updating their
    status as new price data arrives.

    Example:
        detector = OrderBlockDetector(min_move_pct=0.3)
        detector.update(candles)
        bullish_obs = detector.get_active_obs(OrderBlockType.BULLISH)
    """

    def __init__(
        self, min_move_pct: float = 0.5, min_move_candles: int = 3, max_obs: int = 20, max_tests: int = 3
    ) -> None:
        """Initialize Order Block detector.

        Args:
            min_move_pct: Minimum move size as % of price
            min_move_candles: Minimum candles in following move
            max_obs: Maximum OBs to track
            max_tests: Maximum tests before OB is considered weak
        """
        self._min_move_pct = min_move_pct
        self._min_move_candles = min_move_candles
        self._max_obs = max_obs
        self._max_tests = max_tests
        self._order_blocks: list[OrderBlock] = []
        self._last_processed_idx = -1

    @property
    def active_obs(self) -> list[OrderBlock]:
        """All currently valid Order Blocks."""
        return [ob for ob in self._order_blocks if ob.is_valid]

    def update(self, candles: Sequence[Candle]) -> list[OrderBlock]:
        """Update OB tracking with new candle data.

        Args:
            candles: Latest candle data

        Returns:
            List of newly detected OBs
        """
        if len(candles) < self._min_move_candles + 2:
            return []

        current_candle = candles[-1]

        # Update status of existing OBs
        for ob in self._order_blocks:
            ob.update_status(current_candle)

        # Detect new OBs (only process new candles)
        start_idx = max(0, self._last_processed_idx - self._min_move_candles - 1)
        if start_idx < len(candles) - self._min_move_candles - 1:
            new_obs = detect_order_blocks(candles, self._min_move_pct, self._min_move_candles)
            # Filter to only truly new ones
            existing_idxs = {ob.formation_candle_idx for ob in self._order_blocks}
            new_obs = [ob for ob in new_obs if ob.formation_candle_idx not in existing_idxs]
            self._order_blocks.extend(new_obs)
        else:
            new_obs = []

        self._last_processed_idx = len(candles) - 1

        # Prune invalid OBs and those with too many tests
        self._order_blocks = [ob for ob in self._order_blocks if ob.is_valid and ob.test_count <= self._max_tests][
            -self._max_obs :
        ]

        return new_obs

    def get_active_obs(self, ob_type: OrderBlockType | None = None) -> list[OrderBlock]:
        """Get active OBs, optionally filtered by type.

        Args:
            ob_type: Optional filter for BULLISH or BEARISH

        Returns:
            List of matching OBs
        """
        if ob_type is None:
            return self.active_obs
        return [ob for ob in self.active_obs if ob.ob_type == ob_type]

    def get_nearest_ob(self, current_price: float, ob_type: OrderBlockType | None = None) -> OrderBlock | None:
        """Get the OB nearest to current price.

        Args:
            current_price: Current market price
            ob_type: Optional filter

        Returns:
            Nearest OB or None
        """
        candidates = self.get_active_obs(ob_type)
        if not candidates:
            return None

        return min(candidates, key=lambda ob: abs(ob.midpoint - current_price))

    def get_ob_at_price(self, price: float) -> OrderBlock | None:
        """Find OB that contains the given price.

        Args:
            price: Price to check

        Returns:
            OB containing price, or None
        """
        for ob in self.active_obs:
            if ob.contains_price(price):
                return ob
        return None

    def reset(self) -> None:
        """Clear all tracked Order Blocks."""
        self._order_blocks.clear()
        self._last_processed_idx = -1
