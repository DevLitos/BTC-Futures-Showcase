"""
Displacement Handler - Extracted from trading_app.py (Feb 04, 2026)

Handles displacement detection AND activation of EXPANSION_MODE + DIRECTION_LOCK.
Combines detection, stale checks, range floor/ceiling, and edge arming.

This extracts ~350 lines of duplicate SHORT/LONG logic into a single handler.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from src.domain.trading.edge_state_manager import EdgeStateManager as EdgeManager

logger = logging.getLogger(__name__)


@dataclass
class DisplacementActivationResult:
    """Result of displacement activation check."""
    # Detection
    detected: bool = False
    direction: Optional[str] = None
    amount: float = 0.0
    is_strong: bool = False
    
    # Stale/Range checks
    is_stale: bool = False
    stale_reason: str = ""
    range_blocked: bool = False
    range_block_reason: str = ""
    
    # Activation
    expansion_activated: bool = False
    direction_lock_activated: bool = False
    edge_armed: bool = False
    forbidden_cleared: bool = False
    
    # State updates to apply
    expansion_mode: bool = False
    expansion_direction: Optional[str] = None
    expansion_start_time: Optional[datetime] = None
    expansion_initial_displacement: float = 0.0
    direction_lock: Optional[str] = None
    direction_lock_reason: Optional[str] = None
    direction_lock_expires_at: Optional[datetime] = None
    direction_lock_initial_move: float = 0.0
    
    # Tags for diagnostics
    trade_tags: Dict[str, Any] = field(default_factory=dict)


class DisplacementHandler:
    """
    Handles displacement detection and EXPANSION_MODE activation.
    
    Encapsulates:
    - Threshold calculation (adaptive based on volatility)
    - Stale displacement detection
    - Range floor/ceiling blocking
    - EXPANSION_MODE + DIRECTION_LOCK activation
    - Edge arming via EdgeManager
    - Forbidden direction clearing
    """
    
    # Base thresholds
    BASE_DISPLACEMENT_ATR_MULT = 2.8  # ~1.1% of price
    STRONG_DISPLACEMENT_FACTOR = 1.5
    STALE_ATR_MULT = 0.5  # Within 0.5 ATR = stale
    RANGE_FLOOR_CEILING_ATR_MULT = 1.5  # 1.5 ATR from extreme
    
    def __init__(self):
        """Initialize handler."""
        self._last_log_tick_short = 0
        self._last_log_tick_long = 0
        self._last_stale_log_short = False
        self._last_stale_log_long = False
        self._last_range_floor_log = False
        self._last_range_ceiling_log = False
    
    def process_displacement(
        self,
        current_price: float,
        bias_high_since: float,
        bias_low_since: float,
        atr_5m: float,
        volatility_ratio: float,
        trend_phase,  # TrendPhase enum
        direction_lock_timeout_minutes: int,
        asymmetric_invalidation_mult: float,
        tick_count: int,
        # Current state
        expansion_mode: bool,
        expansion_direction: Optional[str],
        forbidden_direction: Optional[str],
        # EdgeManager for arming
        edge_manager: 'EdgeManager',
        market_time: datetime
    ) -> DisplacementActivationResult:
        """
        Process displacement and return activation result.
        
        Args:
            current_price: Current market price
            bias_high_since: Highest price since bias established
            bias_low_since: Lowest price since bias established
            atr_5m: 5-minute ATR
            volatility_ratio: Current volatility ratio
            trend_phase: Current trend phase (for range detection)
            direction_lock_timeout_minutes: Lock timeout setting
            asymmetric_invalidation_mult: Invalidation multiplier
            tick_count: Current tick for logging throttle
            expansion_mode: Is expansion currently active?
            expansion_direction: Current expansion direction
            forbidden_direction: Currently forbidden direction
            edge_manager: EdgeManager for arming displacement edges
            market_time: Current market time
        
        Returns:
            DisplacementActivationResult with all state updates
        """
        result = DisplacementActivationResult()
        
        # Calculate adaptive thresholds
        vol_adj = 1.0 + (volatility_ratio - 1.0) * 0.3
        vol_adj = max(0.7, min(1.5, vol_adj))
        
        displacement_threshold = atr_5m * self.BASE_DISPLACEMENT_ATR_MULT * vol_adj
        strong_threshold = displacement_threshold * self.STRONG_DISPLACEMENT_FACTOR * vol_adj
        stale_threshold = atr_5m * self.STALE_ATR_MULT
        range_threshold = atr_5m * self.RANGE_FLOOR_CEILING_ATR_MULT
        
        # Store thresholds in tags
        result.trade_tags['displacement_threshold'] = displacement_threshold
        result.trade_tags['strong_disp_threshold'] = strong_threshold
        result.trade_tags['vol_displacement_adj'] = vol_adj
        
        # Calculate displacement
        drop_from_high = bias_high_since - current_price
        rise_from_low = current_price - bias_low_since
        
        # Check SHORT displacement
        if drop_from_high >= displacement_threshold:
            result = self._process_short_displacement(
                result, drop_from_high, current_price, bias_low_since, bias_high_since,
                displacement_threshold, strong_threshold, stale_threshold, range_threshold,
                trend_phase, direction_lock_timeout_minutes, asymmetric_invalidation_mult,
                tick_count, expansion_mode, expansion_direction, forbidden_direction,
                edge_manager, market_time, atr_5m
            )
        
        # Check LONG displacement
        elif rise_from_low >= displacement_threshold:
            result = self._process_long_displacement(
                result, rise_from_low, current_price, bias_high_since, bias_low_since,
                displacement_threshold, strong_threshold, stale_threshold, range_threshold,
                trend_phase, direction_lock_timeout_minutes, asymmetric_invalidation_mult,
                tick_count, expansion_mode, expansion_direction, forbidden_direction,
                edge_manager, market_time, atr_5m
            )
        
        return result
    
    def _process_short_displacement(
        self, result: DisplacementActivationResult,
        drop_from_high: float, current_price: float,
        bias_low_since: float, bias_high_since: float,
        displacement_threshold: float, strong_threshold: float,
        stale_threshold: float, range_threshold: float,
        trend_phase, direction_lock_timeout_minutes: int,
        asymmetric_invalidation_mult: float, tick_count: int,
        expansion_mode: bool, expansion_direction: Optional[str],
        forbidden_direction: Optional[str],
        edge_manager: 'EdgeManager', market_time: datetime, atr_5m: float
    ) -> DisplacementActivationResult:
        """Process SHORT displacement detection and activation."""
        from src.domain.market.regime import TrendPhase
        
        result.detected = True
        result.direction = 'SHORT'
        result.amount = drop_from_high
        
        # Strong displacement check
        if drop_from_high >= strong_threshold:
            result.is_strong = True
            
            # Stale check
            distance_from_low = current_price - bias_low_since
            
            # Range floor check
            is_range_bound = trend_phase == TrendPhase.RANGE_BOUND
            is_at_range_floor = is_range_bound and distance_from_low < range_threshold
            
            if is_at_range_floor:
                result.range_blocked = True
                result.range_block_reason = (
                    f"RANGE_BOUND and price ${current_price:.0f} only "
                    f"${distance_from_low:.0f} from low ${bias_low_since:.0f}"
                )
                result.is_stale = True
                result.trade_tags['range_floor_block'] = True
                result.trade_tags['range_floor_distance'] = distance_from_low
                
                if tick_count % 30 == 1 or not self._last_range_floor_log:
                    logger.warning(f"🚫 RANGE FLOOR BLOCK SHORT: {result.range_block_reason}")
                    self._last_range_floor_log = True
            
            elif distance_from_low < stale_threshold and distance_from_low >= 0:
                result.is_stale = True
                result.stale_reason = (
                    f"Price ${current_price:.0f} only ${distance_from_low:.0f} from low"
                )
                result.trade_tags['stale_displacement'] = True
                result.trade_tags['stale_distance_from_extreme'] = distance_from_low
                
                if tick_count % 30 == 1 or not self._last_stale_log_short:
                    logger.warning(f"⚠️ STALE DISPLACEMENT SHORT: {result.stale_reason}")
                    self._last_stale_log_short = True
            
            # Activate EXPANSION_MODE if not stale
            if not result.is_stale and (not expansion_mode or expansion_direction != 'SHORT'):
                now = datetime.now()
                result.expansion_activated = True
                result.expansion_mode = True
                result.expansion_direction = 'SHORT'
                result.expansion_start_time = now
                result.expansion_initial_displacement = drop_from_high
                
                result.direction_lock_activated = True
                result.direction_lock = 'SHORT'
                result.direction_lock_reason = 'STRONG_DISPLACEMENT'
                result.direction_lock_expires_at = now + timedelta(minutes=direction_lock_timeout_minutes)
                result.direction_lock_initial_move = drop_from_high
                
                self._last_stale_log_short = False
                
                logger.warning(
                    f"🚀 EXPANSION_MODE + DIRECTION_LOCK ACTIVATED: SHORT | "
                    f"Displacement ${drop_from_high:.0f} ≥ {strong_threshold:.0f}"
                )
                
                result.trade_tags['expansion_mode_activated'] = True
                result.trade_tags['expansion_direction'] = 'SHORT'
                result.trade_tags['direction_lock'] = 'SHORT'
                
                # Arm edge
                if edge_manager:
                    edge_armed = edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='SHORT',
                        displacement_amount=drop_from_high,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=bias_high_since,
                        is_stale=False,
                        stale_distance=distance_from_low
                    )
                    if edge_armed:
                        result.edge_armed = True
                        result.trade_tags['displacement_edge_armed'] = True
                        result.trade_tags['edge_type'] = 'DISPLACEMENT'
                        logger.info(f"🌊 DISPLACEMENT EDGE ARMED: SHORT")
            
            # Clear forbidden
            if forbidden_direction == 'SHORT':
                result.forbidden_cleared = True
                result.trade_tags['displacement_override_bias_flip'] = True
                logger.info(f"🔥 DISPLACEMENT OVERRIDE: Clearing FORBIDDEN=SHORT")
        
        # Log detection
        if tick_count % 30 == 1:
            logger.info(
                f"🔥 DISPLACEMENT: Down ${drop_from_high:.0f} ≥ {displacement_threshold:.0f}"
                f"{' | STRONG' if result.is_strong else ''}"
            )
        
        result.trade_tags['displacement_direction'] = 'SHORT'
        result.trade_tags['displacement_amount'] = drop_from_high
        result.trade_tags['strong_displacement'] = result.is_strong
        
        return result
    
    def _process_long_displacement(
        self, result: DisplacementActivationResult,
        rise_from_low: float, current_price: float,
        bias_high_since: float, bias_low_since: float,
        displacement_threshold: float, strong_threshold: float,
        stale_threshold: float, range_threshold: float,
        trend_phase, direction_lock_timeout_minutes: int,
        asymmetric_invalidation_mult: float, tick_count: int,
        expansion_mode: bool, expansion_direction: Optional[str],
        forbidden_direction: Optional[str],
        edge_manager: 'EdgeManager', market_time: datetime, atr_5m: float
    ) -> DisplacementActivationResult:
        """Process LONG displacement detection and activation."""
        from src.domain.market.regime import TrendPhase
        
        result.detected = True
        result.direction = 'LONG'
        result.amount = rise_from_low
        
        # Strong displacement check
        if rise_from_low >= strong_threshold:
            result.is_strong = True
            
            # Stale check
            distance_from_high = bias_high_since - current_price
            
            # Range ceiling check
            is_range_bound = trend_phase == TrendPhase.RANGE_BOUND
            is_at_range_ceiling = is_range_bound and distance_from_high < range_threshold
            
            if is_at_range_ceiling:
                result.range_blocked = True
                result.range_block_reason = (
                    f"RANGE_BOUND and price ${current_price:.0f} only "
                    f"${distance_from_high:.0f} from high ${bias_high_since:.0f}"
                )
                result.is_stale = True
                result.trade_tags['range_ceiling_block'] = True
                result.trade_tags['range_ceiling_distance'] = distance_from_high
                
                if tick_count % 30 == 1 or not self._last_range_ceiling_log:
                    logger.warning(f"🚫 RANGE CEILING BLOCK LONG: {result.range_block_reason}")
                    self._last_range_ceiling_log = True
            
            elif distance_from_high < stale_threshold and distance_from_high >= 0:
                result.is_stale = True
                result.stale_reason = (
                    f"Price ${current_price:.0f} only ${distance_from_high:.0f} from high"
                )
                result.trade_tags['stale_displacement'] = True
                result.trade_tags['stale_distance_from_extreme'] = distance_from_high
                
                if tick_count % 30 == 1 or not self._last_stale_log_long:
                    logger.warning(f"⚠️ STALE DISPLACEMENT LONG: {result.stale_reason}")
                    self._last_stale_log_long = True
            
            # Activate EXPANSION_MODE if not stale
            if not result.is_stale and (not expansion_mode or expansion_direction != 'LONG'):
                now = datetime.now()
                result.expansion_activated = True
                result.expansion_mode = True
                result.expansion_direction = 'LONG'
                result.expansion_start_time = now
                result.expansion_initial_displacement = rise_from_low
                
                result.direction_lock_activated = True
                result.direction_lock = 'LONG'
                result.direction_lock_reason = 'STRONG_DISPLACEMENT'
                result.direction_lock_expires_at = now + timedelta(minutes=direction_lock_timeout_minutes)
                result.direction_lock_initial_move = rise_from_low
                
                self._last_stale_log_long = False
                
                logger.warning(
                    f"🚀 EXPANSION_MODE + DIRECTION_LOCK ACTIVATED: LONG | "
                    f"Displacement ${rise_from_low:.0f} ≥ {strong_threshold:.0f}"
                )
                
                result.trade_tags['expansion_mode_activated'] = True
                result.trade_tags['expansion_direction'] = 'LONG'
                result.trade_tags['direction_lock'] = 'LONG'
                
                # Arm edge
                if edge_manager:
                    edge_armed = edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='LONG',
                        displacement_amount=rise_from_low,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=bias_low_since,
                        is_stale=False,
                        stale_distance=distance_from_high
                    )
                    if edge_armed:
                        result.edge_armed = True
                        result.trade_tags['displacement_edge_armed'] = True
                        result.trade_tags['edge_type'] = 'DISPLACEMENT'
                        logger.info(f"🌊 DISPLACEMENT EDGE ARMED: LONG")
            
            # Clear forbidden
            if forbidden_direction == 'LONG':
                result.forbidden_cleared = True
                result.trade_tags['displacement_override_bias_flip'] = True
                logger.info(f"🔥 DISPLACEMENT OVERRIDE: Clearing FORBIDDEN=LONG")
        
        # Log detection
        if tick_count % 30 == 1:
            logger.info(
                f"🔥 DISPLACEMENT: Up ${rise_from_low:.0f} ≥ {displacement_threshold:.0f}"
                f"{' | STRONG' if result.is_strong else ''}"
            )
        
        result.trade_tags['displacement_direction'] = 'LONG'
        result.trade_tags['displacement_amount'] = rise_from_low
        result.trade_tags['strong_displacement'] = result.is_strong
        
        return result
