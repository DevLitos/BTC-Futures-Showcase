"""FVG (Fair Value Gap) detector for ICT strategy patterns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

from src.domain.models.candle import Candle

logger = logging.getLogger("trading")


class FVGType(Enum):
    """Type of Fair Value Gap."""
    BULLISH = "BULLISH"  # Gap up - potential support
    BEARISH = "BEARISH"  # Gap down - potential resistance


class FVGStatus(Enum):
    """Current status of an FVG."""
    UNFILLED = "UNFILLED"      # Gap not yet tested
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # Price entered gap
    FILLED = "FILLED"          # Gap completely filled
    INVALIDATED = "INVALIDATED"  # Gap traded through


@dataclass(slots=True)
class FairValueGap:
    """Represents a Fair Value Gap in price action.
    
    An FVG occurs when there's a gap between:
    - Candle 1's high and Candle 3's low (bullish FVG)
    - Candle 1's low and Candle 3's high (bearish FVG)
    
    With Candle 2 being a strong momentum candle.
    
    Attributes:
        fvg_type: BULLISH or BEARISH
        top: Upper boundary of the gap
        bottom: Lower boundary of the gap
        formed_at: Timestamp when gap was formed
        formation_candle_idx: Index of middle candle in formation
        status: Current fill status
    """
    fvg_type: FVGType
    top: float
    bottom: float
    formed_at: datetime
    formation_candle_idx: int
    status: FVGStatus = FVGStatus.UNFILLED
    fill_percentage: float = 0.0
    
    @property
    def midpoint(self) -> float:
        """Middle of the gap - optimal entry point."""
        return (self.top + self.bottom) / 2
    
    @property
    def size(self) -> float:
        """Gap size in price units."""
        return self.top - self.bottom
    
    @property
    def is_valid(self) -> bool:
        """True if FVG is still tradeable."""
        return self.status in (FVGStatus.UNFILLED, FVGStatus.PARTIALLY_FILLED)
    
    def update_status(self, current_price: float) -> None:
        """Update FVG status based on current price."""
        if self.status in (FVGStatus.FILLED, FVGStatus.INVALIDATED):
            return
        
        if self.fvg_type == FVGType.BULLISH:
            # Price should come down to fill gap
            if current_price < self.bottom:
                self.status = FVGStatus.INVALIDATED
            elif current_price <= self.top:
                filled_amount = self.top - current_price
                self.fill_percentage = (filled_amount / self.size) * 100
                if self.fill_percentage >= 100:
                    self.status = FVGStatus.FILLED
                else:
                    self.status = FVGStatus.PARTIALLY_FILLED
        else:
            # Bearish: price should come up to fill gap
            if current_price > self.top:
                self.status = FVGStatus.INVALIDATED
            elif current_price >= self.bottom:
                filled_amount = current_price - self.bottom
                self.fill_percentage = (filled_amount / self.size) * 100
                if self.fill_percentage >= 100:
                    self.status = FVGStatus.FILLED
                else:
                    self.status = FVGStatus.PARTIALLY_FILLED


@dataclass(slots=True)
class InverseFVG:
    """Represents an Inverse FVG (IFVG).
    
    An IFVG is a regular FVG that has been filled and now acts
    as support (for filled bullish) or resistance (for filled bearish).
    
    After being filled, the gap zone becomes a key level for price reaction.
    """
    original_fvg: FairValueGap
    filled_at: datetime
    reaction_zone_top: float
    reaction_zone_bottom: float
    
    @property
    def fvg_type(self) -> FVGType:
        """Original FVG type."""
        return self.original_fvg.fvg_type
    
    @property
    def midpoint(self) -> float:
        """Midpoint of reaction zone."""
        return (self.reaction_zone_top + self.reaction_zone_bottom) / 2
    
    def is_price_in_zone(self, price: float) -> bool:
        """Check if price is within the reaction zone."""
        return self.reaction_zone_bottom <= price <= self.reaction_zone_top


def detect_fvg(candles: Sequence[Candle], min_gap_pct: float = 0.1, use_imbalance: bool = True) -> list[FairValueGap]:
    """Detect Fair Value Gaps in candle data.
    
    An FVG forms when there's an imbalance between buyers and sellers,
    creating a gap in price that the market typically revisits.
    
    Strict FVG:
    - Bullish FVG: Candle[0].high < Candle[2].low (gap up)
    - Bearish FVG: Candle[0].low > Candle[2].high (gap down)
    
    Imbalance (relaxed - more signals):
    - Bullish: Strong up candle with body > 60% of range, close near high
    - Bearish: Strong down candle with body > 60% of range, close near low
    
    Args:
        candles: Sequence of candles (oldest first)
        min_gap_pct: Minimum gap size as % of price (strict mode)
        use_imbalance: If True, also detect momentum imbalances
        
    Returns:
        List of detected FVGs
    """
    if len(candles) < 3:
        return []
    
    fvgs: list[FairValueGap] = []
    
    for i in range(2, len(candles)):
        c1 = candles[i - 2]  # First candle
        c2 = candles[i - 1]  # Middle candle (momentum)
        c3 = candles[i]      # Third candle
        
        # === STRICT FVG: True gap between c1 high and c3 low ===
        # Check for bullish FVG: C1 high < C3 low
        if c1.high < c3.low:
            gap_size = c3.low - c1.high
            gap_pct = (gap_size / c2.close) * 100
            
            if gap_pct >= min_gap_pct:
                fvg = FairValueGap(
                    fvg_type=FVGType.BULLISH,
                    top=c3.low,
                    bottom=c1.high,
                    formed_at=c3.timestamp,
                    formation_candle_idx=i - 1
                )
                fvgs.append(fvg)
                continue  # Don't double-count
        
        # Check for bearish FVG: C1 low > C3 high
        elif c1.low > c3.high:
            gap_size = c1.low - c3.high
            gap_pct = (gap_size / c2.close) * 100
            
            if gap_pct >= min_gap_pct:
                fvg = FairValueGap(
                    fvg_type=FVGType.BEARISH,
                    top=c1.low,
                    bottom=c3.high,
                    formed_at=c3.timestamp,
                    formation_candle_idx=i - 1
                )
                fvgs.append(fvg)
                continue
        
        # === IMBALANCE DETECTION (more relaxed) ===
        if use_imbalance:
            c2_range = c2.high - c2.low
            if c2_range <= 0:
                continue
                
            c2_body = abs(c2.close - c2.open)
            body_ratio = c2_body / c2_range
            
            # Strong momentum candle: body > 60% of range
            if body_ratio < 0.6:
                continue
            
            # Calculate momentum
            momentum_pct = abs(c2.close - c2.open) / c2.open * 100
            
            # Need at least 0.05% momentum
            if momentum_pct < 0.05:
                continue
            
            # Bullish imbalance: strong up candle
            if c2.close > c2.open:
                # The imbalance zone is the body of the candle
                fvg = FairValueGap(
                    fvg_type=FVGType.BULLISH,
                    top=c2.close,
                    bottom=c2.open,
                    formed_at=c2.timestamp,
                    formation_candle_idx=i - 1
                )
                fvgs.append(fvg)
            
            # Bearish imbalance: strong down candle
            elif c2.close < c2.open:
                fvg = FairValueGap(
                    fvg_type=FVGType.BEARISH,
                    top=c2.open,
                    bottom=c2.close,
                    formed_at=c2.timestamp,
                    formation_candle_idx=i - 1
                )
                fvgs.append(fvg)
    
    return fvgs
    
    return fvgs


def detect_ifvg(
    candles: Sequence[Candle],
    existing_fvgs: list[FairValueGap],
    min_fill_pct: float = 50.0
) -> list[InverseFVG]:
    """Detect Inverse FVGs from filled regular FVGs.
    
    When an FVG gets filled (price trades through it), it becomes
    an IFVG which can act as support/resistance on retests.
    
    Args:
        candles: Current candle data
        existing_fvgs: List of previously detected FVGs
        min_fill_pct: Minimum fill percentage to consider IFVG
        
    Returns:
        List of IFVGs
    """
    if not candles or not existing_fvgs:
        return []
    
    current_price = candles[-1].close
    ifvgs: list[InverseFVG] = []
    
    for fvg in existing_fvgs:
        # Update FVG status
        fvg.update_status(current_price)
        
        # Check if it qualifies as IFVG
        if fvg.fill_percentage >= min_fill_pct and fvg.status != FVGStatus.INVALIDATED:
            ifvg = InverseFVG(
                original_fvg=fvg,
                filled_at=candles[-1].timestamp,
                reaction_zone_top=fvg.top,
                reaction_zone_bottom=fvg.bottom
            )
            ifvgs.append(ifvg)
    
    return ifvgs


class FVGDetector:
    """FVG detection and tracking service.
    
    Maintains a list of active FVGs and IFVGs, updating their
    status as new price data arrives.
    
    Example:
        detector = FVGDetector(min_gap_pct=0.15, max_fvgs=20)
        detector.update(candles)
        active_bullish = detector.get_active_fvgs(FVGType.BULLISH)
    """

    def __init__(
        self,
        min_gap_pct: float = 0.1,
        max_fvgs: int = 50,
        ifvg_min_fill_pct: float = 50.0
    ) -> None:
        """Initialize FVG detector.
        
        Args:
            min_gap_pct: Minimum gap size as % of price
            max_fvgs: Maximum FVGs to track
            ifvg_min_fill_pct: Minimum fill % to create IFVG
        """
        self._min_gap_pct = min_gap_pct
        self._max_fvgs = max_fvgs
        self._ifvg_min_fill_pct = ifvg_min_fill_pct
        self._fvgs: list[FairValueGap] = []
        self._ifvgs: list[InverseFVG] = []
        self._last_processed_idx = -1

    @property
    def active_fvgs(self) -> list[FairValueGap]:
        """All currently valid FVGs."""
        return [f for f in self._fvgs if f.is_valid]

    @property
    def active_ifvgs(self) -> list[InverseFVG]:
        """All IFVGs."""
        return self._ifvgs.copy()

    def update(self, candles: Sequence[Candle]) -> tuple[list[FairValueGap], list[InverseFVG]]:
        """Update FVG tracking with new candle data.
        
        Args:
            candles: Latest candle data
            
        Returns:
            Tuple of (newly detected FVGs, newly formed IFVGs)
        """
        if len(candles) < 3:
            return [], []
        
        current_price = candles[-1].close
        
        # Update status of existing FVGs
        newly_filled: list[FairValueGap] = []
        for fvg in self._fvgs:
            old_status = fvg.status
            fvg.update_status(current_price)
            if old_status != FVGStatus.FILLED and fvg.status == FVGStatus.FILLED:
                newly_filled.append(fvg)
        
        # Convert filled FVGs to IFVGs
        new_ifvgs = detect_ifvg(candles, newly_filled, self._ifvg_min_fill_pct)
        self._ifvgs.extend(new_ifvgs)
        
        # Detect new FVGs (only process new candles)
        start_idx = max(2, self._last_processed_idx + 1)
        if start_idx < len(candles):
            # Pass only the candles we need to check
            new_fvgs = detect_fvg(candles, self._min_gap_pct)
            # Filter to only truly new ones
            new_fvgs = [f for f in new_fvgs if f.formation_candle_idx >= start_idx - 1]
            self._fvgs.extend(new_fvgs)
            
            # Log new FVGs
            for fvg in new_fvgs:
                logger.info(
                    "[FVG] Detected %s gap: %.2f-%.2f (size=%.2f, %.3f%%)",
                    fvg.fvg_type.value, fvg.bottom, fvg.top,
                    fvg.size, (fvg.size / current_price) * 100
                )
        else:
            new_fvgs = []
        
        self._last_processed_idx = len(candles) - 1
        
        # Prune old/invalid FVGs
        self._fvgs = [f for f in self._fvgs if f.is_valid][-self._max_fvgs:]
        self._ifvgs = self._ifvgs[-self._max_fvgs:]
        
        return new_fvgs, new_ifvgs

    def get_active_fvgs(self, fvg_type: FVGType | None = None) -> list[FairValueGap]:
        """Get active FVGs, optionally filtered by type.
        
        Args:
            fvg_type: Optional filter for BULLISH or BEARISH
            
        Returns:
            List of matching FVGs
        """
        if fvg_type is None:
            return self.active_fvgs
        return [f for f in self.active_fvgs if f.fvg_type == fvg_type]

    def get_nearest_fvg(
        self,
        current_price: float,
        fvg_type: FVGType | None = None
    ) -> FairValueGap | None:
        """Get the FVG nearest to current price.
        
        Args:
            current_price: Current market price
            fvg_type: Optional filter
            
        Returns:
            Nearest FVG or None
        """
        candidates = self.get_active_fvgs(fvg_type)
        if not candidates:
            return None
        
        return min(candidates, key=lambda f: abs(f.midpoint - current_price))

    def get_fvg_at_price(self, price: float) -> FairValueGap | None:
        """Find FVG that contains the given price.
        
        Args:
            price: Price to check
            
        Returns:
            FVG containing price, or None
        """
        for fvg in self.active_fvgs:
            if fvg.bottom <= price <= fvg.top:
                return fvg
        return None

    def reset(self) -> None:
        """Clear all tracked FVGs."""
        self._fvgs.clear()
        self._ifvgs.clear()
        self._last_processed_idx = -1
