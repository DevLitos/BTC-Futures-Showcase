"""
Trend Intelligence Module - Detección de INICIO y FIN de Tendencias

El problema actual: Solo detectamos "estamos en RANGE" o "estamos en TREND".
Pero NO detectamos las TRANSICIONES que son donde está el dinero:

1. TREND_BIRTH: El momento donde RANGE → TREND (breakdown/breakout)
2. TREND_MATURE: Trend establecido, momentum sostenido
3. TREND_EXHAUSTION: Señales de agotamiento (divergencias, volumen decreciente)  
4. TREND_DEATH: El momento donde TREND → RANGE (failure, acceptance)

FILOSOFÍA:
- El INICIO de tendencia es el mejor momento para entrar (small stop, big target)
- El FIN de tendencia es el momento para salir/revertir (no perseguir)
- El MEDIO de tendencia es para gestionar (trailing, no entrar)

SIGNALS:
- ADX rising from below 20 = trend birth potential
- ADX falling from above 25 = trend exhaustion
- Price acceptance above/below key levels = confirmation
- Volume profile shift = structure change
"""

import logging
import numpy as np
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


class TrendPhase(Enum):
    """Fase del ciclo de tendencia"""
    ACCUMULATION = "ACCUMULATION"     # Pre-trend: Building pressure
    TREND_BIRTH = "TREND_BIRTH"       # Breakout inicial
    TREND_ACCELERATION = "TREND_ACCELERATION"  # Momentum creciente
    TREND_MATURE = "TREND_MATURE"     # Tendencia establecida
    TREND_EXHAUSTION = "TREND_EXHAUSTION"  # Señales de agotamiento
    DISTRIBUTION = "DISTRIBUTION"     # Pre-reversal
    TREND_DEATH = "TREND_DEATH"       # Failure/reversal
    RANGE_BOUND = "RANGE_BOUND"       # Sin tendencia clara


