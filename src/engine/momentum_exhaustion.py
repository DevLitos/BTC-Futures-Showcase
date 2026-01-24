"""
Momentum Exhaustion Detector - Identifies when to exit at the peak

PHILOSOPHY:
- Current system: Trailing stops that react to price drops
- This engine: Detects EXHAUSTION before price reverses

KEY SIGNALS (Exit when 3+ appear):
1. Volume Climax: Volume spike with price stalling = distribution
2. RSI Divergence: Price HH but RSI LH = weakening momentum
3. Candle Exhaustion: Long wicks, doji, engulfing = indecision
4. Time Decay: Move duration > 2x average = likely exhausted
5. ATR Expansion Peak: ATR spike then contraction = move ending

THIS IS NOT:
- Panic selling
- Premature exit
- Counter-trend bias

THIS IS:
- Detecting the END of a move
- Exiting at 90% of peak instead of 70%
- Capturing the "summum" as requested

Created: January 22, 2026
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ExhaustionLevel(str, Enum):
    """Exhaustion severity levels"""
    NONE = "NONE"
    EARLY = "EARLY"         # 1-2 signals - monitor closely
    MODERATE = "MODERATE"   # 3 signals - tighten stops
    SEVERE = "SEVERE"       # 4+ signals - exit now


@dataclass
class ExhaustionState:
    """Current exhaustion detection state"""
    level: ExhaustionLevel = ExhaustionLevel.NONE
    signal_count: int = 0
    signals_detected: List[str] = field(default_factory=list)
    confidence: float = 0.0
    recommended_action: str = "HOLD"  # HOLD, TIGHTEN, EXIT
    exit_urgency: float = 0.0  # 0-1, higher = more urgent
    
    def __post_init__(self):
        if self.signals_detected is None:
            self.signals_detected = []


class MomentumExhaustionDetector:
    """
    Detects momentum exhaustion for optimal exit timing.
    
    SIGNALS CHECKED:
    1. Volume Climax: Vol spike > 2x avg + price stalls
    2. RSI Divergence: Price makes new extreme but RSI doesn't
    3. Candle Patterns: Doji, pin bar, engulfing at extreme
    4. Time Extension: Move duration > 1.5x historical avg
    5. ATR Reversal: ATR was expanding, now contracting
    6. Velocity Decay: Price velocity slowing despite trend
    
    ACTIONS:
    - 1-2 signals: Monitor (EARLY)
    - 3 signals: Tighten trailing stop 50% (MODERATE)
    - 4+ signals: Market exit recommended (SEVERE)
    """
    
    # Thresholds
    VOLUME_CLIMAX_MULT = 2.0      # Volume > 2x average
    RSI_DIVERGENCE_BARS = 10      # Look back for divergence
    ATR_CONTRACTION_PCT = 0.8     # ATR < 80% of peak
    VELOCITY_DECAY_PCT = 0.5      # Velocity < 50% of peak
    
    def __init__(self):
        self._last_state = ExhaustionState()
        self._move_start_time: Optional[datetime] = None
        self._peak_atr: float = 0.0
        self._peak_velocity: float = 0.0
    
    def _detect_volume_climax(self, df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        """
        Detect volume climax (high volume with price stalling).
        
        Volume climax at tops: High vol + upper wicks + close near open
        Volume climax at bottoms: High vol + lower wicks + close near open
        """
        if len(df) < 20 or 'volume' not in df.columns:
            return False, "No volume data"
        
        recent = df.tail(5)
        avg_vol = df['volume'].tail(20).mean()
        
        for i in range(len(recent)):
            row = recent.iloc[i]
            vol_mult = row['volume'] / avg_vol if avg_vol > 0 else 0
            
            if vol_mult < self.VOLUME_CLIMAX_MULT:
                continue
            
            # Check for price stalling
            body = abs(row['close'] - row['open'])
            range_size = row['high'] - row['low']
            body_ratio = body / range_size if range_size > 0 else 1
            
            # Small body with high volume = climax
            if body_ratio < 0.3:
                if direction == 'LONG':
                    # Check for upper wick (distribution)
                    upper_wick = row['high'] - max(row['open'], row['close'])
                    if upper_wick > body * 1.5:
                        return True, f"Volume climax with distribution wick (vol {vol_mult:.1f}x)"
                else:
                    # Check for lower wick (accumulation)
                    lower_wick = min(row['open'], row['close']) - row['low']
                    if lower_wick > body * 1.5:
                        return True, f"Volume climax with accumulation wick (vol {vol_mult:.1f}x)"
        
        return False, "No volume climax"
    
    def _detect_rsi_divergence(self, df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        """
        Detect RSI divergence (price extreme vs RSI).
        
        Bearish divergence: Price HH, RSI LH (for LONG exit)
        Bullish divergence: Price LL, RSI HL (for SHORT exit)
        """
        if len(df) < self.RSI_DIVERGENCE_BARS + 14:
            return False, "Insufficient data"
        
        from src.engine.indicators import calculate_rsi
        
        closes = df['close'].to_numpy()
        rsi = calculate_rsi(closes, 14)
        
        if len(rsi) < self.RSI_DIVERGENCE_BARS:
            return False, "RSI calculation failed"
        
        # Find recent swing points
        lookback = self.RSI_DIVERGENCE_BARS
        
        if direction == 'LONG':
            # Look for bearish divergence: price HH but RSI LH
            price_highs = []
            rsi_highs = []
            
            for i in range(-lookback, -2):
                if closes[i] > closes[i-1] and closes[i] > closes[i+1]:
                    price_highs.append((i, closes[i]))
                    rsi_highs.append((i, rsi[i]))
            
            if len(price_highs) >= 2:
                # Check if most recent high is higher in price but lower in RSI
                if price_highs[-1][1] > price_highs[-2][1] and rsi_highs[-1][1] < rsi_highs[-2][1]:
                    return True, f"Bearish RSI divergence (price HH, RSI LH)"
        
        else:  # SHORT
            # Look for bullish divergence: price LL but RSI HL
            price_lows = []
            rsi_lows = []
            
            for i in range(-lookback, -2):
                if closes[i] < closes[i-1] and closes[i] < closes[i+1]:
                    price_lows.append((i, closes[i]))
                    rsi_lows.append((i, rsi[i]))
            
            if len(price_lows) >= 2:
                # Check if most recent low is lower in price but higher in RSI
                if price_lows[-1][1] < price_lows[-2][1] and rsi_lows[-1][1] > rsi_lows[-2][1]:
                    return True, f"Bullish RSI divergence (price LL, RSI HL)"
        
        return False, "No RSI divergence"
    
    def _detect_exhaustion_candles(self, df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        """
        Detect exhaustion candle patterns.
        
        Patterns:
        - Doji at extreme: Indecision after trend
        - Pin bar: Rejection wick > 2x body
        - Engulfing: Reversal candle
        """
        if len(df) < 5:
            return False, "Insufficient data"
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        body = abs(last['close'] - last['open'])
        range_size = last['high'] - last['low']
        
        if range_size <= 0:
            return False, "Invalid candle"
        
        body_ratio = body / range_size
        
        # 1. Doji (tiny body)
        if body_ratio < 0.1:
            return True, "Doji candle at extreme"
        
        # 2. Pin bar / Hammer / Shooting star
        if direction == 'LONG':
            upper_wick = last['high'] - max(last['open'], last['close'])
            if upper_wick > body * 2.5:
                return True, f"Shooting star (rejection wick)"
        else:
            lower_wick = min(last['open'], last['close']) - last['low']
            if lower_wick > body * 2.5:
                return True, f"Hammer (rejection wick)"
        
        # 3. Engulfing
        prev_body = abs(prev['close'] - prev['open'])
        if body > prev_body * 1.5:
            if direction == 'LONG':
                # Bearish engulfing
                if last['close'] < last['open'] and prev['close'] > prev['open']:
                    if last['open'] > prev['close'] and last['close'] < prev['open']:
                        return True, "Bearish engulfing"
            else:
                # Bullish engulfing
                if last['close'] > last['open'] and prev['close'] < prev['open']:
                    if last['open'] < prev['close'] and last['close'] > prev['open']:
                        return True, "Bullish engulfing"
        
        return False, "No exhaustion pattern"
    
    def _detect_atr_reversal(self, df: pd.DataFrame) -> Tuple[bool, float, str]:
        """
        Detect ATR expansion followed by contraction.
        """
        if len(df) < 30:
            return False, 0.0, "Insufficient data"
        
        from src.engine.indicators import calculate_atr
        
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        closes = df['close'].to_numpy()
        
        atr = calculate_atr(highs, lows, closes, 14)
        if len(atr) < 10:
            return False, 0.0, "ATR calculation failed"
        
        # Find peak ATR in last 20 bars
        recent_atr = atr[-20:]
        # 🔥 FIX (Jan 22, 2026): Filter NaN values before finding peak
        valid_atr = recent_atr[~np.isnan(recent_atr)]
        if len(valid_atr) == 0:
            return False, 0.0, "No valid ATR data"
        
        peak_atr = np.max(valid_atr)
        current_atr = atr[-1]
        
        # 🔥 FIX (Jan 22, 2026): Guard against NaN current ATR
        if np.isnan(current_atr):
            return False, 0.0, "Current ATR is NaN"
        
        # Update peak tracking
        if peak_atr > self._peak_atr:
            self._peak_atr = peak_atr
        
        if self._peak_atr <= 0:
            return False, 0.0, "No peak ATR recorded"
        
        contraction_ratio = current_atr / self._peak_atr
        
        if contraction_ratio < self.ATR_CONTRACTION_PCT:
            return True, contraction_ratio, f"ATR contracting ({contraction_ratio:.0%} of peak)"
        
        return False, contraction_ratio, "ATR still expanding"
    
    def _detect_velocity_decay(self, df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        """
        Detect price velocity decay (slowing momentum).
        """
        if len(df) < 20:
            return False, "Insufficient data"
        
        closes = df['close'].to_numpy()
        
        # Calculate rolling velocity (price change per bar)
        velocities = np.diff(closes)
        
        # Use signed velocity based on direction
        if direction == 'LONG':
            signed_vel = velocities
        else:
            signed_vel = -velocities
        
        # Peak velocity in move
        if len(signed_vel) < 10:
            return False, "Insufficient velocity data"
        
        recent_vel = signed_vel[-10:]
        peak_vel = np.max(recent_vel)
        current_vel = signed_vel[-1]
        
        if peak_vel > self._peak_velocity:
            self._peak_velocity = peak_vel
        
        # 🔥 FIX (Jan 22, 2026): Safe division - guard against zero/negative peak velocity
        if self._peak_velocity <= 0:
            return False, "No positive velocity recorded"
        
        decay_ratio = current_vel / self._peak_velocity
        
        if decay_ratio < self.VELOCITY_DECAY_PCT:
            return True, f"Velocity decayed to {decay_ratio:.0%} of peak"
        
        return False, "Velocity still strong"
    
    def _detect_time_extension(self, move_start: datetime, market_time: datetime) -> Tuple[bool, str]:
        """
        Check if move has extended beyond typical duration.
        
        Average BTC intraday move: ~30-60 minutes
        Extended: > 90 minutes
        """
        if move_start is None:
            return False, "No move start recorded"
        
        duration_mins = (market_time - move_start).total_seconds() / 60
        
        # Typical move durations
        AVG_MOVE_MINS = 45
        EXTENDED_MULT = 2.0
        
        if duration_mins > AVG_MOVE_MINS * EXTENDED_MULT:
            return True, f"Move extended ({duration_mins:.0f}min > {AVG_MOVE_MINS * EXTENDED_MULT:.0f}min typical)"
        
        return False, f"Move duration normal ({duration_mins:.0f}min)"
    
    def start_tracking(self, market_time: datetime):
        """Call when entering a new position to start tracking move."""
        self._move_start_time = market_time
        self._peak_atr = 0.0
        self._peak_velocity = 0.0
        logger.debug(f"📊 Exhaustion tracker started at {market_time}")
    
    def evaluate(self,
                df_1m: pd.DataFrame,
                df_5m: pd.DataFrame,
                direction: str,
                market_time: datetime,
                mfe_r: float = 0.0) -> ExhaustionState:
        """
        Evaluate momentum exhaustion.
        
        Args:
            df_1m: 1-minute OHLC data
            df_5m: 5-minute OHLC data
            direction: Position direction (LONG/SHORT)
            market_time: Current market time
            mfe_r: Current MFE in R terms
        
        Returns:
            ExhaustionState with recommendations
        """
        signals = []
        
        # Only check exhaustion after meaningful profit (> 0.5R)
        if mfe_r < 0.5:
            self._last_state = ExhaustionState(
                level=ExhaustionLevel.NONE,
                recommended_action="HOLD",
                signals_detected=["MFE too low for exhaustion check"]
            )
            return self._last_state
        
        # 1. Volume climax (use 1m for precision)
        vol_climax, vol_reason = self._detect_volume_climax(df_1m, direction)
        if vol_climax:
            signals.append(f"📊 {vol_reason}")
        
        # 2. RSI divergence (use 5m for signal quality)
        rsi_div, rsi_reason = self._detect_rsi_divergence(df_5m, direction)
        if rsi_div:
            signals.append(f"📉 {rsi_reason}")
        
        # 3. Exhaustion candles (use 1m for timing)
        candle_exh, candle_reason = self._detect_exhaustion_candles(df_1m, direction)
        if candle_exh:
            signals.append(f"🕯️ {candle_reason}")
        
        # 4. ATR reversal (use 5m for signal stability)
        atr_rev, atr_ratio, atr_reason = self._detect_atr_reversal(df_5m)
        if atr_rev:
            signals.append(f"📈 {atr_reason}")
        
        # 5. Velocity decay (use 1m for precision)
        vel_decay, vel_reason = self._detect_velocity_decay(df_1m, direction)
        if vel_decay:
            signals.append(f"🚀 {vel_reason}")
        
        # 6. Time extension
        if self._move_start_time is not None:
            time_ext, time_reason = self._detect_time_extension(self._move_start_time, market_time)
            if time_ext:
                signals.append(f"⏱️ {time_reason}")
        
        # Determine exhaustion level
        signal_count = len(signals)
        
        if signal_count == 0:
            level = ExhaustionLevel.NONE
            action = "HOLD"
            urgency = 0.0
        elif signal_count <= 2:
            level = ExhaustionLevel.EARLY
            action = "HOLD"  # Monitor but don't act
            urgency = 0.2
        elif signal_count == 3:
            level = ExhaustionLevel.MODERATE
            action = "TIGHTEN"  # Tighten trailing stop 50%
            urgency = 0.6
        else:
            level = ExhaustionLevel.SEVERE
            action = "EXIT"  # Market exit recommended
            urgency = 0.9
        
        # Boost urgency if MFE is high (more to protect)
        if mfe_r >= 2.0:
            urgency = min(1.0, urgency * 1.3)
        
        confidence = min(1.0, signal_count / 4.0)
        
        self._last_state = ExhaustionState(
            level=level,
            signal_count=signal_count,
            signals_detected=signals,
            confidence=confidence,
            recommended_action=action,
            exit_urgency=urgency
        )
        
        if signal_count > 0:
            logger.info(f"⚡ EXHAUSTION [{level.value}]: {signal_count} signals | Action: {action}")
            for sig in signals:
                logger.info(f"   {sig}")
        
        return self._last_state
    
    def get_state(self) -> ExhaustionState:
        """Get current exhaustion state"""
        return self._last_state
    
    def reset(self):
        """Reset tracker for new position"""
        self._move_start_time = None
        self._peak_atr = 0.0
        self._peak_velocity = 0.0
        self._last_state = ExhaustionState()


# Singleton
_exhaustion_detector: Optional[MomentumExhaustionDetector] = None

def get_exhaustion_detector() -> MomentumExhaustionDetector:
    global _exhaustion_detector
    if _exhaustion_detector is None:
        _exhaustion_detector = MomentumExhaustionDetector()
    return _exhaustion_detector
