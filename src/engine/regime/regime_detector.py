"""
Regime Detector - Context-Aware Market State Classification

"BTC no paga igual en todos los contextos"

4 Regimes:
- TREND_EXPANSION: ADX alto y creciente, BOS reciente, rango se expande
- TREND_EXHAUSTION: ADX alto pero plano/negativo, divergencias
- RANGE: ADX bajo, VWAP dominante, falsos BOS
- CHAOS: Spread irregular, velocity errática, volumen anómalo

Each regime has different:
- Chasing tolerance
- Order type preference
- Score requirements
- Position sizing
- Minimum R:R
"""

import logging
import numpy as np
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class EntryRegime(Enum):
    """Market regime classification"""
    TREND_EXPANSION = "TREND_EXPANSION"      # Best for momentum, allow chasing
    TREND_EXHAUSTION = "TREND_EXHAUSTION"    # Caution, premium setups only
    RANGE = "RANGE"                          # Mean reversion, anti-chase hard
    CHAOS = "CHAOS"                          # No trade or minimum size


@dataclass
class RegimeConfig:
    """Configuration for each regime"""
    chasing_allowed: bool           # Can we chase price?
    chasing_penalty_multiplier: float  # How much to penalize chasing (0 = none)
    order_type: str                 # 'MARKET', 'LIMIT', 'LIMIT_FALLBACK', 'NONE'
    score_threshold_adj: int        # Adjustment to base score threshold
    size_multiplier: float          # Position size multiplier
    min_rr: float                   # Minimum R:R required
    sl_atr_multiplier: float        # SL distance in ATR (RANGE=tighter, TREND=wider)
    description: str


# Regime behavior table - the heart of the system
REGIME_CONFIGS: Dict[EntryRegime, RegimeConfig] = {
    EntryRegime.TREND_EXPANSION: RegimeConfig(
        chasing_allowed=True,
        chasing_penalty_multiplier=0.3,  # Very light penalty - momentum is expected
        order_type='LIMIT_FALLBACK',     # LIMIT with MARKET fallback
        score_threshold_adj=-1,          # Lower threshold (easier entry)
        size_multiplier=1.2,             # Larger size
        min_rr=0.9,
        sl_atr_multiplier=3.5,           # FIX (Jan 20, 2026): Increased from 2.5 to 3.5 for fee survival
        description="Momentum expansion - allow aggression"
    ),
    EntryRegime.TREND_EXHAUSTION: RegimeConfig(
        chasing_allowed=False,
        chasing_penalty_multiplier=1.5,  # Moderate penalty
        order_type='LIMIT',              # Limit only
        score_threshold_adj=+1,          # Higher threshold (premium only)
        size_multiplier=0.7,             # Reduced size
        min_rr=1.5,
        sl_atr_multiplier=3.5,           # FIX (Jan 20, 2026): Increased from 2.5 to 3.5 for fee survival
        description="Trend tired - premium setups only"
    ),
    EntryRegime.RANGE: RegimeConfig(
        chasing_allowed=True,             # Allow market if far from value!
        chasing_penalty_multiplier=0.5,   # Low penalty - in range, chasing TO value is good
        order_type='LIMIT_FALLBACK',      # LIMIT preferred but MARKET ok near extremes
        score_threshold_adj=0,            # Normal threshold
        size_multiplier=0.9,              # Slightly reduced (range = more trades)
        min_rr=0.5,                        # Lower RR target (quick scalps)
        sl_atr_multiplier=3.0,            # FIX (Jan 20, 2026): Increased from 2.0 to 3.0 for fee survival
        description="Mean reversion - quick scalps to value"
    ),
    EntryRegime.CHAOS: RegimeConfig(
        chasing_allowed=False,
        chasing_penalty_multiplier=5.0,  # Heavy penalty
        order_type='NONE',                # No trading
        score_threshold_adj=+5,           # Impossible threshold
        size_multiplier=0.0,              # Zero size
        min_rr=999.0,
        sl_atr_multiplier=3.0,            # N/A (no trading)
        description="Chaotic - no trade"
    ),
}


