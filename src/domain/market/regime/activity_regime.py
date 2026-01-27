"""
Activity Regime Model - Replaces session-based validation

This module implements continuous market activity analysis for 24/7 crypto markets.
It replaces the London/NY/Asia session paradigm with a data-driven activity regime model.

Core principle: Instead of fixed time windows, measure actual market activity
using price range, volume, and volatility to determine tradability.

Activity Score Components:
1. Activity Range: max(high[-N:]) - min(low[-N:]) where N = 4-6 hours of candles
2. Volatility Regime: ATR vs historical percentiles (not fixed threshold)
3. Volume Profile: Current vs moving average (regime, not absolute)
4. Momentum Quality: Directional strength and consistency

The output is a continuous edge_score from 0.0 to 1.0:
- 0.0-0.3: Low activity regime (avoid or reduce size)
- 0.3-0.6: Normal activity regime (standard operations)
- 0.6-1.0: High activity regime (full engagement)
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from datetime import datetime, UTC
import numpy as np


@dataclass
class ActivityMetrics:
    """Metrics describing current market activity regime"""
    activity_range: float = 0.0          # Price range over lookback period
    activity_range_ma: float = 0.0       # Moving average of activity range
    activity_range_ratio: float = 0.0    # Current / MA ratio
    
    atr_current: float = 0.0             # Current ATR
    atr_percentile: float = 0.0          # Percentile vs history (0-100)
    
    volume_current: float = 0.0          # Current volume (normalized)
    volume_ratio: float = 0.0            # Current / MA ratio
    
    momentum_strength: float = 0.0       # Directional strength (0-1)
    momentum_consistency: float = 0.0    # Consistency of direction (0-1)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging"""
        return {
            'activity_range': round(self.activity_range, 2),
            'activity_range_ratio': round(self.activity_range_ratio, 2),
            'atr_percentile': round(self.atr_percentile, 1),
            'volume_ratio': round(self.volume_ratio, 2),
            'momentum_strength': round(self.momentum_strength, 3),
            'momentum_consistency': round(self.momentum_consistency, 3)
        }


