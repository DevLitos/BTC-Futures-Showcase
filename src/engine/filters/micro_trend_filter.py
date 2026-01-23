"""
Micro Trend Filter v2 - Prevents entries against micro-structure

IMPROVEMENTS (Jan 15, 2026):
1. RSI Override requires REVERSAL CONFIRMATION (not just RSI < 30)
2. LL/LH detection using SWING PIVOTS (not consecutive candles)
3. WEIGHTED signals with proper importance hierarchy
4. HYSTERESIS: State must persist N bars before HARD BLOCK
5. 1m TIMING confirmation combined with 5m REGIME
6. Smart cooldown reset when conditions improve

ARCHITECTURE:
- 5m: REGIME detection (bullish/bearish/neutral)
- 1m: TIMING confirmation (reversal signals, momentum shift)
- Combined: Only block when BOTH timeframes agree on opposition
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Tuple, Optional, List, Dict
from datetime import datetime, timedelta
from collections import deque
import logging

from src.engine.indicators import calculate_ema, calculate_rsi, calculate_atr

logger = logging.getLogger(__name__)


# =============================================================================
# SWING PIVOT DETECTION - More robust than consecutive candles
# =============================================================================

def detect_swing_pivots(highs: np.ndarray, lows: np.ndarray, 
                        left_bars: int = 3, right_bars: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """
    Detect swing highs and lows using fractal/pivot logic.
    
    A swing high is a bar where the high is higher than 'left_bars' bars to the left
    AND higher than 'right_bars' bars to the right.
    
    Returns:
        (swing_highs, swing_lows) - Lists of (index, price) tuples
    """
    swing_highs = []
    swing_lows = []
    
    n = len(highs)
    if n < left_bars + right_bars + 1:
        return swing_highs, swing_lows
    
    for i in range(left_bars, n - right_bars):
        # Check swing high
        is_swing_high = True
        for j in range(1, left_bars + 1):
            if highs[i] <= highs[i - j]:
                is_swing_high = False
                break
        if is_swing_high:
            for j in range(1, right_bars + 1):
                if highs[i] <= highs[i + j]:
                    is_swing_high = False
                    break
        if is_swing_high:
            swing_highs.append((i, highs[i]))
        
        # Check swing low
        is_swing_low = True
        for j in range(1, left_bars + 1):
            if lows[i] >= lows[i - j]:
                is_swing_low = False
                break
        if is_swing_low:
            for j in range(1, right_bars + 1):
                if lows[i] >= lows[i + j]:
                    is_swing_low = False
                    break
        if is_swing_low:
            swing_lows.append((i, lows[i]))
    
    return swing_highs, swing_lows


def check_swing_structure(swing_points: List[Tuple[int, float]], 
                          direction: str = 'lower') -> Tuple[bool, int]:
    """
    Check if last 2-3 swing points form a structure.
    
    Args:
        swing_points: List of (index, price) tuples
        direction: 'lower' for LL/LH, 'higher' for HL/HH
    
    Returns:
        (has_structure, count) - Whether structure exists and how many points confirm
    """
    if len(swing_points) < 2:
        return False, 0
    
    # Get last 3 swings
    recent = swing_points[-3:] if len(swing_points) >= 3 else swing_points[-2:]
    
    count = 0
    for i in range(1, len(recent)):
        if direction == 'lower':
            if recent[i][1] < recent[i-1][1]:
                count += 1
        else:  # higher
            if recent[i][1] > recent[i-1][1]:
                count += 1
    
    # Need at least 1 confirmation for 2 swings, 2 for 3 swings
    threshold = len(recent) - 1
    return count >= threshold, count


# =============================================================================
# RSI REVERSAL DETECTION - Smart override conditions
# =============================================================================

def detect_rsi_bullish_reversal(rsi_values: np.ndarray, 
                                 closes: np.ndarray,
                                 lows: np.ndarray,
                                 threshold: float = 30) -> Tuple[bool, str]:
    """
    Detect bullish RSI reversal patterns (not just oversold).
    
    Conditions (any of):
    1. RSI crosses above threshold from below
    2. RSI makes higher low while price makes lower low (bullish divergence)
    3. RSI slope turns positive after being oversold
    
    Returns:
        (has_reversal, reason)
    """
    if len(rsi_values) < 10:
        return False, "Insufficient RSI data"
    
    current_rsi = rsi_values[-1]
    
    # Not oversold - no reversal check needed
    if current_rsi > threshold + 10:
        return False, "RSI not in reversal zone"
    
    # Condition 1: RSI cross above threshold
    if len(rsi_values) >= 3:
        if rsi_values[-3] < threshold and rsi_values[-2] < threshold and current_rsi > threshold:
            return True, f"RSI crossed above {threshold}"
    
    # Condition 2: Bullish divergence (price LL but RSI HL)
    # Check last 10 bars for divergence
    lookback = min(10, len(rsi_values) - 1)
    if lookback >= 5:
        price_lows_idx = []
        rsi_at_lows = []
        
        for i in range(-lookback, 0):
            # Simple local minimum detection
            if i > -lookback and i < -1:
                if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                    price_lows_idx.append(i)
                    rsi_at_lows.append(rsi_values[i])
        
        if len(price_lows_idx) >= 2:
            # Check if price made LL but RSI made HL
            if lows[price_lows_idx[-1]] < lows[price_lows_idx[-2]]:
                if rsi_at_lows[-1] > rsi_at_lows[-2]:
                    return True, "Bullish divergence: price LL, RSI HL"
    
    # Condition 3: RSI slope turning positive after oversold
    if current_rsi < threshold + 5:  # Still near oversold
        if len(rsi_values) >= 5:
            recent_rsi = rsi_values[-5:]
            slope = (recent_rsi[-1] - recent_rsi[0]) / 4
            if slope > 0.5:  # RSI rising more than 0.5 per bar
                if min(recent_rsi[:-2]) < threshold:  # Was oversold recently
                    return True, f"RSI slope positive ({slope:.1f}/bar) after oversold"
    
    return False, "No reversal pattern"


def detect_rsi_bearish_reversal(rsi_values: np.ndarray, 
                                 closes: np.ndarray,
                                 highs: np.ndarray,
                                 threshold: float = 70) -> Tuple[bool, str]:
    """
    Detect bearish RSI reversal patterns.
    Mirror of bullish detection.
    """
    if len(rsi_values) < 10:
        return False, "Insufficient RSI data"
    
    current_rsi = rsi_values[-1]
    
    if current_rsi < threshold - 10:
        return False, "RSI not in reversal zone"
    
    # Condition 1: RSI cross below threshold
    if len(rsi_values) >= 3:
        if rsi_values[-3] > threshold and rsi_values[-2] > threshold and current_rsi < threshold:
            return True, f"RSI crossed below {threshold}"
    
    # Condition 2: Bearish divergence (price HH but RSI LH)
    lookback = min(10, len(rsi_values) - 1)
    if lookback >= 5:
        price_highs_idx = []
        rsi_at_highs = []
        
        for i in range(-lookback, 0):
            if i > -lookback and i < -1:
                if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                    price_highs_idx.append(i)
                    rsi_at_highs.append(rsi_values[i])
        
        if len(price_highs_idx) >= 2:
            if highs[price_highs_idx[-1]] > highs[price_highs_idx[-2]]:
                if rsi_at_highs[-1] < rsi_at_highs[-2]:
                    return True, "Bearish divergence: price HH, RSI LH"
    
    # Condition 3: RSI slope turning negative after overbought
    if current_rsi > threshold - 5:
        if len(rsi_values) >= 5:
            recent_rsi = rsi_values[-5:]
            slope = (recent_rsi[-1] - recent_rsi[0]) / 4
            if slope < -0.5:
                if max(recent_rsi[:-2]) > threshold:
                    return True, f"RSI slope negative ({slope:.1f}/bar) after overbought"
    
    return False, "No reversal pattern"


# =============================================================================
# MICRO TREND STATE - Enhanced with hysteresis
# =============================================================================

@dataclass
class MicroTrendState:
    """Estado de la micro-tendencia con información extendida"""
    # EMAs 5m
    ema20_5m: float = 0.0
    ema50_5m: float = 0.0
    ema20_slope: float = 0.0  # bps/bar (normalized)
    ema50_slope: float = 0.0
    
    # Structure (swing-based)
    has_swing_ll: bool = False        # Swing lows making LL
    has_swing_lh: bool = False        # Swing highs making LH
    has_swing_hl: bool = False        # Swing lows making HL
    has_swing_hh: bool = False        # Swing highs making HH
    swing_structure_strength: int = 0  # How many swings confirm
    
    # EMA structure
    is_ema_bearish: bool = False      # EMA20 < EMA50
    is_below_ema20: bool = False      # Price < EMA20
    ema_gap_pct: float = 0.0          # Gap between EMA20 and EMA50 as %
    
    # Momentum
    rsi_5m: float = 50.0
    rsi_slope: float = 0.0            # RSI change per bar
    rsi_has_bullish_reversal: bool = False
    rsi_has_bearish_reversal: bool = False
    rsi_reversal_reason: str = ""
    
    # 1m confirmation (optional)
    has_1m_bullish_confirmation: bool = False
    has_1m_bearish_confirmation: bool = False
    
    # Regime determination
    micro_trend: str = "NEUTRAL"      # "BULLISH", "BEARISH", "NEUTRAL"
    raw_strength: float = 0.0         # Before hysteresis
    confirmed_strength: float = 0.0   # After hysteresis
    bars_in_state: int = 0            # How many bars in current state
    
    # Score adjustment
    score_adjustment: float = 0.0
    
    # Backwards compatibility
    has_lower_low: bool = False
    has_lower_high: bool = False
    rsi_falling: bool = False
    strength: float = 0.0
    
    # Debug
    signal_breakdown: Dict[str, float] = field(default_factory=dict)
    
    def __post_init__(self):
        # Sync backwards compat fields
        self.has_lower_low = self.has_swing_ll
        self.has_lower_high = self.has_swing_lh
        self.strength = self.confirmed_strength
        self.rsi_falling = self.rsi_slope < 0
    
    def to_dict(self) -> dict:
        return {
            'ema20_5m': round(self.ema20_5m, 2),
            'ema50_5m': round(self.ema50_5m, 2),
            'ema20_slope': round(self.ema20_slope, 4),
            'ema_gap_pct': round(self.ema_gap_pct, 3),
            'is_ema_bearish': self.is_ema_bearish,
            'has_swing_ll': self.has_swing_ll,
            'has_swing_lh': self.has_swing_lh,
            'swing_structure_strength': self.swing_structure_strength,
            'rsi_5m': round(self.rsi_5m, 1),
            'rsi_slope': round(self.rsi_slope, 2),
            'rsi_has_bullish_reversal': self.rsi_has_bullish_reversal,
            'micro_trend': self.micro_trend,
            'raw_strength': round(self.raw_strength, 2),
            'confirmed_strength': round(self.confirmed_strength, 2),
            'bars_in_state': self.bars_in_state,
            'score_adjustment': round(self.score_adjustment, 1),
            'signal_breakdown': {
                k: (round(v, 2) if isinstance(v, (int, float)) else v) 
                for k, v in self.signal_breakdown.items()
            }
        }


# =============================================================================
# MAIN FILTER CLASS
# =============================================================================

class MicroTrendFilter:
    """
    Filtro de micro-tendencia v2 con:
    - Detección de swings en lugar de velas consecutivas
    - RSI override solo con confirmación de reversión
    - Histéresis para evitar whipsaws
    - Ponderación inteligente de señales
    - Combinación 5m (régimen) + 1m (timing)
    """
    
    # =========================================================================
    # SIGNAL WEIGHTS - Importancia de cada señal (total normalizado a 1.0)
    # =========================================================================
    WEIGHTS = {
        # Estructura EMA (más confiable, peso alto)
        'ema_cross': 3.0,           # EMA20 vs EMA50 - MUY importante
        'ema_slope_alignment': 2.5,  # Ambas EMAs con pendiente en misma dirección
        
        # Estructura de swings (muy informativo)
        'swing_lows': 2.0,          # LL vs HL
        'swing_highs': 1.5,         # LH vs HH
        
        # Posición precio (puede ser ruido en pullbacks)
        'price_vs_ema20': 1.0,      # Peso reducido - pullbacks normales
        'price_vs_ema50': 1.5,      # Más importante que EMA20
        
        # Momentum RSI
        'rsi_zone': 1.0,            # RSI en zona bull/bear
        'rsi_slope': 1.5,           # Dirección del RSI
        
        # 1m confirmation (bonus)
        'ltf_confirmation': 2.0,    # 1m confirma dirección
    }
    
    # =========================================================================
    # THRESHOLDS
    # =========================================================================
    EMA_FAST = 20
    EMA_SLOW = 50
    EMA_SLOPE_LOOKBACK = 5
    
    RSI_PERIOD = 14
    RSI_BEARISH_THRESHOLD = 45
    RSI_BULLISH_THRESHOLD = 55
    RSI_EXTREME_LOW = 30
    RSI_EXTREME_HIGH = 70
    
    # Swing detection params
    SWING_LEFT_BARS = 3
    SWING_RIGHT_BARS = 2
    
    # Hysteresis: bars required to confirm state change
    HYSTERESIS_BARS_TO_BLOCK = 2   # Bars in BEARISH before HARD BLOCK
    HYSTERESIS_BARS_TO_CLEAR = 2   # Bars in BULLISH before clearing block
    
    # Score adjustments
    SCORE_HARD_PENALTY = -3.0
    SCORE_SOFT_PENALTY = -2.0
    SCORE_WARNING_PENALTY = -1.0
    SCORE_ALIGNMENT_BONUS = 1.0
    
    # Strength thresholds (applied AFTER hysteresis)
    # Reduced (Jan 15, 2026) to catch trends faster with confluence boost
    # 🔥 FIX (Jan 22, 2026): Strength threshold for hard block raised
    # RANGE mode: only hard block if strength > 60% AND ADX < 14
    STRENGTH_HARD_BLOCK = 0.55     # Reduced from 0.65 (confluence boost adds confidence)
    STRENGTH_HARD_BLOCK_RANGE = 0.60  # 🔥 NEW: Higher threshold for RANGE mode
    STRENGTH_SOFT_BLOCK = 0.40     # Reduced from 0.45
    STRENGTH_WARNING = 0.25
    
    # Minimum score for RSI override to work
    RSI_OVERRIDE_MIN_BASE_SCORE = 5  # Only override if base score >= 5
    
    # 🔥 NEW: ADX threshold for conditional hard block in RANGE
    ADX_LOW_FOR_RANGE_BLOCK = 14.0  # In RANGE, only hard block if ADX < this
    
    def __init__(self, 
                 hard_block_enabled: bool = True,
                 min_data_5m: int = 60,
                 min_data_1m: int = 20):
        self.hard_block_enabled = hard_block_enabled
        self.min_data_5m = min_data_5m
        self.min_data_1m = min_data_1m
        
        # State tracking for hysteresis
        self._state_history: deque = deque(maxlen=10)  # Last 10 states
        self._current_trend: str = "NEUTRAL"
        self._bars_in_trend: int = 0
        self._last_check_ts: int = 0
        
        # For smart cooldown reset
        self._consecutive_bullish_bars: int = 0
        self._consecutive_bearish_bars: int = 0
        
        # Cache
        self._last_state: Optional[MicroTrendState] = None
    
    def check(self, 
              df_5m: pd.DataFrame, 
              df_1m: Optional[pd.DataFrame],
              current_price: float, 
              htf_bias: str,
              market_time: Optional[datetime] = None,
              base_score: int = 0,
              regime_mode: str = None,  # 🔥 NEW: 'TREND', 'RANGE', 'BREAKOUT'
              adx_value: float = None    # 🔥 NEW: Current ADX for conditional block
              ) -> Tuple[bool, str, MicroTrendState]:
        """
        Check micro-trend filter with all improvements.
        
        🔥 FIX (Jan 22, 2026): Added conditional hard block for RANGE mode.
        In RANGE + high strength + low ADX → hard block
        In TREND or high ADX → only penalty
        
        Args:
            df_5m: 5m candles (regime detection)
            df_1m: 1m candles (timing confirmation, optional)
            current_price: Current price
            htf_bias: 'LONG' or 'SHORT'
            market_time: Current time (for caching)
            base_score: Current entry score (for conditional RSI override)
            regime_mode: 'TREND', 'RANGE', 'BREAKOUT' (for conditional block)
            adx_value: Current ADX value (for conditional block)
        
        Returns:
            (allowed, reason, state)
        """
        state = MicroTrendState()
        
        # Validate data
        if df_5m is None or len(df_5m) < self.min_data_5m:
            logger.debug(f"MicroTrend: Insufficient 5m data ({len(df_5m) if df_5m is not None else 0})")
            return True, "Insufficient data", state
        
        # Extract arrays
        closes_5m = df_5m['close'].values
        highs_5m = df_5m['high'].values
        lows_5m = df_5m['low'].values
        
        # =================================================================
        # 1. CALCULATE INDICATORS
        # =================================================================
        
        # EMAs
        ema20 = calculate_ema(closes_5m, self.EMA_FAST)
        ema50 = calculate_ema(closes_5m, self.EMA_SLOW)
        
        if np.isnan(ema20[-1]) or np.isnan(ema50[-1]):
            return True, "EMA calculation failed", state
        
        state.ema20_5m = float(ema20[-1])
        state.ema50_5m = float(ema50[-1])
        
        # EMA slopes (normalized by price)
        if len(ema20) >= self.EMA_SLOPE_LOOKBACK and current_price > 0:
            state.ema20_slope = ((ema20[-1] - ema20[-self.EMA_SLOPE_LOOKBACK]) / 
                                  current_price * 10000 / self.EMA_SLOPE_LOOKBACK)
            state.ema50_slope = ((ema50[-1] - ema50[-self.EMA_SLOPE_LOOKBACK]) / 
                                  current_price * 10000 / self.EMA_SLOPE_LOOKBACK)
        
        # EMA gap
        state.ema_gap_pct = (state.ema20_5m - state.ema50_5m) / state.ema50_5m * 100
        state.is_ema_bearish = state.ema20_5m < state.ema50_5m
        
        # Price vs EMAs
        buffer = current_price * 0.001
        state.is_below_ema20 = current_price < (state.ema20_5m - buffer)
        
        # RSI
        rsi = calculate_rsi(closes_5m, self.RSI_PERIOD)
        if not np.isnan(rsi[-1]):
            state.rsi_5m = float(rsi[-1])
            if len(rsi) >= 5:
                state.rsi_slope = (rsi[-1] - rsi[-5]) / 4
        
        # =================================================================
        # 2. SWING STRUCTURE (more robust than consecutive candles)
        # =================================================================
        
        swing_highs, swing_lows = detect_swing_pivots(
            highs_5m, lows_5m, 
            self.SWING_LEFT_BARS, self.SWING_RIGHT_BARS
        )
        
        # Check swing structure
        state.has_swing_ll, ll_count = check_swing_structure(swing_lows, 'lower')
        state.has_swing_lh, lh_count = check_swing_structure(swing_highs, 'lower')
        state.has_swing_hl, hl_count = check_swing_structure(swing_lows, 'higher')
        state.has_swing_hh, hh_count = check_swing_structure(swing_highs, 'higher')
        
        # Sync backwards compat
        state.has_lower_low = state.has_swing_ll
        state.has_lower_high = state.has_swing_lh
        
        # Structure strength
        if state.has_swing_ll or state.has_swing_lh:
            state.swing_structure_strength = ll_count + lh_count
        elif state.has_swing_hl or state.has_swing_hh:
            state.swing_structure_strength = hl_count + hh_count
        
        # =================================================================
        # 3. RSI REVERSAL DETECTION (conditional override)
        # =================================================================
        
        state.rsi_has_bullish_reversal, state.rsi_reversal_reason = \
            detect_rsi_bullish_reversal(rsi, closes_5m, lows_5m, self.RSI_EXTREME_LOW)
        
        state.rsi_has_bearish_reversal, _ = \
            detect_rsi_bearish_reversal(rsi, closes_5m, highs_5m, self.RSI_EXTREME_HIGH)
        
        # =================================================================
        # 4. 1m CONFIRMATION (if available)
        # =================================================================
        
        if df_1m is not None and len(df_1m) >= self.min_data_1m:
            closes_1m = df_1m['close'].values
            ema9_1m = calculate_ema(closes_1m, 9)
            ema20_1m = calculate_ema(closes_1m, 20)
            
            if not (np.isnan(ema9_1m[-1]) or np.isnan(ema20_1m[-1])):
                # 1m bullish: EMA9 > EMA20 and price > EMA9
                if ema9_1m[-1] > ema20_1m[-1] and current_price > ema9_1m[-1]:
                    state.has_1m_bullish_confirmation = True
                # 1m bearish: EMA9 < EMA20 and price < EMA9
                elif ema9_1m[-1] < ema20_1m[-1] and current_price < ema9_1m[-1]:
                    state.has_1m_bearish_confirmation = True
        
        # =================================================================
        # 5. CALCULATE WEIGHTED STRENGTH
        # =================================================================
        
        bearish_score = 0.0
        bullish_score = 0.0
        total_weight = sum(self.WEIGHTS.values())
        
        # EMA Cross (highest weight)
        w = self.WEIGHTS['ema_cross']
        if state.is_ema_bearish:
            bearish_score += w
            state.signal_breakdown['ema_cross'] = -w
        else:
            bullish_score += w
            state.signal_breakdown['ema_cross'] = w
        
        # EMA Slope Alignment
        w = self.WEIGHTS['ema_slope_alignment']
        if state.ema20_slope < -1 and state.ema50_slope < 0:
            bearish_score += w
            state.signal_breakdown['ema_slope'] = -w
        elif state.ema20_slope > 1 and state.ema50_slope > 0:
            bullish_score += w
            state.signal_breakdown['ema_slope'] = w
        
        # Swing Lows
        w = self.WEIGHTS['swing_lows']
        if state.has_swing_ll:
            bearish_score += w
            state.signal_breakdown['swing_lows'] = -w
        elif state.has_swing_hl:
            bullish_score += w
            state.signal_breakdown['swing_lows'] = w
        
        # Swing Highs
        w = self.WEIGHTS['swing_highs']
        if state.has_swing_lh:
            bearish_score += w
            state.signal_breakdown['swing_highs'] = -w
        elif state.has_swing_hh:
            bullish_score += w
            state.signal_breakdown['swing_highs'] = w
        
        # Price vs EMA20 (lower weight - can be normal pullback)
        w = self.WEIGHTS['price_vs_ema20']
        if state.is_below_ema20:
            bearish_score += w * 0.5  # Half weight only
            state.signal_breakdown['price_vs_ema20'] = -w * 0.5
        elif current_price > state.ema20_5m + buffer:
            bullish_score += w * 0.5
            state.signal_breakdown['price_vs_ema20'] = w * 0.5
        
        # Price vs EMA50 (more important)
        w = self.WEIGHTS['price_vs_ema50']
        if current_price < state.ema50_5m:
            bearish_score += w
            state.signal_breakdown['price_vs_ema50'] = -w
        elif current_price > state.ema50_5m:
            bullish_score += w
            state.signal_breakdown['price_vs_ema50'] = w
        
        # RSI Zone
        w = self.WEIGHTS['rsi_zone']
        if state.rsi_5m < self.RSI_BEARISH_THRESHOLD:
            bearish_score += w
            state.signal_breakdown['rsi_zone'] = -w
        elif state.rsi_5m > self.RSI_BULLISH_THRESHOLD:
            bullish_score += w
            state.signal_breakdown['rsi_zone'] = w
        
        # RSI Slope
        w = self.WEIGHTS['rsi_slope']
        if state.rsi_slope < -1:
            bearish_score += w
            state.signal_breakdown['rsi_slope'] = -w
        elif state.rsi_slope > 1:
            bullish_score += w
            state.signal_breakdown['rsi_slope'] = w
        
        # 1m Confirmation (bonus when available)
        w = self.WEIGHTS['ltf_confirmation']
        if state.has_1m_bearish_confirmation:
            bearish_score += w
            state.signal_breakdown['ltf_confirm'] = -w
        elif state.has_1m_bullish_confirmation:
            bullish_score += w
            state.signal_breakdown['ltf_confirm'] = w
        
        # Normalize
        bearish_strength = bearish_score / total_weight
        bullish_strength = bullish_score / total_weight
        
        # =================================================================
        # 5.5 CONFLUENCE BOOST (Jan 15, 2026)
        # When multiple HIGH-WEIGHT signals align, apply a multiplier to strength.
        # This catches clear trends faster without waiting for hysteresis.
        # 
        # BEARISH confluence: EMA cross + swing LL + (1m confirm OR negative slope)
        # BULLISH confluence: EMA cross + swing HL + (1m confirm OR positive slope)
        # =================================================================
        
        confluence_count_bearish = 0
        confluence_count_bullish = 0
        
        # Count strong bearish signals
        if state.is_ema_bearish:  # EMA20 < EMA50
            confluence_count_bearish += 1
        if state.has_swing_ll:
            confluence_count_bearish += 1
        if state.ema20_slope < -1.5:
            confluence_count_bearish += 1
        if state.has_1m_bearish_confirmation:
            confluence_count_bearish += 1
        if state.rsi_5m < 40:  # RSI in bearish zone
            confluence_count_bearish += 1
        
        # Count strong bullish signals  
        if not state.is_ema_bearish:  # EMA20 > EMA50
            confluence_count_bullish += 1
        if state.has_swing_hl:
            confluence_count_bullish += 1
        if state.ema20_slope > 1.5:
            confluence_count_bullish += 1
        if state.has_1m_bullish_confirmation:
            confluence_count_bullish += 1
        if state.rsi_5m > 60:  # RSI in bullish zone
            confluence_count_bullish += 1
        
        # Apply confluence boost (multiplicative)
        # 3+ signals = 1.25x, 4+ signals = 1.50x
        if confluence_count_bearish >= 4:
            bearish_strength = min(1.0, bearish_strength * 1.50)
            state.signal_breakdown['confluence_boost'] = 'bearish_4+'
        elif confluence_count_bearish >= 3:
            bearish_strength = min(1.0, bearish_strength * 1.25)
            state.signal_breakdown['confluence_boost'] = 'bearish_3'
        
        if confluence_count_bullish >= 4:
            bullish_strength = min(1.0, bullish_strength * 1.50)
            state.signal_breakdown['confluence_boost'] = 'bullish_4+'
        elif confluence_count_bullish >= 3:
            bullish_strength = min(1.0, bullish_strength * 1.25)
            state.signal_breakdown['confluence_boost'] = 'bullish_3'
        
        # Store confluence counts for debugging
        state.signal_breakdown['confluence_bearish'] = confluence_count_bearish
        state.signal_breakdown['confluence_bullish'] = confluence_count_bullish
        
        # Determine raw trend
        if bearish_strength > bullish_strength + 0.08:
            raw_trend = "BEARISH"
            state.raw_strength = bearish_strength
        elif bullish_strength > bearish_strength + 0.08:
            raw_trend = "BULLISH"
            state.raw_strength = bullish_strength
        else:
            raw_trend = "NEUTRAL"
            state.raw_strength = max(bearish_strength, bullish_strength)
        
        # =================================================================
        # 6. HYSTERESIS - Require persistence before changing state
        # =================================================================
        
        if raw_trend == self._current_trend:
            self._bars_in_trend += 1
        else:
            # Trend changed - check if we should switch
            if raw_trend == "NEUTRAL":
                # Easy to go neutral
                self._current_trend = raw_trend
                self._bars_in_trend = 1
            else:
                # Need confirmation to switch to BULLISH/BEARISH
                self._bars_in_trend = 1
                self._current_trend = raw_trend
        
        state.micro_trend = self._current_trend
        state.bars_in_state = self._bars_in_trend
        
        # Confirmed strength only after hysteresis
        if self._bars_in_trend >= self.HYSTERESIS_BARS_TO_BLOCK:
            state.confirmed_strength = state.raw_strength
        else:
            # Reduce effective strength during confirmation period
            state.confirmed_strength = state.raw_strength * 0.5
        
        # Sync backwards compat
        state.strength = state.confirmed_strength
        state.rsi_falling = state.rsi_slope < 0
        
        # Track consecutive bars for cooldown reset
        if raw_trend == "BULLISH":
            self._consecutive_bullish_bars += 1
            self._consecutive_bearish_bars = 0
        elif raw_trend == "BEARISH":
            self._consecutive_bearish_bars += 1
            self._consecutive_bullish_bars = 0
        else:
            pass  # Keep tracking
        
        # =================================================================
        # 7. EVALUATE ENTRY PERMISSION
        # =================================================================
        
        is_opposing = (htf_bias == "LONG" and state.micro_trend == "BEARISH") or \
                      (htf_bias == "SHORT" and state.micro_trend == "BULLISH")
        
        is_aligned = (htf_bias == "LONG" and state.micro_trend == "BULLISH") or \
                     (htf_bias == "SHORT" and state.micro_trend == "BEARISH")
        
        # Calculate score adjustment
        if is_aligned:
            state.score_adjustment = self.SCORE_ALIGNMENT_BONUS
        elif is_opposing:
            if state.confirmed_strength >= self.STRENGTH_HARD_BLOCK:
                state.score_adjustment = self.SCORE_HARD_PENALTY
            elif state.confirmed_strength >= self.STRENGTH_SOFT_BLOCK:
                state.score_adjustment = self.SCORE_SOFT_PENALTY
            elif state.confirmed_strength >= self.STRENGTH_WARNING:
                state.score_adjustment = self.SCORE_WARNING_PENALTY
        
        # Decision
        allowed = True
        reason = ""
        
        # 🔥 FIX (Jan 22, 2026): Conditional hard block based on regime_mode
        # Hard block only if:
        # 1. Opposing with high strength
        # 2. State confirmed by hysteresis
        # 3. hard_block_enabled
        # 4. 🔥 NEW: In RANGE mode, only block if strength >= 60% AND ADX < 14
        #    In TREND mode, only penalty (no hard block)
        
        strength_threshold = self.STRENGTH_HARD_BLOCK
        should_hard_block = False
        
        if is_opposing and self._bars_in_trend >= self.HYSTERESIS_BARS_TO_BLOCK:
            if regime_mode == 'RANGE':
                # RANGE: Only hard block if strength very high AND ADX very low (chop)
                adx_is_low = (adx_value is not None and adx_value < self.ADX_LOW_FOR_RANGE_BLOCK)
                if state.confirmed_strength >= self.STRENGTH_HARD_BLOCK_RANGE and adx_is_low:
                    should_hard_block = self.hard_block_enabled
                    logger.info(f"🔒 RANGE hard block: strength={state.confirmed_strength:.0%}, ADX={adx_value:.1f}")
                else:
                    # In RANGE with decent ADX: only penalty
                    should_hard_block = False
                    if state.confirmed_strength >= self.STRENGTH_SOFT_BLOCK:
                        logger.info(f"⚠️ RANGE soft block: strength={state.confirmed_strength:.0%}, ADX={adx_value:.1f if adx_value else 'N/A'} - penalty only")
            elif regime_mode == 'TREND':
                # TREND: Never hard block, only penalty
                should_hard_block = False
                if state.confirmed_strength >= self.STRENGTH_HARD_BLOCK:
                    logger.info(f"⚠️ TREND mode: micro opposing (str={state.confirmed_strength:.0%}) - penalty only, no hard block")
            else:
                # Default/BREAKOUT: Original logic
                should_hard_block = (
                    state.confirmed_strength >= self.STRENGTH_HARD_BLOCK and
                    self.hard_block_enabled
                )
        
        if should_hard_block:
            allowed = False
            signals = []
            if state.is_ema_bearish and htf_bias == "LONG":
                signals.append(f"EMA20<EMA50")
            if state.has_swing_ll:
                signals.append("SwingLL")
            if state.ema20_slope < -2:
                signals.append(f"Slope={state.ema20_slope:.1f}")
            if state.has_1m_bearish_confirmation:
                signals.append("1m↓")
            
            reason = f"MICRO BLOCK: {', '.join(signals)} (str={state.confirmed_strength:.0%}, bars={self._bars_in_trend})"
            logger.warning(f"⛔ {reason}")
            
            # =================================================================
            # 8. CONDITIONAL RSI OVERRIDE (with reversal confirmation)
            # =================================================================
            
            # Only allow override if:
            # 1. RSI has actual reversal signal (not just oversold)
            # 2. Base score is high enough (optional safety)
            # 3. 1m shows bullish confirmation (extra safety)
            
            if htf_bias == "LONG" and state.rsi_5m < self.RSI_EXTREME_LOW:
                has_reversal = state.rsi_has_bullish_reversal
                has_1m_confirm = state.has_1m_bullish_confirmation
                score_ok = base_score >= self.RSI_OVERRIDE_MIN_BASE_SCORE or base_score == 0  # 0 means not provided
                
                if has_reversal and (has_1m_confirm or score_ok):
                    allowed = True
                    state.score_adjustment = self.SCORE_WARNING_PENALTY  # Still penalize
                    reason = f"RSI REVERSAL OVERRIDE: {state.rsi_reversal_reason}"
                    if has_1m_confirm:
                        reason += " + 1m bullish"
                    logger.info(f"🔄 {reason}")
                elif has_reversal:
                    # Has reversal but no confirmation - reduce penalty but don't override
                    state.score_adjustment = self.SCORE_SOFT_PENALTY
                    logger.info(f"📊 RSI reversal detected but no 1m confirm - reducing penalty only")
            
            elif htf_bias == "SHORT" and state.rsi_5m > self.RSI_EXTREME_HIGH:
                has_reversal = state.rsi_has_bearish_reversal
                has_1m_confirm = state.has_1m_bearish_confirmation
                score_ok = base_score >= self.RSI_OVERRIDE_MIN_BASE_SCORE or base_score == 0
                
                if has_reversal and (has_1m_confirm or score_ok):
                    allowed = True
                    state.score_adjustment = self.SCORE_WARNING_PENALTY
                    reason = f"RSI REVERSAL OVERRIDE (bearish divergence detected)"
                    logger.info(f"🔄 {reason}")
        
        elif is_opposing:
            reason = f"MICRO {state.micro_trend}: str={state.confirmed_strength:.0%} (penalty={state.score_adjustment})"
            logger.info(f"⚠️ {reason}")
        
        elif is_aligned:
            reason = f"MICRO {state.micro_trend} ✓ (bonus={state.score_adjustment:+.0f})"
            logger.debug(f"✅ {reason}")
        
        else:
            reason = f"MICRO {state.micro_trend}: str={state.confirmed_strength:.0%}"
            logger.debug(f"📊 {reason}")
        
        self._last_state = state
        return allowed, reason, state
    
    def should_reset_cooldown(self, min_bullish_bars: int = 3, 
                               min_strength: float = 0.5) -> Tuple[bool, str]:
        """
        Check if conditions warrant resetting the loss cooldown.
        
        Called from signal_engine to allow early cooldown exit.
        
        Returns:
            (should_reset, reason)
        """
        if self._last_state is None:
            return False, "No state available"
        
        # Reset if micro trend is BULLISH with strength for N bars
        if (self._consecutive_bullish_bars >= min_bullish_bars and 
            self._last_state.confirmed_strength >= min_strength):
            return True, f"Bullish for {self._consecutive_bullish_bars} bars (str={self._last_state.confirmed_strength:.0%})"
        
        # Reset if RSI has bullish reversal
        if self._last_state.rsi_has_bullish_reversal and self._last_state.has_1m_bullish_confirmation:
            return True, "RSI bullish reversal + 1m confirmation"
        
        return False, "Conditions not met"
    
    def get_cooldown_reset_info(self) -> dict:
        """Get info for smart cooldown reset decisions"""
        return {
            'consecutive_bullish_bars': self._consecutive_bullish_bars,
            'consecutive_bearish_bars': self._consecutive_bearish_bars,
            'current_trend': self._current_trend,
            'bars_in_trend': self._bars_in_trend,
            'last_strength': self._last_state.confirmed_strength if self._last_state else 0,
            'has_bullish_reversal': self._last_state.rsi_has_bullish_reversal if self._last_state else False
        }
    
    def get_last_state(self) -> Optional[MicroTrendState]:
        return self._last_state


# =============================================================================
# SINGLETON
# =============================================================================

_micro_trend_filter: Optional[MicroTrendFilter] = None


def get_micro_trend_filter(hard_block_enabled: bool = True) -> MicroTrendFilter:
    global _micro_trend_filter
    if _micro_trend_filter is None:
        _micro_trend_filter = MicroTrendFilter(hard_block_enabled=hard_block_enabled)
    return _micro_trend_filter


def reset_micro_trend_filter():
    global _micro_trend_filter
    _micro_trend_filter = None
