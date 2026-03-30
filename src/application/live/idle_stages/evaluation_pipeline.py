"""
Evaluation pipeline — REFACTORED from 6 stages to 3.

DELETED:
- stage_decision_center (TrendIntelligence) — redundant with direction_orchestrator + mode_handlers
- stage_regime_fusion (RegimeFusion) — redundant with direction checks upstream
- stage_micro_trend — just advisory noise, never actually blocked

Remaining stages (in order):
1. stage_location_filter  — Location gating (hard in RANGE, advisory in TREND)
2. stage_filters_and_ltf  — Trend expansion + filter pipeline + LTF trigger
3. stage_finalize_entry   — Delta volume + direction sync + entry execution (async)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.config.trading_config import (
    BiasStrengthConfig, TrendExpansionConfig,
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
# STAGE 1: LOCATION FILTER
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
# STAGE 2: FILTERS + TREND EXPANSION + LTF
# =========================================================================

def stage_filters_and_ltf(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Filters + trend expansion detection + LTF trigger. Returns True to abort."""
    # ── Fetch candle data (self-contained, no dependency on deleted stages) ──
    candles_5m_list = engine.market_data.get_candles('5m')
    candles_1m_list = engine.market_data.get_candles('1m')
    df_5m_micro = pd.DataFrame(candles_5m_list) if candles_5m_list and len(candles_5m_list) >= 30 else None
    df_1m_micro = pd.DataFrame(candles_1m_list) if candles_1m_list and len(candles_1m_list) >= 20 else None

    df_15m_proxy = None
    if df_5m_micro is not None and len(df_5m_micro) >= 45:
        df_15m_proxy = df_5m_micro.iloc[::3].copy().reset_index(drop=True)

    # ── Trend expansion detection (moved from deleted stage_regime_fusion) ──
    if df_5m_micro is not None and df_1m_micro is not None:
        atr_5m_vals = calculate_atr(
            df_5m_micro['high'].to_numpy(), df_5m_micro['low'].to_numpy(),
            df_5m_micro['close'].to_numpy(), 14
        )
        atr_5m_val = atr_5m_vals[-1] if len(atr_5m_vals) > 0 else ctx.price * 0.005

        expansion_result = engine._trend_expansion_detector.detect(
            df_5m_micro, df_1m_micro, ctx.price, atr_5m_val
        )
        if expansion_result.is_expanding and not engine._trend_expansion_active:
            engine._trend_expansion_active = True
            engine._trend_expansion_start_time = ctx.time
            anti_fade_minutes = TrendExpansionConfig.ANTI_FADE_CANDLES * 5
            engine._anti_fade_until = ctx.time + timedelta(minutes=anti_fade_minutes)
            logger.info(f"🚀 TREND EXPANSION ACTIVATED - anti-fade for {anti_fade_minutes}min")
        elif not expansion_result.is_expanding and engine._trend_expansion_active:
            if engine._trend_expansion_start_time and (ctx.time - engine._trend_expansion_start_time).total_seconds() > 1800:
                engine._trend_expansion_active = False
                engine._trend_expansion_start_time = None
                logger.info("📉 TREND EXPANSION ENDED")

        engine._current_trade_tags['trend_expansion'] = engine._trend_expansion_active
        engine._current_trade_tags['expansion_criteria'] = expansion_result.criteria_met

    # ── Expansion opposite block ──
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
                    f"{engine._expansion_direction} expansion"
                )
            engine._current_trade_tags['skip_reason'] = 'expansion_opposite_block'
            return True

    # ── Anti-fade block ──
    if engine._anti_fade_until and ctx.time < engine._anti_fade_until:
        if engine._expansion_direction and ctx.bias != engine._expansion_direction:
            remaining = (engine._anti_fade_until - ctx.time).total_seconds() / 60
            should_log_af = (
                engine._last_antifade_log is None or
                (ctx.time - engine._last_antifade_log) > engine._log_rate_limit_interval
            )
            if should_log_af:
                engine._last_antifade_log = ctx.time
                logger.warning(
                    f"🚫 ANTI-FADE BLOCK: Cannot fade during expansion "
                    f"({remaining:.0f}min remaining)"
                )
            engine._current_trade_tags['skip_reason'] = 'anti_fade_expansion_block'
            return True

    # ── Spread ──
    spread = None
    if hasattr(engine, 'order_book') and engine.order_book:
        bid_price, ask_price, spread_pct = engine.order_book.get_spread()
        if bid_price and ask_price:
            engine.spread_filter.update_spread(bid_price, ask_price, ctx.price)
            spread = spread_pct
            if spread_pct:
                engine._last_spread_bps = spread_pct * 100

    # ADX for trend-aware filter gating
    adx_1h_vals, _ = calculate_adx(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, 14)
    adx_1h_for_filters = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0

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
        adx_1h=adx_1h_for_filters,
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

    # Dynamic LTF gate by ADX — strong trends need 1 conf, ranging needs 2
    ltf_confs = ltf_info.get('confirmations', 0) if ltf_info else 0
    is_trending = adx_1h_for_filters >= 25.0
    min_ltf_required = 1 if is_trending else 2

    if ltf_confs < min_ltf_required:
        if engine._last_ltf_state != 'weak_ltf':
            logger.warning(f"⛔ LTF WEAK: {ltf_confs}/3 confirmations < {min_ltf_required} min | ADX={adx_1h_for_filters:.0f}")
            engine._last_ltf_state = 'weak_ltf'
        return True

    if engine._last_ltf_state != 'passed':
        logger.info(f"✅ LTF filter passed ({ltf_confs}/3, min={min_ltf_required})")
        engine._last_ltf_state = 'passed'

    return False


