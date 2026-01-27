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
    
    # R-Lock thresholds
    # FIX (Jan 22, 2026): Punto 6 - Retrasar BE lock para no matar trades prematuramente
    # Old: BE at 0.8R killed winners during normal pullbacks
    # New: BE at 1.2R (after TP1), let trade breathe
    R_LOCK_TIERS = [
        (1.2, 0.0, 1),    # (MFE threshold, lock at R, tier) - BE after TP1 hit
        (1.8, 0.6, 2),    # Lock 0.6R at 1.8R MFE (was 1.0→0.4)
        (2.5, 1.2, 3),    # Lock 1.2R at 2.5R MFE (was 1.5→0.9)
        (3.5, 2.0, 4),    # Lock 2.0R at 3.5R MFE (was 2.2→1.6)
        (5.0, 3.5, 5),    # Lock 3.5R at 5.0R MFE (for runners)
    ]
    
    # ATR K factors by regime
    # NOTE: Adjusted per review (Jan 27, 2026):
    # PROBLEM: Trade captured MFE 0.68R but closed at 0.29R - giving back 57% of profit!
    # ROOT CAUSE: K factors too loose, trailing gives back too much during consolidation
    # 
    # NEW VALUES (tighter to capture more MFE):
    # - RANGE: 1.5 (was 2.0 - range needs tighter trailing)
    # - TREND: 2.0 (was 2.5 - even trends don't need 2.5 ATR room)
    # - VOLATILITY: 2.8 (was 3.5 - still give room but not excessive)
    # - CONSERVATIVE: 1.8 (was 2.2 - conservative = tighter trailing)
    K_FACTORS = {
        TrailingRegime.TREND: 2.0,
        TrailingRegime.RANGE: 1.5,
        TrailingRegime.VOLATILITY: 2.8,
        TrailingRegime.CONSERVATIVE: 1.8,
    }
    
    # Swing reference lookback (candles)
    SWING_LOOKBACK = 10
    
    # Minimum stop movement (to avoid micro-adjustments)
    MIN_STOP_MOVE_ATR_MULT = 0.1  # Only move if > 0.1 ATR improvement
    
    # CRITICAL: Only activate ATR trailing after this MFE threshold
    # This prevents killing winners too early
    ATR_TRAIL_ACTIVATION_MFE = 0.3  # Only start ATR trailing after +0.3R
    
    def __init__(self):
        self._state = TrailingState()
        self._entry_price: float = 0.0
        self._initial_stop: float = 0.0
        self._direction: str = "LONG"
        self._is_active: bool = False
        # 🔥 NEW (Jan 26): Track entry time for time_to_02r calculation
        self._entry_time: datetime | None = None
    
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
    
    def get_r_locked_stop(self, mfe_r: float) -> Tuple[float, int]:
        """
        Get the R-locked stop level based on MFE.
        
        Returns:
            (stop_level, tier)
        """
        risk = abs(self._entry_price - self._initial_stop)
        locked_stop = self._initial_stop
        tier = 0
        
        for mfe_threshold, lock_r, lock_tier in self.R_LOCK_TIERS:
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
        k = self.K_FACTORS.get(regime, 2.5)
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
        
        # 1. Get R-locked stop
        r_stop, r_tier = self.get_r_locked_stop(mfe_r)
        
        # Only update if improved
        if self._direction == "LONG":
            if r_stop > self._state.r_locked_level:
                self._state.r_locked_level = r_stop
                self._state.r_lock_tier = r_tier
                logger.info(f"📈 R-LOCK upgraded: Tier {r_tier} → SL={r_stop:.2f} (MFE={mfe_r:.2f}R)")
        else:
            if r_stop < self._state.r_locked_level:
                self._state.r_locked_level = r_stop
                self._state.r_lock_tier = r_tier
                logger.info(f"📉 R-LOCK upgraded: Tier {r_tier} → SL={r_stop:.2f} (MFE={mfe_r:.2f}R)")
        
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
                logger.info(f"🎯 FINAL STOP updated: {old_stop:.2f} → {final:.2f} ({source})")
        else:
            if final < self._state.final_stop:
                old_stop = self._state.final_stop
                self._state.final_stop = final
                self._state.stop_source = source
                logger.info(f"🎯 FINAL STOP updated: {old_stop:.2f} → {final:.2f} ({source})")
        
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
                          regime_str: str = "TREND") -> HybridATRTrailing:
    """
    Factory to create and initialize a hybrid trailing stop.
    
    Args:
        entry_price: Entry price
        initial_stop: Initial stop loss
        direction: 'LONG' or 'SHORT'
        regime_str: Regime string ('TREND', 'RANGE', 'VOLATILITY')
    
    Returns:
        Initialized HybridATRTrailing instance
    """
    try:
        regime = TrailingRegime(regime_str.upper())
    except ValueError:
        regime = TrailingRegime.TREND
    
    trailing = HybridATRTrailing()
    trailing.initialize(entry_price, initial_stop, direction, regime)
    
    return trailing
