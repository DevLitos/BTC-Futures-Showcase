"""
Management Regime - Position management guidance based on market regime.

This module connects TrendIntelligence (phase detection) to position sizing.
It maps TrendPhase → ManagementRegime → risk_multiplier + TP/SL config.

Created: Feb 05, 2026 (extracted from market_regime.py)
Updated: Feb 05, 2026 - Connected to TrendIntelligence (FIX: sizing now reacts to regime)

USAGE:
- ManagementRegime: Enum for regime states  
- RegimeGuidance: Trading guidance configuration
- REGIME_GUIDANCE: Dict mapping regime → guidance
- get_regime_classifier(): Returns classifier that uses TrendIntelligence
- get_regime_risk_multiplier(): Get risk multiplier for current regime

MAPPING TrendPhase → ManagementRegime:
- TREND_BIRTH, TREND_ACCELERATION → TREND_EXPANSION (1.25x risk)
- TREND_MATURE, ACCUMULATION, DISTRIBUTION → MEAN_REVERSION (0.85x risk)
- TREND_EXHAUSTION, TREND_DEATH, RANGE_BOUND → CHOPPY_DEAD (0.3x risk)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ManagementRegime(Enum):
    """Market regime for position management decisions."""
    TREND_EXPANSION = "TREND_EXPANSION"   # Strong trend, aggressive sizing
    MEAN_REVERSION = "MEAN_REVERSION"     # Normal conditions
    CHOPPY_DEAD = "CHOPPY_DEAD"           # Avoid or reduce size


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
        tp1_close_pct=50.0,
        trailing_atr_mult=3.0,
        runner_pct=30.0,
        use_be_stop=True,
        max_hold_minutes=120,
        description="Agresivo: Runner 30%, Trailing alejado (3x ATR)"
    ),
    ManagementRegime.MEAN_REVERSION: RegimeGuidance(
        risk_multiplier=0.85,
        tp1_close_pct=70.0,
        trailing_atr_mult=2.0,
        runner_pct=10.0,
        use_be_stop=True,
        max_hold_minutes=60,
        description="Conservador: TP1 70%, cerrar runner rápido"
    ),
    ManagementRegime.CHOPPY_DEAD: RegimeGuidance(
        risk_multiplier=0.3,
        tp1_close_pct=80.0,
        trailing_atr_mult=1.5,
        runner_pct=0.0,
        use_be_stop=True,
        max_hold_minutes=45,
        description="Defensivo: Riesgo 0.3x o No-Trade"
    )
}


# =============================================================================
# TREND PHASE → MANAGEMENT REGIME MAPPING
# =============================================================================
# Import TrendPhase for mapping (lazy import to avoid circular)
_PHASE_TO_REGIME = None

def _get_phase_to_regime_map():
    """Lazy load phase mapping to avoid circular imports."""
    global _PHASE_TO_REGIME
    if _PHASE_TO_REGIME is None:
        from src.domain.market.regime.trend_intelligence import TrendPhase
        _PHASE_TO_REGIME = {
            # 🔥 AGGRESSIVE: New trends = maximum edge
            TrendPhase.TREND_BIRTH: ManagementRegime.TREND_EXPANSION,
            TrendPhase.TREND_ACCELERATION: ManagementRegime.TREND_EXPANSION,
            
            # 🟡 NORMAL: Established conditions
            TrendPhase.TREND_MATURE: ManagementRegime.MEAN_REVERSION,
            TrendPhase.ACCUMULATION: ManagementRegime.MEAN_REVERSION,
            TrendPhase.DISTRIBUTION: ManagementRegime.MEAN_REVERSION,
            
            # 🔴 DEFENSIVE: Exhaustion/chop = reduce or avoid
            TrendPhase.TREND_EXHAUSTION: ManagementRegime.CHOPPY_DEAD,
            TrendPhase.TREND_DEATH: ManagementRegime.CHOPPY_DEAD,
            TrendPhase.RANGE_BOUND: ManagementRegime.CHOPPY_DEAD,
        }
    return _PHASE_TO_REGIME


# =============================================================================
# INTELLIGENT REGIME CLASSIFIER (Uses TrendIntelligence)
# =============================================================================

class _TrendBasedRegimeClassifier:
    """
    Regime classifier that uses TrendIntelligence for real regime detection.
    
    Sizing now REACTS to market regime!
    - TREND_BIRTH/ACCELERATION → 1.25x risk (maximum edge)
    - TREND_MATURE/ACCUMULATION → 0.85x risk (normal)
    - TREND_EXHAUSTION/DEATH/RANGE → 0.3x risk (defensive)
    """
    
    def __init__(self):
        self._last_regime: Optional[ManagementRegime] = None
        self._last_phase = None
    
    def get_current_regime(self) -> Optional[ManagementRegime]:
        """
        Get current regime based on TrendIntelligence phase.
        
        Returns ManagementRegime based on current TrendPhase.
        """
        try:
            from src.domain.market.regime.trend_intelligence import get_trend_intelligence
            
            trend_intel = get_trend_intelligence()
            current_state = trend_intel.state  # Use property, not method
            
            if current_state is None:
                return ManagementRegime.MEAN_REVERSION  # Fallback
            
            phase = current_state.current_phase
            phase_map = _get_phase_to_regime_map()
            regime = phase_map.get(phase, ManagementRegime.MEAN_REVERSION)
            
            # Log regime changes
            if regime != self._last_regime:
                logger.info(
                    f"📊 REGIME CHANGE: {self._last_regime} → {regime} | "
                    f"TrendPhase={phase.name} | risk_mult={REGIME_GUIDANCE[regime].risk_multiplier}x"
                )
                self._last_regime = regime
                self._last_phase = phase
            
            return regime
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to get TrendIntelligence: {e}, using MEAN_REVERSION")
            return ManagementRegime.MEAN_REVERSION
    
    def get_regime_guidance(self, regime: ManagementRegime | None = None) -> RegimeGuidance:
        """Get trading guidance for a regime."""
        if regime is None:
            regime = self.get_current_regime() or ManagementRegime.MEAN_REVERSION
        return REGIME_GUIDANCE[regime]


_regime_classifier: Optional[_TrendBasedRegimeClassifier] = None


def get_regime_classifier() -> _TrendBasedRegimeClassifier:
    """Get classifier instance that uses TrendIntelligence."""
    global _regime_classifier
    if _regime_classifier is None:
        _regime_classifier = _TrendBasedRegimeClassifier()
    return _regime_classifier


def get_regime_risk_multiplier(regime: ManagementRegime | None = None) -> float:
    """Get risk multiplier for current/specified regime."""
    if regime is None:
        classifier = get_regime_classifier()
        regime = classifier.get_current_regime() or ManagementRegime.MEAN_REVERSION
    return REGIME_GUIDANCE[regime].risk_multiplier


def get_current_regime_with_details() -> tuple[ManagementRegime, dict]:
    """
    Get current regime with full details for logging.
    
    Returns:
        (ManagementRegime, dict with phase, risk_mult, guidance)
    """
    try:
        from src.domain.market.regime.trend_intelligence import get_trend_intelligence
        
        trend_intel = get_trend_intelligence()
        state = trend_intel.state  # Use property, not method
        
        if state is None:
            return ManagementRegime.MEAN_REVERSION, {"phase": "UNKNOWN", "source": "fallback"}
        
        phase_map = _get_phase_to_regime_map()
        regime = phase_map.get(state.current_phase, ManagementRegime.MEAN_REVERSION)
        guidance = REGIME_GUIDANCE[regime]
        
        return regime, {
            "phase": state.current_phase.name,
            "direction": state.direction.name if state.direction else "NEUTRAL",
            "phase_age": state.phase_age_candles,
            "confidence": state.confidence,
            "risk_multiplier": guidance.risk_multiplier,
            "tp1_close_pct": guidance.tp1_close_pct,
            "max_hold_minutes": guidance.max_hold_minutes,
            "source": "TrendIntelligence"
        }
    except Exception as e:
        logger.warning(f"get_current_regime_with_details failed: {e}")
        return ManagementRegime.MEAN_REVERSION, {"error": str(e), "source": "fallback"}


def classify_market_regime(*args, **kwargs):
    """
    Legacy function - now uses TrendIntelligence.
    Returns (ManagementRegime, details_dict)
    """
    return get_current_regime_with_details()
