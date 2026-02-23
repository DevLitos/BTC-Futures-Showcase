"""
Regime Handlers Module - Extracted from _handle_idle_state.

🔥 EXTRACTED Feb 04, 2026

This module contains the regime score calculation and trading mode determination,
extracted from trading_app.py to reduce its size and complexity.

Functions:
- calculate_regime_score: Computes the continuous regime score
- determine_trading_mode: Maps score to RANGE/TRANSITION/TREND
- calculate_intraday_edge_data: Collects data for intraday edge detection
"""

import logging
import numpy as np
import pandas as pd
from typing import TYPE_CHECKING, Dict, Any, Tuple
from dataclasses import dataclass

from src.shared.indicators import calculate_atr
from src.domain.market.regime import TrendPhase
from src.config.unified_constants import REGIME, ADX, SIZING

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

logger = logging.getLogger(__name__)


# Phase scores for regime calculation
PHASE_SCORES = {
    'RANGE_BOUND': -0.8,      # Claramente rango
    'ACCUMULATION': -0.3,     # Posible reversión
    'TREND_BIRTH': 0.5,       # Inicio de tendencia
    'ACCELERATION': 0.9,      # Tendencia fuerte
    'MATURE': 0.6,            # Tendencia estable
    'EXHAUSTION': 0.0,        # Indeterminado
    'TREND_DEATH': -0.5,      # Fin de tendencia
}

# Weights for regime score calculation (sum to 1.0)
W_ADX = 0.40       # ADX contributes 40% (objective, less lagging)
W_PHASE = 0.35     # TrendPhase contributes 35% (more info but subjective)
W_VOLATILITY = 0.25 # Volatility contributes 25%


@dataclass
class RegimeScoreResult:
    """Result of regime score calculation."""
    score: float
    trading_mode: str
    adx_normalized: float
    phase_normalized: float
    vol_normalized: float
    displacement_bonus: float
    adx_suggested_mode: str
    trend_phase_str: str
    
    def to_tags(self) -> Dict[str, Any]:
        """Convert to trade tags dict."""
        tags = {
            'regime_score': round(self.score, 3),
            'trading_mode': self.trading_mode,
            'regime_adx_component': round(self.adx_normalized, 3),
            'regime_phase_component': round(self.phase_normalized, 3),
            'regime_vol_component': round(self.vol_normalized, 3),
            'adx_suggested_mode': self.adx_suggested_mode,
            'trend_phase': self.trend_phase_str,
        }
        if self.displacement_bonus > 0:
            tags['regime_displacement_bonus'] = self.displacement_bonus
        return tags


def calculate_regime_score(
    effective_adx: float,
    trend_phase: TrendPhase,
    volatility_ratio: float,
    has_displacement: bool = False
) -> RegimeScoreResult:
    """
    Calculate continuous regime score from multiple inputs.
    
    🔥 EXTRACTED from _handle_idle_state (Feb 04, 2026)
    
    RegimeScore = w_adx * ADX_norm + w_phase * Phase_score + w_vol * Vol_norm
    
    Args:
        effective_adx: Effective ADX value (min of fast_adx, htf_adx)
        trend_phase: Current TrendPhase from TrendIntelligence
        volatility_ratio: Current volatility ratio
        has_displacement: Whether displacement is active
        
    Returns:
        RegimeScoreResult with score and components
    """
    # === COMPONENT 1: ADX normalized [0, 1] ===
    ADX_MIN, ADX_MAX = 15.0, 45.0
    adx_normalized = (effective_adx - ADX_MIN) / (ADX_MAX - ADX_MIN)
    adx_normalized = max(0.0, min(1.0, adx_normalized))
    
    # === COMPONENT 2: TrendPhase score [-1, 1] → [0, 1] ===
    trend_phase_str = trend_phase.value if hasattr(trend_phase, 'value') else str(trend_phase)
    phase_score = PHASE_SCORES.get(trend_phase_str, 0.0)
    phase_normalized = (phase_score + 1.0) / 2.0  # -1→0, 0→0.5, 1→1
    
    # === COMPONENT 3: Volatility normalized [0, 1] ===
    VOL_MIN, VOL_MAX = 0.7, 1.5
    vol_normalized = (volatility_ratio - VOL_MIN) / (VOL_MAX - VOL_MIN)
    vol_normalized = max(0.0, min(1.0, vol_normalized))
    
    # === REGIME SCORE CALCULATION ===
    regime_score = (W_ADX * adx_normalized + 
                    W_PHASE * phase_normalized + 
                    W_VOLATILITY * vol_normalized)
    
    # === DISPLACEMENT BONUS ===
    # El bonus se registra para sizing/confidence pero NO afecta la determinación
    # del modo (RANGE/TRANSITION/TREND). Antes, +0.25 podía empujar RANGE (0.30)
    # a TRANSITION (0.55), anulando las guards de range floor/ceiling.
    displacement_bonus = 0.0
    if has_displacement:
        displacement_bonus = 0.25
    
    # === DETERMINE MODE FROM SCORE (sin displacement bonus) ===
    if regime_score < REGIME.RANGE_MAX:
        trading_mode = 'RANGE'
    elif regime_score < REGIME.TRANSITION_MAX:
        trading_mode = 'TRANSITION'
    else:
        trading_mode = 'TREND'
    
    # Aplicar bonus DESPUÉS de determinar el modo, solo para scoring
    if displacement_bonus > 0:
        regime_score = min(1.0, regime_score + displacement_bonus)
    
    # === ADX SUGGESTED MODE (legacy comparison) ===
    if effective_adx < ADX.RANGE_MAX:
        adx_suggested_mode = 'RANGE'
    elif effective_adx < ADX.TREND_MIN:
        adx_suggested_mode = 'TRANSITION'
    else:
        adx_suggested_mode = 'TREND'
    
    return RegimeScoreResult(
        score=regime_score,
        trading_mode=trading_mode,
        adx_normalized=adx_normalized,
        phase_normalized=phase_normalized,
        vol_normalized=vol_normalized,
        displacement_bonus=displacement_bonus,
        adx_suggested_mode=adx_suggested_mode,
        trend_phase_str=trend_phase_str,
    )


