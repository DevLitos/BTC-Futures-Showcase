"""
Displacement Fast Path — Fast entry for displacement edge events.

Handles immediate entry when a displacement (strong directional move)
is detected. Enter within 1-5 candles or defer to normal pipeline.

REFACTORED: Removed 6 redundant gates, simplified from 962 to ~530 lines.
DELETED: direction_memory, anticipation_deferral, counter_expansion,
         delta_contradict, entry_at_extreme (overlap with chase),
         fakeout_detector (overlap with pullback_readiness)
"""

import logging
import time as _time_mod
import pandas as pd
from datetime import datetime
from typing import Optional, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx, calculate_rsi
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE
from src.config.trading_config import (
    RiskManagement, OperatingMode, EntryGateConfig, DynamicScoreThreshold,
    SessionTradeLimits, VolatilityRegime, BiasStrengthConfig,
    get_session_name, OrderExecution, AsymmetryConfig
)
from src.domain.market.regime import TrendPhase, EntryRegime
from src.domain.trading.state.state_machine import BotState
from src.application.live.entry_orchestrator import (
    EntryContext, GuardResult, EntryResult, _finalize_entry,
    _should_log_rate_limited,
)

logger = logging.getLogger(__name__)


async def _displacement_fast_path(
    engine: 'SignalEngine',
    ctx: 'EntryContext',
    current_price: float,
    atr_1m: float,
    market_time: datetime
) -> Optional[EntryResult]:
    """
    FAST PATH for DISPLACEMENT — bypasses normal validation pipeline.
    Returns EntryResult if fast path executes, None to continue normal pipeline.
    """
    from src.domain.trading.edge_state_manager import EdgeState, EdgeType, DisplacementEdgeCriteria

    # ── GATE 1: Active DISPLACEMENT edge? ──
    edge_mgr = getattr(engine, '_edge_manager', None)
    if not edge_mgr or edge_mgr.state != EdgeState.EDGE_ACTIVE:
        return None

    current_bos = edge_mgr.current_bos
    if not current_bos or current_bos.edge_type != EdgeType.DISPLACEMENT:
        return None

    disp_direction = current_bos.direction
    bars_since = current_bos.bars_since_displacement

    # ── GATE 2: RANGE mode → skip fast path, let normal pipeline handle ──
    from src.domain.market.regime.trend_intelligence import TrendPhase
    current_trend_phase = getattr(engine, '_trend_phase', None)
    current_trading_mode = getattr(engine, '_current_trade_tags', {}).get('trading_mode', '')
    if current_trend_phase == TrendPhase.RANGE_BOUND or current_trading_mode == 'RANGE':
        logger.info(
            f"⏭️ FAST PATH SKIP: RANGE mode → normal pipeline | Edge preserved"
        )
        return None

    # ── GATE 3: DAILY LIMIT — hard cap, cannot be bypassed ──
    HARD_DAILY_LIMIT = 5
    daily_trades = getattr(engine.state_machine, 'daily_trades', 0)
    if daily_trades >= HARD_DAILY_LIMIT:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: DAILY LIMIT {daily_trades}/{HARD_DAILY_LIMIT}"
        )
        return EntryResult(success=False, reason=f"DAILY LIMIT: {daily_trades}/{HARD_DAILY_LIMIT} (fast path)")

    # ── GATE 4: STALE BLOCK — edge older than 15 min ──
    MAX_EDGE_AGE_MINUTES = 15.0
    edge_age_min = edge_mgr.get_edge_age_minutes()
    if edge_age_min > MAX_EDGE_AGE_MINUTES:
        logger.warning(
            f"🚫 STALE BLOCK: edge age {edge_age_min:.1f}min > {MAX_EDGE_AGE_MINUTES}min"
        )
        edge_mgr._expire_edge(reason=f"STALE_BLOCK:age={edge_age_min:.1f}min")
        return None

    # ── GATE 5: CHASE BLOCK — price moved > 1.5 ATR from origin ──
    CHASE_THRESHOLD_ATR = 1.5
    origin_price = current_bos.displacement_origin if hasattr(current_bos, 'displacement_origin') else current_bos.price

    if origin_price and atr_1m > 0:
        price_moved = (current_price - origin_price) if disp_direction == 'LONG' else (origin_price - current_price)
        chase_threshold = CHASE_THRESHOLD_ATR * atr_1m
        if price_moved > chase_threshold:
            logger.warning(
                f"🚫 CHASE BLOCK: moved ${price_moved:.0f} > ${chase_threshold:.0f} (1.5 ATR) | "
                f"Origin ${origin_price:.0f} → Current ${current_price:.0f}"
            )
            current_bos.force_direct_entry = False
            return None

    # ── GATE 6: BARS SINCE — window management ──
    trend_expansion_active = getattr(engine, '_trend_expansion_active', False)
    FAST_PATH_MAX_BARS = 10 if trend_expansion_active else 5

    if bars_since == 0 and not trend_expansion_active:
        # Bar 0 = displacement candle itself. Wait for pullback.
        logger.debug("⏳ FAST PATH: bar 0 — waiting for pullback")
        return None

    if bars_since > FAST_PATH_MAX_BARS:
        if current_bos.force_direct_entry:
            current_bos.force_direct_entry = False
            mode_str = "EXPANSION" if trend_expansion_active else "RANGE"
            logger.warning(
                f"🚂 FAST PATH MISSED | bars={bars_since} > {FAST_PATH_MAX_BARS} ({mode_str}) | "
                f"Deferring to normal pipeline"
            )
            engine._current_trade_tags['displacement_fast_path_missed'] = True

        if not trend_expansion_active:
            # In RANGE mode with bars > 10: close entry window
            if bars_since > 10:
                current_bos.entry_window_open = False
        return None

    # ── GATE 7: Direction must match bias ──
    if disp_direction != ctx.bias:
        logger.warning(
            f"🚫 FAST PATH: Direction mismatch | DISPLACEMENT={disp_direction} vs BIAS={ctx.bias}"
        )
        return None

    # ── GATE 8: 60d percentile extreme guard ──
    location_ctx = getattr(engine, '_location_context', None)
    if location_ctx:
        pct_60d = location_ctx.percentile_60d
        if disp_direction == 'SHORT' and (location_ctx.in_extreme_low or pct_60d <= 10):
            logger.warning(f"🚫 FAST PATH BLOCKED: EXTREME OVERSOLD (pct_60d={pct_60d:.1f}%)")
            return None
        if disp_direction == 'LONG' and (location_ctx.in_extreme_high or pct_60d >= 90):
            logger.warning(f"🚫 FAST PATH BLOCKED: EXTREME OVERBOUGHT (pct_60d={pct_60d:.1f}%)")
            return None

    # Store swing range position for analysis (non-blocking)
    fp_swing_high = getattr(engine, '_last_swing_high', 0)
    fp_swing_low = getattr(engine, '_last_swing_low', 0)
    if fp_swing_high > 0 and fp_swing_low > 0 and fp_swing_high > fp_swing_low:
        fp_range = fp_swing_high - fp_swing_low
        fp_range_pos = (current_price - fp_swing_low) / fp_range
        engine._current_trade_tags['fast_path_range_position'] = fp_range_pos

    # ── GATE 9: Not in cooldown, not in position ──
    if engine.state_machine.is_in_cooldown():
        logger.debug("🚫 FAST PATH SKIP: In cooldown")
        return None

    if engine.state_machine.state != BotState.IDLE:
        return None

    # ── SCORE CALCULATION (informative, for sizing and minimum gate) ──
    fast_path_score = 0.0
    fast_path_breakdown = {}

    try:
        entry_scorer = getattr(engine, '_entry_scorer', None)
        if entry_scorer and ctx.df_1h is not None and ctx.df_5m is not None and ctx.df_1m is not None:
            expansion_mode_active = engine._current_trade_tags.get('expansion_mode_activated', False)
            effective_adx = engine._current_trade_tags.get('effective_adx', 0) or engine._current_trade_tags.get('htf_adx', 0)

            impulse_start_price: float | None = None
            impulse_age_bars: int = 0
            if edge_mgr:
                impulse_start_price = edge_mgr.get_edge_origin_price()
                edge_age_m = edge_mgr.get_edge_age_minutes(market_time)
                impulse_age_bars = int(edge_age_m / 5.0)

            score, breakdown, structure_blocked = entry_scorer.calculate_entry_score(
                df_1h=ctx.df_1h,
                df_5m=ctx.df_5m,
                df_1m=ctx.df_1m,
                bias=disp_direction,
                current_price=current_price,
                market_time=market_time,
                micro_trend_state=getattr(engine, '_micro_trend_state', None),
                filter_pipeline=getattr(engine, '_entry_filter_pipeline', None),
                displacement_edge_active=True,
                expansion_mode_active=expansion_mode_active,
                strong_trend_adx=effective_adx,
                impulse_start_price=impulse_start_price,
                impulse_age_bars=impulse_age_bars,
            )
            fast_path_score = score
            fast_path_breakdown = breakdown

            engine._current_trade_tags['entry_score'] = fast_path_score
            engine._current_trade_tags['score_breakdown'] = fast_path_breakdown
            for key, val in fast_path_breakdown.items():
                if key.startswith('score_') or key in ('structure', 'timing', 'momentum', 'context', 'synergy'):
                    engine._current_trade_tags[f'score_{key}' if not key.startswith('score_') else key] = val
            engine._entry_score = fast_path_score
            engine._score_breakdown = fast_path_breakdown

            logger.info(f"📊 FAST PATH SCORE: {fast_path_score:.1f} | breakdown={fast_path_breakdown}")
        else:
            engine._current_trade_tags['entry_score'] = 0
            engine._entry_score = 0
            engine._score_breakdown = {}
    except Exception as e:
        logger.warning(f"⚠️ FAST PATH: Score error: {e}")
        engine._current_trade_tags['entry_score'] = 0
        engine._entry_score = 0
        engine._score_breakdown = {}

    # ── GATE 10: SCORE MINIMUM (7.0, no exceptions) ──
    from src.config.unified_constants import SCORES

    FAST_PATH_MIN_SCORE = SCORES.FAST_PATH_MIN_SCORE
    if fast_path_score < FAST_PATH_MIN_SCORE:
        logger.warning(
            f"🛑 FAST PATH BLOCKED: Score {fast_path_score:.1f} < {FAST_PATH_MIN_SCORE} | "
            f"breakdown={engine._score_breakdown}"
        )
        return None

    # ── GATE 11: MOMENTUM == 0 → use normal pipeline ──
    momentum_score = fast_path_breakdown.get('momentum', 1)
    if momentum_score == 0:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: MOMENTUM=0 | Score={fast_path_score:.1f}"
        )
        engine._current_trade_tags['fast_path_blocked_no_momentum'] = True
        return None

    # ── GATE 12: DISPLACEMENT CONSUMED (120s cooldown after trade exit) ──
    last_exit = getattr(engine, '_last_trade_exit_time', None)
    if last_exit is not None:
        elapsed = (datetime.now() - last_exit).total_seconds()
        if elapsed < 120:
            logger.warning(
                f"🚫 DISPLACEMENT CONSUMED: Last trade exited {elapsed:.0f}s ago (<120s)"
            )
            return None

    # ── GATE 13: EXHAUSTION DETECTION — RSI + VWAP distance ──
    # Un scalper NUNCA entra en el techo/piso de un movimiento.
    # Si RSI está en extremo Y precio lejos de VWAP → exhaustion → esperar pullback.
    _exhaustion_blocked = False
    candles_5m_ex = engine.market_data.get_candles('5m')
    df_5m_ex = pd.DataFrame(candles_5m_ex) if candles_5m_ex and len(candles_5m_ex) >= 20 else None

    if df_5m_ex is not None and len(df_5m_ex) >= 20:
        closes_5m = df_5m_ex['close'].to_numpy()
        rsi_vals = calculate_rsi(closes_5m, 14)
        rsi_now = float(rsi_vals[-1]) if len(rsi_vals) > 0 and not pd.isna(rsi_vals[-1]) else 50.0
        engine._current_trade_tags['fast_path_rsi_5m'] = rsi_now

        # RSI gate: LONG above 70 = overbought, SHORT below 30 = oversold
        RSI_OVERBOUGHT = 70
        RSI_OVERSOLD = 30
        if disp_direction == 'LONG' and rsi_now > RSI_OVERBOUGHT:
            _exhaustion_blocked = True
            logger.warning(
                f"🚫 EXHAUSTION BLOCK: RSI(14)={rsi_now:.1f} > {RSI_OVERBOUGHT} → "
                f"LONG at overbought, waiting for pullback"
            )
        elif disp_direction == 'SHORT' and rsi_now < RSI_OVERSOLD:
            _exhaustion_blocked = True
            logger.warning(
                f"🚫 EXHAUSTION BLOCK: RSI(14)={rsi_now:.1f} < {RSI_OVERSOLD} → "
                f"SHORT at oversold, waiting for pullback"
            )

        # VWAP distance gate: price > 2 ATR from VWAP = overextended
        if not _exhaustion_blocked and 'volume' in df_5m_ex.columns:
            from src.shared.indicators import calculate_vwap
            try:
                vwap_series = calculate_vwap(df_5m_ex)
                if len(vwap_series) > 0:
                    vwap_now = float(vwap_series.iloc[-1])
                    atr_5m_vals_ex = calculate_atr(
                        df_5m_ex['high'].to_numpy(), df_5m_ex['low'].to_numpy(),
                        df_5m_ex['close'].to_numpy(), 14
                    )
                    atr_5m_ex = float(atr_5m_vals_ex[-1]) if len(atr_5m_vals_ex) > 0 else atr_1m * 5
                    vwap_dist = abs(current_price - vwap_now)
                    vwap_dist_atr = vwap_dist / atr_5m_ex if atr_5m_ex > 0 else 0
                    engine._current_trade_tags['fast_path_vwap_dist_atr'] = vwap_dist_atr

                    VWAP_MAX_DIST_ATR = 2.0
                    price_above_vwap = current_price > vwap_now
                    if disp_direction == 'LONG' and price_above_vwap and vwap_dist_atr > VWAP_MAX_DIST_ATR:
                        _exhaustion_blocked = True
                        logger.warning(
                            f"🚫 EXHAUSTION BLOCK: Price ${current_price:.0f} is {vwap_dist_atr:.1f} ATR "
                            f"above VWAP ${vwap_now:.0f} (>{VWAP_MAX_DIST_ATR} ATR) → overextended LONG"
                        )
                    elif disp_direction == 'SHORT' and not price_above_vwap and vwap_dist_atr > VWAP_MAX_DIST_ATR:
                        _exhaustion_blocked = True
                        logger.warning(
                            f"🚫 EXHAUSTION BLOCK: Price ${current_price:.0f} is {vwap_dist_atr:.1f} ATR "
                            f"below VWAP ${vwap_now:.0f} (>{VWAP_MAX_DIST_ATR} ATR) → overextended SHORT"
                        )
            except Exception as e:
                logger.debug(f"VWAP calculation error in fast path: {e}")

    if _exhaustion_blocked:
        engine._current_trade_tags['fast_path_exhaustion_blocked'] = True
        return None

    # ── GATE 14: PULLBACK READINESS — require 30% retrace (not 15%) ──
    # Un scalper necesita un pullback real, no un wick de $30.
    MIN_PULLBACK_PCT = 0.30
    if bars_since >= 1 and not trend_expansion_active:
        _pb_origin = current_bos.origin_price or 0
        _pb_disp = current_bos.initial_displacement_amount or 0

        if _pb_disp > 0 and _pb_origin > 0:
            if disp_direction == 'LONG':
                _pb_endpoint = _pb_origin + _pb_disp
                _pb_retrace = _pb_endpoint - current_price
            else:
                _pb_endpoint = _pb_origin - _pb_disp
                _pb_retrace = current_price - _pb_endpoint

            _pb_frac = _pb_retrace / _pb_disp if _pb_disp > 0 else 0

            if _pb_frac < MIN_PULLBACK_PCT:
                _last_pb_log = getattr(engine, '_last_pullback_wait_log', 0)
                _now_pb = _time_mod.time()
                if _now_pb - _last_pb_log >= 30:
                    engine._last_pullback_wait_log = _now_pb
                    logger.info(
                        f"⏳ PULLBACK WAIT: retrace {_pb_frac:.0%} < {MIN_PULLBACK_PCT:.0%} | "
                        f"Endpoint=${_pb_endpoint:.0f} Current=${current_price:.0f}"
                    )
                return None
            else:
                logger.info(
                    f"✅ PULLBACK DETECTED: retrace {_pb_frac:.0%} ≥ {MIN_PULLBACK_PCT:.0%} | "
                    f"Endpoint=${_pb_endpoint:.0f} → Current=${current_price:.0f}"
                )
                engine._current_trade_tags['pullback_retrace_pct'] = _pb_frac

    # ═══════════════════════════════════════════════════════════════════════
    # FAST PATH ACTIVATED — ENTER NOW
    # ═══════════════════════════════════════════════════════════════════════
    origin_log = current_bos.origin_price or 0
    disp_log = current_bos.initial_displacement_amount or 0
    logger.warning(
        f"🚀🚀🚀 DISPLACEMENT FAST PATH ACTIVATED 🚀🚀🚀 | "
        f"bars={bars_since} | {disp_direction} | origin=${origin_log:.0f} | "
        f"disp=${disp_log:.0f} | score={fast_path_score:.1f}"
    )

    # ── SIZING: score-proportional ──
    base_risk_pct = 0.01
    if fast_path_score >= 9:
        fast_path_size_mult = 0.60
    elif fast_path_score >= 8:
        fast_path_size_mult = 0.50
    elif fast_path_score >= 7:
        fast_path_size_mult = 0.40
    else:
        fast_path_size_mult = 0.30

    engine._current_trade_tags['fast_path_size_mult'] = fast_path_size_mult
    engine._current_trade_tags['fast_path_size_reason'] = f'score={fast_path_score:.1f}'

    # ── STOP LOSS: structural, based on displacement origin ──
    origin_price_sl = current_bos.origin_price
    initial_disp = current_bos.initial_displacement_amount or (ctx.atr_5m * 2)
    sl_buffer = initial_disp * 0.10

    if disp_direction == 'LONG':
        stop_loss = origin_price_sl - sl_buffer
        min_sl = current_price - (ctx.atr_5m * 1.5)
        stop_loss = min(stop_loss, min_sl)
    else:
        stop_loss = origin_price_sl + sl_buffer
        max_sl = current_price + (ctx.atr_5m * 1.5)
        stop_loss = max(stop_loss, max_sl)

    risk = abs(current_price - stop_loss)

    # ── TAKE PROFIT: regime-aware ──
    current_regime = getattr(engine, '_current_trade_regime', 'RANGE')
    from src.domain.risk.risk_engine import normalize_regime_mode
    normalized_regime = normalize_regime_mode(current_regime)
    tp_r = 0.30 if normalized_regime == 'RANGE' else 2.0
    if disp_direction == 'LONG':
        take_profit = current_price + (risk * tp_r)
    else:
        take_profit = current_price - (risk * tp_r)

    # ── SIZE CALCULATION ──
    risk_engine = getattr(engine, 'risk_engine', None)
    account_balance = getattr(risk_engine, 'capital', 10000) if risk_engine else 10000
    risk_amount = account_balance * base_risk_pct * fast_path_size_mult

    size = risk_amount / risk if risk > 0 else 0
    size = round_size_to_exchange(size)

    if size < KRAKEN_BTC_MIN_SIZE:
        logger.warning(
            f"🚫 FAST PATH: Size {size:.6f} < min {KRAKEN_BTC_MIN_SIZE}"
        )
        return EntryResult(success=False, reason="Size below minimum for fast path")

    logger.warning(
        f"🚀 FAST PATH ENTRY | {disp_direction} {size:.6f} BTC @ ${current_price:.2f} | "
        f"SL=${stop_loss:.2f} ({risk:.2f} risk) | TP=${take_profit:.2f} ({tp_r}R) | "
        f"Size mult: {fast_path_size_mult:.0%} [IOC]"
    )

    # ── EXECUTE: IOC/Aggressive (taker) ──
    engine._current_trade_tags['entry_type'] = 'FAST_PATH_DISPLACEMENT'
    engine._current_trade_tags['fast_path_bars_since'] = bars_since
    engine._current_trade_tags['fast_path_origin'] = origin_price_sl
    engine._current_trade_tags['fast_path_order_type'] = 'IOC_AGGRESSIVE'

    from src.domain.trading.strategy.pullback_short import TradeType

    result = EntryResult()

    order_result = await engine.execution.place_aggressive_order(
        symbol=engine.symbol,
        side='buy' if disp_direction == 'LONG' else 'sell',
        amount=size,
        price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit
    )

    if order_result and order_result.get('filled', 0) > 0:
        result.success = True
        result.order_id = order_result.get('id')
        fill_price = float(order_result.get('average', current_price))
        fill_size = float(order_result.get('filled', size))

        logger.warning(
            f"✅ FAST PATH IOC FILLED | ID: {result.order_id} | "
            f"{fill_size:.6f} @ ${fill_price:.2f}"
        )

        current_bos.force_direct_entry = False
        engine.pending_order_id = result.order_id
        engine.entry_order_time = market_time
        if result.order_id:
            engine.reconciler.notify_order_placed(result.order_id)

        await _finalize_entry(
            engine=engine,
            direction=disp_direction,
            fill_price=fill_price,
            fill_size=fill_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr_1m=atr_1m,
            market_time=market_time,
            score=fast_path_score,
            breakdown=fast_path_breakdown,
            order_id=result.order_id,
            fill_type='TAKER',
            set_exchange_stops=True,
            transition_reason="Fast path IOC filled",
            entry_type='FAST_PATH_DISPLACEMENT',
            metadata={'fast_path': True, 'bars_since': bars_since},
        )
    else:
        result.success = False
        result.reason = "IOC not filled - price gap too large or no liquidity"
        current_bos.force_direct_entry = False
        logger.warning("🚫 FAST PATH IOC NOT FILLED | Reason: spread too wide or slippage")

    return result
