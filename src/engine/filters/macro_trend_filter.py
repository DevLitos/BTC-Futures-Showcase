"""
Macro Trend Filter - EMA200 on 15m + 1h with CONTEXTUAL slope handling

🔥 FIX (Jan 22, 2026):
- Slope veto is now CONTEXTUAL, not absolute
- Hard veto only for EXTREME slopes (>0.25%)
- Small slopes (-0.05% to -0.1%) just penalize score
- In RANGE mode or BREAKOUT mode: no hard veto at all, only penalties

The EMA200 always lags at the start of expansions.
Using a slow signal as absolute veto kills the first leg of breakouts.

Created: January 15, 2026
Updated: January 22, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime
import logging

from src.engine.filters.filter_interface import BaseFilter, FilterDecision
from src.engine.indicators import calculate_ema

logger = logging.getLogger(__name__)


class MacroTrendFilter(BaseFilter):
    """
    Macro Trend Filter with 15m and 1h EMA200 + contextual slope handling.
    
    🔥 FIX (Jan 22, 2026): Slope no longer does absolute veto.
    
    SLOPE LEVELS:
    - EXTREME (>0.25%): Hard block (true reversal signal)
    - MODERATE (0.10-0.25%): Heavy penalty (-2.5 score)
    - MILD (0.05-0.10%): Light penalty (-1.5 score)
    - MINIMAL (<0.05%): Allowed (slope too small to matter)
    
    In RANGE/BREAKOUT mode: No hard veto, only penalties
    """
    
    # EMA periods
    EMA_FAST = 50
    EMA_SLOW = 200
    
    # Slope calculation
    SLOPE_LOOKBACK_15M = 5   # 5 candles = 1h15m
    SLOPE_LOOKBACK_1H = 4    # 4 candles = 4h
    
    # 🔥 NEW: Slope thresholds (as percentage change)
    # Only EXTREME slope should hard block
    SLOPE_EXTREME_THRESHOLD = 0.0025   # 0.25% - TRUE reversal
    SLOPE_MODERATE_THRESHOLD = 0.0010  # 0.10% - heavy penalty
    SLOPE_MILD_THRESHOLD = 0.0005      # 0.05% - light penalty
    SLOPE_STRONG_THRESHOLD = 0.001     # +0.1% = strong trend bonus
    
    # Score adjustments
    ALIGNED_BONUS = 1.0            # Both TFs aligned with bias
    PARTIAL_PENALTY = -1.5         # Only one TF aligned
    SLOPE_MILD_PENALTY = -1.5      # Small opposing slope
    SLOPE_MODERATE_PENALTY = -2.5  # Moderate opposing slope
    
    # 🔥 NEW (Jan 22, 2026): Size caps when trading against macro slope
    # Different caps per regime_mode
    SIZE_CAP_AGAINST_MACRO = {
        'TREND': 0.5,      # Max 50% size when macro against in trend mode
        'RANGE': 0.25,     # Only scalps against macro in range
        'BREAKOUT': 0.4,   # Probe size for breakout attempts
    }
    
    def __init__(self, 
                 enabled: bool = True,
                 hard_veto_on_slope: bool = True):
        super().__init__(enabled)
        self.hard_veto_on_slope = hard_veto_on_slope  # Only applies to EXTREME slopes now
    
    def calculate_slope(self, 
                        values: np.ndarray, 
                        lookback: int) -> float:
        """
        Calculate percentage slope over lookback period.
        
        Returns:
            Slope as decimal (e.g., 0.001 = 0.1% increase)
        """
        if len(values) < lookback:
            return 0.0
        
        old_val = values[-lookback]
        new_val = values[-1]
        
        if old_val <= 0:
            return 0.0
        
        return (new_val - old_val) / old_val
    
    def check_macro_conditions(self,
                               df: pd.DataFrame,
                               timeframe: str,
                               current_price: float,
                               bias: str) -> dict:
        """
        Check macro conditions for a single timeframe.
        
        Returns:
            dict with conditions and values
        """
        result = {
            'timeframe': timeframe,
            'ema50': None,
            'ema200': None,
            'ema50_slope': None,
            'ema200_slope': None,
            'price_above_ema200': None,
            'ema50_above_ema200': None,
            'slope_aligned': None,
            'fully_aligned': None
        }
        
        if df is None or len(df) < self.EMA_SLOW:
            return result
        
        closes = df['close'].values
        
        # Calculate EMAs
        ema50 = calculate_ema(closes, self.EMA_FAST)
        ema200 = calculate_ema(closes, self.EMA_SLOW)
        
        if np.isnan(ema50[-1]) or np.isnan(ema200[-1]):
            return result
        
        result['ema50'] = float(ema50[-1])
        result['ema200'] = float(ema200[-1])
        
        # Calculate slopes
        lookback = self.SLOPE_LOOKBACK_15M if timeframe == '15m' else self.SLOPE_LOOKBACK_1H
        result['ema50_slope'] = self.calculate_slope(ema50, lookback)
        result['ema200_slope'] = self.calculate_slope(ema200, lookback)
        
        # Conditions for LONG
        if bias == 'LONG':
            result['price_above_ema200'] = current_price > result['ema200']
            result['ema50_above_ema200'] = result['ema50'] > result['ema200']
            # 🔥 FIX: Only mark as NOT aligned if slope is EXTREME
            result['slope_aligned'] = result['ema200_slope'] > -self.SLOPE_EXTREME_THRESHOLD
            result['fully_aligned'] = (
                result['price_above_ema200'] and 
                result['ema50_above_ema200'] and 
                result['slope_aligned']
            )
        else:  # SHORT
            result['price_above_ema200'] = current_price < result['ema200']  # Actually "below"
            result['ema50_above_ema200'] = result['ema50'] < result['ema200']  # Actually "below"
            # 🔥 FIX: Only mark as NOT aligned if slope is EXTREME
            result['slope_aligned'] = result['ema200_slope'] < self.SLOPE_EXTREME_THRESHOLD
            result['fully_aligned'] = (
                result['price_above_ema200'] and 
                result['ema50_above_ema200'] and 
                result['slope_aligned']
            )
        
        return result
    
    def evaluate(self,
                 df_15m: pd.DataFrame = None,
                 df_1h: pd.DataFrame = None,
                 current_price: float = None,
                 bias: str = None,
                 regime_mode: str = None,  # 🔥 NEW: 'TREND', 'RANGE', or 'BREAKOUT'
                 **kwargs) -> FilterDecision:
        """
        Evaluate macro trend conditions.
        
        Args:
            df_15m: 15-minute OHLC data
            df_1h: 1-hour OHLC data
            current_price: Current price
            bias: 'LONG' or 'SHORT'
            regime_mode: Current regime from RegimeClassifier ('TREND', 'RANGE', 'BREAKOUT')
                         In RANGE/BREAKOUT mode, slope never hard blocks
        """
        if current_price is None or bias is None:
            return FilterDecision.allow_default()
        
        # Check conditions on each timeframe
        cond_15m = self.check_macro_conditions(df_15m, '15m', current_price, bias)
        cond_1h = self.check_macro_conditions(df_1h, '1h', current_price, bias)
        
        # Build tags
        tags = {
            'macro_ema200_15m': cond_15m.get('ema200'),
            'macro_ema200_1h': cond_1h.get('ema200'),
            'macro_ema50_15m': cond_15m.get('ema50'),
            'macro_ema50_1h': cond_1h.get('ema50'),
            'macro_slope_15m': round(cond_15m.get('ema200_slope', 0) * 100, 3) if cond_15m.get('ema200_slope') else None,
            'macro_slope_1h': round(cond_1h.get('ema200_slope', 0) * 100, 3) if cond_1h.get('ema200_slope') else None,
            'macro_15m_aligned': cond_15m.get('fully_aligned'),
            'macro_1h_aligned': cond_1h.get('fully_aligned'),
            'regime_mode': regime_mode
        }
        
        reasons = []
        score_adjust = 0.0
        size_factor = 1.0  # 🔥 NEW: Size cap tracking
        
        # ===== SLOPE ANALYSIS (CONTEXTUAL, NOT ABSOLUTE) =====
        slope_15m = cond_15m.get('ema200_slope', 0) or 0
        slope_1h = cond_1h.get('ema200_slope', 0) or 0
        
        # Determine max opposing slope
        if bias == 'LONG':
            worst_slope = min(slope_15m, slope_1h)
            slope_is_opposing = worst_slope < 0
            slope_magnitude = abs(worst_slope) if slope_is_opposing else 0
        else:  # SHORT
            worst_slope = max(slope_15m, slope_1h)
            slope_is_opposing = worst_slope > 0
            slope_magnitude = abs(worst_slope) if slope_is_opposing else 0
        
        # 🔥 NEW: Contextual slope handling with size caps
        if slope_is_opposing:
            veto_tf = '15m' if (bias == 'LONG' and slope_15m < slope_1h) or (bias == 'SHORT' and slope_15m > slope_1h) else '1h'
            
            # 🔥 Apply size cap when trading against macro
            size_factor = self.SIZE_CAP_AGAINST_MACRO.get(regime_mode, 0.5)
            tags['macro_size_cap'] = size_factor
            reasons.append(f"Size capped to {size_factor*100:.0f}% (against macro)")
            
            if slope_magnitude >= self.SLOPE_EXTREME_THRESHOLD:
                # EXTREME slope - only hard block in TREND mode
                if regime_mode in ('RANGE', 'BREAKOUT') or not self.hard_veto_on_slope:
                    # In RANGE/BREAKOUT: heavy penalty but allow
                    score_adjust -= 3.0
                    size_factor = 0.25  # Extra conservative
                    reasons.append(f"Extreme slope {veto_tf}: {worst_slope*100:.3f}% (penalty -3.0)")
                    tags['macro_slope_extreme_penalty'] = True
                    logger.warning(f"⚠️ MACRO EXTREME SLOPE ({veto_tf}={worst_slope*100:.3f}%) - RANGE/BREAKOUT mode, penalty only, size={size_factor}")
                else:
                    # In TREND mode: hard block
                    tags['macro_slope_veto'] = True
                    tags['macro_veto_tf'] = veto_tf
                    logger.warning(f"⛔ MACRO SLOPE VETO: {bias} blocked by {veto_tf} EMA200 slope ({worst_slope*100:.3f}%)")
                    return FilterDecision(
                        allow=False,
                        regime="MACRO_VETO",
                        reasons=[f"Macro slope veto: {veto_tf} EMA200 slope = {worst_slope*100:.3f}%"],
                        tags=tags
                    )
            
            elif slope_magnitude >= self.SLOPE_MODERATE_THRESHOLD:
                # MODERATE slope - penalty
                score_adjust += self.SLOPE_MODERATE_PENALTY
                reasons.append(f"Moderate opposing slope {veto_tf}: {worst_slope*100:.3f}%")
                tags['macro_slope_moderate'] = True
                logger.info(f"📊 MACRO: Moderate opposing slope ({veto_tf}={worst_slope*100:.3f}%) - penalty {self.SLOPE_MODERATE_PENALTY}, size={size_factor}")
            
            elif slope_magnitude >= self.SLOPE_MILD_THRESHOLD:
                # MILD slope - light penalty
                score_adjust += self.SLOPE_MILD_PENALTY
                reasons.append(f"Mild opposing slope {veto_tf}: {worst_slope*100:.3f}%")
                tags['macro_slope_mild'] = True
            
            # else: slope < MILD_THRESHOLD - no penalty, too small to matter
        
        # ===== PRICE VS EMA200 CHECK =====
        price_below_ema200_15m = (
            cond_15m.get('ema200') is not None and 
            not cond_15m.get('price_above_ema200', True)
        )
        price_below_ema200_1h = (
            cond_1h.get('ema200') is not None and 
            not cond_1h.get('price_above_ema200', True)
        )
        
        if bias == 'LONG' and (price_below_ema200_15m or price_below_ema200_1h):
            # Block LONGs when price is below EMA200
            below_tf = '15m' if price_below_ema200_15m else '1h'
            tags['macro_price_veto'] = True
            logger.warning(f"⛔ MACRO PRICE VETO: LONG blocked - price below EMA200({below_tf})")
            return FilterDecision(
                allow=False,
                regime="MACRO_VETO",
                reasons=[f"Price below EMA200({below_tf})"],
                tags=tags
            )
        
        if bias == 'SHORT' and (price_below_ema200_15m or price_below_ema200_1h):
            # For SHORT, "price_above_ema200" means price < EMA200 (inverted logic)
            # So if NOT below (price > EMA200), block
            pass  # Logic is already inverted in check_macro_conditions
        
        # ===== ALIGNMENT SCORING =====
        fully_aligned_15m = cond_15m.get('fully_aligned', False)
        fully_aligned_1h = cond_1h.get('fully_aligned', False)
        
        if fully_aligned_15m and fully_aligned_1h:
            # Both timeframes fully aligned - bonus, full size allowed
            tags['macro_fully_aligned'] = True
            reasons.append(f"Macro fully aligned (15m+1h)")
            
            # Extra bonus for strong slope
            if (bias == 'LONG' and slope_15m > self.SLOPE_STRONG_THRESHOLD) or \
               (bias == 'SHORT' and slope_15m < -self.SLOPE_STRONG_THRESHOLD):
                return FilterDecision(
                    allow=True,
                    score_adjust=score_adjust + self.ALIGNED_BONUS * 1.5,
                    reasons=reasons + ["Strong macro slope"],
                    tags=tags,
                    size_factor=1.0  # Full size when fully aligned
                )
            return FilterDecision(
                allow=True,
                score_adjust=score_adjust + self.ALIGNED_BONUS,
                reasons=reasons,
                tags=tags,
                size_factor=1.0  # Full size when fully aligned
            )
        
        elif fully_aligned_15m or fully_aligned_1h:
            # Only one timeframe aligned - small penalty, slight size cap
            aligned_tf = '15m' if fully_aligned_15m else '1h'
            tags['macro_partial_aligned'] = aligned_tf
            reasons.append(f"Macro partial alignment ({aligned_tf} only)")
            return FilterDecision(
                allow=True,
                score_adjust=score_adjust + self.PARTIAL_PENALTY / 2,
                reasons=reasons,
                tags=tags,
                size_factor=min(size_factor, 0.8)  # 80% max when partial
            )
        
        else:
            # Neither aligned - penalty but allow, capped size
            tags['macro_not_aligned'] = True
            reasons.append("Macro not aligned")
            return FilterDecision(
                allow=True,
                score_adjust=score_adjust + self.PARTIAL_PENALTY,
                reasons=reasons,
                tags=tags,
                size_factor=min(size_factor, 0.6)  # 60% max when not aligned
            )


# Singleton
_macro_trend_filter: Optional[MacroTrendFilter] = None


def get_macro_trend_filter(enabled: bool = True) -> MacroTrendFilter:
    global _macro_trend_filter
    if _macro_trend_filter is None:
        _macro_trend_filter = MacroTrendFilter(enabled=enabled)
    return _macro_trend_filter


def reset_macro_trend_filter():
    global _macro_trend_filter
    _macro_trend_filter = None
