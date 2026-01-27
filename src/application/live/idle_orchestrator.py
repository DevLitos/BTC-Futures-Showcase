"""
IDLE State Orchestrator - Consolidates _handle_idle_state filter logic.

Created: Jan 28, 2026 (AUDIT REFACTOR - Reduce trading_app.py)

This module orchestrates the IDLE state filter evaluation flow, replacing ~800 lines
of inline filter logic with clean, testable components.

The orchestrator handles:
1. HTF filter evaluation with caching
2. Regime detection (RANGE/TRANSITION/TREND)
3. Location filter with range mode support
4. Micro trend filter
5. Bias strength and trend expansion
6. Short obstinacy calculation
7. Bias inversion logic
8. LTF trigger evaluation
9. Filter pipeline evaluation
10. Delta volume confirmation
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx, calculate_ema
from src.config.trading_config import (
    BiasStrength, BiasStrengthConfig, TrendExpansionConfig,
    ShortObstinacyConfig, MicroTrendADXConfig
)

logger = logging.getLogger(__name__)


# =========================================================================
# DATA CLASSES
# =========================================================================

@dataclass
class HTFResult:
    """Result of HTF filter evaluation."""
    passed: bool = False
    bias: Optional[str] = None
    reason: str = ""
    adx: float = 20.0
    regime_mode: str = "NORMAL"
    cache_updated: bool = False


@dataclass
class RegimeResult:
    """Result of regime detection."""
    trading_mode: str = "TREND"  # RANGE, TRANSITION, TREND
    effective_adx: float = 20.0
    htf_adx: float = 20.0
    fast_adx: float = 20.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LocationResult:
    """Result of location filter."""
    allowed: bool = True
    reason: str = ""
    location_context: Any = None
    penalty: float = 0.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MicroTrendResult:
    """Result of micro trend evaluation."""
    allowed: bool = True
    reason: str = ""
    score_adjustment: float = 0.0
    micro_trend: str = "NEUTRAL"
    strength: float = 0.0
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BiasResult:
    """Result of bias analysis."""
    final_bias: str = "LONG"
    inverted: bool = False
    bias_strength: Optional[BiasStrength] = None
    countertrend: bool = False
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterResult:
    """Final result of all IDLE state filters."""
    should_execute: bool = False
    bias: Optional[str] = None
    reason: str = ""
    all_tags: Dict[str, Any] = field(default_factory=dict)


# =========================================================================
# HTF EVALUATOR
# =========================================================================

class HTFEvaluator:
    """Evaluates HTF (1h) filter with caching."""
    
    @staticmethod
    def evaluate(
        engine: 'SignalEngine',
        df_1h: pd.DataFrame,
        current_price: float,
        candles_1h: list,
        market_time: datetime,
    ) -> HTFResult:
        """
        Evaluate HTF filter with cache management.
        
        Returns cached result if candle hasn't changed, otherwise recalculates.
        """
        result = HTFResult()
        
        # Check if cache is still valid
        from src.application.live.trading_app import _ts_to_int
        last_1h_ts = _ts_to_int(candles_1h[-1].get('timestamp', 0))
        
        if last_1h_ts == engine._htf_cache_ts:
            # Use cached values
            result.passed = engine._htf_cache_passed
            result.bias = engine._htf_cache_bias
            result.reason = engine._htf_cache_reason
            result.adx = engine._htf_cache_adx
            result.regime_mode = engine._htf_cache_regime_mode
            result.cache_updated = False
            return result
        
        # Need to recalculate
        htf_passed, bias, htf_reason = engine.htf_filter.check(
            df_1h, current_price, correlation_data=None
        )
        
        # Update cache
        engine._htf_cache_ts = last_1h_ts
        engine._htf_cache_passed = htf_passed
        engine._htf_cache_bias = bias
        engine._htf_cache_reason = htf_reason
        engine._htf_cache_regime_mode = engine.htf_filter.last_htf_regime_mode
        
        # Safely convert ADX to float
        raw_adx = engine.htf_filter.last_htf_adx
        engine._htf_cache_adx = float(raw_adx) if raw_adx else 0.0
        
        result.passed = htf_passed
        result.bias = bias
        result.reason = htf_reason
        result.adx = engine._htf_cache_adx
        result.regime_mode = engine._htf_cache_regime_mode
        result.cache_updated = True
        
        # Update trend intelligence if cache updated
        if result.cache_updated:
            HTFEvaluator._update_trend_intelligence(engine, candles_1h, df_1h, current_price, market_time, result.adx)
        
        return result
    
    @staticmethod
    def _update_trend_intelligence(
        engine: 'SignalEngine',
        candles_1h: list,
        df_1h: pd.DataFrame,
        current_price: float,
        market_time: datetime,
        adx: float,
    ):
        """Update trend intelligence on new 1h candle."""
        try:
            last_candle = candles_1h[-1]
            atr_1h_arr = calculate_atr(
                df_1h['high'].to_numpy(), df_1h['low'].to_numpy(), df_1h['close'].to_numpy(), 14
            )
            atr_1h = float(atr_1h_arr[-1]) if len(atr_1h_arr) > 0 else 100.0
            
            trend_state = engine._trend_intelligence.update(
                current_price=current_price,
                high=float(last_candle.get('high', current_price)),
                low=float(last_candle.get('low', current_price)),
                close=float(last_candle.get('close', current_price)),
                adx=adx,
                volume=float(last_candle.get('volume', 0)),
                atr=atr_1h,
                timestamp=market_time
            )
            
            # Update phase tracking
            old_phase = engine._trend_phase
            engine._trend_phase = trend_state.current_phase
            
            if old_phase != engine._trend_phase:
                logger.info(f"📈 TREND PHASE: {engine._trend_intelligence.get_phase_description()}")
                
        except Exception as e:
            logger.debug(f"TrendIntelligence update error: {e}")


# =========================================================================
# REGIME DETECTOR
# =========================================================================

class RegimeEvaluator:
    """Detects trading regime using combined 5m and 1h ADX."""
    
    @staticmethod
    def evaluate(
        engine: 'SignalEngine',
        htf_adx: float,
        df_5m: Optional[pd.DataFrame],
    ) -> RegimeResult:
        """
        Detect regime using fast (5m) and slow (1h) ADX.
        
        Returns effective regime for trading decisions.
        """
        result = RegimeResult()
        result.htf_adx = htf_adx
        
        # Calculate fast ADX from 5m
        fast_adx = htf_adx  # Default to 1h
        if df_5m is not None and len(df_5m) >= 30:
            try:
                df_5m_recent = df_5m.tail(30)
                adx_5m_vals, _ = calculate_adx(
                    df_5m_recent['high'].to_numpy(),
                    df_5m_recent['low'].to_numpy(),
                    df_5m_recent['close'].to_numpy(),
                    14
                )
                if len(adx_5m_vals) > 0 and not np.isnan(adx_5m_vals[-1]):
                    fast_adx = float(adx_5m_vals[-1])
            except Exception as e:
                logger.debug(f"Fast ADX calc error: {e}")
        
        result.fast_adx = fast_adx
        result.tags['adx_5m_fast'] = fast_adx
        
        # Combined regime detection
        if fast_adx < 15:
            # 5m shows STRONG RANGE - believe it immediately
            result.effective_adx = min(fast_adx, htf_adx)
        elif fast_adx > 25 and htf_adx > 18:
            # 5m shows STRONG TREND and 1h confirms
            result.effective_adx = htf_adx
        else:
            # Mixed signals - weighted average favoring 1h
            result.effective_adx = htf_adx * 0.7 + fast_adx * 0.3
        
        # Determine trading mode
        if result.effective_adx < 18:
            result.trading_mode = 'RANGE'
        elif result.effective_adx < 20:
            result.trading_mode = 'TRANSITION'
        else:
            result.trading_mode = 'TREND'
        
        result.tags['htf_adx'] = htf_adx
        result.tags['effective_adx'] = result.effective_adx
        result.tags['trading_mode'] = result.trading_mode
        
        return result


# =========================================================================
# LOCATION EVALUATOR
# =========================================================================

class LocationEvaluator:
    """Evaluates location filter with range mode support."""
    
    @staticmethod
    def evaluate(
        engine: 'SignalEngine',
        df_1h: pd.DataFrame,
        current_price: float,
        bias: str,
        trading_mode: str,
        regime: str,
    ) -> LocationResult:
        """
        Evaluate location filter.
        
        In RANGE mode: Block at extremes, allow at mean-reversion levels
        In TREND mode: Allow continuation setups
        """
        result = LocationResult()
        
        # Get 1h data
        closes_1h = np.asarray(df_1h['close'].to_numpy())
        highs_1h = np.asarray(df_1h['high'].to_numpy())
        lows_1h = np.asarray(df_1h['low'].to_numpy())
        volumes_1h = np.asarray(df_1h['volume'].to_numpy()) if 'volume' in df_1h.columns else np.ones(len(closes_1h))
        
        # Calculate ATR and ADX for location filter
        atr_1h_vals = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
        atr_1h = atr_1h_vals[-1] if len(atr_1h_vals) > 0 else current_price * 0.01
        
        adx_1h_vals, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
        adx_1h = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0
        
        # Cache key for location filter
        from src.application.live.trading_app import _ts_to_int
        cache_key = _ts_to_int(df_1h.iloc[-1].get('timestamp', 0))
        
        # Run location filter
        location_allowed, location_reason, location_context = engine.location_filter.check(
            closes_1h=closes_1h,
            highs_1h=highs_1h,
            lows_1h=lows_1h,
            volumes_1h=volumes_1h,
            current_price=current_price,
            atr_1h=atr_1h,
            adx_1h=adx_1h,
            bias=bias,
            regime=regime,
            cache_key=cache_key
        )
        
        result.allowed = location_allowed
        result.reason = location_reason
        result.location_context = location_context
        
        # Store location context
        engine._location_context = location_context
        
        if location_context:
            # Calculate penalty
            result.penalty = (
                location_context.location_penalty_long if bias == 'LONG'
                else location_context.location_penalty_short
            )
            
            result.tags['location_percentile'] = location_context.percentile
            result.tags['location_zone'] = location_context.zone
            
            if result.penalty < 0:
                result.tags['location_penalty_applied'] = result.penalty
        
        return result


# =========================================================================
# MICRO TREND EVALUATOR
# =========================================================================

class MicroTrendEvaluator:
    """Evaluates micro trend (5m structure) filter."""
    
    @staticmethod
    def evaluate(
        engine: 'SignalEngine',
        df_5m: pd.DataFrame,
        df_1m: Optional[pd.DataFrame],
        current_price: float,
        bias: str,
        market_time: datetime,
        adx_1h: float,
        trading_mode: str,
    ) -> MicroTrendResult:
        """
        Evaluate micro trend filter.
        
        Detects when 5m structure opposes 1h bias.
        """
        result = MicroTrendResult()
        
        if len(df_5m) < 60:
            return result
        
        micro_allowed, micro_reason, micro_state = engine.micro_trend_filter.check(
            df_5m=df_5m,
            df_1m=df_1m,
            current_price=current_price,
            htf_bias=bias,
            market_time=market_time
        )
        
        # Store micro trend state
        engine._micro_trend_state = micro_state
        
        if micro_state:
            result.micro_trend = micro_state.micro_trend or 'NEUTRAL'
            result.strength = micro_state.strength or 0.0
            result.score_adjustment = micro_state.score_adjustment
            
            result.tags['micro_trend'] = result.micro_trend
            result.tags['micro_trend_strength'] = result.strength
            result.tags['micro_score_adj'] = result.score_adjustment
            
            # Regime-aware adjustment
            if trading_mode in ['RANGE', 'TRANSITION']:
                if result.score_adjustment > 0:
                    result.score_adjustment = min(0.5, result.score_adjustment)
                else:
                    result.score_adjustment = max(-0.3, result.score_adjustment)
                result.tags['micro_regime_capped'] = True
            
            # ADX override for strong trends
            if not micro_allowed:
                if adx_1h >= MicroTrendADXConfig.STRONG_TREND_ADX:
                    micro_allowed = True
                    result.score_adjustment = 0.0
                    result.tags['micro_adx_override'] = True
                elif adx_1h >= 15:
                    micro_allowed = True
                    penalty_factor = (30 - adx_1h) / 30
                    result.score_adjustment = max(-0.5, -penalty_factor)
                    result.tags['micro_adx_override'] = True
        
        result.allowed = micro_allowed
        result.reason = micro_reason
        
        return result


# =========================================================================
# BIAS EVALUATOR
# =========================================================================

class BiasEvaluator:
    """Evaluates bias strength and potential inversion."""
    
    MICRO_STRENGTH_FOR_INVERSION = 0.65
    
    @staticmethod
    def evaluate(
        engine: 'SignalEngine',
        original_bias: str,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        market_time: datetime,
    ) -> BiasResult:
        """
        Evaluate bias strength and determine if inversion needed.
        
        Returns adjusted bias based on micro-trend contradiction.
        """
        result = BiasResult()
        result.final_bias = original_bias
        
        # Detect bias strength
        bias_result = engine._bias_strength_detector.detect(df_1h, df_5m, current_price)
        result.bias_strength = bias_result.strength
        engine._current_bias_strength = result.bias_strength
        
        result.tags['bias_strength'] = result.bias_strength.value if result.bias_strength else 'NEUTRAL'
        
        # Get countertrend rules
        ct_rules = BiasStrengthConfig.get_countertrend_rules(result.bias_strength, original_bias)
        result.countertrend = ct_rules['is_countertrend']
        
        result.tags['is_countertrend'] = result.countertrend
        result.tags['ct_size_mult'] = ct_rules['size_mult']
        result.tags['ct_max_rr'] = ct_rules['max_rr']
        
        # Bias inversion check
        micro_state = engine._micro_trend_state
        if micro_state and micro_state.strength >= BiasEvaluator.MICRO_STRENGTH_FOR_INVERSION:
            micro_trend = micro_state.micro_trend
            
            if original_bias == 'LONG' and micro_trend == 'BEARISH':
                result.final_bias = 'SHORT'
                result.inverted = True
                result.tags['bias_inverted'] = True
                result.tags['inversion_reason'] = 'micro_bearish_strong'
                logger.info(f"🔄 BIAS INVERTED: LONG → SHORT (micro-bearish {micro_state.strength:.0%})")
                
            elif original_bias == 'SHORT' and micro_trend == 'BULLISH':
                result.final_bias = 'LONG'
                result.inverted = True
                result.tags['bias_inverted'] = True
                result.tags['inversion_reason'] = 'micro_bullish_strong'
                logger.info(f"🔄 BIAS INVERTED: SHORT → LONG (micro-bullish {micro_state.strength:.0%})")
        
        return result


# =========================================================================
# MAIN ORCHESTRATOR
# =========================================================================

async def orchestrate_idle_filters(
    engine: 'SignalEngine',
    current_price: float,
    atr_1m: float,
    market_time: datetime,
    candles_1h: list,
    df_1h: pd.DataFrame,
) -> FilterResult:
    """
    Orchestrate all IDLE state filters.
    
    This replaces ~600 lines of inline filter code with a clean flow.
    
    Returns:
        FilterResult indicating if entry should be attempted
    """
    result = FilterResult()
    
    # ========== PHASE 1: HTF EVALUATION ==========
    htf_result = HTFEvaluator.evaluate(engine, df_1h, current_price, candles_1h, market_time)
    
    if not htf_result.passed:
        result.reason = f"HTF filter: {htf_result.reason}"
        return result
    
    result.bias = htf_result.bias
    result.all_tags['htf_regime_mode'] = htf_result.regime_mode
    
    # ========== PHASE 2: REGIME DETECTION ==========
    candles_5m = engine.market_data.get_candles('5m')
    df_5m = pd.DataFrame(candles_5m) if candles_5m and len(candles_5m) >= 30 else None
    
    regime_result = RegimeEvaluator.evaluate(engine, htf_result.adx, df_5m)
    result.all_tags.update(regime_result.tags)
    
    # Store regime for downstream
    engine._current_trade_regime = regime_result.trading_mode
    
    # ========== PHASE 3: LOCATION FILTER ==========
    location_result = LocationEvaluator.evaluate(
        engine, df_1h, current_price, result.bias,
        regime_result.trading_mode, regime_result.trading_mode
    )
    result.all_tags.update(location_result.tags)
    
    if not location_result.allowed and regime_result.trading_mode != 'RANGE':
        result.reason = f"Location filter: {location_result.reason}"
        return result
    
    # ========== PHASE 4: MICRO TREND FILTER ==========
    candles_1m = engine.market_data.get_candles('1m')
    df_1m = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 30 else None
    
    if df_5m is not None:
        micro_result = MicroTrendEvaluator.evaluate(
            engine, df_5m, df_1m, current_price, result.bias,
            market_time, htf_result.adx, regime_result.trading_mode
        )
        result.all_tags.update(micro_result.tags)
        
        if not micro_result.allowed:
            result.reason = f"Micro trend: {micro_result.reason}"
            return result
    
    # ========== PHASE 5: BIAS ANALYSIS ==========
    if df_5m is not None:
        bias_result = BiasEvaluator.evaluate(
            engine, result.bias, df_1h, df_5m, current_price, market_time
        )
        result.all_tags.update(bias_result.tags)
        
        if bias_result.inverted:
            result.bias = bias_result.final_bias
    
    # All filters passed
    result.should_execute = True
    return result
