"""
BB Width Filter - Detects narrow ranges (compression) via Bollinger Band width

Narrow BB width indicates low volatility compression where trend-following
signals produce whipsaws. Works alongside ADX for better regime detection.

PHASE A FIX (Jan 18, 2026): In RANGE regime, BB Width is INFORMATIVE ONLY.
Does not block, only applies soft penalty. This prevents double-veto with 
other volatility filters.

Created: January 15, 2026
Updated: January 18, 2026 - Phase A sobreprotección fix
"""

import numpy as np
import pandas as pd
from typing import Optional
from datetime import datetime
import logging

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


class BBWidthFilter(BaseFilter):
    """
    Bollinger Band Width filter for detecting range/compression regimes.
    
    BB Width = (Upper - Lower) / Middle
    
    Thresholds (calibrated for BTC 5m):
    - < 0.4% = EXTREME COMPRESSION → BLOCK (unless range_informative_mode)
    - < 0.8% = RANGE → Penalize (-2) + require higher score
    - < 1.2% = NARROW → Warning penalty (-1)
    - >= 1.2% = NORMAL → No penalty
    - > 2.5% = HIGH VOLATILITY → Different handling
    
    PHASE A (Jan 18, 2026): range_informative_mode=True makes compression
    return allow=True with penalty instead of blocking.
    
    🔥 FIX #174 (Feb 11, 2026): Relajar BB_EXTREME_COMPRESSION de 0.5% a 0.4%
    ANTES: 0.5% bloqueaba trades válidos en compresión moderada (ej: 0.50%)
    AHORA: Solo bloquea en compresión extrema < 0.4%
    Log Feb 11: ~30 bloqueos entre 03:46-03:49 por 0.50% justo en el umbral
    """
    
    # Thresholds (as decimal, not percentage)
    # 🔥 FIX #174: 0.005 → 0.004 (0.5% → 0.4%)
    BB_EXTREME_COMPRESSION = 0.004   # 0.4% (was 0.5%)
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
    
    # PHASE A: Informative mode penalty (softer than block)
    INFORMATIVE_MODE_PENALTY = -0.5  # Soft penalty when informative only
    
    # Log throttle interval (seconds) — avoid 854-line spam per run
    LOG_THROTTLE_INTERVAL = 60.0
    
    def __init__(self, 
                 enabled: bool = True,
                 extreme_threshold: float | None = None,
                 range_threshold: float | None = None,
                 range_informative_mode: bool = True):
        """
        Args:
            enabled: Whether filter is active
            extreme_threshold: Override extreme compression threshold
            range_threshold: Override range threshold
            range_informative_mode: If True, compression returns allow=True 
                                    with penalty instead of blocking (PHASE A)
        """
        super().__init__(enabled)
        self.range_informative_mode = range_informative_mode
        self._last_bb_block_log_time: float = 0.0
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
        
        return float(bb_width), float(upper), float(lower), float(middle)
    
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
                 df: pd.DataFrame | None = None,
                 closes: np.ndarray | None = None,
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
                closes = df['close'].to_numpy()
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
        tags: dict[str, float | str | bool | None] = {
            'bb_width': round(bb_width * 100, 3),  # As percentage
            'bb_width_percentile': round(bb_width_pct, 1),
            'bb_upper': round(upper, 2),
            'bb_lower': round(lower, 2),
            'bb_middle': round(middle, 2),
        }
        
        # =====================================================================
        # RANGE CLASSIFICATION: PASSIVE vs ACTIVE (Jan 20, 2026 FIX)
        # 
        # PASSIVE RANGE: BB width low AND momentum dead → mean reversion only
        # ACTIVE RANGE (accumulation): BB width low BUT expansion starting
        #   - BB width rising (squeeze breakout imminent)
        #   - Price volatility increasing
        #   
        # In ACTIVE RANGE: Allow breakout trades, not mean reversion
        # =====================================================================
        
        # Calculate BB width expansion (compare to recent history)
        is_expanding = False
        expansion_rate = 0.0
        if len(closes) >= 30:
            # Get BB width 5 candles ago
            old_closes = closes[:-5]
            bb_width_prev, _, _, _ = self.calculate_bb_width(old_closes)
            if bb_width_prev > 0:
                expansion_rate = (bb_width - bb_width_prev) / bb_width_prev
                is_expanding = expansion_rate > 0.05  # 5% expansion = squeeze starting
        
        tags['bb_expanding'] = is_expanding
        tags['bb_expansion_rate'] = round(expansion_rate * 100, 2)  # As percentage
        
        # Evaluate conditions
        if bb_width < self.BB_EXTREME_COMPRESSION:
            # Check if this is ACTIVE compression (expansion starting)
            if is_expanding and expansion_rate > 0.10:  # Strong expansion
                tags['range_type'] = 'ACTIVE_COMPRESSION'
                tags['bb_regime'] = 'BREAKOUT_WATCH'
                logger.info(f"📊 BB WIDTH: Compression {bb_width*100:.2f}% BUT expanding {expansion_rate*100:.1f}% → BREAKOUT MODE")
                return FilterDecision(
                    allow=True,
                    score_adjust=-0.5,  # Small penalty, not blocking
                    regime="BREAKOUT_WATCH",
                    reasons=[f"Active compression (expanding): {bb_width*100:.2f}%"],
                    tags=tags
                )
            
            # PASSIVE extreme compression - No trade zone
            tags['range_type'] = 'PASSIVE'
            tags['range_by_bbwidth'] = True
            tags['bb_regime'] = 'EXTREME_COMPRESSION'
            
            # 🔥 FIX #153 (Feb 10, 2026): EXTREME compression ALWAYS blocks
            # BEFORE: range_informative_mode bypassed the block
            # NOW: < 0.5% BB width = HARD BLOCK (no exceptions)
            # This trade lost -0.44R because it entered in 0.45% compression
            if self.EXTREME_COMPRESSION_BLOCK:
                import time as _time_mod
                _now = _time_mod.time()
                if _now - self._last_bb_block_log_time >= self.LOG_THROTTLE_INTERVAL:
                    self._last_bb_block_log_time = _now
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
            # RANGE - Check if PASSIVE or ACTIVE
            tags['range_by_bbwidth'] = True
            
            if is_expanding:
                # ACTIVE RANGE - expansion starting, allow breakouts
                tags['range_type'] = 'ACTIVE'
                tags['bb_regime'] = 'RANGE_EXPANDING'
                penalty = -0.5  # Minimal penalty for active range
                logger.debug(f"BB WIDTH ACTIVE RANGE: {bb_width*100:.2f}%, expanding {expansion_rate*100:.1f}%")
                return FilterDecision(
                    allow=True,
                    score_adjust=penalty,
                    strength=1 - (bb_width / self.BB_RANGE_THRESHOLD),
                    regime="RANGE_EXPANDING",
                    reasons=[f"Active range (expanding): {bb_width*100:.2f}%"],
                    tags=tags
                )
            else:
                # PASSIVE RANGE - mean reversion territory
                tags['range_type'] = 'PASSIVE'
                tags['bb_regime'] = 'RANGE'
                
                # PHASE A: Use softer penalty in informative mode
                penalty = self.INFORMATIVE_MODE_PENALTY if self.range_informative_mode else self.RANGE_PENALTY
                
                logger.debug(f"BB WIDTH PASSIVE RANGE: {bb_width*100:.2f}% (< {self.BB_RANGE_THRESHOLD*100}%) - penalty {penalty}")
                return FilterDecision(
                    allow=True,
                    score_adjust=penalty,
                    strength=1 - (bb_width / self.BB_RANGE_THRESHOLD),
                    regime="RANGE",
                    reasons=[f"Passive range: {bb_width*100:.2f}%"],
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
