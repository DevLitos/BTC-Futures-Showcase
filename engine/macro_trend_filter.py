"""
Macro Trend Filter - EMA200 on 15m + 1h with slope hard veto

Detects macro trend changes earlier than 1h alone by checking:
1. EMA200 on 1h (existing)
2. EMA200 on 15m (new - faster detection)
3. Slope of EMAs (hard veto if slope opposes bias)

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime
import logging

from engine.filter_interface import BaseFilter, FilterDecision
from engine.indicators import calculate_ema

logger = logging.getLogger(__name__)


class MacroTrendFilter(BaseFilter):
    """
    Macro Trend Filter with 15m and 1h EMA200 + slope veto.
    
    Rules for LONG:
    - Price > EMA200(15m) AND Price > EMA200(1h)
    - EMA50 > EMA200 (on both timeframes for extra confirmation)
    - Slope(EMA200, lookback=5) must not be negative (hard veto)
    
    If any condition fails:
    - Slope negative = HARD BLOCK
    - Price < EMA200 = HARD BLOCK or heavy penalty
    - Only EMA50 < EMA200 = Penalty
    """
    
    # EMA periods
    EMA_FAST = 50
    EMA_SLOW = 200
    
    # Slope calculation
    SLOPE_LOOKBACK_15M = 5   # 5 candles = 1h15m
    SLOPE_LOOKBACK_1H = 4    # 4 candles = 4h
    
    # Slope thresholds (as percentage change)
    SLOPE_VETO_THRESHOLD = -0.0005  # -0.05% decline = veto
    SLOPE_STRONG_THRESHOLD = 0.001   # +0.1% = strong trend bonus
    
    # Score adjustments
    ALIGNED_BONUS = 1.0         # Both TFs aligned with bias
    PARTIAL_PENALTY = -1.5      # Only one TF aligned
    SLOPE_PENALTY = -2.0        # Slope against bias (if not vetoing)
    
    def __init__(self, 
                 enabled: bool = True,
                 hard_veto_on_slope: bool = True):
        super().__init__(enabled)
        self.hard_veto_on_slope = hard_veto_on_slope
    
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
            result['slope_aligned'] = result['ema200_slope'] > self.SLOPE_VETO_THRESHOLD
            result['fully_aligned'] = (
                result['price_above_ema200'] and 
                result['ema50_above_ema200'] and 
                result['slope_aligned']
            )
        else:  # SHORT
            result['price_above_ema200'] = current_price < result['ema200']  # Actually "below"
            result['ema50_above_ema200'] = result['ema50'] < result['ema200']  # Actually "below"
            result['slope_aligned'] = result['ema200_slope'] < -self.SLOPE_VETO_THRESHOLD
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
                 **kwargs) -> FilterDecision:
        """
        Evaluate macro trend conditions.
        
        Args:
            df_15m: 15-minute OHLC data
            df_1h: 1-hour OHLC data
            current_price: Current price
            bias: 'LONG' or 'SHORT'
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
        }
        
        reasons = []
        
        # ===== SLOPE VETO CHECK (HARD BLOCK) =====
        # If either timeframe has slope against bias, veto
        slope_15m = cond_15m.get('ema200_slope', 0) or 0
        slope_1h = cond_1h.get('ema200_slope', 0) or 0
        
        if bias == 'LONG':
            slope_vetoed = (
                (slope_15m < self.SLOPE_VETO_THRESHOLD and cond_15m.get('ema200') is not None) or
                (slope_1h < self.SLOPE_VETO_THRESHOLD and cond_1h.get('ema200') is not None)
            )
            veto_tf = '15m' if slope_15m < self.SLOPE_VETO_THRESHOLD else '1h'
            veto_slope = slope_15m if veto_tf == '15m' else slope_1h
        else:  # SHORT
            slope_vetoed = (
                (slope_15m > -self.SLOPE_VETO_THRESHOLD and cond_15m.get('ema200') is not None) or
                (slope_1h > -self.SLOPE_VETO_THRESHOLD and cond_1h.get('ema200') is not None)
            )
            veto_tf = '15m' if slope_15m > -self.SLOPE_VETO_THRESHOLD else '1h'
            veto_slope = slope_15m if veto_tf == '15m' else slope_1h
        
        if slope_vetoed and self.hard_veto_on_slope:
            tags['macro_slope_veto'] = True
            tags['macro_veto_tf'] = veto_tf
            logger.warning(f"⛔ MACRO SLOPE VETO: {bias} blocked by {veto_tf} EMA200 slope ({veto_slope*100:.3f}%)")
            return FilterDecision(
                allow=False,
                regime="MACRO_VETO",
                reasons=[f"Macro slope veto: {veto_tf} EMA200 slope = {veto_slope*100:.3f}%"],
                tags=tags
            )
        
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
            # Both timeframes fully aligned - bonus
            tags['macro_fully_aligned'] = True
            reasons.append(f"Macro fully aligned (15m+1h)")
            
            # Extra bonus for strong slope
            if (bias == 'LONG' and slope_15m > self.SLOPE_STRONG_THRESHOLD) or \
               (bias == 'SHORT' and slope_15m < -self.SLOPE_STRONG_THRESHOLD):
                return FilterDecision(
                    allow=True,
                    score_adjust=self.ALIGNED_BONUS * 1.5,
                    reasons=reasons + ["Strong macro slope"],
                    tags=tags
                )
            return FilterDecision(
                allow=True,
                score_adjust=self.ALIGNED_BONUS,
                reasons=reasons,
                tags=tags
            )
        
        elif fully_aligned_15m or fully_aligned_1h:
            # Only one timeframe aligned - small penalty
            aligned_tf = '15m' if fully_aligned_15m else '1h'
            tags['macro_partial_aligned'] = aligned_tf
            reasons.append(f"Macro partial alignment ({aligned_tf} only)")
            return FilterDecision(
                allow=True,
                score_adjust=self.PARTIAL_PENALTY / 2,  # Mild penalty
                reasons=reasons,
                tags=tags
            )
        
        else:
            # Neither aligned - penalty but allow
            tags['macro_not_aligned'] = True
            reasons.append("Macro not aligned")
            return FilterDecision(
                allow=True,
                score_adjust=self.PARTIAL_PENALTY,
                reasons=reasons,
                tags=tags
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
