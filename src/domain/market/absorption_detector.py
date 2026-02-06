"""
🛡️ ABSORPTION DETECTOR - Feb 06, 2026

Detecta señales de ABSORCIÓN antes de bloquear trades.

PROBLEMA RESUELTO:
- Sistema bloqueaba SHORTs en "low percentile" incluso cuando mercado estaba cayendo
- Falta detectar ABSORCIÓN = señal de que compradores están entrando
- Sin absorción → el crash puede continuar → debemos permitir shorts

LÓGICA:
- ABSORCIÓN = evidencia de que institucionales están acumulando
- Sin absorción = el movimiento puede continuar

SEÑALES DE ABSORCIÓN (bullish en lows):
1. Bullish engulfing en 5m o 15m
2. VWAP reclaim (precio cruza y se mantiene)
3. No new lows en últimos 20 min
4. Delta positivo (más compradores que vendedores)
5. Aumento de volumen sin nuevo mínimo

SEÑALES DE DISTRIBUCIÓN (bearish en highs):
1. Bearish engulfing
2. VWAP rejection
3. No new highs
4. Delta negativo
"""

from dataclasses import dataclass
from typing import Optional, List
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


@dataclass
class AbsorptionDetectorConfig:
    """Configuration for absorption detection."""
    
    # Engulfing detection
    ENGULF_BODY_RATIO_MIN: float = 0.6     # Body must be >= 60% of range
    ENGULF_SIZE_RATIO_MIN: float = 1.2     # Must be 1.2x previous candle
    
    # VWAP reclaim
    VWAP_HOLD_CANDLES: int = 3             # Must hold above VWAP for 3 candles
    
    # No new lows/highs
    NO_NEW_EXTREME_CANDLES: int = 4        # 4 candles (20 min on 5m TF)
    
    # Delta thresholds
    DELTA_STRONG_THRESHOLD: float = 1.5    # 1.5x more buyers than sellers
    
    # Volume increase without new extreme
    VOL_INCREASE_MIN: float = 1.3          # 30% more volume


@dataclass
class AbsorptionResult:
    """Result of absorption detection."""
    
    is_bullish_absorption: bool = False    # Buyers absorbing at lows
    is_bearish_distribution: bool = False  # Sellers absorbing at highs
    confidence: float = 0.0                # 0-1 confidence score
    reason: str = ""                       # Human-readable reason
    
    # Individual signals
    has_engulfing: bool = False
    has_vwap_reclaim: bool = False
    has_no_new_extreme: bool = False
    has_delta_confirmation: bool = False
    has_volume_confirmation: bool = False
    
    # Signal count for scoring
    signal_count: int = 0


