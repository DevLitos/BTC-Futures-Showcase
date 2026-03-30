"""
Volatility Shock Filter - Detects abnormal candles and enforces cooling-off periods

Detects when TrueRange > k*ATR (individual candle spike) and:
1. Blocks trading during SEVERE/EXTREME spikes only
2. Penalizes score for MODERATE/ELEVATED but allows trading
3. Enforces cooldown ONLY for SEVERE/EXTREME (actual chaos)

Complete rewrite with:
   - Timeframe-specific thresholds (1m vs 5m vs 15m)
   - Uses CLOSED candle only (not live candle)
   - No ATR estimation fallback (requires real data)
   - Dynamic cooldown based on severity ratio
   - MODERATE/ELEVATED only penalize, don't block

Created: January 15, 2026
Rewritten: January 22, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple, Dict
from datetime import datetime, timedelta
from dataclasses import dataclass
import logging

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


@dataclass
class ShockState:
    """State tracking for volatility shocks"""
    shock_detected: bool = False
    shock_ratio: float = 0.0
    shock_level: str = "NONE"  # NONE, MODERATE, ELEVATED, SEVERE, EXTREME
    shock_until: Optional[datetime] = None
    cooldown_remaining_seconds: int = 0
    tr: float = 0.0
    atr: float = 0.0
    timeframe: str = "5m"


# Timeframe-specific thresholds
# BTC on 1m has more noise, so higher thresholds
# BTC on 5m/15m more stable, lower thresholds make sense
THRESHOLDS_BY_TF: Dict[str, Dict[str, float]] = {
    '1m': {
        'MODERATE': 3.0,    # 3.0-3.8x → penalty only
        'ELEVATED': 3.8,    # 3.8-4.5x → penalty only  
        'SEVERE': 4.5,      # 4.5-6.0x → block + cooldown
        'EXTREME': 6.0      # 6.0x+ → chaos block
    },
    '5m': {
        'MODERATE': 2.5,    # 2.5-3.2x → penalty only
        'ELEVATED': 3.2,    # 3.2-4.0x → penalty only
        'SEVERE': 4.0,      # 4.0-5.5x → block + cooldown
        'EXTREME': 5.5      # 5.5x+ → chaos block
    },
    '15m': {
        'MODERATE': 2.2,    # More conservative on higher TF
        'ELEVATED': 2.8,
        'SEVERE': 3.5,
        'EXTREME': 5.0
    }
}


class VolatilityShockFilter(BaseFilter):
    """
    Volatility Shock Filter with intelligent cooldown.
    
    🔥 KEY DESIGN (Jan 22, 2026):
    - Timeframe-aware thresholds (1m is noisier than 5m/15m)
    - Uses CLOSED candle only ([-2] index) to avoid mid-candle false positives
    - MODERATE/ELEVATED: Penalize score only, NO COOLDOWN
    - SEVERE/EXTREME: HARD BLOCK + dynamic cooldown based on ratio
    - No ATR estimation - requires real data or skips
    
    Score penalties:
    - MODERATE: -1.0
    - ELEVATED: -2.0
    
    Cooldown (only SEVERE/EXTREME):
    - Dynamic: base_cooldown * clamp((ratio - severe_threshold) / 1.5, 1.0, 2.0)
    - SEVERE base: 12 min → 12-24 min depending on severity
    - EXTREME base: 25 min → 25-50 min depending on severity
    """
    
    # ATR period
    ATR_PERIOD = 14
    
    # Score adjustments (when allowed but penalized)
    # Lighter penalties - volatility can be opportunity
    MODERATE_PENALTY = -0.5  # Was -1.0
    ELEVATED_PENALTY = -1.0  # Was -2.0
    
    # Base cooldown durations (minutes) - only for SEVERE/EXTREME
    # Reduced cooldowns - post-spike often best entries
    # 🔥 FIX v2: Further reduced - volatility = opportunity, not danger
    SEVERE_BASE_COOLDOWN = 3    # 3-5 min based on ratio (was 6-10)
    EXTREME_BASE_COOLDOWN = 6   # 6-10 min based on ratio (was 12-20)
    
    def __init__(self, 
                 enabled: bool = True,
                 default_timeframe: str = "5m"):
        super().__init__(enabled)
        self.default_timeframe = default_timeframe
        
        # State
        self._shock_until: Optional[datetime] = None
        self._shock_level: str = "NONE"
        self._last_state = ShockState()
    
    def _get_thresholds(self, timeframe: str) -> Dict[str, float]:
        """Get thresholds for timeframe, defaulting to 5m if unknown"""
        return THRESHOLDS_BY_TF.get(timeframe, THRESHOLDS_BY_TF['5m'])
    
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
                      period: int | None = None) -> float:
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
            return float(np.mean(tr_values)) if tr_values else 0.0
        
        # RMA (Wilder's Moving Average)
        atr = np.mean(tr_values[:period])
        alpha = 1 / period
        for tr in tr_values[period:]:
            atr = alpha * tr + (1 - alpha) * atr
        
        return float(atr)
    
    def detect_shock(self, 
                     current_high: float,
                     current_low: float,
                     prev_close: float,
                     atr: float,
                     timeframe: str = "5m") -> Tuple[bool, float, str]:
        """
        Detect if candle is a volatility shock.
        
        Returns:
            (is_shock, shock_ratio, shock_level)
        """
        if atr <= 0:
            return False, 0.0, "NONE"
        
        tr = self.calculate_true_range(current_high, current_low, prev_close)
        ratio = tr / atr
        
        thresholds = self._get_thresholds(timeframe)
        
        if ratio >= thresholds['EXTREME']:
            return True, ratio, "EXTREME"
        elif ratio >= thresholds['SEVERE']:
            return True, ratio, "SEVERE"
        elif ratio >= thresholds['ELEVATED']:
            return True, ratio, "ELEVATED"
        elif ratio >= thresholds['MODERATE']:
            return True, ratio, "MODERATE"
        else:
            return False, ratio, "NONE"
    
    def get_dynamic_cooldown(self, shock_level: str, ratio: float, timeframe: str) -> int:
        """
        Get dynamic cooldown duration in minutes.
        
        Cooldown scales with severity:
        - SEVERE: 12-24 min based on how far above threshold
        - EXTREME: 25-50 min based on how far above threshold
        - MODERATE/ELEVATED: 0 (no cooldown)
        """
        if shock_level not in ("SEVERE", "EXTREME"):
            return 0
        
        thresholds = self._get_thresholds(timeframe)
        
        if shock_level == "EXTREME":
            base = self.EXTREME_BASE_COOLDOWN
            threshold = thresholds['EXTREME']
        else:  # SEVERE
            base = self.SEVERE_BASE_COOLDOWN
            threshold = thresholds['SEVERE']
        
        # Scale factor: 1.0 to 2.0 based on how much above threshold
        # ratio - threshold gives excess, divide by 1.5 to normalize
        scale = max(1.0, min(2.0, 1.0 + (ratio - threshold) / 1.5))
        
        return int(base * scale)

    def evaluate(self,
                 df: pd.DataFrame | None = None,
                 market_time: datetime | None = None,
                 timeframe: str | None = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate volatility shock conditions.
        
        Args:
            df: DataFrame with OHLC data (REQUIRED - must have at least ATR_PERIOD+2 rows)
            market_time: Current time (for cooldown tracking)
            timeframe: "1m", "5m", or "15m" for threshold selection
        """
        now = market_time or datetime.utcnow()
        tf = timeframe or self.default_timeframe
        
        # Check if still in cooldown from previous shock
        if self._shock_until and now < self._shock_until:
            remaining = (self._shock_until - now).total_seconds()
            
            # 🔥 NEW (Jan 22, 2026): Early cooldown exit conditions
            # Allow re-entry if conditions have normalized
            early_exit_allowed = False
            early_exit_reason = None
            
            # Check if we can get spread/bb_width info from kwargs
            spread_normalized = kwargs.get('spread_normalized', False)  # Spread back to normal
            bb_expanding = kwargs.get('bb_expanding', False)  # BB width still expanding (opportunity)
            shock_ratio_now = kwargs.get('current_shock_ratio', None)  # Current TR/ATR ratio
            
            # Allow early exit if:
            # 1. Spread has normalized (not wide anymore)
            # 2. Remaining cooldown is < 50% AND bb expanding (opportunity)
            # 3. Current shock ratio is below MODERATE (calm market)
            if spread_normalized and bb_expanding and remaining < (self.SEVERE_BASE_COOLDOWN * 60 * 0.5):
                early_exit_allowed = True
                early_exit_reason = "Spread normalized + BB expanding"
            elif shock_ratio_now is not None and shock_ratio_now < THRESHOLDS_BY_TF[tf]['MODERATE'] * 0.8:
                early_exit_allowed = True
                early_exit_reason = f"Market calmed (ratio {shock_ratio_now:.1f}x < threshold)"
            
            if early_exit_allowed:
                logger.info(f"✅ SHOCK COOLDOWN EARLY EXIT: {early_exit_reason}")
                self._shock_until = None
                self._shock_level = "NONE"
            else:
                self._last_state = ShockState(
                    shock_detected=False,
                    shock_level=self._shock_level,
                    shock_until=self._shock_until,
                    cooldown_remaining_seconds=int(remaining),
                    timeframe=tf
                )
                logger.info(f"⏳ SHOCK COOLDOWN: {remaining/60:.1f} min remaining (level={self._shock_level})")
                return FilterDecision(
                    allow=False,
                    regime="SHOCK_COOLDOWN",
                    reasons=[f"Shock cooldown: {remaining/60:.1f} min remaining"],
                    tags={
                        'shock_cooldown': True,
                        'shock_level': self._shock_level,
                        'cooldown_remaining_min': round(remaining/60, 1),
                        'timeframe': tf
                    }
                )
        else:
            # Cooldown expired
            self._shock_until = None
            self._shock_level = "NONE"
        
        # 🔥 REQUIRE df with enough data
        if df is None or len(df) < self.ATR_PERIOD + 2:
            logger.debug(f"Volatility shock: insufficient data (need {self.ATR_PERIOD + 2} candles)")
            return FilterDecision(
                allow=True,
                reasons=["Insufficient data for shock detection"],
                tags={'shock_data_valid': False, 'timeframe': tf}
            )
        
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        closes = df['close'].to_numpy()
        
        # 🔥 FIX: Use CLOSED candle ([-2]) not live candle ([-1])
        # This avoids mid-candle false positives
        current_high = highs[-2]
        current_low = lows[-2]
        prev_close = closes[-3]
        
        atr = self.calculate_atr(highs[:-1], lows[:-1], closes[:-1])  # Exclude live candle from ATR
        
        if atr <= 0:
            return FilterDecision(
                allow=True,
                reasons=["ATR calculation failed"],
                tags={'shock_data_valid': False, 'atr': 0, 'timeframe': tf}
            )
        
        # Detect shock on closed candle
        is_shock, ratio, level = self.detect_shock(
            current_high, current_low, prev_close, atr, tf
        )
        
        tr = self.calculate_true_range(current_high, current_low, prev_close)
        
        # Build state
        self._last_state = ShockState(
            shock_detected=is_shock,
            shock_ratio=ratio,
            shock_level=level,
            tr=tr,
            atr=atr,
            timeframe=tf
        )
        
        thresholds = self._get_thresholds(tf)
        tags = {
            'shock_detected': is_shock,
            'shock_ratio': round(ratio, 2),
            'shock_level': level,
            'tr': round(tr, 2),
            'atr': round(atr, 2),
            'timeframe': tf,
            'thresholds': {k: round(v, 2) for k, v in thresholds.items()}
        }
        
        if is_shock:
            # Get dynamic cooldown (0 for MODERATE/ELEVATED)
            cooldown_mins = self.get_dynamic_cooldown(level, ratio, tf)
            
            if cooldown_mins > 0:
                self._shock_until = now + timedelta(minutes=cooldown_mins)
                self._shock_level = level
                tags['shock_until'] = self._shock_until.isoformat()
                tags['shock_cooldown_minutes'] = cooldown_mins
                logger.warning(f"⚡ VOLATILITY SHOCK [{tf}]: TR/ATR={ratio:.1f}x (level={level}) → {cooldown_mins}min cooldown")
            else:
                logger.info(f"📊 VOLATILITY [{tf}]: TR/ATR={ratio:.1f}x (level={level}) → penalty only")
            
            if level == "EXTREME":
                return FilterDecision(
                    allow=False,
                    regime="CHAOS",
                    reasons=[f"Extreme volatility shock [{tf}]: TR={ratio:.1f}x ATR (≥{thresholds['EXTREME']:.1f}x)"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
            elif level == "SEVERE":
                return FilterDecision(
                    allow=False,
                    regime="SHOCK",
                    reasons=[f"Severe volatility shock [{tf}]: TR={ratio:.1f}x ATR (≥{thresholds['SEVERE']:.1f}x)"],
                    tags=tags,
                    cooldown_seconds=cooldown_mins * 60
                )
            elif level == "ELEVATED":
                return FilterDecision(
                    allow=True,
                    score_adjust=self.ELEVATED_PENALTY,
                    regime="ELEVATED_VOL",
                    reasons=[f"Elevated volatility [{tf}]: TR={ratio:.1f}x ATR (penalty -2.0)"],
                    tags=tags
                )
            else:  # MODERATE
                return FilterDecision(
                    allow=True,
                    score_adjust=self.MODERATE_PENALTY,
                    regime="MODERATE_VOL",
                    reasons=[f"Moderate volatility [{tf}]: TR={ratio:.1f}x ATR (penalty -1.0)"],
                    tags=tags
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


def get_volatility_shock_filter(enabled: bool = True, default_timeframe: str = "5m") -> VolatilityShockFilter:
    global _volatility_shock_filter
    if _volatility_shock_filter is None:
        _volatility_shock_filter = VolatilityShockFilter(enabled=enabled, default_timeframe=default_timeframe)
    return _volatility_shock_filter


def reset_volatility_shock_filter():
    global _volatility_shock_filter
    _volatility_shock_filter = None
