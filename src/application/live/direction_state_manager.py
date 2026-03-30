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
    # Reducido de 1.5 a 1.2 para unlock más rápido
    # El usuario tiene razón: el sistema se queda "casado con el bias"
    # En microestructura, 15 minutos es mucho tiempo - el mercado cambia
    asymmetric_invalidation_mult: float = 1.2
    displacement_atr_mult: float = 2.8
    strong_displacement_mult: float = 1.5
    
    # FEB06: Nuevos parámetros para invalidación inteligente
    lock_time_decay_half_life_min: float = 8.0  # Lock pierde 50% fuerza cada 8min
    # Reducido de 0.6 a 0.5 para momentum override más rápido
    momentum_override_threshold: float = 0.5  # Si opuesto > 50% inicial + momentum alto → invalidar
    
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
    # Reducido de 3.0 a 1.8
    # 3.0x era demasiado conservador - mataba reversiones legítimas
    # 1.8x permite invalidar con movimiento opuesto más razonable
    ASYMMETRIC_INVALIDATION_BASE: float = 1.8
    DISPLACEMENT_MEMORY_WINDOW_MINUTES: int = 10  # Memory window
    FORBIDDEN_MIN_DURATION_TICKS: int = 120  # Hysteresis for forbidden
    
    # Threshold constants
    BASE_ATR_MULT_INVALIDATION: float = 1.5
    MIN_INVALIDATION_THRESHOLD: float = 50.0
    MAX_INVALIDATION_THRESHOLD: float = 250.0
    BASE_DISPLACEMENT_ATR_MULT: float = 1.8
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
        
        # Log periodically - Throttle 60 → 300 ticks
        if self._tick_count % 300 == 1:
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
    
    def _clear_expansion_mode(self):
        """Clear expansion mode."""
        self._state.expansion_mode = False
        self._state.expansion_direction = None
        self._state.expansion_start_time = None
        self._state.expansion_initial_displacement = 0.0

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