class RegimeDetector:
    """
    Detect current market regime using simple, explainable logic.
    
    No ML, no magic - just ADX, range expansion, and velocity.
    
    Usage:
        detector = RegimeDetector()
        regime = detector.detect(df_1h, df_5m)
        config = detector.get_config(regime)
    """
    
    # Thresholds (tunable but start conservative)
    ADX_TREND_THRESHOLD = 25.0      # Above = trending
    ADX_WEAK_THRESHOLD = 18.0       # Below = ranging
    ADX_SLOPE_LOOKBACK = 5          # Bars for slope calculation
    RANGE_EXPANSION_THRESHOLD = 1.2  # Current range vs avg range
    VELOCITY_CHAOS_THRESHOLD = 2.5   # ATR multiplier for chaos
    
    def __init__(self):
        self._last_regime: EntryRegime = EntryRegime.RANGE  # Default conservative
        self._regime_duration: int = 0  # How long in current regime
        self._regime_history: list = []  # Last N regimes for stability
        
    def detect(
        self, 
        adx: float,
        adx_values: np.ndarray,  # Last N ADX values for slope
        atr_current: float,
        atr_avg: float,
        has_bos: bool = False,
        spread_pct: float = 0.0,
        volume_spike: bool = False
    ) -> EntryRegime:
        """
        Detect market regime.
        
        Args:
            adx: Current ADX value
            adx_values: Array of recent ADX values (for slope)
            atr_current: Current ATR
            atr_avg: Average ATR (last 20 bars)
            has_bos: Whether BOS was detected recently
            spread_pct: Current spread as percentage
            volume_spike: Whether volume is abnormally high
            
        Returns:
            EntryRegime enum
        """
        # Calculate ADX slope
        adx_slope = self._calculate_slope(adx_values)
        
        # Calculate range expansion
        range_expansion = atr_current / atr_avg if atr_avg > 0 else 1.0
        
        # Detect CHAOS first (takes priority)
        if self._is_chaos(spread_pct, range_expansion, volume_spike):
            regime = EntryRegime.CHAOS
        
        # TREND_EXPANSION: ADX high and rising, range expanding
        elif adx > self.ADX_TREND_THRESHOLD and adx_slope > 0 and range_expansion > self.RANGE_EXPANSION_THRESHOLD:
            regime = EntryRegime.TREND_EXPANSION
        
        # TREND_EXHAUSTION: ADX high but flat/falling
        elif adx > self.ADX_TREND_THRESHOLD and adx_slope <= 0:
            regime = EntryRegime.TREND_EXHAUSTION
        
        # RANGE: ADX low
        elif adx < self.ADX_WEAK_THRESHOLD:
            regime = EntryRegime.RANGE
        
        # Default: between thresholds, use previous or RANGE
        else:
            regime = self._last_regime if self._last_regime != EntryRegime.CHAOS else EntryRegime.RANGE
        
        # Update tracking
        if regime == self._last_regime:
            self._regime_duration += 1
        else:
            self._regime_duration = 1
            logger.info(f"🔄 REGIME CHANGE: {self._last_regime.value} → {regime.value}")
        
        self._last_regime = regime
        self._regime_history.append(regime)
        if len(self._regime_history) > 20:
            self._regime_history.pop(0)
        
        return regime
    
    def _calculate_slope(self, values: np.ndarray) -> float:
        """Calculate slope of values using linear regression"""
        if len(values) < 3:
            return 0.0
        
        # Use last N values
        n = min(self.ADX_SLOPE_LOOKBACK, len(values))
        y = values[-n:]
        x = np.arange(n)
        
        # Simple linear regression slope
        try:
            slope = np.polyfit(x, y, 1)[0]
            return slope
        except (np.linalg.LinAlgError, ValueError, TypeError) as e:
            logger.debug(f"Slope calculation failed: {e}")
            return 0.0
    
    def _is_chaos(self, spread_pct: float, range_expansion: float, volume_spike: bool) -> bool:
        """Detect chaotic market conditions"""
        # Abnormal spread (> 0.1% for BTC)
        if spread_pct > 0.1:
            return True
        
        # Extreme range expansion with volume spike
        if range_expansion > self.VELOCITY_CHAOS_THRESHOLD and volume_spike:
            return True
        
        return False
    
    def get_config(self, regime: EntryRegime = None) -> RegimeConfig:
        """Get configuration for a regime"""
        if regime is None:
            regime = self._last_regime
        return REGIME_CONFIGS[regime]
    
    @property
    def current_regime(self) -> EntryRegime:
        """Current detected regime"""
        return self._last_regime
    
    @property
    def regime_age(self) -> int:
        """How many bars in current regime"""
        return self._regime_duration
    
    def get_chase_penalty(self, chase_atr: float, regime: EntryRegime = None) -> float:
        """
        Calculate chase penalty based on regime.
        
        This is the KEY function that makes anti-chasing regime-aware.
        
        Args:
            chase_atr: How many ATRs price has moved
            regime: Optional regime override
            
        Returns:
            Score penalty (0 = no penalty, higher = worse)
        """
        if regime is None:
            regime = self._last_regime
        
        config = REGIME_CONFIGS[regime]
        
        # Base penalty calculation - GRADUAL, not step function
        # In DISCOVERY mode, we want information, not protection
        # Chase is expensive, but not wrong
        if chase_atr <= 0.6:
            base_penalty = 0.0  # No chase
        elif chase_atr <= 1.0:
            base_penalty = 0.5  # Light chase - common in BTC
        elif chase_atr <= 1.5:
            base_penalty = 1.0  # Moderate chase
        elif chase_atr <= 2.0:
            base_penalty = 1.5  # Heavy chase
        elif chase_atr <= 2.5:
            base_penalty = 2.0  # Very heavy chase
        else:
            base_penalty = 2.5  # Extreme - almost blocking
        
        # Apply regime multiplier
        final_penalty = base_penalty * config.chasing_penalty_multiplier
        
        return final_penalty
    
    def should_block_trade(self, chase_atr: float, regime: EntryRegime = None) -> Tuple[bool, str]:
        """
        Determine if trade should be blocked entirely (not just penalized).
        
        Only blocks in extreme cases:
        - CHAOS regime
        - Chase > 2.0 ATR in non-expansion regime
        - Chase > 3.0 ATR always
        
        Returns:
            (should_block: bool, reason: str)
        """
        if regime is None:
            regime = self._last_regime
        
        # CHAOS always blocks
        if regime == EntryRegime.CHAOS:
            return True, "CHAOS regime - no trading"
        
        # Extreme chase always blocks
        if chase_atr > 3.0:
            return True, f"Extreme chase ({chase_atr:.1f} ATR) - too risky"
        
        # High chase in non-expansion blocks
        if chase_atr > 2.0 and regime != EntryRegime.TREND_EXPANSION:
            return True, f"High chase ({chase_atr:.1f} ATR) in {regime.value}"
        
        return False, ""
    
    def get_momentum_context(
        self,
        has_bos: bool,
        adx_rising: bool,
        candle_expansion: bool,
        vwap_reclaim: bool = False
    ) -> bool:
        """
        Check if momentum context justifies chasing.
        
        If momentum context is positive, chasing is more acceptable.
        """
        positive_signals = sum([
            has_bos,
            adx_rising,
            candle_expansion,
            vwap_reclaim
        ])
        
        # Need at least 2 of 4 for positive context
        return positive_signals >= 2
    
    def get_status(self) -> Dict[str, Any]:
        """Get current detector status"""
        config = self.get_config()
        return {
            'regime': self._last_regime.value,
            'duration': self._regime_duration,
            'chasing_allowed': config.chasing_allowed,
            'order_type': config.order_type,
            'size_multiplier': config.size_multiplier,
            'min_rr': config.min_rr,
            'description': config.description
        }


def calculate_chase_atr(
    current_price: float,
    entry_signal_price: float,  # Price when signal was generated
    atr: float,
    direction: str
) -> float:
    """
    Calculate how many ATRs price has moved since signal.
    
    Args:
        current_price: Current market price
        entry_signal_price: Price when the entry signal was generated
        atr: Current ATR
        direction: 'LONG' or 'SHORT'
        
    Returns:
        Chase distance in ATRs (positive = chasing, negative = better entry)
    """
    if atr <= 0:
        return 0.0
    
    if direction == 'LONG':
        # For LONG, chasing = price went up since signal
        chase_distance = current_price - entry_signal_price
    else:
        # For SHORT, chasing = price went down since signal
        chase_distance = entry_signal_price - current_price
    
    return chase_distance / atr