class TrendDirection(Enum):
    """Dirección de la tendencia"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class TrendTransition:
    """Registro de una transición de tendencia"""
    timestamp: datetime
    from_phase: TrendPhase
    to_phase: TrendPhase
    direction: TrendDirection
    confidence: float  # 0-1
    trigger: str  # What caused the transition
    price_at_transition: float
    adx_at_transition: float
    
    
@dataclass
class TrendState:
    """Estado completo de la inteligencia de tendencia"""
    current_phase: TrendPhase
    direction: TrendDirection
    phase_age_candles: int  # Cuántas velas en esta fase
    confidence: float  # 0-1 confidence en el estado actual
    
    # Metrics para detectar transiciones
    adx_current: float = 0.0
    adx_slope: float = 0.0
    adx_acceleration: float = 0.0  # Segunda derivada
    
    # Price structure
    higher_highs_count: int = 0
    lower_lows_count: int = 0
    swing_high: float = 0.0
    swing_low: float = 0.0
    
    # Volume analysis
    volume_trend: str = "neutral"  # "increasing", "decreasing", "neutral"
    volume_on_moves: str = "neutral"  # "confirms", "diverges", "neutral"
    
    # Key levels
    last_breakout_level: float = 0.0
    last_breakdown_level: float = 0.0
    acceptance_above: bool = False  # Price accepted above breakout
    acceptance_below: bool = False  # Price accepted below breakdown
    
    # Transition history
    recent_transitions: List[TrendTransition] = field(default_factory=list)
    
    # Trading implications
    trade_bias: str = "NEUTRAL"  # "LONG_ONLY", "SHORT_ONLY", "BOTH", "NONE"
    entry_mode: str = "WAIT"  # "AGGRESSIVE", "NORMAL", "CONSERVATIVE", "WAIT"
    

class TrendIntelligence:
    """
    Motor de inteligencia para detección de ciclos de tendencia.
    
    CONCEPTO CLAVE: No solo "qué es" sino "qué viene después"
    
    Estados y sus transiciones esperadas:
    ACCUMULATION → TREND_BIRTH (breakout) o RANGE_BOUND (failure)
    TREND_BIRTH → TREND_ACCELERATION (confirmation) o RANGE_BOUND (false break)
    TREND_ACCELERATION → TREND_MATURE (continuation) o TREND_EXHAUSTION (loss of momentum)
    TREND_MATURE → TREND_EXHAUSTION (divergence) o DISTRIBUTION (top/bottom)
    TREND_EXHAUSTION → DISTRIBUTION (confirmation) o TREND_ACCELERATION (second wind)
    DISTRIBUTION → TREND_DEATH (reversal) o RANGE_BOUND (chop)
    TREND_DEATH → ACCUMULATION (new cycle) o RANGE_BOUND
    """
    
    # Thresholds
    ADX_BIRTH_THRESHOLD = 20.0      # ADX crossing above = potential birth
    ADX_MATURE_THRESHOLD = 25.0     # ADX above = mature trend
    ADX_DEATH_THRESHOLD = 18.0      # ADX crossing below = potential death
    ADX_SLOPE_THRESHOLD = 0.5       # Slope magnitude for direction
    
    # Confirmation requirements
    ACCEPTANCE_CANDLES = 3          # Candles needed for level acceptance
    SWING_LOOKBACK = 20             # Candles for swing detection
    
    def __init__(self):
        self._state = TrendState(
            current_phase=TrendPhase.RANGE_BOUND,
            direction=TrendDirection.NEUTRAL,
            phase_age_candles=0,
            confidence=0.5
        )
        self._price_history: List[float] = []
        self._adx_history: List[float] = []
        self._volume_history: List[float] = []
        self._high_history: List[float] = []
        self._low_history: List[float] = []
        
    def update(
        self,
        current_price: float,
        high: float,
        low: float,
        close: float,
        adx: float,
        volume: float,
        atr: float,
        timestamp: datetime = None
    ) -> TrendState:
        """
        Actualiza el estado de inteligencia de tendencia.
        
        Llamar en cada vela cerrada (1h recomendado para HTF analysis).
        
        Returns:
            TrendState con fase actual y métricas
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)
            
        # Update histories
        self._price_history.append(close)
        self._adx_history.append(adx)
        self._volume_history.append(volume)
        self._high_history.append(high)
        self._low_history.append(low)
        
        # Trim to last 100 candles
        max_history = 100
        for hist in [self._price_history, self._adx_history, self._volume_history,
                     self._high_history, self._low_history]:
            if len(hist) > max_history:
                hist.pop(0)
        
        # Calculate derivatives
        self._update_adx_metrics(adx)
        self._update_structure_metrics()
        self._update_volume_metrics(volume)
        
        # Detect phase transitions
        new_phase, confidence, trigger = self._detect_phase_transition(
            current_price, adx, atr, timestamp
        )
        
        # Update state
        if new_phase != self._state.current_phase:
            self._record_transition(
                timestamp, self._state.current_phase, new_phase,
                self._state.direction, confidence, trigger, current_price, adx
            )
            self._state.current_phase = new_phase
            self._state.phase_age_candles = 0
        else:
            self._state.phase_age_candles += 1
            
        self._state.confidence = confidence
        self._update_trading_implications()
        
        return self._state
    
    def _update_adx_metrics(self, adx: float):
        """Actualiza métricas de ADX (slope, acceleration)"""
        if len(self._adx_history) < 3:
            return
            
        # Slope (first derivative)
        adx_arr = np.array(self._adx_history[-10:])
        if len(adx_arr) >= 3:
            self._state.adx_slope = adx_arr[-1] - adx_arr[-3]
            
        # Acceleration (second derivative)
        if len(adx_arr) >= 5:
            slope_recent = adx_arr[-1] - adx_arr[-3]
            slope_prior = adx_arr[-3] - adx_arr[-5]
            self._state.adx_acceleration = slope_recent - slope_prior
            
        self._state.adx_current = adx
        
    def _update_structure_metrics(self):
        """Detecta HH/HL/LH/LL pattern"""
        if len(self._high_history) < self.SWING_LOOKBACK:
            return
            
        highs = np.array(self._high_history[-self.SWING_LOOKBACK:])
        lows = np.array(self._low_history[-self.SWING_LOOKBACK:])
        
        # Find swing points (simplified: local maxima/minima)
        swing_highs = []
        swing_lows = []
        
        for i in range(2, len(highs) - 2):
            # Swing high: higher than neighbors
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
               highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                swing_highs.append(highs[i])
                
            # Swing low: lower than neighbors  
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
               lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                swing_lows.append(lows[i])
        
        # Count HH and LL
        hh_count = 0
        ll_count = 0
        
        if len(swing_highs) >= 2:
            for i in range(1, len(swing_highs)):
                if swing_highs[i] > swing_highs[i-1]:
                    hh_count += 1
            self._state.swing_high = swing_highs[-1]
                    
        if len(swing_lows) >= 2:
            for i in range(1, len(swing_lows)):
                if swing_lows[i] < swing_lows[i-1]:
                    ll_count += 1
            self._state.swing_low = swing_lows[-1]
                    
        self._state.higher_highs_count = hh_count
        self._state.lower_lows_count = ll_count
        
        # Determine direction from structure
        if hh_count >= 2 and ll_count == 0:
            self._state.direction = TrendDirection.BULLISH
        elif ll_count >= 2 and hh_count == 0:
            self._state.direction = TrendDirection.BEARISH
        else:
            self._state.direction = TrendDirection.NEUTRAL
            
    def _update_volume_metrics(self, volume: float):
        """Analiza tendencia de volumen"""
        if len(self._volume_history) < 10:
            return
            
        vol_arr = np.array(self._volume_history[-10:])
        vol_avg = np.mean(vol_arr[:-3])
        vol_recent = np.mean(vol_arr[-3:])
        
        if vol_recent > vol_avg * 1.2:
            self._state.volume_trend = "increasing"
        elif vol_recent < vol_avg * 0.8:
            self._state.volume_trend = "decreasing"
        else:
            self._state.volume_trend = "neutral"
            
        # Volume on directional moves
        if len(self._price_history) >= 5:
            price_change = self._price_history[-1] - self._price_history[-5]
            vol_change = vol_recent - vol_avg
            
            # Volume confirms direction
            if (price_change > 0 and vol_change > 0) or \
               (price_change < 0 and vol_change > 0):
                self._state.volume_on_moves = "confirms"
            elif (price_change > 0 and vol_change < 0) or \
                 (price_change < 0 and vol_change < 0):
                self._state.volume_on_moves = "diverges"
            else:
                self._state.volume_on_moves = "neutral"
    
    def _detect_phase_transition(
        self, price: float, adx: float, atr: float, timestamp: datetime
    ) -> Tuple[TrendPhase, float, str]:
        """
        Detecta transiciones de fase basándose en múltiples factores.
        
        Returns:
            (new_phase, confidence, trigger_reason)
        """
        current = self._state.current_phase
        direction = self._state.direction
        
        # === RANGE_BOUND transitions ===
        if current == TrendPhase.RANGE_BOUND:
            # → ACCUMULATION: ADX starting to rise from low levels
            if adx < self.ADX_BIRTH_THRESHOLD and self._state.adx_slope > 0.3:
                return TrendPhase.ACCUMULATION, 0.6, "ADX slope turning positive"
                
            # → TREND_BIRTH: ADX crosses above threshold with structure
            if adx > self.ADX_BIRTH_THRESHOLD and self._state.adx_slope > 0.5:
                if direction == TrendDirection.BULLISH:
                    return TrendPhase.TREND_BIRTH, 0.7, "ADX breakout + bullish structure"
                elif direction == TrendDirection.BEARISH:
                    return TrendPhase.TREND_BIRTH, 0.7, "ADX breakout + bearish structure"
                    
        # === ACCUMULATION transitions ===
        elif current == TrendPhase.ACCUMULATION:
            # → TREND_BIRTH: ADX confirms with structure
            if adx > self.ADX_BIRTH_THRESHOLD:
                return TrendPhase.TREND_BIRTH, 0.7, "ADX confirmed trend birth"
                
            # → RANGE_BOUND: ADX fails to rise
            if self._state.adx_slope < -0.3 or self._state.phase_age_candles > 10:
                return TrendPhase.RANGE_BOUND, 0.6, "Accumulation failed"
                
        # === TREND_BIRTH transitions ===
        elif current == TrendPhase.TREND_BIRTH:
            # → TREND_ACCELERATION: Momentum confirmed
            if adx > self.ADX_MATURE_THRESHOLD and self._state.adx_slope > 0:
                return TrendPhase.TREND_ACCELERATION, 0.8, "Momentum acceleration confirmed"
                
            # → RANGE_BOUND: False breakout
            if adx < self.ADX_BIRTH_THRESHOLD or self._state.adx_slope < -0.5:
                return TrendPhase.RANGE_BOUND, 0.7, "False breakout - trend birth failed"
                
        # === TREND_ACCELERATION transitions ===
        elif current == TrendPhase.TREND_ACCELERATION:
            # → TREND_MATURE: Sustained high ADX
            if self._state.phase_age_candles > 5 and adx > self.ADX_MATURE_THRESHOLD:
                return TrendPhase.TREND_MATURE, 0.8, "Trend maturing - sustained momentum"
                
            # → TREND_EXHAUSTION: ADX starts falling
            if self._state.adx_slope < -0.5 and self._state.adx_acceleration < 0:
                return TrendPhase.TREND_EXHAUSTION, 0.7, "Momentum loss - acceleration negative"
                
        # === TREND_MATURE transitions ===
        elif current == TrendPhase.TREND_MATURE:
            # → TREND_EXHAUSTION: ADX falling or volume divergence
            if self._state.adx_slope < -0.3:
                return TrendPhase.TREND_EXHAUSTION, 0.7, "ADX slope turning negative"
            if self._state.volume_on_moves == "diverges":
                return TrendPhase.TREND_EXHAUSTION, 0.6, "Volume divergence detected"
                
        # === TREND_EXHAUSTION transitions ===
        elif current == TrendPhase.TREND_EXHAUSTION:
            # → DISTRIBUTION: Continued weakness
            if adx < self.ADX_MATURE_THRESHOLD and self._state.phase_age_candles > 3:
                return TrendPhase.DISTRIBUTION, 0.7, "ADX below mature threshold"
                
            # → TREND_ACCELERATION: Second wind
            if self._state.adx_slope > 0.5 and adx > self.ADX_MATURE_THRESHOLD:
                return TrendPhase.TREND_ACCELERATION, 0.6, "Second wind - momentum recovered"
                
        # === DISTRIBUTION transitions ===
        elif current == TrendPhase.DISTRIBUTION:
            # → TREND_DEATH: ADX collapses
            if adx < self.ADX_DEATH_THRESHOLD:
                return TrendPhase.TREND_DEATH, 0.8, "Trend death - ADX collapse"
                
            # → RANGE_BOUND: Extended chop
            if self._state.phase_age_candles > 10:
                return TrendPhase.RANGE_BOUND, 0.6, "Distribution complete - ranging"
                
        # === TREND_DEATH transitions ===
        elif current == TrendPhase.TREND_DEATH:
            # → RANGE_BOUND: Normal progression
            if self._state.phase_age_candles > 3:
                return TrendPhase.RANGE_BOUND, 0.7, "Post-death ranging"
                
            # → ACCUMULATION: New cycle forming
            if self._state.adx_slope > 0.3:
                return TrendPhase.ACCUMULATION, 0.5, "New cycle accumulation"
        
        # No transition
        return current, self._state.confidence, "No transition"
    
    def _record_transition(
        self, timestamp: datetime, from_phase: TrendPhase, to_phase: TrendPhase,
        direction: TrendDirection, confidence: float, trigger: str,
        price: float, adx: float
    ):
        """Registra una transición para historial"""
        transition = TrendTransition(
            timestamp=timestamp,
            from_phase=from_phase,
            to_phase=to_phase,
            direction=direction,
            confidence=confidence,
            trigger=trigger,
            price_at_transition=price,
            adx_at_transition=adx
        )
        
        self._state.recent_transitions.append(transition)
        if len(self._state.recent_transitions) > 20:
            self._state.recent_transitions.pop(0)
            
        logger.info(
            f"🔄 TREND TRANSITION: {from_phase.value} → {to_phase.value} | "
            f"Direction: {direction.value} | Confidence: {confidence:.0%} | "
            f"Trigger: {trigger}"
        )
        
    def _update_trading_implications(self):
        """Actualiza las implicaciones de trading basándose en la fase"""
        phase = self._state.current_phase
        direction = self._state.direction
        
        # Trade bias
        if phase in [TrendPhase.TREND_BIRTH, TrendPhase.TREND_ACCELERATION]:
            if direction == TrendDirection.BULLISH:
                self._state.trade_bias = "LONG_ONLY"
            elif direction == TrendDirection.BEARISH:
                self._state.trade_bias = "SHORT_ONLY"
            else:
                self._state.trade_bias = "BOTH"
        elif phase == TrendPhase.TREND_EXHAUSTION:
            # Counter-trend opportunities
            if direction == TrendDirection.BULLISH:
                self._state.trade_bias = "SHORT_PREFERRED"  # Fade exhaustion
            elif direction == TrendDirection.BEARISH:
                self._state.trade_bias = "LONG_PREFERRED"
            else:
                self._state.trade_bias = "NONE"
        elif phase == TrendPhase.DISTRIBUTION:
            self._state.trade_bias = "NONE"  # Wait for clarity
        elif phase == TrendPhase.TREND_DEATH:
            # Opposite of prior trend
            if direction == TrendDirection.BULLISH:
                self._state.trade_bias = "SHORT_ONLY"
            elif direction == TrendDirection.BEARISH:
                self._state.trade_bias = "LONG_ONLY"
            else:
                self._state.trade_bias = "NONE"
        else:
            self._state.trade_bias = "BOTH"  # RANGE_BOUND, ACCUMULATION
            
        # Entry mode
        if phase == TrendPhase.TREND_BIRTH:
            self._state.entry_mode = "AGGRESSIVE"  # Best opportunity
        elif phase == TrendPhase.TREND_ACCELERATION:
            self._state.entry_mode = "NORMAL"
        elif phase in [TrendPhase.TREND_MATURE, TrendPhase.ACCUMULATION]:
            self._state.entry_mode = "CONSERVATIVE"
        elif phase in [TrendPhase.TREND_EXHAUSTION, TrendPhase.DISTRIBUTION]:
            self._state.entry_mode = "WAIT"  # Don't enter, manage existing
        elif phase == TrendPhase.TREND_DEATH:
            self._state.entry_mode = "AGGRESSIVE"  # Reversal opportunity
        else:
            self._state.entry_mode = "CONSERVATIVE"
            
    def get_entry_recommendation(self, desired_direction: str) -> Dict[str, Any]:
        """
        Obtiene recomendación de entrada basándose en el estado actual.
        
        Args:
            desired_direction: 'LONG' or 'SHORT'
            
        Returns:
            dict with recommendation
        """
        phase = self._state.current_phase
        direction = self._state.direction
        bias = self._state.trade_bias
        mode = self._state.entry_mode
        
        # Check if direction aligns with bias
        direction_allowed = True
        if bias == "LONG_ONLY" and desired_direction == "SHORT":
            direction_allowed = False
        elif bias == "SHORT_ONLY" and desired_direction == "LONG":
            direction_allowed = False
        elif bias == "NONE":
            direction_allowed = False
            
        # Calculate size multiplier
        if mode == "AGGRESSIVE":
            size_mult = 1.2
        elif mode == "NORMAL":
            size_mult = 1.0
        elif mode == "CONSERVATIVE":
            size_mult = 0.7
        else:  # WAIT
            size_mult = 0.0
            
        # Score adjustment
        if phase == TrendPhase.TREND_BIRTH:
            score_adj = -1.0  # Easier entry
        elif phase == TrendPhase.TREND_ACCELERATION:
            score_adj = -0.5
        elif phase == TrendPhase.TREND_MATURE:
            score_adj = 0.0
        elif phase in [TrendPhase.TREND_EXHAUSTION, TrendPhase.DISTRIBUTION]:
            score_adj = +2.0  # Harder entry
        elif phase == TrendPhase.TREND_DEATH:
            score_adj = -0.5  # Reversal opportunity
        else:
            score_adj = 0.0
            
        return {
            'allowed': direction_allowed and size_mult > 0,
            'phase': phase.value,
            'direction_bias': bias,
            'entry_mode': mode,
            'size_multiplier': size_mult,
            'score_adjustment': score_adj,
            'confidence': self._state.confidence,
            'reason': f"{phase.value} | {direction.value} trend | {mode} mode"
        }
    
    @property
    def state(self) -> TrendState:
        """Current trend intelligence state"""
        return self._state
    
    def get_phase_description(self) -> str:
        """Human-readable description of current phase"""
        phase = self._state.current_phase
        direction = self._state.direction
        confidence = self._state.confidence
        age = self._state.phase_age_candles
        
        descriptions = {
            TrendPhase.ACCUMULATION: "Building pressure - potential breakout forming",
            TrendPhase.TREND_BIRTH: "🚀 TREND BIRTH - Best entry opportunity!",
            TrendPhase.TREND_ACCELERATION: "📈 Momentum accelerating - ride the wave",
            TrendPhase.TREND_MATURE: "Trend established - manage existing positions",
            TrendPhase.TREND_EXHAUSTION: "⚠️ Trend exhaustion - prepare for reversal",
            TrendPhase.DISTRIBUTION: "Distribution phase - no new entries",
            TrendPhase.TREND_DEATH: "💀 Trend death - reversal opportunity",
            TrendPhase.RANGE_BOUND: "Ranging market - mean reversion plays",
        }
        
        return (
            f"{descriptions.get(phase, 'Unknown')} | "
            f"Direction: {direction.value} | "
            f"Confidence: {confidence:.0%} | "
            f"Age: {age} candles"
        )


# Singleton instance
_trend_intelligence: Optional[TrendIntelligence] = None


def get_trend_intelligence() -> TrendIntelligence:
    """Get singleton TrendIntelligence instance"""
    global _trend_intelligence
    if _trend_intelligence is None:
        _trend_intelligence = TrendIntelligence()
    return _trend_intelligence


def reset_trend_intelligence():
    """Reset singleton (for testing)"""
    global _trend_intelligence
    _trend_intelligence = None
