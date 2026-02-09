"""
Bias Invalidation Handler - Extracted from trading_app.py (Feb 04, 2026)

Handles bias invalidation and forbidden direction logic:
- Bias flip detection based on price movement
- Structural confirmation for invalidation
- Hysteresis to prevent oscillation
- Protection during expansion/direction lock
- Bias restoration when price reclaims

This reduces ~400 lines of inline code in _handle_idle_state to function calls.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class BiasInvalidationConfig:
    """Configuration for bias invalidation thresholds.
    
    FIX #89 (Feb 2026): Increased base_atr_mult from 1.5 to 2.5
    to reduce BIAS FLIP whipsaws. With ATR ~$60:
    - Normal: $60 * 2.5 = $150 threshold (was $90)
    - Direction Lock: $150 * 1.5 = $225 
    - Expansion: $150 * 2.0 = $300
    """
    base_atr_mult: float = 2.5  # Base threshold = ATR_1m * 2.5 (FIX #89: was 1.5)
    min_threshold: float = 80.0  # Minimum $80 (FIX #89: was $50)
    max_threshold: float = 350.0  # Maximum $350 (FIX #89: was $250)
    expansion_mult: float = 2.0  # Harder to invalidate during expansion
    direction_lock_mult: float = 1.5  # Harder during direction lock
    hysteresis_ticks: int = 180  # Min ticks before clearing forbidden (FIX #89: was 120)


@dataclass
class BiasInvalidationResult:
    """Result of bias invalidation check."""
    forbidden_direction: Optional[str] = None  # Direction now blocked
    should_forbid: bool = False  # Should we forbid a direction?
    forbid_reason: str = ""  # Why forbidding
    
    # Protection flags
    protected: bool = False  # Was direction protected?
    protection_reason: str = ""
    
    # Structural confirmation
    structure_broken: bool = False  # Did structure confirm invalidation?
    bias_flip_rejected: bool = False  # Was flip rejected due to intact structure?
    
    # Displacement memory
    displacement_memory_saved: bool = False
    displacement_memory_direction: Optional[str] = None
    displacement_memory_amount: float = 0.0
    
    # For debugging
    drop_from_high: float = 0.0
    rise_from_low: float = 0.0
    threshold_used: float = 0.0
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags."""
        tags = {}
        if self.protected:
            tags['protected_dip' if self.forbidden_direction == 'LONG' else 'protected_bounce'] = True
            tags['protection_reason'] = self.protection_reason
        if self.bias_flip_rejected:
            tags['bias_flip_rejected'] = True
            tags['bias_flip_reject_reason'] = 'structure_intact'
        if self.should_forbid:
            tags['bias_invalidated'] = True
            tags['forbidden_direction'] = self.forbidden_direction
        if self.displacement_memory_saved:
            tags['displacement_memory'] = self.displacement_memory_direction
            tags['displacement_memory_amount'] = self.displacement_memory_amount
        tags['bias_invalidation_threshold'] = self.threshold_used
        return tags


