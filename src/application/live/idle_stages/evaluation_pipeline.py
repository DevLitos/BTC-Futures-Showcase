"""
Evaluation pipeline stages extracted from SignalEngine._handle_idle_state.

Stages (in order):
- stage_location_filter
- stage_decision_center
- stage_regime_fusion
- stage_micro_trend
- stage_filters_and_ltf
- stage_finalize_entry  (async)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.config.trading_config import (
    BiasStrengthConfig, TrendExpansionConfig, MicroTrendADXConfig,
    PullbackShortConfig, LocationFilterOverride,
)
from src.config.unified_constants import REGIME, SIZING, ADX
from src.domain.trading.edge_state_manager import EdgeState, EdgeType
from src.domain.market.regime import TrendPhase
from src.domain.risk.risk_engine import normalize_regime_mode
from src.shared.indicators import calculate_atr, calculate_adx, calculate_ema
from src.domain.trading.strategy.pullback_short import TradeType

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from .context import IdleCtx

logger = logging.getLogger(__name__)


def _ts_to_int(ts) -> int:
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0


# =========================================================================
# LOCATION FILTER
# =========================================================================

def stage_location_filter(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Location filter — macro veto. Returns True to abort."""
    volumes_1h = np.asarray(ctx.df_1h['volume'].to_numpy()) if 'volume' in ctx.df_1h.columns else np.ones(len(ctx.closes_1h))
    atr_1h = ctx.atr_1h

    adx_1h_vals, _ = calculate_adx(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, 14)
    adx_1h = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0

    # Normalize regime
    raw_regime = engine.regime_classifier.current_regime.value if engine.regime_classifier.current_regime else "RANGE"
    regime_for_location = normalize_regime_mode(raw_regime)

    htf_regime_mode = engine._htf_cache_regime_mode
    if htf_regime_mode in ('RANGE', 'WEAK_TREND', 'LOW_ADX'):
        if regime_for_location in ('TREND', 'BREAKOUT'):
            logger.info(f"⚠️ HTF ADX override: classifier={raw_regime} → {regime_for_location} but ADX={engine._htf_cache_adx:.1f} → forcing RANGE logic")
            regime_for_location = 'RANGE'

    engine._current_trade_regime = regime_for_location
    engine._current_trade_tags['htf_regime_mode'] = htf_regime_mode
    engine._current_trade_tags['htf_adx'] = engine._htf_cache_adx

    if ctx.bias is None:
        logger.warning("HTF passed but no bias set - skipping")
        return True

    location_cache_key = _ts_to_int(ctx.df_1h.iloc[-1].get('timestamp', 0))
    location_allowed, location_reason, engine._location_context = engine.location_filter.check(
        closes_1h=ctx.closes_1h,
        highs_1h=ctx.highs_1h,
        lows_1h=ctx.lows_1h,
        volumes_1h=volumes_1h,
        current_price=ctx.price,
        atr_1h=atr_1h,
        adx_1h=adx_1h,
        bias=ctx.bias,
        regime=regime_for_location,
        cache_key=location_cache_key
    )

    # Store location context in tags
    if engine._location_context:
        engine._current_trade_tags['location_pct_60d'] = engine._location_context.percentile_60d
        engine._current_trade_tags['location_headroom_atr'] = (
            engine._location_context.headroom_to_high_atr if ctx.bias == 'LONG'
            else engine._location_context.headroom_to_low_atr
        )
        engine._current_trade_tags['location_in_extreme'] = (
            engine._location_context.in_extreme_high or engine._location_context.in_extreme_low
        )
        engine._current_trade_tags['location_is_breakout'] = (
            engine._location_context.is_breakout_up or engine._location_context.is_breakout_down
        )
        # Intentionally empty — each subsystem handles its own forbidden state
        engine._current_trade_tags['location_penalty_long'] = engine._location_context.location_penalty_long
        engine._current_trade_tags['location_penalty_short'] = engine._location_context.location_penalty_short
        engine._current_trade_tags['location_size_cap_long'] = engine._location_context.size_cap_long
        engine._current_trade_tags['location_size_cap_short'] = engine._location_context.size_cap_short

    is_range_regime = regime_for_location in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']

    if not location_allowed:
        breakout_override = False
        if engine._location_context:
            is_breakout = engine._location_context.is_breakout_up or engine._location_context.is_breakout_down
            htf_adx_strong = adx_1h >= LocationFilterOverride.MIN_HTF_ADX

            has_bos = True
            if LocationFilterOverride.REQUIRE_BOS:
                if ctx.bias == 'LONG' and engine._location_context.is_breakout_up:
                    has_bos = ctx.price > np.max(ctx.highs_1h[-5:]) if len(ctx.highs_1h) >= 5 else False
                elif ctx.bias == 'SHORT' and engine._location_context.is_breakout_down:
                    has_bos = ctx.price < np.min(ctx.lows_1h[-5:]) if len(ctx.lows_1h) >= 5 else False

            if is_breakout and htf_adx_strong and has_bos:
                breakout_override = True
                engine._current_trade_tags['location_breakout_override'] = True
                engine._current_trade_tags['breakout_adx'] = adx_1h
                logger.info(f"🚀 LOCATION OVERRIDE: Breakout with ADX={adx_1h:.1f} (threshold={LocationFilterOverride.MIN_HTF_ADX})")

        if not breakout_override:
            if not is_range_regime:
                location_penalty = (
                    engine._location_context.location_penalty_long if ctx.bias == 'LONG'
                    else engine._location_context.location_penalty_short
                ) if engine._location_context else -1.0
                engine._current_trade_tags['location_penalty_applied'] = location_penalty
                logger.info(f"📍 LOCATION PENALTY (TREND): {location_reason} → penalty {location_penalty:.1f} (continuing)")
            else:
                if engine._last_location_state is None or engine._last_location_state != f'vetoed_{ctx.bias}':
                    logger.warning(f"📍 LOCATION VETO (RANGE): {location_reason}")
                    if engine._location_context:
                        logger.info(engine.location_filter.get_location_summary(engine._location_context))
                    engine._last_location_state = f'vetoed_{ctx.bias}'

                if ctx.bias == 'LONG' and PullbackShortConfig.ENABLED:
                    # Store flag for the caller to handle async pullback short
                    engine._pending_pullback_short = {
                        'current_price': ctx.price,
                        'df_1h': ctx.df_1h,
                        'candles_5m': engine.market_data.get_candles('5m'),
                        'htf_bias': ctx.bias,
                        'regime': regime_for_location,
                        'market_time': ctx.time,
                        'veto_reason': location_reason,
                    }
                return True
    else:
        engine._last_location_state = f'allowed_{ctx.bias}'
        if engine._location_context and engine._idle_eval_count % 60 == 0:
            logger.debug(engine.location_filter.get_location_summary(engine._location_context))

        if engine._location_context:
            location_penalty = (
                engine._location_context.location_penalty_long if ctx.bias == 'LONG'
                else engine._location_context.location_penalty_short
            )
            if location_penalty < 0:
                engine._current_trade_tags['location_penalty_applied'] = location_penalty
                logger.debug(f"📍 Location penalty (allowed): {location_penalty:.1f}")

    return False


