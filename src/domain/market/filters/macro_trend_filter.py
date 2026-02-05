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
from datetime import datetime, timedelta
import logging

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision
from src.shared.indicators import calculate_ema

logger = logging.getLogger(__name__)

# 🔥 FIX #47 (Feb 05): Rate-limit MACRO SLOPE block logs
_last_macro_block_log: dict[str, datetime] = {}  # key = bias+tf -> last log time
_macro_log_interval = timedelta(seconds=60)  # Log at most once per minute


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
    
    # 🔥 FIX #48 (Feb 05): EMA periods adjusted for INTRADAY SCALPING
    # EMA200 en 1h = 200 horas = 8+ días de lag → ABSURDO para scalping
    # Cambiado a EMA20/EMA50 que son mucho más reactivos
    EMA_FAST = 20   # Was 50 - now 20 for faster reaction
    EMA_SLOW = 50   # Was 200 - now 50 (50h = ~2 días, no 8+ días)
    
    # Slope calculation - también más corto para scalping
    SLOPE_LOOKBACK_15M = 3   # Was 5 - now 3 candles = 45min
    SLOPE_LOOKBACK_1H = 2    # Was 4 - now 2 candles = 2h (not 4h)
    
    # 🔥 FIX #48: Slope thresholds más permisivos para scalping
    # Con EMAs más cortas, el slope cambia más rápido
    SLOPE_EXTREME_THRESHOLD = 0.0050   # Was 0.25% - now 0.50% (más permisivo)
    SLOPE_MODERATE_THRESHOLD = 0.0020  # Was 0.10% - now 0.20%
    SLOPE_MILD_THRESHOLD = 0.0010      # Was 0.05% - now 0.10%
    SLOPE_STRONG_THRESHOLD = 0.002     # Was 0.1% - now 0.2% = strong trend bonus
    
    # Score adjustments
    # FIX (Jan 24, 2026): Reduced penalties - bot was at 0 trades in 9h
    ALIGNED_BONUS = 1.0            # Both TFs aligned with bias
    PARTIAL_PENALTY = -0.75        # Only one TF aligned (was -1.5)
    SLOPE_MILD_PENALTY = -0.5      # Small opposing slope (was -1.5)
    SLOPE_MODERATE_PENALTY = -1.0  # Moderate opposing slope (was -2.5)
    
    # 🔥 NEW (Jan 22, 2026): Size caps when trading against macro slope
    # Different caps per regime_mode
    # FIX (Jan 24, 2026): Raised caps to allow more trades
    SIZE_CAP_AGAINST_MACRO = {
        'TREND': 0.6,      # Max 60% size when macro against in trend mode (was 0.5)
        'RANGE': 0.4,      # Probe size in range (was 0.25)
        'BREAKOUT': 0.5,   # Probe size for breakout attempts (was 0.4)
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
        
        closes = df['close'].to_numpy()
        
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
                 df_15m: pd.DataFrame | None = None,
                 df_1h: pd.DataFrame | None = None,
                 current_price: float | None = None,
                 bias: str | None = None,
                 regime_mode: str | None = None,  # 🔥 NEW: 'TREND', 'RANGE', or 'BREAKOUT'
                 displacement_bypass: bool = False,  # 🔥 FIX (Feb 3): Skip hard veto during displacement
                 anticipation_bypass: bool = False,  # 🔥 FIX #46 (Feb 05): ANTICIPATION bypasses macro slope
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
            displacement_bypass: If True, skip hard veto (displacement = volatility shock)
            anticipation_bypass: If True, skip hard veto (ANTICIPATION has own confirmation)
        """
        if current_price is None or bias is None:
            return FilterDecision.allow_default()
        
        # Return early if no data available
        if df_15m is None and df_1h is None:
            return FilterDecision.allow_default()
        
        # Check conditions on each timeframe
        cond_15m = self.check_macro_conditions(df_15m, '15m', current_price, bias) if df_15m is not None else {}  # pyright: ignore[reportArgumentType]
        cond_1h = self.check_macro_conditions(df_1h, '1h', current_price, bias) if df_1h is not None else {}  # pyright: ignore[reportArgumentType]
        
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
            size_factor = self.SIZE_CAP_AGAINST_MACRO.get(regime_mode or 'TREND', 0.5)
            tags['macro_size_cap'] = size_factor
            reasons.append(f"Size capped to {size_factor*100:.0f}% (against macro)")
            
            if slope_magnitude >= self.SLOPE_EXTREME_THRESHOLD:
                # EXTREME slope - only hard block in TREND mode (and NOT during displacement or anticipation)
                # 🔥 FIX (Feb 3): displacement_bypass = volatility shock, skip hard veto
                # 🔥 FIX #46 (Feb 05): anticipation_bypass = ANTICIPATION has its own confirmation
                any_bypass = displacement_bypass or anticipation_bypass
                if regime_mode in ('RANGE', 'BREAKOUT') or not self.hard_veto_on_slope or any_bypass:
                    # In RANGE/BREAKOUT or DISPLACEMENT/ANTICIPATION: heavy penalty but allow
                    score_adjust -= 3.0
                    # 🔥 FIX #46: ANTICIPATION gets 60% size (more confidence than displacement)
                    if anticipation_bypass:
                        size_factor = 0.60
                        tags['anticipation_bypass'] = True
                        tags['macro_size_cap'] = size_factor  # Update final size cap
                        logger.info(f"[BYPASS] MACRO SLOPE: {bias} allowed via ANTICIPATION (slope={worst_slope*100:.3f}%) | Size: 60%")
                    elif displacement_bypass:
                        size_factor = 0.50
                        tags['macro_size_cap'] = size_factor  # Update final size cap
                        logger.info(f"[BYPASS] MACRO SLOPE: {bias} allowed via DISPLACEMENT (slope={worst_slope*100:.3f}%)")
                    else:
                        size_factor = 0.25
                        tags['macro_size_cap'] = size_factor  # Update final size cap
                        logger.debug(f"[WARN] MACRO EXTREME SLOPE ({veto_tf}={worst_slope*100:.3f}%)")
                    reasons.append(f"Extreme slope {veto_tf}: {worst_slope*100:.3f}% (penalty -3.0)")
                    tags['macro_slope_extreme_penalty'] = True
                    tags['displacement_bypass'] = displacement_bypass
                else:
                    # In TREND mode without any bypass: hard block
                    tags['macro_slope_veto'] = True
                    tags['macro_veto_tf'] = veto_tf
                    
                    # 🔥 FIX #47 (Feb 05): Rate-limit MACRO SLOPE block logs
                    log_key = f'{bias}_{veto_tf}'
                    now = datetime.utcnow()
                    last_log = _last_macro_block_log.get(log_key)
                    if last_log is None or (now - last_log) >= _macro_log_interval:
                        _last_macro_block_log[log_key] = now
                        logger.warning(f"[BLOCK] MACRO SLOPE: {bias} blocked by {veto_tf} ({worst_slope*100:.3f}%)")
                    
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
                logger.debug(f"[DATA] MACRO: Moderate slope ({veto_tf}={worst_slope*100:.3f}%)")
            
            elif slope_magnitude >= self.SLOPE_MILD_THRESHOLD:
                # MILD slope - light penalty
                score_adjust += self.SLOPE_MILD_PENALTY
                reasons.append(f"Mild opposing slope {veto_tf}: {worst_slope*100:.3f}%")
                tags['macro_slope_mild'] = True
            
            # else: slope < MILD_THRESHOLD - no penalty, too small to matter
        
        # ===== PRICE VS EMA200 CHECK =====
        # 🔥 FIX (Jan 26, 2026): For INTRADAY trading, EMA200 should NEVER hard block.
        # Reason: Pullbacks within HTF trends are VALID intraday plays.
        # The EMA200 on 1h is too slow for 1m-5m trading decisions.
        # Convert ALL EMA200 violations to penalties, never hard blocks.
        price_below_ema200_15m = (
            cond_15m.get('ema200') is not None and 
            not cond_15m.get('price_above_ema200', True)
        )
        price_below_ema200_1h = (
            cond_1h.get('ema200') is not None and 
            not cond_1h.get('price_above_ema200', True)
        )
        
        if bias == 'LONG' and (price_below_ema200_15m or price_below_ema200_1h):
            # 🔥 FIX (Jan 27, 2026): EMA200 is CONTEXT not GOSPEL for intraday
            # Reduced penalty significantly - intraday pullbacks are valid plays
            # Only apply minimal penalty for awareness, don't destroy position sizing
            below_tf = '15m' if price_below_ema200_15m else '1h'
            
            tags['macro_price_penalty'] = True
            score_adjust -= 0.5  # 🔥 Reduced from 1.5 - just awareness, not blocking
            size_factor = 0.8    # 🔥 Raised from 0.5 - allow normal-ish size
            reasons.append(f"LONG below EMA200({below_tf}) - minor penalty")
            logger.debug(f"[DATA] MACRO: LONG below EMA200({below_tf}) - score -0.5, size 80%")
        
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
