"""
Hybrid ATR Trailing Stop - Combines R-stepped locks with ATR-adaptive trailing

APPROACH:
1. Keep R-stepped locks for guaranteed protection (BE, +0.4R, +0.9R, etc.)
2. Add ATR-adaptive trailing that adjusts based on regime:
   - TREND: k=2.5 ATR (give room for pullbacks)
   - RANGE: k=1.5 ATR (tighter trailing)
   - VOLATILITY: k=3.0 ATR (wider for spikes)

FORMULA:
   For LONG: trail_stop = max(locked_stop, highest(high, N) - k * ATR)
   For SHORT: trail_stop = min(locked_stop, lowest(low, N) + k * ATR)

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TrailingRegime(Enum):
    """Regime for trailing stop behavior"""
    TREND = "TREND"
    RANGE = "RANGE"
    VOLATILITY = "VOLATILITY"
    CONSERVATIVE = "CONSERVATIVE"


@dataclass
class TrailingState:
    """Current state of the hybrid trailing stop"""
    # R-based locks
    r_locked_level: float = 0.0        # Current R-based lock level
    r_lock_tier: int = 0               # Which tier (0=none, 1=BE, 2=+0.4R, etc.)
    
    # ATR-based trailing
    atr_trail_stop: float = 0.0        # ATR-calculated trailing stop
    atr_k_factor: float = 2.5          # Current k factor
    swing_reference: float = 0.0       # Highest high / lowest low reference
    
    # Final stop
    final_stop: float = 0.0            # The actual stop to use
    stop_source: str = "INITIAL"       # "R_LOCK" or "ATR_TRAIL"
    
    # Context
    regime: str = "TREND"
    atr: float = 0.0
    mfe_r: float = 0.0                 # Maximum favorable excursion in R
    
    # =========================================================================
    # 🔥 NEW (Jan 26, 2026): TIME TO +0.2R TRACKING
    # 
    # RATIONALE: Winners reach +0.2R fast, losers never do.
    # This metric helps distinguish:
    # - Good entries: time_to_02r < 60s → edge confirmed
    # - Bad entries: time_to_02r > 300s or never → cut early next time
    # 
    # Actionable: If time_to_02r is consistently >5min, entry quality is poor.
    # =========================================================================
    time_to_02r_sec: float = -1.0      # Seconds to reach +0.2R (-1 = not reached)
    reached_02r_at: datetime | None = None  # Timestamp when +0.2R was first reached
    
    def to_dict(self) -> dict:
        return {
            'r_locked_level': round(self.r_locked_level, 2),
            'r_lock_tier': self.r_lock_tier,
            'atr_trail_stop': round(self.atr_trail_stop, 2),
            'atr_k_factor': self.atr_k_factor,
            'swing_reference': round(self.swing_reference, 2),
            'final_stop': round(self.final_stop, 2),
            'stop_source': self.stop_source,
            'regime': self.regime,
            'mfe_r': round(self.mfe_r, 2),
            'time_to_02r_sec': round(self.time_to_02r_sec, 1) if self.time_to_02r_sec >= 0 else -1,
        }


class HybridATRTrailing:
    """
    Hybrid trailing stop manager combining R-locks with ATR-adaptive trailing.
    
    R-LOCK TIERS (from existing system):
    - MFE >= 0.8R → Lock at breakeven
    - MFE >= 1.0R → Lock at +0.4R
    - MFE >= 1.5R → Lock at +0.9R
    - MFE >= 2.2R → Lock at +1.6R
    
    ATR-TRAIL:
    - Calculated as: swing_high - k*ATR (for LONG)
    - k depends on regime: TREND=2.5, RANGE=1.5, VOLATILITY=3.0
    - Only moves stop UP (never down)
    
    FINAL STOP:
    - max(r_locked_stop, atr_trail_stop) for LONG
    - min(r_locked_stop, atr_trail_stop) for SHORT
    """
    
    # R-Lock tiers by regime
    # TREND: More lenient (pullbacks of 0.3-0.5R are normal in BTC)
    # RANGE: Tighter protection (RANGE oscillation rarely exceeds 0.5R)
    # Both start at 0.3R MFE to prevent trades from reversing all gains.
    R_LOCK_TIERS_RANGE = [
        (0.3, -0.10, 0),  # +0.3R MFE → max loss -0.10R (protects sub-0.5R reversal)
        (0.5, -0.05, 1),  # +0.5R MFE → near breakeven
        (0.8,  0.05, 2),  # +0.8R → small profit locked
        (1.0,  0.20, 3),  # +1.0R → lock 0.20R
        (1.2,  0.40, 4),  # +1.2R → lock 0.40R
        (1.5,  0.70, 5),  # +1.5R → lock 0.70R
        (1.8,  1.00, 6),  # +1.8R → lock 1.00R
        (2.2,  1.40, 7),  # +2.2R → lock 1.40R
    ]
    
    R_LOCK_TIERS_TREND = [
        (0.3, -0.20, 0),  # +0.3R MFE → max loss -0.20R (protects sub-0.5R reversal)
        (0.5, -0.10, 1),  # +0.5R → max loss -0.10R
        (0.8,  0.05, 2),  # +0.8R → small profit locked
        (1.0,  0.20, 3),  # +1.0R → lock 0.20R
        (1.2,  0.50, 4),  # +1.2R → lock 0.50R
        (1.5,  0.90, 5),  # +1.5R → lock 0.90R
        (1.8,  1.20, 6),  # +1.8R → lock 1.20R
        (2.2,  1.60, 7),  # +2.2R → lock 1.60R
        (3.0,  2.20, 8),  # +3.0R → lock 2.20R (runners)
    ]
    
    # Fallback (usado si régimen no está definido)
    R_LOCK_TIERS = R_LOCK_TIERS_RANGE  # Default conservador
    
    # ATR K factors by regime
    # 🔥 FIX (Feb 2, 2026): K FACTORS MÁS AGRESIVOS
    # PROBLEMA: Con K=2.0 ATR, se devuelve 60%+ del MFE
    # DATOS: 9 trades TRAILING_STOP solo capturaron 37% del MFE en promedio
    # 
    # SOLUCIÓN: K factors más ajustados para capturar más profit
    # - TREND: 1.5 ATR (was 2.0 - pullbacks normales son ~1 ATR)
    # - RANGE: 1.3 ATR (needs some room even in range)
    # - VOLATILITY: 2.0 ATR (solo volatility merece espacio)
    # - CONSERVATIVE: 1.2 ATR (was 1.8 - conservative = protect profit)
    K_FACTORS = {
        TrailingRegime.TREND: 1.5,
        TrailingRegime.RANGE: 1.0,       # Tighter: RANGE moves are limited (was 1.3)
        TrailingRegime.VOLATILITY: 2.0,
        TrailingRegime.CONSERVATIVE: 1.2,
    }
    
    # Swing reference lookback (candles)
    SWING_LOOKBACK = 10
    
    # Minimum stop movement (to avoid micro-adjustments)
    MIN_STOP_MOVE_ATR_MULT = 0.1  # Only move if > 0.1 ATR improvement
    
    # CRITICAL: Only activate ATR trailing after this MFE threshold
    # This prevents killing winners too early
    # 🔥 FIX #243 (Feb 16, 2026): Increased from 0.2 to 0.4
    # BTC pullbacks of 0.2-0.3R are normal. Starting ATR trail at +0.2R MFE
    # caused trades to be stopped on normal noise. 0.4R gives room.
    ATR_TRAIL_ACTIVATION_MFE = 0.4  # Start ATR trailing at +0.4R (was 0.2)
    
    # =========================================================================
    # 🔥 BOS QUALITY DYNAMIC TRAILING (Feb 3, 2026)
    # =========================================================================
    # FILOSOFÍA: La calidad de ENTRADA predice probabilidad de runner
    # - BOS STRONG → trailing más suelto → más chance de capturar runner
    # - BOS NORMAL → trailing estándar
    # - BOS WEAK → trailing más apretado → proteger rápido
    # 
    # ESTO NO ES SOBREPROTECCIÓN porque:
    # - NO rechaza trades (todos entran igual)
    # - Solo ajusta GESTIÓN post-entrada
    # - BOS STRONG te da MÁS espacio, no menos
    BOS_QUALITY_K_MULT = {
        'STRONG': 1.15,  # +15% K = más espacio para runners
        'NORMAL': 1.0,   # K estándar
        'WEAK': 0.90,    # -10% K = protege profit más rápido
        'UNKNOWN': 1.0,  # Default to normal
    }
    
    def __init__(self, bos_quality: str = 'NORMAL'):
        self._state = TrailingState()
        self._entry_price: float = 0.0
        self._initial_stop: float = 0.0
        self._direction: str = "LONG"
        self._is_active: bool = False
        # 🔥 NEW (Jan 26): Track entry time for time_to_02r calculation
        self._entry_time: datetime | None = None
        # 🔥 NEW (Feb 3): BOS Quality affects trailing tightness
        self._bos_quality = bos_quality.upper() if bos_quality else 'NORMAL'
    
    def initialize(self,
                   entry_price: float,
                   initial_stop: float,
                   direction: str,
                   initial_regime: TrailingRegime = TrailingRegime.TREND):
        """
        Initialize trailing for a new position.
        """
        self._entry_price = entry_price
        self._initial_stop = initial_stop
        self._direction = direction.upper()
        self._is_active = True
        # 🔥 NEW (Jan 26): Record entry time for time_to_02r
        self._entry_time = datetime.now()
        
        self._state = TrailingState(
            r_locked_level=initial_stop,
            r_lock_tier=0,
            atr_trail_stop=initial_stop,
            atr_k_factor=self.K_FACTORS.get(initial_regime, 2.5),
            swing_reference=entry_price,
            final_stop=initial_stop,
            stop_source="INITIAL",
            regime=initial_regime.value,
            # 🔥 NEW (Jan 26): Initialize time_to_02r tracking
            time_to_02r_sec=-1.0,
            reached_02r_at=None,
        )
        
        logger.info(f"🎯 Hybrid trailing initialized: {direction} @ {entry_price:.2f}, SL={initial_stop:.2f}, regime={initial_regime.value}")
    
    def calculate_mfe_r(self, current_price: float) -> float:
        """Calculate Maximum Favorable Excursion in R terms"""
        if not self._is_active:
            return 0.0
        
        risk = abs(self._entry_price - self._initial_stop)
        if risk <= 0:
            return 0.0
        
        if self._direction == "LONG":
            favorable = current_price - self._entry_price
        else:
            favorable = self._entry_price - current_price
        
        return favorable / risk
    
    def get_r_locked_stop(self, mfe_r: float, regime: TrailingRegime = None) -> Tuple[float, int]:
        """
        Get the R-locked stop level based on MFE and regime.
        
        🔥 FIX #64 (Feb 6, 2026): Use different tiers by regime
        - TREND: More lenient tiers (pullbacks of 0.3-0.5R are normal)
        - RANGE/SCALP: Tighter tiers (protect profits faster)
        
        Returns:
            (stop_level, tier)
        """
        risk = abs(self._entry_price - self._initial_stop)
        locked_stop = self._initial_stop
        tier = 0
        
        # 🔥 FIX #64: Select tiers based on regime
        if regime == TrailingRegime.TREND:
            tiers = self.R_LOCK_TIERS_TREND
        else:
            tiers = self.R_LOCK_TIERS_RANGE  # RANGE, VOLATILITY, CONSERVATIVE
        
        for mfe_threshold, lock_r, lock_tier in tiers:
            if mfe_r >= mfe_threshold:
                if self._direction == "LONG":
                    locked_stop = self._entry_price + (lock_r * risk)
                else:
                    locked_stop = self._entry_price - (lock_r * risk)
                tier = lock_tier
        
        return locked_stop, tier
    
    def get_atr_trail_stop(self,
                           highs: np.ndarray,
                           lows: np.ndarray,
                           atr: float,
                           regime: TrailingRegime) -> Tuple[float, float]:
        """
        Calculate ATR-based trailing stop.
        
        Returns:
            (trail_stop, swing_reference)
        """
        # Base K from regime
        k_base = self.K_FACTORS.get(regime, 2.5)
        
        # 🔥 Apply BOS quality multiplier
        # STRONG entry → more room to run (K * 1.15)
        # WEAK entry → tighter protection (K * 0.90)
        bos_mult = self.BOS_QUALITY_K_MULT.get(self._bos_quality, 1.0)
        k = k_base * bos_mult
        
        lookback = min(self.SWING_LOOKBACK, len(highs))
        
        if self._direction == "LONG":
            # Swing high as reference
            swing_ref = np.max(highs[-lookback:])
            trail_stop = swing_ref - (k * atr)
        else:
            # Swing low as reference
            swing_ref = np.min(lows[-lookback:])
            trail_stop = swing_ref + (k * atr)
        
        return trail_stop, swing_ref
    
    def update(self,
               current_price: float,
               highs: np.ndarray,
               lows: np.ndarray,
               atr: float,
               regime: TrailingRegime | None = None) -> TrailingState:
        """
        Update trailing stop based on current conditions.
        
        Args:
            current_price: Current price
            highs: Array of recent highs
            lows: Array of recent lows
            atr: Current ATR
            regime: Optional regime override
        
        Returns:
            Updated TrailingState
        """
        if not self._is_active:
            return self._state
        
        regime = regime or TrailingRegime(self._state.regime)
        
        # Calculate MFE
        mfe_r = self.calculate_mfe_r(current_price)
        self._state.mfe_r = mfe_r
        self._state.atr = atr
        
        # =========================================================================
        # 🔥 NEW (Jan 26, 2026): TIME TO +0.2R TRACKING
        # Record when MFE first crosses +0.2R threshold
        # This metric helps distinguish good vs bad entries without lookahead
        # =========================================================================
        if mfe_r >= 0.2 and self._state.time_to_02r_sec < 0 and self._entry_time:
            now = datetime.now()
            self._state.reached_02r_at = now
            self._state.time_to_02r_sec = (now - self._entry_time).total_seconds()
            logger.info(f"⏱️ TIME TO +0.2R: {self._state.time_to_02r_sec:.1f}s (MFE={mfe_r:.2f}R)")
        
        # 1. Get R-locked stop (🔥 FIX #64: Use regime-specific tiers)
        r_stop, r_tier = self.get_r_locked_stop(mfe_r, regime)
        
        # Only update if improved
        if self._direction == "LONG":
            if r_stop > self._state.r_locked_level:
                self._state.r_locked_level = r_stop
                self._state.r_lock_tier = r_tier
                logger.debug(f"📈 R-LOCK upgraded: Tier {r_tier} → SL={r_stop:.2f} (MFE={mfe_r:.2f}R)")
        else:
            if r_stop < self._state.r_locked_level:
                self._state.r_locked_level = r_stop
                self._state.r_lock_tier = r_tier
                logger.debug(f"📉 R-LOCK upgraded: Tier {r_tier} → SL={r_stop:.2f} (MFE={mfe_r:.2f}R)")
        
        # 2. Get ATR trailing stop
        # CRITICAL: Only activate ATR trailing after MFE threshold
        # This prevents killing winners too early on imperfect entries
        atr_trail_active = mfe_r >= self.ATR_TRAIL_ACTIVATION_MFE
        
        if atr_trail_active and len(highs) >= 3 and atr > 0:
            atr_stop, swing_ref = self.get_atr_trail_stop(highs, lows, atr, regime)
            self._state.swing_reference = swing_ref
            self._state.atr_k_factor = self.K_FACTORS.get(regime, 2.8)
            
            # Only update if improved and significant
            min_move = atr * self.MIN_STOP_MOVE_ATR_MULT
            
            if self._direction == "LONG":
                if atr_stop > self._state.atr_trail_stop + min_move:
                    self._state.atr_trail_stop = atr_stop
                    logger.debug(f"📊 ATR-TRAIL updated: {atr_stop:.2f} (k={self._state.atr_k_factor}, swing={swing_ref:.2f})")
            else:
                if atr_stop < self._state.atr_trail_stop - min_move:
                    self._state.atr_trail_stop = atr_stop
                    logger.debug(f"📊 ATR-TRAIL updated: {atr_stop:.2f} (k={self._state.atr_k_factor}, swing={swing_ref:.2f})")
        elif not atr_trail_active and len(highs) >= 3:
            # Before activation threshold, just track swing reference without trailing
            lookback = min(self.SWING_LOOKBACK, len(highs))
            if self._direction == "LONG":
                self._state.swing_reference = float(np.max(highs[-lookback:]))
            else:
                self._state.swing_reference = float(np.min(lows[-lookback:]))
        
        # 3. Final stop = best of both
        if self._direction == "LONG":
            final = max(self._state.r_locked_level, self._state.atr_trail_stop)
            source = "R_LOCK" if final == self._state.r_locked_level else "ATR_TRAIL"
        else:
            final = min(self._state.r_locked_level, self._state.atr_trail_stop)
            source = "R_LOCK" if final == self._state.r_locked_level else "ATR_TRAIL"
        
        # Only update if improved
        if self._direction == "LONG":
            if final > self._state.final_stop:
                old_stop = self._state.final_stop
                self._state.final_stop = final
                self._state.stop_source = source
                logger.debug(f"🎯 FINAL STOP updated: {old_stop:.2f} → {final:.2f} ({source})")
        else:
            if final < self._state.final_stop:
                old_stop = self._state.final_stop
                self._state.final_stop = final
                self._state.stop_source = source
                logger.debug(f"🎯 FINAL STOP updated: {old_stop:.2f} → {final:.2f} ({source})")
        
        self._state.regime = regime.value
        return self._state
    
    def get_stop(self) -> float:
        """Get current trailing stop level"""
        return self._state.final_stop
    
    def get_state(self) -> TrailingState:
        """Get current state"""
        return self._state
    
    def is_active(self) -> bool:
        """Check if trailing is active"""
        return self._is_active
    
    def close(self):
        """Close trailing (position closed)"""
        self._is_active = False
        logger.info(f"🏁 Hybrid trailing closed. Final state: {self._state.to_dict()}")
    
    @classmethod
    def calculate_optimal_k(cls, 
                            recent_atr_ratios: np.ndarray,
                            base_k: float = 2.5) -> float:
        """
        Calculate optimal k factor based on recent volatility profile.
        
        If volatility is expanding: increase k (give more room)
        If volatility is contracting: decrease k (tighten)
        
        Args:
            recent_atr_ratios: Array of ATR/price ratios for recent candles
            base_k: Base k factor
        
        Returns:
            Adjusted k factor
        """
        if len(recent_atr_ratios) < 5:
            return base_k
        
        # Check if volatility is expanding or contracting
        recent = np.mean(recent_atr_ratios[-3:])
        older = np.mean(recent_atr_ratios[-6:-3])
        
        if older <= 0:
            return base_k
        
        vol_change = (recent - older) / older
        
        # Adjust k: -20% to +30% based on vol change
        adjustment = np.clip(vol_change * 1.5, -0.2, 0.3)
        
        return float(base_k * (1 + adjustment))


# Factory function for integration with risk_engine
def create_hybrid_trailing(entry_price: float,
                          initial_stop: float,
                          direction: str,
                          regime_str: str = "TREND",
                          bos_quality: str = "NORMAL") -> HybridATRTrailing:
    """
    Factory to create and initialize a hybrid trailing stop.
    
    Args:
        entry_price: Entry price
        initial_stop: Initial stop loss
        direction: 'LONG' or 'SHORT'
        regime_str: Regime string ('TREND', 'RANGE', 'VOLATILITY')
        bos_quality: BOS quality from entry ('STRONG', 'NORMAL', 'WEAK')
    
    Returns:
        Initialized HybridATRTrailing instance
    """
    try:
        regime = TrailingRegime(regime_str.upper())
    except ValueError:
        regime = TrailingRegime.TREND
    
    trailing = HybridATRTrailing(bos_quality=bos_quality)
    trailing.initialize(entry_price, initial_stop, direction, regime)
    
    return trailing