# =========================================================================
# STAGE 3: FINALIZE + ENTRY (async)
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

    # ── EXHAUSTION GATE — Blocks ALL entries at RSI extremes ──
    # Un scalper no entra en el techo de un rally ni en el piso de un dump.
    # Esto aplica a TODAS las entradas, incluyendo displacement bypass.
    from src.shared.indicators import calculate_rsi as _calc_rsi
    from src.shared.indicator_helpers import calculate_rsi_value as _calc_rsi_val
    candles_5m_ex = engine.market_data.get_candles('5m')
    df_5m_ex = pd.DataFrame(candles_5m_ex) if candles_5m_ex and len(candles_5m_ex) >= 20 else None

    if df_5m_ex is not None and len(df_5m_ex) >= 20:
        rsi_5m = _calc_rsi_val(df_5m_ex, 14)
        engine._current_trade_tags['rsi_5m_at_eval'] = rsi_5m

        RSI_OB = 70  # Overbought
        RSI_OS = 30  # Oversold
        if ctx.bias == 'LONG' and rsi_5m > RSI_OB:
            _last_exh_log = getattr(engine, '_last_exhaustion_log_time', None)
            if _last_exh_log is None or (ctx.time - _last_exh_log).total_seconds() >= 30:
                engine._last_exhaustion_log_time = ctx.time
                logger.warning(
                    f"🚫 EXHAUSTION GATE: RSI(14) 5m={rsi_5m:.1f} > {RSI_OB} → "
                    f"LONG at overbought, esperar pullback"
                )
            engine._current_trade_tags['exhaustion_blocked'] = True
            return  # Skip entry, don't abort pipeline permanently
        elif ctx.bias == 'SHORT' and rsi_5m < RSI_OS:
            _last_exh_log = getattr(engine, '_last_exhaustion_log_time', None)
            if _last_exh_log is None or (ctx.time - _last_exh_log).total_seconds() >= 30:
                engine._last_exhaustion_log_time = ctx.time
                logger.warning(
                    f"🚫 EXHAUSTION GATE: RSI(14) 5m={rsi_5m:.1f} < {RSI_OS} → "
                    f"SHORT at oversold, esperar pullback"
                )
            engine._current_trade_tags['exhaustion_blocked'] = True
            return  # Skip entry, don't abort pipeline permanently

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
