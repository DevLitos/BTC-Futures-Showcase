"""
Direction State Manager - Extracted from trading_app.py (Feb 04, 2026)

Manages all directional state:
- EXPANSION_MODE: Strong displacement has been detected
- DIRECTION_LOCK: Prevents bias flips during expansion
- FORBIDDEN_DIRECTION: Direction blocked by invalidation
- STRUCTURAL_BIAS: HTF bias (informative, not blocking)
- ACTIVE_DIRECTION: What direction to trade NOW

HIERARCHY:
EXPANSION > DISPLACEMENT > MICRO_MOMENTUM > STRUCTURAL_BIAS

All state changes go through this manager for proper logging and consistency.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class DirectionState:
    """Immutable snapshot of current directional state."""
    structural_bias: Optional[str] = None  # HTF bias ('LONG', 'SHORT', None)
    active_direction: Optional[str] = None  # What to trade now
    forbidden_direction: Optional[str] = None  # Blocked direction
    
    # Expansion mode state
    expansion_mode: bool = False
    expansion_direction: Optional[str] = None
    expansion_start_time: Optional[datetime] = None
    expansion_initial_displacement: float = 0.0
    expansion_timeout_minutes: int = 45
    
    # Direction lock state
    direction_lock: Optional[str] = None
    direction_lock_reason: Optional[str] = None
    direction_lock_expires_at: Optional[datetime] = None
    direction_lock_initial_move: float = 0.0
    direction_lock_timeout_minutes: int = 30
    direction_lock_start_time: Optional[datetime] = None  # FEB06: Para time decay
    
    # Displacement state
    displacement_bypass: bool = False
    strong_displacement: bool = False
    
    # Price tracking
    bias_high_since: float = 0.0
    bias_low_since: float = 0.0
    bias_established_price: float = 0.0
    bias_established_time: Optional[datetime] = None
    
    # Invalidation tracking
    bias_invalidation_level: float = 0.0
    bias_before_invalidation: Optional[str] = None
    forbidden_set_at_tick: int = 0
    
    # Displacement memory
    displacement_memory_direction: Optional[str] = None
    displacement_memory_amount: float = 0.0
    displacement_memory_expires_at: Optional[datetime] = None


@dataclass
class AdaptiveParams:
    """Adaptive parameters based on volatility."""
    volatility_ratio: float = 1.0
    expansion_timeout_minutes: int = 20
    direction_lock_timeout_minutes: int = 15
    # FEB06: Bajado de 3.0 a 1.5 - era demasiado conservador
    # 3.0x mataba oportunidades intraday (ej: $174 inicial → necesitaba $522 para invalidar)
    asymmetric_invalidation_mult: float = 1.5
    displacement_atr_mult: float = 2.8
    strong_displacement_mult: float = 1.5
    
    # FEB06: Nuevos parámetros para invalidación inteligente
    lock_time_decay_half_life_min: float = 8.0  # Lock pierde 50% fuerza cada 8min
    momentum_override_threshold: float = 0.6  # Si opuesto > 60% inicial + momentum alto → invalidar
    
    # For debugging
    vol_displacement_adj: float = 1.0
    invalidation_reason: str = 'NORMAL'


class DirectionStateManager:
    """
    Manages all directional state for trading decisions.
    
    This class was extracted from trading_app.py to reduce god-class complexity
    and provide a single source of truth for direction-related state.
    """
    
    # Configuration constants
    EXPANSION_TIMEOUT_BASE: int = 20  # Base timeout in minutes
    DIRECTION_LOCK_TIMEOUT_BASE: int = 15  # Base timeout in minutes
    ASYMMETRIC_INVALIDATION_BASE: float = 3.0  # 3x evidence to invalidate
    DISPLACEMENT_MEMORY_WINDOW_MINUTES: int = 10  # Memory window
    FORBIDDEN_MIN_DURATION_TICKS: int = 120  # Hysteresis for forbidden
    
    # Threshold constants
    BASE_ATR_MULT_INVALIDATION: float = 1.5
    MIN_INVALIDATION_THRESHOLD: float = 50.0
    MAX_INVALIDATION_THRESHOLD: float = 250.0
    BASE_DISPLACEMENT_ATR_MULT: float = 2.8
    STRONG_DISPLACEMENT_FACTOR: float = 1.5
    
    def __init__(self):
        """Initialize state manager with default state."""
        self._state = DirectionState()
        self._params = AdaptiveParams()
        self._tick_count = 0
        self._trade_tags: Dict[str, Any] = {}
        
    @property
    def state(self) -> DirectionState:
        """Get current direction state."""
        return self._state
    
    @property
    def params(self) -> AdaptiveParams:
        """Get current adaptive parameters."""
        return self._params
    
    @property
    def trade_tags(self) -> Dict[str, Any]:
        """Get trade tags generated during state updates."""
        return self._trade_tags
    
    def reset_trade_tags(self):
        """Reset trade tags for new evaluation."""
        self._trade_tags = {}
    
    def update_tick_count(self, tick_count: int):
        """Update tick count for logging throttling."""
        self._tick_count = tick_count
    
    # =========================================================================
    # ADAPTIVE PARAMETERS
    # =========================================================================
    
    def calculate_adaptive_params(
        self,
        atr_5m: float,
        atr_5m_avg: float,
        current_htf_adx: float = 20.0
    ) -> AdaptiveParams:
        """
        Calculate adaptive parameters based on volatility.
        
        Volatility ratio = ATR_actual / ATR_promedio
        - ratio > 2.0: Fast markets → shorter timeouts
        - ratio 1.0-2.0: Normal → base timeouts
        - ratio < 0.5: Slow chop → longer timeouts
        """
        vol_ratio = atr_5m / atr_5m_avg if atr_5m_avg > 0 else 1.0
        self._params.volatility_ratio = vol_ratio
        
        # Timeout multiplier (inverse of volatility)
        vol_timeout_mult = 1.0 / max(0.5, min(2.0, vol_ratio))
        
        # Expansion timeout: 10-40 min
        exp_timeout = int(self.EXPANSION_TIMEOUT_BASE * vol_timeout_mult)
        self._params.expansion_timeout_minutes = max(10, min(40, exp_timeout))
        
        # Direction lock timeout: 5-30 min
        lock_timeout = int(self.DIRECTION_LOCK_TIMEOUT_BASE * vol_timeout_mult)
        self._params.direction_lock_timeout_minutes = max(5, min(30, lock_timeout))
        
        # Asymmetric invalidation multiplier
        # 🔄 REFACTORED Feb 04: Use ADXThresholds for consistent threshold
        from src.config.trading_config import ADXThresholds
        regime_mult = 1.0
        if self._state.expansion_mode:
            regime_mult = 1.25
            self._params.invalidation_reason = 'EXPANSION_MODE'
        elif ADXThresholds.is_ranging(current_htf_adx):
            regime_mult = 0.75
            self._params.invalidation_reason = 'RANGE'
        else:
            self._params.invalidation_reason = 'NORMAL'
        
        vol_inv_adj = min(0.3, max(-0.3, (vol_ratio - 1.0) * 0.15))
        inv_mult = self.ASYMMETRIC_INVALIDATION_BASE * regime_mult + vol_inv_adj
        self._params.asymmetric_invalidation_mult = max(1.2, min(3.0, inv_mult))
        
        # Displacement thresholds
        vol_disp_adj = 1.0 + (vol_ratio - 1.0) * 0.3
        vol_disp_adj = max(0.7, min(1.5, vol_disp_adj))
        self._params.vol_displacement_adj = vol_disp_adj
        self._params.displacement_atr_mult = self.BASE_DISPLACEMENT_ATR_MULT * vol_disp_adj
        self._params.strong_displacement_mult = self.STRONG_DISPLACEMENT_FACTOR * vol_disp_adj
        
        # Update state with new timeout values
        self._state.expansion_timeout_minutes = self._params.expansion_timeout_minutes
        self._state.direction_lock_timeout_minutes = self._params.direction_lock_timeout_minutes
        
        # Log periodically
        if self._tick_count % 60 == 1:
            logger.info(
                f"🌊 ADAPTIVE PARAMS: vol_ratio={vol_ratio:.2f} | "
                f"exp_timeout={self._params.expansion_timeout_minutes}min | "
                f"lock_timeout={self._params.direction_lock_timeout_minutes}min | "
                f"inv_mult={self._params.asymmetric_invalidation_mult:.2f}x"
            )
        
        # Add to trade tags
        self._trade_tags['volatility_ratio'] = vol_ratio
        self._trade_tags['adaptive_exp_timeout'] = self._params.expansion_timeout_minutes
        self._trade_tags['adaptive_inv_mult'] = self._params.asymmetric_invalidation_mult
        
        return self._params
    
    # =========================================================================
    # PRICE TRACKING
    # =========================================================================
    
    def initialize_price_tracking(self, current_price: float, market_time: datetime):
        """Initialize price tracking if not already done."""
        if self._state.bias_high_since == 0.0:
            self._state.bias_high_since = current_price
            self._state.bias_low_since = current_price
            self._state.bias_established_price = current_price
            self._state.bias_established_time = market_time
            logger.debug(f"📍 BIAS TRACKING INITIALIZED: high/low = ${current_price:.0f}")
    
    def update_price_tracking(self, current_price: float):
        """Update high/low tracking."""
        if self._state.bias_high_since > 0:
            self._state.bias_high_since = max(self._state.bias_high_since, current_price)
            self._state.bias_low_since = min(self._state.bias_low_since, current_price)
    
    def reset_price_tracking(self, current_price: float, market_time: datetime):
        """Reset price tracking on bias change."""
        self._state.bias_established_price = current_price
        self._state.bias_established_time = market_time
        self._state.bias_high_since = current_price
        self._state.bias_low_since = current_price
    
    def get_price_displacement(self, current_price: float) -> tuple[float, float]:
        """
        Get displacement from high and low since bias established.
        
        Returns:
            (drop_from_high, rise_from_low)
        """
        drop = self._state.bias_high_since - current_price
        rise = current_price - self._state.bias_low_since
        return drop, rise
    
    # =========================================================================
    # DIRECTION LOCK MANAGEMENT
    # =========================================================================
    
    def check_direction_lock_timeout(self, current_time: datetime) -> bool:
        """
        Check if direction lock has timed out.
        
        Returns True if lock expired and was cleared.
        """
        if not self._state.direction_lock or not self._state.direction_lock_expires_at:
            return False
        
        if current_time >= self._state.direction_lock_expires_at:
            # Check if expansion mode is still active - extend lock if so
            if (self._state.expansion_mode and 
                self._state.expansion_direction and 
                self._state.expansion_direction == self._state.direction_lock):
                # Extend lock by 15 min
                self._state.direction_lock_expires_at = current_time + timedelta(minutes=15)
                logger.info(
                    f"⏰ DIRECTION_LOCK EXTENDED: {self._state.direction_lock} | "
                    f"EXPANSION_MODE still active - extending lock 15min"
                )
                self._trade_tags['direction_lock_extended'] = True
                return False
            else:
                # Lock expired
                logger.warning(
                    f"⏰ DIRECTION_LOCK EXPIRED: {self._state.direction_lock} | "
                    f"Reason: {self._state.direction_lock_reason} | "
                    f"Initial move: ${self._state.direction_lock_initial_move:.0f}"
                )
                self._clear_direction_lock()
                self._trade_tags['direction_lock_expired'] = True
                return True
        
        return False
    
    def check_direction_lock_invalidation(
        self,
        drop_from_high: float,
        rise_from_low: float,
        atr_5m: float = 0.0,
        adx: float = 0.0,
        vol_percentile: float = 50.0
    ) -> bool:
        """
        Check if direction lock should be invalidated by opposite movement.
        
        FEB06 REFACTOR: Ahora con time decay y momentum override.
        - Time decay: Lock pierde fuerza con half-life de 8min
        - Momentum override: Si opuesto > 60% inicial + ADX alto + vol alta → invalidar
        - Multiplicador base: 1.5x (antes era 3.0x)
        
        Returns True if lock was invalidated.
        """
        import math
        
        if not self._state.direction_lock or self._state.direction_lock_initial_move <= 0:
            return False
        
        opposite_move = 0.0
        if self._state.direction_lock == 'SHORT':
            opposite_move = rise_from_low
        elif self._state.direction_lock == 'LONG':
            opposite_move = drop_from_high
        
        initial_move = self._state.direction_lock_initial_move
        base_mult = self._params.asymmetric_invalidation_mult  # 1.5
        
        # =====================================================================
        # FEB06: TIME DECAY - Lock pierde fuerza con el tiempo
        # Half-life de 8min: después de 8min, lock_strength = 0.5
        # =====================================================================
        elapsed_min = 0.0
        lock_strength = 1.0
        if self._state.direction_lock_start_time:
            elapsed_min = (datetime.now() - self._state.direction_lock_start_time).total_seconds() / 60
            half_life = self._params.lock_time_decay_half_life_min  # 8.0
            # Decay exponencial: strength = 0.5^(elapsed/half_life)
            lock_strength = math.pow(0.5, elapsed_min / half_life)
        
        # El threshold efectivo = initial * mult * lock_strength
        # A medida que pasa el tiempo, es más fácil invalidar
        effective_mult = base_mult * lock_strength
        effective_mult = max(0.8, effective_mult)  # Floor: mínimo 0.8x
        
        threshold = initial_move * effective_mult
        
        # =====================================================================
        # FEB06: MOMENTUM OVERRIDE - Si hay impulso fuerte opuesto, invalidar antes
        # Condiciones: opuesto > 60% inicial + ADX > 25 + vol_percentile > 60
        # =====================================================================
        momentum_threshold = initial_move * self._params.momentum_override_threshold  # 0.6
        adx_high = adx > 25
        vol_high = vol_percentile > 60
        
        if opposite_move >= momentum_threshold and adx_high and vol_high:
            logger.warning(
                f"🔴 DIRECTION_LOCK INVALIDATED (MOMENTUM OVERRIDE): "
                f"Opposite ${opposite_move:.0f} ≥ {self._params.momentum_override_threshold:.0%} of "
                f"${initial_move:.0f} = ${momentum_threshold:.0f} | "
                f"ADX={adx:.1f}>25 ✓ | VOL%={vol_percentile:.0f}>60 ✓ | "
                f"Lock was {self._state.direction_lock} - NOW RELEASED"
            )
            self._clear_direction_lock()
            self._trade_tags['direction_lock_invalidated_momentum'] = True
            return True
        
        # =====================================================================
        # INVALIDACIÓN ESTÁNDAR (con time decay aplicado)
        # =====================================================================
        if opposite_move >= threshold:
            logger.warning(
                f"🔴 DIRECTION_LOCK INVALIDATED (ASYMMETRIC): "
                f"Opposite ${opposite_move:.0f} ≥ "
                f"effective {effective_mult:.2f}x (base {base_mult:.1f} × strength {lock_strength:.2f}) "
                f"× initial ${initial_move:.0f} = ${threshold:.0f} | "
                f"Elapsed: {elapsed_min:.1f}min | "
                f"Lock was {self._state.direction_lock} - NOW RELEASED"
            )
            self._clear_direction_lock()
            self._trade_tags['direction_lock_invalidated_asymmetric'] = True
            self._trade_tags['lock_strength_at_invalidation'] = lock_strength
            return True
        
        return False
    
    def activate_direction_lock(
        self,
        direction: str,
        reason: str,
        initial_move: float
    ):
        """Activate direction lock."""
        timeout = self._params.direction_lock_timeout_minutes
        self._state.direction_lock = direction
        self._state.direction_lock_reason = reason
        self._state.direction_lock_expires_at = datetime.now() + timedelta(minutes=timeout)
        self._state.direction_lock_initial_move = initial_move
        self._state.direction_lock_start_time = datetime.now()  # FEB06: Para time decay
        
        self._trade_tags['direction_lock'] = direction
        self._trade_tags['direction_lock_reason'] = reason
    
    def _clear_direction_lock(self):
        """Clear direction lock."""
        self._state.direction_lock = None
        self._state.direction_lock_reason = None
        self._state.direction_lock_expires_at = None
        self._state.direction_lock_initial_move = 0.0
        self._state.direction_lock_start_time = None  # FEB06
    
    # =========================================================================
    # EXPANSION MODE MANAGEMENT
    # =========================================================================
    
    def check_expansion_timeout(self) -> bool:
        """
        Check if expansion mode has timed out.
        
        Returns True if expansion expired and was cleared.
        """
        if not self._state.expansion_mode or not self._state.expansion_start_time:
            return False
        
        elapsed = (datetime.now() - self._state.expansion_start_time).total_seconds() / 60
        
        if elapsed >= self._state.expansion_timeout_minutes:
            logger.warning(
                f"⏰ EXPANSION_MODE TIMEOUT: {elapsed:.1f}min elapsed ≥ "
                f"{self._state.expansion_timeout_minutes}min | "
                f"Direction: {self._state.expansion_direction} | "
                f"Initial displacement: ${self._state.expansion_initial_displacement:.0f}"
            )
            self._clear_expansion_mode()
            self._trade_tags['expansion_timeout'] = True
            return True
        
        return False
    
    def check_expansion_invalidation(
        self,
        drop_from_high: float,
        rise_from_low: float,
        atr_5m: float,
        atr_1m: float
    ) -> bool:
        """
        Check if expansion mode should be invalidated by opposite movement.
        
        Uses adaptive asymmetric invalidation.
        Returns True if expansion was invalidated.
        """
        if not self._state.expansion_mode or self._state.expansion_initial_displacement <= 0:
            return False
        
        opposite_displacement = 0.0
        if self._state.expansion_direction == 'SHORT':
            opposite_displacement = rise_from_low
        elif self._state.expansion_direction == 'LONG':
            opposite_displacement = drop_from_high
        
        # Calculate adaptive multiplier
        adaptive_mult = self._params.asymmetric_invalidation_mult
        elapsed_min = 0.0
        price_decay = 0.0
        
        # Volatility adjustment
        vol_ratio = atr_5m / atr_1m if atr_1m > 0 else 1.0
        if vol_ratio > 2.0:
            adaptive_mult += 0.5
        elif vol_ratio < 1.2:
            adaptive_mult -= 0.5
        
        # Time decay
        if self._state.expansion_start_time:
            elapsed_min = (datetime.now() - self._state.expansion_start_time).total_seconds() / 60
            time_decay = min(0.5, elapsed_min * 0.005)
            adaptive_mult -= time_decay
        
        # Price-based decay
        if atr_5m > 0:
            opposite_atr_multiples = opposite_displacement / (atr_5m * 0.5)
            price_decay = min(1.0, opposite_atr_multiples * 0.3)
            adaptive_mult -= price_decay
        
        # Floor
        adaptive_mult = max(1.5, adaptive_mult)
        
        threshold = self._state.expansion_initial_displacement * adaptive_mult
        
        if opposite_displacement >= threshold:
            logger.warning(
                f"🔴 EXPANSION_MODE INVALIDATED (ASYMMETRIC): "
                f"Opposite ${opposite_displacement:.0f} ≥ "
                f"{adaptive_mult:.1f}x initial ${self._state.expansion_initial_displacement:.0f} "
                f"= ${threshold:.0f} | "
                f"(base={self._params.asymmetric_invalidation_mult:.1f}, vol={vol_ratio:.1f}, "
                f"time={elapsed_min:.0f}min, price_decay={price_decay:.2f}) | "
                f"Exiting {self._state.expansion_direction} expansion"
            )
            self._clear_expansion_mode()
            self._clear_direction_lock()
            self._trade_tags['expansion_invalidated_asymmetric'] = True
            self._trade_tags['invalidation_mult_used'] = adaptive_mult
            return True
        
        return False
    
    def activate_expansion_mode(
        self,
        direction: str,
        initial_displacement: float
    ):
        """Activate expansion mode."""
        self._state.expansion_mode = True
        self._state.expansion_direction = direction
        self._state.expansion_start_time = datetime.now()
        self._state.expansion_initial_displacement = initial_displacement
        
        self._trade_tags['expansion_mode_activated'] = True
        self._trade_tags['expansion_direction'] = direction
        
        logger.warning(
            f"🚀 EXPANSION_MODE ACTIVATED: {direction} | "
            f"Initial displacement ${initial_displacement:.0f} | "
            f"Timeout: {self._state.expansion_timeout_minutes}min | "
            f"INVALIDATION requires ${initial_displacement * self._params.asymmetric_invalidation_mult:.0f} opposite"
        )
    
    def _clear_expansion_mode(self):
        """Clear expansion mode."""
        self._state.expansion_mode = False
        self._state.expansion_direction = None
        self._state.expansion_start_time = None
        self._state.expansion_initial_displacement = 0.0
    
    # =========================================================================
    # FORBIDDEN DIRECTION MANAGEMENT
    # =========================================================================
    
    def set_forbidden_direction(
        self,
        direction: str,
        invalidation_level: float,
        tick_count: int
    ):
        """Set forbidden direction with hysteresis."""
        previous = self._state.forbidden_direction
        
        self._state.forbidden_direction = direction
        self._state.forbidden_set_at_tick = tick_count
        self._state.bias_invalidation_level = invalidation_level
        self._state.bias_before_invalidation = direction
        
        self._trade_tags['bias_invalidated'] = True
        self._trade_tags['forbidden_direction'] = direction
        self._trade_tags['reclaim_level'] = invalidation_level
        
        if previous != direction:
            logger.warning(
                f"⚠️ BIAS FLIP: {direction} invalidated → FORBID {direction} | "
                f"Reclaim level: ${invalidation_level:.0f}"
            )
    
    def clear_forbidden_if_allowed(self, tick_count: int) -> bool:
        """
        Clear forbidden direction if hysteresis allows.
        
        Returns True if forbidden was cleared.
        """
        if not self._state.forbidden_direction:
            return False
        
        ticks_since = tick_count - self._state.forbidden_set_at_tick
        
        if ticks_since >= self.FORBIDDEN_MIN_DURATION_TICKS:
            previous = self._state.forbidden_direction
            self._state.forbidden_direction = None
            logger.info(f"🔓 FORBIDDEN {previous} CLEARED after {ticks_since} ticks (hysteresis)")
            return True
        
        return False
    
    def is_direction_protected(self, direction: str) -> tuple[bool, str]:
        """
        Check if direction is protected by expansion or lock.
        
        Returns (is_protected, protection_reason)
        """
        if self._state.expansion_mode and self._state.expansion_direction == direction:
            return True, 'EXPANSION_MODE'
        
        if self._state.direction_lock == direction:
            return True, 'DIRECTION_LOCK'
        
        return False, ''
    
    # =========================================================================
    # BIAS RESTORATION
    # =========================================================================
    
    def check_bias_restoration(
        self,
        current_price: float,
        recent_candles: list
    ) -> tuple[bool, Optional[str]]:
        """
        Check if invalidated bias should be restored.
        
        Returns (restored, direction)
        """
        if not self._state.bias_before_invalidation or self._state.bias_invalidation_level <= 0:
            return False, None
        
        # Check price reclaim
        reclaim_ok = False
        if self._state.bias_before_invalidation == 'LONG':
            reclaim_ok = current_price >= self._state.bias_invalidation_level
        elif self._state.bias_before_invalidation == 'SHORT':
            reclaim_ok = current_price <= self._state.bias_invalidation_level
        
        if not reclaim_ok:
            return False, None
        
        # Check momentum (2 closes in the right direction)
        momentum_ok = False
        if recent_candles and len(recent_candles) >= 3:
            try:
                closes = [c['close'] for c in recent_candles[-3:]]
                opens = [c['open'] for c in recent_candles[-3:]]
                
                if self._state.bias_before_invalidation == 'LONG':
                    bullish_count = sum(1 for i in range(len(closes)) if closes[i] > opens[i])
                    momentum_ok = bullish_count >= 2
                else:
                    bearish_count = sum(1 for i in range(len(closes)) if closes[i] < opens[i])
                    momentum_ok = bearish_count >= 2
            except Exception:
                pass
        
        if reclaim_ok and momentum_ok:
            restored_bias = self._state.bias_before_invalidation
            logger.info(
                f"🔄 BIAS RESTORED: {restored_bias} | "
                f"Price reclaimed ${self._state.bias_invalidation_level:.0f} + momentum confirmed"
            )
            
            self._state.forbidden_direction = None
            self._state.bias_invalidation_level = 0.0
            self._state.bias_before_invalidation = None
            
            self._trade_tags['bias_restored'] = True
            self._trade_tags['bias_restored_direction'] = restored_bias
            
            return True, restored_bias
        
        return False, None
    
    # =========================================================================
    # DISPLACEMENT MEMORY
    # =========================================================================
    
    def save_displacement_memory(self, direction: str, amount: float):
        """Save displacement memory for obstinacy soft mode."""
        self._state.displacement_memory_direction = direction
        self._state.displacement_memory_amount = amount
        self._state.displacement_memory_expires_at = datetime.now() + timedelta(
            minutes=self.DISPLACEMENT_MEMORY_WINDOW_MINUTES
        )
        
        logger.info(
            f"🧠 DISPLACEMENT MEMORY: {direction} saved (${amount:.0f} move) | "
            f"OBSTINACY SOFT mode enabled for {self.DISPLACEMENT_MEMORY_WINDOW_MINUTES}min"
        )
        
        self._trade_tags['displacement_memory'] = direction
        self._trade_tags['displacement_memory_amount'] = amount
    
    def is_displacement_memory_active(self) -> tuple[bool, Optional[str], float]:
        """
        Check if displacement memory is active.
        
        Returns (is_active, direction, amount)
        """
        if not self._state.displacement_memory_direction:
            return False, None, 0.0
        
        if self._state.displacement_memory_expires_at and datetime.now() >= self._state.displacement_memory_expires_at:
            # Expired
            self._state.displacement_memory_direction = None
            self._state.displacement_memory_amount = 0.0
            self._state.displacement_memory_expires_at = None
            return False, None, 0.0
        
        return (
            True, 
            self._state.displacement_memory_direction, 
            self._state.displacement_memory_amount
        )
    
    # =========================================================================
    # RESET ON BIAS CHANGE
    # =========================================================================
    
    def on_bias_change(self, old_bias: Optional[str], new_bias: Optional[str]):
        """Handle bias change - reset expansion and direction lock state."""
        if old_bias != new_bias and old_bias is not None:
            logger.info(
                f"📊 BIAS CHANGED: {old_bias}→{new_bias} - "
                f"resetting displacement and expansion state"
            )
            
            self._clear_expansion_mode()
            self._clear_direction_lock()
            self._state.forbidden_direction = None
    
    # =========================================================================
    # STATE SETTERS
    # =========================================================================
    
    def set_structural_bias(self, bias: Optional[str]):
        """Set structural (HTF) bias."""
        self._state.structural_bias = bias
        self._trade_tags['structural_bias'] = bias
    
    def set_active_direction(self, direction: Optional[str]):
        """Set active trading direction."""
        self._state.active_direction = direction
        self._trade_tags['active_direction'] = direction
    
    def set_displacement_state(self, bypass: bool, strong: bool):
        """Set displacement state."""
        self._state.displacement_bypass = bypass
        self._state.strong_displacement = strong
        self._trade_tags['displacement_bypass'] = bypass
        self._trade_tags['strong_displacement'] = strong

    # =========================================================================
    # SYNC METHODS - Bridge between handler and trading_app
    # =========================================================================
    
    def sync_from_app(
        self,
        # Direction lock
        direction_lock: Optional[str],
        direction_lock_reason: Optional[str],
        direction_lock_expires_at: Optional[datetime],
        direction_lock_initial_move: float,
        # Expansion mode
        expansion_mode: bool,
        expansion_direction: Optional[str],
        expansion_start_time: Optional[datetime],
        expansion_initial_displacement: float,
        # Bias tracking
        bias_high_since: float,
        bias_low_since: float,
        # Forbidden
        forbidden_direction: Optional[str] = None,
        # Timeouts
        expansion_timeout_minutes: int = 20,
        asymmetric_invalidation_mult: float = 3.0
    ):
        """
        Sync internal state from trading_app's instance variables.
        
        Call this BEFORE using check methods to ensure handler has current state.
        """
        # Direction lock
        self._state.direction_lock = direction_lock
        self._state.direction_lock_reason = direction_lock_reason
        self._state.direction_lock_expires_at = direction_lock_expires_at
        self._state.direction_lock_initial_move = direction_lock_initial_move
        
        # Expansion mode
        self._state.expansion_mode = expansion_mode
        self._state.expansion_direction = expansion_direction
        self._state.expansion_start_time = expansion_start_time
        self._state.expansion_initial_displacement = expansion_initial_displacement
        self._state.expansion_timeout_minutes = expansion_timeout_minutes
        
        # Bias tracking
        self._state.bias_high_since = bias_high_since
        self._state.bias_low_since = bias_low_since
        
        # Forbidden
        self._state.forbidden_direction = forbidden_direction
        
        # Params
        self._params.asymmetric_invalidation_mult = asymmetric_invalidation_mult
    
    def sync_to_app(self) -> Dict[str, Any]:
        """
        Get state values to sync back to trading_app.
        
        Call this AFTER using check methods to get updated values.
        
        Returns dict with all state values that trading_app needs to update.
        """
        return {
            # Direction lock
            'direction_lock': self._state.direction_lock,
            'direction_lock_reason': self._state.direction_lock_reason,
            'direction_lock_expires_at': self._state.direction_lock_expires_at,
            'direction_lock_initial_move': self._state.direction_lock_initial_move,
            # Expansion mode
            'expansion_mode': self._state.expansion_mode,
            'expansion_direction': self._state.expansion_direction,
            'expansion_start_time': self._state.expansion_start_time,
            'expansion_initial_displacement': self._state.expansion_initial_displacement,
            # Forbidden
            'forbidden_direction': self._state.forbidden_direction,
        }

    # =========================================================================
    # UNIFIED DIRECTION VALIDATION (Feb 04, 2026 - AUDIT FIX)
    # 🔥 FIX #77 (Feb 07, 2026): REDUCE instead of BLOCK
    # =========================================================================
    # PHILOSOPHY: Direction conflicts should PENALIZE, not BLOCK.
    # The market gives signals that conflict sometimes. That's normal.
    # Instead of refusing to trade, we trade with reduced size/score.
    # 
    # NEW RETURN: (is_allowed, reason, category, size_mult, score_penalty)
    # - is_allowed: False ONLY for truly dangerous situations (< 2min expansion)
    # - size_mult: 1.0 = normal, 0.5 = half size, etc
    # - score_penalty: negative adjustment to entry score
    # =========================================================================
    
    def validate_direction(
        self,
        proposed_direction: str,
        current_time: Optional[datetime] = None,
        is_edge_active: bool = False
    ) -> tuple[bool, str, str, float, float]:
        """
        Validate if a proposed trade direction is allowed.
        
        🔥 FIX #77: Returns penalties instead of hard blocks for most cases.
        
        Args:
            proposed_direction: 'LONG' or 'SHORT' - direction we want to trade
            current_time: Current time for timeout calculations
            is_edge_active: If True, reduce penalties (edge already confirmed)
            
        Returns:
            (is_allowed, block_reason, block_category, size_mult, score_penalty)
            - is_allowed: True if trade can proceed (even with penalties)
            - block_reason: Human-readable reason for penalty/block
            - block_category: Category code
            - size_mult: Position size multiplier (1.0 = full, 0.5 = half)
            - score_penalty: Score adjustment (0 = no penalty, -1 = small, -2 = medium)
        """
        now = current_time or datetime.now()
        
        # Default: fully allowed
        size_mult = 1.0
        score_penalty = 0.0
        
        # =====================================================================
        # CHECK 1: DIRECTION LOCK
        # 🔥 FIX #77: Lock conflict → REDUCE, not BLOCK
        # 
        # OLD: direction_lock != proposed → HARD BLOCK
        # NEW: direction_lock != proposed → 50% size, -1.5 score
        # 
        # WHY: The lock was set based on PAST momentum. The current signal
        # may represent a LEGITIMATE reversal. Reducing size is safer than
        # missing a valid trade.
        # =====================================================================
        if self._state.direction_lock and self._state.direction_lock != proposed_direction:
            # Not a hard block anymore - just penalize
            lock_size_mult = 0.50
            lock_score_penalty = -1.5
            
            # If edge is active, reduce the penalty (edge already confirmed direction)
            if is_edge_active:
                lock_size_mult = 0.70
                lock_score_penalty = -0.8
                logger.info(
                    f"⚠️ FIX #77 DIRECTION SOFT: {proposed_direction} vs lock {self._state.direction_lock} | "
                    f"Edge active → size={lock_size_mult}, penalty={lock_score_penalty}"
                )
            else:
                logger.info(
                    f"⚠️ FIX #77 DIRECTION SOFT: {proposed_direction} vs lock {self._state.direction_lock} | "
                    f"Reducing size={lock_size_mult}, penalty={lock_score_penalty}"
                )
            
            size_mult = min(size_mult, lock_size_mult)
            score_penalty += lock_score_penalty
            self._trade_tags['direction_lock_penalty'] = True
            self._trade_tags['direction_lock_size_mult'] = lock_size_mult
        
        # =====================================================================
        # CHECK 2: FORBIDDEN DIRECTION
        # 🔥 FIX #77: Forbidden → HEAVY REDUCE (not block)
        # 
        # OLD: forbidden == proposed → HARD BLOCK
        # NEW: forbidden == proposed → 30% size, -2.0 score (but CAN trade)
        # 
        # WHY: Forbidden was set because price moved against us. But the
        # signal now may be a BETTER entry at lower price. Allow with caution.
        # =====================================================================
        if self._state.forbidden_direction == proposed_direction:
            forbidden_size_mult = 0.30
            forbidden_score_penalty = -2.0
            
            # If edge is active, it overrides the forbidden (edge detected fresh move)
            if is_edge_active:
                forbidden_size_mult = 0.60
                forbidden_score_penalty = -1.0
                logger.info(
                    f"⚠️ FIX #77 FORBIDDEN SOFT: {proposed_direction} forbidden but edge active | "
                    f"Reducing size={forbidden_size_mult}, penalty={forbidden_score_penalty}"
                )
            else:
                logger.warning(
                    f"⚠️ FIX #77 FORBIDDEN SOFT: {proposed_direction} forbidden | "
                    f"Heavy reduction size={forbidden_size_mult}, penalty={forbidden_score_penalty}"
                )
            
            size_mult = min(size_mult, forbidden_size_mult)
            score_penalty += forbidden_score_penalty
            self._trade_tags['forbidden_penalty'] = True
            self._trade_tags['forbidden_size_mult'] = forbidden_size_mult
        
        # =====================================================================
        # CHECK 3: COUNTER-EXPANSION
        # 🔥 FIX #77: ONLY hard block during first 2 minutes
        # After that, degrade to penalty
        #
        # WHY: Expansion mode protects against immediate reversal. But after
        # a few minutes, if there's a signal in opposite direction, the
        # expansion may have failed. Better to trade small than miss reversal.
        # =====================================================================
        if (self._state.expansion_mode and 
            self._state.expansion_direction and 
            self._state.expansion_direction != proposed_direction):
            
            if self._state.expansion_start_time:
                elapsed_sec = (now - self._state.expansion_start_time).total_seconds()
                elapsed_min = elapsed_sec / 60
                
                # First 2 minutes: HARD BLOCK (expansion just started)
                if elapsed_sec < 120:
                    return (
                        False,
                        f"Counter-expansion: {proposed_direction} vs {self._state.expansion_direction} ({elapsed_min:.1f}min - too fresh)",
                        'counter_expansion',
                        0.0,
                        0.0
                    )
                # 2-5 minutes: HEAVY penalty (expansion still recent)
                elif elapsed_sec < 300:
                    exp_size_mult = 0.30
                    exp_score_penalty = -2.5
                    logger.warning(
                        f"⚠️ FIX #77 EXPANSION SOFT: {proposed_direction} vs {self._state.expansion_direction} | "
                        f"{elapsed_min:.1f}min elapsed → size={exp_size_mult}, penalty={exp_score_penalty}"
                    )
                    size_mult = min(size_mult, exp_size_mult)
                    score_penalty += exp_score_penalty
                    self._trade_tags['expansion_counter_penalty'] = True
                # 5-10 minutes: MODERATE penalty (expansion may be failing)
                elif elapsed_sec < 600:
                    exp_size_mult = 0.50
                    exp_score_penalty = -1.5
                    logger.info(
                        f"⚠️ FIX #77 EXPANSION SOFT: {proposed_direction} vs {self._state.expansion_direction} | "
                        f"{elapsed_min:.1f}min elapsed → size={exp_size_mult}, penalty={exp_score_penalty}"
                    )
                    size_mult = min(size_mult, exp_size_mult)
                    score_penalty += exp_score_penalty
                    self._trade_tags['expansion_counter_penalty'] = True
                # >10 minutes: Just log, no penalty (expansion is old)
        
        # All checks passed (may have accumulated penalties)
        has_any_penalty = size_mult < 1.0 or score_penalty < 0
        reason = ""
        category = ""
        
        if has_any_penalty:
            reason = f"Direction penalties: size×{size_mult:.0%}, score{score_penalty:+.1f}"
            category = "direction_penalty"
        
        return (True, reason, category, size_mult, score_penalty)
    
    def is_direction_blocked(self, proposed_direction: str) -> bool:
        """Simple check if direction is blocked (no details).
        
        🔥 FIX #77: Now only returns True for HARD blocks (counter-expansion < 2min).
        """
        is_allowed, _, _, _, _ = self.validate_direction(proposed_direction)
        return not is_allowed
    
    def get_active_direction(self) -> Optional[str]:
        """
        Get the currently active trading direction.
        
        Priority:
        1. Expansion direction (if active)
        2. Direction lock
        3. Active direction (if set)
        4. Structural bias (fallback)
        """
        if self._state.expansion_mode and self._state.expansion_direction:
            return self._state.expansion_direction
        
        if self._state.direction_lock:
            return self._state.direction_lock
        
        if self._state.active_direction:
            return self._state.active_direction
        
        return self._state.structural_bias