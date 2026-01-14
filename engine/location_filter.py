"""
Location Filter - Macro Context Veto System

"Location" is distinct from momentum/local structure.
EMA/ADX/BOS/VWAP measure dynamics, not absolute context:
WHERE you are within the HTF map (20-60 day range, proximity to highs, anchor levels).

This "macro veto" differentiates a bot that survives from one that
self-liquidates buying tops.

Implemented Rules (Hard Vetoes):
1. EXTREMES_VETO: In RANGE regime, prohibit longs in top 10% / shorts in bottom 10%
2. HEADROOM_VETO: If distance to structure high < k*ATR, no longs in RANGE
3. BREAKOUT_OVERRIDE: Allow longs at highs if ADX high + breakout confirmed + volume expansion

Author: DayTrader Team
Date: January 2026
"""

import logging
import numpy as np
from typing import Tuple, Dict, Optional, List
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class LocationConfig:
    """Configuration for location-based filters."""
    
    # Percentile thresholds for extremes veto
    EXTREME_HIGH_PERCENTILE: float = 90.0   # Top 10% of range
    EXTREME_LOW_PERCENTILE: float = 10.0    # Bottom 10% of range
    
    # Lookback periods for range calculation
    RANGE_LOOKBACK_DAYS: int = 60           # Primary lookback (60 days)
    RANGE_LOOKBACK_SHORT_DAYS: int = 20     # Short lookback for confirmation
    
    # Headroom thresholds (in ATR units)
    MIN_HEADROOM_ATR: float = 0.5           # Minimum space to target in ATR
    MIN_HEADROOM_ATR_STRICT: float = 1.0    # Stricter version for weaker setups
    
    # Breakout override thresholds
    BREAKOUT_ADX_MIN: float = 30.0          # Minimum ADX for breakout regime
    BREAKOUT_VOLUME_EXPANSION: float = 1.3  # Volume > 130% of avg
    BREAKOUT_CLOSE_ABOVE_HIGH: bool = True  # Must close above structure high
    
    # YTD high/low reference (for major levels)
    USE_YTD_REFERENCE: bool = True          # Include YTD high/low in headroom calc


# Global configuration instance
_config = LocationConfig()


def get_location_config() -> LocationConfig:
    """Get location filter configuration."""
    return _config


# =============================================================================
# LOCATION CONTEXT
# =============================================================================

