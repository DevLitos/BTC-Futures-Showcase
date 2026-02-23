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
from datetime import datetime, timedelta
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
        # 🔥 FIX #41: Track last log time to prevent spam
        self._last_expansion_log: datetime | None = None
        self._log_interval = timedelta(minutes=2)
    
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
            
            # 🔥 FIX #41: Rate limit expansion log to prevent spam
            if is_expanding:
                now = datetime.utcnow()
                should_log = (
                    self._last_expansion_log is None or
                    (now - self._last_expansion_log) > self._log_interval
                )
                if should_log:
                    self._last_expansion_log = now
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


def get_bias_strength_detector(config: BiasStrengthConfig | None = None) -> BiasStrengthDetector:
    """Factory for BiasStrengthDetector"""
    return BiasStrengthDetector(config)


def get_trend_expansion_detector(config: TrendExpansionConfig | None = None) -> TrendExpansionDetector:
    """Factory for TrendExpansionDetector"""
    return TrendExpansionDetector(config)

