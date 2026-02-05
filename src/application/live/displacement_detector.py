"""
Displacement Detector - Extracted from trading_app.py (Feb 04, 2026)

Detects price displacement (strong directional moves) and handles:
- Displacement thresholds based on ATR
- Strong vs regular displacement
- Stale displacement detection (momentum exhausted)
- Range floor/ceiling blocks

The displacement IS the edge - when detected, trade without waiting for HTF confirmation.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class DisplacementResult:
    """Result of displacement detection."""
    detected: bool = False
    direction: Optional[str] = None  # 'LONG' or 'SHORT'
    strong: bool = False  # Strong displacement (1.5x threshold)
    amount: float = 0.0  # Displacement amount in price units
    
    # Stale detection
    is_stale: bool = False
    stale_distance: float = 0.0
    stale_reason: Optional[str] = None
    
    # Range floor/ceiling blocks
    range_blocked: bool = False
    range_block_reason: Optional[str] = None
    
    # Should arm edge
    should_arm_edge: bool = False
    
    # Thresholds used (for debugging)
    threshold: float = 0.0
    strong_threshold: float = 0.0
    stale_threshold: float = 0.0
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags."""
        tags = {}
        if self.detected:
            tags['displacement_direction'] = self.direction
            tags['displacement_amount'] = self.amount
            tags['strong_displacement'] = self.strong
            tags['displacement_threshold'] = self.threshold
            tags['strong_disp_threshold'] = self.strong_threshold
        if self.is_stale:
            tags['stale_displacement'] = True
            tags['stale_distance_from_extreme'] = self.stale_distance
            tags['stale_threshold'] = self.stale_threshold
        if self.range_blocked:
            tags['range_floor_block' if self.direction == 'SHORT' else 'range_ceiling_block'] = True
        return tags


