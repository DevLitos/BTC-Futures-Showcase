"""
Anticipation Engine - Detects setups BEFORE breakout for earlier entries

PHILOSOPHY:
- Current system: Waits for BOS → enters AFTER the move starts
- This engine: Detects pre-breakout conditions → enters BEFORE breakout

KEY CONCEPTS:
1. Compression Detection: BB squeeze + low ATR = energy building
2. Order Flow Imbalance: Large resting orders above/below = magnet
3. Time-of-Day Patterns: London/NY open = predictable volatility
4. Failed Test Pattern: Price tests level, gets rejected → reversal coming

THIS IS NOT:
- Blind mean-reversion
- Counter-trend gambling
- Scalping

THIS IS:
- Identifying energy building (compression)
- Entering with tight SL before the crowd
- Getting 1.5-2R where current system gets 0.8-1R

Created: January 22, 2026
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class AnticipationSignal(str, Enum):
    """Pre-breakout signal types"""
    NONE = "NONE"
    COMPRESSION_BREAKOUT = "COMPRESSION_BREAKOUT"  # BB squeeze about to pop
    FAILED_TEST = "FAILED_TEST"                     # Level tested, rejected
    TIME_BASED_EXPANSION = "TIME_EXPANSION"         # Session open incoming
    STRUCTURE_BUILD = "STRUCTURE_BUILD"              # Higher lows building (accumulation)
    DIVERGENCE_UNWIND = "DIVERGENCE_UNWIND"         # RSI divergence about to resolve


@dataclass
class AnticipationState:
    """Current state of anticipation detection"""
    signal: AnticipationSignal = AnticipationSignal.NONE
    direction: Optional[str] = None  # LONG or SHORT
    confidence: float = 0.0  # 0-1
    entry_price: float = 0.0
    stop_loss: float = 0.0
    expected_target: float = 0.0
    time_valid_until: Optional[datetime] = None
    reasons: List[str] | None = None
    # 🔥 FIX #29 (Feb 04): Store structure level for breakout trigger
    structure_level: float = 0.0  # The level from "Building X from Y"
    
    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


class AnticipationEngine:
    """
    Detects pre-breakout conditions for earlier entries.
    
    ENTRY CONDITIONS (need 2+ for signal):
    1. BB Width compression (squeeze) - width < 20th percentile
    2. ADX < 20 but rising - trending about to begin
    3. Volume declining - calm before storm
    4. Structure building - HLs for LONG, LHs for SHORT
    5. Key level proximity - within 0.5 ATR of support/resistance
    
    EXTRA CONFIRMATION (bonus):
    - Time of day (session open approaching)
    - RSI divergence
    - Order book imbalance
    """
    
    # Thresholds
    # 🔥 FIX #60 (Feb 06): Use centralized ADXThresholds
    from src.config.trading_config import ADXThresholds
    
    BB_SQUEEZE_PERCENTILE = 20  # Width below this = squeezed
    ADX_LOW_THRESHOLD = ADXThresholds.TREND  # 20 - Use centralized value
    ADX_RISING_MIN_SLOPE = 0.3
    VOLUME_DECLINE_RATIO = 0.7  # Current vol < 70% of avg
    STRUCTURE_MIN_POINTS = 3    # Need 3 HLs or LHs
    LEVEL_PROXIMITY_ATR = 0.5   # Within 0.5 ATR of level
    
    # Time windows (minutes before session open)
    SESSION_ANTICIPATION_WINDOW = 15  # Look 15min before session
    
    def __init__(self):
        self._last_state = AnticipationState()
        self._bb_width_history: List[float] = []
        self._compression_start_time: Optional[datetime] = None
    
    def _detect_bb_squeeze(self, df_5m: pd.DataFrame) -> Tuple[bool, float, str]:
        """
        Detect Bollinger Band compression (squeeze).
        
        Returns:
            (is_squeezed, squeeze_score, reason)
        """
        if len(df_5m) < 50:
            return False, 0.0, "Insufficient data"
        
        closes = df_5m['close'].values
        
        # Calculate BB width
        sma20 = pd.Series(closes).rolling(20).mean().values  # pyright: ignore[reportAttributeAccessIssue]
        std20 = pd.Series(closes).rolling(20).std().values  # pyright: ignore[reportAttributeAccessIssue]
        
        upper = sma20 + 2 * std20  # type: ignore[operator]
        lower = sma20 - 2 * std20  # type: ignore[operator]
        
        # Width as percentage of price
        width_pct = ((upper - lower) / closes) * 100
        current_width = width_pct[-1]
        
        # Track history for percentile
        self._bb_width_history.append(current_width)
        if len(self._bb_width_history) > 200:
            self._bb_width_history = self._bb_width_history[-200:]
        
        # Calculate percentile
        if len(self._bb_width_history) >= 50:
            percentile = (np.sum(np.array(self._bb_width_history) < current_width) / 
                         len(self._bb_width_history)) * 100
        else:
            # Not enough history, use simple comparison
            avg_width = np.mean(width_pct[-50:])
            # 🔥 FIX (Jan 22, 2026): Safe division with guard
            if avg_width > 0 and not np.isnan(avg_width):
                percentile = (current_width / avg_width) * 50
            else:
                percentile = 50  # Neutral if no valid average
        
        # 🔥 FIX (Jan 22, 2026): Guard against NaN current_width
        if np.isnan(current_width):
            return False, 0.0, "Invalid BB width (NaN)"
        
        is_squeezed = percentile < self.BB_SQUEEZE_PERCENTILE
        squeeze_score = max(0, (self.BB_SQUEEZE_PERCENTILE - percentile) / self.BB_SQUEEZE_PERCENTILE)
        
        reason = f"BB width {current_width:.2f}% (percentile {percentile:.0f})"
        
        return is_squeezed, squeeze_score, reason
    
    def _detect_adx_coiling(self, df_5m: pd.DataFrame) -> Tuple[bool, float, str]:
        """
        Detect ADX coiling (low but rising = trend about to start).
        """
        if len(df_5m) < 30:
            return False, 0.0, "Insufficient data"
        
        from src.shared.indicators import calculate_adx
        
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        closes = df_5m['close'].to_numpy()
        
        adx, _ = calculate_adx(highs, lows, closes, 14)
        if len(adx) < 5:
            return False, 0.0, "ADX calculation failed"
        
        current_adx = adx[-1]
        adx_5_ago = adx[-5]
        
        # 🔥 FIX (Jan 22, 2026): Guard against NaN ADX values
        if np.isnan(current_adx) or np.isnan(adx_5_ago):
            return False, 0.0, "ADX values contain NaN"
        
        adx_slope = (current_adx - adx_5_ago) / 5
        
        # ADX low but rising = coiling
        is_coiling = (current_adx < self.ADX_LOW_THRESHOLD and 
                     adx_slope > self.ADX_RISING_MIN_SLOPE)
        
        coil_score = 0.0
        if is_coiling:
            # Score based on how low ADX is (lower = more potential)
            coil_score = (self.ADX_LOW_THRESHOLD - current_adx) / self.ADX_LOW_THRESHOLD
            coil_score *= (adx_slope / self.ADX_RISING_MIN_SLOPE)  # Boost by slope
            coil_score = min(1.0, coil_score)
        
        reason = f"ADX {current_adx:.1f} (slope {adx_slope:+.2f})"
        
        return is_coiling, coil_score, reason
    
    def _detect_volume_decline(self, df_5m: pd.DataFrame) -> Tuple[bool, float, str]:
        """
        Detect volume decline (calm before storm).
        """
        if len(df_5m) < 30 or 'volume' not in df_5m.columns:
            return False, 0.0, "No volume data"
        
        volumes = df_5m['volume'].to_numpy()
        
        # Recent vs average
        avg_vol = float(np.mean(np.asarray(volumes[-30:])))
        recent_vol = float(np.mean(np.asarray(volumes[-5:])))
        
        if avg_vol <= 0:
            return False, 0.0, "Invalid volume data"
        
        vol_ratio = recent_vol / avg_vol
        is_declining = vol_ratio < self.VOLUME_DECLINE_RATIO
        
        decline_score = 0.0
        if is_declining:
            decline_score = (self.VOLUME_DECLINE_RATIO - vol_ratio) / self.VOLUME_DECLINE_RATIO
            decline_score = min(1.0, decline_score)
        
        reason = f"Volume ratio {vol_ratio:.2f} ({recent_vol:.0f}/{avg_vol:.0f})"
        
        return is_declining, decline_score, reason
    
    def _detect_structure_building(self, df_5m: pd.DataFrame, current_price: float) -> Tuple[bool, str | None, float, float]:
        """
        Detect accumulation (HLs) or distribution (LHs) structure.
        
        Returns:
            (detected, direction, confidence, base_level)
        """
        if len(df_5m) < 30:
            return False, None, 0.0, 0.0
        
        highs = df_5m['high'].to_numpy()[-20:]
        lows = df_5m['low'].to_numpy()[-20:]
        
        # Find swing points (simplified: local max/min over 3 bars)
        swing_highs = []
        swing_lows = []
        
        for i in range(2, len(highs) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1] and \
               highs[i] > highs[i-2] and highs[i] > highs[i+2]:
                swing_highs.append((i, highs[i]))
            if lows[i] < lows[i-1] and lows[i] < lows[i+1] and \
               lows[i] < lows[i-2] and lows[i] < lows[i+2]:
                swing_lows.append((i, lows[i]))
        
        if len(swing_lows) < self.STRUCTURE_MIN_POINTS:
            return False, None, 0.0, 0.0
        
        # Check for Higher Lows (accumulation → LONG)
        hl_count = 0
        for i in range(1, len(swing_lows)):
            if swing_lows[i][1] > swing_lows[i-1][1]:
                hl_count += 1
        
        # Check for Lower Highs (distribution → SHORT)
        lh_count = 0
        if len(swing_highs) >= self.STRUCTURE_MIN_POINTS:
            for i in range(1, len(swing_highs)):
                if swing_highs[i][1] < swing_highs[i-1][1]:
                    lh_count += 1
        
        if hl_count >= self.STRUCTURE_MIN_POINTS - 1:
            confidence = min(1.0, hl_count / self.STRUCTURE_MIN_POINTS)
            base_level = swing_lows[-1][1]  # Most recent swing low
            return True, 'LONG', confidence, base_level
        
        if lh_count >= self.STRUCTURE_MIN_POINTS - 1:
            confidence = min(1.0, lh_count / self.STRUCTURE_MIN_POINTS)
            base_level = swing_highs[-1][1]  # Most recent swing high
            return True, 'SHORT', confidence, base_level
        
        return False, None, 0.0, 0.0
    
    def _check_session_timing(self, market_time: datetime) -> Tuple[bool, float, str]:
        """
        Check if we're in anticipation window before major session open.
        
        London: 08:00 UTC
        NY: 13:30 UTC (8:30 ET for NYSE cash open)
        """
        hour = market_time.hour
        minute = market_time.minute
        
        # London anticipation: 07:45-08:00 UTC
        if hour == 7 and minute >= 45:
            mins_to_open = 60 - minute
            score = max(0, (15 - mins_to_open) / 15)
            return True, score, f"London open in {mins_to_open}min"
        
        # NY anticipation: 13:15-13:30 UTC
        if hour == 13 and minute >= 15 and minute < 30:
            mins_to_open = 30 - minute
            score = max(0, (15 - mins_to_open) / 15)
            return True, score, f"NY open in {mins_to_open}min"
        
        return False, 0.0, "No session event imminent"
    
    def evaluate(self,
                df_5m: pd.DataFrame,
                current_price: float,
                atr: float,
                market_time: datetime,
                htf_bias: Optional[str] = None,
                important_levels: Optional[List[float]] = None,
                regime: Optional[str] = None) -> AnticipationState:
        """
        Evaluate pre-breakout conditions.
        
        Args:
            df_5m: 5-minute OHLC data
            current_price: Current market price
            atr: Current ATR
            market_time: Current market time
            htf_bias: HTF bias if known (LONG/SHORT)
            important_levels: Key S/R levels nearby
            regime: Market regime (RANGE, TREND, TRANSITION)
        
        Returns:
            AnticipationState with signal details
        """
        # 🔥 REGIME GATING (Jan 22, 2026)
        # Only allow anticipation in RANGE or TRANSITION
        # In TREND: only allow if aligned with HTF bias (pullback/continuation)
        allowed_regimes = {'RANGE', 'TRANSITION', 'UNKNOWN', None}
        
        if regime and regime.upper() not in allowed_regimes:
            # TREND regime - more restrictive
            if regime.upper() in {'TREND', 'TREND_EXPANSION', 'STRONG_TREND'}:
                # Only allow if we'll detect pullback continuation aligned with HTF
                if not htf_bias:
                    logger.debug(f"🔒 Anticipation blocked: TREND regime without HTF bias")
                    self._last_state = AnticipationState()
                    return self._last_state
        
        signals_detected = []
        total_score = 0.0
        
        # 1. BB Squeeze
        is_squeezed, squeeze_score, squeeze_reason = self._detect_bb_squeeze(df_5m)
        if is_squeezed:
            signals_detected.append(('squeeze', squeeze_score, squeeze_reason))
            total_score += squeeze_score
        
        # 2. ADX Coiling
        is_coiling, coil_score, coil_reason = self._detect_adx_coiling(df_5m)
        if is_coiling:
            signals_detected.append(('coiling', coil_score, coil_reason))
            total_score += coil_score
        
        # 3. Volume Decline
        is_declining, decline_score, decline_reason = self._detect_volume_decline(df_5m)
        if is_declining:
            signals_detected.append(('volume_decline', decline_score, decline_reason))
            total_score += decline_score
        
        # 4. Structure Building
        has_structure, struct_direction, struct_confidence, base_level = \
            self._detect_structure_building(df_5m, current_price)
        if has_structure:
            signals_detected.append(('structure', struct_confidence, f"Building {struct_direction} from {base_level:.0f}"))
            total_score += struct_confidence
        
        # 5. Session Timing (bonus)
        in_window, session_score, session_reason = self._check_session_timing(market_time)
        if in_window:
            signals_detected.append(('session', session_score * 0.5, session_reason))  # Half weight
            total_score += session_score * 0.5
        
        # Need at least 2 signals for anticipation
        if len(signals_detected) < 2:
            self._last_state = AnticipationState()
            return self._last_state
        
        # Determine direction
        direction = None
        if has_structure:
            direction = struct_direction
        elif htf_bias:
            direction = htf_bias
        else:
            # Default based on recent momentum
            closes = df_5m['close'].to_numpy()[-10:]
            direction = 'LONG' if closes[-1] > closes[0] else 'SHORT'
        
        # Calculate entry/SL/target
        # 🔥 FIX (Jan 22, 2026): Tight SL for anticipation - if no structure, use 0.6*ATR
        # This enforces "if no breakout soon, exit quickly" thesis
        closes = df_5m['close'].to_numpy()
        ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
        
        if direction == 'LONG':
            entry = current_price  # Enter now (anticipating)
            if base_level > 0:
                # Use structure level with small buffer
                stop_loss = base_level - (atr * 0.3)
            else:
                # No structure: use EMA20 or tight ATR (whichever is tighter)
                ema_sl = ema20 - (atr * 0.3)
                tight_sl = current_price - (atr * 0.6)  # Tight 0.6 ATR
                stop_loss = max(ema_sl, tight_sl)  # Use tighter (closer to price)
            target = current_price + (atr * 2)  # 2 ATR target
        else:
            entry = current_price
            if base_level > 0:
                stop_loss = base_level + (atr * 0.3)
            else:
                ema_sl = ema20 + (atr * 0.3)
                tight_sl = current_price + (atr * 0.6)
                stop_loss = min(ema_sl, tight_sl)  # Use tighter (closer to price)
            target = current_price - (atr * 2)
        
        # Normalize confidence
        confidence = min(1.0, total_score / 3.0)  # Cap at 3 signals for 100%
        
        # 🔥 LEVEL PROXIMITY VALIDATION (Jan 22, 2026)
        # If important_levels provided, check proximity for higher confidence
        # If no levels or far from levels, reduce confidence
        level_proximity_bonus = 0.0
        if important_levels and len(important_levels) > 0:
            distances = [abs(current_price - lvl) for lvl in important_levels]
            nearest_distance = min(distances)
            nearest_distance_atr = nearest_distance / atr if atr > 0 else float('inf')
            
            if nearest_distance_atr <= self.LEVEL_PROXIMITY_ATR:
                # Close to key level - boost confidence
                level_proximity_bonus = 0.15 * (1 - nearest_distance_atr / self.LEVEL_PROXIMITY_ATR)
                confidence = min(1.0, confidence + level_proximity_bonus)
                signals_detected.append(('level_proximity', level_proximity_bonus, f"Near level at {nearest_distance_atr:.2f} ATR"))
            else:
                # Far from levels - reduce confidence slightly
                confidence *= 0.85
        else:
            # No levels provided - reduce confidence
            confidence *= 0.9
        
        # Determine signal type (most significant)
        if is_squeezed and squeeze_score > 0.5:
            signal_type = AnticipationSignal.COMPRESSION_BREAKOUT
        elif has_structure:
            signal_type = AnticipationSignal.STRUCTURE_BUILD
        elif in_window:
            signal_type = AnticipationSignal.TIME_BASED_EXPANSION
        else:
            signal_type = AnticipationSignal.NONE  # Weak signal
        
        reasons = [f"{name}: {reason}" for name, _, reason in signals_detected]
        
        # Signal valid for 10 minutes (must act or lose it)
        valid_until = market_time + timedelta(minutes=10)
        
        self._last_state = AnticipationState(
            signal=signal_type,
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=stop_loss,
            expected_target=target,
            time_valid_until=valid_until,
            reasons=reasons,
            structure_level=base_level if has_structure else 0.0  # 🔥 FIX #29: Store for trigger
        )
        
        # 🔥 FIX (Feb 02, 2026): REMOVED duplicate log here - already logged in idle_state_handlers
        # The previous log caused duplicate "ANTICIPATION" messages in logs
        # if signal_type != AnticipationSignal.NONE:
        #     logger.info(f"🔮 ANTICIPATION: {signal_type.value} {direction} | conf={confidence:.0%}")
        #     for reason in reasons:
        #         logger.info(f"   → {reason}")
        
        return self._last_state
    
    def get_state(self) -> AnticipationState:
        """Get current anticipation state"""
        return self._last_state

    # =========================================================================
    # 🔥 FIX #58 (Feb 06, 2026): MÉTODOS ADICIONALES DE ANTICIPACIÓN
    # 
    # El método evaluate() original detecta COMPRESSION_BREAKOUT.
    # Estos métodos complementarios detectan otros patrones PRE-MOVE:
    # 
    # 1. TREND_PULLBACK - Pullback a EMA en tendencia fuerte
    # 2. FAILED_TEST - Rechazo de nivel clave → reversión
    # 3. DIVERGENCE - RSI divergence → agotamiento
    # 4. MOMENTUM_EXHAUSTION - ADX cayendo + precio en extremo
    # =========================================================================

    def detect_trend_pullback(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        atr: float,
        htf_bias: Optional[str] = None,
        adx_1h: float = 0.0
    ) -> Optional[AnticipationState]:
        """
        Detecta pullback a zona de valor en tendencia establecida.
        
        CONDICIONES:
        - Tendencia clara (ADX 1H >= 25)
        - Precio retrocede a EMA20/50 en 5m
        - RSI no en extremo (30-70)
        - Volumen bajo durante pullback
        
        ENTRY: En el pullback, ANTES de que retome la tendencia
        """
        if len(df_5m) < 60 or adx_1h < 25:
            return None
        
        closes = df_5m['close'].to_numpy()
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        
        # EMAs
        ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1]
        
        # RSI simple
        delta = pd.Series(closes).diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean().iloc[-1]
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean().iloc[-1]
        rsi = 100 - (100 / (1 + gain / loss)) if loss > 0 else 50
        
        # Determinar dirección de tendencia
        trend_up = ema20 > ema50 and closes[-1] > ema50
        trend_down = ema20 < ema50 and closes[-1] < ema50
        
        if not (trend_up or trend_down):
            return None
        
        # Verificar pullback a EMA
        ema_zone_upper = max(ema20, ema50) + atr * 0.3
        ema_zone_lower = min(ema20, ema50) - atr * 0.3
        in_ema_zone = ema_zone_lower <= current_price <= ema_zone_upper
        
        if not in_ema_zone:
            return None
        
        # RSI no en extremo
        if rsi < 30 or rsi > 70:
            return None
        
        # Volumen bajo (pullback silencioso)
        if 'volume' in df_5m.columns:
            recent_vol = float(np.mean(df_5m['volume'].to_numpy()[-5:]))
            avg_vol = float(np.mean(df_5m['volume'].to_numpy()[-30:]))
            if avg_vol > 0 and recent_vol / avg_vol > 0.8:
                return None  # Volumen alto = no es pullback tranquilo
        
        # Dirección y niveles
        direction = 'LONG' if trend_up else 'SHORT'
        
        # Conflicto con HTF bias
        if htf_bias and htf_bias != direction:
            return None
        
        if direction == 'LONG':
            entry = current_price
            stop_loss = min(ema20, ema50) - atr * 0.5
            target = current_price + atr * 2
            structure_level = float(np.min(lows[-10:]))  # Swing low reciente
        else:
            entry = current_price
            stop_loss = max(ema20, ema50) + atr * 0.5
            target = current_price - atr * 2
            structure_level = float(np.max(highs[-10:]))  # Swing high reciente
        
        confidence = 0.60
        # Boost si ADX muy fuerte
        if adx_1h >= 35:
            confidence += 0.10
        # Boost si RSI cerca de 50 (neutral = mejor timing)
        if 45 <= rsi <= 55:
            confidence += 0.05
        
        return AnticipationState(
            signal=AnticipationSignal.STRUCTURE_BUILD,  # Reusa enum existente
            direction=direction,
            confidence=min(0.85, confidence),
            entry_price=entry,
            stop_loss=stop_loss,
            expected_target=target,
            time_valid_until=datetime.now(timezone.utc) + timedelta(minutes=15),
            reasons=[
                f"TREND_PULLBACK: {direction} trend (ADX={adx_1h:.1f})",
                f"Price at EMA zone ({ema20:.0f}/{ema50:.0f})",
                f"RSI neutral ({rsi:.1f})"
            ],
            structure_level=structure_level
        )

    def detect_failed_test(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        atr: float,
        key_levels: Optional[List[float]] = None
    ) -> Optional[AnticipationState]:
        """
        Detecta rechazo de nivel clave → anticipar reversión.
        
        CONDICIONES:
        - Precio tocó nivel clave en últimas 5 velas
        - Mecha larga en dirección del test (rechazo)
        - Cierre lejos del nivel (no rompió)
        - Volumen spike en el test (institucional rechazando)
        
        ENTRY: Después del rechazo, dirección opuesta al test
        """
        if len(df_5m) < 20 or not key_levels or len(key_levels) == 0:
            return None
        
        closes = df_5m['close'].to_numpy()
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        
        # Buscar test reciente de nivel
        for level in key_levels:
            distance_to_level = abs(current_price - level)
            
            # Nivel debe estar cerca pero no tocándolo ahora
            if distance_to_level < atr * 0.2 or distance_to_level > atr * 1.5:
                continue
            
            # Verificar si testeamos el nivel en últimas 5 velas
            level_tested_high = any(h >= level - atr * 0.1 and h <= level + atr * 0.1 
                                    for h in highs[-5:])
            level_tested_low = any(l >= level - atr * 0.1 and l <= level + atr * 0.1 
                                   for l in lows[-5:])
            
            if not (level_tested_high or level_tested_low):
                continue
            
            # Determinar dirección del test
            if level_tested_high and current_price < level:
                # Testeó resistencia, rechazó hacia abajo → SHORT
                direction = 'SHORT'
                # Verificar mecha superior (rechazo)
                wick_ratios = [(highs[i] - max(closes[i], closes[i-1] if i > 0 else closes[i])) / 
                              (highs[i] - lows[i] + 0.01) for i in range(-5, 0)]
                has_rejection_wick = any(r > 0.4 for r in wick_ratios)
                
                if not has_rejection_wick:
                    continue
                
                entry = current_price
                stop_loss = level + atr * 0.3
                target = current_price - atr * 2
                structure_level = level
                
            elif level_tested_low and current_price > level:
                # Testeó soporte, rechazó hacia arriba → LONG
                direction = 'LONG'
                # Verificar mecha inferior (rechazo)
                wick_ratios = [(min(closes[i], closes[i-1] if i > 0 else closes[i]) - lows[i]) / 
                              (highs[i] - lows[i] + 0.01) for i in range(-5, 0)]
                has_rejection_wick = any(r > 0.4 for r in wick_ratios)
                
                if not has_rejection_wick:
                    continue
                
                entry = current_price
                stop_loss = level - atr * 0.3
                target = current_price + atr * 2
                structure_level = level
            else:
                continue
            
            # Calcular confianza
            confidence = 0.55
            # Boost por distancia del nivel (más lejos = más confirmación)
            dist_atr = distance_to_level / atr
            if dist_atr >= 0.5:
                confidence += 0.10
            # Boost por wick grande
            if any(r > 0.6 for r in wick_ratios):
                confidence += 0.10
            
            return AnticipationState(
                signal=AnticipationSignal.FAILED_TEST,
                direction=direction,
                confidence=min(0.80, confidence),
                entry_price=entry,
                stop_loss=stop_loss,
                expected_target=target,
                time_valid_until=datetime.now(timezone.utc) + timedelta(minutes=10),
                reasons=[
                    f"FAILED_TEST: {direction} after rejection @ {level:.0f}",
                    f"Distance from level: {dist_atr:.2f} ATR",
                    f"Rejection wick detected"
                ],
                structure_level=structure_level
            )
        
        return None

    def detect_momentum_exhaustion(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        atr: float,
        adx_5m: float = 0.0
    ) -> Optional[AnticipationState]:
        """
        Detecta agotamiento de momentum → anticipar reversión/pausa.
        
        CONDICIONES:
        - ADX 5m fue alto (>30) pero ahora cayendo
        - RSI en extremo (>70 o <30)
        - Precio en extremo de rango reciente
        - Divergencia RSI vs precio (opcional pero potente)
        
        ENTRY: Contra la tendencia agotada, con SL ajustado
        """
        if len(df_5m) < 50 or adx_5m < 15:
            return None
        
        closes = df_5m['close'].to_numpy()
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        
        # RSI
        delta = pd.Series(closes).diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-10)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_now = rsi_series.iloc[-1]
        rsi_5_ago = rsi_series.iloc[-5] if len(rsi_series) >= 5 else rsi_now
        
        # ADX slope (necesitamos historial)
        from src.shared.indicators import calculate_adx
        adx_arr, _ = calculate_adx(highs, lows, closes, 14)
        if len(adx_arr) < 10:
            return None
        
        adx_now = adx_arr[-1]
        adx_5_ago = adx_arr[-5]
        adx_falling = adx_now < adx_5_ago - 2  # ADX cayendo significativamente
        
        if not adx_falling:
            return None
        
        # Rango reciente
        range_high = float(np.max(highs[-20:]))
        range_low = float(np.min(lows[-20:]))
        range_size = range_high - range_low
        
        if range_size <= 0:
            return None
        
        price_position = (current_price - range_low) / range_size
        
        # Detectar exhaustion
        direction = None
        reasons = []
        
        # Exhaustion alcista (RSI alto + precio cerca de máximos + ADX cayendo)
        if rsi_now > 65 and price_position > 0.75 and adx_falling:
            direction = 'SHORT'
            # Divergencia bearish? (precio sube, RSI baja)
            price_rising = closes[-1] > closes[-5]
            rsi_falling = rsi_now < rsi_5_ago
            if price_rising and rsi_falling:
                reasons.append("BEARISH DIVERGENCE detected")
        
        # Exhaustion bajista (RSI bajo + precio cerca de mínimos + ADX cayendo)
        elif rsi_now < 35 and price_position < 0.25 and adx_falling:
            direction = 'LONG'
            # Divergencia bullish? (precio baja, RSI sube)
            price_falling = closes[-1] < closes[-5]
            rsi_rising = rsi_now > rsi_5_ago
            if price_falling and rsi_rising:
                reasons.append("BULLISH DIVERGENCE detected")
        
        if not direction:
            return None
        
        # Niveles
        if direction == 'LONG':
            entry = current_price
            stop_loss = range_low - atr * 0.3
            target = current_price + atr * 1.5  # Target conservador en reversal
            structure_level = range_low
        else:
            entry = current_price
            stop_loss = range_high + atr * 0.3
            target = current_price - atr * 1.5
            structure_level = range_high
        
        # Confianza base
        confidence = 0.50
        if len(reasons) > 0:  # Tiene divergencia
            confidence += 0.15
        if adx_now < adx_5_ago - 5:  # ADX cayendo fuerte
            confidence += 0.10
        
        reasons.insert(0, f"EXHAUSTION: ADX falling ({adx_5_ago:.1f}→{adx_now:.1f})")
        reasons.append(f"RSI={rsi_now:.1f}, Price position={price_position:.0%}")
        
        return AnticipationState(
            signal=AnticipationSignal.DIVERGENCE_UNWIND,
            direction=direction,
            confidence=min(0.75, confidence),
            entry_price=entry,
            stop_loss=stop_loss,
            expected_target=target,
            time_valid_until=datetime.now(timezone.utc) + timedelta(minutes=10),
            reasons=reasons,
            structure_level=structure_level
        )

    def evaluate_all(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        atr: float,
        market_time: datetime,
        htf_bias: Optional[str] = None,
        important_levels: Optional[List[float]] = None,
        regime: Optional[str] = None,
        adx_1h: float = 0.0,
        adx_5m: float = 0.0
    ) -> AnticipationState:
        """
        Evalúa TODOS los métodos de anticipación y retorna el mejor.
        
        Prioridad:
        1. COMPRESSION_BREAKOUT (método original) - mejor timing
        2. TREND_PULLBACK - alta probabilidad en tendencias
        3. FAILED_TEST - buen R:R en niveles
        4. MOMENTUM_EXHAUSTION - contra-tendencia, más riesgoso
        
        Retorna el de mayor confianza entre los detectados.
        """
        candidates: List[AnticipationState] = []
        
        # 1. Original COMPRESSION_BREAKOUT (evaluate)
        compression_state = self.evaluate(
            df_5m=df_5m,
            current_price=current_price,
            atr=atr,
            market_time=market_time,
            htf_bias=htf_bias,
            important_levels=important_levels,
            regime=regime
        )
        if compression_state.signal != AnticipationSignal.NONE:
            candidates.append(compression_state)
        
        # 2. TREND_PULLBACK (nuevo)
        pullback_state = self.detect_trend_pullback(
            df_5m=df_5m,
            current_price=current_price,
            atr=atr,
            htf_bias=htf_bias,
            adx_1h=adx_1h
        )
        if pullback_state:
            candidates.append(pullback_state)
        
        # 3. FAILED_TEST (nuevo)
        failed_test_state = self.detect_failed_test(
            df_5m=df_5m,
            current_price=current_price,
            atr=atr,
            key_levels=important_levels
        )
        if failed_test_state:
            candidates.append(failed_test_state)
        
        # 4. MOMENTUM_EXHAUSTION (nuevo) - solo si no hay otras señales
        if not candidates:
            exhaustion_state = self.detect_momentum_exhaustion(
                df_5m=df_5m,
                current_price=current_price,
                atr=atr,
                adx_5m=adx_5m
            )
            if exhaustion_state:
                candidates.append(exhaustion_state)
        
        # Retornar el de mayor confianza
        if not candidates:
            self._last_state = AnticipationState()
            return self._last_state
        
        best = max(candidates, key=lambda s: s.confidence)
        self._last_state = best
        return best


# Singleton
_anticipation_engine: Optional[AnticipationEngine] = None

def get_anticipation_engine() -> AnticipationEngine:
    global _anticipation_engine
    if _anticipation_engine is None:
        _anticipation_engine = AnticipationEngine()
    return _anticipation_engine