def calculate_intraday_edge_data(
    candles_1m: list,
    current_trade_tags: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Calculate data needed for INTRADAY edge detection.
    
    🔥 EXTRACTED from _handle_idle_state (Feb 04, 2026)
    
    Args:
        candles_1m: List of 1m candles
        current_trade_tags: Dict to update with calculated values
        
    Returns:
        Updated trade tags dict
    """
    if not candles_1m or len(candles_1m) < 30:
        # Set defaults
        current_trade_tags.setdefault('atr_1m', 100.0)
        current_trade_tags.setdefault('atr_1m_avg', 100.0)
        current_trade_tags.setdefault('volume_current', 1.0)
        current_trade_tags.setdefault('volume_average', 1.0)
        current_trade_tags.setdefault('volume_ratio', 1.0)
        current_trade_tags.setdefault('swing_direction', 'NEUTRAL')
        current_trade_tags.setdefault('swing_count', 0)
        return current_trade_tags
    
    try:
        df_1m = pd.DataFrame(candles_1m[-30:])
        
        # ATR current vs average (for expansion ratio)
        atr_1m_arr = calculate_atr(
            df_1m['high'].to_numpy(),
            df_1m['low'].to_numpy(),
            df_1m['close'].to_numpy(),
            14
        )
        
        if len(atr_1m_arr) >= 10:
            atr_1m_current = float(atr_1m_arr[-1])
            
            # Proper bounds check for slice
            if len(atr_1m_arr) >= 21:
                atr_1m_avg = float(np.mean(atr_1m_arr[-20:-1]))
            else:
                atr_1m_avg = float(np.mean(atr_1m_arr[:-1])) if len(atr_1m_arr) > 1 else atr_1m_current
            
            current_trade_tags['atr_1m'] = atr_1m_current
            current_trade_tags['atr_1m_avg'] = atr_1m_avg
            current_trade_tags['atr_expansion_ratio'] = atr_1m_current / atr_1m_avg if atr_1m_avg > 0 else 1.0
        
        # Volume current vs average
        if 'volume' in df_1m.columns:
            vol_series = df_1m['volume']
            # Skip last candle if volume=0 (incomplete/just-started candle)
            vol_current = float(vol_series.iloc[-1])
            if vol_current == 0 and len(vol_series) > 2:
                vol_current = float(vol_series.iloc[-2])
            vol_avg = float(vol_series.iloc[:-1].mean()) if len(vol_series) > 1 else vol_current
            current_trade_tags['volume_current'] = vol_current
            current_trade_tags['volume_average'] = vol_avg
            current_trade_tags['volume_ratio'] = vol_current / vol_avg if vol_avg > 0 else 1.0
        
        # Swing direction (HH/HL = BULLISH, LL/LH = BEARISH)
        highs = df_1m['high'].to_numpy()[-10:]
        lows = df_1m['low'].to_numpy()[-10:]
        
        hh_count = 0
        ll_count = 0
        for i in range(2, len(highs)):
            if highs[i] > highs[i-1] and highs[i-1] > highs[i-2]:
                hh_count += 1
            if lows[i] > lows[i-1] and lows[i-1] > lows[i-2]:
                hh_count += 1  # Higher lows also bullish
            if lows[i] < lows[i-1] and lows[i-1] < lows[i-2]:
                ll_count += 1
            if highs[i] < highs[i-1] and highs[i-1] < highs[i-2]:
                ll_count += 1  # Lower highs also bearish
        
        if hh_count > ll_count + 1:
            swing_dir = 'BULLISH'
            swing_count = hh_count
        elif ll_count > hh_count + 1:
            swing_dir = 'BEARISH'
            swing_count = ll_count
        else:
            swing_dir = 'NEUTRAL'
            swing_count = 0
        
        current_trade_tags['swing_direction'] = swing_dir
        current_trade_tags['swing_count'] = swing_count
                
    except Exception as e:
        logger.debug(f"Error calculating intraday edge data: {e}")
        # Set defaults
        current_trade_tags.setdefault('atr_1m', 100.0)
        current_trade_tags.setdefault('atr_1m_avg', 100.0)
        current_trade_tags.setdefault('volume_current', 1.0)
        current_trade_tags.setdefault('volume_average', 1.0)
        current_trade_tags.setdefault('swing_direction', 'NEUTRAL')
        current_trade_tags.setdefault('swing_count', 0)
    
    return current_trade_tags


def log_regime_score_if_discrepancy(
    regime_result: RegimeScoreResult,
    effective_adx: float,
    vol_ratio: float,
    eval_count: int
) -> None:
    """Log when regime score differs from ADX-only mode."""
    # 🔥 FIX #122: Throttle de 60 → 180 ticks para reducir spam
    if regime_result.trading_mode != regime_result.adx_suggested_mode and eval_count % 180 == 1:
        logger.info(
            f"📊 REGIME SCORE: {regime_result.score:.2f} → {regime_result.trading_mode} | "
            f"ADX={effective_adx:.1f}({regime_result.adx_normalized:.2f}) "
            f"Phase={regime_result.trend_phase_str}({PHASE_SCORES.get(regime_result.trend_phase_str, 0):+.1f}) "
            f"Vol={vol_ratio:.2f}({regime_result.vol_normalized:.2f}) | "
            f"ADX_legacy={regime_result.adx_suggested_mode}"
        )