@dataclass
class EdgeScore:
    """
    Continuous edge score replacing binary validation.
    
    Score ranges:
    - 0.0-0.3: LOW regime - minimal/no trading
    - 0.3-0.6: NORMAL regime - reduced or standard size
    - 0.6-1.0: HIGH regime - full engagement
    """
    value: float = 0.5                   # Continuous score 0-1
    regime: str = "NORMAL"               # LOW, NORMAL, HIGH
    components: dict | None = None              # Breakdown of score components
    blocking_reasons: List[str] = field(default_factory=list)   # Reasons if score < threshold
    
    def __post_init__(self):
        if self.components is None:
            self.components = {}
        
        # Auto-classify regime based on value
        if self.value < 0.3:
            self.regime = "LOW"
        elif self.value < 0.6:
            self.regime = "NORMAL"
        else:
            self.regime = "HIGH"
    
    def can_trade(self, min_threshold: float = 0.25) -> bool:
        """Returns True if score meets minimum threshold"""
        return self.value >= min_threshold
    
    def get_size_multiplier(self) -> float:
        """Returns position size multiplier based on regime"""
        if self.value < 0.25:
            return 0.0   # No trade
        elif self.value < 0.4:
            return 0.5   # Half size
        elif self.value < 0.6:
            return 0.75  # 75% size
        else:
            return 1.0   # Full size
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging"""
        return {
            'value': round(self.value, 3),
            'regime': self.regime,
            'can_trade': self.can_trade(),
            'size_multiplier': self.get_size_multiplier(),
            'components': self.components,
            'blocking_reasons': self.blocking_reasons
        }


class ActivityRegimeAnalyzer:
    """
    Analyzes market activity to determine tradability regime.
    
    Replaces session-based logic with continuous activity measurement.
    Designed for 24/7 crypto markets where session boundaries are arbitrary.
    """
    
    def __init__(self,
                 activity_lookback_hours: int = 4,
                 atr_history_days: int = 14,
                 volume_ma_periods: int = 20,
                 ignore_volume: bool = False):
        """
        Initialize the analyzer.
        
        Args:
            activity_lookback_hours: Hours to look back for activity range (default 4)
            atr_history_days: Days of ATR history for percentile calculation
            volume_ma_periods: Periods for volume moving average
            ignore_volume: If True, ignore volume component (e.g., when data has V=0)
        """
        self.activity_lookback_hours = activity_lookback_hours
        self.atr_history_days = atr_history_days
        self.volume_ma_periods = volume_ma_periods
        self.ignore_volume = ignore_volume
        
        # Scoring weights (sum to 1.0)
        if ignore_volume:
            # Redistribute volume weight to other components
            self.WEIGHT_ACTIVITY_RANGE = 0.43  # 0.35 + 0.08
            self.WEIGHT_VOLATILITY = 0.32      # 0.25 + 0.07
            self.WEIGHT_VOLUME = 0.0           # Ignored
            self.WEIGHT_MOMENTUM = 0.25        # 0.20 + 0.05
        else:
            self.WEIGHT_ACTIVITY_RANGE = 0.35
            self.WEIGHT_VOLATILITY = 0.25
            self.WEIGHT_VOLUME = 0.20
            self.WEIGHT_MOMENTUM = 0.20
        
        # Thresholds for component scores
        # RETUNED (Jan 19, 2026): Much more permissive - old values blocked 90%+ of winners
        self.MIN_ACTIVITY_RATIO = 0.25   # Was 0.5 - too strict for 24/7 market
        self.MIN_ATR_PERCENTILE = 5.0    # Was 20 - low ATR != bad trade
        self.MIN_VOLUME_RATIO = 0.15     # Was 0.3 - BTC always liquid enough
        
    def calculate_activity_range(self,
                                highs: np.ndarray,
                                lows: np.ndarray,
                                candle_minutes: int = 1) -> Tuple[float, float]:
        """
        Calculate activity range (replaces session_range).
        
        Activity Range = max(highs[-N:]) - min(lows[-N:])
        Where N = number of candles in lookback period
        
        POLICY: Returns (0, 0) when data is insufficient.
        This results in LOW edge score, blocking trades.
        
        Args:
            highs: Array of high prices
            lows: Array of low prices
            candle_minutes: Minutes per candle (1 for 1m, 60 for 1h)
        
        Returns:
            Tuple (current_range, moving_average_range)
        """
        # POLICY: Insufficient data = return zeros (will result in LOW edge score)
        MIN_REQUIRED = 60  # At least 1 hour of 1m data
        if len(highs) < MIN_REQUIRED or len(lows) < MIN_REQUIRED:
            import logging
            logging.getLogger(__name__).warning(
                f"Insufficient data for activity range: {len(highs)} candles < {MIN_REQUIRED}"
            )
            return 0.0, 1.0  # Return low ratio (0/1) to signal insufficient data
        
        # Calculate lookback in candles
        candles_per_hour = 60 // candle_minutes
        lookback_candles = self.activity_lookback_hours * candles_per_hour
        lookback_candles = min(lookback_candles, len(highs))
        
        # Current activity range
        recent_highs = highs[-lookback_candles:]
        recent_lows = lows[-lookback_candles:]
        current_range = float(np.max(recent_highs) - np.min(recent_lows))
        
        # Moving average of activity range (rolling windows)
        ma_window = min(24 * candles_per_hour, len(highs) // 2)  # Up to 24 hours
        if ma_window < lookback_candles:
            return current_range, current_range
        
        ranges = []
        for i in range(ma_window, len(highs)):
            window_high = np.max(highs[i-lookback_candles:i])
            window_low = np.min(lows[i-lookback_candles:i])
            ranges.append(window_high - window_low)
        
        if len(ranges) > 0:
            ma_range = float(np.mean(ranges))
        else:
            ma_range = current_range
        
        return current_range, ma_range
    
    def calculate_atr_percentile(self,
                                atr_values: np.ndarray,
                                current_atr: float) -> float:
        """
        Calculate ATR percentile vs history.
        
        POLICY: Returns low percentile (10) when data is insufficient.
        This results in reduced edge score.
        
        Args:
            atr_values: Historical ATR values
            current_atr: Current ATR value
        
        Returns:
            Percentile (0-100)
        """
        MIN_ATR_HISTORY = 50  # Need enough history for meaningful percentile
        
        if len(atr_values) < MIN_ATR_HISTORY or np.isnan(current_atr) or current_atr <= 0:
            import logging
            logging.getLogger(__name__).debug(
                f"Insufficient ATR data: {len(atr_values)} samples, current={current_atr}"
            )
            return 10.0  # Return LOW percentile to reduce edge score (not neutral 50)
        
        valid_atrs = atr_values[~np.isnan(atr_values)]
        if len(valid_atrs) < 10:
            return 50.0
        
        percentile = float(np.sum(valid_atrs < current_atr) / len(valid_atrs) * 100)
        return percentile
    
    def calculate_volume_ratio(self,
                              volumes: np.ndarray) -> Tuple[float, float]:
        """
        Calculate current volume vs moving average.
        
        Args:
            volumes: Array of volume values
        
        Returns:
            Tuple (current_volume, volume_ratio)
        """
        if len(volumes) < self.volume_ma_periods + 1:
            return 0.0, 1.0  # Default to neutral
        
        current_vol = float(volumes[-1])
        ma_vol = float(np.mean(volumes[-self.volume_ma_periods-1:-1]))
        
        if ma_vol <= 0:
            return current_vol, 1.0
        
        ratio = current_vol / ma_vol
        return current_vol, ratio
    
    def calculate_momentum_quality(self,
                                  closes: np.ndarray,
                                  lookback: int = 20) -> Tuple[float, float]:
        """
        Calculate momentum strength and consistency.
        
        Strength: Magnitude of price change vs ATR
        Consistency: Percentage of candles moving in same direction
        
        Args:
            closes: Array of close prices
            lookback: Candles to analyze
        
        Returns:
            Tuple (strength 0-1, consistency 0-1)
        """
        if len(closes) < lookback + 1:
            return 0.5, 0.5  # Neutral defaults
        
        recent = closes[-lookback:]
        
        # Price change
        price_change = recent[-1] - recent[0]
        price_range = np.max(recent) - np.min(recent)
        
        if price_range <= 0:
            return 0.5, 0.5
        
        # Strength: normalized price change
        strength = min(abs(price_change) / price_range, 1.0)
        
        # Consistency: direction agreement
        changes = np.diff(recent)
        if len(changes) == 0:
            return float(strength), 0.5  # Return neutral consistency if no changes
        if price_change >= 0:
            consistent = np.sum(changes >= 0) / len(changes)
        else:
            consistent = np.sum(changes <= 0) / len(changes)
        
        return float(strength), float(consistent)
    
    def analyze(self,
               highs: np.ndarray,
               lows: np.ndarray,
               closes: np.ndarray,
               volumes: np.ndarray,
               atr_values: np.ndarray,
               current_atr: float,
               candle_minutes: int = 1) -> Tuple[ActivityMetrics, EdgeScore]:
        """
        Full activity regime analysis.
        
        POLICY: If data is insufficient, returns EdgeScore with low value
        and explicit "insufficient data" in blocking_reasons.
        
        Args:
            highs: High prices array
            lows: Low prices array
            closes: Close prices array
            volumes: Volume array
            atr_values: Historical ATR values
            current_atr: Current ATR
            candle_minutes: Minutes per candle
        
        Returns:
            Tuple (ActivityMetrics, EdgeScore)
            EdgeScore.blocking_reasons will contain "Insufficient data" if applicable
        """
        import logging
        logger = logging.getLogger(__name__)
        
        metrics = ActivityMetrics()
        blocking_reasons = []
        component_scores = {}
        
        # POLICY: Check data sufficiency FIRST - explicit blocking if insufficient
        MIN_CANDLES_REQUIRED = 60  # At least 1 hour of 1m data
        MIN_ATR_HISTORY = 50
        
        data_insufficient = False
        
        if len(highs) < MIN_CANDLES_REQUIRED or len(lows) < MIN_CANDLES_REQUIRED:
            blocking_reasons.append(f"Insufficient price data: {len(highs)} candles < {MIN_CANDLES_REQUIRED} required")
            data_insufficient = True
            logger.warning(f"[ACTIVITY] Insufficient data for edge analysis: {len(highs)} candles")
        
        if len(atr_values) < MIN_ATR_HISTORY:
            blocking_reasons.append(f"Insufficient ATR history: {len(atr_values)} < {MIN_ATR_HISTORY} required")
            data_insufficient = True
        
        # If data is insufficient, return LOW edge score to block trading
        if data_insufficient:
            edge_score = EdgeScore(
                value=0.0,  # Explicitly LOW to block trading
                components={'data_quality': 'INSUFFICIENT'},
                blocking_reasons=blocking_reasons
            )
            return metrics, edge_score
        
        # 1. Activity Range Score
        # RETUNED (Jan 19, 2026): More forgiving scoring curve
        act_range, act_ma = self.calculate_activity_range(highs, lows, candle_minutes)
        metrics.activity_range = act_range
        metrics.activity_range_ma = act_ma
        metrics.activity_range_ratio = act_range / act_ma if act_ma > 0 else 1.0
        
        # Score: Start at 0.3 even at ratio=0.25, reach 1.0 at ratio=1.2
        # This gives more credit to quieter but still valid markets
        range_score = max(0.2, min(1, 0.3 + (metrics.activity_range_ratio - 0.25) * 0.7))
        component_scores['activity_range'] = round(range_score, 3)
        
        if metrics.activity_range_ratio < self.MIN_ACTIVITY_RATIO:
            blocking_reasons.append(f"Activity range below threshold vs MA")
        
        # 2. Volatility Score (ATR Percentile)
        # RETUNED (Jan 19, 2026): Low ATR doesn't mean bad trade, just lower volatility
        metrics.atr_current = current_atr
        metrics.atr_percentile = self.calculate_atr_percentile(atr_values, current_atr)
        
        # Score: Start at 0.3 even at percentile 5, reach 1.0 at percentile 60+
        # Lower ATR can still be good trades with tighter stops
        volatility_score = max(0.2, min(1, 0.3 + metrics.atr_percentile / 85))
        component_scores['volatility'] = round(volatility_score, 3)
        
        if metrics.atr_percentile < self.MIN_ATR_PERCENTILE:
            blocking_reasons.append(f"ATR below minimum percentile")
        
        # 3. Volume Score (skipped if ignore_volume=True or volume data is invalid)
        if self.ignore_volume or np.all(volumes == 0) or np.all(np.isnan(volumes)):
            # Skip volume component entirely
            vol_current, vol_ratio = 0.0, 1.0
            volume_score = 0.0  # Won't affect score due to weight=0
            component_scores['volume'] = 'IGNORED'
        else:
            vol_current, vol_ratio = self.calculate_volume_ratio(volumes)
            # RETUNED (Jan 19, 2026): More forgiving - BTC is always liquid
            # Score: Start at 0.3 even at ratio=0.15, reach 1.0 at ratio=1.2
            volume_score = max(0.25, min(1, 0.3 + (vol_ratio - 0.15) * 0.66))
            component_scores['volume'] = round(volume_score, 3)
            
            if vol_ratio < self.MIN_VOLUME_RATIO:
                blocking_reasons.append(f"Volume below threshold vs MA")
        
        metrics.volume_current = vol_current
        metrics.volume_ratio = vol_ratio
        
        # 4. Momentum Score
        strength, consistency = self.calculate_momentum_quality(closes)
        metrics.momentum_strength = strength
        metrics.momentum_consistency = consistency
        
        # Combined momentum score - boost slightly as momentum is a strong signal
        momentum_score = min(1.0, (strength + consistency) / 2 + 0.1)
        component_scores['momentum'] = round(momentum_score, 3)
        
        # 5. Calculate weighted edge score
        # RETUNED (Jan 19, 2026): Add base score of 0.15 to prevent overly harsh blocking
        # The filters already passed (HTF, MTF, LTF) which means setup has merit
        base_score = 0.15  # Give credit for passing structural filters
        edge_value = base_score + (
            self.WEIGHT_ACTIVITY_RANGE * range_score +
            self.WEIGHT_VOLATILITY * volatility_score +
            self.WEIGHT_VOLUME * volume_score +
            self.WEIGHT_MOMENTUM * momentum_score
        ) * 0.85  # Scale to leave room for base score
        
        # Cap at 1.0
        edge_value = min(1.0, edge_value)
        
        edge_score = EdgeScore(
            value=round(edge_value, 3),
            components=component_scores,
            blocking_reasons=blocking_reasons
        )
        
        return metrics, edge_score
    
    def quick_check(self,
                   highs: np.ndarray,
                   lows: np.ndarray,
                   candle_minutes: int = 1) -> bool:
        """
        Quick activity check without full analysis.
        Useful for pre-screening before detailed analysis.
        
        POLICY: Returns False (block) if data is insufficient.
        This prevents trading with poor data.
        
        Args:
            highs: High prices array
            lows: Low prices array
            candle_minutes: Minutes per candle
        
        Returns:
            True if activity level is acceptable, False otherwise
        """
        MIN_CANDLES = 60  # At least 1 hour of 1m data
        
        if len(highs) < MIN_CANDLES:
            import logging
            logging.getLogger(__name__).warning(
                f"[ACTIVITY] quick_check: Insufficient data ({len(highs)} < {MIN_CANDLES}), blocking"
            )
            return False  # POLICY: Block when data is insufficient
        
        act_range, act_ma = self.calculate_activity_range(highs, lows, candle_minutes)
        ratio = act_range / act_ma if act_ma > 0 else 1.0
        
        return ratio >= self.MIN_ACTIVITY_RATIO


class EnforcementMode:
    """
    Enforcement mode configuration for different operational contexts.
    
    Backtest: Relaxed thresholds, allow most trades for strategy evaluation
    Forward: Normal thresholds, standard operation
    Live: Strict thresholds, conservative risk management
    """
    
    BACKTEST = "backtest"
    FORWARD = "forward"
    LIVE = "live"
    
    @staticmethod
    def get_thresholds(mode: str) -> dict:
        """Get enforcement thresholds for mode
        
        RETUNED (Jan 4, 2026): Relaxed thresholds to allow viable intraday frequency.
        Previous settings blocked >95% of valid setups.
        
        Target: 5-10 trades/week instead of 1/month
        """
        if mode == EnforcementMode.BACKTEST:
            return {
                'min_edge_score': 0.10,      # Very relaxed (was 0.15)
                'min_activity_ratio': 0.20,   # Was 0.3 - more relaxed
                'min_atr_percentile': 0.0,    # DISABLED for backtest - ATR percentile check kills trades
                'min_volume_ratio': 0.10,     # Was 0.2 - almost disabled (Kraken has V=0)
                'max_drawdown': 0.30,         # 30% (was 25%)
            }
        elif mode == EnforcementMode.FORWARD:
            # RETUNED (Jan 19, 2026): Much more relaxed to capture winning setups
            # Previous settings blocked 90%+ of 1R+ winners
            return {
                'min_edge_score': 0.10,       # Was 0.20 - too strict, blocked 1.6R winners
                'min_activity_ratio': 0.20,   # Was 0.35 - market is 24/7, always some activity
                'min_atr_percentile': 5.0,    # Was 15 - low ATR != bad trade, just lower volatility
                'min_volume_ratio': 0.15,     # Was 0.25 - volume varies, not critical for BTC
                'max_drawdown': 0.25,         # 25% unchanged
            }
        else:  # LIVE
            # RETUNED (Jan 19, 2026): More permissive to capture edge
            # Still slightly more conservative than FORWARD
            return {
                'min_edge_score': 0.12,       # Was 0.25 - blocking too many good setups
                'min_activity_ratio': 0.25,   # Was 0.40 - allow quieter periods
                'min_atr_percentile': 8.0,    # Was 20 - ATR percentile != trade quality
                'min_volume_ratio': 0.20,     # Was 0.30 - BTC liquid enough always
                'max_drawdown': 0.20,         # 20% unchanged
            }


# Singleton instance (for backwards compatibility)
# Prefer explicit instantiation for new code
_analyzer_instance: Optional[ActivityRegimeAnalyzer] = None

def get_analyzer(ignore_volume: bool = False, instance: Optional[ActivityRegimeAnalyzer] = None) -> ActivityRegimeAnalyzer:
    """
    Get or create ActivityRegimeAnalyzer instance.
    
    Args:
        ignore_volume: If True, ignore volume component in scoring.
        instance: Optional explicit instance to use (DI pattern)
        
    Returns:
        ActivityRegimeAnalyzer instance
    """
    global _analyzer_instance
    
    # DI: si se pasa instancia explícita, usarla
    if instance is not None:
        return instance
    
    # Singleton fallback
    if _analyzer_instance is None or _analyzer_instance.ignore_volume != ignore_volume:
        _analyzer_instance = ActivityRegimeAnalyzer(ignore_volume=ignore_volume)
    return _analyzer_instance


def reset_analyzer():
    """Reset singleton (for testing)"""
    global _analyzer_instance
    _analyzer_instance = None
