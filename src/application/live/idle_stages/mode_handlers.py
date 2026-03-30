"""
Trading mode handlers extracted from SignalEngine._handle_idle_state.

Each function returns True if the idle pipeline should abort.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.config.trading_config import BiasStrengthConfig
from src.config.unified_constants import REGIME, SIZING, ADX
from src.domain.trading.edge_state_manager import EdgeState, EdgeType
from src.shared.indicators import calculate_atr, calculate_adx

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from .context import IdleCtx

logger = logging.getLogger(__name__)


def _ts_to_int(ts) -> int:
    """Convert any timestamp to int (epoch ms)."""
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0


# =========================================================================
# RANGE MODE HANDLER
# =========================================================================

def stage_range_mode(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """
    RANGE mode rules (ADX < 18).
    Returns True if the pipeline should abort.
    """
    # Micro-range detection: low volatility → size penalty, NOT hard block
    # A RANGE market by definition has small moves; blocking them entirely
    # creates a self-reinforcing dead zone. Instead, reduce size.
    skip_range_analysis = False
    if not engine._displacement_bypass and not engine._expansion_mode:
        try:
            _candles_micro = engine.market_data.get_candles('1m')
            if _candles_micro and len(_candles_micro) >= 30:
                _n_check = min(60, len(_candles_micro))
                _recent_candles = _candles_micro[-_n_check:]
                _micro_high = max(float(c.get('high', 0) or 0) for c in _recent_candles)
                _micro_low = min(float(c.get('low', 1e9) or 1e9) for c in _recent_candles)
                _micro_range = _micro_high - _micro_low
                _micro_range_pct = (_micro_range / ctx.price) * 100 if ctx.price > 0 else 0
                MICRO_RANGE_PENALTY_PCT = 0.10  # Below this → half size
                if _micro_range_pct < MICRO_RANGE_PENALTY_PCT:
                    engine._current_trade_tags['micro_range_penalty'] = 0.50
                    if engine._idle_eval_count % 300 == 1:
                        logger.info(
                            f"⚠️ MICRO-RANGE PENALTY: Last 60 candles range={_micro_range_pct:.2f}% "
                            f"< {MICRO_RANGE_PENALTY_PCT}% | Size reduced 50% (not blocked)"
                        )
        except Exception:
            pass

    # Displacement edge kills range analysis
    has_displacement_edge_active = (
        hasattr(engine, '_edge_manager') and
        engine._edge_manager.state == EdgeState.EDGE_ACTIVE and
        engine._edge_manager.current_bos is not None and
        engine._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT
    )
    if has_displacement_edge_active:
        skip_range_analysis = True
        if engine._idle_eval_count % 60 == 1:
            disp_dir = engine._edge_manager.current_bos.direction if engine._edge_manager.current_bos else 'UNKNOWN'
            logger.info(
                f"🌊 RANGE SKIPPED: DISPLACEMENT EDGE {disp_dir} is active | "
                f"Range analysis disabled - market already broke range"
            )

    if skip_range_analysis:
        return False

    # Early location_context calculation if missing
    if not engine._location_context:
        try:
            closes_1h_early = np.asarray(ctx.df_1h['close'].to_numpy())
            highs_1h_early = np.asarray(ctx.df_1h['high'].to_numpy())
            lows_1h_early = np.asarray(ctx.df_1h['low'].to_numpy())
            volumes_1h_early = np.asarray(ctx.df_1h['volume'].to_numpy()) if 'volume' in ctx.df_1h.columns else np.ones(len(closes_1h_early))

            atr_1h_vals_early = calculate_atr(highs_1h_early, lows_1h_early, closes_1h_early, 14)
            atr_1h_early = atr_1h_vals_early[-1] if len(atr_1h_vals_early) > 0 else ctx.price * 0.01

            adx_1h_vals_early, _ = calculate_adx(highs_1h_early, lows_1h_early, closes_1h_early, 14)
            adx_1h_early = adx_1h_vals_early[-1] if len(adx_1h_vals_early) > 0 else 20.0

            location_cache_key_early = _ts_to_int(ctx.df_1h.iloc[-1].get('timestamp', 0))
            _, _, engine._location_context = engine.location_filter.check(
                closes_1h=closes_1h_early,
                highs_1h=highs_1h_early,
                lows_1h=lows_1h_early,
                volumes_1h=volumes_1h_early,
                current_price=ctx.price,
                atr_1h=atr_1h_early,
                adx_1h=adx_1h_early,
                bias=ctx.bias,
                regime='RANGE',
                cache_key=location_cache_key_early
            )
            logger.debug(f"📍 RANGE MODE: Calculated early location_context")
        except Exception as e:
            logger.warning(f"⚠️ RANGE MODE: Failed to calculate early location_context: {e}")

    # Check at-extreme logic
    at_extreme = False
    extreme_type = None

    if not engine._location_context:
        if engine._displacement_bypass or engine._expansion_mode:
            logger.info(
                f"🔥 RANGE MODE: No location_context but DISPLACEMENT active | "
                f"BYPASSING range check - displacement takes priority"
            )
        else:
            if engine._idle_eval_count % 120 == 1:
                logger.info(
                    f"⏸️ RANGE MODE: No location context available | "
                    f"Cannot determine if at extreme - SKIPPING"
                )
            return True

    pct_60d = engine._location_context.percentile_60d if engine._location_context else 50.0
    engine._current_trade_tags['percentile_60d'] = pct_60d

    # Adaptive volume threshold
    BASE_MIN_VOLUME_RATIO = 0.5
    vol_volume_adj = 1.0 + (engine._current_volatility_ratio - 1.0) * 0.2
    vol_volume_adj = max(0.7, min(1.3, vol_volume_adj))
    MIN_VOLUME_RATIO_RANGE = BASE_MIN_VOLUME_RATIO * vol_volume_adj
    MIN_VOLUME_RATIO_RANGE = max(0.3, min(0.7, MIN_VOLUME_RATIO_RANGE))

    volume_ratio = engine._current_trade_tags.get('volume_ratio', 1.0)
    if volume_ratio < MIN_VOLUME_RATIO_RANGE:
        if engine._displacement_bypass or engine._expansion_mode:
            if engine._idle_eval_count % 180 == 1:
                logger.info(
                    f"🔥 RANGE VOLUME BYPASS: vol_ratio={volume_ratio:.0%} < {MIN_VOLUME_RATIO_RANGE:.0%} | "
                    f"But DISPLACEMENT active - proceeding"
                )
        else:
            low_vol_penalty = max(0.3, volume_ratio / MIN_VOLUME_RATIO_RANGE)
            engine._current_trade_tags['low_volume_penalty'] = low_vol_penalty
            if engine._idle_eval_count % 180 == 1:
                logger.info(
                    f"⚠️ RANGE: Low volume | vol_ratio={volume_ratio:.0%} < {MIN_VOLUME_RATIO_RANGE:.0%} | "
                    f"PENALTY: size_mult={low_vol_penalty:.2f}"
                )

    # Adaptive extreme percentiles
    BASE_EXTREME_HIGH_PCT = 85
    BASE_EXTREME_LOW_PCT = 15
    vol_extreme_adj = (engine._current_volatility_ratio - 1.0) * 5
    vol_extreme_adj = max(-5, min(5, vol_extreme_adj))
    EXTREME_HIGH_PCT = max(80, min(95, BASE_EXTREME_HIGH_PCT + vol_extreme_adj))
    EXTREME_LOW_PCT = max(5, min(20, BASE_EXTREME_LOW_PCT - vol_extreme_adj))

    engine._current_trade_tags['adaptive_extreme_high'] = EXTREME_HIGH_PCT
    engine._current_trade_tags['adaptive_extreme_low'] = EXTREME_LOW_PCT

    if pct_60d >= EXTREME_HIGH_PCT:
        at_extreme = True
        extreme_type = 'HIGH'
        if ctx.bias == 'LONG':
            if engine.has_momentum_override:
                logger.info(
                    f"🔥 RANGE HIGH BYPASS: pct={pct_60d:.0f}% (HIGH) but MOMENTUM_OVERRIDE active | "
                    f"Allowing LONG - potential ATH breakout"
                )
                engine._current_trade_tags['range_high_bypass'] = True
            else:
                logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (HIGH≥{EXTREME_HIGH_PCT:.0f}%) - LONG blocked, only SHORT")
                return True
    elif pct_60d <= EXTREME_LOW_PCT:
        at_extreme = True
        extreme_type = 'LOW'
        if ctx.bias == 'SHORT':
            if engine.has_momentum_override:
                logger.info(
                    f"🔥 RANGE LOW BYPASS: pct={pct_60d:.0f}% (LOW) but MOMENTUM_OVERRIDE active | "
                    f"Allowing SHORT - potential breakdown"
                )
                engine._current_trade_tags['range_low_bypass'] = True
            else:
                logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (LOW) - SHORT blocked, only LONG")
                return True

    if not at_extreme:
        return _handle_range_mid(engine, ctx, pct_60d, EXTREME_HIGH_PCT, EXTREME_LOW_PCT)
    else:
        return _handle_range_extreme(engine, ctx, pct_60d, extreme_type, EXTREME_HIGH_PCT, EXTREME_LOW_PCT)


def _handle_range_mid(
    engine: 'SignalEngine', ctx: IdleCtx,
    pct_60d: float, EXTREME_HIGH_PCT: float, EXTREME_LOW_PCT: float,
) -> bool:
    """RANGE MID logic — not at extreme. Returns True to abort pipeline."""
    # Unified bypass check
    should_bypass, bypass_reason = engine._should_bypass_regime_filter('RANGE_MID')

    if should_bypass:
        if engine._idle_eval_count % 180 == 1:
            logger.info(
                f"🔥 RANGE BYPASS: {bypass_reason} | pct={pct_60d:.0f}% ignored | "
                f"ACTIVE_DIRECTION={engine._active_direction} | "
                f"{'EXPANSION=' + str(engine._expansion_direction) + ' | ' if engine._expansion_mode else ''}"
                f"Range filter bypassed - continuing evaluation"
            )
        engine._current_trade_tags['range_bypass_reason'] = bypass_reason
        engine._current_trade_tags['range_size_mult'] = 0.60

        bypass_direction = engine._active_direction or engine._expansion_direction
        if bypass_direction:
            # Even BYPASS must confirm price is moving in direction
            price_confirms, _, confirm_reason = engine._price_confirms_direction(bypass_direction)
            if not price_confirms:
                if engine._idle_eval_count % 120 == 1:
                    logger.warning(f"⚠️ RANGE BYPASS: {bypass_direction} BLOCKED BY PRICE | {confirm_reason}")
                return True
            if engine._is_post_bias_flip_blocked(ctx.price, ctx.atr_1m):
                return True
            macro_str = engine._current_bias_strength.value if engine._current_bias_strength else None
            edge_armed = engine._edge_manager.arm_consensus_edge(
                direction=bypass_direction,
                current_price=ctx.price,
                atr=ctx.atr_1m,
                market_time=ctx.time,
                reason=f"RANGE_BYPASS ({bypass_reason})",
                macro_bias_strength=macro_str,
                has_momentum_override=engine.has_momentum_override,
                forbidden_direction=engine._forbidden_direction
            )
            if edge_armed:
                engine._current_trade_tags['range_edge_armed'] = True
                engine._current_trade_tags['edge_source'] = 'RANGE_BYPASS'
                logger.info(
                    f"🎯 EDGE ARMED: RANGE_BYPASS {bypass_direction} | {bypass_reason}"
                )
        return False  # Continue pipeline

    # Direction from orchestrator
    direction = engine._active_direction
    source = engine._active_direction_source
    confidence = engine._active_direction_confidence

    # RANGE Mean-Reversion direction
    RANGE_MR_LOW_PCT = 30.0
    RANGE_MR_HIGH_PCT = 70.0

    _has_armed_edge = engine._edge_manager.is_edge_active()

    if engine._trend_expansion_active:
        if engine._idle_eval_count % 120 == 1:
            logger.info(
                f"⛔ RANGE MR BLOCKED: TREND EXPANSION active | "
                f"Expansion contradicts RANGE classification"
            )
        engine._current_trade_tags['range_mr_blocked'] = 'TREND_EXPANSION'
        _has_armed_edge = True

    if not _has_armed_edge and pct_60d < RANGE_MR_LOW_PCT:
        original_dir = direction
        direction = 'LONG'
        source = 'RANGE_MR_LOW'
        confidence = 0.65
        engine._active_direction = direction
        engine._active_direction_source = 'RANGE_MR_LOW'
        engine._active_direction_confidence = confidence
        if engine._idle_eval_count % 60 == 1:
            logger.info(
                f"🔄 RANGE MR LOW: pct={pct_60d:.0f}% < {RANGE_MR_LOW_PCT:.0f}% | "
                f"Direction={original_dir} → LONG (buy near range support)"
            )
        engine._current_trade_tags['range_mr_override'] = True
        engine._current_trade_tags['range_mr_from'] = original_dir
        engine._current_trade_tags['range_mr_pct'] = pct_60d
    elif not _has_armed_edge and pct_60d > RANGE_MR_HIGH_PCT:
        original_dir = direction
        direction = 'SHORT'
        source = 'RANGE_MR_HIGH'
        confidence = 0.65
        engine._active_direction = direction
        engine._active_direction_source = 'RANGE_MR_HIGH'
        engine._active_direction_confidence = confidence
        if engine._idle_eval_count % 60 == 1:
            logger.info(
                f"🔄 RANGE MR HIGH: pct={pct_60d:.0f}% > {RANGE_MR_HIGH_PCT:.0f}% | "
                f"Direction={original_dir} → SHORT (sell near range resistance)"
            )
        engine._current_trade_tags['range_mr_override'] = True
        engine._current_trade_tags['range_mr_from'] = original_dir
        engine._current_trade_tags['range_mr_pct'] = pct_60d

    engine._current_trade_tags['range_mid_mode'] = True
    engine._current_trade_tags['range_mid_direction'] = direction
    engine._current_trade_tags['range_mid_source'] = source
    engine._current_trade_tags['range_mid_confidence'] = confidence

    # NO STRUCTURAL_RANGE_FALLBACK — coin flip signals eliminados.
    # Si no hay dirección real, el bot ESPERA.

    if direction and (source in ctx.core_sources or confidence >= 0.60):
        # Range coherence guard
        if direction == 'SHORT' and pct_60d < 20.0:
            if engine._idle_eval_count % 120 == 1:
                logger.warning(
                    f"🚫 RANGE COHERENCE: SHORT at pct={pct_60d:.0f}% < 20% | "
                    f"Cannot SHORT at range floor | source={source}"
                )
            return True
        if direction == 'LONG' and pct_60d > 80.0:
            if engine._idle_eval_count % 120 == 1:
                logger.warning(
                    f"🚫 RANGE COHERENCE: LONG at pct={pct_60d:.0f}% > 80% | "
                    f"Cannot LONG at range ceiling | source={source}"
                )
            return True

        # Price MUST confirm direction before arming edge — no blind entries
        price_confirms, price_change, confirm_reason = engine._price_confirms_direction(direction)
        if not price_confirms:
            if engine._idle_eval_count % 120 == 1:
                logger.warning(f"⚠️ RANGE MID: {source} {direction} BLOCKED BY PRICE | {confirm_reason}")
            engine._current_trade_tags['range_mid_reason'] = 'PRICE_CONTRADICTED'
            return True

        if engine._idle_eval_count % 300 == 1:
            logger.info(
                f"✅ RANGE MID: {source} | direction={direction} | conf={confidence:.0%} | "
                f"{confirm_reason} | pct={pct_60d:.0f}% | Size=40% | TRADING"
            )
        engine._current_trade_tags['range_size_mult'] = 0.40
        engine._current_trade_tags['range_mid_reason'] = source

        # ATR gate → penalty instead of block (compounds with micro_range_penalty)
        atr_1m_avg = engine._current_trade_tags.get('atr_1m_avg', ctx.atr_1m)
        atr_ratio_check = ctx.atr_1m / atr_1m_avg if atr_1m_avg > 0 else 1.0
        MIN_ATR_RATIO_FOR_RANGE = 0.30
        if atr_ratio_check < MIN_ATR_RATIO_FOR_RANGE:
            engine._current_trade_tags['low_atr_penalty'] = 0.50
            if engine._idle_eval_count % 300 == 1:
                logger.info(
                    f"⚠️ RANGE LOW VOL: ATR ratio {atr_ratio_check:.2f} < {MIN_ATR_RATIO_FOR_RANGE} | "
                    f"Size penalty 50% (not blocked)"
                )

        if engine._is_post_bias_flip_blocked(ctx.price, ctx.atr_1m):
            return True
        macro_str = engine._current_bias_strength.value if engine._current_bias_strength else None
        edge_armed = engine._edge_manager.arm_consensus_edge(
            direction=direction,
            current_price=ctx.price,
            atr=ctx.atr_1m,
            market_time=ctx.time,
            reason=f"RANGE_CONSENSUS (pct={pct_60d:.0f}%, conf={confidence:.0%})",
            macro_bias_strength=macro_str,
            has_momentum_override=engine.has_momentum_override,
            forbidden_direction=engine._forbidden_direction
        )
        if edge_armed:
            engine._current_trade_tags['range_edge_armed'] = True
            engine._current_trade_tags['edge_source'] = f'RANGE_{source}'
            logger.info(
                f"🎯 EDGE ARMED: RANGE_{source} {direction} | pct={pct_60d:.0f}% | "
                f"conf={confidence:.0%} | Size=40%"
            )
        return False  # Continue
    else:
        engine._current_trade_tags['range_mid_reason'] = 'NO_CONSENSUS'
        engine._regime_blocked = True
        engine._regime_block_reason = f"RANGE_MID_NO_CONSENSUS (source={source}, conf={confidence:.0%})"
        if engine._idle_eval_count % 300 == 1:
            logger.info(
                f"📊 RANGE MID: NO CONSENSUS | source={source} | conf={confidence:.0%} | "
                f"pct={pct_60d:.0f}% | NO TRADE"
            )
        return True


def _handle_range_extreme(
    engine: 'SignalEngine', ctx: IdleCtx,
    pct_60d: float, extreme_type: str,
    EXTREME_HIGH_PCT: float, EXTREME_LOW_PCT: float,
) -> bool:
    """RANGE EXTREME logic — at range boundary. Returns True to abort."""
    engine._current_trade_tags['range_mode_extreme'] = extreme_type

    if extreme_type == 'LOW':
        extremity = (EXTREME_LOW_PCT - pct_60d) / EXTREME_LOW_PCT
    else:
        extremity = (pct_60d - EXTREME_HIGH_PCT) / (100 - EXTREME_HIGH_PCT)
    extremity = max(0.0, min(1.0, extremity))

    size_mult = 0.35 + extremity * 0.25
    engine._current_trade_tags['range_size_mult'] = size_mult
    engine._current_trade_tags['range_extremity'] = round(extremity, 2)

    range_direction = 'SHORT' if extreme_type == 'HIGH' else 'LONG'
    if engine._is_post_bias_flip_blocked(ctx.price, ctx.atr_1m):
        return True
    macro_str = engine._current_bias_strength.value if engine._current_bias_strength else None
    edge_armed = engine._edge_manager.arm_consensus_edge(
        direction=range_direction,
        current_price=ctx.price,
        atr=ctx.atr_1m,
        market_time=ctx.time,
        reason=f"RANGE_EXTREME_{extreme_type} (pct={pct_60d:.0f}%, extremity={extremity:.0%})",
        macro_bias_strength=macro_str,
        has_momentum_override=engine.has_momentum_override,
        forbidden_direction=engine._forbidden_direction
    )
    if edge_armed:
        engine._current_trade_tags['range_edge_armed'] = True
        engine._current_trade_tags['edge_source'] = f'RANGE_EXTREME_{extreme_type}'
        logger.info(
            f"🎯 EDGE ARMED: RANGE_EXTREME_{extreme_type} {range_direction} | "
            f"pct={pct_60d:.0f}% | extremity={extremity:.0%} | Size={size_mult:.0%}"
        )

    if engine._idle_eval_count % 60 == 1:
        logger.info(
            f"📊 RANGE EXTREME: {extreme_type} | pct={pct_60d:.0f}% | extremity={extremity:.0%} | "
            f"Size={size_mult:.0%} | TRADING"
        )
    return False  # Continue pipeline


# =========================================================================
# TRANSITION MODE HANDLER
# =========================================================================

def stage_transition_mode(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """
    TRANSITION mode rules (RegimeScore 0.35-0.50).
    Returns True if the pipeline should abort.
    """
    engine._current_trade_tags['transition_mode'] = True

    transition_progress = (ctx.regime_score - REGIME.RANGE_MAX) / (REGIME.TRANSITION_MAX - REGIME.RANGE_MAX)
    transition_progress = max(0.0, min(1.0, transition_progress))
    gradual_size = SIZING.SIZE_MULT_TRANSITION + (transition_progress * (SIZING.SIZE_MULT_TREND - SIZING.SIZE_MULT_TRANSITION))

    engine._current_trade_tags['transition_size_mult'] = gradual_size
    engine._current_trade_tags['transition_progress'] = transition_progress

    direction = engine._active_direction
    source = engine._active_direction_source
    confidence = engine._active_direction_confidence
    engine._current_trade_tags['transition_direction'] = direction
    engine._current_trade_tags['transition_source'] = source
    engine._current_trade_tags['transition_confidence'] = confidence

    if direction and (source in ctx.core_sources or confidence >= 0.6):
        edge_already_active = (
            engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
            engine._edge_manager.current_bos is not None and
            engine._edge_manager.current_bos.direction == direction
        )

        if not edge_already_active:
            price_confirms, price_change, confirm_reason = engine._price_confirms_direction(direction)
            if not price_confirms:
                if engine._idle_eval_count % 30 == 1:
                    logger.warning(f"⚠️ TRANSITION: {source} BLOCKED BY PRICE | {confirm_reason}")
                engine._current_trade_tags['transition_reason'] = 'PRICE_CONTRADICTED'
                engine._regime_blocked = True
                engine._regime_block_reason = f"PRICE_CONTRADICTS: {confirm_reason}"
                return True

        now = ctx.time
        last_trans_log = getattr(engine, '_last_transition_log', None)
        last_trans_dir = getattr(engine, '_last_transition_dir', None)
        should_log = (
            last_trans_log is None or
            (now - last_trans_log).total_seconds() >= 60 or
            last_trans_dir != direction
        )
        if should_log:
            logger.info(
                f"✅ TRANSITION: {source} | dir={direction} | conf={confidence:.0%} | "
                f"Score={ctx.regime_score:.2f} | Size={gradual_size:.0%} | TRADING"
            )
            engine._last_transition_log = now
            engine._last_transition_dir = direction
        engine._current_trade_tags['transition_reason'] = source

        if engine._is_post_bias_flip_blocked(ctx.price, ctx.atr_1m):
            return True

        macro_str = engine._current_bias_strength.value if engine._current_bias_strength else None
        edge_armed = engine._edge_manager.arm_consensus_edge(
            direction=direction,
            current_price=ctx.price,
            atr=ctx.atr_1m,
            market_time=ctx.time,
            reason=f"TRANSITION_{source} (Score={ctx.regime_score:.2f}, conf={confidence:.0%})",
            macro_bias_strength=macro_str,
            has_momentum_override=engine.has_momentum_override,
            forbidden_direction=engine._forbidden_direction
        )
        if edge_armed:
            engine._current_trade_tags['consensus_edge_armed'] = True
            engine._current_trade_tags['edge_source'] = f'TRANSITION_{source}'
    else:
        engine._current_trade_tags['transition_reason'] = 'NO_CONSENSUS'
        engine._regime_blocked = True
        engine._regime_block_reason = f"TRANSITION_NO_CONSENSUS (source={source}, conf={confidence:.0%})"
        if engine._idle_eval_count % 30 == 1:
            logger.info(
                f"📊 TRANSITION: NO CONSENSUS | source={source} | conf={confidence:.0%} | "
                f"Score={ctx.regime_score:.2f} | NO TRADE"
            )
        return True

    return False  # Continue pipeline


# =========================================================================
# TREND MODE HANDLER
# =========================================================================

def stage_trend_mode(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """
    TREND mode rules (RegimeScore >= 0.50).
    Returns True if the pipeline should abort.
    """
    engine._current_trade_tags['trend_mode'] = True

    direction = engine._active_direction
    source = engine._active_direction_source
    confidence = engine._active_direction_confidence
    engine._current_trade_tags['trend_direction'] = direction
    engine._current_trade_tags['trend_source'] = source
    engine._current_trade_tags['trend_confidence'] = confidence

    has_displacement = engine._displacement_bypass or engine._expansion_mode
    engine._current_trade_tags['trend_displacement'] = has_displacement

    if direction and confidence >= 0.5:
        edge_already_active = (
            engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
            engine._edge_manager.current_bos is not None and
            engine._edge_manager.current_bos.direction == direction
        )

        # Price MUST confirm direction — NO bypasses except existing active edge
        if not edge_already_active:
            price_confirms, price_change, confirm_reason = engine._price_confirms_direction(direction)
            if not price_confirms:
                if engine._idle_eval_count % 120 == 1:
                    logger.warning(f"⚠️ TREND: {source} BLOCKED BY PRICE | {confirm_reason}")
                engine._current_trade_tags['trend_reason'] = 'PRICE_CONTRADICTED'
                return True

        adx_strength = min(1.0, (ctx.effective_adx - ADX.TREND_MIN) / 30)
        trend_size = SIZING.TREND_BASE + adx_strength * SIZING.TREND_ADX_BONUS

        if engine._idle_eval_count % 180 == 1:
            logger.info(
                f"✅ TREND: {source} | dir={direction} | conf={confidence:.0%} | "
                f"ADX={ctx.effective_adx:.1f} | Size={trend_size:.0%} | TRADING"
            )
        engine._current_trade_tags['trend_size_mult'] = trend_size
        engine._current_trade_tags['trend_reason'] = source

        # RANGE_BOUND phase guard removed (Feb 2026):
        # When regime_score >= 0.50 (TREND), but _trend_phase == RANGE_BOUND,
        # the phase is driven by lagging ADX 1H data. The regime scorer already
        # classified this as TREND — the stale phase contradicts it and blocked
        # entries within 4.5×ATR of recent high/low (virtually all prices in a
        # small range). Trust the regime scorer, not the lagging phase.

        if engine._is_post_bias_flip_blocked(ctx.price, ctx.atr_1m):
            return True

        macro_str = engine._current_bias_strength.value if engine._current_bias_strength else None
        edge_armed = engine._edge_manager.arm_consensus_edge(
            direction=direction,
            current_price=ctx.price,
            atr=ctx.atr_1m,
            market_time=ctx.time,
            reason=f"TREND_{source} (ADX={ctx.effective_adx:.1f}, conf={confidence:.0%})",
            macro_bias_strength=macro_str,
            has_momentum_override=engine.has_momentum_override,
            forbidden_direction=engine._forbidden_direction
        )
        if edge_armed:
            engine._current_trade_tags['consensus_edge_armed'] = True
            engine._current_trade_tags['edge_source'] = f'TREND_{source}'
            logger.info(
                f"🎯 EDGE ARMED: TREND_{source} {direction} | ADX={ctx.effective_adx:.1f} | "
                f"conf={confidence:.0%} | DIRECT TO ACTIVE"
            )
    else:
        engine._current_trade_tags['trend_reason'] = 'WAITING_EDGE'
        if engine._idle_eval_count % 180 == 1:
            logger.info(
                f"📊 TREND: WAITING EDGE | source={source} | conf={confidence:.0%} | "
                f"ADX={ctx.effective_adx:.1f} | Need BOS/breakout"
            )
        # DON'T return — let EdgeStateManager try to find edge

    return False  # Continue pipeline