class BiasInvalidationHandler:
    """
    Handles bias invalidation and forbidden direction logic.
    
    Key behaviors:
    1. LONG bias + drop > threshold → FORBID LONG (allow SHORT)
    2. SHORT bias + rise > threshold → FORBID SHORT (allow LONG)
    3. Protection during EXPANSION_MODE or DIRECTION_LOCK
    4. Structural confirmation required (price must break swing)
    5. Hysteresis prevents rapid oscillation
    """
    
    def __init__(self, config: Optional[BiasInvalidationConfig] = None):
        """Initialize handler."""
        self.config = config or BiasInvalidationConfig()
        self._forbidden_set_at_tick: int = 0
        self._previous_forbidden: Optional[str] = None
    
    def calculate_threshold(
        self,
        atr_1m: float,
        expansion_mode: bool = False,
        direction_lock: Optional[str] = None
    ) -> tuple[float, str]:
        """
        Calculate invalidation threshold based on context.
        
        Returns:
            (threshold, reason)
        """
        # Determine multiplier based on context
        if expansion_mode:
            mult = self.config.expansion_mult
            reason = 'EXPANSION_MODE'
        elif direction_lock:
            mult = self.config.direction_lock_mult
            reason = 'DIRECTION_LOCK'
        else:
            mult = 1.0
            reason = 'NORMAL'
        
        threshold = atr_1m * self.config.base_atr_mult * mult
        threshold = max(self.config.min_threshold, 
                       min(self.config.max_threshold, threshold))
        
        return threshold, reason
    
    def check_invalidation(
        self,
        bias: Optional[str],
        current_price: float,
        bias_high_since: float,
        bias_low_since: float,
        atr_1m: float,
        swing_high_5m: float,
        swing_low_5m: float,
        micro_swing_high: Optional[float] = None,  # 🔥 NEW: 1m micro structure
        micro_swing_low: Optional[float] = None,   # 🔥 NEW: 1m micro structure
        expansion_mode: bool = False,
        expansion_direction: Optional[str] = None,
        direction_lock: Optional[str] = None,
        expansion_initial_displacement: float = 0.0,
        direction_lock_initial_move: float = 0.0,
        tick_count: int = 0,
        can_clear_forbidden: bool = False,
        current_forbidden: Optional[str] = None
    ) -> BiasInvalidationResult:
        """
        Check if bias should be invalidated based on price movement.
        
        Args:
            bias: Current HTF bias ('LONG', 'SHORT', None)
            current_price: Current price
            bias_high_since: Highest price since bias established
            bias_low_since: Lowest price since bias established
            atr_1m: 1-minute ATR for threshold
            swing_high_5m: Recent 5m swing high (for structural confirmation)
            swing_low_5m: Recent 5m swing low (for structural confirmation)
            micro_swing_high: 🔥 NEW - 1m swing high for faster structure break detection
            micro_swing_low: 🔥 NEW - 1m swing low for faster structure break detection
            expansion_mode: Is expansion mode active?
            expansion_direction: Direction of expansion
            direction_lock: Current direction lock
            expansion_initial_displacement: Initial displacement when expansion started
            direction_lock_initial_move: Initial move when lock started
            tick_count: Current tick count
            can_clear_forbidden: Has hysteresis period passed?
            current_forbidden: Current forbidden direction
        
        Returns:
            BiasInvalidationResult with invalidation details
        """
        result = BiasInvalidationResult()
        
        # 🔥 NEW (Feb 04, 2026): Store micro swings for use in handlers
        self._micro_swing_high = micro_swing_high
        self._micro_swing_low = micro_swing_low
        
        # Calculate displacement from extremes
        result.drop_from_high = bias_high_since - current_price
        result.rise_from_low = current_price - bias_low_since
        
        # Calculate threshold
        threshold, reason = self.calculate_threshold(
            atr_1m, expansion_mode, direction_lock
        )
        result.threshold_used = threshold
        
        # Store previous for change detection
        self._previous_forbidden = current_forbidden
        
        # Check LONG invalidation
        if bias == 'LONG' and result.drop_from_high > threshold:
            result = self._handle_long_invalidation(
                result, current_price, swing_low_5m,
                expansion_mode, expansion_direction, direction_lock,
                expansion_initial_displacement, direction_lock_initial_move,
                tick_count
            )
        
        # Check SHORT invalidation
        elif bias == 'SHORT' and result.rise_from_low > threshold:
            result = self._handle_short_invalidation(
                result, current_price, swing_high_5m,
                expansion_mode, expansion_direction, direction_lock,
                expansion_initial_displacement, direction_lock_initial_move,
                tick_count
            )
        
        # No invalidation condition - consider clearing forbidden
        else:
            if current_forbidden and can_clear_forbidden:
                ticks_since = tick_count - self._forbidden_set_at_tick
                # 🔥 FIX (Feb 05, 2026): Only log ONCE when actually clearing
                # The previous code logged every tick which spammed logs
                if self._previous_forbidden and result.forbidden_direction is None:
                    # Actually clearing - log this
                    logger.debug(  # Changed to DEBUG - not actionable info
                        f"FORBIDDEN {self._previous_forbidden} cleared "
                        f"after {ticks_since} ticks (hysteresis)"
                    )
                    self._previous_forbidden = None  # Clear so we don't log again
                result.forbidden_direction = None
        
        return result
    
    def _handle_long_invalidation(
        self,
        result: BiasInvalidationResult,
        current_price: float,
        swing_low_5m: float,
        expansion_mode: bool,
        expansion_direction: Optional[str],
        direction_lock: Optional[str],
        expansion_initial_displacement: float,
        direction_lock_initial_move: float,
        tick_count: int
    ) -> BiasInvalidationResult:
        """Handle LONG bias invalidation."""
        
        # 🔥 FIX (Feb 04, 2026): Use micro_swing if available for faster detection
        micro_swing_low = getattr(self, '_micro_swing_low', None)
        
        # Check structural confirmation
        # 🔥 NEW: Structure is broken if EITHER 5m OR micro swing is broken
        structure_broken_5m = current_price < swing_low_5m
        structure_broken_micro = (micro_swing_low is not None and current_price < micro_swing_low)
        
        result.structure_broken = structure_broken_5m or structure_broken_micro
        
        # Log which structure broke
        if result.structure_broken and not structure_broken_5m and structure_broken_micro:
            if tick_count % 30 == 1:
                logger.info(
                    f"🔬 MICRO STRUCTURE BREAK: price ${current_price:.0f} < micro_swing ${micro_swing_low:.0f} "
                    f"(5m swing ${swing_low_5m:.0f} not yet broken)"
                )
        
        # Check protection
        direction_locked_long = direction_lock == 'LONG'
        expansion_protected_long = expansion_mode and expansion_direction == 'LONG'
        
        if expansion_protected_long or direction_locked_long:
            # PROTECTION CASE
            result.protected = True
            result.protection_reason = 'DIRECTION_LOCK' if direction_locked_long else 'EXPANSION_MODE'
            
            if self._previous_forbidden != 'LONG' and tick_count % 30 == 1:
                logger.info(
                    f"🛡️ {result.protection_reason} PROTECTED: "
                    f"Drop ${result.drop_from_high:.0f} would FORBID LONG, "
                    f"but {result.protection_reason}=LONG active - ignoring dip"
                )
            return result
        
        if not result.structure_broken:
            # Threshold exceeded but structure NOT broken (neither 5m nor micro)
            result.bias_flip_rejected = True
            
            if self._previous_forbidden != 'LONG' and tick_count % 60 == 1:
                micro_info = f", micro_swing=${micro_swing_low:.0f}" if micro_swing_low else ""
                logger.info(
                    f"🔄 BIAS FLIP REJECTED: Drop ${result.drop_from_high:.0f} > "
                    f"threshold ${result.threshold_used:.0f} "
                    f"BUT price ${current_price:.0f} still above swing_low ${swing_low_5m:.0f}{micro_info} | "
                    f"Structure intact - treating as pullback, not flip"
                )
            return result
        
        # FORBID CASE: Threshold exceeded AND structure broken (5m OR micro)
        result.should_forbid = True
        result.forbidden_direction = 'LONG'
        structure_type = "micro_swing" if (not structure_broken_5m and structure_broken_micro) else "swing_low"
        structure_level = micro_swing_low if (not structure_broken_5m and structure_broken_micro) else swing_low_5m
        result.forbid_reason = (
            f"Drop ${result.drop_from_high:.0f} > {result.threshold_used:.0f} "
            f"and below {structure_type} ${structure_level:.0f}"
        )
        self._forbidden_set_at_tick = tick_count
        
        # Save displacement memory (SHORT displacement caused the drop)
        if expansion_initial_displacement > 0 or direction_lock_initial_move > 0:
            disp_amount = max(expansion_initial_displacement, direction_lock_initial_move)
            result.displacement_memory_saved = True
            result.displacement_memory_direction = 'SHORT'
            result.displacement_memory_amount = disp_amount
            
            logger.info(
                f"🧠 DISPLACEMENT MEMORY: SHORT saved (${disp_amount:.0f} move) | "
                f"OBSTINACY SOFT mode enabled"
            )
        
        if self._previous_forbidden != 'LONG':
            logger.warning(
                f"⚠️ BIAS FLIP: LONG invalidated ({result.forbid_reason}) "
                f"→ FORBID LONG, SHORT detection ENABLED"
            )
        
        return result
    
    def _handle_short_invalidation(
        self,
        result: BiasInvalidationResult,
        current_price: float,
        swing_high_5m: float,
        expansion_mode: bool,
        expansion_direction: Optional[str],
        direction_lock: Optional[str],
        expansion_initial_displacement: float,
        direction_lock_initial_move: float,
        tick_count: int
    ) -> BiasInvalidationResult:
        """Handle SHORT bias invalidation."""
        
        # 🔥 FIX (Feb 04, 2026): Use micro_swing if available for faster detection
        # MICRO structure break: check 1m swing first (more responsive)
        # If micro structure is broken, consider overall structure broken
        micro_swing_high = getattr(self, '_micro_swing_high', None)
        
        # Check structural confirmation
        # 🔥 NEW: Structure is broken if EITHER 5m OR micro swing is broken
        structure_broken_5m = current_price > swing_high_5m
        structure_broken_micro = (micro_swing_high is not None and current_price > micro_swing_high)
        
        # Use micro structure break if 5m not broken but micro is
        result.structure_broken = structure_broken_5m or structure_broken_micro
        
        # Log which structure broke
        if result.structure_broken and not structure_broken_5m and structure_broken_micro:
            if tick_count % 30 == 1:
                logger.info(
                    f"🔬 MICRO STRUCTURE BREAK: price ${current_price:.0f} > micro_swing ${micro_swing_high:.0f} "
                    f"(5m swing ${swing_high_5m:.0f} not yet broken)"
                )
        
        # Check protection
        direction_locked_short = direction_lock == 'SHORT'
        expansion_protected_short = expansion_mode and expansion_direction == 'SHORT'
        
        if expansion_protected_short or direction_locked_short:
            # PROTECTION CASE
            result.protected = True
            result.protection_reason = 'DIRECTION_LOCK' if direction_locked_short else 'EXPANSION_MODE'
            
            if self._previous_forbidden != 'SHORT' and tick_count % 30 == 1:
                logger.info(
                    f"🛡️ {result.protection_reason} PROTECTED: "
                    f"Rise ${result.rise_from_low:.0f} would FORBID SHORT, "
                    f"but {result.protection_reason}=SHORT active - ignoring bounce"
                )
            return result
        
        if not result.structure_broken:
            # Threshold exceeded but structure NOT broken (neither 5m nor micro)
            result.bias_flip_rejected = True
            
            if self._previous_forbidden != 'SHORT' and tick_count % 60 == 1:
                micro_info = f", micro_swing=${micro_swing_high:.0f}" if micro_swing_high else ""
                logger.info(
                    f"🔄 BIAS FLIP REJECTED: Rise ${result.rise_from_low:.0f} > "
                    f"threshold ${result.threshold_used:.0f} "
                    f"BUT price ${current_price:.0f} still below swing_high ${swing_high_5m:.0f}{micro_info} | "
                    f"Structure intact - treating as bounce, not flip"
                )
            return result
        
        # FORBID CASE: Threshold exceeded AND structure broken (5m OR micro)
        result.should_forbid = True
        result.forbidden_direction = 'SHORT'
        structure_type = "micro_swing" if (not structure_broken_5m and structure_broken_micro) else "swing_high"
        structure_level = micro_swing_high if (not structure_broken_5m and structure_broken_micro) else swing_high_5m
        result.forbid_reason = (
            f"Rise ${result.rise_from_low:.0f} > {result.threshold_used:.0f} "
            f"and above {structure_type} ${structure_level:.0f}"
        )
        self._forbidden_set_at_tick = tick_count
        
        # Save displacement memory (LONG displacement caused the rise)
        if expansion_initial_displacement > 0 or direction_lock_initial_move > 0:
            disp_amount = max(expansion_initial_displacement, direction_lock_initial_move)
            result.displacement_memory_saved = True
            result.displacement_memory_direction = 'LONG'
            result.displacement_memory_amount = disp_amount
            
            logger.info(
                f"🧠 DISPLACEMENT MEMORY: LONG saved (${disp_amount:.0f} move) | "
                f"OBSTINACY SOFT mode enabled"
            )
        
        if self._previous_forbidden != 'SHORT':
            logger.warning(
                f"⚠️ BIAS FLIP: SHORT invalidated ({result.forbid_reason}) "
                f"→ FORBID SHORT, LONG detection ENABLED"
            )
        
        return result


