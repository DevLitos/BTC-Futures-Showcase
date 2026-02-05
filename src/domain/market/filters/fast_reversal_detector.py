"""
Fast Reversal Detector - React to trend reversals in real-time

Problem: Traditional trend detection (EMA, HH/HL counting) lags behind
actual market reversals. 5 bearish candles followed by 1 bullish reversal
still shows bearish bias because the scoring needs 4+ candles to flip.

Solution: Detect single-candle reversals based on:
1. Strong body (>= 1.2x ATR) against current direction
2. Engulfing pattern (covers previous candle completely)
3. Volume spike (1.5x average)
4. Price momentum shift

When detected: Returns new direction to override micro_momentum immediately.

Created: Feb 2, 2026
"""

from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any
import numpy as np
import logging

from src.config.trading_config import RiskManagement

logger = logging.getLogger(__name__)


@dataclass
class ReversalSignal:
    """Describes a detected reversal signal."""
    detected: bool
    new_direction: Optional[str]  # 'LONG' or 'SHORT'
    signal_type: str  # 'ENGULFING', 'STRONG_BODY', 'MOMENTUM_SHIFT', 'NONE'
    confidence: float  # 0.0 to 1.0
    reason: str
    body_atr: float  # Body size in ATRs
    volume_mult: float  # Volume vs average
    

