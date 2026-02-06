"""
Trading Regime System - Enhanced regime classification with transitional states

╔══════════════════════════════════════════════════════════════════════════════╗
║ ⛔ DEPRECATED - Feb 05, 2026                                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ REASON: This entire module is UNUSED in production (application/live/).     ║
║                                                                              ║
║ ANALYSIS:                                                                    ║
║ - TradingRegimeClassifier: Never imported in application layer              ║
║ - get_regime_classifier(): Conflicts with 2 other functions with same name  ║
║ - REGIME_CHARACTERISTICS: Only used by frequency_controller.py (also dead)  ║
║ - TrendDirection: Moved to unified_constants.py (SINGLE SOURCE)             ║
║                                                                              ║
║ REPLACEMENT:                                                                 ║
║ - Use TrendIntelligence for trend cycle detection (trend_intelligence.py)   ║
║ - Use EdgeRegimeClassifier for edge validity (edge_regime_classifier.py)    ║
║ - Use DailyRegimeDetector for daily regime (trading_config.py)              ║
║                                                                              ║
║ MIGRATION: File kept for backward compatibility. Will be removed in v2.2.0  ║
╚══════════════════════════════════════════════════════════════════════════════╝

OLD DESCRIPTION:
----------------
PRINCIPLE (Jan 20, 2026):
Detect regime changes EARLIER, not later.
The system should identify regime transitions BEFORE they fully materialize.

REGIMES:
- RANGE_PASIVO: Low volatility consolidation, minimal opportunity
- RANGE_ACTIVO: Range with good swings, mean-reversion friendly
- TREND: Directional movement, momentum strategies
- EXHAUSTION: Trend losing steam, prepare for reversal
- TRANSITIONAL: Between states, reduced exposure

FREQUENCY CONTROL BY REGIME:
- TREND: More trades allowed (riding momentum)
- RANGE_ACTIVO: Moderate frequency (selective mean-reversion)
- RANGE_PASIVO: Limited trades (low opportunity)
- EXHAUSTION: Very limited (high risk of reversal)
- TRANSITIONAL: Minimal (wait for clarity)
"""

import warnings
warnings.warn(
    "trading_regime.py is DEPRECATED. Use TrendIntelligence or EdgeRegimeClassifier instead.",
    DeprecationWarning,
    stacklevel=2
)

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC
import numpy as np
import logging

logger = logging.getLogger(__name__)


class TradingRegime(str, Enum):
    """Enhanced trading regime classification."""
    RANGE_PASIVO = "RANGE_PASIVO"       # Low vol consolidation
    RANGE_ACTIVO = "RANGE_ACTIVO"       # Active range with swings
    TREND = "TREND"                      # Directional movement
    EXHAUSTION = "EXHAUSTION"            # Trend ending
    TRANSITIONAL = "TRANSITIONAL"        # Between regimes
    UNKNOWN = "UNKNOWN"                  # Insufficient data


# 🔥 FIX (Feb 05): Import TrendDirection from unified source
from src.config.unified_constants import TrendDirection

# ⚠️ DEPRECATED: TrendDirection was previously defined here.
# Now imported from unified_constants.py - SINGLE SOURCE OF TRUTH
# Old definition kept commented for reference:
# class TrendDirection(str, Enum):
#     BULLISH = "BULLISH"
#     BEARISH = "BEARISH"
#     NEUTRAL = "NEUTRAL"


@dataclass
class RegimeCharacteristics:
    """Characteristics and trading parameters for each regime."""
    regime: TradingRegime
    
    # Trade frequency control
    max_trades_per_session: int           # Max new entries per session
    min_minutes_between_trades: int       # Minimum spacing
    
    # Sizing adjustments
    base_size_multiplier: float           # 1.0 = full, 0.5 = half
    probe_allowed: bool                   # Allow probe entries?
    
    # RR adjustments
    min_rr_required: float                # Minimum acceptable RR
    
    # Strategy preferences
    prefer_mean_reversion: bool           # Mean-reversion strategies
    prefer_momentum: bool                 # Momentum strategies
    
    # Risk adjustments
    trailing_stop_aggressive: bool        # Use aggressive trailing?
    quick_profit_taking: bool             # Take profits faster?