@dataclass
class ExpansionActivationResult:
    """Result of expansion mode activation check."""
    should_activate: bool = False
    direction: Optional[str] = None
    displacement_amount: float = 0.0
    is_stale: bool = False
    stale_reason: str = ""
    range_blocked: bool = False
    range_block_reason: str = ""
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags."""
        tags = {}
        if self.should_activate:
            tags['expansion_mode_activated'] = True
            tags['expansion_direction'] = self.direction
            tags['direction_lock'] = self.direction
        if self.is_stale:
            tags['stale_displacement'] = True
        if self.range_blocked:
            tags['range_floor_block' if self.direction == 'SHORT' else 'range_ceiling_block'] = True
        return tags


class ExpansionActivationHandler:
    """
    Handles expansion mode and direction lock activation.
    
    Activated when strong displacement is detected.
    Includes stale and range floor/ceiling checks.
    """
    
    STALE_ATR_MULT: float = 0.5  # Within 0.5 ATR = stale
    RANGE_ATR_MULT: float = 1.5  # Within 1.5 ATR of floor/ceiling = blocked
    
    def __init__(self):
        """Initialize handler."""
        self._last_log_tick_short: int = 0
        self._last_log_tick_long: int = 0
    
    def check_short_activation(
        self,
        current_price: float,
        bias_low_since: float,
        displacement_amount: float,
        atr_5m: float,
        trend_phase: str,
        expansion_mode: bool,
        expansion_direction: Optional[str],
        tick_count: int
    ) -> ExpansionActivationResult:
        """Check if SHORT expansion should be activated."""
        result = ExpansionActivationResult()
        result.direction = 'SHORT'
        result.displacement_amount = displacement_amount
        
        distance_from_low = current_price - bias_low_since
        stale_threshold = atr_5m * self.STALE_ATR_MULT
        range_threshold = atr_5m * self.RANGE_ATR_MULT
        
        # Check range floor
        is_range_bound = trend_phase == 'RANGE_BOUND'
        if is_range_bound and distance_from_low < range_threshold:
            result.range_blocked = True
            result.range_block_reason = (
                f"Price ${current_price:.0f} only ${distance_from_low:.0f} from low "
                f"(< {range_threshold:.0f} = 1.5 ATR)"
            )
            result.is_stale = True  # Treat as stale
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_short:
                logger.warning(
                    f"🚫 RANGE FLOOR BLOCK SHORT: {result.range_block_reason}"
                )
                self._last_log_tick_short = tick_count
            return result
        
        # Check stale
        if distance_from_low < stale_threshold and distance_from_low >= 0:
            result.is_stale = True
            result.stale_reason = (
                f"Price ${current_price:.0f} only ${distance_from_low:.0f} from low "
                f"(< {stale_threshold:.0f})"
            )
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_short:
                logger.warning(
                    f"⚠️ STALE DISPLACEMENT SHORT: {result.stale_reason}"
                )
                self._last_log_tick_short = tick_count
            return result
        
        # Should activate if not already in SHORT expansion
        if not expansion_mode or expansion_direction != 'SHORT':
            result.should_activate = True
        
        return result
    
    def check_long_activation(
        self,
        current_price: float,
        bias_high_since: float,
        displacement_amount: float,
        atr_5m: float,
        trend_phase: str,
        expansion_mode: bool,
        expansion_direction: Optional[str],
        tick_count: int
    ) -> ExpansionActivationResult:
        """Check if LONG expansion should be activated."""
        result = ExpansionActivationResult()
        result.direction = 'LONG'
        result.displacement_amount = displacement_amount
        
        distance_from_high = bias_high_since - current_price
        stale_threshold = atr_5m * self.STALE_ATR_MULT
        range_threshold = atr_5m * self.RANGE_ATR_MULT
        
        # Check range ceiling
        is_range_bound = trend_phase == 'RANGE_BOUND'
        if is_range_bound and distance_from_high < range_threshold:
            result.range_blocked = True
            result.range_block_reason = (
                f"Price ${current_price:.0f} only ${distance_from_high:.0f} from high "
                f"(< {range_threshold:.0f} = 1.5 ATR)"
            )
            result.is_stale = True  # Treat as stale
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_long:
                logger.warning(
                    f"🚫 RANGE CEILING BLOCK LONG: {result.range_block_reason}"
                )
                self._last_log_tick_long = tick_count
            return result
        
        # Check stale
        if distance_from_high < stale_threshold and distance_from_high >= 0:
            result.is_stale = True
            result.stale_reason = (
                f"Price ${current_price:.0f} only ${distance_from_high:.0f} from high "
                f"(< {stale_threshold:.0f})"
            )
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_long:
                logger.warning(
                    f"⚠️ STALE DISPLACEMENT LONG: {result.stale_reason}"
                )
                self._last_log_tick_long = tick_count
            return result
        
        # Should activate if not already in LONG expansion
        if not expansion_mode or expansion_direction != 'LONG':
            result.should_activate = True
        
        return result