class FastReversalDetector:
    """
    Detect fast trend reversals using real-time candle analysis.
    
    This detector runs every tick and looks for:
    1. Strong reversal candles (big body opposite to bias)
    2. Engulfing patterns
    3. Momentum shifts (volume + body combined)
    
    When a reversal is detected, it returns the new direction which
    should override the lagging indicators.
    """
    
    def __init__(self):
        self._override_direction: Optional[str] = None
        self._override_expires_at_tick: int = 0
        self._last_signal: Optional[ReversalSignal] = None
        # 🔥 NEW: Swing levels for structure validation
        self._swing_high: Optional[float] = None
        self._swing_low: Optional[float] = None
        
    def check(self, 
              candles_1m: List[Dict[str, Any]],
              current_bias: str,
              atr_1m: float,
              current_tick: int = 0,
              swing_high: Optional[float] = None,
              swing_low: Optional[float] = None) -> ReversalSignal:
        """
        Check for fast reversal signals.
        
        Args:
            candles_1m: Recent 1m candles (need at least 10)
            current_bias: Current trading direction ('LONG' or 'SHORT')
            atr_1m: Current ATR on 1m timeframe
            current_tick: Current tick number (for expiration)
            swing_high: Current swing high (for structure validation)
            swing_low: Current swing low (for structure validation)
            
        Returns:
            ReversalSignal with detection result
        """
        # Check if feature is disabled
        if not RiskManagement.FAST_REVERSAL_ENABLED:
            return ReversalSignal(
                detected=False,
                new_direction=None,
                signal_type='DISABLED',
                confidence=0.0,
                reason="Fast reversal detection disabled",
                body_atr=0.0,
                volume_mult=0.0
            )
        
        # 🔥 NEW: Check structure break requirement
        require_structure_break = getattr(RiskManagement, 'FAST_REVERSAL_REQUIRE_STRUCTURE_BREAK', False)
        
        # Store swing levels for structure validation
        self._swing_high = swing_high
        self._swing_low = swing_low
        
        # Check if we have an active override
        if self._override_direction and current_tick < self._override_expires_at_tick:
            return ReversalSignal(
                detected=True,
                new_direction=self._override_direction,
                signal_type='OVERRIDE_ACTIVE',
                confidence=1.0,
                reason=f"Override active until tick {self._override_expires_at_tick}",
                body_atr=0.0,
                volume_mult=0.0
            )
        
        # Clear expired override
        if current_tick >= self._override_expires_at_tick:
            self._override_direction = None
            
        # Need minimum candles
        if not candles_1m or len(candles_1m) < 10:
            return self._no_signal("Not enough candles")
            
        if atr_1m <= 0:
            return self._no_signal("Invalid ATR")
            
        # Get recent candles
        current = candles_1m[-1]
        prev = candles_1m[-2]
        prev2 = candles_1m[-3]
        
        # Calculate candle properties
        curr_open = float(current.get('open', 0))
        curr_close = float(current.get('close', 0))
        curr_high = float(current.get('high', 0))
        curr_low = float(current.get('low', 0))
        curr_volume = float(current.get('volume', 1))
        
        prev_open = float(prev.get('open', 0))
        prev_close = float(prev.get('close', 0))
        prev_high = float(prev.get('high', 0))
        prev_low = float(prev.get('low', 0))
        
        # Body analysis
        curr_body = abs(curr_close - curr_open)
        curr_bullish = curr_close > curr_open
        prev_body = abs(prev_close - prev_open)
        prev_bullish = prev_close > prev_open
        
        body_atr = curr_body / atr_1m if atr_1m > 0 else 0
        
        # Volume analysis
        volumes = [float(c.get('volume', 1)) for c in candles_1m[-10:-1]]
        avg_volume = np.mean(volumes) if volumes else 1.0
        volume_mult = curr_volume / avg_volume if avg_volume > 0 else 1.0
        
        # =====================================================================
        # SIGNAL 1: STRONG BODY REVERSAL
        # Single candle with body >= 1.2x ATR opposite to current bias
        # =====================================================================
        min_body = RiskManagement.FAST_REVERSAL_MIN_BODY_ATR
        min_volume = RiskManagement.FAST_REVERSAL_VOLUME_MULT
        
        strong_body_signal = False
        strong_body_direction = None
        
        if body_atr >= min_body:
            if curr_bullish and current_bias == 'SHORT':
                # Strong bullish candle against SHORT bias
                strong_body_signal = True
                strong_body_direction = 'LONG'
            elif not curr_bullish and current_bias == 'LONG':
                # Strong bearish candle against LONG bias
                strong_body_signal = True
                strong_body_direction = 'SHORT'
        
        # =====================================================================
        # SIGNAL 2: ENGULFING PATTERN
        # Current candle completely engulfs previous candle body
        # =====================================================================
        engulfing_signal = False
        engulfing_direction = None
        
        if RiskManagement.FAST_REVERSAL_ENGULF_REQUIRED or True:  # Always check
            # Bullish engulfing: curr bullish, engulfs prev bearish
            bullish_engulfing = (
                curr_bullish and 
                not prev_bullish and
                curr_close > prev_open and
                curr_open < prev_close
            )
            
            # Bearish engulfing: curr bearish, engulfs prev bullish
            bearish_engulfing = (
                not curr_bullish and
                prev_bullish and
                curr_close < prev_open and
                curr_open > prev_close
            )
            
            if bullish_engulfing and current_bias == 'SHORT':
                engulfing_signal = True
                engulfing_direction = 'LONG'
            elif bearish_engulfing and current_bias == 'LONG':
                engulfing_signal = True
                engulfing_direction = 'SHORT'
        
        # =====================================================================
        # SIGNAL 3: MOMENTUM SHIFT
        # Strong body + high volume = high conviction reversal
        # =====================================================================
        momentum_shift_signal = False
        momentum_shift_direction = None
        
        if body_atr >= min_body * 0.8 and volume_mult >= min_volume:
            # Combined strong body + volume
            if curr_bullish and current_bias == 'SHORT':
                momentum_shift_signal = True
                momentum_shift_direction = 'LONG'
            elif not curr_bullish and current_bias == 'LONG':
                momentum_shift_signal = True
                momentum_shift_direction = 'SHORT'
        
        # =====================================================================
        # DETERMINE FINAL SIGNAL
        # Priority: Engulfing > Strong Body > Momentum Shift
        # =====================================================================
        
        # Calculate confidence based on multiple confirmations
        confirmations = sum([
            strong_body_signal,
            engulfing_signal,
            momentum_shift_signal,
            volume_mult >= min_volume
        ])
        confidence = min(1.0, float(confirmations) / 3.0)
        
        # Cast numpy floats to Python floats for type safety
        body_atr_float = float(body_atr)
        volume_mult_float = float(volume_mult)
        
        # 🔥 NEW: Structure break validation
        # Prevents false LONG signals during downtrends (and vice versa)
        require_structure_break = getattr(RiskManagement, 'FAST_REVERSAL_REQUIRE_STRUCTURE_BREAK', False)
        
        def _validates_structure_break(direction: str) -> bool:
            """
            Validates that price actually breaks structure for reversal.
            LONG: needs to break above swing high
            SHORT: needs to break below swing low
            """
            if not require_structure_break:
                return True  # Disabled, allow all signals
            
            current_price = curr_close
            
            if direction == 'LONG':
                # For LONG reversal, price should break above recent swing high
                if self._swing_high is not None:
                    if current_price <= self._swing_high:
                        logger.debug(
                            f"🚫 FAST_REVERSAL LONG blocked: price {current_price:.2f} "
                            f"hasn't broken swing high {self._swing_high:.2f}"
                        )
                        return False
                return True  # No swing high to validate against
                
            elif direction == 'SHORT':
                # For SHORT reversal, price should break below recent swing low  
                if self._swing_low is not None:
                    if current_price >= self._swing_low:
                        logger.debug(
                            f"🚫 FAST_REVERSAL SHORT blocked: price {current_price:.2f} "
                            f"hasn't broken swing low {self._swing_low:.2f}"
                        )
                        return False
                return True  # No swing low to validate against
            
            return True  # Unknown direction, allow
        
        if engulfing_signal and engulfing_direction and (strong_body_signal or volume_mult >= min_volume):
            # High confidence: Engulfing + (strong body OR volume)
            # 🔥 NEW: Validate structure break before accepting
            if not _validates_structure_break(engulfing_direction):
                return self._no_signal(f"Engulfing {engulfing_direction} blocked: structure not broken")
            
            self._set_override(engulfing_direction, current_tick)
            signal = ReversalSignal(
                detected=True,
                new_direction=engulfing_direction,
                signal_type='ENGULFING',
                confidence=confidence,
                reason=f"Engulfing + {'body' if strong_body_signal else 'volume'} confirmation",
                body_atr=body_atr_float,
                volume_mult=volume_mult_float
            )
            self._last_signal = signal
            return signal
        
        elif strong_body_signal and strong_body_direction and volume_mult >= min_volume:
            # High confidence: Strong body + volume
            # 🔥 NEW: Validate structure break before accepting
            if not _validates_structure_break(strong_body_direction):
                return self._no_signal(f"Strong body {strong_body_direction} blocked: structure not broken")
            
            self._set_override(strong_body_direction, current_tick)
            signal = ReversalSignal(
                detected=True,
                new_direction=strong_body_direction,
                signal_type='STRONG_BODY',
                confidence=confidence,
                reason=f"Strong body ({body_atr:.2f}x ATR) + volume ({volume_mult:.1f}x avg)",
                body_atr=body_atr_float,
                volume_mult=volume_mult_float
            )
            self._last_signal = signal
            return signal
        
        elif engulfing_signal and engulfing_direction:
            # Medium confidence: Just engulfing
            # Require engulfing to have above-average volume
            if volume_mult >= 1.0:
                # 🔥 NEW: Validate structure break before accepting
                if not _validates_structure_break(engulfing_direction):
                    return self._no_signal(f"Engulfing {engulfing_direction} blocked: structure not broken")
                
                self._set_override(engulfing_direction, current_tick)
                signal = ReversalSignal(
                    detected=True,
                    new_direction=engulfing_direction,
                    signal_type='ENGULFING',
                    confidence=confidence * 0.7,
                    reason=f"Engulfing pattern detected",
                    body_atr=body_atr_float,
                    volume_mult=volume_mult_float
                )
                self._last_signal = signal
                return signal
        
        elif momentum_shift_signal and momentum_shift_direction:
            # Medium confidence: Momentum shift
            # 🔥 NEW: Validate structure break before accepting
            if not _validates_structure_break(momentum_shift_direction):
                return self._no_signal(f"Momentum shift {momentum_shift_direction} blocked: structure not broken")
            
            self._set_override(momentum_shift_direction, current_tick)
            signal = ReversalSignal(
                detected=True,
                new_direction=momentum_shift_direction,
                signal_type='MOMENTUM_SHIFT',
                confidence=confidence * 0.8,
                reason=f"Momentum shift: body {body_atr:.2f}x ATR, volume {volume_mult:.1f}x",
                body_atr=body_atr_float,
                volume_mult=volume_mult_float
            )
            self._last_signal = signal
            return signal
        
        # =====================================================================
        # 🔥 SIGNAL 4: SQUEEZE BREAK (Feb 05 - from ANTICIPATION ideas)
        # Detecta cuando el precio sale de una compresión (Bollinger narrow)
        # contra el bias actual → señal de reversal temprana
        # =====================================================================
        squeeze_break_signal = self._check_squeeze_break(
            candles_1m, current_bias, atr_1m, curr_close
        )
        if squeeze_break_signal and squeeze_break_signal[0]:
            squeeze_dir = squeeze_break_signal[1]
            squeeze_conf = squeeze_break_signal[2]
            if _validates_structure_break(squeeze_dir):
                self._set_override(squeeze_dir, current_tick)
                signal = ReversalSignal(
                    detected=True,
                    new_direction=squeeze_dir,
                    signal_type='SQUEEZE_BREAK',
                    confidence=squeeze_conf,
                    reason=f"Squeeze break against {current_bias} bias",
                    body_atr=body_atr_float,
                    volume_mult=volume_mult_float
                )
                self._last_signal = signal
                return signal
        
        return self._no_signal("No reversal signal")
    
    def _set_override(self, direction: str, current_tick: int) -> None:
        """Set direction override with expiration."""
        self._override_direction = direction
        duration = RiskManagement.FAST_REVERSAL_OVERRIDE_DURATION_TICKS
        self._override_expires_at_tick = current_tick + duration
        logger.warning(
            f"🔄 FAST REVERSAL: Override set to {direction} for {duration} ticks"
        )
    
    def _check_squeeze_break(
        self,
        candles_1m: List[Dict[str, Any]],
        current_bias: str,
        atr_1m: float,
        current_price: float
    ) -> Optional[Tuple[bool, str, float]]:
        """
        🔥 NEW (Feb 05): Check for squeeze break reversal (from ANTICIPATION ideas).
        
        Detecta cuando precio sale de compresión (BB narrow) contra bias actual.
        Esto es una señal temprana de reversal - antes de que ocurra la vela grande.
        
        Returns:
            (detected, new_direction, confidence) or None
        """
        if len(candles_1m) < 20:
            return None
        
        try:
            # Calcular Bollinger Bands width de las últimas 20 velas
            closes = np.array([float(c.get('close', 0)) for c in candles_1m[-20:]])
            bb_mid = np.mean(closes)
            bb_std = np.std(closes)
            
            if bb_std <= 0 or atr_1m <= 0:
                return None
            
            # BB width normalizado por ATR (mide compresión)
            bb_width_atr = (2 * bb_std) / atr_1m
            
            # Umbral de squeeze: BB width < 0.8x ATR = comprimido
            is_squeezed = bb_width_atr < 0.8
            
            if not is_squeezed:
                return None
            
            # Verificar dirección del breakout de la compresión
            bb_upper = bb_mid + bb_std
            bb_lower = bb_mid - bb_std
            
            # Breakout hacia arriba de la compresión contra SHORT bias
            if current_price > bb_upper and current_bias == 'SHORT':
                confidence = min(0.75, 0.5 + (0.8 - bb_width_atr))  # Más comprimido = más confianza
                logger.info(
                    f"📦 SQUEEZE BREAK LONG: price ${current_price:.0f} > BB upper ${bb_upper:.0f} | "
                    f"BB width: {bb_width_atr:.2f}x ATR (squeezed)"
                )
                return (True, 'LONG', confidence)
            
            # Breakout hacia abajo de la compresión contra LONG bias
            elif current_price < bb_lower and current_bias == 'LONG':
                confidence = min(0.75, 0.5 + (0.8 - bb_width_atr))
                logger.info(
                    f"📦 SQUEEZE BREAK SHORT: price ${current_price:.0f} < BB lower ${bb_lower:.0f} | "
                    f"BB width: {bb_width_atr:.2f}x ATR (squeezed)"
                )
                return (True, 'SHORT', confidence)
            
            return None
            
        except Exception as e:
            logger.debug(f"Squeeze break check failed: {e}")
            return None
    
    def _no_signal(self, reason: str) -> ReversalSignal:
        """Return empty signal."""
        return ReversalSignal(
            detected=False,
            new_direction=None,
            signal_type='NONE',
            confidence=0.0,
            reason=reason,
            body_atr=0.0,
            volume_mult=0.0
        )
    
    def get_override(self, current_tick: int) -> Optional[str]:
        """Get current direction override if active."""
        if self._override_direction and current_tick < self._override_expires_at_tick:
            return self._override_direction
        return None
    
    def clear_override(self) -> None:
        """Manually clear override."""
        self._override_direction = None
        self._override_expires_at_tick = 0


