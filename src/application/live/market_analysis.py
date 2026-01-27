"""
Market Analysis Helpers - Extracted from SignalEngine
Reduces trading_app.py complexity by ~600 lines.

Contains:
- BiasStrengthDetector: Bull/Bear/Neutral detection
- TrendExpansionDetector: Range→Trend transition
- ProbingCriteriaChecker: Probing eligibility
- ShortObstinacyCalculator: Short in bullish context penalty
- ExpectedRCalculator: R/R filter based on HTF levels
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

import numpy as np
import pandas as pd

from src.shared.indicators import calculate_atr, calculate_adx
from src.domain.market.filters import calculate_vpoc

# 🔥 FIX: Import BiasStrength from config to avoid type mismatch
from src.config.trading_config import BiasStrength

if TYPE_CHECKING:
    pass  # Future type imports

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS AND CONFIGS
# ============================================================================

# 🔥 FIX: BiasStrength moved to src.config.trading_config to avoid duplication


@dataclass(frozen=True)
class BiasStrengthConfig:
    """Configuration for bias strength detection"""
    ADX_STRONG_THRESHOLD: float = 25.0
    ADX_WEAK_THRESHOLD: float = 18.0
    EMA_SEPARATION_PCT: float = 1.0  # 1% separation
    VWAP_SLOPE_STRONG: float = 0.0015


@dataclass(frozen=True)
class TrendExpansionConfig:
    """Configuration for trend expansion detection"""
    BB_WIDTH_LOOKBACK: int = 20
    BB_WIDTH_EXPANSION_RATIO: float = 1.2
    ATR_EXPANSION_RATIO: float = 1.3
    VWAP_DISTANCE_PERSISTENCE: int = 5
    VWAP_DISTANCE_ATR: float = 1.0
    ADX_VALUE_EXPANDING: float = 20.0
    ADX_SLOPE_POSITIVE_THRESHOLD: float = 0.3
    MIN_CRITERIA_FOR_EXPANSION: int = 3


@dataclass(frozen=True)
class ShortObstinacyConfig:
    """Configuration for short obstinacy penalty"""
    PRICE_ABOVE_VWAP: bool = True
    VWAP_SLOPE_POSITIVE: bool = True
    VWAP_DISTANCE_K: float = 1.0  # k*ATR above VWAP
    EMA20_ABOVE_EMA50: bool = True
    MAX_PENALTY: float = -0.3  # 30% size reduction
    
    @staticmethod
    def calculate_short_penalty(bullish_signals: int) -> float:
        """Calculate penalty based on number of bullish signals"""
        if bullish_signals >= 4:
            return -0.30  # Full penalty
        elif bullish_signals >= 3:
            return -0.20
        elif bullish_signals >= 2:
            return -0.10
        return 0.0


# ============================================================================
# DATACLASSES FOR RESULTS
# ============================================================================

@dataclass
class BiasStrengthResult:
    """Result of bias strength detection"""
    strength: BiasStrength
    adx: float
    ema_separation: float
    vwap_slope: float
    is_bullish_structure: bool


@dataclass
class TrendExpansionResult:
    """Result of trend expansion detection"""
    is_expanding: bool
    criteria_met: dict
    criteria_count: int


@dataclass
class ProbingResult:
    """Result of probing criteria check"""
    size_multiplier: float  # 0.0 = blocked
    reason: str
    conviction_score: int
    level: str  # PROBE_MIN, PROBE_MED, PROBE_MAX, or "BLOCKED"


@dataclass
class ExpectedRResult:
    """Result of expected R calculation"""
    can_enter: bool
    reason: str
    expected_r: float
    position_in_range: float


# ============================================================================
# BIAS STRENGTH DETECTOR
# ============================================================================

class BiasStrengthDetector:
    """
    Detects market bias strength for asymmetric direction rules.
    
    Uses:
    - ADX level (strong vs weak trend)
    - EMA50/EMA200 separation
    - VWAP slope
    """
    
    def __init__(self, config: BiasStrengthConfig | None = None):
        self.config = config or BiasStrengthConfig()
    
    def detect(
        self,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float
    ) -> BiasStrengthResult:
        """
        Detect the strength of market bias.
        
        Returns: BiasStrengthResult with BULL_STRONG, BEAR_STRONG, or NEUTRAL
        """
        try:
            # Calculate indicators
            ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
            ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            
            adx_vals, _ = calculate_adx(
                df_1h['high'].to_numpy(),
                df_1h['low'].to_numpy(),
                df_1h['close'].to_numpy(),
                14
            )
            adx = float(adx_vals[-1]) if len(adx_vals) > 0 else 20.0
            
            # EMA separation percentage
            ema_separation = abs(ema_50 - ema_200) / ema_200 * 100 if ema_200 > 0 else 0
            
            # VWAP slope from 5m
            vwap_slope = self._calculate_vwap_slope(df_5m, current_price)
            
            # Determine direction
            is_bullish_structure = ema_50 > ema_200
            is_strong_adx = adx > self.config.ADX_STRONG_THRESHOLD
            is_weak_adx = adx < self.config.ADX_WEAK_THRESHOLD
            has_separation = ema_separation > self.config.EMA_SEPARATION_PCT
            
            logger.debug(
                f"BiasStrength: ADX={adx:.1f}, EMA_sep={ema_separation:.2f}%, "
                f"VWAP_slope={vwap_slope:.6f}, bull_struct={is_bullish_structure}"
            )
            
            # Decision logic
            if is_weak_adx or not has_separation:
                strength = BiasStrength.NEUTRAL
            elif is_strong_adx and has_separation:
                if is_bullish_structure:
                    strength = BiasStrength.BULL_STRONG
                else:
                    strength = BiasStrength.BEAR_STRONG
            else:
                # Medium ADX - check VWAP slope for tiebreaker
                if abs(vwap_slope) > self.config.VWAP_SLOPE_STRONG:
                    if vwap_slope > 0:
                        strength = BiasStrength.BULL_STRONG
                    else:
                        strength = BiasStrength.BEAR_STRONG
                else:
                    strength = BiasStrength.NEUTRAL
            
            return BiasStrengthResult(
                strength=strength,
                adx=adx,
                ema_separation=ema_separation,
                vwap_slope=vwap_slope,
                is_bullish_structure=is_bullish_structure
            )
            
        except Exception as e:
            logger.debug(f"BiasStrength detection error: {e}")
            return BiasStrengthResult(
                strength=BiasStrength.NEUTRAL,
                adx=20.0,
                ema_separation=0.0,
                vwap_slope=0.0,
                is_bullish_structure=False
            )
    
    def _calculate_vwap_slope(self, df_5m: pd.DataFrame, current_price: float) -> float:
        """Calculate VWAP slope from 5m data"""
        try:
            if 'vwap' in df_5m.columns and len(df_5m) >= 10:
                vwap_vals = df_5m['vwap'].tail(10).values
                return (vwap_vals[-1] - vwap_vals[0]) / (10 * current_price) if current_price > 0 else 0
            else:
                # Calculate simple VWAP proxy
                typical_price = (df_5m['high'] + df_5m['low'] + df_5m['close']) / 3
                volume = df_5m['volume'] if 'volume' in df_5m.columns else pd.Series(1, index=df_5m.index)
                vwap = (typical_price * volume).cumsum() / volume.cumsum()
                vwap_vals = vwap.tail(10).values
                if len(vwap_vals) >= 10 and current_price > 0:
                    return (vwap_vals[-1] - vwap_vals[0]) / (10 * current_price)
                return 0.0
        except Exception:
            return 0.0


# ============================================================================
# TREND EXPANSION DETECTOR
# ============================================================================

class TrendExpansionDetector:
    """
    Detects when market shifts from range to trending.
    
    Criteria (need 3 of 4):
    1. BB width expanding after compression
    2. ATR ratio increasing
    3. Price persistently away from VWAP
    4. ADX slope positive
    """
    
    def __init__(self, config: TrendExpansionConfig | None = None):
        self.config = config or TrendExpansionConfig()
    
    def detect(
        self,
        df_5m: pd.DataFrame,
        df_1m: pd.DataFrame,
        current_price: float,
        atr_5m: float
    ) -> TrendExpansionResult:
        """
        Detect if market is in trend expansion mode (not ranging).
        
        Returns: TrendExpansionResult with criteria details
        """
        criteria_met = {
            'bb_expansion': False,
            'atr_expansion': False,
            'vwap_distance': False,
            'adx_rising': False
        }
        
        try:
            # 1. Bollinger Band Width expansion
            criteria_met['bb_expansion'] = self._check_bb_expansion(df_5m)
            
            # 2. ATR expansion
            criteria_met['atr_expansion'] = self._check_atr_expansion(df_5m)
            
            # 3. VWAP distance persistence
            criteria_met['vwap_distance'] = self._check_vwap_distance(df_5m, atr_5m)
            
            # 4. ADX rising
            criteria_met['adx_rising'] = self._check_adx_rising(df_5m)
            
            # Count criteria met
            criteria_count = sum(criteria_met.values())
            is_expanding = criteria_count >= self.config.MIN_CRITERIA_FOR_EXPANSION
            
            if is_expanding:
                logger.info(
                    f"🚀 TREND EXPANSION DETECTED: "
                    f"{criteria_count}/4 criteria met: {criteria_met}"
                )
            
            return TrendExpansionResult(
                is_expanding=is_expanding,
                criteria_met=criteria_met,
                criteria_count=criteria_count
            )
            
        except Exception as e:
            logger.debug(f"TrendExpansion detection error: {e}")
            return TrendExpansionResult(
                is_expanding=False,
                criteria_met=criteria_met,
                criteria_count=0
            )
    
    def _check_bb_expansion(self, df_5m: pd.DataFrame) -> bool:
        """Check Bollinger Band width expansion"""
        if len(df_5m) < self.config.BB_WIDTH_LOOKBACK:
            return False
        
        try:
            bb_ma = df_5m['close'].rolling(20).mean()
            bb_std = df_5m['close'].rolling(20).std()
            bb_width = (2 * bb_std / bb_ma).fillna(0)
            
            current_width = bb_width.iloc[-1]
            avg_width = bb_width.tail(self.config.BB_WIDTH_LOOKBACK).mean()
            
            if avg_width > 0 and current_width > avg_width * self.config.BB_WIDTH_EXPANSION_RATIO:
                logger.debug(
                    f"TrendExpansion: BB width {current_width:.4f} > "
                    f"{avg_width * self.config.BB_WIDTH_EXPANSION_RATIO:.4f}"
                )
                return True
        except Exception:
            pass
        return False
    
    def _check_atr_expansion(self, df_5m: pd.DataFrame) -> bool:
        """Check ATR expansion"""
        try:
            atr_vals = calculate_atr(
                df_5m['high'].to_numpy(),
                df_5m['low'].to_numpy(),
                df_5m['close'].to_numpy(),
                14
            )
            if len(atr_vals) >= 20:
                current_atr = atr_vals[-1]
                avg_atr = np.mean(atr_vals[-20:])
                
                if avg_atr > 0 and current_atr > avg_atr * self.config.ATR_EXPANSION_RATIO:
                    logger.debug(
                        f"TrendExpansion: ATR {current_atr:.2f} > "
                        f"{avg_atr * self.config.ATR_EXPANSION_RATIO:.2f}"
                    )
                    return True
        except Exception:
            pass
        return False
    
    def _check_vwap_distance(self, df_5m: pd.DataFrame, atr_5m: float) -> bool:
        """Check VWAP distance persistence"""
        if len(df_5m) < self.config.VWAP_DISTANCE_PERSISTENCE:
            return False
        
        try:
            # Calculate VWAP
            typical_price = (df_5m['high'] + df_5m['low'] + df_5m['close']) / 3
            volume = df_5m['volume'] if 'volume' in df_5m.columns else pd.Series(1, index=df_5m.index)
            vwap = (typical_price * volume).cumsum() / volume.cumsum()
            
            # Check last N candles
            recent_closes = df_5m['close'].tail(self.config.VWAP_DISTANCE_PERSISTENCE).values
            recent_vwap = vwap.tail(self.config.VWAP_DISTANCE_PERSISTENCE).values
            
            # Count candles where price > 1 ATR from VWAP
            away_count = 0
            for close, vw in zip(recent_closes, recent_vwap):
                if abs(close - vw) > atr_5m * self.config.VWAP_DISTANCE_ATR:
                    away_count += 1
            
            if away_count >= self.config.VWAP_DISTANCE_PERSISTENCE - 1:
                logger.debug(
                    f"TrendExpansion: VWAP distance persisted "
                    f"{away_count}/{self.config.VWAP_DISTANCE_PERSISTENCE} candles"
                )
                return True
        except Exception:
            pass
        return False
    
    def _check_adx_rising(self, df_5m: pd.DataFrame) -> bool:
        """Check ADX rising"""
        try:
            adx_vals, _ = calculate_adx(
                df_5m['high'].to_numpy(),
                df_5m['low'].to_numpy(),
                df_5m['close'].to_numpy(),
                14
            )
            if len(adx_vals) >= 5:
                adx_current = float(adx_vals[-1])
                adx_prev = float(adx_vals[-5])
                adx_slope = (adx_current - adx_prev) / 5
                
                if (adx_current > self.config.ADX_VALUE_EXPANDING and 
                    adx_slope > self.config.ADX_SLOPE_POSITIVE_THRESHOLD):
                    logger.debug(
                        f"TrendExpansion: ADX {adx_current:.1f} "
                        f"rising with slope {adx_slope:.2f}"
                    )
                    return True
        except Exception:
            pass
        return False


# ============================================================================
# PROBING CRITERIA CHECKER
# ============================================================================

class ProbingCriteriaChecker:
    """
    PROBING = micro-risk validation with GRADUATED sizing.
    
    Levels:
    - PROBE_MIN (0.25x): Base criteria met, minimal conviction
    - PROBE_MED (0.50x): Base + EMA alignment + VWAP slope
    - PROBE_MAX (0.65x): Base + squeeze ending + volume spike
    """
    
    def check(
        self,
        current_price: float,
        atr_5m: float,
        bias: str | None,
        htf_cache_passed: bool,
        consecutive_losses: int,
        session_quality: str,
        df_1h: pd.DataFrame | None = None,
        df_5m: pd.DataFrame | None = None
    ) -> ProbingResult:
        """
        Check PROBING eligibility and determine sizing level.
        
        Returns: ProbingResult with size_multiplier (0.0 = blocked)
        """
        # Phase 1: Hard blocks
        if bias is None:
            return ProbingResult(0.0, "No bias determined", 0, "BLOCKED")
        
        if not htf_cache_passed:
            return ProbingResult(0.0, "HTF filter not passed", 0, "BLOCKED")
        
        if consecutive_losses >= 2:
            return ProbingResult(
                0.0,
                f"Too many recent losses ({consecutive_losses})",
                0,
                "BLOCKED"
            )
        
        if session_quality == 'poor':
            return ProbingResult(0.0, "Poor session quality", 0, "BLOCKED")
        
        # Phase 2: Calculate metrics
        metrics = self._calculate_metrics(current_price, atr_5m, bias, df_1h, df_5m)
        
        # Phase 3: Base criteria check
        if metrics['vpoc_price'] is None or metrics['vpoc_price'] <= 0:
            return ProbingResult(0.0, "No VPOC reference available", 0, "BLOCKED")
        
        if metrics['vpoc_distance_atr'] > 3.0:
            return ProbingResult(
                0.0,
                f"Too far from VPOC ({metrics['vpoc_distance_atr']:.1f} ATR)",
                0,
                "BLOCKED"
            )
        
        if metrics['vwap_distance_atr'] > 4.0:
            return ProbingResult(
                0.0,
                f"Too far from VWAP ({metrics['vwap_distance_atr']:.1f} ATR)",
                0,
                "BLOCKED"
            )
        
        # Check VWAP slope isn't against bias
        if bias == 'LONG' and metrics['vwap_slope'] < -0.002:
            return ProbingResult(
                0.0,
                f"VWAP falling against LONG ({metrics['vwap_slope']*100:.2f}%)",
                0,
                "BLOCKED"
            )
        elif bias == 'SHORT' and metrics['vwap_slope'] > 0.002:
            return ProbingResult(
                0.0,
                f"VWAP rising against SHORT ({metrics['vwap_slope']*100:.2f}%)",
                0,
                "BLOCKED"
            )
        
        # Phase 4: Determine probing level
        conviction_score, conviction_reasons = self._calculate_conviction(bias, metrics)
        
        # Phase 5: Map conviction to size
        if conviction_score >= 5:
            size_mult = 0.65
            level = "PROBE_MAX"
        elif conviction_score >= 3:
            size_mult = 0.50
            level = "PROBE_MED"
        else:
            size_mult = 0.25
            level = "PROBE_MIN"
        
        reason = (
            f"{level}(score={conviction_score}) "
            f"VPOC={metrics['vpoc_distance_atr']:.1f} VWAP={metrics['vwap_distance_atr']:.1f}"
        )
        if conviction_reasons:
            reason += f" [{','.join(conviction_reasons)}]"
        
        return ProbingResult(size_mult, reason, conviction_score, level)
    
    def _calculate_metrics(
        self,
        current_price: float,
        atr_5m: float,
        bias: str,
        df_1h: pd.DataFrame | None,
        df_5m: pd.DataFrame | None
    ) -> dict:
        """Calculate all metrics needed for probing decision"""
        metrics = {
            'vpoc_price': None,
            'vpoc_distance_atr': 999.0,
            'current_vwap': 0.0,
            'vwap_distance_atr': 999.0,
            'vwap_slope': 0.0,
            'ema20_aligned': False,
            'ema50_aligned': False,
            'bb_squeeze_ending': False,
            'volume_spike': False
        }
        
        # VPOC calculation
        if df_1h is not None and len(df_1h) >= 20:
            try:
                metrics['vpoc_price'] = calculate_vpoc(df_1h)
                if metrics['vpoc_price'] and metrics['vpoc_price'] > 0 and atr_5m > 0:
                    metrics['vpoc_distance_atr'] = abs(
                        current_price - float(metrics['vpoc_price'])
                    ) / atr_5m
            except Exception as e:
                logger.debug(f"PROBING: VPOC calc error: {e}")
        
        # VWAP and slope calculation from 5m
        if df_5m is not None and len(df_5m) >= 20:
            try:
                typical_price = (df_5m['high'] + df_5m['low'] + df_5m['close']) / 3
                volume = df_5m['volume'] if 'volume' in df_5m.columns else pd.Series(1, index=df_5m.index)
                vwap = (typical_price * volume).cumsum() / volume.cumsum()
                metrics['current_vwap'] = float(vwap.iloc[-1])
                
                if metrics['current_vwap'] > 0 and atr_5m > 0:
                    metrics['vwap_distance_atr'] = abs(
                        current_price - metrics['current_vwap']
                    ) / atr_5m
                
                # VWAP slope (last 10 bars)
                if len(vwap) >= 10 and vwap.iloc[-10] > 0:
                    metrics['vwap_slope'] = (vwap.iloc[-1] - vwap.iloc[-10]) / vwap.iloc[-10]
            except Exception as e:
                logger.debug(f"PROBING: VWAP calc error: {e}")
        
        # EMA alignment from 5m
        if df_5m is not None and len(df_5m) >= 55:
            try:
                ema20 = df_5m['close'].ewm(span=20, adjust=False).mean()
                ema50 = df_5m['close'].ewm(span=50, adjust=False).mean()
                
                # EMA20 slope check
                ema20_slope = (ema20.iloc[-1] - ema20.iloc[-5]) / ema20.iloc[-5] if ema20.iloc[-5] > 0 else 0
                
                # EMA alignment
                if bias == 'LONG' and ema20_slope > 0.0005:
                    metrics['ema20_aligned'] = True
                elif bias == 'SHORT' and ema20_slope < -0.0005:
                    metrics['ema20_aligned'] = True
                
                # EMA50 alignment
                if bias == 'LONG' and ema20.iloc[-1] > ema50.iloc[-1]:
                    metrics['ema50_aligned'] = True
                elif bias == 'SHORT' and ema20.iloc[-1] < ema50.iloc[-1]:
                    metrics['ema50_aligned'] = True
                    
            except Exception as e:
                logger.debug(f"PROBING: EMA calc error: {e}")
        
        # BB squeeze detection from 5m
        if df_5m is not None and len(df_5m) >= 25:
            try:
                bb_ma = df_5m['close'].rolling(20).mean()
                bb_std = df_5m['close'].rolling(20).std()
                bb_width = (2 * bb_std / bb_ma).fillna(0)
                
                current_width = bb_width.iloc[-1]
                prev_width = bb_width.iloc[-5]
                avg_width = bb_width.tail(20).mean()
                
                if prev_width < avg_width * 0.9 and current_width > prev_width * 1.05:
                    metrics['bb_squeeze_ending'] = True
            except Exception as e:
                logger.debug(f"PROBING: BB calc error: {e}")
        
        # Volume spike detection from 5m
        if df_5m is not None and len(df_5m) >= 20 and 'volume' in df_5m.columns:
            try:
                vol_avg = df_5m['volume'].tail(20).mean()
                vol_current = df_5m['volume'].iloc[-1]
                
                if vol_avg > 0 and vol_current > vol_avg * 1.5:
                    metrics['volume_spike'] = True
            except Exception as e:
                logger.debug(f"PROBING: Volume calc error: {e}")
        
        return metrics
    
    def _calculate_conviction(self, bias: str, metrics: dict) -> tuple[int, list[str]]:
        """Calculate conviction score and reasons"""
        conviction_score = 1  # Base passed
        conviction_reasons = []
        
        # VPOC very close (<0.75 ATR)
        if metrics['vpoc_distance_atr'] < 0.75:
            conviction_score += 1
            conviction_reasons.append(f"VPOC_close({metrics['vpoc_distance_atr']:.1f})")
        
        # VWAP very close (<1.0 ATR)
        if metrics['vwap_distance_atr'] < 1.0:
            conviction_score += 1
            conviction_reasons.append(f"VWAP_close({metrics['vwap_distance_atr']:.1f})")
        
        # VWAP slope supports bias
        if (bias == 'LONG' and metrics['vwap_slope'] > 0.0005) or \
           (bias == 'SHORT' and metrics['vwap_slope'] < -0.0005):
            conviction_score += 1
            conviction_reasons.append("VWAP_slope_aligned")
        
        # EMA20 aligned
        if metrics['ema20_aligned']:
            conviction_score += 1
            conviction_reasons.append("EMA20_aligned")
        
        # EMA50 aligned
        if metrics['ema50_aligned']:
            conviction_score += 1
            conviction_reasons.append("EMA50_aligned")
        
        # BB squeeze ending
        if metrics['bb_squeeze_ending']:
            conviction_score += 1
            conviction_reasons.append("BB_squeeze_ending")
        
        # Volume spike
        if metrics['volume_spike']:
            conviction_score += 1
            conviction_reasons.append("Volume_spike")
        
        return conviction_score, conviction_reasons


# ============================================================================
# SHORT OBSTINACY CALCULATOR
# ============================================================================

class ShortObstinacyCalculator:
    """
    Calculates penalty for shorting in bullish context.
    
    Checks:
    1. Price above VWAP
    2. VWAP slope positive
    3. Price > VWAP + k*ATR
    4. EMA20 > EMA50 on 5m
    """
    
    def __init__(self, config: ShortObstinacyConfig | None = None):
        self.config = config or ShortObstinacyConfig()
    
    def calculate(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        atr_5m: float
    ) -> tuple[float, int, str]:
        """
        Calculate penalty for shorting in bullish context.
        
        Returns: (penalty, bullish_signals, reason_string)
        """
        bullish_signals = 0
        reasons = []
        
        try:
            # Calculate VWAP
            typical_price = (df_5m['high'] + df_5m['low'] + df_5m['close']) / 3
            volume = df_5m['volume'] if 'volume' in df_5m.columns else pd.Series(1, index=df_5m.index)
            vwap = (typical_price * volume).cumsum() / volume.cumsum()
            current_vwap = vwap.iloc[-1]
            
            # 1. Price above VWAP
            if self.config.PRICE_ABOVE_VWAP and current_price > current_vwap:
                bullish_signals += 1
                reasons.append("price>VWAP")
            
            # 2. VWAP slope positive
            if self.config.VWAP_SLOPE_POSITIVE and len(vwap) >= 10:
                vwap_slope = (vwap.iloc[-1] - vwap.iloc[-10]) / vwap.iloc[-10] if vwap.iloc[-10] > 0 else 0
                if vwap_slope > 0:
                    bullish_signals += 1
                    reasons.append("VWAP_slope+")
            
            # 3. Price > VWAP + k*ATR
            if current_price > current_vwap + self.config.VWAP_DISTANCE_K * atr_5m:
                bullish_signals += 1
                reasons.append(f"price>{self.config.VWAP_DISTANCE_K}ATR_above_VWAP")
            
            # 4. EMA20 > EMA50
            if self.config.EMA20_ABOVE_EMA50:
                ema_20 = df_5m['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                ema_50 = df_5m['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                if ema_20 > ema_50:
                    bullish_signals += 1
                    reasons.append("EMA20>EMA50")
            
            penalty = self.config.calculate_short_penalty(bullish_signals)
            reason_str = ", ".join(reasons) if reasons else "none"
            
            if penalty < 0:
                logger.info(
                    f"⚠️ SHORT OBSTINACY: {bullish_signals} bullish signals "
                    f"({reason_str}) → penalty {penalty}"
                )
            
            return penalty, bullish_signals, reason_str
            
        except Exception as e:
            logger.debug(f"ShortObstinacy calculation error: {e}")
            return 0.0, 0, "error"


# ============================================================================
# EXPECTED R CALCULATOR
# ============================================================================

class ExpectedRCalculator:
    """
    Calculate expected R based on distance to HTF levels.
    
    Problem: System enters setups that can only produce 1R max.
    Solution: Calculate distance to next HTF level (resistance/support).
    If expected_move / sl_distance < 1.5 → NO trade.
    """
    
    MIN_EXPECTED_R = 1.5
    
    def check(
        self,
        current_price: float,
        sl_distance: float,
        bias: str,
        df_1h: pd.DataFrame
    ) -> ExpectedRResult:
        """
        Check if expected R is sufficient for entry.
        
        Returns: ExpectedRResult with can_enter flag
        """
        if len(df_1h) < 20:
            return ExpectedRResult(True, "Not enough HTF data for Expected R", 0.0, 0.5)
        
        if sl_distance <= 0:
            return ExpectedRResult(True, "Invalid SL distance", 0.0, 0.5)
        
        highs = df_1h['high'].to_numpy()
        lows = df_1h['low'].to_numpy()
        
        # Find recent swing high and swing low (last 20 1h candles)
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        range_size = recent_high - recent_low
        
        if range_size <= 0:
            return ExpectedRResult(True, "No valid range", 0.0, 0.5)
        
        # Calculate position in range (0 = at low, 1 = at high)
        position_in_range = (current_price - recent_low) / range_size
        
        if bias == 'LONG':
            distance_to_target = recent_high - current_price
            
            if position_in_range > 0.70:
                return ExpectedRResult(
                    False,
                    f"LONG near range top: {position_in_range:.0%} (no upside room)",
                    0.0,
                    position_in_range
                )
        else:  # SHORT
            distance_to_target = current_price - recent_low
            
            if position_in_range < 0.30:
                return ExpectedRResult(
                    False,
                    f"SHORT near range bottom: {position_in_range:.0%} (no downside room)",
                    0.0,
                    position_in_range
                )
        
        # Calculate Expected R
        expected_r = distance_to_target / sl_distance
        
        if expected_r < self.MIN_EXPECTED_R:
            return ExpectedRResult(
                False,
                f"Expected R too low: {expected_r:.2f}R < {self.MIN_EXPECTED_R}R "
                f"(target ${distance_to_target:.0f} vs SL ${sl_distance:.0f})",
                expected_r,
                position_in_range
            )
        
        return ExpectedRResult(
            True,
            f"Expected R OK: {expected_r:.2f}R >= {self.MIN_EXPECTED_R}R "
            f"(pos {position_in_range:.0%} in range)",
            expected_r,
            position_in_range
        )


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

def get_bias_strength_detector(config: BiasStrengthConfig | None = None) -> BiasStrengthDetector:
    """Factory for BiasStrengthDetector"""
    return BiasStrengthDetector(config)


def get_trend_expansion_detector(config: TrendExpansionConfig | None = None) -> TrendExpansionDetector:
    """Factory for TrendExpansionDetector"""
    return TrendExpansionDetector(config)


def get_probing_checker() -> ProbingCriteriaChecker:
    """Factory for ProbingCriteriaChecker"""
    return ProbingCriteriaChecker()


def get_short_obstinacy_calculator(config: ShortObstinacyConfig | None = None) -> ShortObstinacyCalculator:
    """Factory for ShortObstinacyCalculator"""
    return ShortObstinacyCalculator(config)


def get_expected_r_calculator() -> ExpectedRCalculator:
    """Factory for ExpectedRCalculator"""
    return ExpectedRCalculator()


# ============================================================================
# CHASE ATR CALCULATOR (simple function, no class needed)
# ============================================================================

def calculate_chase_atr(
    df_1m: pd.DataFrame | None,
    atr_1m: float,
    bias: str | None
) -> float:
    """Calculate chase ATR using config values"""
    from src.config.trading_config import OrderExecution
    
    if df_1m is None or len(df_1m) < 5 or atr_1m <= 0:
        return 0.0
    
    lookback = OrderExecution.ANTI_CHASING_LOOKBACK
    
    if bias == 'LONG':
        move = df_1m['close'].iloc[-1] - df_1m['open'].iloc[-lookback]
    else:
        move = df_1m['open'].iloc[-lookback] - df_1m['close'].iloc[-1]
    
    if move <= 0 or atr_1m <= 0:
        return 0.0
    
    return move / atr_1m
