"""
Displacement Fast Path - Fast entry path for displacement edge events.

This handles the immediate entry logic when a displacement (strong directional move)
is detected. The system must enter within 0-1 candles or cancel.

Extracted from entry_orchestrator.py for maintainability (~880 lines).
"""

import logging
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




# =========================================================================
# 🚀 DISPLACEMENT FAST PATH (Jan 30, 2026)
# =========================================================================
# Este es el pipeline URGENTE para displacement.
# Bypasea TODA la ceremonia de validación normal.
# 
# Filosofía: "Prefiero entrar feo que perder el tren"
# 
# Regla brutal:
#   Primer displacement válido = entrada inmediata o cancelación total
#   Si no puedes entrar dentro de 0-1 velas, NO es trade de displacement
# =========================================================================

async def _displacement_fast_path(
    engine: 'SignalEngine',
    ctx: 'EntryContext', 
    current_price: float,
    atr_1m: float,
    market_time: datetime
) -> Optional[EntryResult]:
    """
    FAST PATH para DISPLACEMENT - bypasea validación normal.
    
    🔥 FIX #160 (Feb 10): MODE_EXPANSION elimina frenos por barras
    En expansión de tendencia, no hay "chase block" por número de velas.
    Solo se bloquea si la ESTRUCTURA se invalida (precio cruza origen).
    
    Returns:
        EntryResult si debe ejecutar fast path
        None si debe continuar con pipeline normal
    """
    from src.domain.trading.edge_state_manager import EdgeState, EdgeType, DisplacementEdgeCriteria
    
    # =====================================================================
    # CHECK 1: ¿Hay DISPLACEMENT EDGE activo con entrada directa?
    # =====================================================================
    edge_mgr = getattr(engine, '_edge_manager', None)
    if not edge_mgr:
        return None
    
    # Solo actuar si DISPLACEMENT está ACTIVE
    if edge_mgr.state != EdgeState.EDGE_ACTIVE:
        return None
    
    current_bos = edge_mgr.current_bos
    if not current_bos:
        return None
    
    # Solo para DISPLACEMENT edge type
    if current_bos.edge_type != EdgeType.DISPLACEMENT:
        return None
    
    # =====================================================================
    # RANGE MODE: Skip fast path but DON'T expire edge
    # Let normal pipeline handle displacement in range with proper scoring.
    # Previously this expired the edge → normal pipeline had no edge → 0 trades.
    # =====================================================================
    from src.domain.market.regime.trend_intelligence import TrendPhase
    current_trend_phase = getattr(engine, '_trend_phase', None)
    current_trading_mode = getattr(engine, '_current_trade_tags', {}).get('trading_mode', '')
    if current_trend_phase == TrendPhase.RANGE_BOUND or current_trading_mode == 'RANGE':
        logger.info(
            f"⏭️ FAST PATH SKIP: RANGE mode → normal pipeline will evaluate | "
            f"TrendPhase={current_trend_phase} | Edge preserved for scoring"
        )
        return None
    
    # =====================================================================
    # 🔥 DIRECTION MEMORY: Block same-direction fast path for 30 minutes
    # Feb 19 evidence: TP hit → 66s displacement re-entry at bottom → instant SL
    # After ANY trade close, block same direction displacement for 30 min.
    # Opposite direction allowed after 3 min (fresh opportunity).
    # =====================================================================
    disp_direction = current_bos.direction
    _SAME_DIR_BLOCK_S = 1800  # 30 minutes
    _ANY_DIR_BLOCK_S = 180    # 3 minutes
    _last_dir = getattr(engine, '_last_trade_direction', None)
    _last_exit = getattr(engine, '_last_trade_exit_time', None)
    if _last_exit is not None:
        from datetime import datetime as _dt
        _elapsed = (_dt.now() - _last_exit).total_seconds()
        _same_dir = (_last_dir == disp_direction and _elapsed < _SAME_DIR_BLOCK_S)
        _any_block = (_elapsed < _ANY_DIR_BLOCK_S)
        if _same_dir or _any_block:
            _block_type = "SAME DIR 30min" if _same_dir else "CONSUMED 3min"
            _remaining = (_SAME_DIR_BLOCK_S if _same_dir else _ANY_DIR_BLOCK_S) - _elapsed
            logger.info(
                f"🚫 FAST PATH BLOCKED: DIRECTION MEMORY ({_block_type}) | "
                f"Last={_last_dir} {_elapsed:.0f}s ago | "
                f"Displacement={disp_direction} | Wait {_remaining:.0f}s"
            )
            return None
    
    # =====================================================================
    # 🔥 GUARD 0 CHECK: DAILY LIMIT — CANNOT BE BYPASSED (Feb 19, 2026)
    # The fast path was BEFORE GuardOrchestrator.run_all_guards(), so
    # HARD_DAILY_LIMIT was NEVER checked for displacement entries.
    # With $231 capital, every unnecessary trade can kill the project.
    # =====================================================================
    HARD_DAILY_LIMIT = 5
    daily_trades = getattr(engine.state_machine, 'daily_trades', 0)
    if daily_trades >= HARD_DAILY_LIMIT:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: DAILY LIMIT {daily_trades}/{HARD_DAILY_LIMIT} (HARD CAP) | "
            f"No displacement entries allowed — daily limit reached"
        )
        return EntryResult(success=False, reason=f"DAILY LIMIT: {daily_trades}/{HARD_DAILY_LIMIT} (fast path)")
    
    bars_since = current_bos.bars_since_displacement
    
    # =====================================================================
    # 🔥 FIX #186 (Feb 11, 2026): STALE + CHASE BLOCK for DISPLACEMENT
    # =====================================================================
    # PROBLEMA CRÍTICO: DISPLACEMENT bypasea TODAS las protecciones incluyendo:
    #   1. STALE SIGNAL (-1.5 penalty) → Un edge de 40 min igual que de 1 min
    #   2. CHASE detection → Precio ya subió $1,300 antes de entrar
    #   3. CONSENSUS BLOCKED → "LONG vs BEAR_STRONG" ignorado
    #
    # Caso real Feb 11: 
    #   - 14:32:05: Edge LONG @ $67,218
    #   - 14:35 → 15:10: BLOCKED por BEAR_STRONG (39 min)
    #   - 15:11:03: DISPLACEMENT detected, bypasea TODO
    #   - 15:11:04: Entry LONG @ $68,507 (top of 2% move)
    #   - Resultado: -0.32R
    #
    # SOLUCIÓN:
    #   - Edge age > 15 min → BLOCK (no solo penalty)
    #   - Price moved > 1.5 ATR en dirección del edge → CHASE BLOCK
    # =====================================================================
    
    # CHECK 1.5a: STALE BLOCK - No entrar con edge de más de 15 min
    MAX_EDGE_AGE_MINUTES = 15.0
    edge_age_min = edge_mgr.get_edge_age_minutes()
    if edge_age_min > MAX_EDGE_AGE_MINUTES:
        logger.warning(
            f"🚫 FIX #186 STALE BLOCK: DISPLACEMENT edge age {edge_age_min:.1f}min > {MAX_EDGE_AGE_MINUTES}min | "
            f"Edge too old - not chasing. FAST PATH BLOCKED."
        )
        # Expirar el edge para no seguir intentando
        edge_mgr._expire_edge(reason=f"STALE_BLOCK:age={edge_age_min:.1f}min")
        return None
    
    # CHECK 1.5b: CHASE BLOCK - No entrar si precio ya se movió > 1.5 ATR
    CHASE_THRESHOLD_ATR = 1.5
    disp_direction = current_bos.direction
    origin_price = current_bos.displacement_origin if hasattr(current_bos, 'displacement_origin') else current_bos.price
    
    if origin_price and atr_1m > 0:
        if disp_direction == 'LONG':
            price_moved = current_price - origin_price
        else:
            price_moved = origin_price - current_price
        
        chase_threshold = CHASE_THRESHOLD_ATR * atr_1m
        if price_moved > chase_threshold:
            logger.warning(
                f"🚫 FIX #186 CHASE BLOCK: Price moved ${price_moved:.0f} > ${chase_threshold:.0f} (1.5 ATR) | "
                f"Origin ${origin_price:.0f} → Current ${current_price:.0f} | "
                f"FAST PATH BLOCKED - would chase the move."
            )
            # NO expirar edge - solo bloquear fast path, dejar que pullback pipeline evalúe
            current_bos.force_direct_entry = False
            return None
    
    # =====================================================================
    # 🔥 FIX #160 (Feb 10): MODE_EXPANSION vs MODE_RANGE
    # 
    # PROBLEMA CRÍTICO: El sistema trataba TODOS los trades igual.
    # En RANGE: cooldowns, waits, chase blocks → correcto
    # En EXPANSION: los mismos frenos → DESTRUYE el edge
    #
    # SOLUCIÓN: Dos modos de operación completamente diferentes:
    #
    # MODE_RANGE (trend_expansion_active=False):
    #   - FAST_PATH_MAX_BARS = 3 (entrada rápida)
    #   - bars 4-10: Zona pullback
    #   - bars > 10: CHASE BLOCK
    #
    # MODE_EXPANSION (trend_expansion_active=True):
    #   - FAST_PATH_MAX_BARS = 10 (ventana extendida)
    #   - NO chase block por barras
    #   - Solo invalidar si ESTRUCTURA rompe (precio cruza origen)
    # =====================================================================
    trend_expansion_active = getattr(engine, '_trend_expansion_active', False)
    
    if trend_expansion_active:
        # ═══════════════════════════════════════════════════════════════════
        # MODE_EXPANSION: Ventana extendida, sin chase block por barras
        # ═══════════════════════════════════════════════════════════════════
        FAST_PATH_MAX_BARS = 10  # Ventana mucho más amplia en expansión
        
        if bars_since > FAST_PATH_MAX_BARS:
            if current_bos.force_direct_entry:
                current_bos.force_direct_entry = False
                logger.info(
                    f"🚀 MODE_EXPANSION: bars={bars_since} > {FAST_PATH_MAX_BARS} | "
                    f"Fast path cerrado pero PIPELINE NORMAL permitido (sin chase block)"
                )
                engine._current_trade_tags['expansion_mode_entry'] = True
            # En EXPANSION: NO cerramos entry_window, permitimos pipeline normal
            return None
        
        logger.info(
            f"🚀 MODE_EXPANSION FAST PATH | bars={bars_since} | "
            f"Trend expansion activo - ventana extendida"
        )
    else:
        # ═══════════════════════════════════════════════════════════════════
        # MODE_RANGE: Frenos normales, ventana corta
        # ═══════════════════════════════════════════════════════════════════
        FAST_PATH_MAX_BARS = 3
        PULLBACK_ZONE_MAX = 10  # Usar config en vez de hardcode 5
        
        if bars_since > FAST_PATH_MAX_BARS:
            if current_bos.force_direct_entry:
                current_bos.force_direct_entry = False
                logger.warning(
                    f"🚂 DISPLACEMENT FAST PATH MISSED | bars={bars_since} > {FAST_PATH_MAX_BARS} | "
                    f"El tren se fue - ahora solo PULLBACK entry permitida"
                )
                engine._current_trade_tags['displacement_fast_path_missed'] = True
                engine._current_trade_tags['displacement_missed_at_bars'] = bars_since
            
            if bars_since <= PULLBACK_ZONE_MAX:
                logger.debug(
                    f"🌊 DISPLACEMENT: bars={bars_since} - PULLBACK ZONE | "
                    f"Pipeline normal evaluará pullback entry"
                )
            else:
                # MODE_RANGE: Sí aplicar CHASE BLOCK
                logger.info(
                    f"🚫 MODE_RANGE CHASE BLOCK | bars={bars_since} > {PULLBACK_ZONE_MAX} | "
                    f"Edge expirado - no perseguir"
                )
                current_bos.entry_window_open = False
            
            return None
    
    # =====================================================================
    # 🔥 CHECK 1.5: ANTICIPATION EDGE PRIORITY (FIX #29)
    # =====================================================================
    # Si hay un BREAKOUT edge armado (de ANTICIPATION) con structure_level,
    # NO hacer DISPLACEMENT fast_path - esperar a que el precio rompa el
    # structure_level para una entrada más precisa.
    #
    # Ejemplo del log:
    # - ANTICIPATION detectó SHORT @ $76,672 (structure level)
    # - DISPLACEMENT quiere entrar @ $76,480 
    # - Mejor esperar ruptura de $76,672 para entrada más temprana
    # =====================================================================
    breakout_edge_armed = False
    breakout_has_structure = False
    
    # Get DISPLACEMENT direction from current_bos (available at this point)
    displacement_direction = current_bos.direction
    
    # Check si hay un BREAKOUT edge armado en algún edge manager
    # (puede estar pendiente de confirmación)
    structure_level_for_log: float = 0.0  # Initialize for logging
    anticipation_direction: str = ''
    anticipation_armed_time: Optional[datetime] = None
    
    if hasattr(engine, '_edge_manager') and engine._edge_manager:
        alt_edge_mgr = engine._edge_manager
        from src.domain.trading.edge_state_manager import EdgeState, EdgeType
        
        # Si el edge actual es BREAKOUT (de ANTICIPATION) y está ARMED
        if alt_edge_mgr.state == EdgeState.EDGE_ARMED:
            alt_bos = alt_edge_mgr.current_bos
            if alt_bos and alt_bos.edge_type == EdgeType.BREAKOUT:
                breakout_edge_armed = True
                anticipation_direction = alt_bos.direction
                anticipation_armed_time = alt_bos.timestamp  # timestamp = when edge was created
                # Check if it has a real structure_level (not just squeeze level)
                structure_level = alt_bos.bos_level
                squeeze_level = alt_bos.price
                if abs(structure_level - squeeze_level) > 0.3 * atr_1m:
                    breakout_has_structure = True
                    structure_level_for_log = structure_level
    
    # 🧠 SMART DEFERRAL: Solo diferir si las direcciones COINCIDEN
    # Si ANTICIPATION=SHORT y DISPLACEMENT=LONG, son setups diferentes - no diferir
    same_direction = (anticipation_direction == displacement_direction)
    
    # 🧠 TIMEOUT: No diferir si ANTICIPATION lleva más de 10 min sin confirmar
    # Después de 10 min, el edge está "stale" - mejor entrar con DISPLACEMENT
    MAX_DEFER_MINUTES = 10
    defer_timeout_exceeded = False
    minutes_armed: float = 0.0
    if anticipation_armed_time:
        minutes_armed = (market_time - anticipation_armed_time).total_seconds() / 60
        defer_timeout_exceeded = minutes_armed > MAX_DEFER_MINUTES
    
    if breakout_edge_armed and breakout_has_structure and same_direction and not defer_timeout_exceeded:
        # ANTICIPATION tiene un edge más preciso esperando Y es la misma dirección
        logger.info(
            f"⏸️ FAST PATH DEFERRED: ANTICIPATION EDGE ARMED | "
            f"BREAKOUT {anticipation_direction} structure=${structure_level_for_log:.0f} waiting for break | "
            f"DISPLACEMENT @ ${current_price:.0f} deferred"
        )
        engine._current_trade_tags['fast_path_deferred_anticipation'] = True
        engine._current_trade_tags['anticipation_structure_level'] = structure_level_for_log
        # Don't return None completely - allow pipeline to check BREAKOUT edge
        # But skip the DISPLACEMENT fast path
        return None
    elif breakout_edge_armed and breakout_has_structure and not same_direction:
        # Direcciones opuestas - log pero NO diferir
        logger.debug(
            f"🔀 ANTICIPATION {anticipation_direction} vs DISPLACEMENT {displacement_direction} | "
            f"Different directions - proceeding with DISPLACEMENT"
        )
    elif breakout_edge_armed and defer_timeout_exceeded:
        # Timeout - el ANTICIPATION edge está stale
        logger.warning(
            f"⏰ ANTICIPATION TIMEOUT: Edge armed for {minutes_armed:.1f} min > {MAX_DEFER_MINUTES} min | "
            f"Proceeding with DISPLACEMENT"
        )
        engine._current_trade_tags['anticipation_timeout'] = True
    
    # =====================================================================
    # CHECK 2: Validaciones MÍNIMAS (solo las críticas)
    # =====================================================================
    
    # 2a. Dirección debe coincidir con bias actual
    disp_direction = current_bos.direction
    if disp_direction != ctx.bias:
        logger.warning(
            f"🚫 FAST PATH SKIP: Direction mismatch | "
            f"DISPLACEMENT={disp_direction} vs BIAS={ctx.bias}"
        )
        return None
    
    # =====================================================================
    # 🔥 CHECK 2a.5: LOCATION BLOCK — FAST PATH (Feb 20, 2026 REFACTORED)
    # =====================================================================
    # Uses SWING RANGE position (not just percentile_60d which was too loose)
    # Even for DISPLACEMENT, don't short at the very bottom or buy at the top.
    # Swing range gives real-time context vs 60-day percentile.
    # =====================================================================
    fp_swing_high = getattr(engine, '_last_swing_high', 0)
    fp_swing_low = getattr(engine, '_last_swing_low', 0)
    
    if fp_swing_high > 0 and fp_swing_low > 0 and fp_swing_high > fp_swing_low:
        fp_range = fp_swing_high - fp_swing_low
        fp_range_pos = (current_price - fp_swing_low) / fp_range
        
        # DISPLACEMENT gets looser limits (20%/80%) but NOT unlimited
        if disp_direction == 'SHORT' and fp_range_pos < 0.20:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: SHORT at {fp_range_pos:.0%} of range (bottom 20%) | "
                f"price={current_price:.0f} SwH={fp_swing_high:.0f} SwL={fp_swing_low:.0f}"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            engine._current_trade_tags['fast_path_range_position'] = fp_range_pos
            return None
        
        if disp_direction == 'LONG' and fp_range_pos > 0.80:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: LONG at {fp_range_pos:.0%} of range (top 20%) | "
                f"price={current_price:.0f} SwH={fp_swing_high:.0f} SwL={fp_swing_low:.0f}"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            engine._current_trade_tags['fast_path_range_position'] = fp_range_pos
            return None
    
    # Also check percentile_60d as fallback
    location_ctx = getattr(engine, '_location_context', None)
    if location_ctx:
        pct_60d = location_ctx.percentile_60d
        in_extreme_low = location_ctx.in_extreme_low or pct_60d <= 10
        in_extreme_high = location_ctx.in_extreme_high or pct_60d >= 90
        
        if disp_direction == 'SHORT' and in_extreme_low:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: EXTREME OVERSOLD (pct_60d={pct_60d:.1f}%)"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            return None
        
        if disp_direction == 'LONG' and in_extreme_high:
            logger.warning(
                f"🚫 FAST PATH BLOCKED: EXTREME OVERBOUGHT (pct_60d={pct_60d:.1f}%)"
            )
            engine._current_trade_tags['fast_path_blocked_extreme'] = True
            return None
    
    # 2b. No en cooldown global
    # 🔧 FIX: El método es is_in_cooldown(), no in_cooldown()
    if engine.state_machine.is_in_cooldown():
        logger.debug("🚫 FAST PATH SKIP: In cooldown")
        return None
    
    # 2c. No ya en posición
    # 🔧 FIX: El atributo es .state (BotState enum), no .current_state
    # También importamos BotState para comparar correctamente
    from src.domain.trading.state.state_machine import BotState
    if engine.state_machine.state != BotState.IDLE:
        return None
    
    # =====================================================================
    # � FIX #91 (Feb 09, 2026): SIMPLIFICAR BLOQUEOS - ERAN CONTRAPRODUCENTES
    # =====================================================================
    # PROBLEMA: Los bloqueos creaban ciclos donde SIEMPRE llegábamos tarde:
    # 1. Detecta SHORT → forbidden_direction=LONG ✓
    # 2. Pero forbidden_direction bloquea el SIGUIENTE short también
    # 3. Porque el bias flip es tan rápido que forbidden_dir aún no se limpió
    #
    # SOLUCIÓN: Para DISPLACEMENT FAST PATH, SOLO bloquear si:
    # - direction_lock ACTIVO en dirección OPUESTA (no forbidden_direction)
    # - expansion_mode ACTIVO reciente (<5 min) en dirección OPUESTA
    #
    # El DISPLACEMENT es la señal más fuerte. Si hay displacement,
    # forbidden_direction es STALE (viejo) - ignorarlo.
    # =====================================================================
    
    # Check: Counter-expansion block (<5 min opposite direction)
    expansion_mode = getattr(engine, '_expansion_mode', False)
    expansion_dir = getattr(engine, '_expansion_direction', None)
    if expansion_mode and expansion_dir and expansion_dir != disp_direction:
        exp_start = getattr(engine, '_expansion_start_time', None)
        if exp_start and (datetime.now() - exp_start).total_seconds() < 300:  # 5 min
            logger.warning(
                f"🚫 FAST PATH BLOCKED: COUNTER-EXPANSION | "
                f"{disp_direction} vs EXPANSION={expansion_dir} (started {(datetime.now() - exp_start).seconds}s ago)"
            )
            engine._current_trade_tags['fast_path_blocked_counter_momentum'] = True
            return None

    # =====================================================================
    # 🔥 FIX #92: DELTA CONTRADICT - RELAJAR UMBRAL
    # =====================================================================
    # El delta contradict era útil pero el umbral de 2x era muy restrictivo.
    # Subimos a 5x para solo bloquear casos extremos de fakeout.
    # =====================================================================
    delta_ratio = engine._current_trade_tags.get('volume_delta_ratio', 0)
    
    # Umbral RELAJADO: Solo bloquear si delta es >5x (era 2x)
    DELTA_CONTRADICT_THRESHOLD = 5.0  # 🔥 FIX: era 2.0
    
    if disp_direction == 'LONG' and delta_ratio < -DELTA_CONTRADICT_THRESHOLD:
        # Volumen vendedor masivo contra LONG
        logger.warning(
            f"🚫🚫🚫 FAST PATH BLOCKED: DELTA CONTRADICT | "
            f"Direction=LONG but delta={delta_ratio:.2f}x (selling pressure) | "
            f"Displacement puede ser FAKEOUT - esperando confirmación"
        )
        engine._current_trade_tags['fast_path_blocked_by_delta'] = True
        engine._current_trade_tags['fast_path_block_delta_ratio'] = delta_ratio
        return None
    
    if disp_direction == 'SHORT' and delta_ratio > DELTA_CONTRADICT_THRESHOLD:
        # Volumen comprador masivo contra SHORT
        logger.warning(
            f"🚫🚫🚫 FAST PATH BLOCKED: DELTA CONTRADICT | "
            f"Direction=SHORT but delta=+{delta_ratio:.2f}x (buying pressure) | "
            f"Displacement puede ser FAKEOUT - esperando confirmación"
        )
        engine._current_trade_tags['fast_path_blocked_by_delta'] = True
        engine._current_trade_tags['fast_path_block_delta_ratio'] = delta_ratio
        return None
    
    # =====================================================================
    # 🚨 CHECK 4: ENTRY AT EXTREME (Fix #14 - Feb 02, 2026)
    # =====================================================================
    # PROBLEMA CRÍTICO DETECTADO:
    # - LONG entró DESPUÉS de que el precio subiera $560
    # - El displacement origin era $77,658, entry fue en $78,214
    # - Esto es entrar en el TOP del movimiento → MFE 0.03R → loss
    #
    # CASO REAL (Feb 02, 2026):
    # - Displacement LONG detectado con origin $77,658
    # - Precio actual $78,214 (ya subió $560 = 2.4 ATR desde origin)
    # - Sistema entró en el EXTREMO → reversión inmediata → -0.30R
    #
    # 🔧 FIX INTELIGENTE (Fix #26): Usar displacement-ratio, no ATR
    # El problema era que ATR_1m (~$100) hacía que un disp de $400 ya fuera 4 ATR
    # Ahora: bloqueamos solo si perseguimos >50% MÁS ALLÁ del displacement inicial
    # =====================================================================
    origin_for_extreme_check = current_bos.origin_price or 0
    initial_disp_for_check = current_bos.initial_displacement_amount or 0
    
    if origin_for_extreme_check > 0 and initial_disp_for_check > 0:
        # Calcular distancia desde origin al precio actual
        if disp_direction == 'LONG':
            distance_from_origin = current_price - origin_for_extreme_check
        else:
            distance_from_origin = origin_for_extreme_check - current_price
        
        # Si retrocedimos (distance < 0), no hay chase
        if distance_from_origin > 0:
            # Ratio: 1.0 = exactamente en el displacement, 1.5 = 50% más allá
            displacement_ratio = distance_from_origin / initial_disp_for_check
            
            # BLOCK si >50% más allá del displacement inicial
            MAX_CHASE_RATIO = 1.5
            if displacement_ratio > MAX_CHASE_RATIO:
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: ENTRY AT EXTREME | "
                    f"{disp_direction} entry at {displacement_ratio:.1f}x displacement | "
                    f"Origin=${origin_for_extreme_check:.0f}, Disp=${initial_disp_for_check:.0f}, "
                    f"Current=${current_price:.0f}, Distance=${distance_from_origin:.0f} | "
                    f"Don't chase - wait for pullback"
                )
                engine._current_trade_tags['fast_path_blocked_by_extreme_entry'] = True
                engine._current_trade_tags['fast_path_displacement_ratio'] = displacement_ratio
                return None
    
    # =====================================================================
    # 🚨 CHECK 5: PRECIO YA REVIRTIÓ? (FAKEOUT DETECTOR)
    # =====================================================================
    # Si el precio ya retrocedió >40% del displacement inicial = FAKEOUT
    # 
    # Caso real:
    # - Displacement SHORT $326 detectado @ $82301
    # - 8 segundos después precio ya en $82481 (+$180 = 55% reversal)
    # - Esto era un wick/fakeout, no un displacement real
    #
    # Regla: Si precio ha revertido >40%, el displacement ya se invalidó
    # =====================================================================
    origin_price_check = current_bos.origin_price or 0
    initial_disp_check = current_bos.initial_displacement_amount or 0
    
    if initial_disp_check > 0 and origin_price_check > 0:
        if disp_direction == 'LONG':
            # En LONG: origin era el LOW, displacement subió
            # Si precio actual < origin + (disp * 0.60) → ya retrocedió >40%
            min_continuation = origin_price_check + (initial_disp_check * 0.60)
            if current_price < min_continuation:
                retracement_pct = (origin_price_check + initial_disp_check - current_price) / initial_disp_check * 100
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: FAKEOUT DETECTED | "
                    f"LONG displacement pero precio ya retrocedió {retracement_pct:.0f}% | "
                    f"Origin=${origin_price_check:.0f} Disp=${initial_disp_check:.0f} Current=${current_price:.0f}"
                )
                engine._current_trade_tags['fast_path_blocked_by_reversal'] = True
                engine._current_trade_tags['fast_path_reversal_pct'] = retracement_pct
                return None
        else:
            # En SHORT: origin era el HIGH, displacement bajó
            # Si precio actual > origin - (disp * 0.60) → ya retrocedió >40%
            max_continuation = origin_price_check - (initial_disp_check * 0.60)
            if current_price > max_continuation:
                retracement_pct = (current_price - (origin_price_check - initial_disp_check)) / initial_disp_check * 100
                logger.warning(
                    f"🚫 FAST PATH BLOCKED: FAKEOUT DETECTED | "
                    f"SHORT displacement pero precio ya retrocedió {retracement_pct:.0f}% | "
                    f"Origin=${origin_price_check:.0f} Disp=${initial_disp_check:.0f} Current=${current_price:.0f}"
                )
                engine._current_trade_tags['fast_path_blocked_by_reversal'] = True
                engine._current_trade_tags['fast_path_reversal_pct'] = retracement_pct
                return None
    
    # =====================================================================
    # � FIX (Feb 02): SCORE INFORMATIVO PARA FAST_PATH
    # =====================================================================
    # PROBLEMA: FAST_PATH no calculaba score → score=0 en logs → imposible analizar
    # 
    # SOLUCIÓN: Calcular score INFORMATIVO (no bloquea, solo para análisis)
    # Esto permite:
    # 1. Distinguir "FAST_PATH buenos" de "FAST_PATH malos"
    # 2. Aplicar TIME_INVALIDATION correctamente basado en score real
    # 3. Análisis post-trade para mejorar criterios
    #
    # El score se calcula pero NO bloquea la entrada (FAST_PATH siempre ejecuta)
    # =====================================================================
    fast_path_score = 0.0
    fast_path_breakdown = {}
    
    try:
        # Obtener entry_scorer si está disponible
        entry_scorer = getattr(engine, '_entry_scorer', None)
        if entry_scorer and ctx.df_1h is not None and ctx.df_5m is not None and ctx.df_1m is not None:
            # 🔥 FIX #56: Get context for structure gate overrides
            expansion_mode_active = engine._current_trade_tags.get('expansion_mode_activated', False)
            effective_adx = engine._current_trade_tags.get('effective_adx', 0) or engine._current_trade_tags.get('htf_adx', 0)
            
            # 🔥 FIX #230: Get impulse age and origin price
            impulse_start_price: float | None = None
            impulse_age_bars: int = 0
            if hasattr(engine, '_edge_manager') and engine._edge_manager is not None:
                impulse_start_price = engine._edge_manager.get_edge_origin_price()
                edge_age_min = engine._edge_manager.get_edge_age_minutes(market_time)
                impulse_age_bars = int(edge_age_min / 5.0)
            
            # Calcular score con la firma correcta del método
            score, breakdown, structure_blocked = entry_scorer.calculate_entry_score(
                df_1h=ctx.df_1h,
                df_5m=ctx.df_5m,
                df_1m=ctx.df_1m,
                bias=disp_direction,
                current_price=current_price,
                market_time=market_time,
                micro_trend_state=getattr(engine, '_micro_trend_state', None),
                filter_pipeline=getattr(engine, '_entry_filter_pipeline', None),
                displacement_edge_active=True,  # FAST_PATH = displacement activo
                expansion_mode_active=expansion_mode_active,  # 🔥 FIX #56
                strong_trend_adx=effective_adx,               # 🔥 FIX #56
                impulse_start_price=impulse_start_price,      # 🔥 FIX #230
                impulse_age_bars=impulse_age_bars,            # 🔥 FIX #230
            )
            fast_path_score = score
            fast_path_breakdown = breakdown
            
            # Guardar en tags para logging y análisis
            engine._current_trade_tags['entry_score'] = fast_path_score
            engine._current_trade_tags['score_breakdown'] = fast_path_breakdown
            
            # También guardar componentes individuales para análisis
            for key, val in fast_path_breakdown.items():
                if key.startswith('score_') or key in ('structure', 'timing', 'momentum', 'context', 'synergy'):
                    engine._current_trade_tags[f'score_{key}' if not key.startswith('score_') else key] = val
            
            # Guardar en engine._entry_score para TIME_INVALIDATION fix
            engine._entry_score = fast_path_score
            engine._score_breakdown = fast_path_breakdown
            
            logger.info(
                f"📊 FAST PATH INFORMATIVE SCORE: {fast_path_score:.1f} | "
                f"breakdown={fast_path_breakdown}"
            )
        else:
            # Fallback: score = 0 porque no hay scorer o DataFrames disponibles
            missing = []
            if not entry_scorer:
                missing.append('scorer')
            if ctx.df_1h is None:
                missing.append('df_1h')
            if ctx.df_5m is None:
                missing.append('df_5m')
            if ctx.df_1m is None:
                missing.append('df_1m')
            
            engine._current_trade_tags['entry_score'] = 0
            engine._current_trade_tags['score_not_calculated'] = f"missing_{','.join(missing)}"
            engine._entry_score = 0
            engine._score_breakdown = {}
            logger.warning(
                f"⚠️ FAST PATH: Score=0 (missing: {missing}) - proceeding anyway"
            )
            
    except Exception as e:
        # Si falla el scoring, no bloqueamos FAST_PATH - solo logueamos
        logger.warning(f"⚠️ FAST PATH: Score calculation failed: {e} - continuing without score")
        engine._current_trade_tags['entry_score'] = 0
        engine._current_trade_tags['score_calculation_error'] = str(e)
        engine._entry_score = 0
        engine._score_breakdown = {}
    
    # ML inference removed from fast path — Opción A
    
    # =====================================================================
    # CHECK 2.5e: SCORE MÍNIMO PARA FAST PATH (Feb 03, 2026)
    # =====================================================================
    # Análisis de 49 trades FAST PATH: expectancy -0.012R, 35% TIME_INVALIDATION
    # With only 3 trades/day and $231 capital, every entry must be quality.
    # Score >= 7.0 for ALL fast path entries — no TREND_BIRTH/ACCELERATION bypass.
    # =====================================================================
    from src.config.unified_constants import SCORES
    from src.domain.market.regime import TrendPhase
    
    FAST_PATH_MIN_SCORE = SCORES.FAST_PATH_MIN_SCORE  # 7.0 — NO EXCEPTIONS
    
    if fast_path_score < FAST_PATH_MIN_SCORE:
        trend_phase = getattr(engine, '_trend_phase', TrendPhase.RANGE_BOUND)
        logger.warning(
            f"🛑 FAST PATH BLOCKED: Score {fast_path_score:.1f} < {FAST_PATH_MIN_SCORE} minimum | "
            f"direction={disp_direction} | TrendPhase={trend_phase.value if hasattr(trend_phase, 'value') else trend_phase} | "
            f"breakdown={engine._score_breakdown}"
        )
        return None
    
    # CHECK 2.5g: MOMENTUM == 0 -> BLOCK FAST PATH (Feb 04, 2026)
    # =====================================================================
    # Score puede ser alto (8.0) pero si momentum=0, el tren ya paro.
    # FAST PATH es para "subirse al tren en movimiento".
    # Sin momentum = sin urgencia = usar pipeline normal.
    # =====================================================================
    momentum_score = fast_path_breakdown.get('momentum', 1)
    
    if momentum_score == 0:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: MOMENTUM=0 | "
            f"Score={fast_path_score:.1f} but momentum=0 | "
            f"breakdown={fast_path_breakdown} | "
            f"No momentum = no urgency - use normal pipeline"
        )
        engine._current_trade_tags['fast_path_blocked_no_momentum'] = True
        return None

    # =====================================================================
    # 🔥 FIX (Feb 19, 2026): DISPLACEMENT CONSUMED GUARD
    # Block re-entry using the SAME displacement that made the previous trade.
    # After a trade exits, the rolling extremes still show the old move.
    # Wait 180s before allowing any new displacement-based entry.
    # =====================================================================
    last_exit = getattr(engine, '_last_trade_exit_time', None)
    if last_exit is not None:
        from datetime import datetime
        elapsed = (datetime.now() - last_exit).total_seconds()
        if elapsed < 180:
            logger.warning(
                f"🚫 DISPLACEMENT CONSUMED: Last trade exited {elapsed:.0f}s ago (<180s) | "
                f"Blocking fast path to prevent recycled displacement entry"
            )
            engine._current_trade_tags['displacement_consumed_blocked'] = True
            return None

    # =====================================================================
    # FAST PATH ACTIVADO - ENTRAR AHORA
    # =====================================================================
    origin_log = current_bos.origin_price or 0
    disp_log = current_bos.initial_displacement_amount or 0
    logger.warning(
        f"🚀🚀🚀 DISPLACEMENT FAST PATH ACTIVATED 🚀🚀🚀 | "
        f"bars_since={bars_since} | direction={disp_direction} | "
        f"origin=${origin_log:.0f} | displacement=${disp_log:.0f} | "
        f"INFO_SCORE={fast_path_score:.1f}"
    )
    
    # =====================================================================
    # SIZING: Basado en score calculado (Feb 01, 2026)
    # PROBLEMA: Buenos trades tenían 0.25x sizing, malos también 0.25x
    # SOLUCIÓN: Usar el score informativo para sizing proporcional
    # =====================================================================
    base_risk_pct = 0.01  # 1% of capital - industry standard
    
    # 🔥 FIX (Feb 04, 2026): Sizing CONSERVADOR - MAX 50%
    # Análisis: FAST PATH tiene expectancy -0.012R vs Normal -0.004R
    # Reducir sizing hasta que la estrategia sea net-positive
    # BEFORE: Score 7+ = 100% (demasiado agresivo para expectancy negativa)
    # AFTER: Score 8+ = 50% MAX (reducir pérdidas mientras optimizamos)
    if fast_path_score >= 9:
        fast_path_size_mult = 0.50  # Excellent score = 50% MAX
    elif fast_path_score >= 8:
        fast_path_size_mult = 0.40  # Very good score = 40%
    elif fast_path_score >= 7:
        fast_path_size_mult = 0.30  # Good score = 30%
    else:
        fast_path_size_mult = 0.25  # Minimum = 25%
    
    # Bar 0 aún más conservador (chasing risk)
    if bars_since == 0:
        fast_path_size_mult = min(fast_path_size_mult, 0.30)
    
    engine._current_trade_tags['fast_path_size_mult'] = fast_path_size_mult
    engine._current_trade_tags['fast_path_size_reason'] = f'score={fast_path_score:.1f}'
    
    # =====================================================================
    # STOP LOSS: Estructural basado en origen del displacement
    # NO ATR tight - el SL es el ORIGEN del movimiento
    # =====================================================================
    origin_price = current_bos.origin_price
    initial_disp = current_bos.initial_displacement_amount or (ctx.atr_5m * 2)
    
    # SL más allá del origen (buffer de 10%)
    sl_buffer = initial_disp * 0.10
    
    if disp_direction == 'LONG':
        # En LONG, SL debajo del origen
        stop_loss = origin_price - sl_buffer
        # Asegurar mínimo de 1x ATR5m
        min_sl = current_price - (ctx.atr_5m * 1.5)
        stop_loss = min(stop_loss, min_sl)
    else:
        # En SHORT, SL encima del origen
        stop_loss = origin_price + sl_buffer
        # Asegurar mínimo de 1x ATR5m
        max_sl = current_price + (ctx.atr_5m * 1.5)
        stop_loss = max(stop_loss, max_sl)
    
    risk = abs(current_price - stop_loss)
    
    # =====================================================================
    # TAKE PROFIT: Regime-aware TP
    # RANGE: 0.30R (scalp), TREND: 2.0R (runners)
    # =====================================================================
    current_regime = getattr(engine, '_current_trade_regime', 'RANGE')
    from src.domain.risk.risk_engine import normalize_regime_mode
    normalized_regime = normalize_regime_mode(current_regime)
    if normalized_regime == 'RANGE':
        tp_r = 0.30
    else:
        tp_r = 2.0
    if disp_direction == 'LONG':
        take_profit = current_price + (risk * tp_r)
    else:
        take_profit = current_price - (risk * tp_r)
    
    # =====================================================================
    # SIZE: Calcular basado en risk
    # =====================================================================
    # 🔧 FIX: Usar risk_engine.capital en lugar de _current_balance inexistente
    risk_engine = getattr(engine, 'risk_engine', None)
    account_balance = getattr(risk_engine, 'capital', 10000) if risk_engine else 10000
    risk_amount = account_balance * base_risk_pct * fast_path_size_mult
    
    size = risk_amount / risk if risk > 0 else 0
    size = round_size_to_exchange(size)
    
    # Validar mínimo
    if size < KRAKEN_BTC_MIN_SIZE:
        logger.warning(
            f"🚫 FAST PATH: Size {size:.6f} < min {KRAKEN_BTC_MIN_SIZE} | "
            f"risk=${risk_amount:.2f}, risk_dist=${risk:.2f}"
        )
        result = EntryResult()
        result.reason = "Size below minimum for fast path"
        return result
    
    logger.warning(
        f"🚀 FAST PATH ENTRY | {disp_direction} {size:.6f} BTC @ ${current_price:.2f} | "
        f"SL=${stop_loss:.2f} ({risk:.2f} risk) | TP=${take_profit:.2f} ({tp_r}R) | "
        f"Size mult: {fast_path_size_mult:.0%} [IOC]"
    )
    
    # =====================================================================
    # EJECUTAR: IOC/Aggressive (taker permitido)
    # Nota: momentum=0 ya fue bloqueado arriba, aquí siempre hay momentum
    # =====================================================================
    engine._current_trade_tags['entry_type'] = 'FAST_PATH_DISPLACEMENT'
    engine._current_trade_tags['fast_path_bars_since'] = bars_since
    engine._current_trade_tags['fast_path_origin'] = origin_price
    engine._current_trade_tags['fast_path_size_mult'] = fast_path_size_mult
    engine._current_trade_tags['fast_path_order_type'] = 'IOC_AGGRESSIVE'
    
    from src.domain.trading.state.state_machine import BotState
    from src.domain.trading.strategy.pullback_short import TradeType
    
    result = EntryResult()
    
    # Llamar place_aggressive_order directamente para tener acceso al fill info
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
        logger.warning(
            f"🚫 FAST PATH IOC NOT FILLED | "
            f"Reason: IOC expired (spread too wide or slippage)"
        )

    return result