def check_immediate_flip(candles_1m: List[Dict[str, Any]],
                         current_bias: str,
                         atr_5m: float) -> Tuple[bool, Optional[str], str]:
    """
    Check if immediate direction flip is needed.
    
    This is a simpler check for when price has moved significantly
    against current bias in just a few candles.
    
    Args:
        candles_1m: Recent 1m candles
        current_bias: Current trading direction
        atr_5m: ATR on 5m timeframe
        
    Returns:
        (should_flip, new_direction, reason)
    """
    if not RiskManagement.IMMEDIATE_FLIP_ENABLED:
        return False, None, "Immediate flip disabled"
    
    if not candles_1m or len(candles_1m) < 5:
        return False, None, "Not enough candles"
    
    if atr_5m <= 0:
        return False, None, "Invalid ATR"
    
    # Get last N candles
    n_candles = RiskManagement.IMMEDIATE_FLIP_MAX_CANDLES
    recent = candles_1m[-(n_candles + 1):]
    
    # Calculate move over N candles
    start_price = float(recent[0].get('close', 0))
    end_price = float(recent[-1].get('close', 0))
    move = end_price - start_price
    move_atr = abs(move) / atr_5m if atr_5m > 0 else 0
    
    min_move = RiskManagement.IMMEDIATE_FLIP_MIN_MOVE_ATR
    min_volume = RiskManagement.IMMEDIATE_FLIP_VOLUME_MULT
    
    # Check volume
    volumes = [float(c.get('volume', 1)) for c in candles_1m[-10:-n_candles]]
    avg_volume = np.mean(volumes) if volumes else 1.0
    recent_volumes = [float(c.get('volume', 1)) for c in recent[1:]]  # Exclude first
    recent_avg = np.mean(recent_volumes) if recent_volumes else 1.0
    volume_mult = recent_avg / avg_volume if avg_volume > 0 else 1.0
    
    if move_atr >= min_move and volume_mult >= min_volume:
        if move > 0 and current_bias == 'SHORT':
            # Strong bullish move against SHORT bias
            return True, 'LONG', f"Immediate flip: +{move:.0f}$ ({move_atr:.2f}x ATR) in {n_candles} candles"
        elif move < 0 and current_bias == 'LONG':
            # Strong bearish move against LONG bias
            return True, 'SHORT', f"Immediate flip: {move:.0f}$ ({move_atr:.2f}x ATR) in {n_candles} candles"
    
    return False, None, "No immediate flip needed"


# Singleton instance
_detector: Optional[FastReversalDetector] = None

def get_fast_reversal_detector() -> FastReversalDetector:
    """Get singleton instance of FastReversalDetector."""
    global _detector
    if _detector is None:
        _detector = FastReversalDetector()
    return _detector