# Pre-defined regime characteristics
REGIME_CHARACTERISTICS: Dict[TradingRegime, RegimeCharacteristics] = {
    TradingRegime.TREND: RegimeCharacteristics(
        regime=TradingRegime.TREND,
        max_trades_per_session=5,
        min_minutes_between_trades=15,
        base_size_multiplier=1.0,
        probe_allowed=True,
        min_rr_required=1.0,
        prefer_mean_reversion=False,
        prefer_momentum=True,
        trailing_stop_aggressive=False,  # Let winners run
        quick_profit_taking=False
    ),
    TradingRegime.RANGE_ACTIVO: RegimeCharacteristics(
        regime=TradingRegime.RANGE_ACTIVO,
        max_trades_per_session=4,
        min_minutes_between_trades=20,
        base_size_multiplier=0.8,
        probe_allowed=True,
        min_rr_required=0.8,
        prefer_mean_reversion=True,
        prefer_momentum=False,
        trailing_stop_aggressive=True,
        quick_profit_taking=True
    ),
    TradingRegime.RANGE_PASIVO: RegimeCharacteristics(
        regime=TradingRegime.RANGE_PASIVO,
        max_trades_per_session=2,
        min_minutes_between_trades=45,
        base_size_multiplier=0.5,
        probe_allowed=True,
        min_rr_required=0.6,
        prefer_mean_reversion=True,
        prefer_momentum=False,
        trailing_stop_aggressive=True,
        quick_profit_taking=True
    ),
    TradingRegime.EXHAUSTION: RegimeCharacteristics(
        regime=TradingRegime.EXHAUSTION,
        max_trades_per_session=2,
        min_minutes_between_trades=60,
        base_size_multiplier=0.5,
        probe_allowed=True,
        min_rr_required=1.2,  # Need better RR in risky environment
        prefer_mean_reversion=True,  # Fade exhaustion
        prefer_momentum=False,
        trailing_stop_aggressive=True,
        quick_profit_taking=True
    ),
    TradingRegime.TRANSITIONAL: RegimeCharacteristics(
        regime=TradingRegime.TRANSITIONAL,
        max_trades_per_session=1,
        min_minutes_between_trades=90,
        base_size_multiplier=0.25,  # Probe only
        probe_allowed=True,
        min_rr_required=1.2,
        prefer_mean_reversion=False,
        prefer_momentum=False,
        trailing_stop_aggressive=True,
        quick_profit_taking=True
    ),
    TradingRegime.UNKNOWN: RegimeCharacteristics(
        regime=TradingRegime.UNKNOWN,
        max_trades_per_session=1,
        min_minutes_between_trades=120,
        base_size_multiplier=0.25,
        probe_allowed=True,
        min_rr_required=1.5,
        prefer_mean_reversion=False,
        prefer_momentum=False,
        trailing_stop_aggressive=True,
        quick_profit_taking=True
    )
}


@dataclass
class RegimeAnalysis:
    """Complete regime analysis result."""
    regime: TradingRegime
    confidence: float                     # 0-1 confidence in classification
    characteristics: RegimeCharacteristics
    
    # Trending info (when applicable)
    trend_direction: TrendDirection = TrendDirection.NEUTRAL
    trend_strength: float = 0.0           # 0-1
    
    # Range info (when applicable)
    range_high: float = 0.0
    range_low: float = 0.0
    current_position_in_range: float = 0.5  # 0=low, 1=high
    
    # Transitional indicators
    regime_age_minutes: int = 0
    stability_score: float = 0.5          # 0=unstable, 1=stable
    
    # Early warning signals
    exhaustion_warning: bool = False
    breakout_imminent: bool = False
    volatility_expanding: bool = False
    
    # Trading guidance
    recommended_direction: Optional[str] = None  # 'LONG', 'SHORT', None
    avoid_direction: Optional[str] = None        # Direction to avoid
    
    def __str__(self) -> str:
        trend_str = f", {self.trend_direction.value}" if self.regime == TradingRegime.TREND else ""
        warnings = []
        if self.exhaustion_warning:
            warnings.append("⚠️exhaustion")
        if self.breakout_imminent:
            warnings.append("🚀breakout?")
        if self.volatility_expanding:
            warnings.append("📈vol_exp")
        warn_str = f" [{', '.join(warnings)}]" if warnings else ""
        return f"Regime: {self.regime.value}{trend_str} ({self.confidence:.0%}){warn_str}"


