"""
Volatility Shock Filter - Detects abnormal candles and enforces cooling-off periods

Detects when TrueRange > k*ATR (individual candle spike) and:
1. Blocks trading during the spike
2. Enforces a cooldown period after the spike

This catches single-candle anomalies that "range_expansion" might miss.

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
import logging

from engine.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


@dataclass
class ShockState:
    """State tracking for volatility shocks"""
    shock_detected: bool = False
    shock_ratio: float = 0.0
    shock_level: str = "NONE"  # NONE, MODERATE, SEVERE, EXTREME
    shock_until: Optional[datetime] = None
    cooldown_remaining_seconds: int = 0
    tr: float = 0.0
    atr: float = 0.0


class VolatilityShockFilter(BaseFilter):
    """
    Volatility Shock Filter with temporal cooldown.
    
    Detects:
    - TrueRange > 2.5 * ATR → MODERATE shock (10-15 min cooldown)
    - TrueRange > 3.5 * ATR → SEVERE shock (20-30 min cooldown)
    - TrueRange > 5.0 * ATR → EXTREME shock (45-60 min cooldown, CHAOS regime)
    
    The filter maintains internal state to track cooldown periods.
    """
    
    # Shock thresholds (TR/ATR ratio)
    # NOTE: 2.5-3.0 penalizes but allows (post-expansion can be good)
    #       3.0-3.5 penalizes heavily with longer cooldown
    #       3.5+ is HARD BLOCK (real chaos)
    MODERATE_SHOCK_RATIO = 2.5     # Penalize with short cooldown
    ELEVATED_SHOCK_RATIO = 3.0     # Penalize heavily
    SEVERE_SHOCK_RATIO = 3.5       # HARD BLOCK
    EXTREME_SHOCK_RATIO = 5.0      # CHAOS mode
    
    # Cooldown durations (minutes) - applies even when allowed
    MODERATE_COOLDOWN_MINUTES = 8   # Reduced from 12 (can be good entries)
    ELEVATED_COOLDOWN_MINUTES = 15
    SEVERE_COOLDOWN_MINUTES = 25
    EXTREME_COOLDOWN_MINUTES = 45
    
    # ATR period
    ATR_PERIOD = 14
    
    # Score adjustments (when allowed but penalized)
    MODERATE_PENALTY = -1.0        # Reduced from -1.5
    ELEVATED_PENALTY = -2.0
    SEVERE_PENALTY = -2.5          # If allowing SEVERE (rare)
    
    def __init__(self, 
                 enabled: bool = True,
                 moderate_ratio: float = None,
                 severe_ratio: float = None):
        super().__init__(enabled)
        if moderate_ratio:
            self.MODERATE_SHOCK_RATIO = moderate_ratio
        if severe_ratio:
            self.SEVERE_SHOCK_RATIO = severe_ratio
        
        # State
        self._shock_until: Optional[datetime] = None
        self._shock_level: str = "NONE"
        self._last_state = ShockState()
    
    def calculate_true_range(self, 
                             high: float, 
                             low: float, 
                             prev_close: float) -> float:
        """
        Calculate True Range for a single candle.
        
        TR = max(high-low, |high-prev_close|, |low-prev_close|)
        """
        return max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
    
    def calculate_atr(self, 
                      highs: np.ndarray, 
                      lows: np.ndarray, 
                      closes: np.ndarray,
                      period: int = None) -> float:
        """
        Calculate ATR using RMA (Wilder's smoothing).
        """
        period = period or self.ATR_PERIOD
        if len(highs) < period + 1:
            return 0.0
        
        # Calculate TR series
        tr_values = []
        for i in range(1, len(highs)):
            tr = self.calculate_true_range(highs[i], lows[i], closes[i-1])
            tr_values.append(tr)
        
        if len(tr_values) < period:
            return np.mean(tr_values) if tr_values else 0.0
        
        # RMA (Wilder's Moving Average)
        atr = np.mean(tr_values[:period])
        alpha = 1 / period
        for tr in tr_values[period:]:
            atr = alpha * tr + (1 - alpha) * atr
        
        return atr
    
    def detect_shock(self, 
                     current_high: float,
                     current_low: float,
                     prev_close: float,
                     atr: float) -> Tuple[bool, float, str]:
        """
        Detect if current candle is a volatility shock.
        
        Returns:
            (is_shock, shock_ratio, shock_level)
            
        Levels:
            MODERATE (2.5-3.0): Penalize, short cooldown, ALLOW
            ELEVATED (3.0-3.5): Penalize heavily, ALLOW
            SEVERE (3.5-5.0): HARD BLOCK
            EXTREME (5.0+): CHAOS BLOCK
        """
        if atr <= 0:
            return False, 0.0, "NONE"
        
        tr = self.calculate_true_range(current_high, current_low, prev_close)
        ratio = tr / atr
        
        if ratio >= self.EXTREME_SHOCK_RATIO:
            return True, ratio, "EXTREME"
        elif ratio >= self.SEVERE_SHOCK_RATIO:
            return True, ratio, "SEVERE"
        elif ratio >= self.ELEVATED_SHOCK_RATIO:
            return True, ratio, "ELEVATED"
        elif ratio >= self.MODERATE_SHOCK_RATIO:
            return True, ratio, "MODERATE"
        else:
            return False, ratio, "NONE"
    
    def get_cooldown_duration(self, shock_level: str) -> int:
        """Get cooldown duration in minutes for shock level"""
        if shock_level == "EXTREME":
            return self.EXTREME_COOLDOWN_MINUTES
        elif shock_level == "SEVERE":
            return self.SEVERE_COOLDOWN_MINUTES
        elif shock_level == "ELEVATED":
            return self.ELEVATED_COOLDOWN_MINUTES
        elif shock_level == "MODERATE":
            return self.MODERATE_COOLDOWN_MINUTES
        return 0
    
    def evaluate(self,
                 df: pd.DataFrame = None,
                 current_high: float = None,
                 current_low: float = None,
                 prev_close: float = None,
                 market_time: datetime = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate volatility shock conditions.
        
        Args:
            df: DataFrame with OHLC data (alternative to individual values)
            current_high: Current candle high
            current_low: Current candle low
            prev_close: Previous candle close
            market_time: Current time (for cooldown tracking)
        """
        now = market_time or datetime.utcnow()
        
        # Check if still in cooldown from previous shock
        if self._shock_until and now < self._shock_until:
            remaining = (self._shock_until - now).total_seconds()
            self._last_state = ShockState(
                shock_detected=False,
                shock_level=self._shock_level,
                shock_until=self._shock_until,
                cooldown_remaining_seconds=int(remaining)
            )
            logger.info(f"⏳ SHOCK COOLDOWN: {remaining/60:.1f} min remaining (level={self._shock_level})")
            return FilterDecision(
                allow=False,
                regime="SHOCK_COOLDOWN",
                reasons=[f"Shock cooldown: {remaining/60:.1f} min remaining"],
                tags={
                    'shock_cooldown': True,
                    'shock_level': self._shock_level,
                    'cooldown_remaining_min': round(remaining/60, 1)
                }
            )
        else:
            # Cooldown expired
            self._shock_until = None
            self._shock_level = "NONE"
        
        # Extract OHLC from DataFrame if provided
        if df is not None and len(df) >= self.ATR_PERIOD + 1:
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            
            current_high = highs[-1]
            current_low = lows[-1]
            prev_close = closes[-2]
            
            atr = self.calculate_atr(highs, lows, closes)
        elif current_high is not None and current_low is not None and prev_close is not None:
            # Need ATR from somewhere - estimate from recent data
            atr = kwargs.get('atr', (current_high - current_low) * 1.5)
        else:
            return FilterDecision.allow_default()
        
        # Detect shock
        is_shock, ratio, level = self.detect_shock(
            current_high, current_low, prev_close, atr
        )
        
        tr = self.calculate_true_range(current_high, current_low, prev_close)
        
        # Build state
        self._last_state = ShockState(
            shock_detected=is_shock,
            shock_ratio=ratio,
            shock_level=level,
            tr=tr,
            atr=atr
        )
        
        tags = {
            'shock_detected': is_shock,
            'shock_ratio': round(ratio, 2),
            'shock_level': level,
            'tr': round(tr, 2),
            'atr': round(atr, 2)
        }
        
        if is_shock:
            # Set cooldown
            cooldown_mins = self.get_cooldown_duration(level)
            self._shock_until = now + timedelta(minutes=cooldown_mins)
            self._shock_level = level
            
            tags['shock_until'] = self._shock_until.isoformat()
            tags['shock_cooldown_minutes'] = cooldown_mins
            
            logger.warning(f"⚡ VOLATILITY SHOCK: TR/ATR={ratio:.1f}x (level={level}) → {cooldown_mins}min cooldown")
            
            if level == "EXTREME":
                # HARD BLOCK - True chaos
                return FilterDecision(
                    allow=False,
                    regime="CHAOS",
                    reasons=[f"Extreme volatility shock: TR={ratio:.1f}x ATR"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
            elif level == "SEVERE":
                # HARD BLOCK at 3.5+ (real danger zone)
                return FilterDecision(
                    allow=False,
                    regime="SHOCK",
                    reasons=[f"Severe volatility shock: TR={ratio:.1f}x ATR (≥3.5x)"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
            elif level == "ELEVATED":
                # ALLOW but penalize heavily (3.0-3.5x)
                # Post-expansion can still have good trades
                return FilterDecision(
                    allow=True,
                    score_adjust=self.ELEVATED_PENALTY,
                    regime="SHOCK",
                    reasons=[f"Elevated volatility: TR={ratio:.1f}x ATR (3.0-3.5x)"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
            else:  # MODERATE (2.5-3.0x)
                # ALLOW with light penalty - often good entries post-expansion
                return FilterDecision(
                    allow=True,
                    score_adjust=self.MODERATE_PENALTY,
                    regime="SHOCK",
                    reasons=[f"Moderate volatility shock: TR={ratio:.1f}x ATR"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
        
        # No shock
        return FilterDecision(
            allow=True,
            reasons=["No volatility shock"],
            tags=tags
        )
    
    def get_state(self) -> ShockState:
        """Get current shock state"""
        return self._last_state
    
    def clear_cooldown(self):
        """Manually clear cooldown (for testing)"""
        self._shock_until = None
        self._shock_level = "NONE"


# Singleton
_volatility_shock_filter: Optional[VolatilityShockFilter] = None


def get_volatility_shock_filter(enabled: bool = True) -> VolatilityShockFilter:
    global _volatility_shock_filter
    if _volatility_shock_filter is None:
        _volatility_shock_filter = VolatilityShockFilter(enabled=enabled)
    return _volatility_shock_filter


def reset_volatility_shock_filter():
    global _volatility_shock_filter
    _volatility_shock_filter = None
