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
from datetime import datetime, timedelta
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
    BB_SQUEEZE_PERCENTILE = 20  # Width below this = squeezed
    ADX_LOW_THRESHOLD = 20
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
        
        from src.engine.indicators import calculate_adx
        
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
            reasons=reasons
        )
        
        if signal_type != AnticipationSignal.NONE:
            logger.info(f"🔮 ANTICIPATION: {signal_type.value} {direction} | conf={confidence:.0%}")
            for reason in reasons:
                logger.info(f"   → {reason}")
        
        return self._last_state
    
    def get_state(self) -> AnticipationState:
        """Get current anticipation state"""
        return self._last_state


# Singleton
_anticipation_engine: Optional[AnticipationEngine] = None

def get_anticipation_engine() -> AnticipationEngine:
    global _anticipation_engine
    if _anticipation_engine is None:
        _anticipation_engine = AnticipationEngine()
    return _anticipation_engine
