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

# 🔥 FIX (Feb 05): Import TrendDirection from unified source
from src.config.unified_constants import TrendDirection

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


# 🔥 NOTE: TrendDirection now imported from unified_constants.py
# Previously defined here - now single source of truth


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
    MIN_DATA_FOR_CONFIDENCE = 20    # Minimum candles for high confidence
    
    # 🔥 NEW (Jan 24): Warmup requirements for reliable state detection
    MIN_WARMUP_FOR_LOW_CONFIDENCE = 20  # 🔥 FIX (Jan 24): Increased from 6 for more reliable detection    # Minimum candles for 40%+ confidence
    MIN_WARMUP_FOR_MED_CONFIDENCE = 12   # Minimum candles for 60%+ confidence
    MIN_WARMUP_FOR_HIGH_CONFIDENCE = 24  # Minimum candles for 80%+ confidence
    
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
        self._is_warmed_up = False
        self._warmup_candles = 0
        self._is_warmed_up = False
        self._warmup_candles = 0
        
    def warm_start(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        volumes: List[float],
        adx_values: List[float]
    ) -> TrendState:
        """
        Initialize TrendIntelligence with historical data.
        
        Call this once at startup with the last N 1h candles to properly
        calculate trend phase and confidence from the start.
        
        Args:
            highs: List of high prices (oldest first)
            lows: List of low prices
            closes: List of close prices  
            volumes: List of volumes
            adx_values: List of ADX values (must be same length or will use last)
            
        Returns:
            Current TrendState after processing historical data
        """
        if not closes:
            logger.warning("warm_start called with empty data")
            return self._state
            
        n = len(closes)
        logger.info(f"🔥 TrendIntelligence warm_start with {n} historical candles")
        
        # Ensure all arrays same length
        highs = list(highs)[-n:] if len(highs) >= n else list(highs)
        lows = list(lows)[-n:] if len(lows) >= n else list(lows)
        volumes = list(volumes)[-n:] if len(volumes) >= n else list(volumes) + [0.0] * (n - len(volumes))
        
        # ADX might be shorter due to warmup period
        if len(adx_values) < n:
            # Pad with first value or 15.0 (neutral)
            first_adx = adx_values[0] if adx_values else 15.0
            adx_values = [first_adx] * (n - len(adx_values)) + list(adx_values)
        else:
            adx_values = list(adx_values)[-n:]
        
        # Process each candle to build history and detect transitions
        from datetime import datetime, UTC, timedelta
        base_time = datetime.now(UTC) - timedelta(hours=n)
        
        for i in range(n):
            self._price_history.append(closes[i])
            self._high_history.append(highs[i])
            self._low_history.append(lows[i])
            self._volume_history.append(volumes[i])
            self._adx_history.append(adx_values[i])
        
        # Trim to max 100
        max_history = 100
        for hist in [self._price_history, self._adx_history, self._volume_history,
                     self._high_history, self._low_history]:
            while len(hist) > max_history:
                hist.pop(0)
        
        # Now run proper analysis with full history
        self._warmup_candles = len(self._price_history)
        
        # 🔥 FIX (Jan 25, 2026): Ensure ADX values are Python floats
        # Calculate metrics with historical data
        _adx_for_warmup = float(adx_values[-1]) if adx_values is not None and len(adx_values) > 0 else 15.0
        self._update_adx_metrics(_adx_for_warmup)
        self._update_structure_metrics()
        self._update_volume_metrics(float(volumes[-1]) if volumes else 0.0)
        
        # Detect current phase
        current_price = float(closes[-1])
        adx = float(adx_values[-1]) if adx_values is not None and len(adx_values) > 0 else 15.0
        atr = self._estimate_atr_from_history()
        
        new_phase, confidence, trigger = self._detect_phase_transition(
            current_price, adx, atr, datetime.now(UTC)
        )
        
        self._state.current_phase = new_phase
        self._state.confidence = self._calculate_confidence(confidence)
        self._is_warmed_up = True
        
        # Estimate phase age based on ADX stability
        self._state.phase_age_candles = self._estimate_phase_age(adx_values)
        
        self._update_trading_implications()
        
        logger.info(
            f"🧠 TrendIntelligence warmed up: {self._state.current_phase.value} | "
            f"Direction: {self._state.direction.value} | "
            f"Confidence: {self._state.confidence:.0%} | "
            f"Age: ~{self._state.phase_age_candles} candles | "
            f"ADX: {adx:.1f} (slope: {self._state.adx_slope:.2f}) | "
            f"Data: {n} candles (min_high_conf: {self.MIN_WARMUP_FOR_HIGH_CONFIDENCE})"
        )
        
        # 🔥 NEW: Log warning if confidence is low due to insufficient data
        if self._state.confidence < 0.50:
            logger.warning(
                f"⚠️ TrendIntelligence LOW CONFIDENCE ({self._state.confidence:.0%}): "
                f"Need {self.MIN_WARMUP_FOR_HIGH_CONFIDENCE - n} more candles for reliable detection"
            )
        
        return self._state
    
    def _estimate_atr_from_history(self) -> float:
        """Estimate ATR from historical high/low data"""
        if len(self._high_history) < 14:
            return 100.0
        tr_list = []
        for i in range(1, min(14, len(self._high_history))):
            h = self._high_history[-i]
            l = self._low_history[-i]
            c = self._price_history[-i-1] if i < len(self._price_history) else self._price_history[-i]
            tr = max(h - l, abs(h - c), abs(l - c))
            tr_list.append(tr)
        return float(np.mean(tr_list)) if tr_list else 100.0
    
    def _estimate_phase_age(self, adx_values: List[float]) -> int:
        """
        Estimate how long we've been in current phase based on ADX pattern.
        
        🔥 IMPROVED (Jan 25, 2026): More accurate age estimation
        - Counts backwards until ADX pattern breaks
        - Uses phase-specific thresholds
        - Returns minimum of 1 candle
        """
        if len(adx_values) < 5:
            return 1
        
        current_phase = self._state.current_phase
        # 🔥 FIX (Jan 25, 2026): Ensure ADX is Python float
        adx = float(adx_values[-1])
        
        # Count candles that match current phase criteria
        age = 0
        lookback_limit = min(50, len(adx_values))  # Look back up to 50 candles
        
        for i in range(len(adx_values) - 1, max(0, len(adx_values) - lookback_limit), -1):
            adx_val = float(adx_values[i])
            
            if current_phase == TrendPhase.RANGE_BOUND:
                # In range: ADX should be below birth threshold
                if adx_val < self.ADX_BIRTH_THRESHOLD:
                    age += 1
                else:
                    break
            elif current_phase in [TrendPhase.TREND_BIRTH, TrendPhase.TREND_ACCELERATION]:
                # Trending: ADX should be above birth threshold
                if adx_val > self.ADX_BIRTH_THRESHOLD:
                    age += 1
                else:
                    break
            elif current_phase == TrendPhase.TREND_MATURE:
                # Mature trend: ADX should be above mature threshold
                if adx_val > self.ADX_MATURE_THRESHOLD:
                    age += 1
                else:
                    break
            elif current_phase in [TrendPhase.TREND_EXHAUSTION, TrendPhase.DISTRIBUTION]:
                # Exhaustion: ADX declining or below mature
                if adx_val < self.ADX_MATURE_THRESHOLD:
                    age += 1  
                else:
                    break
            elif current_phase == TrendPhase.ACCUMULATION:
                # Accumulation: ADX very low, building
                # 🔄 REFACTORED Feb 04: Use ADXThresholds.RANGE for consistent threshold
                from src.config.trading_config import ADXThresholds
                if adx_val < ADXThresholds.RANGE:
                    age += 1
                else:
                    break
            else:
                age += 1
                if age > 10:
                    break
        
        # 🔥 NEW: Ensure minimum age based on warmup data quality
        # If we have lots of data, the age should reflect it
        min_age_from_data = min(5, len(adx_values) // 10)  # At least 1 per 10 candles of data
        
        return max(1, max(age, min_age_from_data))
    
    def _calculate_confidence(self, base_confidence: float) -> float:
        """
        Calculate confidence based on data quality and base confidence.
        
        🔥 IMPROVED (Jan 24): More conservative warmup requirements.
        Confidence must be EARNED through data, not assumed.
        """
        # Data quality factor: more data = higher confidence  
        data_count = len(self._price_history)
        
        # 🔥 Stricter warmup requirements
        if data_count < self.MIN_WARMUP_FOR_LOW_CONFIDENCE:
            data_factor = 0.35  # Very low confidence with minimal data
        elif data_count < self.MIN_WARMUP_FOR_MED_CONFIDENCE:
            data_factor = 0.55  # Still cautious
        elif data_count < self.MIN_WARMUP_FOR_HIGH_CONFIDENCE:
            data_factor = 0.75  # Getting confident
        elif data_count < self.MIN_DATA_FOR_CONFIDENCE:
            data_factor = 0.85
        else:
            data_factor = 1.0  # Full confidence only with enough data
        
        # ADX clarity: how clear is the regime?
        # 🔄 REFACTORED Feb 04: Use ADXThresholds for consistent thresholds
        from src.config.trading_config import ADXThresholds
        adx = self._state.adx_current
        if adx > 30:
            adx_factor = 1.0  # Very clear trend
        elif ADXThresholds.is_strong_trend(adx):  # > 25
            adx_factor = 0.95  # Clear trend
        elif adx < 15:
            adx_factor = 0.90  # Clear range  
        elif ADXThresholds.is_ranging(adx):  # < 18
            adx_factor = 0.80  # Probably range
        else:
            adx_factor = 0.70  # Transition zone - less certain
        
        # Structure clarity bonus
        hh = self._state.higher_highs_count
        ll = self._state.lower_lows_count
        structure_bonus = 0.0
        if (hh >= 2 and ll == 0) or (ll >= 2 and hh == 0):
            structure_bonus = 0.10  # Clear structure adds confidence
        elif hh > 0 or ll > 0:
            structure_bonus = 0.05
        
        # Phase age bonus: longer in phase = more confidence
        age = self._state.phase_age_candles
        age_bonus = min(0.10, age * 0.02)  # Max +10% for being in phase 5+ candles
        
        # Combine factors (weighted average instead of pure multiplication)
        raw_confidence = (base_confidence * 0.4 + data_factor * 0.3 + adx_factor * 0.3)
        confidence = raw_confidence + structure_bonus + age_bonus
        
        # 🔥 NEW: Hard cap confidence based on warmup state
        if not self._is_warmed_up:
            confidence = min(confidence, 0.50)  # Max 50% before warmup complete
        elif data_count < self.MIN_WARMUP_FOR_HIGH_CONFIDENCE:
            confidence = min(confidence, 0.70)  # Max 70% with limited data
        
        return max(0.25, min(0.95, confidence))  # Clamp between 25-95%
    
    @property
    def is_warmed_up(self) -> bool:
        return self._is_warmed_up
        
    def update(
        self,
        current_price: float,
        high: float,
        low: float,
        close: float,
        adx: float,
        volume: float,
        atr: float,
        timestamp: datetime | None = None
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
        
        # Use improved confidence calculation
        self._state.confidence = self._calculate_confidence(confidence)
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
        # 🔥 FIX M1 (Feb 07, 2026): Less sensitive divergence detection
        # Previous: Any volume below average = diverges
        # Now: Need SIGNIFICANT divergence (>20% below average on meaningful move)
        if len(self._price_history) >= 5:
            price_change = self._price_history[-1] - self._price_history[-5]
            price_change_pct = abs(price_change) / self._price_history[-5] if self._price_history[-5] > 0 else 0
            vol_ratio = vol_recent / vol_avg if vol_avg > 0 else 1.0
            
            # 🔥 FIX M1: Only flag divergence if:
            # 1. Meaningful price move (>0.1%)
            # 2. Volume significantly below average (<0.7x)
            is_meaningful_move = price_change_pct > 0.001  # 0.1% minimum
            is_significant_vol_drop = vol_ratio < 0.7  # 30% below average
            is_significant_vol_rise = vol_ratio > 1.3  # 30% above average
            
            if is_meaningful_move:
                if is_significant_vol_rise:
                    # High volume on move = confirms
                    self._state.volume_on_moves = "confirms"
                elif is_significant_vol_drop:
                    # Low volume on move = diverges
                    self._state.volume_on_moves = "diverges"
                else:
                    # Normal volume = neutral
                    self._state.volume_on_moves = "neutral"
            else:
                # Small move = neutral (don't overinterpret noise)
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
        
        # 🔥 FIX #61 (Feb 06): COLD START COHERENCE
        # When TrendPhase is stuck in RANGE_BOUND but ADX is very high,
        # this indicates a cold start problem - the system initialized
        # during an already-mature trend. Force transition to appropriate phase.
        if current == TrendPhase.RANGE_BOUND and adx >= self.ADX_MATURE_THRESHOLD + 5:
            # ADX >= 30 but still RANGE_BOUND = cold start issue
            logger.info(
                f"🔧 COLD START FIX: ADX={adx:.1f} too high for RANGE_BOUND | "
                f"Force transitioning to TREND_MATURE"
            )
            return TrendPhase.TREND_MATURE, 0.7, "Cold start correction (ADX already high)"
        
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
                # 🔥 FIX (Feb 06, 2026): NEUTRAL direction in exhaustion = allow BOTH
                # EdgeManager will decide direction based on price action
                self._state.trade_bias = "BOTH"
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
        elif phase == TrendPhase.TREND_EXHAUSTION:
            # 🔥 FIX (Feb 06, 2026): EXHAUSTION = fade opportunity, NOT wait
            # BULLISH exhaustion = SHORT opportunity
            # BEARISH exhaustion = LONG opportunity
            # Use CONSERVATIVE mode to fade with reduced size
            self._state.entry_mode = "CONSERVATIVE"
        elif phase == TrendPhase.DISTRIBUTION:
            self._state.entry_mode = "WAIT"  # True wait - no clarity
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
        size_bonus = 1.0  # Default no bonus
        
        if bias == "LONG_ONLY" and desired_direction == "SHORT":
            direction_allowed = False
        elif bias == "SHORT_ONLY" and desired_direction == "LONG":
            direction_allowed = False
        elif bias == "SHORT_PREFERRED":
            # SHORT is preferred but LONG is allowed with penalty
            if desired_direction == "SHORT":
                size_bonus = 1.0  # Full size for preferred direction
            else:
                size_bonus = 0.5  # Reduced size for non-preferred
        elif bias == "LONG_PREFERRED":
            # LONG is preferred but SHORT is allowed with penalty
            if desired_direction == "LONG":
                size_bonus = 1.0
            else:
                size_bonus = 0.5
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
        
        # Apply size bonus from preferred direction
        size_mult *= size_bonus
            
        # Score adjustment
        if phase == TrendPhase.TREND_BIRTH:
            score_adj = -1.0  # Easier entry
        elif phase == TrendPhase.TREND_ACCELERATION:
            score_adj = -0.5
        elif phase == TrendPhase.TREND_MATURE:
            score_adj = 0.0
        elif phase == TrendPhase.TREND_EXHAUSTION:
            # 🔥 FIX (Feb 06, 2026): EXHAUSTION fade trades need easier entry
            # If trading preferred direction (fade), reduce threshold
            if (bias == "SHORT_PREFERRED" and desired_direction == "SHORT") or \
               (bias == "LONG_PREFERRED" and desired_direction == "LONG"):
                score_adj = -0.5  # Easier entry for fade
            else:
                score_adj = +2.0  # Harder entry for with-trend
        elif phase == TrendPhase.DISTRIBUTION:
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
