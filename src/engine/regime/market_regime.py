"""
Market Regime Engine (Context Engine) - V2.1

El sistema debe "sentir" el mercado antes de decidir cómo gestionar la posición.

Definición Matemática de 3 Estados usando ADX + Análisis de Velas:

| Régimen        | Condición Técnica                          | Lógica de Gestión                                |
|----------------|--------------------------------------------|-------------------------------------------------|
| TREND_EXPANSION| ADX > 25 + Velas con cuerpo grande         | Agresivo: Runner 30%, Trailing alejado (3x ATR) |
| MEAN_REVERSION | ADX < 20 + Wicks largos en extremos        | Conservador: TP1 70%, cerrar runner rápido      |
| CHOPPY_DEAD    | ADX < 15 + Volumen decreciente             | Defensivo: Riesgo 0.3x o No-Trade               |

Implementación sin "Lag":
- Usa Volatilidad Relativa: ATR_ratio = ATR_current / ATR_baseline
- Si ATR_ratio > 2.0: régimen de expansión o pánico → reduce apalancamiento
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
from enum import Enum
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


# =============================================================================
# MARKET REGIME DEFINITIONS
# =============================================================================

class ManagementRegime(Enum):
    """Three market states based on ADX and price action."""
    TREND_EXPANSION = "TREND_EXPANSION"   # ADX > 25, clean candles
    MEAN_REVERSION = "MEAN_REVERSION"     # ADX < 20, wicks dominate
    CHOPPY_DEAD = "CHOPPY_DEAD"           # ADX < 15, low volume


@dataclass
class RegimeConfig:
    """Configuration for regime classification."""
    
    # ADX thresholds
    ADX_TREND_THRESHOLD: float = 25.0     # Above this = trending
    ADX_MEAN_REVERSION_MAX: float = 20.0  # Below this = mean reverting
    ADX_CHOPPY_MAX: float = 15.0          # Below this = choppy/dead
    
    # Candle analysis (body vs wick ratio)
    BODY_RATIO_TREND: float = 0.6         # Body > 60% of range = trend candle
    BODY_RATIO_WICK: float = 0.3          # Body < 30% of range = wick candle
    
    # Volume thresholds
    VOLUME_DECAY_THRESHOLD: float = 0.7   # Volume < 70% of avg = decaying
    
    # ATR ratio thresholds (for panic detection)
    ATR_PANIC_RATIO: float = 2.0          # ATR > 2x baseline = panic
    ATR_LOW_VOL_RATIO: float = 0.5        # ATR < 0.5x baseline = dead market
    ATR_BASELINE_PERIODS: int = 100       # Periods for ATR baseline
    
    # Lookback for candle analysis
    CANDLE_LOOKBACK: int = 10             # Number of candles to analyze


@dataclass
class RegimeGuidance:
    """Trading guidance based on regime."""
    
    # Position sizing multiplier
    risk_multiplier: float = 1.0
    
    # TP1 percentage (how much to close at first target)
    tp1_close_pct: float = 50.0
    
    # Trailing stop ATR multiplier
    trailing_atr_mult: float = 2.5
    
    # Runner percentage (position to let run)
    runner_pct: float = 20.0
    
    # Should use breakeven stop?
    use_be_stop: bool = True
    
    # Maximum hold time (minutes)
    max_hold_minutes: int = 90
    
    # Description for logging
    description: str = ""


# Guidance per regime
REGIME_GUIDANCE: Dict[ManagementRegime, RegimeGuidance] = {
    ManagementRegime.TREND_EXPANSION: RegimeGuidance(
        risk_multiplier=1.25,
        tp1_close_pct=50.0,      # Aggressive: less at TP1
        trailing_atr_mult=3.0,   # Wide trailing to capture trend
        runner_pct=30.0,         # Bigger runner
        use_be_stop=True,
        max_hold_minutes=120,    # Let trends develop
        description="Agresivo: Runner 30%, Trailing alejado (3x ATR)"
    ),
    ManagementRegime.MEAN_REVERSION: RegimeGuidance(
        risk_multiplier=0.85,
        tp1_close_pct=70.0,      # Conservative: more at TP1
        trailing_atr_mult=2.0,   # Tight trailing 
        runner_pct=10.0,         # Small runner
        use_be_stop=True,
        max_hold_minutes=60,     # Close quick
        description="Conservador: TP1 70%, cerrar runner rápido"
    ),
    ManagementRegime.CHOPPY_DEAD: RegimeGuidance(
        risk_multiplier=0.3,     # Very defensive
        tp1_close_pct=80.0,      # Close most at TP1
        trailing_atr_mult=1.5,   # Very tight
        runner_pct=0.0,          # No runner
        use_be_stop=True,
        max_hold_minutes=45,     # Quick exits
        description="Defensivo: Riesgo 0.3x o No-Trade"
    )
}


# =============================================================================
# MARKET REGIME CLASSIFIER
# =============================================================================

class ManagementRegimeClassifier:
    """
    Classifies market regime using ADX and price action analysis.
    
    No lagging indicators - uses volatility relative to baseline.
    """
    
    def __init__(self, config: RegimeConfig = None):
        self.config = config or RegimeConfig()
        
        # ATR baseline tracking
        self._atr_history: List[float] = []
        self._atr_baseline: float = 0.0
        
        # Regime history for smoothing
        self._regime_history: List[ManagementRegime] = []
        
        # Last classification
        self._last_regime: Optional[ManagementRegime] = None
        self._last_classification_time: Optional[datetime] = None
    
    def update_atr_baseline(self, current_atr: float) -> float:
        """
        Update ATR baseline with new value.
        
        Returns current ATR ratio (current / baseline).
        """
        self._atr_history.append(current_atr)
        
        # Keep only recent history
        max_history = self.config.ATR_BASELINE_PERIODS
        if len(self._atr_history) > max_history:
            self._atr_history = self._atr_history[-max_history:]
        
        # Calculate baseline (median for robustness)
        if len(self._atr_history) >= 20:
            self._atr_baseline = float(np.median(self._atr_history))
        else:
            self._atr_baseline = float(np.mean(self._atr_history)) if self._atr_history else current_atr
        
        # Calculate ratio
        if self._atr_baseline > 0:
            return current_atr / self._atr_baseline
        return 1.0
    
    def analyze_candle_structure(self, 
                                  opens: np.ndarray,
                                  highs: np.ndarray,
                                  lows: np.ndarray,
                                  closes: np.ndarray) -> Dict[str, float]:
        """
        Analyze recent candle structure for body/wick ratios.
        
        Returns:
            - avg_body_ratio: Average body size as % of range
            - wick_dominance: % of candles with wick > body
            - trend_candle_pct: % of candles that are trend candles (body > 60%)
        """
        n = min(len(closes), self.config.CANDLE_LOOKBACK)
        if n < 3:
            return {'avg_body_ratio': 0.5, 'wick_dominance': 0.5, 'trend_candle_pct': 0.5}
        
        body_ratios = []
        wick_dominant_count = 0
        trend_candle_count = 0
        
        for i in range(-n, 0):
            range_size = highs[i] - lows[i]
            if range_size <= 0:
                continue
                
            body_size = abs(closes[i] - opens[i])
            body_ratio = body_size / range_size
            body_ratios.append(body_ratio)
            
            if body_ratio < self.config.BODY_RATIO_WICK:
                wick_dominant_count += 1
            
            if body_ratio > self.config.BODY_RATIO_TREND:
                trend_candle_count += 1
        
        total = len(body_ratios)
        if total == 0:
            return {'avg_body_ratio': 0.5, 'wick_dominance': 0.5, 'trend_candle_pct': 0.5}
        
        return {
            'avg_body_ratio': float(np.mean(body_ratios)),
            'wick_dominance': wick_dominant_count / total,
            'trend_candle_pct': trend_candle_count / total
        }
    
    def analyze_volume_trend(self, volumes: np.ndarray) -> Dict[str, float]:
        """
        Analyze volume trend for decay detection.
        
        Returns:
            - volume_ratio: Current vol vs average
            - is_decaying: True if volume is declining
        """
        n = min(len(volumes), self.config.CANDLE_LOOKBACK)
        if n < 5:
            return {'volume_ratio': 1.0, 'is_decaying': False}
        
        recent_vol = float(np.mean(volumes[-3:]))  # Last 3
        baseline_vol = float(np.mean(volumes[-n:-3])) if n > 3 else recent_vol
        
        if baseline_vol <= 0:
            return {'volume_ratio': 1.0, 'is_decaying': False}
        
        volume_ratio = recent_vol / baseline_vol
        is_decaying = volume_ratio < self.config.VOLUME_DECAY_THRESHOLD
        
        return {
            'volume_ratio': volume_ratio,
            'is_decaying': is_decaying
        }
    
    def classify_regime(self,
                        adx: float,
                        current_atr: float,
                        opens: np.ndarray = None,
                        highs: np.ndarray = None,
                        lows: np.ndarray = None,
                        closes: np.ndarray = None,
                        volumes: np.ndarray = None,
                        market_time: datetime = None) -> Tuple[ManagementRegime, Dict[str, Any]]:
        """
        Classify current market regime.
        
        Args:
            adx: Current ADX value
            current_atr: Current ATR value
            opens/highs/lows/closes: OHLC arrays for candle analysis
            volumes: Volume array
            market_time: Current market time
            
        Returns:
            Tuple of (regime, analysis_details)
        """
        # Update ATR baseline and get ratio
        atr_ratio = self.update_atr_baseline(current_atr)
        
        # Check for panic mode (extreme volatility)
        is_panic = atr_ratio > self.config.ATR_PANIC_RATIO
        is_dead = atr_ratio < self.config.ATR_LOW_VOL_RATIO
        
        # Analyze candle structure if available
        candle_analysis = {}
        if opens is not None and highs is not None and lows is not None and closes is not None:
            candle_analysis = self.analyze_candle_structure(opens, highs, lows, closes)
        
        # Analyze volume if available
        volume_analysis = {}
        if volumes is not None:
            volume_analysis = self.analyze_volume_trend(volumes)
        
        # Classification logic
        regime = self._classify_by_conditions(
            adx=adx,
            atr_ratio=atr_ratio,
            is_panic=is_panic,
            is_dead=is_dead,
            candle_analysis=candle_analysis,
            volume_analysis=volume_analysis
        )
        
        # Build analysis details
        details = {
            'adx': adx,
            'atr_ratio': atr_ratio,
            'atr_baseline': self._atr_baseline,
            'current_atr': current_atr,
            'is_panic': is_panic,
            'is_dead': is_dead,
            **candle_analysis,
            **volume_analysis,
            'regime': regime.value,
            'guidance': REGIME_GUIDANCE[regime].description
        }
        
        # Store last regime
        self._last_regime = regime
        self._last_classification_time = market_time or datetime.now(UTC)
        
        # Add to history for smoothing
        self._regime_history.append(regime)
        if len(self._regime_history) > 20:
            self._regime_history = self._regime_history[-20:]
        
        logger.info(f"🎯 REGIME: {regime.value} | ADX={adx:.1f}, ATR_ratio={atr_ratio:.2f}")
        
        return regime, details
    
    def _classify_by_conditions(self,
                                 adx: float,
                                 atr_ratio: float,
                                 is_panic: bool,
                                 is_dead: bool,
                                 candle_analysis: Dict,
                                 volume_analysis: Dict) -> ManagementRegime:
        """Internal classification logic."""
        
        # PANIC: Reduce risk immediately regardless of ADX
        if is_panic:
            logger.warning(f"⚠️ PANIC MODE: ATR ratio {atr_ratio:.2f} > {self.config.ATR_PANIC_RATIO}")
            return ManagementRegime.CHOPPY_DEAD  # Defensive
        
        # DEAD MARKET: Very low volatility
        if is_dead:
            return ManagementRegime.CHOPPY_DEAD
        
        # Extract candle metrics
        trend_candle_pct = candle_analysis.get('trend_candle_pct', 0.5)
        wick_dominance = candle_analysis.get('wick_dominance', 0.3)
        
        # Extract volume metrics
        is_volume_decaying = volume_analysis.get('is_decaying', False)
        
        # TREND EXPANSION: Strong ADX + trend candles
        if adx > self.config.ADX_TREND_THRESHOLD:
            if trend_candle_pct >= 0.4:  # At least 40% trend candles
                return ManagementRegime.TREND_EXPANSION
            # High ADX but wicky candles = be careful
            if wick_dominance > 0.5:
                return ManagementRegime.MEAN_REVERSION
            return ManagementRegime.TREND_EXPANSION
        
        # MEAN REVERSION: Low ADX + wick dominance
        if adx < self.config.ADX_MEAN_REVERSION_MAX:
            if wick_dominance > 0.4:  # Lots of wicks
                return ManagementRegime.MEAN_REVERSION
        
        # CHOPPY/DEAD: Very low ADX + decaying volume
        if adx < self.config.ADX_CHOPPY_MAX:
            if is_volume_decaying:
                return ManagementRegime.CHOPPY_DEAD
            return ManagementRegime.MEAN_REVERSION  # Low ADX but volume OK
        
        # Default: Between thresholds, use candle analysis
        if trend_candle_pct > 0.5:
            return ManagementRegime.TREND_EXPANSION
        elif wick_dominance > 0.4:
            return ManagementRegime.MEAN_REVERSION
        
        # Fallback
        return ManagementRegime.MEAN_REVERSION
    
    def get_regime_guidance(self, regime: ManagementRegime = None) -> RegimeGuidance:
        """Get trading guidance for a regime."""
        regime = regime or self._last_regime or ManagementRegime.MEAN_REVERSION
        return REGIME_GUIDANCE[regime]
    
    def get_current_regime(self) -> Optional[ManagementRegime]:
        """Get the last classified regime."""
        return self._last_regime
    
    def get_regime_stability(self) -> float:
        """
        Get regime stability (0-1).
        
        Higher = more stable (same regime for longer).
        Used to reduce risk during regime transitions.
        """
        if not self._regime_history:
            return 0.5
        
        # Count most common regime in recent history
        recent = self._regime_history[-5:] if len(self._regime_history) >= 5 else self._regime_history
        most_common = max(set(recent), key=recent.count)
        stability = recent.count(most_common) / len(recent)
        
        return stability


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_regime_classifier: Optional[ManagementRegimeClassifier] = None


def get_regime_classifier() -> ManagementRegimeClassifier:
    """Get global ManagementRegimeClassifier instance."""
    global _regime_classifier
    if _regime_classifier is None:
        _regime_classifier = ManagementRegimeClassifier()
    return _regime_classifier


def classify_market_regime(adx: float, 
                           current_atr: float,
                           **kwargs) -> Tuple[ManagementRegime, Dict[str, Any]]:
    """
    Convenience function to classify market regime.
    
    Args:
        adx: Current ADX value
        current_atr: Current ATR value
        **kwargs: Additional parameters (opens, highs, lows, closes, volumes)
        
    Returns:
        Tuple of (regime, analysis_details)
    """
    classifier = get_regime_classifier()
    return classifier.classify_regime(adx, current_atr, **kwargs)


def get_regime_risk_multiplier(regime: ManagementRegime = None) -> float:
    """Get risk multiplier for current/specified regime."""
    classifier = get_regime_classifier()
    guidance = classifier.get_regime_guidance(regime)
    return guidance.risk_multiplier


def should_reduce_risk_for_panic(current_atr: float) -> Tuple[bool, float]:
    """
    Check if we should reduce risk due to panic volatility.
    
    Returns:
        Tuple of (should_reduce, atr_ratio)
    """
    classifier = get_regime_classifier()
    atr_ratio = classifier.update_atr_baseline(current_atr)
    
    should_reduce = atr_ratio > classifier.config.ATR_PANIC_RATIO
    
    return should_reduce, atr_ratio