@dataclass
class LocationContext:
    """Context about current price location within market structure."""
    
    # Current price position
    current_price: float = 0.0
    
    # 60-day range
    range_high_60d: float = 0.0
    range_low_60d: float = 0.0
    percentile_60d: float = 50.0            # Where price is in 60d range (0-100)
    
    # 20-day range (for short-term context)
    range_high_20d: float = 0.0
    range_low_20d: float = 0.0
    percentile_20d: float = 50.0
    
    # YTD range (if available)
    ytd_high: Optional[float] = None
    ytd_low: Optional[float] = None
    
    # Headroom calculations
    headroom_to_high_atr: float = float('inf')   # Distance to 60d high in ATR
    headroom_to_low_atr: float = float('inf')    # Distance to 60d low in ATR
    headroom_to_ytd_high_atr: float = float('inf')
    headroom_to_ytd_low_atr: float = float('inf')
    
    # Market regime (from external classifier)
    regime: str = "UNKNOWN"                 # RANGE, TREND_EXPANSION, etc.
    regime_confidence: float = 0.0
    
    # Breakout status
    is_breakout_up: bool = False            # Breaking above structure
    is_breakout_down: bool = False          # Breaking below structure
    breakout_up_score: int = 0              # Score for breakout up detection
    breakout_down_score: int = 0            # Score for breakout down detection
    volume_expansion: float = 1.0           # Current volume / avg volume
    adx_current: float = 0.0
    
    # Location flags
    in_extreme_high: bool = False           # In top X percentile
    in_extreme_low: bool = False            # In bottom X percentile
    has_headroom_long: bool = True          # Enough room for long target
    has_headroom_short: bool = True         # Enough room for short target
    
    # Derived vetoes
    veto_long: bool = False
    veto_short: bool = False
    veto_reasons: List[str] = None
    
    def __post_init__(self):
        if self.veto_reasons is None:
            self.veto_reasons = []
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for logging."""
        return {
            'price': self.current_price,
            'pct_60d': round(self.percentile_60d, 1),
            'pct_20d': round(self.percentile_20d, 1),
            'headroom_high_atr': round(self.headroom_to_high_atr, 2),
            'headroom_low_atr': round(self.headroom_to_low_atr, 2),
            'regime': self.regime,
            'in_extreme': self.in_extreme_high or self.in_extreme_low,
            'is_breakout': self.is_breakout_up or self.is_breakout_down,
            'veto_long': self.veto_long,
            'veto_short': self.veto_short,
            'veto_reasons': self.veto_reasons
        }


# =============================================================================
# LOCATION FILTER
# =============================================================================

class LocationFilter:
    """
    Location-based macro veto system.
    
    This filter operates at a HIGHER level than momentum/structure filters.
    It answers: "Given where we are in the broader market structure,
    should we even CONSIDER this direction?"
    
    Key Concept: In ranging markets, buying in the top decile has 
    asymmetric risk - upside capped, downside fast. This filter
    implements hard vetoes for these situations.
    
    Usage:
        filter = LocationFilter()
        context = filter.calculate_context(df_1h, current_price, atr, regime)
        can_long, can_short, reasons = filter.apply_vetoes(context, bias)
    """
    
    def __init__(self, config: LocationConfig = None):
        self.config = config or get_location_config()
        
        # Cache for efficiency (recalculate only on new 1h bar)
        self._cache_timestamp: int = 0
        self._cached_context: Optional[LocationContext] = None
    
    def calculate_context(
        self,
        closes_1h: np.ndarray,
        highs_1h: np.ndarray,
        lows_1h: np.ndarray,
        volumes_1h: np.ndarray,
        current_price: float,
        atr_1h: float,
        adx_1h: float,
        regime: str = "RANGE",
        regime_confidence: float = 0.5,
        ytd_high: float = None,
        ytd_low: float = None,
        cache_key: int = 0
    ) -> LocationContext:
        """
        Calculate location context from price data.
        
        Args:
            closes_1h: Array of 1h close prices (need 60+ days = 1440+ bars)
            highs_1h: Array of 1h high prices
            lows_1h: Array of 1h low prices
            volumes_1h: Array of 1h volumes
            current_price: Current market price
            atr_1h: Current 1h ATR
            adx_1h: Current 1h ADX
            regime: Market regime from classifier
            regime_confidence: Confidence in regime classification
            ytd_high: Year-to-date high (optional)
            ytd_low: Year-to-date low (optional)
            cache_key: Timestamp for cache invalidation
            
        Returns:
            LocationContext with all calculated metrics
        """
        # Check cache
        if cache_key > 0 and cache_key == self._cache_timestamp and self._cached_context:
            return self._cached_context
        
        context = LocationContext()
        context.current_price = current_price
        context.regime = regime
        context.regime_confidence = regime_confidence
        context.adx_current = adx_1h
        
        # Ensure we have enough data
        bars_60d = 24 * 60  # 60 days of 1h bars
        bars_20d = 24 * 20
        
        if len(closes_1h) < bars_20d:
            logger.warning(f"LocationFilter: Insufficient data ({len(closes_1h)} < {bars_20d} bars)")
            return context
        
        # ============= RANGE CALCULATIONS =============
        
        # 60-day range
        if len(highs_1h) >= bars_60d:
            context.range_high_60d = float(np.max(highs_1h[-bars_60d:]))
            context.range_low_60d = float(np.min(lows_1h[-bars_60d:]))
        else:
            # Use all available data
            context.range_high_60d = float(np.max(highs_1h))
            context.range_low_60d = float(np.min(lows_1h))
        
        # 20-day range
        context.range_high_20d = float(np.max(highs_1h[-bars_20d:]))
        context.range_low_20d = float(np.min(lows_1h[-bars_20d:]))
        
        # YTD range (if provided)
        if ytd_high is not None:
            context.ytd_high = ytd_high
        if ytd_low is not None:
            context.ytd_low = ytd_low
        
        # ============= PERCENTILE CALCULATIONS =============
        
        # Where is price within the 60-day range? (0 = at low, 100 = at high)
        range_60d = context.range_high_60d - context.range_low_60d
        if range_60d > 0:
            context.percentile_60d = ((current_price - context.range_low_60d) / range_60d) * 100
        else:
            context.percentile_60d = 50.0
        
        # Same for 20-day
        range_20d = context.range_high_20d - context.range_low_20d
        if range_20d > 0:
            context.percentile_20d = ((current_price - context.range_low_20d) / range_20d) * 100
        else:
            context.percentile_20d = 50.0
        
        # ============= HEADROOM CALCULATIONS =============
        
        if atr_1h > 0:
            # Distance to 60d highs/lows in ATR units
            context.headroom_to_high_atr = (context.range_high_60d - current_price) / atr_1h
            context.headroom_to_low_atr = (current_price - context.range_low_60d) / atr_1h
            
            # Distance to YTD highs/lows if available
            if context.ytd_high is not None:
                context.headroom_to_ytd_high_atr = (context.ytd_high - current_price) / atr_1h
            if context.ytd_low is not None:
                context.headroom_to_ytd_low_atr = (current_price - context.ytd_low) / atr_1h
        
        # ============= VOLUME EXPANSION =============
        
        if len(volumes_1h) >= 20 and volumes_1h[-1] > 0:
            avg_volume = np.mean(volumes_1h[-20:])
            if avg_volume > 0:
                context.volume_expansion = volumes_1h[-1] / avg_volume
        
        # ============= BREAKOUT DETECTION =============
        # Multiple criteria for breakout - any ONE strong signal OR combination of weaker signals
        # IMPORTANT: Breakout means price is BREAKING BEYOND the range, not just at range boundary
        
        recent_closes = closes_1h[-5:] if len(closes_1h) >= 5 else closes_1h
        recent_highs = highs_1h[-5:] if len(highs_1h) >= 5 else highs_1h
        recent_lows = lows_1h[-5:] if len(lows_1h) >= 5 else lows_1h
        
        # Score-based breakout detection (more flexible than AND logic)
        breakout_up_score = 0
        breakout_down_score = 0
        
        # Factor 1: Price clearly ABOVE/BELOW 60D range (2 points) - needs to exceed by > 0.5%
        if current_price > context.range_high_60d * 1.005:  # More than 0.5% above range high
            breakout_up_score += 2
        if current_price < context.range_low_60d * 0.995:  # More than 0.5% below range low
            breakout_down_score += 2
        
        # Factor 2: Multiple closes above/below the range (1 point)
        closes_above_high = np.sum(recent_closes > context.range_high_60d * 1.002)  # > 0.2% above
        closes_below_low = np.sum(recent_closes < context.range_low_60d * 0.998)   # > 0.2% below
        if closes_above_high >= 3:
            breakout_up_score += 1
        if closes_below_low >= 3:
            breakout_down_score += 1
        
        # Factor 3: Strong ADX (1 point if >= 25, 2 points if >= 30)
        if context.adx_current >= 30:
            breakout_up_score += 2
            breakout_down_score += 2
        elif context.adx_current >= 25:
            breakout_up_score += 1
            breakout_down_score += 1
        
        # Factor 4: Volume expansion (1 point)
        if context.volume_expansion >= self.config.BREAKOUT_VOLUME_EXPANSION:
            breakout_up_score += 1
            breakout_down_score += 1
        
        # Factor 5: New high/low ABOVE/BELOW previous range in recent bars (1 point)
        if np.max(recent_highs) > context.range_high_60d * 1.002:  # New high above range
            breakout_up_score += 1
        if np.min(recent_lows) < context.range_low_60d * 0.998:  # New low below range
            breakout_down_score += 1
        
        # Breakout confirmed if score >= 3 (combination of factors)
        context.is_breakout_up = breakout_up_score >= 3
        context.is_breakout_down = breakout_down_score >= 3
        
        # Store scores for debugging
        context.breakout_up_score = breakout_up_score
        context.breakout_down_score = breakout_down_score
        
        # ============= EXTREME FLAGS =============
        
        context.in_extreme_high = context.percentile_60d >= self.config.EXTREME_HIGH_PERCENTILE
        context.in_extreme_low = context.percentile_60d <= self.config.EXTREME_LOW_PERCENTILE
        
        # ============= HEADROOM FLAGS =============
        
        # Consider both 60d and YTD headroom
        min_headroom_high = min(
            context.headroom_to_high_atr,
            context.headroom_to_ytd_high_atr if context.ytd_high else float('inf')
        )
        min_headroom_low = min(
            context.headroom_to_low_atr,
            context.headroom_to_ytd_low_atr if context.ytd_low else float('inf')
        )
        
        context.has_headroom_long = min_headroom_high >= self.config.MIN_HEADROOM_ATR
        context.has_headroom_short = min_headroom_low >= self.config.MIN_HEADROOM_ATR
        
        # Cache the result
        self._cache_timestamp = cache_key
        self._cached_context = context
        
        return context
    
    def apply_vetoes(
        self,
        context: LocationContext,
        requested_bias: str = None
    ) -> Tuple[bool, bool, List[str]]:
        """
        Apply location-based veto rules.
        
        Args:
            context: LocationContext from calculate_context()
            requested_bias: 'LONG' or 'SHORT' if checking specific direction
            
        Returns:
            (can_long, can_short, reasons) - whether each direction is allowed
        """
        can_long = True
        can_short = True
        reasons = []
        
        # Determine if we're in a range regime (where vetoes apply strictly)
        is_range_regime = context.regime in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']
        is_trend_regime = context.regime in ['TREND_EXPANSION', 'TREND_UP', 'TREND_DOWN']
        
        # ============= RULE 1: EXTREMES VETO BY REGIME =============
        # Si regime == RANGE y percentile > 90% → NO LONG
        # Si regime == RANGE y percentile < 10% → NO SHORT
        
        if is_range_regime:
            if context.in_extreme_high:
                # Check for breakout override
                if not context.is_breakout_up:
                    can_long = False
                    reasons.append(
                        f"EXTREME_HIGH_VETO: In RANGE regime at {context.percentile_60d:.0f}th percentile "
                        f"(>{self.config.EXTREME_HIGH_PERCENTILE}%) - NO LONG"
                    )
                else:
                    reasons.append(
                        f"EXTREME_HIGH_OVERRIDE: At {context.percentile_60d:.0f}th pct but BREAKOUT detected "
                        f"(ADX={context.adx_current:.0f}, Vol={context.volume_expansion:.1f}x)"
                    )
            
            if context.in_extreme_low:
                # Check for breakdown override
                if not context.is_breakout_down:
                    can_short = False
                    reasons.append(
                        f"EXTREME_LOW_VETO: In RANGE regime at {context.percentile_60d:.0f}th percentile "
                        f"(<{self.config.EXTREME_LOW_PERCENTILE}%) - NO SHORT"
                    )
                else:
                    reasons.append(
                        f"EXTREME_LOW_OVERRIDE: At {context.percentile_60d:.0f}th pct but BREAKDOWN detected"
                    )
        
        # In TREND regime, vetoes are more lenient
        elif is_trend_regime:
            # ============= EXPLICIT TREND EXCEPTION =============
            # In strong trends, price living at extremes for weeks is NORMAL
            # We WANT to trade in direction of trend even at extremes
            # Only apply soft warnings, never hard vetoes
            
            if context.in_extreme_high:
                if context.regime in ['TREND_UP', 'TREND_EXPANSION']:
                    # Longing at highs in uptrend is the BEST trade
                    # Only warn if no breakout confirmation
                    if not context.is_breakout_up and context.adx_current < 25:
                        reasons.append(
                            f"TREND_CAUTION: At {context.percentile_60d:.0f}th pct in {context.regime} "
                            f"(ADX={context.adx_current:.0f}) - valid but monitor closely"
                        )
                    else:
                        reasons.append(
                            f"TREND_VALID: At {context.percentile_60d:.0f}th pct in {context.regime} "
                            f"(ADX={context.adx_current:.0f}, breakout_score={context.breakout_up_score}) - ALLOWED"
                        )
                elif context.regime == 'TREND_DOWN':
                    # Longing at highs in downtrend - more risky but still allow
                    # This is a potential reversal trade
                    reasons.append(
                        f"COUNTER_TREND: Long at {context.percentile_60d:.0f}th pct in TREND_DOWN - "
                        f"reversal attempt, use caution"
                    )
            
            if context.in_extreme_low:
                if context.regime in ['TREND_DOWN']:
                    # Shorting at lows in downtrend is the BEST trade
                    if not context.is_breakout_down and context.adx_current < 25:
                        reasons.append(
                            f"TREND_CAUTION: At {context.percentile_60d:.0f}th pct in {context.regime} "
                            f"(ADX={context.adx_current:.0f}) - valid but monitor closely"
                        )
                    else:
                        reasons.append(
                            f"TREND_VALID: At {context.percentile_60d:.0f}th pct in {context.regime} "
                            f"(ADX={context.adx_current:.0f}, breakdown_score={context.breakout_down_score}) - ALLOWED"
                        )
                elif context.regime in ['TREND_UP', 'TREND_EXPANSION']:
                    # Shorting at lows in uptrend - risky counter-trend
                    reasons.append(
                        f"COUNTER_TREND: Short at {context.percentile_60d:.0f}th pct in {context.regime} - "
                        f"reversal attempt, use caution"
                    )
        
        # ============= RULE 2: HEADROOM MINIMUM =============
        # Si distancia al 60D/YTD high < k * ATR_1h → NO LONG (en RANGE)
        
        if is_range_regime:
            if not context.has_headroom_long:
                can_long = False
                reasons.append(
                    f"HEADROOM_VETO_LONG: Only {context.headroom_to_high_atr:.1f} ATR to high "
                    f"(min={self.config.MIN_HEADROOM_ATR}) - insufficient upside"
                )
            
            if not context.has_headroom_short:
                can_short = False
                reasons.append(
                    f"HEADROOM_VETO_SHORT: Only {context.headroom_to_low_atr:.1f} ATR to low "
                    f"(min={self.config.MIN_HEADROOM_ATR}) - insufficient downside"
                )
        
        # ============= RULE 3: BREAKOUT OVERRIDE =============
        # Allow long at highs ONLY if:
        # - ADX HTF alto (>30)
        # - Close above structure high + retest
        # - Volume expansion
        
        # This is handled in Rule 1 via is_breakout_up/is_breakout_down checks
        
        # ============= STORE VETOES IN CONTEXT =============
        
        context.veto_long = not can_long
        context.veto_short = not can_short
        context.veto_reasons = reasons
        
        # Log if vetoes applied
        if not can_long or not can_short:
            logger.info(f"📍 LOCATION VETO: can_long={can_long}, can_short={can_short} | {reasons}")
        
        return can_long, can_short, reasons
    
    def check(
        self,
        closes_1h: np.ndarray,
        highs_1h: np.ndarray,
        lows_1h: np.ndarray,
        volumes_1h: np.ndarray,
        current_price: float,
        atr_1h: float,
        adx_1h: float,
        bias: str,
        regime: str = "RANGE",
        cache_key: int = 0,
        ytd_high: float = None,
        ytd_low: float = None
    ) -> Tuple[bool, str, LocationContext]:
        """
        Convenience method: check if a specific trade direction is allowed.
        
        Args:
            bias: 'LONG' or 'SHORT'
            ... other args same as calculate_context
            
        Returns:
            (allowed: bool, reason: str, context: LocationContext)
        """
        context = self.calculate_context(
            closes_1h=closes_1h,
            highs_1h=highs_1h,
            lows_1h=lows_1h,
            volumes_1h=volumes_1h,
            current_price=current_price,
            atr_1h=atr_1h,
            adx_1h=adx_1h,
            regime=regime,
            cache_key=cache_key,
            ytd_high=ytd_high,
            ytd_low=ytd_low
        )
        
        can_long, can_short, reasons = self.apply_vetoes(context, bias)
        
        if bias == 'LONG':
            allowed = can_long
            reason = "; ".join([r for r in reasons if 'LONG' in r]) if not allowed else ""
        elif bias == 'SHORT':
            allowed = can_short
            reason = "; ".join([r for r in reasons if 'SHORT' in r]) if not allowed else ""
        else:
            allowed = True
            reason = ""
        
        return allowed, reason, context
    
    def get_location_summary(self, context: LocationContext) -> str:
        """Get a human-readable summary of location context."""
        return (
            f"📍 Location: Price={context.current_price:.0f} | "
            f"60D pct={context.percentile_60d:.0f}% "
            f"(range {context.range_low_60d:.0f}-{context.range_high_60d:.0f}) | "
            f"Headroom: ↑{context.headroom_to_high_atr:.1f}ATR ↓{context.headroom_to_low_atr:.1f}ATR | "
            f"Regime={context.regime}"
        )


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_location_filter: Optional[LocationFilter] = None


def get_location_filter() -> LocationFilter:
    """Get global LocationFilter instance."""
    global _location_filter
    if _location_filter is None:
        _location_filter = LocationFilter()
    return _location_filter


# =============================================================================
# CONVENIENCE FUNCTION FOR INTEGRATION
# =============================================================================

def check_location_veto(
    closes_1h: np.ndarray,
    highs_1h: np.ndarray,
    lows_1h: np.ndarray,
    volumes_1h: np.ndarray,
    current_price: float,
    atr_1h: float,
    adx_1h: float,
    bias: str,
    regime: str = "RANGE"
) -> Tuple[bool, str]:
    """
    Quick check if location vetoes a trade direction.
    
    Args:
        bias: 'LONG' or 'SHORT'
        
    Returns:
        (allowed: bool, reason: str)
    """
    location_filter = get_location_filter()
    allowed, reason, _ = location_filter.check(
        closes_1h=closes_1h,
        highs_1h=highs_1h,
        lows_1h=lows_1h,
        volumes_1h=volumes_1h,
        current_price=current_price,
        atr_1h=atr_1h,
        adx_1h=adx_1h,
        bias=bias,
        regime=regime
    )
    return allowed, reason