# =========================================================================
# DECISION CENTER (TrendIntelligence)
# =========================================================================

def stage_decision_center(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Intelligent trade gating via TrendIntelligence. Returns True to abort."""
    ti_check_direction = ctx.bias
    if engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
        if engine._edge_manager.current_bos:
            ti_check_direction = engine._edge_manager.current_bos.direction
    elif engine._active_direction:
        ti_check_direction = engine._active_direction

    entry_recommendation = engine._trend_intelligence.get_entry_recommendation(ti_check_direction)
    engine._current_trade_tags['ti_allowed'] = entry_recommendation['allowed']
    engine._current_trade_tags['ti_phase'] = entry_recommendation['phase']
    engine._current_trade_tags['ti_direction_bias'] = entry_recommendation['direction_bias']
    engine._current_trade_tags['ti_entry_mode'] = entry_recommendation['entry_mode']
    engine._current_trade_tags['ti_size_mult'] = entry_recommendation['size_multiplier']
    engine._current_trade_tags['ti_score_adj'] = entry_recommendation['score_adjustment']
    engine._current_trade_tags['ti_confidence'] = entry_recommendation['confidence']

    if not entry_recommendation['allowed']:
        edge_state = engine._edge_manager.state
        edge_bos = engine._edge_manager.current_bos
        has_edge_override = (
            edge_state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
            edge_bos is not None
        )
        has_displacement_override = engine._displacement_bypass

        logger.debug(
            f"🔍 TI CHECK: allowed={entry_recommendation['allowed']} | "
            f"edge_state={edge_state.value if edge_state else 'None'} | "
            f"edge_bos={edge_bos is not None} | "
            f"has_edge_override={has_edge_override} | "
            f"displacement={has_displacement_override}"
        )

        ti_phase_check = entry_recommendation.get('phase', '')
        is_bad_phase_for_override = ti_phase_check in ['TREND_EXHAUSTION', 'DISTRIBUTION', 'TREND_DEATH']

        if (has_edge_override or has_displacement_override) and not is_bad_phase_for_override:
            override_type = "DISPLACEMENT" if has_displacement_override else (
                edge_bos.edge_type.value if edge_bos else "EDGE"
            )
            logger.info(
                f"🌊 TI OVERRIDE: {entry_recommendation['reason']} but {override_type} active → allowing"
            )
            engine._current_trade_tags['ti_overridden'] = True
        elif is_bad_phase_for_override:
            logger.warning(
                f"🚫 FIX #221: TI HARD BLOCK (BAD PHASE) | Phase={ti_phase_check} | "
                f"Edge present but phase is END-OF-TREND | NOT overriding"
            )
            engine._current_trade_tags['skip_reason'] = f"ti_hard_block_{ti_phase_check}"
            return True
        else:
            if entry_recommendation['confidence'] >= 0.6:
                logger.warning(
                    f"🧠 DECISION CENTER BLOCK: {entry_recommendation['reason']} | "
                    f"Confidence: {entry_recommendation['confidence']:.0%}"
                )
                engine._current_trade_tags['skip_reason'] = f"ti_block_{entry_recommendation['phase']}"
                return True
            else:
                logger.info(
                    f"📊 TI LOW CONFIDENCE ({entry_recommendation['confidence']:.0%}): "
                    f"{entry_recommendation['reason']} - proceeding with caution"
                )
                engine._current_trade_tags['ti_low_confidence_proceed'] = True

    # Apply TI multipliers
    engine._ti_size_multiplier = entry_recommendation['size_multiplier']
    engine._ti_score_adjustment = entry_recommendation['score_adjustment']

    # Additional intelligence
    ti_state = engine._trend_intelligence.state
    engine._current_trade_tags['ti_volume_trend'] = ti_state.volume_trend
    engine._current_trade_tags['ti_volume_confirms'] = ti_state.volume_on_moves
    engine._current_trade_tags['ti_hh_count'] = ti_state.higher_highs_count
    engine._current_trade_tags['ti_ll_count'] = ti_state.lower_lows_count
    engine._current_trade_tags['ti_adx_slope'] = ti_state.adx_slope
    engine._current_trade_tags['ti_adx_accel'] = ti_state.adx_acceleration

    if ti_state.volume_on_moves == "diverges":
        engine._ti_size_multiplier *= 0.7
        market_time = engine.market_data.get_market_time()
        if engine._last_vol_divergence_log is None or \
           (market_time - engine._last_vol_divergence_log).total_seconds() >= 120:
            logger.info(f"📉 VOLUME DIVERGENCE: Reducing size by 30%")
            engine._last_vol_divergence_log = market_time
        engine._current_trade_tags['ti_volume_penalty'] = True

    if ctx.bias == 'LONG' and ti_state.higher_highs_count >= 2:
        engine._ti_score_adjustment -= 0.5
        engine._current_trade_tags['ti_structure_aligned'] = True
    elif ctx.bias == 'SHORT' and ti_state.lower_lows_count >= 2:
        engine._ti_score_adjustment -= 0.5
        engine._current_trade_tags['ti_structure_aligned'] = True

    if ti_state.adx_acceleration > 1.0:
        engine._ti_size_multiplier = min(1.3, engine._ti_size_multiplier * 1.1)
        logger.debug(f"📈 ADX ACCELERATION +: Size boost (accel={ti_state.adx_acceleration:.2f})")
    elif ti_state.adx_acceleration < -1.0:
        engine._ti_size_multiplier *= 0.85
        logger.debug(f"📉 ADX DECELERATION: Size reduction (accel={ti_state.adx_acceleration:.2f})")

    if entry_recommendation['entry_mode'] == 'AGGRESSIVE':
        now = ctx.time
        last_agg_log = getattr(engine, '_last_ti_aggressive_log', None)
        if last_agg_log is None or (now - last_agg_log).total_seconds() >= 60:
            logger.info(
                f"🚀 TI AGGRESSIVE MODE: {entry_recommendation['phase']} | "
                f"Direction: {entry_recommendation['direction_bias']} | "
                f"Best entry opportunity!"
            )
            engine._last_ti_aggressive_log = now
    elif entry_recommendation['entry_mode'] == 'WAIT':
        logger.debug(f"⏳ TI WAIT MODE: {entry_recommendation['phase']} - proceed with reduced size")

    return False


# =========================================================================
# REGIME FUSION + EXPANSION / ANTI-FADE
# =========================================================================

def stage_regime_fusion(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """3-layer regime fusion + trend expansion + anti-fade guards. Returns True to abort."""
    # Determine micro signal quality
    has_displacement = engine._displacement_bypass or engine._strong_displacement
    has_structure = (
        engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
        engine._edge_manager.current_bos is not None
    )

    micro_confidence = 0.5
    if has_displacement:
        micro_confidence = 0.9
    elif has_structure:
        micro_confidence = 0.7

    engine._regime_fusion.update_micro(
        direction=ctx.bias,
        confidence=micro_confidence,
        has_displacement=has_displacement,
        has_structure=has_structure,
    )

    regime_decision = engine._regime_fusion.fuse(micro_direction=ctx.bias)
    engine._last_regime_decision = regime_decision

    engine._current_trade_tags['regime_alignment'] = regime_decision.alignment
    engine._current_trade_tags['regime_size_mult'] = regime_decision.size_mult
    engine._current_trade_tags['regime_rr_min'] = regime_decision.rr_min
    engine._current_trade_tags['regime_is_fade'] = regime_decision.is_fade
    engine._current_trade_tags['regime_blocked'] = regime_decision.is_blocked

    if regime_decision.is_blocked:
        logger.warning(
            f"☠️ REGIME FUSION BLOCK: {regime_decision.reason} | "
            f"macro={engine._regime_fusion.regime_state.regime.value}, "
            f"score={engine._regime_fusion.regime_state.macro_score:.2f}, "
            f"micro={ctx.bias}"
        )
        engine._current_trade_tags['skip_reason'] = 'regime_fade_block'
        return True

    if regime_decision.is_fade:
        should_log_fusion = (
            engine._last_countertrend_log is None or
            (ctx.time - engine._last_countertrend_log) > engine._log_rate_limit_interval
        )
        if should_log_fusion:
            engine._last_countertrend_log = ctx.time
            logger.info(
                f"🔀 REGIME FADE ALLOWED: {ctx.bias} vs macro={engine._regime_fusion.regime_state.regime.value} | "
                f"size={regime_decision.size_mult:.0%}, rr_min={regime_decision.rr_min:.1f}R | "
                f"override={regime_decision.override_type or 'none'}"
            )

    engine._current_trade_tags['is_counter_preference'] = regime_decision.is_fade
    engine._current_trade_tags['counter_pref_size_mult'] = regime_decision.size_mult
    engine._current_trade_tags['counter_pref_rr_add'] = regime_decision.rr_min - 1.0

    ct_rules = BiasStrengthConfig.get_countertrend_rules(engine._current_bias_strength, ctx.bias)
    is_countertrend = ct_rules['is_countertrend']
    engine._current_trade_tags['is_countertrend'] = is_countertrend
    engine._current_trade_tags['ct_size_mult'] = regime_decision.size_mult
    engine._current_trade_tags['ct_max_rr'] = ct_rules['max_rr']

    # Trend expansion detection
    candles_5m = ctx.candles_5m
    candles_1m = engine.market_data.get_candles('1m')
    df_5m_pre = pd.DataFrame(candles_5m) if candles_5m and len(candles_5m) >= 30 else None
    df_1m_pre = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 20 else None
    if df_5m_pre is not None and df_1m_pre is not None:
        atr_5m_pre = calculate_atr(df_5m_pre['high'].to_numpy(), df_5m_pre['low'].to_numpy(), df_5m_pre['close'].to_numpy(), 14)
        atr_5m_val = atr_5m_pre[-1] if len(atr_5m_pre) > 0 else ctx.price * 0.005

        expansion_result = engine._trend_expansion_detector.detect(
            df_5m_pre, df_1m_pre, ctx.price, atr_5m_val
        )
        expansion_detected = expansion_result.is_expanding
        expansion_criteria = expansion_result.criteria_met

        if expansion_detected and not engine._trend_expansion_active:
            engine._trend_expansion_active = True
            engine._trend_expansion_start_time = ctx.time
            anti_fade_minutes = TrendExpansionConfig.ANTI_FADE_CANDLES * 5
            engine._anti_fade_until = ctx.time + timedelta(minutes=anti_fade_minutes)
            logger.info(f"🚀 TREND EXPANSION MODE ACTIVATED - anti-fade for {anti_fade_minutes}min")
        elif not expansion_detected and engine._trend_expansion_active:
            if engine._trend_expansion_start_time and (ctx.time - engine._trend_expansion_start_time).total_seconds() > 1800:
                engine._trend_expansion_active = False
                engine._trend_expansion_start_time = None
                logger.info("📉 TREND EXPANSION MODE ENDED")

        engine._current_trade_tags['trend_expansion'] = engine._trend_expansion_active
        engine._current_trade_tags['expansion_criteria'] = expansion_criteria

    # Expansion opposite block
    if engine._expansion_mode and engine._expansion_direction:
        if ctx.bias != engine._expansion_direction:
            should_log = (
                engine._last_antifade_log is None or
                (ctx.time - engine._last_antifade_log) > engine._log_rate_limit_interval
            )
            if should_log:
                engine._last_antifade_log = ctx.time
                logger.warning(
                    f"🚫 EXPANSION BLOCK: Cannot enter {ctx.bias} during "
                    f"{engine._expansion_direction} expansion | HARD BLOCK"
                )
            engine._current_trade_tags['skip_reason'] = 'expansion_opposite_block'
            return True

    # Anti-fade block
    if engine._anti_fade_until and ctx.time < engine._anti_fade_until:
        if regime_decision.is_fade:
            remaining = (engine._anti_fade_until - ctx.time).total_seconds() / 60
            should_log_af = (
                engine._last_antifade_log is None or
                (ctx.time - engine._last_antifade_log) > engine._log_rate_limit_interval
            )
            if should_log_af:
                engine._last_antifade_log = ctx.time
                logger.warning(
                    f"🚫 ANTI-FADE BLOCK: Cannot fade during expansion "
                    f"({remaining:.0f}min remaining) | HARD BLOCK"
                )
            engine._current_trade_tags['skip_reason'] = 'anti_fade_expansion_block'
            return True

    return False


# =========================================================================
# MICRO TREND FILTER
# =========================================================================

def stage_micro_trend(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Micro trend (5m) filter + EMA distance context. Returns True to abort."""
    candles_1m_list = engine.market_data.get_candles('1m')
    candles_5m_list = engine.market_data.get_candles('5m')

    df_5m_micro = None
    df_1m_micro = None

    adx_1h_vals, _ = calculate_adx(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, 14)
    adx_1h = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0

    if candles_5m_list and len(candles_5m_list) >= 60:
        df_5m_micro = pd.DataFrame(candles_5m_list)
        df_1m_micro = pd.DataFrame(candles_1m_list) if candles_1m_list and len(candles_1m_list) >= 30 else None

        micro_allowed, micro_reason, engine._micro_trend_state = engine.micro_trend_filter.check(
            df_5m=df_5m_micro,
            df_1m=df_1m_micro,
            current_price=ctx.price,
            htf_bias=ctx.bias,
            market_time=ctx.time
        )

        if engine._micro_trend_state:
            engine._current_trade_tags['micro_trend'] = engine._micro_trend_state.micro_trend
            engine._current_trade_tags['micro_trend_strength'] = engine._micro_trend_state.strength
            engine._current_trade_tags['micro_ema20_5m'] = engine._micro_trend_state.ema20_5m
            engine._current_trade_tags['micro_ema50_5m'] = engine._micro_trend_state.ema50_5m
            engine._current_trade_tags['micro_rsi_5m'] = engine._micro_trend_state.rsi_5m
            engine._current_trade_tags['micro_score_adj'] = engine._micro_trend_state.score_adjustment
            engine._current_trade_tags['micro_has_ll'] = engine._micro_trend_state.has_lower_low

            # Regime-aware micro trend adjustment
            regime_for_micro = engine._current_trade_regime
            is_range_for_micro = regime_for_micro in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION', 'TREND_EXHAUSTION']

            if is_range_for_micro and engine._micro_trend_state.score_adjustment != 0:
                original_adj = engine._micro_trend_state.score_adjustment
                if original_adj > 0:
                    engine._micro_trend_state.score_adjustment = min(0.5, original_adj)
                else:
                    engine._micro_trend_state.score_adjustment = max(-0.3, original_adj)

                if original_adj != engine._micro_trend_state.score_adjustment:
                    logger.info(
                        f"📊 MICRO ADJ CAPPED (RANGE regime): {original_adj:+.1f} → "
                        f"{engine._micro_trend_state.score_adjustment:+.1f}"
                    )
                    engine._current_trade_tags['micro_score_adj'] = engine._micro_trend_state.score_adjustment
                    engine._current_trade_tags['micro_regime_capped'] = True

        if not micro_allowed:
            adx_override = False

            if adx_1h >= MicroTrendADXConfig.STRONG_TREND_ADX:
                adx_override = True
                engine._current_trade_tags['micro_adx_override'] = True
                engine._current_trade_tags['micro_htf_adx'] = adx_1h
                logger.info(f"⚡ MICRO TREND override: HTF ADX={adx_1h:.1f} >= {MicroTrendADXConfig.STRONG_TREND_ADX} (strong trend - NO penalty)")
                if engine._micro_trend_state:
                    engine._micro_trend_state.score_adjustment = 0.0
            elif adx_1h >= 15:
                adx_override = True
                engine._current_trade_tags['micro_adx_override'] = True
                engine._current_trade_tags['micro_htf_adx'] = adx_1h
                penalty_factor = (30 - adx_1h) / 30
                capped_penalty = max(-0.5, -penalty_factor)
                if engine._micro_trend_state:
                    engine._micro_trend_state.score_adjustment = capped_penalty
                logger.info(f"⚠️ MICRO TREND: HTF ADX={adx_1h:.1f} (mid-range) → capped penalty {capped_penalty:.2f}")
            else:
                logger.info(f"⛔ MICRO TREND: HTF ADX={adx_1h:.1f} < 15 (weak trend, full block)")

            if not adx_override:
                engine._current_trade_tags['micro_trend_veto'] = True
                engine._current_trade_tags['micro_veto_reason'] = micro_reason
                logger.debug(f"📊 [CONTEXT] Micro Trend opposes {ctx.bias} - no block (FIX #135)")
        else:
            engine._last_micro_state = f'allowed_{ctx.bias}'
            if engine._micro_trend_state and engine._micro_trend_state.score_adjustment != 0:
                logger.debug(f"📊 MICRO TREND: {engine._micro_trend_state.micro_trend} (strength={engine._micro_trend_state.strength:.0%}, score_adj={engine._micro_trend_state.score_adjustment:+.1f})")
    else:
        engine._micro_trend_state = None
        logger.debug("⚠️ MICRO TREND: Insufficient 5m data - skipping filter")

    # EMA distance context
    if df_5m_micro is not None and len(df_5m_micro) >= 50:
        closes_5m = df_5m_micro['close'].to_numpy()
        ema20_5m_val = calculate_ema(closes_5m, 20)[-1]
        ema50_5m_val = calculate_ema(closes_5m, 50)[-1]

        if not np.isnan(ema20_5m_val) and not np.isnan(ema50_5m_val) and ema20_5m_val > 0:
            distance_to_ema20 = (ctx.price - ema20_5m_val) / ema20_5m_val
            distance_to_ema50 = (ctx.price - ema50_5m_val) / ema50_5m_val
            engine._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
            engine._current_trade_tags['ema50_distance_pct'] = distance_to_ema50 * 100

    # Store for later stages
    engine._df_5m_micro = df_5m_micro
    engine._df_1m_micro = df_1m_micro

    return False


# =========================================================================
# FILTER PIPELINE + LTF TRIGGER
# =========================================================================

def stage_filters_and_ltf(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Unified filter pipeline + LTF trigger. Returns True to abort."""
    df_5m_micro = getattr(engine, '_df_5m_micro', None)
    df_1m_micro = getattr(engine, '_df_1m_micro', None)
    df_15m_proxy = df_5m_micro

    # Spread calculation
    spread = None
    if hasattr(engine, 'order_book') and engine.order_book:
        bid_price, ask_price, spread_pct = engine.order_book.get_spread()
        if bid_price and ask_price:
            engine.spread_filter.update_spread(bid_price, ask_price, ctx.price)
            spread = spread_pct
            if spread_pct:
                engine._last_spread_bps = spread_pct * 100

    valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
    anticipation_bypass = (
        engine._current_trade_tags.get('anticipation_signal') in valid_anticipation_signals and
        engine._current_trade_tags.get('anticipation_confidence', 0) >= 0.50
    )

    bias_flip_bypass = (
        engine._current_trade_tags.get('bias_invalidated', False) or
        engine._current_trade_tags.get('bias_inverted', False)
    )

    filter_decision = engine.filter_pipeline.evaluate(
        df_1m=df_1m_micro,
        df_5m=df_5m_micro,
        df_15m=df_15m_proxy,
        df_1h=ctx.df_1h,
        current_price=ctx.price,
        bias=ctx.bias,
        atr=ctx.atr_1m,
        spread=spread,
        market_time=ctx.time,
        displacement_bypass=engine._displacement_bypass,
        anticipation_bypass=anticipation_bypass,
        bias_flip_bypass=bias_flip_bypass
    )

    if filter_decision.tags:
        for key, value in filter_decision.tags.items():
            engine._current_trade_tags[f'fp_{key}'] = value

    if not filter_decision.allow:
        blocker = filter_decision.tags.get('blocked_by', 'unknown')
        if engine._last_fp_block is None or engine._last_fp_block != f'{blocker}_{ctx.bias}':
            logger.warning(f"[BLOCK] FILTER [{blocker}]: {filter_decision.reasons[0] if filter_decision.reasons else 'veto'}")
            engine._last_fp_block = f'{blocker}_{ctx.bias}'
        return True
    else:
        engine._last_fp_block = None
        if filter_decision.score_adjust != 0:
            logger.debug(f"📊 FILTER PIPELINE: score_adj={filter_decision.score_adjust:+.1f}")

    # ── LTF Trigger ──
    logger.debug("Evaluating LTF trigger (1m)...")
    candles_1m = engine.market_data.get_candles('1m')
    df_1m = pd.DataFrame(candles_1m)

    regime_value = engine.regime_classifier.current_regime.value if engine.regime_classifier.current_regime else None
    regime_conf = engine.regime_classifier.regime_confidence

    ltf_passed, ltf_reason = engine.ltf_filter.check(
        df_1m,
        ctx.bias,
        {},
        regime=regime_value,
        regime_confidence=regime_conf
    )

    ltf_info = engine.ltf_filter.get_last_ltf_info()
    if ltf_info:
        engine._current_trade_tags['ltf_bos'] = ltf_info.get('bos_confirmed', False)
        engine._current_trade_tags['ltf_ema_reclaim'] = ltf_info.get('ema_reclaim', False)
        engine._current_trade_tags['ltf_slope_positive'] = ltf_info.get('slope_positive', False)
        engine._current_trade_tags['ltf_confirmations'] = ltf_info.get('confirmations', 0)
        engine._current_trade_tags['ltf_ema_slope'] = ltf_info.get('ema_slope', 0.0)
        engine._current_trade_tags['rel_vol'] = ltf_info.get('rel_vol', 1.0)
        engine._current_trade_tags['volume_bucket'] = ltf_info.get('volume_bucket', 'unknown')

    if not ltf_passed:
        if engine._last_ltf_state != f'rejected_{ltf_reason}':
            logger.info(f"⛔ LTF filter rejected: {ltf_reason}")
            ema_9_ltf = df_1m['close'].ewm(span=9, adjust=False).mean().iloc[-1]
            ema_20_ltf = df_1m['close'].ewm(span=20, adjust=False).mean().iloc[-1]
            logger.info(f"   📊 Price: ${ctx.price:.2f} | EMA9: ${ema_9_ltf:.2f} | EMA20: ${ema_20_ltf:.2f}")
            logger.info(f"   🎯 Bias: {ctx.bias} | Impulse High: ${engine.impulse_high:.2f} | Low: ${engine.impulse_low:.2f}")
            engine._last_ltf_state = f'rejected_{ltf_reason}'
        return True

    # LTF confirmation gate — 1 confirmation is sufficient
    # BOS alone is a valid entry signal at 1m. Requiring 2/3 (BOS + EMA reclaim
    # + slope) blocked many valid entries during pullbacks when EMA is still
    # lagging or slope is temporarily negative.
    ltf_confs = ltf_info.get('confirmations', 0) if ltf_info else 0
    min_ltf_required = 1

    if ltf_confs < min_ltf_required:
        if engine._last_ltf_state != 'weak_ltf':
            logger.warning(f"⛔ LTF WEAK BLOCK: {ltf_confs}/3 confirmations < {min_ltf_required} minimum | Entry blocked")
            engine._last_ltf_state = 'weak_ltf'
        return True

    if engine._last_ltf_state != 'passed':
        logger.info(f"✅ LTF filter passed")
        engine._last_ltf_state = 'passed'

    return False


# =========================================================================
# FINALIZE + ENTRY (async)
# =========================================================================

async def stage_finalize_entry(engine: 'SignalEngine', ctx: IdleCtx) -> None:
    """
    Delta volume, direction validation, setup-complete log, and entry execution.
    This is the final stage — always continues to entry (no abort return).
    """
    # ── Delta Volume Confirmation ──
    delta_score_bonus = 0
    try:
        candles_1m = engine.market_data.get_candles('1m')
        df_1m = pd.DataFrame(candles_1m)
        if 'volume' in df_1m.columns and len(df_1m) >= 5:
            df_1m_recent = df_1m.tail(5).copy()
            df_1m_recent['is_green'] = df_1m_recent['close'] >= df_1m_recent['open']
            df_1m_recent['signed_vol'] = np.where(
                df_1m_recent['is_green'],
                df_1m_recent['volume'],
                -df_1m_recent['volume']
            )
            delta = df_1m_recent['signed_vol'].sum()
            avg_vol = df_1m['volume'].tail(20).mean()
            delta_ratio = delta / avg_vol if avg_vol > 0 else 0

            engine._current_trade_tags['volume_delta'] = delta
            engine._current_trade_tags['volume_delta_ratio'] = delta_ratio

            if ctx.bias == 'LONG' and delta_ratio > 0.5:
                delta_score_bonus = 0.5 if delta_ratio > 1.0 else 0.25
                logger.debug(f"📊 DELTA ALIGNED: +{delta_ratio:.2f}x avg vol supports LONG (+{delta_score_bonus} score)")
            elif ctx.bias == 'SHORT' and delta_ratio < -0.5:
                delta_score_bonus = 0.5 if delta_ratio < -1.0 else 0.25
                logger.debug(f"📊 DELTA ALIGNED: {delta_ratio:.2f}x avg vol supports SHORT (+{delta_score_bonus} score)")
            elif ctx.bias == 'LONG' and delta_ratio < -0.5:
                engine._current_trade_tags['delta_contradicts'] = True
                logger.debug(f"[INFO] Delta contradicts LONG (not penalized): {delta_ratio:.2f}x vol")
            elif ctx.bias == 'SHORT' and delta_ratio > 0.5:
                engine._current_trade_tags['delta_contradicts'] = True
                logger.debug(f"[INFO] Delta contradicts SHORT (not penalized): +{delta_ratio:.2f}x vol")
    except Exception as e:
        logger.debug(f"Delta volume calculation error: {e}")

    engine._current_trade_tags['delta_score_bonus'] = delta_score_bonus

    # ── Final direction validation ──
    edge_direction_final = engine._current_trade_tags.get('edge_direction')
    edge_type_final = engine._current_trade_tags.get('edge_type')

    if not edge_type_final:
        if engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if engine._edge_manager.current_bos:
                edge_direction_final = engine._edge_manager.current_bos.direction
                edge_type_final = engine._edge_manager.current_bos.edge_type
                if edge_type_final:
                    engine._current_trade_tags['edge_type'] = edge_type_final.value if hasattr(edge_type_final, 'value') else edge_type_final
                    engine._current_trade_tags['edge_direction'] = edge_direction_final
                    logger.debug(f"📊 Edge stored from manager: type={engine._current_trade_tags['edge_type']}, dir={edge_direction_final}")
    else:
        logger.debug(f"📊 Edge already in tags: type={edge_type_final}, dir={edge_direction_final}")

    # Sync bias with breakout edge
    is_breakout = (
        edge_type_final == EdgeType.BREAKOUT or
        edge_type_final == 'BREAKOUT' or
        (hasattr(edge_type_final, 'value') and edge_type_final.value == 'BREAKOUT')
    )
    if is_breakout and edge_direction_final:
        if ctx.bias != edge_direction_final:
            if engine._idle_eval_count % 180 == 1:
                logger.debug(
                    f"📊 Syncing local bias with edge: {ctx.bias} → {edge_direction_final}"
                )
            ctx.bias = edge_direction_final
            engine.current_bias = ctx.bias

    # ── Setup complete log ──
    ltf_confs = engine._current_trade_tags.get('ltf_confirmations', '?')
    now = ctx.time
    last_setup_log = getattr(engine, '_last_setup_complete_log', None)
    should_log_setup = last_setup_log is None or (now - last_setup_log).total_seconds() >= 60

    if engine._edge_context and not engine._edge_context.is_valid():
        if should_log_setup:
            logger.info(f"🎯 FILTERS PASSED - {ctx.bias} | HTF✓ MTF✓ LTF({ltf_confs}/3) | ⚠️ Edge context pending validation")
            engine._last_setup_complete_log = now
    else:
        edge_info = ""
        if edge_type_final and edge_direction_final:
            edge_type_str = edge_type_final.value if hasattr(edge_type_final, 'value') else edge_type_final
            edge_info = f" | Edge: {edge_type_str} {edge_direction_final}"
        if should_log_setup:
            logger.info(f"🎯 SETUP COMPLETE - {ctx.bias} | HTF✓ MTF✓ LTF({ltf_confs}/3){edge_info}")
            engine._last_setup_complete_log = now

    engine._current_trade_type = TradeType.LONG_MAIN if ctx.bias == 'LONG' else TradeType.SHORT_STRUCTURAL

    try:
        await engine._execute_entry(ctx.price, ctx.atr_1m, ctx.time)
    except Exception as e:
        logger.error(f"⛔ _execute_entry EXCEPTION: {e}")
        import traceback
        logger.error(traceback.format_exc())
