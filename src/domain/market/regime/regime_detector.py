"""
Regime Detector - Context-Aware Market State Classification

⚠️ PARTIAL DEPRECATION (Feb 05, 2026):
RegimeConfig defined here is a DUPLICATE.
Prefer using UnifiedRegimeConfig from src/config/unified_constants.py
EntryRegime enum is still valid and actively used.

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

# 🔥 NEW (Feb 05): Import unified config for future migration
from src.config.unified_constants import UnifiedRegimeConfig, UNIFIED_REGIMES, get_unified_regime

logger = logging.getLogger(__name__)


class EntryRegime(Enum):
    """Market regime classification - FIVE STATES for intraday (Jan 26, 2026)"""
    TREND_EXPANSION = "TREND_EXPANSION"      # Best for momentum, allow chasing (1.0-1.2x)
    TREND_EXHAUSTION = "TREND_EXHAUSTION"    # Caution, premium setups only (0.7x)
    NORMAL = "NORMAL"                        # 🔥 NEW (Jan 26): ADX 18-25, standard operation (0.8x)
    PROBING = "PROBING"                      # Validate location, micro-risk (0.25x)
    RANGE = "RANGE"                          # No edge detected (0.0x) - ADX < 18
    CHAOS = "CHAOS"                          # No trade or minimum size (0.0x)


# ⚠️ LEGACY (Feb 05): This RegimeConfig is LOCAL to this file.
# For new code, prefer using UnifiedRegimeConfig from unified_constants.py
@dataclass
class RegimeConfig:
    """Configuration for each regime - LOCAL LEGACY VERSION"""
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
    # =========================================================================
    # 🔥 NEW (Jan 26, 2026): NORMAL MODE - Standard Trading
    # 
    # WHEN: ADX between 18-25, neither strong trend nor weak range
    # PHILOSOPHY: "Market is tradeable, use standard risk management"
    # 
    # This REPLACES the broken logic where ADX 18-25 fell into RANGE (NO_EDGE).
    # With ADX ~23, the market has SOME trend, not zero edge.
    # =========================================================================
    EntryRegime.NORMAL: RegimeConfig(
        chasing_allowed=True,             # Allow some chasing
        chasing_penalty_multiplier=1.0,   # Standard penalty
        order_type='LIMIT_FALLBACK',      # LIMIT preferred, MARKET ok
        score_threshold_adj=0,            # Standard threshold
        size_multiplier=0.8,              # Slightly reduced (DISCOVERY mode)
        min_rr=0.6,                       # Reasonable floor for standard trading
        sl_atr_multiplier=2.5,            # Standard stop distance
        description="📊 NORMAL - standard trading mode"
    ),
    # =========================================================================
    # 🔥 NEW (Jan 26, 2026): PROBING MODE - Diagnostic Probe, Not Exploitation
    # 
    # PHILOSOPHY: "Let the market SHOW you edge, don't guess"
    # 
    # WHEN: No TREND_EXPANSION but location is promising (near VWAP/VPOC)
    # HOW: Graduated sizing via range_size_mult (0.25x-0.65x), limit only, tight stop
    # EXIT RULES:
    #   - If +0.2R in <60s → edge validated, can enable more trades
    #   - If MAE > 0.3R → abort without insisting
    # 
    # NOTE: size_multiplier=1.0 here because actual sizing is controlled by
    # range_size_mult in trade tags (graduated: PROBE_MIN=0.25, MED=0.50, MAX=0.65)
    # =========================================================================
    EntryRegime.PROBING: RegimeConfig(
        chasing_allowed=False,            # NO chasing - diagnostic mode
        chasing_penalty_multiplier=3.0,   # Heavy penalty if any chasing
        order_type='LIMIT',               # LIMIT ONLY - no urgency
        score_threshold_adj=+1,           # Slightly higher bar
        size_multiplier=1.0,              # 🔥 1.0 here - actual sizing from range_size_mult
        min_rr=0.8,                        # Lower RR ok (quick scalp validation)
        sl_atr_multiplier=1.5,            # 🔥 TIGHT stop - 1.5 ATR max
        description="🔍 PROBING - validate location with micro-risk"
    ),
    # =========================================================================
    # RANGE = NO_EDGE (0.0x) - Pure observation
    # This is NOT the old RANGE. This is "no edge detected, watch only".
    # =========================================================================
    EntryRegime.RANGE: RegimeConfig(
        chasing_allowed=False,            # No trading
        chasing_penalty_multiplier=5.0,   # Heavy penalty
        order_type='NONE',                # No orders
        score_threshold_adj=+5,           # Impossible threshold
        size_multiplier=0.0,              # Zero size
        min_rr=999.0,                      # Impossible RR
        sl_atr_multiplier=2.0,            # N/A (no trading)
        description="⛔ NO_EDGE - pure observation mode"
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
        self._last_regime: EntryRegime = EntryRegime.NORMAL  # Default: standard trading
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
        
        # RANGE: ADX very low (< 18) - no edge, pure observation
        elif adx < self.ADX_WEAK_THRESHOLD:
            regime = EntryRegime.RANGE
        
        # 🔥 FIX (Jan 26, 2026): NORMAL for ADX between 18-25
        # Previously this fell into RANGE (NO_EDGE) which blocked ALL trades
        # ADX ~23 is normal tradeable market, not zero edge!
        else:
            # ADX between WEAK (18) and TREND (25) → NORMAL trading
            regime = EntryRegime.NORMAL
        
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
    
    def get_config(self, regime: EntryRegime | None = None) -> RegimeConfig:
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
    
    def get_chase_penalty(self, chase_atr: float, regime: EntryRegime | None = None) -> float:
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
        # 🔥 FIX (Jan 27, 2026): Further reduced penalties
        # Chase penalty was blocking winners:
        # - LONG @ 88213 with 1.35 ATR chase → -0.6pts → missed +1.72R
        # BTC moves fast, chasing 1-2 ATR is NORMAL in intraday.
        if chase_atr <= 1.0:              # 🔥 Was 0.8
            base_penalty = 0.0  # No chase
        elif chase_atr <= 1.5:            # 🔥 Was 1.2
            base_penalty = 0.2  # Light chase (was 0.3)
        elif chase_atr <= 2.0:            # 🔥 Was 1.8  
            base_penalty = 0.4  # Moderate chase (was 0.6)
        elif chase_atr <= 2.5:            # 🔥 Was 2.3
            base_penalty = 0.7  # Heavy chase (was 1.0)
        elif chase_atr <= 3.0:            # 🔥 Was 2.8
            base_penalty = 1.0  # Very heavy chase (was 1.5)
        else:
            base_penalty = 1.5  # Extreme - almost blocking (was 2.0)
        
        # Apply regime multiplier
        final_penalty = base_penalty * config.chasing_penalty_multiplier
        
        return final_penalty
    
    def should_block_trade(self, chase_atr: float, regime: EntryRegime | None = None) -> Tuple[bool, str]:
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
        # FIX (Jan 24, 2026): Raised from 3.0 to 4.0 - was too restrictive
        if chase_atr > 4.0:
            return True, f"Extreme chase ({chase_atr:.1f} ATR) - too risky"
        
        # =========================================================
        # 🔥 FIX #84 (Feb 7, 2026): STRICTER CHASE IN TRANSITION/RANGE
        # 
        # PROBLEMA: Con threshold 2.8 ATR, trades con 2.72 ATR pasaban
        # pero entraban en el TOPE del movimiento y perdían.
        # 
        # SOLUCIÓN: En regímenes NORMAL/RANGE, usar 2.0 ATR como max.
        # El displacement detecta que el precio YA subió, pero si chase
        # es alto = el movimiento ya pasó = no entrar.
        # 
        # 🔧 FIX #85 (Feb 8, 2026): TRANSITION no existe en EntryRegime
        # Cambiado a NORMAL que es el equivalente (ADX 18-25)
        # =========================================================
        is_low_trend_regime = regime in [EntryRegime.NORMAL, EntryRegime.RANGE]
        max_chase_for_regime = 2.0 if is_low_trend_regime else 2.8
        
        if chase_atr > max_chase_for_regime and regime != EntryRegime.TREND_EXPANSION:
            return True, f"High chase ({chase_atr:.1f} ATR > {max_chase_for_regime}) in {regime.value} - movement exhausted"
        
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


# ⚠️ DEPRECATED (Feb 09, 2026 - FIX #128)
# Use src.application.live.market_analysis.calculate_chase_atr instead
# This function is kept for backward compatibility but unused
def calculate_chase_atr(
    current_price: float,
    entry_signal_price: float,
    atr: float,
    direction: str
) -> float:
    """
    ⚠️ DEPRECATED: Use market_analysis.calculate_chase_atr instead.
    
    Calculate how many ATRs price has moved since signal.
    """
    import warnings
    warnings.warn(
        "calculate_chase_atr in regime_detector is deprecated. "
        "Use src.application.live.market_analysis.calculate_chase_atr instead.",
        DeprecationWarning,
        stacklevel=2
    )
    if atr <= 0:
        return 0.0
    
    if direction == 'LONG':
        chase_distance = current_price - entry_signal_price
    else:
        chase_distance = entry_signal_price - current_price
    
    return chase_distance / atr
