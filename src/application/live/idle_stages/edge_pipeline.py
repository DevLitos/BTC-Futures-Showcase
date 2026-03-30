"""
Edge detection pipeline extracted from SignalEngine._handle_idle_state.

Stages:
- stage_counter_trend_htf: Counter-trend defensive + HTF final gate
- stage_edge_detection: Swing detection, BOS, Range/Breakout edges, pullback confirmation
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.config.trading_config import HTFFilters, ADXThresholds
from src.domain.trading.edge_state_manager import EdgeState, EdgeType, RangeEdgeCriteria
from src.domain.trading.edge_types import TrendEdgePath
from src.shared.indicators import (
    calculate_atr, calculate_adx, detect_swing_high, detect_swing_low,
    calculate_bollinger_bands,
)

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from .context import IdleCtx

logger = logging.getLogger(__name__)


# =========================================================================
# COUNTER-TREND / HTF FINAL GATE
# =========================================================================

def stage_counter_trend_htf(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """
    Counter-trend defensive mode + final HTF pass/fail gate.
    Returns True if the pipeline should abort.
    """
    # Allow counter-trend trading in defensive mode
    if not ctx.htf_passed and HTFFilters.ALLOW_COUNTERTREND_DEFENSIVE:
        if engine.black_swan_filter.is_defensive(ctx.time):
            ema_50 = ctx.df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
            ema_200 = ctx.df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]

            if ema_50 > ema_200:
                counter_bias = 'SHORT'
            else:
                counter_bias = 'LONG'

            logger.info(f"⚠️ HTF rejected but DEFENSIVE MODE active - allowing counter-trend {counter_bias}")
            ctx.allow_countertrend = True
            ctx.bias = counter_bias
            ctx.htf_passed = True

    if not ctx.htf_passed:
        if engine._last_htf_reject_log_time is None:
            engine._last_htf_reject_log_time = ctx.time

        seconds_since_log = (ctx.time - engine._last_htf_reject_log_time).total_seconds()
        should_log = (seconds_since_log >= 300) or (engine._last_htf_state != 'rejected')

        if should_log:
            logger.info(f"[BLOCK] HTF filter: {ctx.htf_reason}")
            ema_50 = ctx.df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
            ema_200 = ctx.df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            adx_vals, _ = calculate_adx(ctx.df_1h['high'].to_numpy(), ctx.df_1h['low'].to_numpy(), ctx.df_1h['close'].to_numpy(), 14)
            adx = float(adx_vals[-1]) if len(adx_vals) > 0 else 0.0
            adx_slope = float(adx_vals[-1] - adx_vals[-2]) if len(adx_vals) > 1 else 0.0

            logger.debug(f"   [DATA] Price: ${ctx.price:.2f} | EMA50: ${ema_50:.2f} | EMA200: ${ema_200:.2f}")
            logger.debug(f"   [UP] ADX: {adx:.2f} | Slope: {adx_slope:.4f}")

            engine._last_htf_reject_log_time = ctx.time
            engine._last_htf_state = 'rejected'
        return True

    engine.current_bias = ctx.bias
    bias_label = f'{ctx.bias} (counter-trend)' if ctx.allow_countertrend else ctx.bias
    if engine._last_htf_state != f'passed_{ctx.bias}':
        logger.debug(f"[OK] HTF filter passed - Bias: {bias_label}")
        engine._last_htf_state = f'passed_{ctx.bias}'

    return False


# =========================================================================
# EDGE DETECTION PIPELINE
# =========================================================================

def stage_edge_detection(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """
    Full edge detection pipeline: swings, BOS, range/breakout/trend edges,
    pullback/displacement/breakout confirmation, edge ownership of direction.
    Returns True if the pipeline should abort.
    """
    # Prepare 1h data arrays
    ctx.closes_1h = np.asarray(ctx.df_1h['close'].to_numpy())
    ctx.highs_1h = np.asarray(ctx.df_1h['high'].to_numpy())
    ctx.lows_1h = np.asarray(ctx.df_1h['low'].to_numpy())

    atr_1h_vals = calculate_atr(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, 14)
    ctx.atr_1h = float(atr_1h_vals[-1]) if len(atr_1h_vals) > 0 else ctx.price * 0.01

    # ── Swing detection ──
    SWING_LOOKBACK_1H = 3

    swing_high_detected, swing_high_price, _ = detect_swing_high(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, lookback=SWING_LOOKBACK_1H)
    swing_low_detected, swing_low_price, _ = detect_swing_low(ctx.highs_1h, ctx.lows_1h, ctx.closes_1h, lookback=SWING_LOOKBACK_1H)

    MAX_SWING_DISTANCE_PCT = 0.15

    def is_swing_relevant(swing_price: float, ref_price: float) -> bool:
        if swing_price <= 0 or ref_price <= 0:
            return False
        return abs(swing_price - ref_price) / ref_price <= MAX_SWING_DISTANCE_PCT

    # Update swings if relevant
    if swing_high_detected and swing_high_price > 0:
        if swing_high_price != engine._last_swing_high:
            if is_swing_relevant(swing_high_price, ctx.price):
                engine._prev_swing_high = engine._last_swing_high
                engine._last_swing_high = swing_high_price
                logger.info(f"📍 NEW SWING HIGH: ${swing_high_price:.0f} (prev: ${engine._prev_swing_high:.0f})")

    if swing_low_detected and swing_low_price > 0:
        if swing_low_price != engine._last_swing_low:
            if is_swing_relevant(swing_low_price, ctx.price):
                engine._prev_swing_low = engine._last_swing_low
                engine._last_swing_low = swing_low_price
                logger.info(f"📍 NEW SWING LOW: ${swing_low_price:.0f} (prev: ${engine._prev_swing_low:.0f})")

    # Fallback initialization
    if engine._last_swing_high == 0 or not is_swing_relevant(engine._last_swing_high, ctx.price):
        RECENT_BARS = 24
        if len(ctx.highs_1h) >= RECENT_BARS:
            candidate_high = float(np.max(ctx.highs_1h[-RECENT_BARS:]))
            candidate_low = float(np.min(ctx.lows_1h[-RECENT_BARS:])) if len(ctx.lows_1h) >= RECENT_BARS else ctx.price * 0.98
            if ctx.atr_1h > 0 and (candidate_high - candidate_low) > 10 * ctx.atr_1h:
                RECENT_BARS = 12
                candidate_high = float(np.max(ctx.highs_1h[-RECENT_BARS:]))
                logger.info(f"📍 SWING SANITY: 24-bar range too wide, using {RECENT_BARS} bars")
            engine._last_swing_high = candidate_high
            engine._prev_swing_high = engine._last_swing_high
            logger.info(f"📍 INIT SWING HIGH: ${engine._last_swing_high:.0f} (from last {RECENT_BARS} bars)")
        else:
            engine._last_swing_high = ctx.price * 1.02
            engine._prev_swing_high = engine._last_swing_high

    if engine._last_swing_low == 0 or not is_swing_relevant(engine._last_swing_low, ctx.price):
        RECENT_BARS = 24
        if len(ctx.lows_1h) >= RECENT_BARS:
            candidate_low = float(np.min(ctx.lows_1h[-RECENT_BARS:]))
            candidate_high = float(np.max(ctx.highs_1h[-RECENT_BARS:])) if len(ctx.highs_1h) >= RECENT_BARS else ctx.price * 1.02
            if ctx.atr_1h > 0 and (candidate_high - candidate_low) > 10 * ctx.atr_1h:
                RECENT_BARS = 12
                candidate_low = float(np.min(ctx.lows_1h[-RECENT_BARS:]))
                logger.info(f"📍 SWING SANITY: 24-bar range too wide, using {RECENT_BARS} bars")
            engine._last_swing_low = candidate_low
            engine._prev_swing_low = engine._last_swing_low
            logger.info(f"📍 INIT SWING LOW: ${engine._last_swing_low:.0f} (from last {RECENT_BARS} bars)")
        else:
            engine._last_swing_low = ctx.price * 0.98
            engine._prev_swing_low = engine._last_swing_low

    # ── Edge expiration / invalidation ──
    swing_range_1h = engine._last_swing_high - engine._last_swing_low if (engine._last_swing_high > 0 and engine._last_swing_low > 0) else 0.0
    engine._edge_manager.check_expiration(ctx.time, ctx.price)
    engine._edge_manager.check_invalidation(ctx.price, ctx.atr_1h, swing_range_1h)

    # ── Displacement entry window update ──
    if engine._edge_manager.current_bos and engine._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT:
        should_kill, kill_reason = engine._edge_manager.update_displacement_entry_window(ctx.time)
        if should_kill:
            logger.warning(f"🚫 {kill_reason}")
            engine._edge_manager._expire_edge(kill_reason)
            engine._current_trade_tags['displacement_killed_late'] = True
            engine._current_trade_tags['post_displacement_cooldown'] = True

    # ── Edge invalidation on direction mismatch ──
    anticipation_signal = engine._current_trade_tags.get('anticipation_signal')
    anticipation_direction = engine._current_trade_tags.get('anticipation_direction')
    valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
    has_anticipation_breakout = (anticipation_signal in valid_anticipation_signals and anticipation_direction)

    if engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
        edge_direction = None
        edge_type = None
        if engine._edge_manager.current_bos:
            edge_direction = engine._edge_manager.current_bos.direction
            edge_type = engine._edge_manager.current_bos.edge_type

        if edge_direction and engine._active_direction:
            if edge_direction != engine._active_direction:
                if edge_type in [EdgeType.BREAKOUT, EdgeType.DISPLACEMENT]:
                    if engine._idle_eval_count % 120 == 1:
                        logger.debug(
                            f"📊 Edge-Direction divergence: Edge={edge_direction} vs "
                            f"Orchestrated={engine._active_direction} | Edge type={edge_type.value} | "
                            f"Not invalidating - higher priority signal active"
                        )
                elif has_anticipation_breakout:
                    if engine._idle_eval_count % 120 == 1:
                        logger.debug(
                            f"🎯 ANTICIPATION BREAKOUT PENDING: Skipping edge invalidation | "
                            f"Old edge will be replaced by BREAKOUT {anticipation_direction}"
                        )
                else:
                    logger.info(
                        f"🔄 EDGE INVALIDATED: Direction mismatch | "
                        f"Edge={edge_direction} vs Active={engine._active_direction} | "
                        f"Market changed direction → resetting edge"
                    )
                    engine._edge_manager._transition_to(
                        EdgeState.NO_EDGE,
                        f"Direction flip: {edge_direction}→{engine._active_direction}",
                        ctx.time
                    )
                    engine._current_trade_tags['edge_invalidated_by'] = 'direction_flip'

    # ── Detect new edges (NO_EDGE or EXPIRED state) ──
    # Also detect in EXPIRED state.
    # EXPIRED is transient and auto-resets to NO_EDGE via check_expiration(),
    # but as safety net, allow detection here too to prevent deadlock.
    if engine._edge_manager.state in [EdgeState.NO_EDGE, EdgeState.EDGE_EXPIRED]:
        _detect_new_edges(engine, ctx, has_anticipation_breakout, anticipation_direction)

    # ── Pullback / breakout / displacement / consensus confirmation (EDGE_ARMED) ──
    if engine._edge_manager.state == EdgeState.EDGE_ARMED:
        # First try consensus-specific confirmation (price must move in direction)
        candles_1m_for_confirm = engine.market_data.get_candles('1m')
        promoted = engine._edge_manager.confirm_consensus_armed(
            current_price=ctx.price,
            candles_1m=candles_1m_for_confirm,
            market_time=ctx.time,
        )
        if not promoted:
            # Fall back to standard confirmation (pullback, displacement, breakout)
            _confirm_armed_edge(engine, ctx)

    # ── Store edge status in tags ──
    edge_status = engine._edge_manager.get_status()
    engine._current_trade_tags['edge_state'] = edge_status['state']
    engine._current_trade_tags['edge_can_trade'] = edge_status['can_trade']
    if edge_status.get('current_bos'):
        engine._current_trade_tags['edge_bos_direction'] = edge_status['current_bos']['direction']
        engine._current_trade_tags['edge_trades_remaining'] = edge_status['current_bos']['trades_remaining']

    # ── Edge owns direction — sync local bias ──
    if engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
        edge_bos = engine._edge_manager.current_bos
        if edge_bos and edge_bos.direction:
            edge_direction = edge_bos.direction
            if ctx.bias != edge_direction and edge_direction == engine._active_direction:
                old_bias = ctx.bias
                ctx.bias = edge_direction
                engine.current_bias = ctx.bias
                if engine._idle_eval_count % 60 == 1:
                    logger.debug(
                        f"📊 Syncing local bias with orchestrated direction: {old_bias}→{edge_direction}"
                    )

    # ── NO_EDGE reason tagging ──
    if engine._edge_manager.state == EdgeState.NO_EDGE:
        percentile_60d = engine._current_trade_tags.get('percentile_60d', 50.0)
        regime = engine._current_trade_tags.get('regime_mode', engine._htf_cache_regime_mode)
        anticipation = engine._current_trade_tags.get('anticipation_signal')

        if regime == 'RANGE':
            if 15 < percentile_60d < 85:
                reason = f"RANGE pero pct={percentile_60d:.0f}% no extremo (need <15 o >85)"
            else:
                reason = "RANGE extremo pero sin sweep+rejection"
        elif anticipation:
            reason = f"ANTICIPATION {anticipation} sin confirmación micro-swing"
        else:
            reason = "Sin BOS/displacement detectado"
        engine._edge_manager._no_edge_reason = reason

    # ── Can-evaluate check ──
    if not engine._edge_manager.can_evaluate_entry():
        block_reason = engine._edge_manager.get_block_reason()

        if engine._idle_eval_count % 60 == 1:
            diag_parts = []
            regime = engine._current_trade_tags.get('regime_mode', engine._htf_cache_regime_mode)
            diag_parts.append(f"mode={regime}")
            percentile = engine._current_trade_tags.get('percentile_60d', 50.0)
            diag_parts.append(f"pct={percentile:.0f}%")
            adx_eff = engine._current_trade_tags.get('adx_effective', 20.0)
            diag_parts.append(f"ADX={adx_eff:.0f}")
            anticipation = engine._current_trade_tags.get('anticipation_signal')
            if anticipation:
                ant_dir = engine._current_trade_tags.get('anticipation_direction', '?')
                ant_conf = engine._current_trade_tags.get('anticipation_confidence', 0)
                diag_parts.append(f"ANT={anticipation}_{ant_dir}@{ant_conf:.0%}")
            bb_pct = engine._current_trade_tags.get('bb_width_percentile', 50.0)
            if bb_pct <= 10:
                diag_parts.append(f"⚡SQUEEZE={bb_pct:.0f}%")
            bias_diag = engine._active_direction or 'NONE'
            diag_parts.append(f"bias={bias_diag}")
            diag_str = " | ".join(diag_parts)
            logger.info(f"⏸️ {block_reason} | {diag_str}")

            stats = engine._edge_manager.stats
            if stats.total_bos_events > 0:
                logger.info(
                    f"   📊 Edge Stats: {stats.total_bos_events} BOS events | "
                    f"Expectancy/BOS: {stats.get_expectancy_per_bos():.3f}R"
                )

        if engine._edge_manager.state not in [EdgeState.EDGE_ARMED]:
            return True

    # Edge active — log
    if engine._edge_manager.can_trade():
        bos = engine._edge_manager.current_bos
        if bos:
            if engine._idle_eval_count % 180 == 1:
                logger.info(
                    f"🎯 EDGE ACTIVE: {bos.direction} | BOS level: {bos.bos_level:.2f} | "
                    f"Trades: {bos.trades_attempted}/{engine._edge_manager.MAX_TRADES_PER_EDGE}"
                )

    return False  # Continue pipeline


# ── Helpers ──────────────────────────────────────────────────────────────

def _detect_new_edges(engine: 'SignalEngine', ctx: IdleCtx,
                      has_anticipation_breakout: bool,
                      anticipation_direction: str | None) -> None:
    """Detect BREAKOUT, RANGE, or TREND edges when in NO_EDGE state."""

    # ── BREAKOUT edge from ANTICIPATION ──
    anticipation_signal = engine._current_trade_tags.get('anticipation_signal')
    valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
    if anticipation_signal in valid_anticipation_signals:
        _try_anticipation_edge(engine, ctx)

    # ── RANGE edge ──
    try_range_edge = ctx.mode == 'RANGE'
    range_edge = False

    if not try_range_edge and ctx.effective_adx < ADXThresholds.RANGE_EDGE_MAX:
        percentile_check = engine._location_context.percentile_60d if engine._location_context else 50.0
        if percentile_check <= RangeEdgeCriteria.EXTREME_LOW_PCT or percentile_check >= RangeEdgeCriteria.EXTREME_HIGH_PCT:
            try_range_edge = True
            logger.info(
                f"📊 RANGE EDGE OVERRIDE: ADX={ctx.effective_adx:.1f}<{ADXThresholds.RANGE_EDGE_MAX} "
                f"+ extreme pct={percentile_check:.0f}% → trying RANGE edge in {ctx.mode} mode"
            )

    if try_range_edge:
        range_edge = _try_range_edge(engine, ctx)

    # ── TREND BOS (if no range edge) ──
    if not try_range_edge or not range_edge:
        bos_detected = engine._edge_manager.check_for_bos(
            current_price=ctx.price,
            htf_bias=ctx.bias,
            swing_high=engine._last_swing_high,
            swing_low=engine._last_swing_low,
            prev_swing_high=engine._prev_swing_high,
            prev_swing_low=engine._prev_swing_low,
            atr=ctx.atr_1h,
            market_time=ctx.time
        )
        if bos_detected:
            engine._current_trade_tags['bos_detected'] = True
            engine._current_trade_tags['edge_type'] = 'TREND'


def _try_anticipation_edge(engine: 'SignalEngine', ctx: IdleCtx) -> None:
    """Attempt to arm a BREAKOUT or quick-confirm edge from ANTICIPATION."""
    anticipation_signal = engine._current_trade_tags.get('anticipation_signal')
    anticipation_direction = engine._current_trade_tags.get('anticipation_direction', 'LONG')
    anticipation_bb_pct = engine._current_trade_tags.get('anticipation_bb_percentile', 50.0)
    anticipation_vol_ratio = engine._current_trade_tags.get('anticipation_volume_ratio', 1.0)
    anticipation_confidence = engine._current_trade_tags.get('anticipation_confidence', 0.55)
    structure_level = engine._current_trade_tags.get('anticipation_structure_level', 0.0)

    micro_high = getattr(engine, '_current_micro_swing_high', None)
    micro_low = getattr(engine, '_current_micro_swing_low', None)

    quick_confirmed = False
    if anticipation_direction == 'LONG' and micro_high is not None:
        if ctx.price > micro_high:
            quick_confirmed = True
            logger.info(
                f"⚡ ANTICIPATION QUICK CONFIRM: LONG | price ${ctx.price:.0f} > "
                f"micro_high ${micro_high:.0f} | conf={anticipation_confidence:.0%}"
            )
    elif anticipation_direction == 'SHORT' and micro_low is not None:
        if ctx.price < micro_low:
            quick_confirmed = True
            logger.info(
                f"⚡ ANTICIPATION QUICK CONFIRM: SHORT | price ${ctx.price:.0f} < "
                f"micro_low ${micro_low:.0f} | conf={anticipation_confidence:.0%}"
            )

    if quick_confirmed and anticipation_confidence >= 0.50:
        engine._current_trade_tags['anticipation_quick_confirm'] = True
        engine._current_trade_tags['anticipation_entry'] = True
        engine._current_trade_tags['anticipation_probe_size'] = 0.35
        engine._active_direction = anticipation_direction
        engine._active_direction_source = 'ANTICIPATION'
        engine._active_direction_confidence = anticipation_confidence
        logger.info(
            f"🚀 ANTICIPATION QUICK ENTRY ARMED | {anticipation_direction} | "
            f"Confidence: {anticipation_confidence:.0%} | Quick confirm via micro break"
        )
    elif anticipation_confidence >= 0.50:
        breakout_armed = engine._edge_manager.check_for_breakout_edge(
            current_price=ctx.price,
            bb_width_percentile=anticipation_bb_pct,
            volume_ratio=anticipation_vol_ratio,
            direction=anticipation_direction,
            atr=ctx.atr_1h,
            market_time=ctx.time,
            structure_level=structure_level
        )
        if breakout_armed:
            engine._current_trade_tags['breakout_edge_armed'] = True
            engine._current_trade_tags['edge_type'] = 'BREAKOUT'
            engine._current_trade_tags['breakout_structure_level'] = structure_level
            logger.info(
                f"🔥 BREAKOUT EDGE ARMED from ANTICIPATION | "
                f"Direction: {anticipation_direction} | conf={anticipation_confidence:.0%} | "
                f"Structure: ${structure_level:.0f}"
            )


def _try_range_edge(engine: 'SignalEngine', ctx: IdleCtx) -> bool:
    """Attempt to detect a RANGE edge. Returns True if edge was found."""
    percentile_60d = engine._location_context.percentile_60d if engine._location_context else 50.0

    # Sweep + rejection data
    curr_high, curr_low, curr_open, curr_close = 0.0, 0.0, 0.0, 0.0
    prev_high, prev_low = 0.0, 0.0

    if ctx.candles_5m and len(ctx.candles_5m) >= 2:
        curr_candle = ctx.candles_5m[-1]
        prev_candle = ctx.candles_5m[-2]
        curr_high = float(curr_candle.get('high', 0))
        curr_low = float(curr_candle.get('low', 0))
        curr_open = float(curr_candle.get('open', 0))
        curr_close = float(curr_candle.get('close', 0))
        prev_high = float(prev_candle.get('high', 0))
        prev_low = float(prev_candle.get('low', 0))

    # BB width percentile for compression check
    bb_width_percentile = 50.0
    try:
        candles_1m = engine.market_data.get_candles('1m')
        if candles_1m and len(candles_1m) >= 50:
            df_1m = pd.DataFrame(candles_1m)
            closes_1m = df_1m['close'].to_numpy()
            bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes_1m, period=20, std_dev=2)
            if len(bb_upper) >= 20:
                bb_widths = bb_upper[-20:] - bb_lower[-20:]
                current_bb_width = bb_upper[-1] - bb_lower[-1]
                bb_width_percentile = (np.sum(bb_widths < current_bb_width) / len(bb_widths)) * 100
    except Exception as e:
        logger.debug(f"BB width percentile calc failed: {e}")

    range_edge = engine._edge_manager.check_for_range_edge(
        current_price=ctx.price,
        htf_bias=ctx.bias,
        percentile_60d=percentile_60d,
        atr=ctx.atr_1h,
        market_time=ctx.time,
        curr_high=curr_high, curr_low=curr_low,
        curr_open=curr_open, curr_close=curr_close,
        prev_high=prev_high, prev_low=prev_low,
        bb_width_percentile=bb_width_percentile
    )

    if range_edge:
        engine._current_trade_tags['range_edge_activated'] = True
        engine._current_trade_tags['edge_type'] = 'RANGE'
        engine._current_trade_tags['sweep_rejection_confirmed'] = True
        if engine._edge_manager.current_bos:
            engine._current_trade_tags['range_invalidation_level'] = engine._edge_manager.current_bos.bos_level

    return range_edge


def _confirm_armed_edge(engine: 'SignalEngine', ctx: IdleCtx) -> None:
    """Confirm an armed edge via pullback, displacement, or breakout.
    
    CONSENSUS edges are NOT handled here — they have their own promotion
    path via confirm_consensus_armed() which requires strict price confirmation.
    """
    current_bos = engine._edge_manager.current_bos
    if not current_bos:
        return
    
    # CONSENSUS edges must only be promoted via confirm_consensus_armed()
    if current_bos.trend_path == TrendEdgePath.CONSENSUS_DIRECT:
        return

    if current_bos.edge_type == EdgeType.DISPLACEMENT:
        # Displacement confirmation
        if current_bos.direction == 'SHORT':
            current_displacement = engine._bias_high_since - ctx.price
        else:
            current_displacement = ctx.price - engine._bias_low_since

        displacement_confirmed = engine._edge_manager.confirm_displacement_edge(
            current_price=ctx.price,
            displacement_direction=current_bos.direction,
            initial_displacement=engine._expansion_initial_displacement if hasattr(engine, '_expansion_initial_displacement') else 0,
            current_displacement=current_displacement,
            atr=ctx.atr_1h,
            market_time=ctx.time
        )
        if displacement_confirmed:
            engine._current_trade_tags['displacement_confirmed'] = True
            logger.info(
                f"🌊 DISPLACEMENT EDGE CONFIRMED: {current_bos.direction} | "
                f"Current disp: ${current_displacement:.0f}"
            )

    elif current_bos.edge_type == EdgeType.BREAKOUT:
        # Breakout confirmation with BB data
        bb_upper, bb_lower, bb_expansion_ratio = 0.0, 0.0, 0.0
        close_price = ctx.price

        try:
            candles_1m = engine.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 50:
                df_1m = pd.DataFrame(candles_1m)
                closes_1m = df_1m['close'].to_numpy()
                bb_upper_arr, bb_middle, bb_lower_arr = calculate_bollinger_bands(closes_1m, period=20, std_dev=2)

                if len(bb_upper_arr) >= 20:
                    bb_upper = float(bb_upper_arr[-1])
                    bb_lower = float(bb_lower_arr[-1])
                    close_price = float(closes_1m[-1])
                    bb_widths = bb_upper_arr[-20:] - bb_lower_arr[-20:]
                    avg_bb_width = float(np.mean(bb_widths[:-1]))
                    current_bb_width = bb_upper - bb_lower
                    bb_expansion_ratio = float(current_bb_width / avg_bb_width) if avg_bb_width > 0 else 1.0
        except Exception as e:
            logger.debug(f"BB data for breakout confirmation failed: {e}")

        direction_ok = (
            (current_bos.direction == 'LONG' and ctx.price > current_bos.price) or
            (current_bos.direction == 'SHORT' and ctx.price < current_bos.price)
        )

        if direction_ok:
            breakout_confirmed = engine._edge_manager.confirm_breakout(
                current_price=ctx.price,
                breakout_direction=current_bos.direction,
                atr=ctx.atr_1h,
                market_time=ctx.time,
                bb_upper=bb_upper,
                bb_lower=bb_lower,
                close_price=close_price,
                expansion_ratio=bb_expansion_ratio
            )
            if breakout_confirmed:
                engine._current_trade_tags['breakout_confirmed'] = True
                engine._current_trade_tags['bb_expansion_ratio'] = bb_expansion_ratio
    else:
        # TREND edge — pullback check
        pullback_valid = engine._edge_manager.check_for_pullback(
            current_price=ctx.price,
            atr=ctx.atr_1h,
            market_time=ctx.time
        )
        if pullback_valid:
            engine._current_trade_tags['pullback_validated'] = True