class AbsorptionDetector:
    """
    Detects absorption patterns that signal potential reversals.
    
    Use cases:
    - Before blocking shorts at lows, check for bullish absorption
    - Before blocking longs at highs, check for bearish distribution
    - No absorption = continuation likely = allow trade
    """
    
    def __init__(self, config: Optional[AbsorptionDetectorConfig] = None):
        self.config = config or AbsorptionDetectorConfig()
        self._last_result: Optional[AbsorptionResult] = None
    
    def detect(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        vwap: Optional[float] = None,
        delta_ratio: Optional[float] = None,
        direction: str = 'SHORT'  # Which direction we're trying to trade
    ) -> AbsorptionResult:
        """
        Detect absorption patterns.
        
        Args:
            df_5m: 5-minute DataFrame with OHLCV
            current_price: Current market price
            vwap: Current VWAP price (optional)
            delta_ratio: Buy/Sell delta ratio (optional)
            direction: 'SHORT' or 'LONG' - which direction we're checking
        
        Returns:
            AbsorptionResult with detection status
        """
        result = AbsorptionResult()
        cfg = self.config
        
        if df_5m is None or len(df_5m) < 10:
            result.reason = "Insufficient data"
            return result
        
        signals = []
        
        if direction == 'SHORT':
            # We want to SHORT at lows - check for BULLISH absorption (would block our short)
            result.has_engulfing = self._check_bullish_engulfing(df_5m, cfg)
            result.has_vwap_reclaim = self._check_vwap_reclaim(df_5m, current_price, vwap, cfg)
            result.has_no_new_extreme = self._check_no_new_low(df_5m, cfg)
            result.has_delta_confirmation = delta_ratio is not None and delta_ratio >= cfg.DELTA_STRONG_THRESHOLD
            result.has_volume_confirmation = self._check_vol_without_new_low(df_5m, cfg)
            
            if result.has_engulfing:
                signals.append("bull_engulf")
            if result.has_vwap_reclaim:
                signals.append("vwap_reclaim")
            if result.has_no_new_extreme:
                signals.append("no_new_low")
            if result.has_delta_confirmation:
                signals.append(f"delta={delta_ratio:.1f}x")
            if result.has_volume_confirmation:
                signals.append("vol_absorption")
            
            result.signal_count = len(signals)
            result.is_bullish_absorption = result.signal_count >= 2
            result.confidence = min(1.0, result.signal_count * 0.25)
            
            if result.is_bullish_absorption:
                result.reason = f"BULLISH ABSORPTION: {', '.join(signals)}"
            else:
                result.reason = f"NO_ABSORPTION: signals={result.signal_count} ({', '.join(signals) if signals else 'none'})"
                
        else:  # LONG
            # We want to LONG at highs - check for BEARISH distribution (would block our long)
            result.has_engulfing = self._check_bearish_engulfing(df_5m, cfg)
            result.has_vwap_reclaim = self._check_vwap_rejection(df_5m, current_price, vwap, cfg)
            result.has_no_new_extreme = self._check_no_new_high(df_5m, cfg)
            result.has_delta_confirmation = delta_ratio is not None and delta_ratio <= (1 / cfg.DELTA_STRONG_THRESHOLD)
            result.has_volume_confirmation = self._check_vol_without_new_high(df_5m, cfg)
            
            if result.has_engulfing:
                signals.append("bear_engulf")
            if result.has_vwap_reclaim:
                signals.append("vwap_reject")
            if result.has_no_new_extreme:
                signals.append("no_new_high")
            if result.has_delta_confirmation:
                signals.append(f"delta={delta_ratio:.1f}x")
            if result.has_volume_confirmation:
                signals.append("vol_distribution")
            
            result.signal_count = len(signals)
            result.is_bearish_distribution = result.signal_count >= 2
            result.confidence = min(1.0, result.signal_count * 0.25)
            
            if result.is_bearish_distribution:
                result.reason = f"BEARISH DISTRIBUTION: {', '.join(signals)}"
            else:
                result.reason = f"NO_DISTRIBUTION: signals={result.signal_count} ({', '.join(signals) if signals else 'none'})"
        
        self._last_result = result
        
        # Log if absorption detected
        if result.is_bullish_absorption or result.is_bearish_distribution:
            mode = "🛡️ ABSORPTION" if result.is_bullish_absorption else "🔻 DISTRIBUTION"
            logger.info(f"{mode} DETECTED: conf={result.confidence:.0%} | {result.reason}")
        
        return result
    
    def _check_bullish_engulfing(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check for bullish engulfing pattern in last 3 candles."""
        if len(df) < 3:
            return False
        
        for i in range(-2, 0):  # Check last 2 candles
            curr = df.iloc[i]
            prev = df.iloc[i - 1]
            
            curr_body = abs(curr['close'] - curr['open'])
            curr_range = curr['high'] - curr['low']
            prev_range = prev['high'] - prev['low']
            
            # Must be bullish (close > open)
            if curr['close'] <= curr['open']:
                continue
            
            # Body must be >= 60% of range
            if curr_range > 0 and curr_body / curr_range < cfg.ENGULF_BODY_RATIO_MIN:
                continue
            
            # Must be larger than previous candle
            if prev_range > 0 and curr_range / prev_range >= cfg.ENGULF_SIZE_RATIO_MIN:
                # Must engulf previous body
                if curr['close'] > prev['open'] and curr['open'] < prev['close']:
                    return True
        
        return False
    
    def _check_bearish_engulfing(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check for bearish engulfing pattern in last 3 candles."""
        if len(df) < 3:
            return False
        
        for i in range(-2, 0):
            curr = df.iloc[i]
            prev = df.iloc[i - 1]
            
            curr_body = abs(curr['close'] - curr['open'])
            curr_range = curr['high'] - curr['low']
            prev_range = prev['high'] - prev['low']
            
            # Must be bearish (close < open)
            if curr['close'] >= curr['open']:
                continue
            
            if curr_range > 0 and curr_body / curr_range < cfg.ENGULF_BODY_RATIO_MIN:
                continue
            
            if prev_range > 0 and curr_range / prev_range >= cfg.ENGULF_SIZE_RATIO_MIN:
                if curr['close'] < prev['open'] and curr['open'] > prev['close']:
                    return True
        
        return False
    
    def _check_vwap_reclaim(self, df: pd.DataFrame, price: float, vwap: Optional[float], cfg: AbsorptionDetectorConfig) -> bool:
        """Check if price reclaimed and holds above VWAP."""
        if vwap is None:
            return False
        
        # Current price above VWAP
        if price <= vwap:
            return False
        
        # Check if last N closes are above VWAP (crude approximation)
        recent_closes = df['close'].tail(cfg.VWAP_HOLD_CANDLES)
        return bool((recent_closes > vwap * 0.999).all())  # Small tolerance
    
    def _check_vwap_rejection(self, df: pd.DataFrame, price: float, vwap: Optional[float], cfg: AbsorptionDetectorConfig) -> bool:
        """Check if price rejected and holds below VWAP."""
        if vwap is None:
            return False
        
        if price >= vwap:
            return False
        
        recent_closes = df['close'].tail(cfg.VWAP_HOLD_CANDLES)
        return bool((recent_closes < vwap * 1.001).all())
    
    def _check_no_new_low(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check if no new lows in recent candles."""
        if len(df) < cfg.NO_NEW_EXTREME_CANDLES + 2:
            return False
        
        # Get lowest low before the check period
        prior_low = df['low'].iloc[:-cfg.NO_NEW_EXTREME_CANDLES].min()
        # Get lowest low in recent period
        recent_low = df['low'].tail(cfg.NO_NEW_EXTREME_CANDLES).min()
        
        # No new low if recent low is higher than prior
        return recent_low > prior_low
    
    def _check_no_new_high(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check if no new highs in recent candles."""
        if len(df) < cfg.NO_NEW_EXTREME_CANDLES + 2:
            return False
        
        prior_high = df['high'].iloc[:-cfg.NO_NEW_EXTREME_CANDLES].max()
        recent_high = df['high'].tail(cfg.NO_NEW_EXTREME_CANDLES).max()
        
        return recent_high < prior_high
    
    def _check_vol_without_new_low(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check for volume increase without new low."""
        if len(df) < 5 or 'volume' not in df.columns:
            return False
        
        recent = df.tail(3)
        prior = df.iloc[-8:-3] if len(df) >= 8 else df.iloc[:-3]
        
        recent_vol = recent['volume'].mean()
        prior_vol = prior['volume'].mean() if len(prior) > 0 else recent_vol
        
        if prior_vol == 0:
            return False
        
        vol_increase = recent_vol / prior_vol >= cfg.VOL_INCREASE_MIN
        no_new_low = recent['low'].min() >= prior['low'].min()
        
        return vol_increase and no_new_low
    
    def _check_vol_without_new_high(self, df: pd.DataFrame, cfg: AbsorptionDetectorConfig) -> bool:
        """Check for volume increase without new high."""
        if len(df) < 5 or 'volume' not in df.columns:
            return False
        
        recent = df.tail(3)
        prior = df.iloc[-8:-3] if len(df) >= 8 else df.iloc[:-3]
        
        recent_vol = recent['volume'].mean()
        prior_vol = prior['volume'].mean() if len(prior) > 0 else recent_vol
        
        if prior_vol == 0:
            return False
        
        vol_increase = recent_vol / prior_vol >= cfg.VOL_INCREASE_MIN
        no_new_high = recent['high'].max() <= prior['high'].max()
        
        return vol_increase and no_new_high
    
    @property
    def last_result(self) -> Optional[AbsorptionResult]:
        """Get last detection result."""
        return self._last_result
