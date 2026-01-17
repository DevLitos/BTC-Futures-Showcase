"""
BB Width Filter - Detects narrow ranges (compression) via Bollinger Band width

Narrow BB width indicates low volatility compression where trend-following
signals produce whipsaws. Works alongside ADX for better regime detection.

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional
from datetime import datetime
import logging

from engine.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


class BBWidthFilter(BaseFilter):
    """
    Bollinger Band Width filter for detecting range/compression regimes.
    
    BB Width = (Upper - Lower) / Middle
    
    Thresholds (calibrated for BTC 5m):
    - < 0.5% = EXTREME COMPRESSION → NO TRADE
    - < 0.8% = RANGE → Penalize (-2) + require higher score
    - < 1.2% = NARROW → Warning penalty (-1)
    - >= 1.2% = NORMAL → No penalty
    - > 2.5% = HIGH VOLATILITY → Different handling
    """
    
    # Thresholds (as decimal, not percentage)
    BB_EXTREME_COMPRESSION = 0.005   # 0.5%
    BB_RANGE_THRESHOLD = 0.008       # 0.8%
    BB_NARROW_THRESHOLD = 0.012      # 1.2%
    BB_HIGH_VOL_THRESHOLD = 0.025    # 2.5%
    
    # Bollinger params
    BB_PERIOD = 20
    BB_STD_DEV = 2.0
    
    # Score adjustments
    EXTREME_COMPRESSION_BLOCK = True
    RANGE_PENALTY = -2.0
    NARROW_PENALTY = -1.0
    HIGH_VOL_PENALTY = -0.5  # Slight penalty for high vol (wider stops needed)
    
    def __init__(self, 
                 enabled: bool = True,
                 extreme_threshold: float = None,
                 range_threshold: float = None):
        super().__init__(enabled)
        if extreme_threshold:
            self.BB_EXTREME_COMPRESSION = extreme_threshold
        if range_threshold:
            self.BB_RANGE_THRESHOLD = range_threshold
    
    def calculate_bb_width(self, closes: np.ndarray) -> tuple[float, float, float, float]:
        """
        Calculate Bollinger Band width.
        
        Returns:
            (bb_width, upper, lower, middle)
        """
        if len(closes) < self.BB_PERIOD:
            return 0.0, 0.0, 0.0, 0.0
        
        # SMA (middle band)
        middle = np.mean(closes[-self.BB_PERIOD:])
        
        # Standard deviation
        std = np.std(closes[-self.BB_PERIOD:])
        
        # Upper and lower bands
        upper = middle + self.BB_STD_DEV * std
        lower = middle - self.BB_STD_DEV * std
        
        # Width normalized by middle
        bb_width = (upper - lower) / middle if middle > 0 else 0.0
        
        return bb_width, upper, lower, middle
    
    def get_bb_width_percentile(self, closes: np.ndarray, lookback: int = 100) -> float:
        """
        Calculate where current BB width sits in recent history.
        
        Returns:
            Percentile 0-100 (0 = narrowest, 100 = widest)
        """
        if len(closes) < lookback:
            return 50.0  # Default to middle
        
        # Calculate rolling BB widths
        widths = []
        for i in range(lookback):
            idx = len(closes) - lookback + i
            if idx >= self.BB_PERIOD:
                segment = closes[idx - self.BB_PERIOD:idx]
                middle = np.mean(segment)
                std = np.std(segment)
                width = (2 * self.BB_STD_DEV * std) / middle if middle > 0 else 0
                widths.append(width)
        
        if not widths:
            return 50.0
        
        current_width = widths[-1]
        percentile = sum(1 for w in widths if w <= current_width) / len(widths) * 100
        return percentile
    
    def evaluate(self, 
                 df: pd.DataFrame = None,
                 closes: np.ndarray = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate BB width and return decision.
        
        Args:
            df: DataFrame with 'close' column (5m recommended)
            closes: Alternative - numpy array of closes
        """
        # Extract closes
        if closes is None:
            if df is not None and 'close' in df.columns:
                closes = df['close'].values
            else:
                return FilterDecision.allow_default()
        
        if len(closes) < self.BB_PERIOD:
            return FilterDecision(
                allow=True, 
                reasons=["Insufficient data for BB width"],
                tags={'bb_width': None}
            )
        
        # Calculate BB width
        bb_width, upper, lower, middle = self.calculate_bb_width(closes)
        bb_width_pct = self.get_bb_width_percentile(closes)
        
        # Build tags
        tags = {
            'bb_width': round(bb_width * 100, 3),  # As percentage
            'bb_width_percentile': round(bb_width_pct, 1),
            'bb_upper': round(upper, 2),
            'bb_lower': round(lower, 2),
            'bb_middle': round(middle, 2),
        }
        
        # Evaluate conditions
        if bb_width < self.BB_EXTREME_COMPRESSION:
            # EXTREME COMPRESSION - No trade zone
            tags['range_by_bbwidth'] = True
            tags['bb_regime'] = 'EXTREME_COMPRESSION'
            
            if self.EXTREME_COMPRESSION_BLOCK:
                logger.warning(f"⛔ BB WIDTH BLOCK: {bb_width*100:.2f}% (extreme compression < {self.BB_EXTREME_COMPRESSION*100}%)")
                return FilterDecision(
                    allow=False,
                    regime="NO_TRADE",
                    reasons=[f"Extreme BB compression: {bb_width*100:.2f}%"],
                    tags=tags
                )
            else:
                return FilterDecision(
                    allow=True,
                    score_adjust=self.RANGE_PENALTY * 1.5,
                    regime="COMPRESSION",
                    reasons=[f"Extreme BB compression: {bb_width*100:.2f}%"],
                    tags=tags
                )
        
        elif bb_width < self.BB_RANGE_THRESHOLD:
            # RANGE - Penalize
            tags['range_by_bbwidth'] = True
            tags['bb_regime'] = 'RANGE'
            logger.debug(f"BB WIDTH RANGE: {bb_width*100:.2f}% (< {self.BB_RANGE_THRESHOLD*100}%)")
            return FilterDecision(
                allow=True,
                score_adjust=self.RANGE_PENALTY,
                strength=1 - (bb_width / self.BB_RANGE_THRESHOLD),
                regime="RANGE",
                reasons=[f"BB range regime: {bb_width*100:.2f}%"],
                tags=tags
            )
        
        elif bb_width < self.BB_NARROW_THRESHOLD:
            # NARROW - Warning
            tags['range_by_bbwidth'] = False
            tags['bb_regime'] = 'NARROW'
            return FilterDecision(
                allow=True,
                score_adjust=self.NARROW_PENALTY,
                reasons=[f"BB narrow: {bb_width*100:.2f}%"],
                tags=tags
            )
        
        elif bb_width > self.BB_HIGH_VOL_THRESHOLD:
            # HIGH VOLATILITY - Slight penalty (need wider stops)
            tags['range_by_bbwidth'] = False
            tags['bb_regime'] = 'HIGH_VOL'
            return FilterDecision(
                allow=True,
                score_adjust=self.HIGH_VOL_PENALTY,
                reasons=[f"BB high volatility: {bb_width*100:.2f}%"],
                tags=tags
            )
        
        else:
            # NORMAL - No adjustment
            tags['range_by_bbwidth'] = False
            tags['bb_regime'] = 'NORMAL'
            return FilterDecision(
                allow=True,
                score_adjust=0,
                reasons=[f"BB normal: {bb_width*100:.2f}%"],
                tags=tags
            )


# Singleton
_bb_width_filter: Optional[BBWidthFilter] = None


def get_bb_width_filter(enabled: bool = True) -> BBWidthFilter:
    global _bb_width_filter
    if _bb_width_filter is None:
        _bb_width_filter = BBWidthFilter(enabled=enabled)
    return _bb_width_filter


def reset_bb_width_filter():
    global _bb_width_filter
    _bb_width_filter = None