class TradingRegimeClassifier:
    """
    Enhanced regime classifier with early detection of regime transitions.
    
    PRINCIPLE: Detect changes EARLY, not late.
    
    Detection hierarchy:
    1. ADX for trend strength
    2. BB Width for volatility state
    3. ATR ratio for momentum
    4. Price position relative to MAs
    5. Momentum indicators (RSI, MACD)
    
    Usage:
        classifier = TradingRegimeClassifier()
        analysis = classifier.classify(
            adx=25.0,
            adx_slope=0.5,
            bb_width_percentile=0.6,
            atr_ratio=1.2,
            price_vs_ema20=0.002,
            price_vs_ema50=0.005,
            rsi=55,
            macd_histogram=0.001
        )
        
        if analysis.regime == TradingRegime.TREND:
            # Use momentum strategies
        elif analysis.regime == TradingRegime.RANGE_ACTIVO:
            # Use mean-reversion
    """
    
    # =========================================================================
    # THRESHOLDS FOR CLASSIFICATION
    # =========================================================================
    
    # ADX thresholds (trend strength)
    ADX_TREND_MIN = 25.0              # Below = ranging
    ADX_STRONG_TREND = 35.0           # Strong trend
    ADX_EXHAUSTION = 50.0             # Potential exhaustion
    ADX_SLOPE_THRESHOLD = 0.3         # Significant ADX change
    
    # BB Width percentile thresholds
    BB_WIDTH_COMPRESSION = 0.3        # Low volatility
    BB_WIDTH_NORMAL = 0.5             # Normal volatility
    BB_WIDTH_EXPANSION = 0.7          # High volatility
    
    # ATR ratio thresholds
    ATR_RATIO_LOW = 0.8               # Below normal volatility
    ATR_RATIO_HIGH = 1.5              # Above normal volatility
    ATR_RATIO_EXTREME = 2.0           # Extreme volatility
    
    # Range activity thresholds
    RANGE_SWING_MIN = 0.003           # 0.3% swing = active range
    
    def __init__(self):
        self.current_regime = TradingRegime.UNKNOWN
        self.regime_start_time: Optional[datetime] = None
        self.regime_history: List[Tuple[TradingRegime, datetime]] = []
        self._last_analysis: Optional[RegimeAnalysis] = None
        
        # Smoothing: require multiple confirmations for regime change
        self._pending_regime: Optional[TradingRegime] = None
        self._pending_count = 0
        self.REGIME_CHANGE_CONFIRMATIONS = 2
    
    def classify(self,
                 # ADX metrics
                 adx: float = 20.0,
                 adx_slope: float = 0.0,           # Change in ADX over N periods
                 plus_di: float = 20.0,
                 minus_di: float = 20.0,
                 
                 # Bollinger Width metrics
                 bb_width_percentile: float = 0.5,  # Current width vs historical
                 bb_expanding: bool = False,        # Is BB expanding?
                 
                 # ATR metrics
                 atr_ratio: float = 1.0,           # current_atr / avg_atr
                 
                 # Price position
                 price_vs_ema20: float = 0.0,      # % distance from EMA20
                 price_vs_ema50: float = 0.0,      # % distance from EMA50
                 price_vs_ema200: float = 0.0,     # % distance from EMA200
                 
                 # Momentum indicators
                 rsi: float = 50.0,
                 macd_histogram: float = 0.0,
                 
                 # Additional context
                 recent_swing_size: float = 0.0,   # Size of recent swings
                 range_high: float = 0.0,
                 range_low: float = 0.0,
                 current_price: float = 0.0,
                 
                 **kwargs) -> RegimeAnalysis:
        """
        Classify current market regime.
        
        Returns:
            RegimeAnalysis with regime, confidence, and trading guidance
        """
        # Calculate intermediate values
        di_diff = plus_di - minus_di
        trend_direction = TrendDirection.BULLISH if di_diff > 5 else (
            TrendDirection.BEARISH if di_diff < -5 else TrendDirection.NEUTRAL
        )
        
        # Calculate position in range
        range_size = range_high - range_low if range_high > range_low else 1.0
        position_in_range = (current_price - range_low) / range_size if range_size > 0 else 0.5
        position_in_range = max(0.0, min(1.0, position_in_range))
        
        # =====================================================================
        # REGIME CLASSIFICATION LOGIC
        # =====================================================================
        
        regime = TradingRegime.UNKNOWN
        confidence = 0.5
        exhaustion_warning = False
        breakout_imminent = False
        volatility_expanding = bb_expanding
        
        # ----- TREND DETECTION -----
        if adx >= self.ADX_TREND_MIN:
            if adx >= self.ADX_EXHAUSTION and adx_slope < 0:
                # ADX very high but declining = exhaustion
                regime = TradingRegime.EXHAUSTION
                exhaustion_warning = True
                confidence = 0.7 + min(0.2, (adx - self.ADX_EXHAUSTION) / 20)
            elif adx >= self.ADX_STRONG_TREND:
                # Strong trend
                regime = TradingRegime.TREND
                confidence = 0.75 + min(0.2, (adx - self.ADX_STRONG_TREND) / 30)
                # Check for exhaustion warning
                if rsi > 75 or rsi < 25:
                    exhaustion_warning = True
            else:
                # Moderate trend
                regime = TradingRegime.TREND
                confidence = 0.6 + min(0.15, (adx - self.ADX_TREND_MIN) / 20)
        
        # ----- RANGE DETECTION -----
        else:
            # ADX below trend threshold = ranging
            if bb_width_percentile < self.BB_WIDTH_COMPRESSION:
                # Tight range with low volatility
                if bb_expanding or adx_slope > self.ADX_SLOPE_THRESHOLD:
                    # Compression ending, potential breakout
                    regime = TradingRegime.TRANSITIONAL
                    breakout_imminent = True
                    confidence = 0.6
                else:
                    # Passive range
                    regime = TradingRegime.RANGE_PASIVO
                    confidence = 0.7
            elif recent_swing_size >= self.RANGE_SWING_MIN:
                # Range with decent swings = active
                regime = TradingRegime.RANGE_ACTIVO
                confidence = 0.65 + min(0.2, recent_swing_size / 0.01)
            else:
                # Low volatility, low swings
                regime = TradingRegime.RANGE_PASIVO
                confidence = 0.6
        
        # ----- TRANSITIONAL DETECTION -----
        # Check for regime transition signals
        if regime in [TradingRegime.TREND, TradingRegime.RANGE_ACTIVO]:
            # Signs of transition?
            if abs(adx_slope) > self.ADX_SLOPE_THRESHOLD * 2:
                # Rapid ADX change = transitional
                if regime == TradingRegime.TREND and adx_slope < 0:
                    regime = TradingRegime.TRANSITIONAL
                    confidence *= 0.8
                elif regime != TradingRegime.TREND and adx_slope > 0:
                    regime = TradingRegime.TRANSITIONAL
                    confidence *= 0.8
        
        # ----- SMOOTHING: Require confirmations -----
        regime = self._apply_smoothing(regime)
        
        # ----- BUILD ANALYSIS RESULT -----
        characteristics = REGIME_CHARACTERISTICS.get(
            regime, 
            REGIME_CHARACTERISTICS[TradingRegime.UNKNOWN]
        )
        
        # Calculate regime age
        regime_age_minutes = 0
        if self.regime_start_time:
            delta = datetime.now(UTC) - self.regime_start_time
            regime_age_minutes = int(delta.total_seconds() / 60)
        
        # Calculate stability score
        stability_score = min(1.0, regime_age_minutes / 30)  # Max stability after 30 min
        
        # Determine recommended direction
        recommended_direction = None
        avoid_direction = None
        
        if regime == TradingRegime.TREND:
            if trend_direction == TrendDirection.BULLISH:
                recommended_direction = 'LONG'
                avoid_direction = 'SHORT'
            elif trend_direction == TrendDirection.BEARISH:
                recommended_direction = 'SHORT'
                avoid_direction = 'LONG'
        elif regime == TradingRegime.EXHAUSTION:
            # Fade the exhaustion
            if trend_direction == TrendDirection.BULLISH:
                recommended_direction = 'SHORT'
            elif trend_direction == TrendDirection.BEARISH:
                recommended_direction = 'LONG'
        elif regime in [TradingRegime.RANGE_ACTIVO, TradingRegime.RANGE_PASIVO]:
            # Mean reversion
            if position_in_range > 0.8:
                recommended_direction = 'SHORT'
            elif position_in_range < 0.2:
                recommended_direction = 'LONG'
        
        analysis = RegimeAnalysis(
            regime=regime,
            confidence=confidence,
            characteristics=characteristics,
            trend_direction=trend_direction,
            trend_strength=adx / 100.0,  # Normalize to 0-1
            range_high=range_high,
            range_low=range_low,
            current_position_in_range=position_in_range,
            regime_age_minutes=regime_age_minutes,
            stability_score=stability_score,
            exhaustion_warning=exhaustion_warning,
            breakout_imminent=breakout_imminent,
            volatility_expanding=volatility_expanding,
            recommended_direction=recommended_direction,
            avoid_direction=avoid_direction
        )
        
        self._last_analysis = analysis
        return analysis
    
    def _apply_smoothing(self, new_regime: TradingRegime) -> TradingRegime:
        """
        Apply smoothing to prevent regime flip-flopping.
        Require N confirmations before changing regime.
        """
        if new_regime == self.current_regime:
            self._pending_regime = None
            self._pending_count = 0
            return new_regime
        
        if new_regime == self._pending_regime:
            self._pending_count += 1
            if self._pending_count >= self.REGIME_CHANGE_CONFIRMATIONS:
                # Confirm regime change
                self._record_regime_change(new_regime)
                self._pending_regime = None
                self._pending_count = 0
                return new_regime
        else:
            # New pending regime
            self._pending_regime = new_regime
            self._pending_count = 1
        
        # Return current regime until confirmed
        return self.current_regime
    
    def _record_regime_change(self, new_regime: TradingRegime):
        """Record regime change in history."""
        now = datetime.now(UTC)
        
        if self.current_regime != TradingRegime.UNKNOWN and self.regime_start_time is not None:
            self.regime_history.append((self.current_regime, self.regime_start_time))
        
        self.current_regime = new_regime
        self.regime_start_time = now
        
        logger.info(f"📊 Regime change: {self.current_regime.value} → {new_regime.value}")
        
        # Trim history
        if len(self.regime_history) > 50:
            self.regime_history = self.regime_history[-50:]
    
    def get_trading_guidance(self) -> Dict:
        """Get current trading guidance based on regime."""
        if not self._last_analysis:
            return {
                'allow_trade': False,
                'reason': 'No regime analysis available'
            }
        
        analysis = self._last_analysis
        chars = analysis.characteristics
        
        return {
            'allow_trade': analysis.regime not in [TradingRegime.UNKNOWN],
            'regime': analysis.regime.value,
            'confidence': analysis.confidence,
            'max_trades': chars.max_trades_per_session,
            'min_spacing_minutes': chars.min_minutes_between_trades,
            'size_multiplier': chars.base_size_multiplier,
            'min_rr': chars.min_rr_required,
            'prefer_mean_reversion': chars.prefer_mean_reversion,
            'prefer_momentum': chars.prefer_momentum,
            'quick_profit_taking': chars.quick_profit_taking,
            'recommended_direction': analysis.recommended_direction,
            'avoid_direction': analysis.avoid_direction,
            'warnings': {
                'exhaustion': analysis.exhaustion_warning,
                'breakout_imminent': analysis.breakout_imminent,
                'volatility_expanding': analysis.volatility_expanding
            }
        }
    
    def is_favorable_for_direction(self, direction: str) -> Tuple[bool, str]:
        """
        Check if current regime is favorable for a given direction.
        
        Returns:
            Tuple of (is_favorable, reason)
        """
        if not self._last_analysis:
            return False, "No regime analysis"
        
        analysis = self._last_analysis
        direction = direction.upper()
        
        # Check if direction is explicitly avoided
        if analysis.avoid_direction == direction:
            return False, f"Regime {analysis.regime.value} advises against {direction}"
        
        # Check if direction is recommended
        if analysis.recommended_direction == direction:
            return True, f"Regime {analysis.regime.value} favors {direction}"
        
        # Check regime-specific rules
        if analysis.regime == TradingRegime.TREND:
            if analysis.trend_direction == TrendDirection.BULLISH and direction == 'SHORT':
                return False, "Counter-trend SHORT in bullish trend"
            if analysis.trend_direction == TrendDirection.BEARISH and direction == 'LONG':
                return False, "Counter-trend LONG in bearish trend"
        
        if analysis.regime == TradingRegime.EXHAUSTION:
            # Exhaustion = fade the trend
            if analysis.trend_direction == TrendDirection.BULLISH and direction == 'LONG':
                return False, "Adding to exhausted bullish trend"
            if analysis.trend_direction == TrendDirection.BEARISH and direction == 'SHORT':
                return False, "Adding to exhausted bearish trend"
        
        # Neutral
        return True, f"Direction {direction} acceptable in {analysis.regime.value}"
    
    def get_last_analysis(self) -> Optional[RegimeAnalysis]:
        """Get the last regime analysis."""
        return self._last_analysis


# =============================================================================
# SINGLETON
# =============================================================================

_regime_classifier: Optional[TradingRegimeClassifier] = None


def get_regime_classifier() -> TradingRegimeClassifier:
    """Get or create the regime classifier singleton."""
    global _regime_classifier
    if _regime_classifier is None:
        _regime_classifier = TradingRegimeClassifier()
    return _regime_classifier


def reset_regime_classifier():
    """Reset the singleton."""
    global _regime_classifier
    _regime_classifier = None


def classify_regime(**kwargs) -> RegimeAnalysis:
    """Convenience function to classify regime."""
    return get_regime_classifier().classify(**kwargs)
