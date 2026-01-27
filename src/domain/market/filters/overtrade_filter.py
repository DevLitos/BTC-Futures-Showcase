"""
Overtrade Filter - Prevents same-zone re-entries and requires structure reset

Two main protections:
1. Same-Zone Prevention: No re-entry near last failed entry
2. HH/HL Reset Logic: After loss, require new structure before re-entry

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from dataclasses import dataclass
import logging

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


@dataclass
class ZoneTradeEntry:
    """Record of a recent trade for zone tracking (local to overtrade filter)"""
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    direction: str  # 'LONG' or 'SHORT'
    result: str     # 'WIN', 'LOSS', 'BE'
    pnl: float


class OvertradeFilter(BaseFilter):
    """
    Overtrade Filter with same-zone prevention and structure reset.
    
    Same-Zone Prevention:
    - After a loss, block re-entry if price is within zone_radius of last entry
    - Zone radius = 0.3 * ATR (or configurable %)
    - Block persists until timeout OR price exits zone significantly
    
    HH/HL Reset Logic:
    - After LONG loss: require price > highest(high, N) before new LONG
    - After SHORT loss: require price < lowest(low, N) before new SHORT
    - Alternative: EMA20 slope reversal + price above/below EMA20
    """
    
    # Same-zone parameters
    ZONE_RADIUS_ATR_MULT = 0.3      # Zone radius as ATR multiple
    ZONE_RADIUS_PCT = 0.0025        # Alternative: 0.25% of price
    ZONE_BLOCK_MINUTES = 30         # How long to block same-zone re-entry
    
    # HH/HL reset parameters
    RESET_LOOKBACK = 20             # Look for new HH/HL in last N candles
    EMA_RESET_PERIOD = 20           # EMA period for slope check
    EMA_SLOPE_LOOKBACK = 5          # Bars for slope calculation
    EMA_SLOPE_THRESHOLD = 0.0001    # Minimum slope for "reversal"
    
    # Score adjustments
    ZONE_BLOCK_PENALTY = -3.0       # If allowing zone re-entry with penalty
    NO_RESET_PENALTY = -2.0         # No structure reset yet
    FRESH_STRUCTURE_BONUS = 0.5     # New HH/HL confirmed
    
    def __init__(self, 
                 enabled: bool = True,
                 hard_block_zone: bool = True,
                 hard_block_no_reset: bool = False):
        super().__init__(enabled)
        self.hard_block_zone = hard_block_zone
        self.hard_block_no_reset = hard_block_no_reset
        
        # Trade history
        self._trade_history: List[ZoneTradeEntry] = []
        self._max_history = 10
        
        # Zone blocks
        self._zone_block_until: Optional[datetime] = None
        self._blocked_zone_center: Optional[float] = None
        self._blocked_zone_direction: Optional[str] = None
        
        # Reset state
        self._awaiting_reset_long: bool = False
        self._awaiting_reset_short: bool = False
        self._reset_reference_high: Optional[float] = None
        self._reset_reference_low: Optional[float] = None
    
    def record_trade(self, trade: ZoneTradeEntry):
        """Record a completed trade"""
        self._trade_history.append(trade)
        if len(self._trade_history) > self._max_history:
            self._trade_history.pop(0)
        
        # If loss, set up blocking conditions
        if trade.result == 'LOSS':
            self._blocked_zone_center = trade.entry_price
            self._blocked_zone_direction = trade.direction
            self._zone_block_until = trade.exit_time + timedelta(minutes=self.ZONE_BLOCK_MINUTES)
            
            if trade.direction == 'LONG':
                self._awaiting_reset_long = True
            else:
                self._awaiting_reset_short = True
            
            logger.info(f"🚫 ZONE BLOCK set: {trade.direction} @ {trade.entry_price:.2f} until {self._zone_block_until}")
    
    def record_trade_from_params(self,
                                  entry_price: float,
                                  exit_price: float,
                                  entry_time: datetime,
                                  exit_time: datetime,
                                  direction: str,
                                  pnl: float):
        """Convenience method to record trade from individual params"""
        if pnl > 0:
            result = 'WIN'
        elif pnl < 0:
            result = 'LOSS'
        else:
            result = 'BE'
        
        trade = ZoneTradeEntry(
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            direction=direction,
            result=result,
            pnl=pnl
        )
        self.record_trade(trade)
    
    def is_in_blocked_zone(self,
                           current_price: float,
                           atr: float,
                           bias: str,
                           market_time: datetime) -> Tuple[bool, str]:
        """
        Check if current price is in a blocked zone.
        
        Returns:
            (is_blocked, reason)
        """
        # Check if block has expired
        if self._zone_block_until is None or market_time >= self._zone_block_until:
            self._blocked_zone_center = None
            self._blocked_zone_direction = None
            self._zone_block_until = None
            return False, ""
        
        # Only block same direction
        if self._blocked_zone_direction != bias:
            return False, ""
        
        if self._blocked_zone_center is None:
            return False, ""
        
        # Calculate zone radius
        zone_radius = max(
            atr * self.ZONE_RADIUS_ATR_MULT,
            current_price * self.ZONE_RADIUS_PCT
        )
        
        distance = abs(current_price - self._blocked_zone_center)
        
        if distance <= zone_radius:
            remaining = (self._zone_block_until - market_time).total_seconds() / 60
            return True, f"Same-zone block: within {distance:.2f} of last {bias} loss @ {self._blocked_zone_center:.2f} ({remaining:.0f}min left)"
        
        return False, ""
    
    def check_structure_reset(self,
                              highs: np.ndarray,
                              lows: np.ndarray,
                              closes: np.ndarray,
                              current_price: float,
                              bias: str) -> Tuple[bool, str]:
        """
        Check if market structure has reset after a loss.
        
        For LONG: Need new HH or EMA20 slope positive + price > EMA20
        For SHORT: Need new LL or EMA20 slope negative + price < EMA20
        
        Returns:
            (has_reset, reason)
        """
        if len(highs) < self.RESET_LOOKBACK or len(closes) < self.EMA_RESET_PERIOD:
            return True, "Insufficient data for reset check"
        
        if bias == 'LONG' and self._awaiting_reset_long:
            # Check for new HH
            if self._reset_reference_high is None:
                self._reset_reference_high = np.max(highs[-self.RESET_LOOKBACK-5:-5])
            
            current_highest = np.max(highs[-self.RESET_LOOKBACK:])
            if current_highest > self._reset_reference_high:
                self._awaiting_reset_long = False
                self._reset_reference_high = None
                logger.info(f"✅ LONG RESET: New HH {current_highest:.2f} > ref {self._reset_reference_high:.2f}")
                return True, f"New HH: {current_highest:.2f}"
            
            # Alternative: EMA20 slope positive + price > EMA20
            ema20 = self._calculate_ema(closes, self.EMA_RESET_PERIOD)
            if len(ema20) >= self.EMA_SLOPE_LOOKBACK:
                slope = (ema20[-1] - ema20[-self.EMA_SLOPE_LOOKBACK]) / ema20[-self.EMA_SLOPE_LOOKBACK]
                if slope > self.EMA_SLOPE_THRESHOLD and current_price > ema20[-1]:
                    self._awaiting_reset_long = False
                    self._reset_reference_high = None
                    logger.info(f"✅ LONG RESET: EMA20 slope positive ({slope:.4f}) + price > EMA20")
                    return True, f"EMA20 reversal + price above"
            
            return False, f"Awaiting LONG reset: need HH > {self._reset_reference_high:.2f}"
        
        elif bias == 'SHORT' and self._awaiting_reset_short:
            # Check for new LL
            if self._reset_reference_low is None:
                self._reset_reference_low = np.min(lows[-self.RESET_LOOKBACK-5:-5])
            
            current_lowest = np.min(lows[-self.RESET_LOOKBACK:])
            if current_lowest < self._reset_reference_low:
                self._awaiting_reset_short = False
                self._reset_reference_low = None
                logger.info(f"✅ SHORT RESET: New LL {current_lowest:.2f} < ref {self._reset_reference_low:.2f}")
                return True, f"New LL: {current_lowest:.2f}"
            
            # Alternative: EMA20 slope negative + price < EMA20
            ema20 = self._calculate_ema(closes, self.EMA_RESET_PERIOD)
            if len(ema20) >= self.EMA_SLOPE_LOOKBACK:
                slope = (ema20[-1] - ema20[-self.EMA_SLOPE_LOOKBACK]) / ema20[-self.EMA_SLOPE_LOOKBACK]
                if slope < -self.EMA_SLOPE_THRESHOLD and current_price < ema20[-1]:
                    self._awaiting_reset_short = False
                    self._reset_reference_low = None
                    logger.info(f"✅ SHORT RESET: EMA20 slope negative ({slope:.4f}) + price < EMA20")
                    return True, f"EMA20 reversal + price below"
            
            return False, f"Awaiting SHORT reset: need LL < {self._reset_reference_low:.2f}"
        
        return True, "No reset required"
    
    def _calculate_ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Calculate EMA"""
        if period <= 0 or len(data) < period:
            return np.array([np.nan])
        
        alpha = 2 / (period + 1)
        ema = np.zeros(len(data))
        ema[period-1] = np.mean(data[:period])
        
        for i in range(period, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
        
        return ema[period-1:]
    
    def evaluate(self,
                 df: pd.DataFrame | None = None,
                 current_price: float | None = None,
                 atr: float | None = None,
                 bias: str | None = None,
                 market_time: datetime | None = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate overtrade conditions.
        
        Args:
            df: OHLC DataFrame
            current_price: Current price
            atr: Current ATR
            bias: 'LONG' or 'SHORT'
            market_time: Current time
        """
        if current_price is None or bias is None:
            return FilterDecision.allow_default()
        
        market_time = market_time or datetime.utcnow()
        atr = atr or (current_price * 0.005)  # Default 0.5% ATR estimate
        
        tags: dict[str, bool | float | str | None] = {
            'zone_blocked': False,
            'awaiting_reset': False,
        }
        reasons = []
        
        # ===== 1. SAME-ZONE CHECK =====
        in_zone, zone_reason = self.is_in_blocked_zone(
            current_price, atr, bias, market_time
        )
        
        if in_zone:
            tags['zone_blocked'] = True
            tags['zone_center'] = self._blocked_zone_center
            tags['zone_direction'] = self._blocked_zone_direction
            
            if self.hard_block_zone:
                logger.warning(f"⛔ OVERTRADE BLOCK: {zone_reason}")
                return FilterDecision(
                    allow=False,
                    reasons=[zone_reason],
                    tags=tags
                )
            else:
                reasons.append(zone_reason)
                tags['zone_penalty'] = self.ZONE_BLOCK_PENALTY
        
        # ===== 2. STRUCTURE RESET CHECK =====
        if df is not None and len(df) >= self.RESET_LOOKBACK:
            highs = df['high'].to_numpy()
            lows = df['low'].to_numpy()
            closes = df['close'].to_numpy()
            
            has_reset, reset_reason = self.check_structure_reset(
                highs, lows, closes, current_price, bias
            )
            
            if not has_reset:
                tags['awaiting_reset'] = True
                tags['reset_reason'] = reset_reason
                
                if self.hard_block_no_reset:
                    logger.warning(f"⛔ OVERTRADE BLOCK: {reset_reason}")
                    return FilterDecision(
                        allow=False,
                        reasons=[reset_reason],
                        tags=tags
                    )
                else:
                    reasons.append(reset_reason)
            elif "New HH" in reset_reason or "New LL" in reset_reason:
                # Fresh structure - bonus
                tags['fresh_structure'] = True
                reasons.append(f"Fresh structure: {reset_reason}")
        
        # ===== FINAL DECISION =====
        total_penalty = 0.0
        
        if tags.get('zone_blocked'):
            total_penalty += abs(self.ZONE_BLOCK_PENALTY)
        
        if tags.get('awaiting_reset'):
            total_penalty += abs(self.NO_RESET_PENALTY)
        
        if tags.get('fresh_structure'):
            total_penalty -= self.FRESH_STRUCTURE_BONUS
        
        if total_penalty > 0:
            return FilterDecision(
                allow=True,
                score_adjust=-total_penalty,
                reasons=reasons if reasons else ["Overtrade risk"],
                tags=tags
            )
        elif total_penalty < 0:
            return FilterDecision(
                allow=True,
                score_adjust=abs(total_penalty),
                reasons=reasons if reasons else ["Fresh setup"],
                tags=tags
            )
        else:
            return FilterDecision(
                allow=True,
                reasons=["No overtrade risk"],
                tags=tags
            )
    
    def clear_blocks(self):
        """Clear all blocks (for testing)"""
        self._zone_block_until = None
        self._blocked_zone_center = None
        self._blocked_zone_direction = None
        self._awaiting_reset_long = False
        self._awaiting_reset_short = False
        self._reset_reference_high = None
        self._reset_reference_low = None
    
    def get_state(self) -> dict:
        """Get current filter state"""
        return {
            'zone_blocked': self._zone_block_until is not None,
            'zone_center': self._blocked_zone_center,
            'zone_direction': self._blocked_zone_direction,
            'zone_until': self._zone_block_until.isoformat() if self._zone_block_until else None,
            'awaiting_reset_long': self._awaiting_reset_long,
            'awaiting_reset_short': self._awaiting_reset_short,
            'recent_trades': len(self._trade_history)
        }


# Singleton
_overtrade_filter: Optional[OvertradeFilter] = None


def get_overtrade_filter(enabled: bool = True) -> OvertradeFilter:
    global _overtrade_filter
    if _overtrade_filter is None:
        _overtrade_filter = OvertradeFilter(enabled=enabled)
    return _overtrade_filter


def reset_overtrade_filter():
    global _overtrade_filter
    _overtrade_filter = None