class DisplacementDetector:
    """
    Detects price displacement and determines trading direction.
    
    Displacement is a strong price move that indicates institutional flow.
    When detected, it overrides other direction signals.
    """
    
    # Base thresholds (scaled by volatility)
    BASE_DISPLACEMENT_ATR_MULT: float = 2.8  # ~1.1% of price for typical ATR
    STRONG_DISPLACEMENT_FACTOR: float = 1.5  # 1.5x for strong
    STALE_ATR_MULT: float = 0.5  # Within 0.5 ATR = stale
    RANGE_FLOOR_CEILING_ATR_MULT: float = 1.5  # 1.5 ATR from extreme = at floor/ceiling
    
    def __init__(self):
        """Initialize detector."""
        self._last_log_tick_short: int = 0
        self._last_log_tick_long: int = 0
    
    def detect(
        self,
        current_price: float,
        bias_high_since: float,
        bias_low_since: float,
        atr_5m: float,
        vol_displacement_adj: float = 1.0,
        trend_phase: Optional[str] = None,
        tick_count: int = 0
    ) -> DisplacementResult:
        """
        Detect displacement from price movement.
        
        Args:
            current_price: Current price
            bias_high_since: Highest price since bias established
            bias_low_since: Lowest price since bias established
            atr_5m: 5-minute ATR
            vol_displacement_adj: Volatility adjustment multiplier
            trend_phase: Current trend phase (e.g., 'RANGE_BOUND')
            tick_count: Current tick count for logging
        
        Returns:
            DisplacementResult with detection details
        """
        result = DisplacementResult()
        
        # Calculate thresholds
        disp_mult = self.BASE_DISPLACEMENT_ATR_MULT * vol_displacement_adj
        result.threshold = atr_5m * disp_mult
        result.strong_threshold = result.threshold * self.STRONG_DISPLACEMENT_FACTOR
        result.stale_threshold = atr_5m * self.STALE_ATR_MULT
        
        # Calculate displacement from extremes
        drop_from_high = bias_high_since - current_price
        rise_from_low = current_price - bias_low_since
        
        # Check for downward displacement
        if drop_from_high >= result.threshold:
            result.detected = True
            result.direction = 'SHORT'
            result.amount = drop_from_high
            result.strong = drop_from_high >= result.strong_threshold
            
            if result.strong:
                # Check stale and range conditions
                result = self._check_short_conditions(
                    result, current_price, bias_low_since, 
                    atr_5m, trend_phase, tick_count
                )
            
            # Log detection
            if tick_count % 30 == 1:
                self._log_displacement(result, drop_from_high, 'Down')
        
        # Check for upward displacement
        elif rise_from_low >= result.threshold:
            result.detected = True
            result.direction = 'LONG'
            result.amount = rise_from_low
            result.strong = rise_from_low >= result.strong_threshold
            
            if result.strong:
                # Check stale and range conditions
                result = self._check_long_conditions(
                    result, current_price, bias_high_since,
                    atr_5m, trend_phase, tick_count
                )
            
            # Log detection
            if tick_count % 30 == 1:
                self._log_displacement(result, rise_from_low, 'Up')
        
        return result
    
    def _check_short_conditions(
        self,
        result: DisplacementResult,
        current_price: float,
        bias_low_since: float,
        atr_5m: float,
        trend_phase: Optional[str],
        tick_count: int
    ) -> DisplacementResult:
        """Check stale and range conditions for SHORT displacement."""
        
        # Distance from low (for stale detection)
        distance_from_low = current_price - bias_low_since
        range_threshold = atr_5m * self.RANGE_FLOOR_CEILING_ATR_MULT
        
        # Check range floor
        is_range_bound = trend_phase == 'RANGE_BOUND'
        if is_range_bound and distance_from_low < range_threshold:
            result.range_blocked = True
            result.range_block_reason = (
                f"TrendPhase=RANGE_BOUND and price ${current_price:.0f} "
                f"is only ${distance_from_low:.0f} from low ${bias_low_since:.0f} "
                f"(< {range_threshold:.0f} = 1.5 ATR)"
            )
            result.is_stale = True  # Treat as stale to prevent entry
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_short:
                logger.warning(
                    f"🚫 RANGE FLOOR BLOCK SHORT: {result.range_block_reason} | "
                    f"Cannot SHORT at the floor of a range - wait for breakout"
                )
                self._last_log_tick_short = tick_count
            
            return result
        
        # Check stale displacement
        if distance_from_low < result.stale_threshold and distance_from_low >= 0:
            result.is_stale = True
            result.stale_distance = distance_from_low
            result.stale_reason = (
                f"Price ${current_price:.0f} is only ${distance_from_low:.0f} "
                f"from low ${bias_low_since:.0f} (< {result.stale_threshold:.0f})"
            )
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_short:
                logger.warning(
                    f"⚠️ STALE DISPLACEMENT SHORT: {result.stale_reason} | "
                    f"Momentum exhausted → NOT arming edge"
                )
                self._last_log_tick_short = tick_count
        
        # Should arm edge if strong and not stale
        result.should_arm_edge = result.strong and not result.is_stale
        
        return result
    
    def _check_long_conditions(
        self,
        result: DisplacementResult,
        current_price: float,
        bias_high_since: float,
        atr_5m: float,
        trend_phase: Optional[str],
        tick_count: int
    ) -> DisplacementResult:
        """Check stale and range conditions for LONG displacement."""
        
        # Distance from high (for stale detection)
        distance_from_high = bias_high_since - current_price
        range_threshold = atr_5m * self.RANGE_FLOOR_CEILING_ATR_MULT
        
        # Check range ceiling
        is_range_bound = trend_phase == 'RANGE_BOUND'
        if is_range_bound and distance_from_high < range_threshold:
            result.range_blocked = True
            result.range_block_reason = (
                f"TrendPhase=RANGE_BOUND and price ${current_price:.0f} "
                f"is only ${distance_from_high:.0f} from high ${bias_high_since:.0f} "
                f"(< {range_threshold:.0f} = 1.5 ATR)"
            )
            result.is_stale = True  # Treat as stale to prevent entry
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_long:
                logger.warning(
                    f"🚫 RANGE CEILING BLOCK LONG: {result.range_block_reason} | "
                    f"Cannot LONG at the ceiling of a range - wait for breakout"
                )
                self._last_log_tick_long = tick_count
            
            return result
        
        # Check stale displacement
        if distance_from_high < result.stale_threshold and distance_from_high >= 0:
            result.is_stale = True
            result.stale_distance = distance_from_high
            result.stale_reason = (
                f"Price ${current_price:.0f} is only ${distance_from_high:.0f} "
                f"from high ${bias_high_since:.0f} (< {result.stale_threshold:.0f})"
            )
            
            if tick_count % 30 == 1 and tick_count != self._last_log_tick_long:
                logger.warning(
                    f"⚠️ STALE DISPLACEMENT LONG: {result.stale_reason} | "
                    f"Momentum exhausted → NOT arming edge"
                )
                self._last_log_tick_long = tick_count
        
        # Should arm edge if strong and not stale
        result.should_arm_edge = result.strong and not result.is_stale
        
        return result
    
    def _log_displacement(
        self,
        result: DisplacementResult,
        amount: float,
        direction_word: str
    ):
        """Log displacement detection."""
        extra_info = []
        if result.strong:
            extra_info.append('STRONG DISPLACEMENT')
        if result.is_stale:
            extra_info.append('STALE')
        
        extra_str = ' | '.join(extra_info) if extra_info else ''
        
        logger.info(
            f"🔥 DISPLACEMENT DETECTED: {direction_word} ${amount:.0f} ≥ {result.threshold:.0f} "
            f"→ ACTIVE_DIRECTION={result.direction} | Bypass filters enabled"
            f"{' | ' + extra_str if extra_str else ''}"
        )


