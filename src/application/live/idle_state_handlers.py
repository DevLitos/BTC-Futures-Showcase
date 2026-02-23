"""
IDLE State Handlers Module - Extracted from _handle_idle_state.

This module contains the helper functions for IDLE state processing,
extracted from trading_app.py as part of AUDIT REFACTOR Point 5.

Each handler is a focused unit that can be tested independently.
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING

from src.shared.indicators import calculate_atr, calculate_adx, calculate_ema
from src.config.trading_config import DailyRegimeDetector
from src.domain.trading.anticipation import AnticipationSignal, AnticipationState

# FIX #116: Throttle for ANTICIPATION preserved log
_last_anticipation_preserved_log: dict = {"direction": None, "time": None}
_ANTICIPATION_PRESERVED_LOG_INTERVAL = 60  # seconds

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

logger = logging.getLogger(__name__)


# ==============================================================================
# REJECTION COOLDOWN CHECK
# ==============================================================================

def check_rejection_cooldown(engine: "SignalEngine") -> Tuple[bool, str]:
    """
    Check if bot is in rejection cooldown.
    
    Returns:
        (in_cooldown, reason) tuple
    """
    return engine._is_in_rejection_cooldown()


# ==============================================================================
# EVENT-BASED EVALUATION GATE (Jan 28, 2026)
# ==============================================================================
# 
# PROBLEMA ANTERIOR:
# - Sistema solo evaluaba una vez por vela de 1m (60+ segundos de latencia)
# - Edges intraday viven en segundos, no en minutos
# - "Timeframe fantasma": datos en ms pero razonamiento en minutos
#
# SOLUCIÓN:
# - Event-based: evalúa cuando hay un EVENTO significativo
# - Max ticks fallback: evalúa cada N ticks aunque no haya evento
# - Candle close: siempre evalúa en cierre de vela (backward compatible)
#
# EVENTOS QUE DISPARAN EVALUACIÓN:
# 1. Nueva vela de 1m cerrada
# 2. Movimiento de precio >= 0.3 * ATR desde última evaluación
# 3. Max ticks alcanzado (30 ticks sin evento)
# ==============================================================================

# Configuration for event-based evaluation
# 🔧 FIX #129 (Feb 09, 2026): Increased PRICE_MOVE_ATR_THRESHOLD from 0.3 to 0.5
# - 0.3 ATR was triggering too many evaluations on minor price wiggles
# - 0.5 ATR = ~$38 on typical $76 ATR = meaningful moves only
EVENT_GATE_CONFIG = {
    "MAX_TICKS_WITHOUT_EVAL": 30,        # Evaluate every 30 ticks max
    "PRICE_MOVE_ATR_THRESHOLD": 0.5,     # 🔧 FIX #129: Was 0.3, now 0.5 ATR
    "MIN_TICKS_BETWEEN_EVAL": 5,         # Minimum 5 ticks between evaluations
    "LOG_FREQUENCY": 30,                 # Log every N ticks
}


def check_candle_gate(
    engine: "SignalEngine",
    setup_state: str | None,
    idle_eval_count: int,
    current_price: float,
    atr_1m: float = 0.0,
) -> Tuple[bool, int]:
    """
    Event-based evaluation gate for intraday trading.
    
    Evaluates when:
    1. New 1m candle closed (candle-based, always)
    2. Significant price movement (>= 0.3 ATR since last eval)
    3. Max ticks reached (fallback every 30 ticks)
    
    Args:
        engine: SignalEngine instance
        setup_state: Current setup state
        idle_eval_count: Current idle evaluation count
        current_price: Current market price
        atr_1m: Current 1m ATR (for price movement detection)
    
    Returns:
        (should_evaluate, new_eval_count) tuple
    """
    new_count = idle_eval_count + 1
    
    # =========================================================================
    # 1. NEW CANDLE: Always evaluate on candle close (backward compatible)
    # =========================================================================
    if engine._is_new_candle():
        engine._mark_candle_evaluated()
        _update_last_eval_price(engine, current_price)
        
        # 🔥 NEW (Feb 04, 2026): Track candles since startup for warmup
        engine._candles_since_start = getattr(engine, '_candles_since_start', 0) + 1
        
        # FIX #84: Reducido a DEBUG para eliminar spam
        logger.debug(f"🕯️ NEW CANDLE CLOSE → EVALUATING | Price: ${current_price:.2f} | ATR: {atr_1m:.2f}")
        return True, new_count
    
    # =========================================================================
    # 3. MINIMUM COOLDOWN: Prevent over-evaluation
    # =========================================================================
    ticks_since_last_eval = getattr(engine, '_ticks_since_last_eval', 0) + 1
    engine._ticks_since_last_eval = ticks_since_last_eval
    
    if ticks_since_last_eval < EVENT_GATE_CONFIG["MIN_TICKS_BETWEEN_EVAL"]:
        # Too soon since last evaluation
        return False, new_count
    
    # =========================================================================
    # 4. PRICE MOVEMENT EVENT: Evaluate if significant price move
    # =========================================================================
    # 🔥 FIX #240 (Feb 15, 2026): ADAPTIVE THRESHOLD DURING ACTIVE EDGE
    # When an edge is active, lower the threshold to 0.2 ATR and max ticks to 10.
    # This gives 3× more evaluation opportunities during live setups.
    # Without active edge: 0.5 ATR threshold, 30 ticks max (normal).
    edge_mgr = getattr(engine, '_edge_manager', None)
    has_active_edge = False
    if edge_mgr:
        from src.domain.trading.edge_state_manager import EdgeState
        has_active_edge = edge_mgr.state in (EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL)
    
    price_threshold_mult = 0.2 if has_active_edge else EVENT_GATE_CONFIG["PRICE_MOVE_ATR_THRESHOLD"]
    max_ticks = 10 if has_active_edge else EVENT_GATE_CONFIG["MAX_TICKS_WITHOUT_EVAL"]
    
    if atr_1m > 0:
        last_eval_price = getattr(engine, '_last_eval_price', current_price)
        price_delta = abs(current_price - last_eval_price)
        threshold = atr_1m * price_threshold_mult
        
        if price_delta >= threshold:
            direction = "↑" if current_price > last_eval_price else "↓"
            _update_last_eval_price(engine, current_price)
            engine._ticks_since_last_eval = 0
            logger.debug(
                f"[UP] PRICE EVENT {direction} → EVALUATING | "
                f"Move: ${price_delta:.2f} (threshold: ${threshold:.2f}) | "
                f"Price: ${last_eval_price:.2f} → ${current_price:.2f}"
                f"{' | 🔥 ACTIVE EDGE fast-eval' if has_active_edge else ''}"
            )
            return True, new_count
    
    # =========================================================================
    # 5. MAX TICKS FALLBACK: Evaluate every N ticks regardless
    # =========================================================================
    if ticks_since_last_eval >= max_ticks:
        _update_last_eval_price(engine, current_price)
        engine._ticks_since_last_eval = 0
        logger.debug(
            f"⏰ MAX TICKS ({max_ticks}) → EVALUATING | "
            f"Price: ${current_price:.2f} | ATR: {atr_1m:.2f}"
            f"{' | 🔥 ACTIVE EDGE fast-eval' if has_active_edge else ''}"
        )
        return True, new_count
    
    # =========================================================================
    # 6. NO EVENT: Skip evaluation (log rate-limited)
    # =========================================================================
    # FIX #84: Eliminado log de IDLE: Waiting - no aporta nada
    # Solo logear en DEBUG si realmente se necesita diagnóstico
    
    return False, new_count


def _update_last_eval_price(engine: "SignalEngine", price: float):
    """Update the last evaluated price on engine."""
    engine._last_eval_price = price


# ==============================================================================
# PENDING ORDER / POSITION CHECKS
# ==============================================================================

def check_pending_state_conflicts(engine: "SignalEngine") -> Optional[str]:
    """
    Check if there's a pending order or position that conflicts with IDLE state.
    
    Returns:
        State to transition to, or None if OK to proceed
    """
    from src.domain.trading.state.state_machine import BotState
    
    if engine.pending_order_id:
        logger.critical(f"⛔ IDLE STATE ABORT: Pending order {engine.pending_order_id} exists")
        return "WAIT_FILL"
    
    if engine.risk_engine.has_open_position():
        logger.critical(f"⛔ IDLE STATE ABORT: Open position exists")
        return "MANAGE"
    
    return None


# ==============================================================================
# EXCHANGE SYNC
# ==============================================================================

async def sync_with_exchange_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    sync_interval_seconds: float = 30.0,
) -> Tuple[bool, Optional[str]]:
    """
    Sync with exchange periodically to detect orphaned positions.
    
    Returns:
        (should_return_early, transition_state) tuple
    """
    from src.domain.trading.state.state_machine import BotState
    
    if engine._last_exchange_sync is None:
        engine._last_exchange_sync = market_time
    
    time_since_sync = (market_time - engine._last_exchange_sync).total_seconds()
    if time_since_sync <= sync_interval_seconds:
        return False, None
    
    # Perform sync
    try:
        async with asyncio.timeout(10):
            async with engine._sync_lock:
                try:
                    engine._syncing_position = True
                    
                    exchange_has_position = await engine.execution.has_open_position(engine.symbol)
                    if exchange_has_position:
                        logger.critical("🔄 IDLE but exchange has position - syncing state...")
                        
                        # Get ATR for proper SL calculation
                        candles_5m = engine.market_data.get_candles('5m')
                        atr_5m = None
                        if candles_5m:
                            df_5m = pd.DataFrame(candles_5m)
                            atr_5m = engine._calculate_atr_5m(df_5m)
                        
                        exchange_position = await engine.execution.get_position(engine.symbol)
                        if exchange_position:
                            await engine.risk_engine.update_from_exchange_position(
                                exchange_position, atr=atr_5m
                            )
                        
                        return True, "MANAGE"
                    
                    engine._last_exchange_sync = market_time
                    
                except Exception as e:
                    logger.error(f"Error during exchange sync in IDLE: {e}")
                finally:
                    engine._syncing_position = False
    except asyncio.TimeoutError:
        logger.warning("Exchange sync lock acquisition timeout - will retry next cycle")
    
    return False, None


# ==============================================================================
# ANTICIPATION ENGINE CHECK
# ==============================================================================

def check_anticipation_signal(
    engine: "SignalEngine",
    current_price: float,
    market_time: datetime,
) -> Dict[str, Any]:
    """
    Check anticipation engine for early entry signals.
    
    Returns:
        Dict of trade tags to apply if anticipation detected
    """
    tags = {}
    
    try:
        candles_5m = engine.market_data.get_candles('5m')
        if not candles_5m or len(candles_5m) < 50:
            return tags
        
        df_5m = pd.DataFrame(candles_5m)
        atr_5m_arr = calculate_atr(
            df_5m['high'].to_numpy(),
            df_5m['low'].to_numpy(),
            df_5m['close'].to_numpy(),
            14
        )
        
        # Safe ATR extraction with NaN guard
        atr_5m_val = current_price * 0.005  # Default fallback
        if len(atr_5m_arr) > 0 and not np.isnan(atr_5m_arr[-1]):
            atr_5m_val = atr_5m_arr[-1]
        
        # Get HTF bias hint
        htf_bias_hint = getattr(engine, '_htf_cache_bias', None)
        
        # Get current regime
        current_regime = None
        if engine.regime_classifier and engine.regime_classifier.current_regime:
            current_regime = engine.regime_classifier.current_regime.value
        
        # =========================================================================
        # 🔥 FIX #58 (Feb 06, 2026): Usar evaluate_all() para detectar TODOS los
        # patrones de anticipación, no solo COMPRESSION_BREAKOUT
        # 
        # Patrones detectados:
        # 1. COMPRESSION_BREAKOUT - BB squeeze (original)
        # 2. TREND_PULLBACK - Pullback a EMA en tendencia
        # 3. FAILED_TEST - Rechazo de nivel clave
        # 4. MOMENTUM_EXHAUSTION - Agotamiento con divergencia
        # =========================================================================
        
        # Get ADX values for extended anticipation methods
        adx_1h_val = getattr(engine, '_last_htf_adx', 0.0)
        adx_5m_val = getattr(engine, '_last_effective_adx', 0.0)
        
        # Get important levels from location filter if available
        important_levels = None
        if hasattr(engine, '_location_filter') and engine._location_filter:
            lf = engine._location_filter
            levels = []
            if hasattr(lf, '_range_high') and lf._range_high > 0:
                levels.append(lf._range_high)
            if hasattr(lf, '_range_low') and lf._range_low > 0:
                levels.append(lf._range_low)
            if levels:
                important_levels = levels
        
        anticipation = engine.anticipation_engine.evaluate_all(
            df_5m=df_5m,
            current_price=current_price,
            atr=atr_5m_val,
            market_time=market_time,
            htf_bias=htf_bias_hint,
            important_levels=important_levels,
            regime=current_regime,
            adx_1h=adx_1h_val,
            adx_5m=adx_5m_val
        )
        
        engine._last_anticipation_state = anticipation
        
        # =========================================================================
        # 🔥 FIX (Jan 28, 2026): VERIFICAR MOMENTUM EN TIEMPO REAL
        # 🔥 FIX #25 (Feb 04): NO invalidar por ACTIVE_DIRECTION si edge EXPIRED
        # 
        # PROBLEMA ORIGINAL (Jan 28): Anticipation detecta compresión pero precio
        # ya se movió contra la dirección sugerida → entrada en peor ubicación
        #
        # PROBLEMA NUEVO (Feb 03): Anticipation (COMPRESSION_BREAKOUT) invalidada
        # por ACTIVE_DIRECTION=SHORT cuando edge está EXPIRED y no hay posición.
        # El ACTIVE_DIRECTION es "stale" (viejo) → crea dead zone.
        #
        # SOLUCIÓN MEJORADA:
        # 1. Solo invalidar si displacement_bypass está ACTIVO (no stale)
        # 2. Si edge está EXPIRED o NO_EDGE y no hay posición, ACTIVE_DIRECTION stale
        # 3. En ese caso, no invalidar ANTICIPATION
        # =========================================================================
        active_direction = getattr(engine, '_active_direction', None)
        displacement_bypass = getattr(engine, '_displacement_bypass', False)
        
        # Check if edge is still active or if it's stale
        edge_is_active = False
        direction_is_stale = False
        if hasattr(engine, '_edge_manager'):
            from src.domain.trading.edge_state_manager import EdgeState
            edge_is_active = engine._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]
            # 🔥 FIX #59 (Feb 06, 2026): Use edge_manager's stale detection
            direction_is_stale = engine._edge_manager.is_direction_stale(stale_threshold_minutes=5)
        
        # Check if we have a position (if so, ACTIVE_DIRECTION matters)
        has_position = getattr(engine, '_position', None) is not None
        
        # 🔥 FIX #25 + #59: Only invalidate if:
        # - Edge is actually active OR we have a position
        # - AND direction is NOT stale
        # If edge is EXPIRED/NO_EDGE and no position → ACTIVE_DIRECTION is stale, ignore it
        direction_is_relevant = (edge_is_active or has_position) and not direction_is_stale
        
        if displacement_bypass and active_direction and direction_is_relevant:
            # Hay displacement activo Y dirección es relevante - verificar si contradice anticipation
            if anticipation.direction and anticipation.direction != active_direction:
                logger.info(
                    f"⛔ ANTICIPATION INVALIDATED: {anticipation.direction} vs "
                    f"ACTIVE_DIRECTION={active_direction} (displacement_bypass=True, edge_active={edge_is_active}) | Ignorando señal"
                )
                anticipation = AnticipationState()  # Reset to empty
                engine._last_anticipation_state = anticipation
        elif active_direction and (not direction_is_relevant or direction_is_stale):
            # Edge is stale o no hay edge - dirección es obsoleta, permitir anticipation
            if anticipation.direction and anticipation.direction != active_direction:
                # 🔥 FIX #116 (Feb 09): Throttle log to 1 per minute per direction
                global _last_anticipation_preserved_log
                now = datetime.now()
                should_log = (
                    _last_anticipation_preserved_log["direction"] != anticipation.direction or
                    _last_anticipation_preserved_log["time"] is None or
                    (now - _last_anticipation_preserved_log["time"]).total_seconds() > _ANTICIPATION_PRESERVED_LOG_INTERVAL
                )
                if should_log:
                    _last_anticipation_preserved_log = {"direction": anticipation.direction, "time": now}
                    logger.info(
                        f"📝 ANTICIPATION preserved: {anticipation.direction} (conf={anticipation.confidence:.0%}) differs from stale "
                        f"ACTIVE_DIRECTION={active_direction} (stale={direction_is_stale}, edge={engine._edge_manager.state.value if hasattr(engine, '_edge_manager') else 'N/A'}) | "
                        f"New signal allowed (throttled: 1/min)"
                    )
        
        # =========================================================================
        # 🔥 AJUSTE (Jan 27, 2026): ANTICIPATION = información, NO trigger
        # 
        # Crítica institucional:
        # - ANTICIPATION no es un edge, es un early warning
        # - Solo debe servir para cambiar estado interno / alertas
        # - NUNCA como trigger directo de trades
        #
        # 🔥 FIX #97 (Feb 09): BAJAR umbral de 55% a 50% - anticipation acierta más de lo esperado
        # =========================================================================
        
        # Check for valid anticipation signal (confidence >= 50% - bajado de 55%)
        ANTICIPATION_MIN_CONFIDENCE = 0.50  # 🔥 FIX #97: Bajado de 0.55
        
        # =========================================================================
        # 🔥 FIX #113 (Feb 09, 2026): ANTICIPATION QUALITY GATE
        #
        # PROBLEMA (P1): ANTICIPATION spam con señales mediocres:
        # - conf=51%, ADX=0, phase=UNKNOWN → 40+ logs sin valor
        # - Estas señales NUNCA convierten porque faltan condiciones
        #
        # SOLUCIÓN: Quality gate antes de procesar:
        # - Si conf < 60% AND (ADX < 5 OR phase = UNKNOWN) → skip
        # - Señales de baja confianza sin contexto son ruido puro
        #
        # 🔥 FIX #177 (Feb 11, 2026): EXCEPCIÓN para COMPRESSION_BREAKOUT
        # PROBLEMA: El quality gate filtraba señales de COMPRESSION_BREAKOUT
        # porque durante el squeeze el ADX es típicamente bajo (<5).
        # PERO eso es PRECISAMENTE cuando queremos anticipation!
        #
        # SOLUCIÓN: Si signal == COMPRESSION_BREAKOUT, ignorar quality gate.
        # El squeeze confirmado (BB percentile bajo) ES la calidad.
        # =========================================================================
        adx_5m = getattr(engine, '_last_effective_adx', 0.0)
        trend_phase = getattr(engine, '_current_trend_phase', 'UNKNOWN')
        
        if anticipation.signal != AnticipationSignal.NONE:
            # 🔥 FIX #177: NO filtrar COMPRESSION_BREAKOUT por low ADX
            # El squeeze implica ADX bajo - eso es esperado, no ruido
            is_compression_breakout = anticipation.signal == AnticipationSignal.COMPRESSION_BREAKOUT
            
            is_low_quality = (
                anticipation.confidence < 0.60 and 
                (adx_5m < 5.0 or trend_phase == 'UNKNOWN')
            )
            
            if is_low_quality and not is_compression_breakout:
                logger.debug(
                    f"⏭️ FIX #113: ANTICIPATION skipped (low quality): conf={anticipation.confidence:.0%}, "
                    f"ADX={adx_5m:.1f}, phase={trend_phase}"
                )
                anticipation = AnticipationState()  # Reset to empty
                engine._last_anticipation_state = anticipation
        
        if anticipation.signal != AnticipationSignal.NONE and anticipation.confidence >= ANTICIPATION_MIN_CONFIDENCE:
            tags['anticipation_signal'] = anticipation.signal.value
            tags['anticipation_confidence'] = anticipation.confidence
            tags['anticipation_direction'] = anticipation.direction
            
            # 🔥 FIX #29 (Feb 04): Store structure_level for breakout trigger
            if anticipation.structure_level > 0:
                tags['anticipation_structure_level'] = anticipation.structure_level
            
            # =====================================================================
            # 🔥 FIX (Jan 28, 2026): Extraer BB percentile y volume ratio de reasons
            # para que EdgeStateManager pueda usar estos valores para BREAKOUT edge
            # =====================================================================
            if anticipation.reasons:
                for reason in anticipation.reasons:
                    # Parse "squeeze: BB width X.XX% (percentile YY)"
                    if 'squeeze' in reason.lower() and 'percentile' in reason.lower():
                        import re
                        pct_match = re.search(r'percentile\s*(\d+)', reason)
                        if pct_match:
                            tags['anticipation_bb_percentile'] = float(pct_match.group(1))
                    
                    # Parse "volume_decline: Volume ratio X.XX (Y/Z)"
                    if 'volume' in reason.lower() and 'ratio' in reason.lower():
                        import re
                        vol_match = re.search(r'ratio\s*([\d.]+)', reason)
                        if vol_match:
                            tags['anticipation_volume_ratio'] = float(vol_match.group(1))
            
            # 🔥 FIX (Feb 02, 2026): Only log anticipation here (removed duplicate in anticipation.py)
            # Also add ADX context for debugging ADX vs RANGE_BOUND conflict
            # NOTE: adx_5m and trend_phase already defined in FIX #113 quality gate above
            
            # 🔥 FIX (Feb 05, 2026): Include current price to show context changes
            # Also detect if structure is invalidated by current price
            structure_invalidated = False
            if anticipation.structure_level > 0 and anticipation.direction:
                if anticipation.direction == 'LONG' and current_price < anticipation.structure_level:
                    structure_invalidated = True
                elif anticipation.direction == 'SHORT' and current_price > anticipation.structure_level:
                    structure_invalidated = True
            
            # =================================================================
            # 🔥 FIX #302 (Feb 19, 2026): STALE BREAKOUT CHECK
            #
            # PROBLEMA: COMPRESSION_BREAKOUT detecta squeeze desde $66,117
            # pero precio ya está en $66,449 ($332 arriba). El breakout
            # ya ocurrió, la señal es stale. El bot entra en el TOPE del
            # bounce y se come el reversal completo (Trade 3: -0.32R).
            #
            # SOLUCIÓN: Si el precio ya se movió > 2.5×ATR_5m desde el
            # structure_level en la dirección del entry, el breakout ya
            # se agotó. Invalidar la señal.
            # =================================================================
            stale_breakout = False
            if (not structure_invalidated and 
                anticipation.structure_level > 0 and 
                anticipation.direction and
                anticipation.signal == AnticipationSignal.COMPRESSION_BREAKOUT):
                
                if anticipation.direction == 'LONG':
                    distance_from_structure = current_price - anticipation.structure_level
                else:
                    distance_from_structure = anticipation.structure_level - current_price
                
                atr_5m_for_stale = getattr(engine, '_last_atr_5m', 100.0)
                STALE_BREAKOUT_ATR_MULT = 2.5
                
                if distance_from_structure > STALE_BREAKOUT_ATR_MULT * atr_5m_for_stale:
                    stale_breakout = True
                    logger.warning(
                        f"⛔ FIX #302 STALE BREAKOUT: {anticipation.direction} structure @ "
                        f"${anticipation.structure_level:.0f} but price ${current_price:.0f} | "
                        f"Distance ${distance_from_structure:.0f} > "
                        f"{STALE_BREAKOUT_ATR_MULT:.1f}×ATR_5m (${atr_5m_for_stale * STALE_BREAKOUT_ATR_MULT:.0f}) | "
                        f"Breakout already played out"
                    )
                    anticipation = AnticipationState()
                    engine._last_anticipation_state = anticipation
            
            if structure_invalidated:
                # Structure broken - invalidate anticipation
                logger.warning(
                    f"⛔ ANTICIPATION INVALIDATED: {anticipation.direction} structure broken | "
                    f"Price {current_price:.0f} vs level {anticipation.structure_level:.0f}"
                )
                anticipation = AnticipationState()  # Reset to empty
                engine._last_anticipation_state = anticipation
            elif stale_breakout:
                pass  # Already handled above
            else:
                # =====================================================================
                # 🔥 FIX #107 (Feb 09, 2026): THROTTLE ANTICIPATION LOGS
                # 
                # PROBLEMA: ~40 logs de "🔮 ANTICIPATION: COMPRESSION_BREAKOUT" en pocos
                # minutos cuando detecta squeeze. Es spam que no aporta valor.
                #
                # SOLUCIÓN: Throttle a máximo 1 log por minuto por signal+direction.
                # - Guarda last log time/signal/direction en engine
                # - Solo loguea si cambió el signal o direction O pasó 1 minuto
                # =====================================================================
                should_log_anticipation = True
                log_key = f"{anticipation.signal.value}_{anticipation.direction}"
                
                if hasattr(engine, '_anticipation_last_log_time') and hasattr(engine, '_anticipation_last_log_key'):
                    time_since_last = (market_time - engine._anticipation_last_log_time).total_seconds()
                    key_changed = log_key != engine._anticipation_last_log_key
                    
                    # Only log if: signal/direction changed OR 60+ seconds passed
                    if not key_changed and time_since_last < 60:
                        should_log_anticipation = False
                
                if should_log_anticipation:
                    engine._anticipation_last_log_time = market_time
                    engine._anticipation_last_log_key = log_key
                    
                    logger.info(
                        f"🔮 ANTICIPATION: {anticipation.signal.value} {anticipation.direction} @ ${current_price:.0f} | "
                        f"conf={anticipation.confidence:.0%} | ADX={adx_5m:.1f} | phase={trend_phase}"
                    )
                    if anticipation.reasons:
                        for reason in anticipation.reasons[:3]:
                            logger.info(f"   → {reason}")
            
            # =========================================================================
            # 🔥 FIX #58 (Feb 06, 2026): RE-HABILITAR ANTICIPATION COMO TRIGGER
            # 
            # PROBLEMA: Sistema siempre detecta TARDE porque:
            # - Solo entra por BOS (ya movió 0.5-1.5%)
            # - ANTICIPATION detecta ANTES pero estaba deshabilitado
            #
            # SOLUCIÓN: Habilitar ANTICIPATION como trigger con:
            # - Umbral de confianza 65% (era 75% - muy restrictivo)
            # - Probe size 0.35x (pequeño pero útil)
            # - Solo si tiene structure_level válido (no a ciegas)
            #
            # Esto permite ENTRAR ANTES DEL MOVIMIENTO:
            # - Anticipation detecta compresión → entra pequeño
            # - Si breakout confirma → edge upgrades to ACTIVE → añade
            # - Si falla → sale con pérdida pequeña (0.35x)
            # =========================================================================
            ANTICIPATION_TRIGGER_CONFIDENCE = 0.65  # Bajado de 0.75
            ANTICIPATION_PROBE_SIZE = 0.35          # Tamaño pequeño para probar
            
            # =========================================================================
            # 🔥 FIX #59 (Feb 06, 2026): Evitar duplicación BREAKOUT edge vs ANTICIPATION
            # 
            # Si ya hay un BREAKOUT edge activo, COMPRESSION_BREAKOUT anticipation es
            # redundante - ambos detectan el mismo patrón (BB squeeze).
            # En ese caso, NO activar anticipation como trigger - el edge ya lo maneja.
            # =========================================================================
            anticipation_redundant = False
            
            # =========================================================================
            # 🔥 FIX #110 (Feb 09, 2026): ANTICIPATION FORBIDDEN CHECK
            #
            # PROBLEMA: ANTICIPATION seguía detectando SHORT después de BIAS_FLIP
            # que prohibió SHORT. Esto causa esquizofrenia sistémica.
            #
            # SOLUCIÓN: Verificar forbidden_direction ANTES de procesar anticipation.
            # Si la dirección está prohibida, resetear a estado vacío.
            # =========================================================================
            forbidden_dir = getattr(engine, '_forbidden_direction', None)
            if forbidden_dir and anticipation.direction == forbidden_dir:
                logger.debug(
                    f"⛔ ANTICIPATION {anticipation.direction} blocked by forbidden_direction"
                )
                anticipation = AnticipationState()  # Reset to empty
                engine._last_anticipation_state = anticipation
                anticipation_redundant = True  # Skip further processing
            
            if anticipation.signal == AnticipationSignal.COMPRESSION_BREAKOUT:
                if hasattr(engine, '_edge_manager'):
                    from src.domain.trading.edge_state_manager import EdgeType
                    edge_mgr = engine._edge_manager
                    if (edge_mgr.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
                        edge_mgr.current_bos and 
                        edge_mgr.current_bos.edge_type == EdgeType.BREAKOUT):
                        anticipation_redundant = True
                        logger.debug(
                            f"📝 ANTICIPATION redundant: BREAKOUT edge already {edge_mgr.state.value} | "
                            f"Edge dir={edge_mgr.current_bos.direction} vs Antic dir={anticipation.direction}"
                        )
            
            if (anticipation.confidence >= ANTICIPATION_TRIGGER_CONFIDENCE 
                and anticipation.direction
                and anticipation.structure_level > 0
                and not anticipation_redundant):  # 🔥 FIX #59: No duplicar
                
                tags['anticipation_entry'] = True
                tags['anticipation_probe_size'] = ANTICIPATION_PROBE_SIZE
                
                logger.info(
                    f"⚡ ANTICIPATION TRIGGER ENABLED: {anticipation.direction} @ ${current_price:.0f} | "
                    f"conf={anticipation.confidence:.0%} | probe={ANTICIPATION_PROBE_SIZE:.0%} | "
                    f"structure={anticipation.structure_level:.0f}"
                )
    
    except Exception as e:
        logger.warning(f"⚠️ Anticipation engine error (non-fatal): {e}")
    
    return tags


# ==============================================================================
# DAILY REGIME DETECTOR
# ==============================================================================

def update_daily_regime_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    update_interval_seconds: float = 3600.0,  # 1 hour
) -> Optional[Dict[str, Any]]:
    """
    Update daily regime classification if enough time has passed.
    
    Returns:
        New regime config dict if updated, None otherwise
    """
    candles_1h = engine.market_data.get_candles('1h')
    if not candles_1h or len(candles_1h) < 50:
        return None
    
    should_update = (
        engine._daily_regime_last_update is None or
        (market_time - engine._daily_regime_last_update).total_seconds() > update_interval_seconds
    )
    
    if not should_update:
        return None
    
    # Calculate daily indicators from 1h candles
    highs_1h = np.array([c['high'] for c in candles_1h])
    lows_1h = np.array([c['low'] for c in candles_1h])
    closes_1h = np.array([c['close'] for c in candles_1h])
    
    # Daily range (last 24 1h candles)
    daily_high = np.max(highs_1h[-24:]) if len(highs_1h) >= 24 else np.max(highs_1h)
    daily_low = np.min(lows_1h[-24:]) if len(lows_1h) >= 24 else np.min(lows_1h)
    daily_range = daily_high - daily_low
    
    # Daily ATR (from 1h data averaged)
    atr_1h = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
    daily_atr = float(atr_1h[-1] * 24) if len(atr_1h) > 0 else daily_range
    
    # Daily ADX
    adx_1h_values, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
    daily_adx = 15.0
    if len(adx_1h_values) > 0 and not np.isnan(adx_1h_values[-1]):
        daily_adx = float(adx_1h_values[-1])
    
    # Classify daily regime
    new_daily_regime = DailyRegimeDetector.classify_daily_regime(
        daily_range=daily_range,
        daily_atr=daily_atr,
        daily_adx=daily_adx
    )
    
    # Update state if changed
    if new_daily_regime != engine._daily_regime:
        old_regime = engine._daily_regime
        logger.info(f"🔄 DAILY REGIME TRANSITION: {old_regime} → {new_daily_regime}")
        logger.info(DailyRegimeDetector.log_daily_regime(
            new_daily_regime, daily_range, daily_atr, daily_adx
        ))
        
        engine._daily_regime = new_daily_regime
        # 🔥 FIX: Method is get_daily_config, not get_regime_config
        engine._daily_regime_config = DailyRegimeDetector.get_daily_config(new_daily_regime)
    
    engine._daily_regime_last_update = market_time
    return engine._daily_regime_config


# ==============================================================================
# PRE-TRADE CHECKS
# ==============================================================================

async def check_pre_trade_conditions(
    engine: "SignalEngine",
    market_time: datetime,
) -> Tuple[bool, str]:
    """
    Check conditions that must pass before evaluating trades.
    
    Returns:
        (can_trade, reason) tuple
    """
    # 🔥 FIX #87 (Feb 09, 2026): STALE DATA KILL SWITCH
    # If market data is stale >60s, trading decisions are GARBAGE.
    # ADX, ATR, Volume, Bias - ALL WRONG. Don't trade with bad data.
    from datetime import timezone
    if hasattr(engine, 'market_data') and hasattr(engine.market_data, 'get_feed_age_sec'):
        feed_age = engine.market_data.get_feed_age_sec()
        STALE_THRESHOLD_SECONDS = 60.0  # 60s = data too old
        
        if feed_age > STALE_THRESHOLD_SECONDS:
            # Don't spam logs - only log every 30 ticks when stale
            if not hasattr(engine, '_stale_data_tick_count'):
                engine._stale_data_tick_count = 0
            engine._stale_data_tick_count += 1
            
            if engine._stale_data_tick_count % 30 == 1:
                logger.warning(
                    f"🛑 STALE DATA BLOCK: Feed age {feed_age:.0f}s > {STALE_THRESHOLD_SECONDS}s | "
                    f"Indicators invalid, NO TRADING"
                )
            return False, f"DATA_STALE_{feed_age:.0f}s"
        else:
            # Reset counter when data is fresh
            engine._stale_data_tick_count = 0
    
    # 🔥 FIX (Feb 1, 2026): LOSS STREAK COOLDOWN + KILL SWITCH CHECK
    # This was MISSING! _check_loss_cooldown was defined but never called
    if hasattr(engine, '_check_loss_cooldown'):
        can_enter, loss_reason = engine._check_loss_cooldown(market_time)
        if not can_enter:
            # Log clearly when blocked by kill switch or loss cooldown
            if "KILL SWITCH" in loss_reason:
                logger.warning(f"[BLOCKED] {loss_reason}")
            else:
                # 🔥 FIX #291: Throttle LOSS COOLDOWN log (was spamming every tick)
                # Only log once per 60s instead of every evaluation
                _last_loss_cd_log = getattr(engine, '_last_loss_cooldown_log_time', None)
                _now = datetime.now()
                if _last_loss_cd_log is None or (_now - _last_loss_cd_log).total_seconds() > 60:
                    engine._last_loss_cooldown_log_time = _now
                    logger.info(f"⏸️ {loss_reason}")
            return False, loss_reason
    
    # Daily halt check
    is_halted, halt_reason = engine.state_machine.check_daily_halt()
    if is_halted:
        return False, f"Daily halt: {halt_reason}"
    
    # Exchange availability
    if hasattr(engine.execution, 'is_exchange_available') and not engine.execution.is_exchange_available:
        return False, "Exchange unavailable"
    
    # State machine check
    can_trade, reason = engine.state_machine.can_trade()
    if not can_trade:
        return False, reason
    
    # External data check
    ext_can_trade, ext_reason = engine.external_data.can_trade()
    if not ext_can_trade:
        return False, f"External data: {ext_reason}"
    
    return True, "OK"


async def refresh_balance_if_needed(
    engine: "SignalEngine",
    market_time: datetime,
    refresh_interval_seconds: float = 300.0,  # 5 minutes
):
    """Refresh account balance periodically."""
    if engine._last_balance_check is None:
        engine._last_balance_check = market_time
    
    if (market_time - engine._last_balance_check).total_seconds() <= refresh_interval_seconds:
        return
    
    try:
        balance = await engine.execution.get_account_balance()
        if balance:
            await engine.risk_engine.update_capital_from_balance(balance)
        engine._last_balance_check = market_time
    except Exception as e:
        logger.warning(f"Balance check error: {e}")