@dataclass 
class MicroMomentumResult:
    """Result of micro momentum detection."""
    direction: Optional[str] = None  # 'LONG', 'SHORT', None
    strength: str = 'WEAK'  # 'STRONG' or 'WEAK'
    
    # Move quality analysis
    move_quality: str = 'UNKNOWN'  # 'FRESH', 'DEVELOPING', 'EXHAUSTING'
    move_size_mult: float = 1.0  # 0.0 for EXHAUSTING, 1.0 for FRESH
    
    # Detailed metrics
    vol_ratio: float = 1.0
    shrinkage: float = 1.0
    wick_ratio: float = 0.0
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags."""
        tags = {}
        if self.direction:
            tags['micro_momentum'] = self.direction
            tags['micro_momentum_strength'] = self.strength
        if self.move_quality != 'UNKNOWN':
            tags['move_quality'] = self.move_quality
            tags['move_quality_size_mult'] = self.move_size_mult
            tags['move_vol_ratio'] = self.vol_ratio
            tags['move_shrinkage'] = self.shrinkage
            tags['move_wick_ratio'] = self.wick_ratio
        return tags


class MicroMomentumDetector:
    """
    Detects micro momentum from recent 1m candles.
    
    Used when no strong displacement is detected to determine
    the current momentum direction from candle structure.
    """
    
    def __init__(self):
        """Initialize detector."""
        self._cache: Optional[MicroMomentumResult] = None
        self._cache_tick: int = 0
    
    def detect(
        self,
        candles_1m: List[dict],
        tick_count: int,
        recalc_interval: int = 3
    ) -> MicroMomentumResult:
        """
        Detect micro momentum from recent candles.
        
        Uses caching to reduce computation - only recalculates every N ticks.
        
        Args:
            candles_1m: List of 1m candle dictionaries
            tick_count: Current tick count
            recalc_interval: Only recalculate every N ticks
        
        Returns:
            MicroMomentumResult with momentum details
        """
        import numpy as np
        
        # Check cache
        should_calc = (tick_count % recalc_interval == 0) or self._cache is None
        
        if not should_calc and self._cache is not None:
            return self._cache
        
        result = MicroMomentumResult()
        
        if not candles_1m or len(candles_1m) < 10:
            self._cache = result
            return result
        
        try:
            # Extract data using numpy for efficiency
            recent = candles_1m[-10:]
            highs = np.array([c['high'] for c in recent])
            lows = np.array([c['low'] for c in recent])
            closes = np.array([c['close'] for c in recent])
            opens = np.array([c['open'] for c in recent])
            volumes = np.array([c.get('volume', 1.0) for c in recent])
            
            # Calculate differences
            high_diffs = np.diff(highs)
            low_diffs = np.diff(lows)
            close_diffs = np.diff(closes)
            
            # Count HH/HL/LL/LH (last 7 candles)
            hh_count = np.sum(high_diffs[-6:] > 0)
            hl_count = np.sum(low_diffs[-6:] > 0)
            ll_count = np.sum(low_diffs[-6:] < 0)
            lh_count = np.sum(high_diffs[-6:] < 0)
            
            bullish_score = hh_count + hl_count
            bearish_score = ll_count + lh_count
            
            # Determine direction
            if bullish_score >= 4 and bullish_score > bearish_score + 2:
                result.direction = 'LONG'
            elif bearish_score >= 4 and bearish_score > bullish_score + 2:
                result.direction = 'SHORT'
            
            # Determine strength from closes
            up_closes = np.sum(close_diffs > 0)
            if result.direction == 'LONG' and up_closes >= 4:
                result.strength = 'STRONG'
            elif result.direction == 'SHORT' and up_closes <= 2:
                result.strength = 'STRONG'
            
            # Move quality analysis (if direction detected)
            if result.direction:
                result = self._analyze_move_quality(
                    result, closes, opens, highs, lows, volumes
                )
        
        except Exception as e:
            logger.debug(f"Micro momentum detection error: {e}")
        
        self._cache = result
        self._cache_tick = tick_count
        return result
    
    def _analyze_move_quality(
        self,
        result: MicroMomentumResult,
        closes: 'np.ndarray',
        opens: 'np.ndarray', 
        highs: 'np.ndarray',
        lows: 'np.ndarray',
        volumes: 'np.ndarray'
    ) -> MicroMomentumResult:
        """Analyze move quality to determine if FRESH, DEVELOPING, or EXHAUSTING."""
        import numpy as np
        
        # Body sizes
        body_sizes = np.abs(closes - opens)
        avg_body = np.mean(body_sizes[:-1]) if len(body_sizes) > 1 else 1.0
        last_bodies = body_sizes[-3:]
        
        # Volume profile
        vol_early = np.mean(volumes[-6:-3]) if len(volumes) >= 6 else np.mean(volumes[:3])
        vol_late = np.mean(volumes[-3:])
        result.vol_ratio = vol_late / vol_early if vol_early > 0 else 1.0
        
        # Candle shrinkage
        prev_bodies_mean = np.mean(last_bodies[:-1]) if len(last_bodies) > 1 else 1.0
        result.shrinkage = last_bodies[-1] / prev_bodies_mean if prev_bodies_mean > 0 else 1.0
        
        # Wick analysis
        if result.direction == 'LONG':
            upper_wicks = highs - np.maximum(opens, closes)
            result.wick_ratio = upper_wicks[-1] / body_sizes[-1] if body_sizes[-1] > 0 else 0
        else:
            lower_wicks = np.minimum(opens, closes) - lows
            result.wick_ratio = lower_wicks[-1] / body_sizes[-1] if body_sizes[-1] > 0 else 0
        
        # Determine quality
        if result.vol_ratio >= 1.0 and result.shrinkage >= 0.8 and result.wick_ratio < 0.5:
            result.move_quality = 'FRESH'
            result.move_size_mult = 1.0
        elif result.vol_ratio >= 0.7 and result.shrinkage >= 0.5:
            result.move_quality = 'DEVELOPING'
            result.move_size_mult = 0.7
        elif result.vol_ratio < 0.7 or result.shrinkage < 0.5 or result.wick_ratio >= 1.0:
            result.move_quality = 'EXHAUSTING'
            result.move_size_mult = 0.0  # Don't enter
        
        return result
    
    def get_cached(self) -> Optional[MicroMomentumResult]:
        """Get cached result without recalculating."""
        return self._cache
