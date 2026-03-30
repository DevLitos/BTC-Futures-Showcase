"""
EdgeStateManager - El edge como EVENTO DISCRETO, no como contexto continuo.

Manages edge lifecycle: detection, arming, activation, expiration.
Types/enums extracted to edge_types.py for maintainability.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
import logging
import pandas as pd

from src.domain.trading.edge_types import (
    _edge_log, _STATE_SYMBOLS, _TYPE_SYMBOLS,
    EdgePriority, TrendEdgePath, EdgeType, EdgeState,
    RangeEdgeCriteria, BreakoutEdgeCriteria, DisplacementEdgeCriteria,
    BOSEvent, EdgeStats, EdgeTransition,
)

logger = logging.getLogger(__name__)


class EdgeStateManager:
    """
    Gestor de estado del edge - EDGE OWNER SOBERANO.

    🌊 BE WATER MY FRIEND: El edge fluye con el mercado.

    - Transiciones FLUIDAS, no abruptas
    - Detección RÁPIDA de cambios de contexto
    - Logs INTELIGENTES (1-2 líneas con info clave)

    Soporta 4 tipos de edge:
    - TREND: BOS + Pullback (ADX > 18)
    - RANGE: Extremo 90/10 + Sweep + Rejection
    - BREAKOUT: Compresión + Ruptura confirmada
    - INTRADAY: Micro-ineficiencias alta frecuencia

    Soporta 2 modos de operación:
    - INSTITUTIONAL: 1-5/semana, size 1.0x-1.5x
    - INTRADAY: 5-20/día, size 0.15x-0.30x
    """

    # Configuración TREND
    MAX_TRADES_PER_EDGE = 2          # Máximo 2 intentos por edge
    EDGE_ARMED_TIMEOUT_MIN = 30      # Timeout si no hay pullback (TREND)
    EDGE_ACTIVE_TIMEOUT_MIN = 30     # Ventana de entrada tras pullback (was 15 — too short for evaluation pipeline)
    PULLBACK_MIN_ATR = 0.3           # Mínimo pullback en ATR
    PULLBACK_MAX_ATR = 1.5           # Máximo pullback en ATR

    # Configuración RANGE
    RANGE_EDGE_TIMEOUT_MIN = 15      # Ventana en rango (was 10)

    # Cooldown por contexto - evita pérdidas correlacionadas
    CONTEXT_COOLDOWN_MINUTES = 10    # (was 15)

    # Cooldown por DIRECCIÓN tras pérdida (evita revenge trading algorítmico)
    DIRECTION_COOLDOWN_AFTER_LOSS_MIN = 5  # (was 10)

    def __init__(self):
        self._state = EdgeState.NO_EDGE
        self._current_bos: Optional[BOSEvent] = None
        self._stats = EdgeStats()
        self._bos_history: List[BOSEvent] = []

        # Track reason for NO_EDGE state
        # This allows informative logging about why no edge is detected
        self._no_edge_reason: str = "Initializing..."
        self._last_edge_check_result: Dict[str, Any] = {}  # Details from last check

        # Transition history for debugging
        self._transition_history: List[EdgeTransition] = []

        # Cache para evitar re-evaluación
        self._last_bos_check_ts: Optional[datetime] = None
        self._last_bos_check_price: Optional[float] = None

        # Cooldown por contexto de edge
        self._context_cooldown: Dict[str, datetime] = {}

        # =====================================================================
        # LOSS ZONE MEMORY
        # LOG EVIDENCE: 3 identical LONG entries in $68,860-$68,950 zone, all lost.
        # System didn't learn from first loss — entered twice more in same zone.
        # Track recent loss zones and refuse identical setups for 30 minutes.
        # Each entry: {'direction': str, 'price': float, 'atr': float, 'expires': datetime}
        # =====================================================================
        self._loss_zone_memory: List[Dict[str, Any]] = []
        self.LOSS_ZONE_MEMORY_MINUTES = 15   # (was 30)
        self.LOSS_ZONE_RADIUS_ATR = 1.0  # Block within 1.0 ATR of loss price (was 1.5)

        # Current market regime
        self._current_regime: str = "UNKNOWN"
        self._current_adx: float = 20.0

        # =========================================================================
        # POST-DISPLACEMENT COOLDOWN

        # PROBLEMA: Cuando un DISPLACEMENT expira (edge_type=DISPLACEMENT),
        # el sistema crea inmediatamente un nuevo edge (CONSENSUS/TREND) y entra
        # tarde cuando el momentum ya murió.

        # CASO REAL (Feb 10, 2026):
        # 15:39 - DISPLACEMENT $919 detectado → STALE/CHASE BLOCK
        # 15:45 - LATE DISPLACEMENT: 21 bars > 20 - NO CHASE → edge expira
        # 15:46 - Nuevo TREND_EXPANSION_LOCK edge → permite entrar
        # 15:50 - Entrada SHORT a $68,000 → precio rebota → PIERDE

        # SOLUCIÓN: Cuando un DISPLACEMENT expira, activar cooldown de 10 min
        # para NO crear nuevos edges CONSENSUS en esa dirección.
        # El mercado necesita mostrar NUEVA estructura, no la vieja.
        # =========================================================================
        self._post_displacement_cooldown_until: Optional[datetime] = None
        self._post_displacement_cooldown_direction: Optional[str] = None  # 'SHORT' or 'LONG'

        # =========================================================================
        # Direction staleness tracking

        # PROBLEMA: Cuando edge expira, _active_direction en trading_app queda
        # "congelada" y puede bloquear nuevas señales ANTICIPATION válidas.

        # SOLUCIÓN: Marcar cuándo la dirección necesita refresh.
        # trading_app.py debe consultar este flag y resetear _active_direction.
        # =========================================================================
        self._direction_stale_since: Optional[datetime] = None  # Cuando se volvió stale
        self._last_valid_direction: Optional[str] = None  # Última dirección válida

        # =========================================================================
        # SIGNAL AGE TRACKING

        # PROBLEMA: El sistema VE el movimiento al inicio pero ESPERA.
        # Mientras el precio avanza, el sistema "perfecciona" sus filtros.
        # Cuando todo está "perfecto", el movimiento ya terminó y entra al FINAL.

        # SOLUCIÓN: Trackear cuándo se activó el edge (EDGE_ACTIVE).
        # - Edge FRESH (< 3 min): Listo para entrar, no penalizar
        # - Edge STALE (> 5 min): Señal vieja, el movimiento probablemente ya pasó
        # → Aplicar penalty de -1.5 al score para RECHAZAR entradas tardías

        # Esto hace que el sistema:
        # - Entre MÁS RÁPIDO al inicio del movimiento (antes de acumular chase)
        # - RECHACE entradas cuando el edge lleva demasiado tiempo sin ejecutarse
        # =========================================================================
        self._edge_activated_at: Optional[datetime] = None  # Timestamp de activación
        self._edge_origin_price: Optional[float] = None  # Price at edge activation

        # =========================================================================
        _edge_log("NO_EDGE", "TREND", "-", "EdgeStateManager initialized | BE WATER MY FRIEND")

    # =========================================================================
    # 🌊 CORE TRANSITION METHOD - ALL STATE CHANGES GO THROUGH HERE
    # =========================================================================

    def _transition_to(
        self,
        new_state: EdgeState,
        reason: str,
        market_time: Optional[datetime] = None
    ) -> bool:
        """
        Central method for ALL state transitions.

        This ensures:
        - Valid transitions only
        - Consistent logging
        - History tracking
        - Clean state management

        Returns: True if transition succeeded
        """
        old_state = self._state

        # Build transition record
        edge_type = self._current_bos.edge_type if self._current_bos else None
        direction = self._current_bos.direction if self._current_bos else None

        transition = EdgeTransition(
            from_state=old_state,
            to_state=new_state,
            reason=reason,
            timestamp=market_time or datetime.now(UTC),
            edge_type=edge_type,
            direction=direction
        )

        # Validate transition (unless resetting to NO_EDGE)
        if new_state != EdgeState.NO_EDGE and not transition.is_valid():
            logger.warning(f"⚠️ Invalid transition: {old_state.value}→{new_state.value}")
            return False

        # Execute transition
        self._state = new_state
        self._transition_history.append(transition)

        # =========================================================================
        # expiry

        # Cuando el edge expira o se resetea, la dirección asociada se vuelve
        # "stale" (obsoleta). trading_app.py debe consultar is_direction_stale()
        # y considerar resetear _active_direction para permitir nuevas señales.
        # =========================================================================
        if new_state in [EdgeState.EDGE_EXPIRED, EdgeState.NO_EDGE]:
            if direction:  # Solo si había dirección
                self._direction_stale_since = market_time or datetime.now(UTC)
                self._last_valid_direction = direction
            self._edge_activated_at = None
            self._edge_origin_price = None
        elif new_state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            # Nueva edge activa - dirección ya no es stale
            self._direction_stale_since = None
            if new_state == EdgeState.EDGE_ACTIVE and self._edge_activated_at is None:
                ts = market_time or datetime.now(UTC)
                if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                self._edge_activated_at = ts
                logger.info(f"🎯 Edge activated at {self._edge_activated_at.strftime('%H:%M:%S')}")
            if self._current_bos and self._edge_origin_price is None:
                self._edge_origin_price = self._current_bos.price

        # Keep history bounded
        if len(self._transition_history) > 100:
            self._transition_history = self._transition_history[-100:]

        # Log the transition
        transition.log()

        return True

    @property
    def state(self) -> EdgeState:
        return self._state

    @property
    def current_bos(self) -> Optional[BOSEvent]:
        return self._current_bos

    @property
    def stats(self) -> EdgeStats:
        return self._stats

    @property
    def current_edge_type(self) -> Optional[EdgeType]:
        """Tipo de edge actual"""
        if self._current_bos:
            return self._current_bos.edge_type
        return None

    @property
    def direction(self) -> Optional[str]:
        """
        patibilidad.

        Devuelve la dirección del edge actual (LONG/SHORT) o None si no hay edge.
        Esto evita el AttributeError cuando código externo accede a _edge_manager.direction
        """
        if self._current_bos:
            return self._current_bos.direction
        return None

    # =========================================================================

    # =========================================================================

    def is_direction_stale(self, stale_threshold_minutes: int = 5) -> bool:
        """
        ¿La dirección del último edge está "stale" (obsoleta)?

        Se considera stale si:
        - El edge expiró/reseteó hace más de `stale_threshold_minutes`
        - No hay edge activo actualmente

        Uso: trading_app.py debe consultar esto y considerar resetear
        _active_direction para permitir nuevas señales ANTICIPATION.
        """
        if self._direction_stale_since is None:
            return False

        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False  # Hay edge activo, no es stale

        now_aware = datetime.now(UTC)
        stale_since = self._direction_stale_since
        # Ensure both are timezone-aware
        if stale_since.tzinfo is None:
            stale_since = stale_since.replace(tzinfo=UTC)
        stale_duration = now_aware - stale_since
        return stale_duration > timedelta(minutes=stale_threshold_minutes)

    def is_edge_active(self) -> bool:
        """
        ¿Está el edge en estado activo para trading?
        EDGE_ARMED o EDGE_ACTIVE permiten trading.
        """
        return self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]

    def can_evaluate_entry(self) -> bool:
        """
        ¿Puede el sistema evaluar entradas?
        ACTIVE o PROVISIONAL permiten evaluación.
        """
        return self._state in [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]

    # =========================================================================
    # SIGNAL AGE METHODS

    # Permite calcular cuánto tiempo lleva activo el edge para aplicar
    # penalty a entradas tardías ("stale signal penalty")
    # =========================================================================

    def get_edge_age_minutes(self, now: Optional[datetime] = None) -> float:
        """
        Calcula cuántos minutos lleva el edge en estado EDGE_ACTIVE.

        Returns:
            float: Minutos desde activación, o 0.0 si no está activo
        """
        # ks FIRST
        try:
            if self._edge_activated_at is None:
                return 0.0
            if self._state not in [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
                return 0.0

            # Get current time - always use naive UTC
            current = now or datetime.now(UTC)

            # Normalize both to naive UTC for comparison (handle timezone mismatch)
            activated = self._edge_activated_at
            if hasattr(activated, 'tzinfo') and activated.tzinfo is not None:
                activated = activated.replace(tzinfo=None)
            if hasattr(current, 'tzinfo') and current.tzinfo is not None:
                current = current.replace(tzinfo=None)

            age_seconds = (current - activated).total_seconds()
            return max(0.0, age_seconds / 60.0)
        except Exception as e:
            # Log silently and return safe default
            return 0.0

    def get_edge_origin_price(self) -> Optional[float]:
        """
         activated.
        Used for distance-based staleness calculation.
        """
        return self._edge_origin_price

    def refresh_edge_origin(self, new_origin_price: float, market_time: Optional[datetime] = None) -> None:
        """
        ser BOS level.

        When a continuation BOS occurs closer to price while edge is ACTIVE,
        update the origin to reduce distance-based penalties. This prevents
        the edge from being penalized for "distance" when the market has
        given a fresh signal at a closer level.

        Only refreshes if the new origin is CLOSER to current price than the old one.
        """
        if self._state not in (EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL):
            return

        old_origin = self._edge_origin_price
        self._edge_origin_price = new_origin_price

        # Also refresh the activation timestamp to reset time-based stale
        ts = market_time or datetime.now(UTC)
        self._edge_activated_at = ts

        logger.info(
            f"🔄 ESH: origin ${old_origin:.0f} → ${new_origin_price:.0f} | "
            f"Edge timer reset at {ts.strftime('%H:%M:%S')}"
        )

    def can_trade(self) -> bool:
        """
        ¿Puede el sistema ejecutar un trade?
        Requiere EDGE_ACTIVE o EDGE_PROVISIONAL y que el edge no esté quemado.
        """
        if self._state not in [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            return False
        if self._current_bos is None:
            return False
        return self._current_bos.can_trade()

    def get_block_reason(self) -> Optional[str]:
        """Razón por la que no se puede operar - informative format

        n
        """
        if self._state == EdgeState.NO_EDGE:
            # Provide informative reason instead of just "NO_EDGE"
            return f"NO_EDGE: {self._no_edge_reason}"
        elif self._state == EdgeState.EDGE_ARMED:
            return f"ARMED:wait_confirm"
        elif self._state == EdgeState.EDGE_PROVISIONAL:
            return "PROVISIONAL:starter"
        elif self._state == EdgeState.EDGE_EXPIRED:
            return "EXPIRED"
        elif self._state == EdgeState.EDGE_ACTIVE:
            if self._current_bos and self._current_bos.is_burned():
                return f"BURNED:{self._current_bos.trades_attempted}att"
        return None

    # =========================================================================
    # POST-DISPLACEMENT COOLDOWN METHODS
    # =========================================================================

    def set_post_displacement_cooldown(
        self,
        direction: str,
        market_time: datetime,
        cooldown_minutes: int = 10
    ) -> None:
        """
        Set cooldown after DISPLACEMENT edge expires.

        Prevents creating new CONSENSUS edge
        immediately after a displacement expires. The market needs to
        show NEW structure, not just re-arm on the same stale setup.

        Args:
            direction: 'SHORT' or 'LONG' - the displacement direction that expired
            market_time: Current timestamp
            cooldown_minutes: How long to wait (default 10 min)
        """
        self._post_displacement_cooldown_until = market_time + timedelta(minutes=cooldown_minutes)
        self._post_displacement_cooldown_direction = direction
        logger.warning(
            f"POST-DISPLACEMENT COOLDOWN | {direction} blocked for {cooldown_minutes}min | "
            f"Momentum expired - wait for NEW structure"
        )

    def is_post_displacement_cooldown_active(self, direction: str, market_time: datetime) -> bool:
        """
        Check if post-displacement cooldown is active for given direction.

        Returns True if we should NOT arm a CONSENSUS edge in this direction.
        """
        if self._post_displacement_cooldown_until is None:
            return False
        if self._post_displacement_cooldown_direction != direction:
            return False
        # Ensure both datetimes are timezone-aware to prevent crash
        cooldown_until = self._post_displacement_cooldown_until
        mt = market_time
        if mt.tzinfo is None and cooldown_until.tzinfo is not None:
            mt = mt.replace(tzinfo=UTC)
        elif mt.tzinfo is not None and cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=UTC)
        if mt >= cooldown_until:
            # Cooldown expired - clear it
            self._post_displacement_cooldown_until = None
            self._post_displacement_cooldown_direction = None
            logger.info(f"🔓 POST-DISPLACEMENT COOLDOWN expired for {direction} | New edges allowed")
            return False
        return True

    def is_simple_direction_cooldown_active(self, direction: str, market_time: datetime) -> tuple:
        """DEAD_EDGE direction cooldown REMOVED — was causing 162x blocks (16h wasted) in trending markets."""
        return False, 0

    # =========================================================================
    # RANGE EDGE DETECTION - CON SWEEP + REJECTION
    # =========================================================================

    # 🔥 AJUSTE (Jan 28): Compresión extrema NO bloquea PROBE
    # Antes: COMPRESSION_DISABLES_RANGE_PCT = 5 → bloqueaba PROBE
    # Ahora: Dejamos que ProbeValidator decida rentabilidad en compresión
    # Usar -1 para deshabilitar completamente
    COMPRESSION_DISABLES_RANGE_PCT = -1  # Disabled - never block by compression

    # Buffer para sweep (evitar micro-ruido)
    SWEEP_ATR_BUFFER = 0.1  # Sweep debe ser >= 0.1 ATR para ser válido

    @staticmethod
    def check_sweep_and_rejection(
        direction: str,
        curr_high: float,
        curr_low: float,
        curr_open: float,
        curr_close: float,
        prev_high: float,
        prev_low: float,
        atr: float = 0.0  # 🔥 NUEVO: ATR para buffer
    ) -> tuple[bool, float | None, str]:
        """
        Verifica sweep + rejection en extremo de rango.

        SWEEP = El mercado intentó romper el extremo previo (con buffer ATR)
        REJECTION = El cierre rechazó el intento inmediatamente

        Args:
            direction: 'LONG' o 'SHORT'
            curr_*: Datos de la vela actual
            prev_*: Datos de la vela anterior
            atr: ATR para calcular buffer de sweep

        Returns:
            (is_valid, invalidation_level, reason)
            - is_valid: True si hay sweep + rejection válido
            - invalidation_level: Nivel donde se invalida el edge (SL binario)
            - reason: Descripción del resultado para logging
        """
        # Buffer mínimo para evitar micro-ruido
        sweep_buffer = atr * EdgeStateManager.SWEEP_ATR_BUFFER if atr > 0 else 0

        if direction == "LONG":
            # SWEEP: El low actual rompió el low anterior - buffer (intención real)
            sweep_threshold = prev_low - sweep_buffer
            sweep = curr_low < sweep_threshold
            sweep_depth = prev_low - curr_low if sweep else 0
            # REJECTION: La vela cerró alcista (rechazo del sweep)
            rejection = curr_close > curr_open
            # INVALIDATION: Si rompe este low, la tesis está mal
            invalidation_level = curr_low

            # Logging detallado
            reason = (f"LONG: low={curr_low:.2f} vs prev_low={prev_low:.2f} "
                     f"(thresh={sweep_threshold:.2f}) | sweep={sweep} depth={sweep_depth:.2f} | "
                     f"rejection={rejection} (close={curr_close:.2f} vs open={curr_open:.2f})")

        elif direction == "SHORT":
            # SWEEP: El high actual rompió el high anterior + buffer (intención real)
            sweep_threshold = prev_high + sweep_buffer
            sweep = curr_high > sweep_threshold
            sweep_depth = curr_high - prev_high if sweep else 0
            # REJECTION: La vela cerró bajista (rechazo del sweep)
            rejection = curr_close < curr_open
            # INVALIDATION: Si rompe este high, la tesis está mal
            invalidation_level = curr_high

            # Logging detallado
            reason = (f"SHORT: high={curr_high:.2f} vs prev_high={prev_high:.2f} "
                     f"(thresh={sweep_threshold:.2f}) | sweep={sweep} depth={sweep_depth:.2f} | "
                     f"rejection={rejection} (close={curr_close:.2f} vs open={curr_open:.2f})")

        else:
            return False, None, "INVALID_DIRECTION"

        logger.debug(f"[SWEEP_CHECK] {reason}")

        if sweep and rejection:
            logger.info(f"✅ [SWEEP+REJECTION] {reason}")
            return True, invalidation_level, reason

        return False, None, reason

    def check_for_range_edge(
        self,
        current_price: float,
        htf_bias: str | None,  # Puede ser None si HTF no tiene bias claro
        percentile_60d: float,
        atr: float,
        market_time: datetime,
        # 🔥 NUEVOS PARÁMETROS PARA SWEEP + REJECTION
        curr_high: float = 0.0,
        curr_low: float = 0.0,
        curr_open: float = 0.0,
        curr_close: float = 0.0,
        prev_high: float = 0.0,
        prev_low: float = 0.0,
        # Parámetros legacy (ignorados con PROBING_ENABLED=False)
        vpoc_distance_atr: float = 0.0,
        vwap_distance_atr: float = 0.0,
        probing_score: int = 0,
        # 🔥 NUEVO: BB width percentile para detectar compresión
        bb_width_percentile: float = 50.0
    ) -> bool:
        """
        Detecta edge en mercado de RANGO con SWEEP + REJECTION obligatorio.

        FILOSOFÍA INSTITUCIONAL:
        - Localización (percentil 10/90) NO es edge, es contexto
        - El edge existe SOLO si hay:
          1. Sweep del extremo previo (intento de ruptura)
          2. Rejection inmediato (vela de rechazo)
        - El SL es BINARIO: el nivel del sweep
        - COMPRESIÓN EXTREMA DESACTIVA RANGE (pre-breakout territory)

        Sin sweep + rejection → NO hay trade (no importa la localización)

        🔥 MUTUAL EXCLUSION (Jan 28, 2026):
        - Si BREAKOUT edge está ARMED → RANGE bloqueado automáticamente
        - Si PROVISIONAL INTRADAY activo → RANGE bloqueado (correlación oculta)
        - BREAKOUT invalida RANGE, no compiten

        Returns: True si se activó edge con sweep+rejection confirmado
        """
        # =================================================================
        # 🔥 MUTUAL EXCLUSION: Si BREAKOUT armado → RANGE bloqueado
        # BREAKOUT invalida RANGE, no compiten entre sí
        # =================================================================
        if self._state == EdgeState.EDGE_ARMED and self._current_bos:
            if self._current_bos.edge_type == EdgeType.BREAKOUT:
                logger.debug(
                    "[RANGE_BLOCKED] BREAKOUT edge armado - RANGE deshabilitado por mutual exclusion"
                )
                return False

        # No crear nuevo edge si ya hay uno activo
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False

        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._state = EdgeState.NO_EDGE

        # =================================================================
        # 🔥 PASO 0: COMPRESIÓN EXTREMA DESACTIVA RANGE
        # En squeeze extremo no hay sweeps reales, solo ruido
        # =================================================================
        if bb_width_percentile <= self.COMPRESSION_DISABLES_RANGE_PCT:
            logger.info(
                f"⛔ [RANGE_DISABLED] BB width percentile={bb_width_percentile:.0f}% "
                f"<= {self.COMPRESSION_DISABLES_RANGE_PCT}% | EXTREME_COMPRESSION - no range edge possible"
            )
            return False

        # =================================================================
        # PASO 1: VERIFICAR LOCALIZACIÓN (extremo del rango)
        # =================================================================
        direction = None

        if percentile_60d >= RangeEdgeCriteria.EXTREME_HIGH_PCT:
            # Extremo alto → potencial SHORT
            if htf_bias in ['SHORT', 'NEUTRAL', None]:
                direction = 'SHORT'
                logger.debug(f"[LOCATION_CHECK] pct={percentile_60d:.0f}% >= {RangeEdgeCriteria.EXTREME_HIGH_PCT} → potencial SHORT")
            else:
                logger.info(f"⏸️ [RANGE_BLOCKED] pct={percentile_60d:.0f}% extremo alto pero HTF bias={htf_bias} bloquea SHORT")
                return False

        elif percentile_60d <= RangeEdgeCriteria.EXTREME_LOW_PCT:
            # Extremo bajo → potencial LONG
            if htf_bias in ['LONG', 'NEUTRAL', None]:
                direction = 'LONG'
                logger.debug(f"[LOCATION_CHECK] pct={percentile_60d:.0f}% <= {RangeEdgeCriteria.EXTREME_LOW_PCT} → potencial LONG")
            else:
                logger.info(f"⏸️ [RANGE_BLOCKED] pct={percentile_60d:.0f}% extremo bajo pero HTF bias={htf_bias} bloquea LONG")
                return False
        else:
            # NO estamos en extremo → NO hay edge posible (no loguear - spam)
            return False

        # =================================================================
        # PASO 2: VERIFICAR SWEEP + REJECTION (obligatorio)
        # =================================================================

        # Verificar que tenemos datos de candles
        if curr_high == 0 or prev_high == 0:
            logger.debug(f"⏸️ [SWEEP_CHECK] Sin datos de candles para sweep+rejection")
            return False

        sweep_valid, invalidation_level, sweep_reason = self.check_sweep_and_rejection(
            direction=direction,
            curr_high=curr_high,
            curr_low=curr_low,
            curr_open=curr_open,
            curr_close=curr_close,
            prev_high=prev_high,
            prev_low=prev_low,
            atr=atr  # 🔥 Pasar ATR para buffer
        )

        if not sweep_valid:
            # Sin sweep+rejection → NO hay edge (solo localización)
            logger.info(
                f"⛔ [RANGE_NO_EDGE] pct={percentile_60d:.0f}% ({direction}) | "
                f"NO sweep+rejection | {sweep_reason}"
            )
            return False

        # Guard: invalidation_level should always exist if sweep_valid=True
        if invalidation_level is None:
            logger.warning(f"⚠️ [RANGE_BUG] sweep_valid=True but invalidation_level=None")
            return False

        # =================================================================
        # PASO 3: EDGE CONFIRMADO - Crear con SL binario y transicionar
        # =================================================================

        # Crear edge de RANGO con invalidación binaria
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=htf_bias,
            bos_level=invalidation_level,  # SL binario = nivel del sweep
            atr_at_bos=atr,
            edge_type=EdgeType.RANGE,
            percentile_60d=percentile_60d,
            vpoc_distance_atr=vpoc_distance_atr,
            vwap_distance_atr=vwap_distance_atr,
            probing_score=probing_score,
            pullback_detected_at=market_time  # RANGE activa inmediatamente
        )

        # Stats
        self._stats.total_bos_events += 1
        self._stats.range_edges += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1

        # Transición fluida a ACTIVE (RANGE no pasa por ARMED)
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"pct={percentile_60d:.0f}% sweep+rej SL:{invalidation_level:.0f}",
            market_time
        )
        return True

    # =========================================================================
    # 🔥 BREAKOUT EDGE DETECTION (Jan 27, 2026)
    # =========================================================================

    def check_for_breakout_edge(
        self,
        current_price: float,
        bb_width_percentile: float,
        volume_ratio: float,
        direction: str,  # Dirección del anticipation
        atr: float,
        market_time: datetime,
        structure_level: float = 0.0  # Structure level for trigger
    ) -> bool:
        """
        Detecta edge de BREAKOUT (compresión).

        Llamado cuando Anticipation detecta COMPRESSION_BREAKOUT.
        Arma el edge, esperando confirmación de ruptura.

        structure_level is the "Building X from Y" level.
        When price breaks this level, the edge is confirmed.

        BREAKOUT tiene PRIORIDAD sobre TREND edge.
        Si hay un TREND edge activo, BREAKOUT puede OVERRIDE porque tiene
        mayor convicción (compresión + anticipation vs solo consensus).

        Returns: True si se armó edge
        """
        # Check context cooldown
        if self.is_context_in_cooldown(EdgeType.BREAKOUT, market_time):
            return False

        # =====================================================================
        # BREAKOUT OVERRIDE LOGIC
        # BREAKOUT tiene prioridad 80, TREND tiene 20-40
        # Si hay TREND activo, lo reemplazamos (mayor convicción)
        # =====================================================================
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._current_bos:
                current_priority = EdgePriority.get_priority(self._current_bos.edge_type)
                breakout_priority = EdgePriority.BREAKOUT

                if current_priority >= breakout_priority:
                    # Edge actual tiene igual o mayor prioridad - no override
                    logger.debug(
                        f"[BREAKOUT_BLOCKED] Current edge {self._current_bos.edge_type.value} "
                        f"has priority {current_priority} >= {breakout_priority}"
                    )
                    return False
                else:
                    # BREAKOUT tiene mayor prioridad - OVERRIDE
                    logger.info(
                        f"🔥 BREAKOUT OVERRIDE: Replacing {self._current_bos.edge_type.value} "
                        f"(priority {current_priority}) with BREAKOUT (priority {breakout_priority})"
                    )
                    self._expire_edge(f"BREAKOUT override {self._current_bos.edge_type.value}")

        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)

        # More relaxed criteria when we have structure_level
        # With structure level, we already have a clear breakout level to watch
        has_structure = structure_level > 0

        # Verificar criterios de compresión
        is_squeezed = bb_width_percentile <= BreakoutEdgeCriteria.BB_WIDTH_PERCENTILE_MAX
        vol_declining = volume_ratio <= BreakoutEdgeCriteria.VOLUME_DECLINE_RATIO

        # Allow edge with structure_level even if volume not declining
        criteria_met = (is_squeezed and vol_declining) or (has_structure and is_squeezed)

        if criteria_met:
            # Use structure_level as bos_level if available
            bos_level = structure_level if has_structure else current_price

            # Armar edge de breakout (esperar confirmación)
            self._current_bos = BOSEvent(
                timestamp=market_time,
                price=current_price,
                direction=direction,
                htf_bias=direction,
                bos_level=bos_level,  # Structure level as trigger
                atr_at_bos=atr,
                edge_type=EdgeType.BREAKOUT,
                bb_width_percentile=bb_width_percentile,
                volume_ratio=volume_ratio
            )

            # Stats
            self._stats.total_bos_events += 1
            self._stats.breakout_edges += 1

            # Transición fluida
            level_info = f"level=${bos_level:.0f}" if has_structure else ""
            self._transition_to(
                EdgeState.EDGE_ARMED,
                f"BB%={bb_width_percentile:.0f} {level_info}",
                market_time
            )
            return True

        return False

    def confirm_breakout(
        self,
        current_price: float,
        breakout_direction: str,
        atr: float,
        market_time: datetime,
        bb_upper: float = 0.0,
        bb_lower: float = 0.0,
        close_price: float = 0.0,
        expansion_ratio: float = 0.0
    ) -> bool:
        """
        Confirma ruptura del breakout con criterios INSTITUCIONALES.

        ANTICIPATION EDGE TRIGGER
        Criterios (en orden de prioridad):
        1. STRUCTURE LEVEL BREAK: Price breaks bos_level (from ANTICIPATION)
        2. Close FUERA de Bollinger Bands (fallback)
        3. Dirección coincide con anticipation
        """
        if self._state != EdgeState.EDGE_ARMED:
            return False

        if self._current_bos is None or self._current_bos.edge_type != EdgeType.BREAKOUT:
            return False

        # Verificar que dirección coincide
        if breakout_direction != self._current_bos.direction:
            self._expire_edge(f"dir mismatch: {breakout_direction} ≠ {self._current_bos.direction}")
            return False

        # PRIMARY TRIGGER - Structure level break
        # When ANTICIPATION provides structure_level, use it as trigger
        structure_level = self._current_bos.bos_level
        has_structure_trigger = False
        structure_broken = False

        # Check if we have a real structure level (not just current_price)
        if abs(structure_level - self._current_bos.price) > 0.5 * atr:
            # bos_level is different from squeeze level = we have structure
            has_structure_trigger = True

            # 🧠 SMART CONFIRMATION: Require MOMENTUM, not just touch
            # Price must be at least 0.1 ATR BEYOND the level (not just touching)
            momentum_buffer = 0.1 * atr

            if breakout_direction == 'SHORT' and current_price < (structure_level - momentum_buffer):
                structure_broken = True
            elif breakout_direction == 'LONG' and current_price > (structure_level + momentum_buffer):
                structure_broken = True

        # CRITERIO 1: Structure level break WITH MOMENTUM (ANTICIPATION trigger)
        if has_structure_trigger and structure_broken:
            # 🎯 INSTANT TRIGGER: Price broke structure level from ANTICIPATION
            self._current_bos.pullback_detected_at = market_time
            self._current_bos.breakout_confirmation_price = current_price
            self._current_bos.breakout_bb_upper = bb_upper
            self._current_bos.breakout_bb_lower = bb_lower

            self._transition_to(
                EdgeState.EDGE_ACTIVE,
                f"STRUCTURE BREAK ${structure_level:.0f} → ${current_price:.0f}",
                market_time
            )
            return True

        # CRITERIO 2: Close FUERA de Bollinger Bands (fallback for non-structure)
        close_outside_bb = False
        if bb_upper > 0 and bb_lower > 0 and close_price > 0:
            if breakout_direction == 'LONG' and close_price > bb_upper:
                close_outside_bb = True
            elif breakout_direction == 'SHORT' and close_price < bb_lower:
                close_outside_bb = True
        else:
            # Fallback: usar precio actual vs squeeze level + 0.5 ATR
            squeeze_level = self._current_bos.price
            if breakout_direction == 'LONG':
                close_outside_bb = current_price > (squeeze_level + 0.5 * atr)
            else:
                close_outside_bb = current_price < (squeeze_level - 0.5 * atr)

        if not close_outside_bb:
            return False  # Still waiting for confirmation

        # Guardar datos para invalidación lógica
        self._current_bos.pullback_detected_at = market_time
        self._current_bos.breakout_confirmation_price = current_price
        self._current_bos.breakout_bb_upper = bb_upper
        self._current_bos.breakout_bb_lower = bb_lower

        # Transición fluida ARMED → ACTIVE
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"close outside BB, expansion={expansion_ratio:.2f}",
            market_time
        )
        return True

    # =========================================================================
    # 🌊 DISPLACEMENT EDGE DETECTION (Jan 30, 2026)

    # PROBLEMA RESUELTO:
    # - Sistema detectaba EXPANSION_MODE + DIRECTION_LOCK correctamente
    # - Pero NO se armaba EDGE → 0 trades en caída de 2.3%
    # - RANGE_EDGE solo permite LONG en pct bajo, SHORT en pct alto
    # - TREND_EDGE necesita BOS + pullback (no había BOS)

    # SOLUCIÓN:
    # - DISPLACEMENT_EDGE se arma AUTOMÁTICAMENTE con EXPANSION_MODE
    # - El displacement ES el edge - no necesita más confirmación
    # - El mercado YA DECIDIÓ con momentum institucional
    # =========================================================================

    def arm_displacement_edge(
        self,
        current_price: float,
        direction: str,
        displacement_amount: float,
        atr: float,
        market_time: datetime,
        initial_price: float = 0.0,
        is_stale: bool = False,
        stale_distance: float = 0.0
    ) -> bool:
        """
        Arma un DISPLACEMENT_EDGE cuando se detecta movimiento fuerte.

        LLAMAR cuando EXPANSION_MODE + DIRECTION_LOCK se activan.

        A diferencia de otros edges:
        - NO necesita BOS (el displacement ES la ruptura)
        - NO necesita sweep/rejection (no es mean reversion)
        - NO necesita compresión (es lo opuesto - expansión)

        El displacement ES el edge - el mercado ya mostró intención institucional.

        🔥 CRITICAL (Jan 30, 2026): ENTRY WINDOW + DIRECT ENTRY
        - En las primeras barras, forzar entrada directa (no esperar pullback)
        - Si pasa la ventana de entrada, NO perseguir

        STALE DISPLACEMENT BLOCKING
        - Si is_stale=True, NO armar edge (momentum exhausted)
        - STALE = precio cerca del extremo reciente (< 0.5 ATR)

        Args:
            is_stale: True si el displacement está agotado (precio cerca del extremo)
            stale_distance: Distancia al extremo (para logging)

        Returns: True si se armó el edge
        """
        # STALE CHECK FIRST
        # If displacement is stale (price near extreme), do NOT arm edge
        if is_stale:
            logger.warning(
                f"🚫 DISPLACEMENT EDGE BLOCKED (STALE): {direction} | "
                f"Price only ${stale_distance:.0f} from extreme (< 0.5 ATR) | "
                f"Momentum exhausted → NOT arming"
            )
            return False

        # Check context cooldown
        if self.is_context_in_cooldown(EdgeType.DISPLACEMENT, market_time):
            logger.debug(f"[DISPLACEMENT_EDGE] Cooldown activo - no armar")
            return False

        # Si ya hay edge, verificar si debemos reemplazarlo
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            # Si es el mismo tipo y dirección, reforzar
            if self._current_bos and self._current_bos.edge_type == EdgeType.DISPLACEMENT:
                if self._current_bos.direction == direction:
                    # Mismo displacement, ya armado - no hacer nada
                    return False
                else:
                    # Dirección opuesta - invalidar el anterior
                    self._expire_edge(f"displacement flip: {self._current_bos.direction}→{direction}")
            else:
                # Otro tipo de edge - DISPLACEMENT tiene prioridad en EXPANSION_MODE
                logger.info(
                    f"🌊 DISPLACEMENT OVERRIDE: Reemplazando {self._current_bos.edge_type.value if self._current_bos else 'None'} "
                    f"con DISPLACEMENT {direction}"
                )
                self._expire_edge(f"displacement override: {direction}")

        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)

        # =====================================================================
        # 🔥 INVALIDATION LEVEL DESDE ORIGEN (Jan 30, 2026)
        # El nivel de invalidación se calcula desde el ORIGEN del movimiento
        # No desde el precio actual (que puede estar muy lejos en cascadas)
        # =====================================================================
        invalidation_buffer = displacement_amount * DisplacementEdgeCriteria.INVALIDATION_OPPOSITE_DISPLACEMENT_MULT

        # origin_price = desde donde empezó el movimiento
        origin = initial_price if initial_price > 0 else current_price

        if direction == 'LONG':
            # En LONG, invalidar si cae por debajo del origen
            invalidation_level = origin
        else:
            # En SHORT, invalidar si sube por encima del origen
            invalidation_level = origin

        # Crear edge de DISPLACEMENT con datos de origen
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,  # Displacement define su propio bias
            bos_level=invalidation_level,
            atr_at_bos=atr,
            edge_type=EdgeType.DISPLACEMENT,
            # 🔥 NUEVOS CAMPOS para DISPLACEMENT
            origin_price=origin,
            initial_displacement_amount=displacement_amount,
            bars_since_displacement=0,
            entry_window_open=True,
            force_direct_entry=True  # Primeras barras = entrada directa permitida
        )

        # Stats
        self._stats.total_bos_events += 1
        self._stats.displacement_edges += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1

        # =====================================================================
        # 🔥 TRANSICIÓN DIRECTA A ACTIVE (Jan 30, 2026)
        # En displacement, NO esperamos pullback en las primeras barras
        # El edge se activa INMEDIATAMENTE para permitir entrada directa
        # =====================================================================
        self._transition_to(
            EdgeState.EDGE_ACTIVE,  # 🔥 DIRECTO A ACTIVE, no ARMED
            f"DISP ${displacement_amount:.0f} {direction} | DIRECT ENTRY WINDOW OPEN",
            market_time
        )

        _edge_log(
            "EDGE_ACTIVE", "DISPLACEMENT", direction,
            f"Displacement ${displacement_amount:.0f} | Origin ${origin:.0f} | DIRECT ENTRY ALLOWED"
        )

        return True

    def confirm_displacement_edge(
        self,
        current_price: float,
        displacement_direction: str,
        initial_displacement: float,
        current_displacement: float,
        atr: float,
        market_time: datetime
    ) -> bool:
        """
        Confirma DISPLACEMENT_EDGE para pasar de ARMED → ACTIVE.

        Confirmación:
        1. Pullback pequeño (15-50% del displacement) = entrada óptima
        2. Continuación (+10% más) = follow-through confirmado

        Si el precio SIGUE moviéndose en la dirección del displacement,
        se activa directamente sin esperar pullback.
        """
        if self._state != EdgeState.EDGE_ARMED:
            return False

        if self._current_bos is None or self._current_bos.edge_type != EdgeType.DISPLACEMENT:
            return False

        # Verificar dirección
        if displacement_direction != self._current_bos.direction:
            self._expire_edge(f"displacement dir flip: {displacement_direction} ≠ {self._current_bos.direction}")
            return False

        # Verificar timeout
        armed_duration = (market_time - self._current_bos.timestamp).total_seconds() / 60
        if armed_duration > DisplacementEdgeCriteria.CONFIRMATION_TIMEOUT_MIN:
            # Timeout pero NO expirar - darle más tiempo si displacement sigue activo
            if current_displacement >= initial_displacement * 0.5:
                # Displacement sigue activo, activar directamente
                logger.info(
                    f"🌊 DISPLACEMENT EDGE: Timeout pero displacement activo "
                    f"(${current_displacement:.0f} >= ${initial_displacement * 0.5:.0f}) - activando"
                )
            else:
                self._expire_edge(f"displacement timeout {armed_duration:.0f}min, displacement debilitado")
                return False

        # CONFIRMACIÓN 1: Continuación del displacement (+10% más)
        continuation_threshold = initial_displacement * (1 + DisplacementEdgeCriteria.CONTINUATION_THRESHOLD_PCT)
        if current_displacement >= continuation_threshold:
            self._current_bos.pullback_detected_at = market_time
            self._transition_to(
                EdgeState.EDGE_ACTIVE,
                f"follow-through: ${current_displacement:.0f} >= ${continuation_threshold:.0f}",
                market_time
            )
            return True

        # CONFIRMACIÓN 2: Pullback pequeño (15-50% del displacement)
        # Un pullback es una OPORTUNIDAD, no una invalidación
        pullback_amount = initial_displacement - current_displacement
        pullback_pct = pullback_amount / initial_displacement if initial_displacement > 0 else 0

        min_pullback = DisplacementEdgeCriteria.PULLBACK_MIN_PCT
        max_pullback = DisplacementEdgeCriteria.PULLBACK_MAX_PCT

        if min_pullback <= pullback_pct <= max_pullback:
            self._current_bos.pullback_detected_at = market_time
            self._transition_to(
                EdgeState.EDGE_ACTIVE,
                f"pullback {pullback_pct*100:.0f}% (${pullback_amount:.0f}) = entry zone",
                market_time
            )
            return True

        # Pullback demasiado grande = posible invalidación
        if pullback_pct > max_pullback:
            logger.warning(
                f"⚠️ DISPLACEMENT PULLBACK WARNING: {pullback_pct*100:.0f}% > {max_pullback*100:.0f}% max | "
                f"No invalidando aún - esperando"
            )

        return False

    def update_displacement_entry_window(
        self,
        market_time: datetime
    ) -> tuple[bool, str]:
        """
        Actualiza la ventana de entrada del DISPLACEMENT edge.

        🔥 CRITICAL (Jan 30, 2026):
        - Incrementa contador de barras
        - Cierra ventana de entrada si pasa el tiempo
        - MATA el edge si está demasiado tarde (NO PERSEGUIR)

        Returns: (should_kill_edge, reason)
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False, ""

        if self._current_bos is None or self._current_bos.edge_type != EdgeType.DISPLACEMENT:
            return False, ""

        bos = self._current_bos

        # Incrementar contador de barras (llamar cada nueva vela 1m)
        bos.bars_since_displacement += 1

        # Calcular tiempo transcurrido
        elapsed_min = (market_time - bos.timestamp).total_seconds() / 60

        # =====================================================================
        # 🔥 CERRAR VENTANA DE ENTRADA DIRECTA
        # Después de las primeras barras, ya no se permite entrada directa
        # =====================================================================
        if bos.bars_since_displacement > DisplacementEdgeCriteria.FORCE_DIRECT_ENTRY_BARS:
            if bos.force_direct_entry:
                bos.force_direct_entry = False
                logger.info(
                    f"⏰ DISPLACEMENT DIRECT ENTRY CLOSED: {bos.bars_since_displacement} bars > "
                    f"{DisplacementEdgeCriteria.FORCE_DIRECT_ENTRY_BARS} | Now requires pullback"
                )

        # =====================================================================
        # 🔥 CERRAR VENTANA DE ENTRADA COMPLETA
        # Después del tiempo máximo, ya no se puede entrar
        # =====================================================================
        if bos.bars_since_displacement > DisplacementEdgeCriteria.MAX_ENTRY_WINDOW_BARS:
            bos.entry_window_open = False

        if elapsed_min > DisplacementEdgeCriteria.MAX_ENTRY_WINDOW_MIN:
            bos.entry_window_open = False

        # =====================================================================
        # 🔥 KILL LATE EDGE (NO PERSEGUIR)
        # Si pasó demasiado tiempo sin entrada, matar el edge

        # Expirar edge después de KILL threshold
        # SIEMPRE, no solo si trades_attempted == 0.
        # Si ya hubo 1+ trade y estamos >5 barras, el edge está muerto.

        # Si entry_window_open = False, el edge está funcionalmente
        # muerto para nuevas entradas. Matarlo para evitar evaluaciones zombie
        # que generan spam de "MISSED" cuando el precio se mueve contra la dirección.
        # =====================================================================
        if bos.bars_since_displacement > DisplacementEdgeCriteria.KILL_EDGE_AFTER_BARS:
            # Si no ha entrado, matar inmediatamente
            if bos.trades_attempted == 0:
                return True, f"LATE DISPLACEMENT: {bos.bars_since_displacement} bars > {DisplacementEdgeCriteria.KILL_EDGE_AFTER_BARS} - NO CHASE"
            # Si ya entró Y la ventana está cerrada, matar también
            # Esto evita que el edge siga evaluando y generando MISSED spam
            elif not bos.entry_window_open:
                return True, f"DISPLACEMENT WINDOW CLOSED: {bos.bars_since_displacement} bars, {bos.trades_attempted} trades taken"

        if elapsed_min > DisplacementEdgeCriteria.KILL_EDGE_AFTER_MIN:
            if bos.trades_attempted == 0:  # Solo si no ha entrado
                return True, f"LATE DISPLACEMENT: {elapsed_min:.0f}min > {DisplacementEdgeCriteria.KILL_EDGE_AFTER_MIN}min - NO CHASE"

        return False, ""

    # =========================================================================
    # 🔥 CONSENSUS EDGE (Feb 05, 2026)
    # Cuando hay consenso entre structural y active direction en TREND mode,
    # creamos un edge directamente sin necesitar BOS.

    # FILOSOFÍA: Si dos fuentes independientes (HTF structure + momentum)
    # coinciden en dirección, hay suficiente evidencia para tradear.

    # CONSENSUS es el fallback de menor prioridad
    # Si hay un edge de mayor prioridad pendiente (BREAKOUT, DISPLACEMENT),
    # NO armar CONSENSUS para no bloquear el edge mejor.
    # =========================================================================

    def arm_consensus_edge(
        self,
        direction: str,
        current_price: float,
        atr: float,
        market_time: datetime,
        reason: str = "CONSENSUS",
        macro_bias_strength: str | None = None,
        has_momentum_override: bool = False,
        forbidden_direction: str | None = None
    ) -> bool:
        """
        Arma un CONSENSUS_EDGE cuando structural == active en TREND mode.

        CONSENSUS tiene la MENOR prioridad (20).
        MACRO BIAS COHERENCE CHECK
        FORBIDDEN DIRECTION CHECK

        Este edge NO requiere:
        - BOS (el consenso ES la señal)
        - Sweep/rejection (no es mean reversion)
        - Compresión (no es breakout)

        REQUIERE:
        - structural_bias == active_direction
        - ADX >= 20 (confirmado por trading_mode='TREND')
        - macro_bias_strength coherente con direction:
          * BEAR_STRONG + LONG = INCOHERENTE → BLOCKED (unless momentum_override)
          * BULL_STRONG + SHORT = INCOHERENTE → BLOCKED (unless momentum_override)

        NO puede armar edge si direction == forbidden_direction
        Sizing: 0.40-0.60x (menor que BOS_PULLBACK por ser entrada directa)

        Returns: True si se armó el edge
        """
        # =====================================================================
        # FORBIDDEN DIRECTION CHECK

        # PROBLEMA: El log mostró que cuando hay BIAS FLIP (ej: "FORBID SHORT,
        # LONG detection ENABLED"), el sistema seguía creando edges SHORT porque
        # arm_consensus_edge() no verificaba forbidden_direction.

        # SOLUCIÓN: Si direction == forbidden_direction, BLOQUEAR el edge.
        # Esto es PRIORITARIO sobre todo lo demás (incluso momentum override).
        # =====================================================================
        # =====================================================================
        # RESET EXPIRED STATE BEFORE ANY GUARDS

        # CRITICAL: The EXPIRED→NO_EDGE reset MUST happen before cooldown,
        # forbidden, and macro checks. If any guard returns False early,
        # the reset at the old location (after all guards) was never reached,
        # leaving state permanently stuck in EDGE_EXPIRED.
        # =====================================================================
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired for consensus", market_time)

        if forbidden_direction and direction == forbidden_direction:
            logger.warning(
                f"🚫 CONSENSUS BLOCKED by FORBIDDEN_DIRECTION | "
                f"Cannot create {direction} edge while {direction} is forbidden | "
                f"reason={reason}"
            )
            return False

        # =====================================================================
        # MACRO BIAS COHERENCE CHECK

        # Always block edges that contradict EXTREME macro (BEAR_STRONG / BULL_STRONG).
        # REGIME FUSION blocks ALL fades against strong macro (strength > 2.0),
        # so creating such edges only wastes 12+ minutes as DEAD_EDGE.

        # LOG EVIDENCE (Feb 23): 7+ LONG edges created in BEAR_STRONG with
        # momentum_override=True. ALL blocked by REGIME FUSION → 12-min deadlock
        # each time. ~2 hours wasted on dead LONG edges in a single session.

        # Added safeguard: macro=None → BLOCK (prevents first-tick bypass)
        # =====================================================================
        if not macro_bias_strength:
            # Macro not yet calculated — BLOCK edge arming.
            _last_none_log = getattr(self, '_last_macro_none_log', 0)
            import time as _time_mod
            _now_ts = _time_mod.time()
            if _now_ts - _last_none_log >= 30:
                self._last_macro_none_log = _now_ts
                logger.warning(
                    f"🚫 CONSENSUS BLOCKED: macro not yet calculated (None) | "
                    f"direction={direction} | reason={reason} | Waiting for macro"
                )
            return False

        is_incoherent = (
            (macro_bias_strength == 'BEAR_STRONG' and direction == 'LONG') or
            (macro_bias_strength == 'BULL_STRONG' and direction == 'SHORT')
        )
        if is_incoherent:
            import time as _time_mod
            _now_ts = _time_mod.time()
            _last_consensus_block = getattr(self, '_last_consensus_block_log', 0)
            if _now_ts - _last_consensus_block >= 60:
                self._last_consensus_block_log = _now_ts
                logger.warning(
                    f"🚫 CONSENSUS BLOCKED: {direction} incoherent with macro={macro_bias_strength} | "
                    f"reason={reason} | momentum_override={has_momentum_override} (ignored — REGIME FUSION would block)"
                )
            return False
        # =====================================================================
        # CONSENSUS es el FALLBACK más bajo
        # Si ya hay edge de cualquier tipo, NO reemplazar
        # (edges de mayor prioridad pueden override CONSENSUS, no al revés)
        # =====================================================================
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._current_bos and self._current_bos.direction == direction:
                # Mismo edge, ya armado - no hacer nada
                return False
            # Edge diferente activo - CONSENSUS NO puede override nada
            logger.debug(
                f"[CONSENSUS_BLOCKED] Edge {self._current_bos.edge_type.value if self._current_bos else 'unknown'} "
                f"already active - CONSENSUS has lowest priority"
            )
            return False

        # =====================================================================
        # Post-displacement cooldown check
        # If a DISPLACEMENT edge recently expired, don't create new CONSENSUS edge
        # The market needs to show NEW structure, not re-arm on stale momentum
        # =====================================================================
        # POST-DISPLACEMENT COOLDOWN: REMOVED — was the #2 cause of bot paralysis.
        # Evidence: After displacement expires, market often starts a new move.
        # Blocking consensus for 5 minutes after displacement means missing
        # the follow-through trade. Direction coherence is already validated
        # by the macro checks above + direction_orchestrator upstream.

        # DEAD_EDGE cooldown REMOVED in refactor — was the #1 cause of bot paralysis.
        # Evidence: 162 blocks x ~6min = ~16 hours wasted in a single 12h session.
        # Direction_orchestrator + mode_handlers already gate direction coherence.

        # Reset from EXPIRED state to allow re-arming
        # Moved to top of method (before guards) — see above

        # =====================================================================
        # LOSS ZONE MEMORY CHECK
        # Don't re-enter the same direction in the same price zone after a loss.
        # This prevents the "3 identical losing entries" pattern from the log.
        # =====================================================================
        now_utc = datetime.now(UTC)
        self._loss_zone_memory = [z for z in self._loss_zone_memory if z['expires'] > now_utc]
        for zone in self._loss_zone_memory:
            if zone['direction'] == direction:
                zone_radius = zone['atr'] * self.LOSS_ZONE_RADIUS_ATR
                if abs(current_price - zone['price']) < zone_radius:
                    remaining_min = (zone['expires'] - now_utc).total_seconds() / 60
                    _last_zone_log = getattr(self, '_last_zone_memory_log', 0)
                    import time as _time_mod3
                    _now_ts3 = _time_mod3.time()
                    if _now_ts3 - _last_zone_log >= 60:
                        self._last_zone_memory_log = _now_ts3
                        logger.warning(
                            f"🚫 LOSS ZONE BLOCK: {direction} @ ${current_price:.0f} | "
                            f"Previous loss @ ${zone['price']:.0f} ± ${zone_radius:.0f} | "
                            f"~{remaining_min:.0f}min remaining | "
                            f"Won't repeat same losing setup"
                        )
                    return False

        # =====================================================================
        # CORRECT EDGE TYPE FOR RANGE CONSENSUS

        # LOG EVIDENCE: Edge source = RANGE_CONSENSUS but edge_type = TREND.
        # This caused TP to be set at 1.20R (trend) instead of 1.0R (range),
        # and trailing stop to use trend-style behavior (loose) instead of
        # range-style (tight). In a RANGE_BOUND market with ADX 18.4, this
        # mismatch causes systematic give-back of MFE.

        # FIX: If reason contains "RANGE", set edge_type to RANGE.
        # This flows through to TP calculation, trailing stop, and sizing.
        # =====================================================================
        is_range_consensus = 'RANGE' in reason.upper()
        consensus_edge_type = EdgeType.RANGE if is_range_consensus else EdgeType.TREND

        # Crear BOSEvent para consensus → EDGE_ARMED (NOT ACTIVE)
        # CONSENSUS va a ARMED — necesita confirmación de precio para activarse.
        # El pipeline de edge_pipeline._confirm_armed_edge o el tick-level
        # confirm_consensus_armed lo promueven a ACTIVE solo cuando el precio
        # confirma la dirección.
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,
            bos_level=current_price,
            atr_at_bos=atr,
            edge_type=consensus_edge_type,
            trend_path=TrendEdgePath.CONSENSUS_DIRECT,
            force_direct_entry=False,
            entry_window_open=False
        )

        # Stats
        self._stats.total_bos_events += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1

        edge_type_str = consensus_edge_type.value

        # ARMED, no ACTIVE — el precio debe confirmar antes de poder tradear
        self._transition_to(
            EdgeState.EDGE_ARMED,
            f"CONSENSUS_ARMED {direction} | {reason} | Awaiting price confirmation",
            market_time
        )

        _edge_log(
            "EDGE_ARMED", edge_type_str, direction,
            f"Consensus edge ARMED (NOT active) | {reason} | Need price to move {direction}"
        )

        return True

    def confirm_consensus_armed(
        self,
        current_price: float,
        candles_1m: list[dict] | None,
        market_time: datetime,
    ) -> bool:
        """
        Promote CONSENSUS ARMED → ACTIVE only when price confirms direction.

        This is the STRICT positive confirmation gate. The mode handlers already
        filtered out active contradictions (_price_confirms_direction as anti-
        contradiction). Here we require actual positive movement.

        Confirmation requires ALL of:
        1. Price moved ≥ 0.06% in the trade direction in last 5 closed candles
        2. Last closed candle's body is in the trade direction
        3. Edge is not stale (< 15 minutes since armed)

        Called every tick from the idle pipeline.
        Returns True if promoted to ACTIVE.
        """
        if self._state != EdgeState.EDGE_ARMED:
            return False
        if not self._current_bos:
            return False
        if self._current_bos.trend_path != TrendEdgePath.CONSENSUS_DIRECT:
            return False  # Only for CONSENSUS edges

        direction = self._current_bos.direction
        armed_price = self._current_bos.price
        atr = self._current_bos.atr_at_bos

        # Staleness check: expire after 15 minutes
        elapsed = (market_time - self._current_bos.timestamp).total_seconds()
        if elapsed > 900:
            self._transition_to(
                EdgeState.EDGE_EXPIRED,
                f"CONSENSUS expired: {elapsed:.0f}s without price confirmation",
                market_time
            )
            return False

        # Need 1m candle data for confirmation
        if not candles_1m or len(candles_1m) < 6:
            return False

        # Check last 5 closed candles (not current forming candle)
        recent_closed = candles_1m[-6:-1]  # 5 closed candles
        if not recent_closed:
            return False

        # 1. Price movement check: must have moved ≥ 0.06% in direction
        price_start = recent_closed[0].get('open', recent_closed[0].get('close', 0))
        price_end = recent_closed[-1].get('close', 0)
        if price_start <= 0 or price_end <= 0:
            return False

        move_pct = (price_end - price_start) / price_start * 100
        MIN_CONFIRM_PCT = 0.15  # 0.15% = ~$100 for BTC — filters noise, requires real move

        direction_confirms = (
            (direction == 'LONG' and move_pct >= MIN_CONFIRM_PCT) or
            (direction == 'SHORT' and move_pct <= -MIN_CONFIRM_PCT)
        )

        if not direction_confirms:
            return False

        # 2. Last closed candle body must be in trade direction
        last_candle = recent_closed[-1]
        candle_open = last_candle.get('open', 0)
        candle_close = last_candle.get('close', 0)
        body_confirms = (
            (direction == 'LONG' and candle_close > candle_open) or
            (direction == 'SHORT' and candle_close < candle_open)
        )

        if not body_confirms:
            return False

        # All checks passed — promote to ACTIVE
        self._current_bos.force_direct_entry = True
        self._current_bos.entry_window_open = True

        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"CONSENSUS CONFIRMED {direction} | move={move_pct:+.2f}% in {elapsed:.0f}s | Size 0.40-0.60x",
            market_time
        )

        edge_type_str = self._current_bos.edge_type.value if self._current_bos.edge_type else "CONSENSUS"
        _edge_log(
            "EDGE_ACTIVE", edge_type_str, direction,
            f"CONSENSUS confirmed by price: {move_pct:+.2f}% move + body confirms {direction}"
        )

        return True

    # =========================================================================
    # DETECCIÓN DE BOS (TREND - original)
    # =========================================================================

    def check_for_bos(
        self,
        current_price: float,
        htf_bias: str | None,
        swing_high: float,
        swing_low: float,
        prev_swing_high: float,
        prev_swing_low: float,
        atr: float,
        market_time: datetime
    ) -> bool:
        """
        Detecta Break of Structure.

        BOS LONG: Precio rompe swing high previo con bias alcista
        BOS SHORT: Precio rompe swing low previo con bias bajista

        Returns: True si se detectó nuevo BOS
        """
        if htf_bias is None:
            return False

        # Check context cooldown
        if self.is_context_in_cooldown(EdgeType.TREND, market_time):
            return False

        # No detectar nuevo BOS si ya tenemos uno activo
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False

        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)

        bos_detected = False
        direction = None
        bos_level = None
        break_distance = 0.0

        # BOS LONG: Rompe swing high previo
        if htf_bias == 'LONG':
            if current_price > prev_swing_high:
                break_distance = (current_price - prev_swing_high) / atr
                if 0.1 < break_distance < 1.5:
                    bos_detected = True
                    direction = 'LONG'
                    bos_level = prev_swing_high

        # BOS SHORT: Rompe swing low previo
        elif htf_bias == 'SHORT':
            if current_price < prev_swing_low:
                break_distance = (prev_swing_low - current_price) / atr
                if 0.1 < break_distance < 1.5:
                    bos_detected = True
                    direction = 'SHORT'
                    bos_level = prev_swing_low

        if bos_detected:
            assert direction is not None and bos_level is not None
            self._arm_edge(
                timestamp=market_time,
                price=current_price,
                direction=direction,
                htf_bias=htf_bias or 'UNKNOWN',
                bos_level=bos_level,
                atr=atr,
                break_distance=break_distance
            )
            return True

        return False

    def _arm_edge(
        self,
        timestamp: datetime,
        price: float,
        direction: str,
        htf_bias: str,
        bos_level: float,
        atr: float,
        edge_type: EdgeType = EdgeType.TREND,
        break_distance: float = 0.0,
        trend_path: Optional[TrendEdgePath] = None,  # 🔥 NEW: Path para TREND
        setup_origin_price: Optional[float] = None   # Price when setup started
    ):
        """
        Armar el edge tras detectar BOS.

        Para TREND edge, especificar el path:
        - BOS_PULLBACK: BOS detectado, esperar pullback (mejor entry)
        - CONSENSUS_DIRECT: Solo alineación, entrada directa

        setup_origin_price for timing classification
        """
        # 🔥 Para BOS, siempre usar BOS_PULLBACK path (mejor sizing)
        if edge_type == EdgeType.TREND and trend_path is None:
            trend_path = TrendEdgePath.BOS_PULLBACK

        # Calculate move at detection
        origin = setup_origin_price or bos_level
        if direction == 'LONG':
            move_at_detection = ((price - origin) / origin) * 100 if origin > 0 else 0.0
        else:
            move_at_detection = ((origin - price) / origin) * 100 if origin > 0 else 0.0

        self._current_bos = BOSEvent(
            timestamp=timestamp,
            price=price,
            direction=direction,
            htf_bias=htf_bias,
            bos_level=bos_level,
            atr_at_bos=atr,
            edge_type=edge_type,
            trend_path=trend_path,  # 🔥 NEW: Path específico
            # Timing fields
            move_at_detection_pct=abs(move_at_detection),
            setup_origin_price=setup_origin_price or bos_level
        )

        # Stats
        self._stats.total_bos_events += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1

        if edge_type == EdgeType.TREND:
            self._stats.trend_edges += 1

        # Transición fluida NO_EDGE → ARMED
        path_info = f" path={trend_path.value}" if trend_path else ""
        self._transition_to(
            EdgeState.EDGE_ARMED,
            f"BOS@{bos_level:.0f} dist={break_distance:.2f}ATR{path_info}",
            timestamp
        )

    # =========================================================================
    # DETECCIÓN DE PULLBACK
    # =========================================================================

    def check_for_pullback(
        self,
        current_price: float,
        atr: float,
        market_time: datetime
    ) -> bool:
        """
        Detecta pullback válido tras BOS.

        LONG: Precio retrocede hacia BOS level
        SHORT: Precio retrocede hacia BOS level

        Returns: True si se detectó pullback válido
        """
        if self._state != EdgeState.EDGE_ARMED:
            return False

        if self._current_bos is None:
            return False

        # Verificar timeout
        armed_duration = (market_time - self._current_bos.timestamp).total_seconds() / 60
        if armed_duration > self.EDGE_ARMED_TIMEOUT_MIN:
            self._expire_edge(f"timeout {armed_duration:.0f}min no pullback")
            return False

        bos = self._current_bos
        pullback_valid = False
        pullback_distance = 0.0

        if bos.direction == 'LONG':
            pullback_distance = (bos.price - current_price) / atr
            if self.PULLBACK_MIN_ATR <= pullback_distance <= self.PULLBACK_MAX_ATR:
                if current_price > bos.bos_level:
                    pullback_valid = True

        elif bos.direction == 'SHORT':
            pullback_distance = (current_price - bos.price) / atr
            if self.PULLBACK_MIN_ATR <= pullback_distance <= self.PULLBACK_MAX_ATR:
                if current_price < bos.bos_level:
                    pullback_valid = True

        if pullback_valid:
            self._activate_edge(market_time, pullback_distance)
            return True

        return False

    def _activate_edge(self, market_time: datetime, pullback_distance: float = 0.0):
        """Activar el edge tras detectar pullback o confirmación"""
        if self._current_bos:
            self._current_bos.pullback_detected_at = market_time

        # Transición fluida ARMED → ACTIVE
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"pullback {pullback_distance:.2f}ATR window:{self.EDGE_ACTIVE_TIMEOUT_MIN}min",
            market_time
        )

    # =========================================================================
    # EXPIRACIÓN DEL EDGE
    # =========================================================================

    def check_expiration(self, market_time: datetime, current_price: float = 0.0) -> bool:
        """
        Verifica si el edge debe expirar.

        Args:
            market_time: Timestamp actual del mercado
            current_price: Precio actual (usado por para smart cooldown)

        Returns: True si expiró
        """
        # =====================================================================
        # AUTO-RESET EXPIRED → NO_EDGE

        # EDGE_EXPIRED is a TRANSIENT state that must be cleared every tick.
        # Without this, EXPIRED blocks _detect_new_edges (which only runs
        # on NO_EDGE) and blocks can_evaluate_entry — creating a permanent
        # deadlock where no new edges are ever detected.

        # ROOT CAUSE: _expire_edge transitions to EXPIRED, but the only
        # code that resets to NO_EDGE lives inside arm_consensus_edge and
        # check_for_bos — which have their own guards (cooldowns, macro
        # checks) that can prevent reaching the reset line.
        # =====================================================================
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "auto-reset from expired", market_time)
            return False

        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            return False

        if self._current_bos is None:
            return False

        # EXPIRE DEAD EDGES AUTOMATICALLY
        # PROBLEMA: Edges con >35 min no expiraban, solo se bloqueaban con penalty -99.
        # El edge quedaba en EDGE_ACTIVE eternamente, nunca detectando un nuevo edge.
        # SOLUCIÓN: Expirar automáticamente edges DEAD (>15 min) antes de otros timeouts.

        # COOLDOWN TRAS DEAD_EDGE
        # PROBLEMA: Cuando edge expira por DEAD_EDGE, el sistema inmediatamente
        # crea un nuevo edge igual y el ciclo continúa (cycling):
        # EDGE_ACTIVE → DEAD_EDGE expired → EDGE_ACTIVE → DEAD_EDGE expired → ...
        # SOLUCIÓN: Activar cooldown de 5 min tras expiración por DEAD_EDGE.
        # El mercado debe mostrar NUEVO momentum, no detectar el mismo edge muerto.

        # REDUCE EDGE DEATH SPIRAL
        # Edge max lifetime — edges that can't execute in this window are stale.
        # Increased from 12 to 25min to give the evaluation pipeline time to align.
        # With DEAD_EDGE cooldown removed, there's no dead time after expiry.
        MAX_EDGE_AGE_DEAD = 25.0
        # Extend to 30min during transition_mode (post-BIAS-FLIP stabilization).
        transition_mode_active = getattr(self, '_transition_mode_from_engine', False)
        if transition_mode_active:
            MAX_EDGE_AGE_DEAD = 30.0
        edge_age_min = self.get_edge_age_minutes(market_time)
        if edge_age_min > MAX_EDGE_AGE_DEAD:
            # =====================================================================
            # Unificación + #
            # ANTES: (dentro de _expire_edge) aplicaba cooldown 5min para
            # CONSENSUS edges SIEMPRE. (aquí fuera) intentaba saltar cooldown
            # si la tendencia estaba confirmada, pero llegaba DESPUÉS → cooldown de
            # ya estaba escrito → nunca funcionaba.

            # AHORA: Pasar current_price a _expire_edge() para que la evaluación
            # de tendencia confirmada se haga ANTES de escribir cooldowns.
            # =====================================================================
            self._expire_edge(
                f"🔥 DEAD_EDGE: age {edge_age_min:.1f}min > {MAX_EDGE_AGE_DEAD}min",
                current_price=current_price
            )
            return True

        bos = self._current_bos
        edge_type = bos.edge_type

        # Timeouts diferentes según tipo de edge
        if edge_type == EdgeType.RANGE:
            armed_timeout = self.RANGE_EDGE_TIMEOUT_MIN
            active_timeout = self.RANGE_EDGE_TIMEOUT_MIN
            provisional_timeout = 5
        elif edge_type == EdgeType.BREAKOUT:
            armed_timeout = BreakoutEdgeCriteria.BREAKOUT_TIMEOUT_MIN
            active_timeout = self.EDGE_ACTIVE_TIMEOUT_MIN
            provisional_timeout = 5
        elif edge_type == EdgeType.INTRADAY:
            armed_timeout = 5  # 5 min
            active_timeout = 5
            provisional_timeout = 3
        else:  # TREND
            armed_timeout = self.EDGE_ARMED_TIMEOUT_MIN
            active_timeout = self.EDGE_ACTIVE_TIMEOUT_MIN
            provisional_timeout = 5

        # Timeout en ARMED
        if self._state == EdgeState.EDGE_ARMED:
            armed_duration = (market_time - bos.timestamp).total_seconds() / 60
            if armed_duration > armed_timeout:
                self._expire_edge(f"timeout:{armed_duration:.0f}min no confirm")
                return True

        # Timeout en PROVISIONAL (shorter window - starter position)
        elif self._state == EdgeState.EDGE_PROVISIONAL:
            provisional_duration = (market_time - bos.timestamp).total_seconds() / 60
            if provisional_duration > provisional_timeout:
                self._expire_edge(f"provisional timeout:{provisional_duration:.0f}min")
                return True

        # Timeout en ACTIVE
        elif self._state == EdgeState.EDGE_ACTIVE:
            if bos.pullback_detected_at:
                active_duration = (market_time - bos.pullback_detected_at).total_seconds() / 60
                if active_duration > active_timeout:
                    self._expire_edge(f"window closed:{active_duration:.0f}min")
                    return True

        # Edge quemado por intentos
        if bos.is_burned():
            self._expire_edge(f"burned:{bos.trades_attempted}att/{bos.trades_lost}loss")
            return True

        return False

    def _expire_edge(self, reason: str, set_context_cooldown: bool = True, current_price: float = 0.0):
        """
        Expirar el edge actual.

        Cooldown por contexto si hay pérdidas:
        - BREAKOUT falla → TREND cooldown 15min
        - TREND falla → BREAKOUT cooldown 15min
        - RANGE no afecta otros (contexto independiente)

        Si es DISPLACEMENT, SIEMPRE activar post-displacement cooldown
        Unifica — cooldown condicional
        """
        edge_type_str = "UNKNOWN"
        direction = "-"

        if self._current_bos:
            self._current_bos.expired_at = datetime.now(UTC)
            self._bos_history.append(self._current_bos)
            edge_type_str = self._current_bos.edge_type.value
            direction = self._current_bos.direction

            # =====================================================================
            # SMART COOLDOWN — evaluar tendencia confirmada ANTES
            # de aplicar cualquier cooldown de dirección.

            # Si el precio confirma la dirección del edge muerto (>0.5 ATR),
            # NO aplicar cooldown ni de (CONSENSUS) ni de DEAD_EDGE.
            # =====================================================================
            trend_confirmed = False
            is_dead_edge = "DEAD_EDGE" in reason
            edge_origin = self._current_bos.price

            if is_dead_edge and direction != "-" and edge_origin > 0 and current_price > 0:
                price_moved = current_price - edge_origin
                atr_ref = self._current_bos.atr_at_bos if self._current_bos.atr_at_bos else 100
                if direction == 'LONG' and price_moved > atr_ref * 0.5:
                    trend_confirmed = True
                    logger.info(
                        f"DEAD_EDGE sin cooldown | {direction} confirmado | "
                        f"Precio subió ${price_moved:.0f} (>{atr_ref*0.5:.0f}) desde edge origin"
                    )
                elif direction == 'SHORT' and price_moved < -atr_ref * 0.5:
                    trend_confirmed = True
                    logger.info(
                        f"DEAD_EDGE sin cooldown | {direction} confirmado | "
                        f"Precio bajó ${abs(price_moved):.0f} (>{atr_ref*0.5:.0f}) desde edge origin"
                    )

            # =====================================================================
            # POST-DISPLACEMENT COOLDOWN AUTOMÁTICO
            # =====================================================================
            if self._current_bos.edge_type == EdgeType.DISPLACEMENT:
                logger.warning(
                    f"DISPLACEMENT expired ({reason}) → "
                    f"Activating 10min POST-DISPLACEMENT COOLDOWN for {direction}"
                )
                self.set_post_displacement_cooldown(
                    direction=direction,
                    market_time=datetime.now(UTC),
                    cooldown_minutes=5  # (was 10 — too long for intraday)
                )

            # =====================================================================
            # CONSENSUS EXPIRY DIRECTION COOLDOWN

            # Solo aplicar cooldown si la tendencia NO está confirmada.
            # Si trend_confirmed=True, saltar TODOS los cooldowns de dirección
            # para permitir re-armar inmediatamente en la misma dirección.
            # =====================================================================
            # DEAD_EDGE direction cooldown REMOVED — bot re-arms immediately.
            # Trend confirmed or not, the mode_handlers and direction_orchestrator
            # already prevent incoherent edges. No artificial wait needed.
            if trend_confirmed:
                logger.info(f"Edge expired ({reason}) | {direction} trend confirmed | Re-arm allowed immediately")
            else:
                logger.info(f"Edge expired ({reason}) | {direction} | Re-arm allowed (no cooldown)")

            # COOLDOWN POR CONTEXTO: bloquear edges relacionados
            if set_context_cooldown and self._current_bos.trades_lost > 0:
                edge_type = self._current_bos.edge_type
                cooldown_until = datetime.now(UTC) + timedelta(minutes=self.CONTEXT_COOLDOWN_MINUTES)

                if edge_type == EdgeType.BREAKOUT:
                    self._context_cooldown['TREND'] = cooldown_until
                elif edge_type == EdgeType.TREND:
                    self._context_cooldown['BREAKOUT'] = cooldown_until
                # RANGE no establece cooldown (contexto independiente)

            # Keep bounded history
            if len(self._bos_history) > 50:
                self._bos_history = self._bos_history[-50:]

        # Transición fluida a EXPIRED
        self._transition_to(EdgeState.EDGE_EXPIRED, reason)

    def is_context_in_cooldown(self, edge_type: EdgeType, market_time: datetime) -> bool:
        """
        Check if edge type is in cooldown due to related edge failure.
        RANGE is always available (independent context).
        """
        cooldown_until = self._context_cooldown.get(edge_type.value)
        if cooldown_until and market_time < cooldown_until:
            return True
        return False

    # =========================================================================
    # INVALIDACIÓN DEL EDGE
    # =========================================================================

    def check_invalidation(
        self,
        current_price: float,
        atr: float,
        swing_range: float = 0.0  # swing_high - swing_low for adaptive buffer
    ) -> bool:
        """
        Verifica si el edge se ha invalidado por precio.

        TREND: Invalidado si precio cruza BOS level
        RANGE: INVALIDACIÓN BINARIA - si rompe el sweep level, tesis rota
        BREAKOUT: Invalidado si precio vuelve al squeeze
        DISPLACEMENT: Invalidado si precio vuelve >60% del displacement al origen
        PROVISIONAL: Usa mismas reglas que su edge_type subyacente

        swing_range permite buffer adaptativo al contexto actual

        Returns: True si se invalidó
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            return False

        if self._current_bos is None:
            return False

        bos = self._current_bos

        # =====================================================================
        # RANGE INVALIDATION CON MICRO-BUFFER

        # ANTES: Invalidación binaria SIN buffer — un wobble de $5 mataba el edge.
        # Run 20260217 (08:38-09:18): "binary: price 67890 < sweep 67918" (diff=$28)
        # → 5+ edges armados y expirados en 40 min, sistema nunca entró.

        # AHORA: Buffer de 0.15×ATR (~$30 para ATR=$200). El precio debe romper
        # el sweep level por más de $30 para invalidar. Wobbles no matan el edge.

        # Justificación: si el sweep level es soporte (LONG) o resistencia (SHORT),
        # una penetración de <0.30×ATR es ruido, no ruptura estructural.
        # Was 0.15×ATR (~$12-$19). Edges died in <30 seconds
        # because any $15 wobble exceeded the buffer. Increased to 0.30×ATR (~$30).
        # =====================================================================
        if bos.edge_type == EdgeType.RANGE:
            range_buffer = atr * 0.30
            if bos.direction == 'LONG' and current_price < (bos.bos_level - range_buffer):
                self._expire_edge(
                    f"binary: price {current_price:.0f} < sweep {bos.bos_level:.0f} "
                    f"(buffer ${range_buffer:.0f})"
                )
                return True
            elif bos.direction == 'SHORT' and current_price > (bos.bos_level + range_buffer):
                self._expire_edge(
                    f"binary: price {current_price:.0f} > sweep {bos.bos_level:.0f} "
                    f"(buffer ${range_buffer:.0f})"
                )
                return True
            return False

        # =====================================================================
        # 🌊 DISPLACEMENT: INVALIDACIÓN MUY DIFÍCIL (Jan 30, 2026)
        # En displacement real, el mercado no retrocede tanto
        # Solo invalidar si vuelve al ORIGEN del movimiento
        # =====================================================================
        if bos.edge_type == EdgeType.DISPLACEMENT:
            if bos.direction == 'LONG':
                # En LONG, solo invalidar si cae por debajo del origen
                if current_price < bos.bos_level:
                    self._expire_edge(f"displacement invalid: price {current_price:.0f} < origin {bos.bos_level:.0f}")
                    return True
            else:
                # En SHORT, solo invalidar si sube por encima del origen
                if current_price > bos.bos_level:
                    self._expire_edge(f"displacement invalid: price {current_price:.0f} > origin {bos.bos_level:.0f}")
                    return True
            # DISPLACEMENT no se invalida fácilmente - NO aplicar buffer ATR
            return False

        # =====================================================================
        # BUFFER ADAPTATIVO CON SWING

        # OLD: invalidation_buffer = atr * 0.6 (estático)
        # NEW: invalidation_buffer = max(atr * 0.6, swing_range * 0.3)

        # WHY ADAPTIVE:
        # - En swings pequeños (mercado tranquilo): usa 0.6× ATR
        # - En swings grandes (volatilidad): usa 30% del swing range
        # - Esto adapta automáticamente al contexto del mercado

        # EXAMPLE:
        # - ATR = $500, swing = $1200 → max(300, 360) = $360 buffer
        # - ATR = $500, swing = $600 → max(300, 180) = $300 buffer
        # - ATR = $500, swing = $2000 → max(300, 600) = $600 buffer

        # CAP swing_buffer to prevent absurd SL
        # PROBLEMA: swing_range de $5,000+ → swing_buffer de $1,500+
        # Esto hace que 1R = $1,500, y cualquier movimiento pequeño sea -0.3R
        # Trade real: SL a $1,197 → movimiento de $500 = -0.40R → TIME_INVALIDATION
        # SOLUCIÓN: Cap swing_buffer a max 2× ATR o $600, lo que sea menor
        # =====================================================================
        atr_buffer = atr * 0.6
        swing_buffer_raw = swing_range * 0.3 if swing_range > 0 else 0.0
        # Cap swing_buffer to prevent absurd SL distances
        MAX_SWING_BUFFER_ATR_MULT = 2.0  # Max 2× ATR
        MAX_SWING_BUFFER_ABS = 600.0     # Max $600 absolute
        swing_buffer_cap = min(atr * MAX_SWING_BUFFER_ATR_MULT, MAX_SWING_BUFFER_ABS)
        swing_buffer = min(swing_buffer_raw, swing_buffer_cap)

        if swing_buffer_raw > swing_buffer_cap:
            logger.debug(
                f"Capped swing_buffer ${swing_buffer_raw:.0f} → ${swing_buffer:.0f} "
                f"(max: {MAX_SWING_BUFFER_ATR_MULT}× ATR or ${MAX_SWING_BUFFER_ABS})"
            )

        invalidation_buffer = max(atr_buffer, swing_buffer)

        # Log if using swing-based buffer (significant difference) - THROTTLED
        # Solo loggear la primera vez para reducir spam
        if swing_buffer > atr_buffer * 1.2 and not getattr(self, '_swing_buffer_logged', False):
            _edge_log(
                self._state.value, bos.edge_type.value, bos.direction,
                f"Using swing buffer ${swing_buffer:.0f} > ATR buffer ${atr_buffer:.0f}"
            )
            self._swing_buffer_logged = True

        if bos.direction == 'LONG':
            if current_price < (bos.bos_level - invalidation_buffer):
                self._expire_edge(f"invalidated: price {current_price:.0f} < level {bos.bos_level:.0f} (buffer ${invalidation_buffer:.0f})")
                return True
        elif bos.direction == 'SHORT':
            if current_price > (bos.bos_level + invalidation_buffer):
                self._expire_edge(f"invalidated: price {current_price:.0f} > level {bos.bos_level:.0f} (buffer ${invalidation_buffer:.0f})")
                return True

        return False

    # =========================================================================
    # REGISTRO DE TRADES
    # =========================================================================

    def record_entry(self, market_time: datetime):
        """Registra que se ha intentado una entrada"""
        if self._current_bos:
            if self._current_bos.first_entry_at is None:
                self._current_bos.first_entry_at = market_time

    def record_trade_result(self, won: bool, r_captured: float):
        """Registra resultado de trade y actualiza stats."""
        if self._current_bos is None:
            return

        bos = self._current_bos
        bos.record_trade(won, r_captured)

        # =====================================================================
        # RECORD LOSS ZONE MEMORY
        # After a loss, remember this price zone + direction so we don't
        # re-enter the same losing setup within 30 minutes.
        # =====================================================================
        if not won and bos.price > 0 and bos.atr_at_bos > 0:
            # Clean expired entries first
            now = datetime.now(UTC)
            self._loss_zone_memory = [
                z for z in self._loss_zone_memory if z['expires'] > now
            ]
            self._loss_zone_memory.append({
                'direction': bos.direction,
                'price': bos.price,
                'atr': bos.atr_at_bos,
                'expires': now + timedelta(minutes=self.LOSS_ZONE_MEMORY_MINUTES)
            })
            logger.warning(
                f"LOSS ZONE RECORDED | {bos.direction} @ ${bos.price:.0f} ± "
                f"${bos.atr_at_bos * self.LOSS_ZONE_RADIUS_ATR:.0f} | "
                f"Blocked for {self.LOSS_ZONE_MEMORY_MINUTES}min | "
                f"Active zones: {len(self._loss_zone_memory)}"
            )

        # Stats globales
        self._stats.total_trades += 1
        if won:
            self._stats.total_wins += 1
        else:
            self._stats.total_losses += 1
        self._stats.total_r_captured += r_captured

        # Stats por dirección
        if bos.direction == 'LONG':
            self._stats.long_trades += 1
            self._stats.long_r += r_captured
        else:
            self._stats.short_trades += 1
            self._stats.short_r += r_captured

        # Log con formato unificado
        result = "WIN" if won else "LOSS"
        _edge_log(
            self._state.value,
            bos.edge_type.value,
            bos.direction,
            f"{result} {r_captured:+.2f}R | trades:{bos.trades_attempted}/{self.MAX_TRADES_PER_EDGE} exp:{self._stats.get_expectancy_per_bos():.3f}R"
        )

        # Verificar si edge está quemado
        if bos.is_burned():
            self._expire_edge(f"max trades reached ({bos.trades_attempted})")

    # =========================================================================
    # 📏 UNIFIED EDGE SIZING (Feb 04 Architecture)
    # =========================================================================

    def get_edge_size_multiplier(
        self,
        percentile: float = 50.0,
        bb_percentile: float = 50.0,
        session_quality: str = 'moderate'
    ) -> tuple[float, float, str]:
        """
        Retorna (min_mult, max_mult, edge_info) basado en EdgeType actual.

        SIZING BY EDGE TYPE (New Architecture Feb 04):
        - DISPLACEMENT: 0.30x - 0.60x (macro reversals, lower conviction till confirmed)
        - BREAKOUT:     0.50x - 0.80x (structure break with confirmation)
        - RANGE:        0.50x - 0.80x (mean reversion, reliable)
        - TREND/BOS:    0.60x - 0.80x (confirmed pullback entry)
        - TREND/CONSENSUS: 0.40x - 0.60x (no BOS, just trend alignment)

        Returns:
            (min_mult, max_mult, edge_info): Size range and description
        """
        if self._current_bos is None:
            return (0.50, 0.70, "NO_EDGE")  # 0.40-0.60 → 0.50-0.70

        edge_type = self._current_bos.edge_type
        trend_path = getattr(self._current_bos, 'trend_path', None)

        # Determine sizing by EdgeType
        if edge_type == EdgeType.DISPLACEMENT:
            # Macro reversal - start conservative
            base_min, base_max = 0.30, 0.60
            edge_info = "DISPLACEMENT"

        elif edge_type == EdgeType.BREAKOUT:
            # Structure break - higher conviction
            base_min, base_max = 0.50, 0.80
            edge_info = "BREAKOUT"

        elif edge_type == EdgeType.RANGE:
            # Mean reversion - reliable
            base_min, base_max = 0.50, 0.80
            edge_info = "RANGE"

        elif edge_type == EdgeType.TREND:
            if trend_path == TrendEdgePath.BOS_PULLBACK:
                # Confirmed BOS entry
                base_min, base_max = 0.70, 0.90
                edge_info = "TREND_BOS"  # 0.60-0.80 → 0.70-0.90
            else:  # CONSENSUS_DIRECT or None
                # No BOS, just alignment - Use more capital
                base_min, base_max = 0.60, 0.80
                edge_info = "TREND_CONSENSUS"  # 0.40-0.60 → 0.60-0.80

        else:
            # Unknown edge type - conservative
            base_min, base_max = 0.40, 0.60
            edge_info = f"UNKNOWN_{edge_type.value}"

        # Apply session quality boost for high conviction scenarios
        from src.config.trading_config import SessionQuality
        session_mult = SessionQuality.get_quality_size_mult(session_quality)

        # Boost toward max if good session
        if session_mult >= 1.0:
            # Good session: can use up to max
            final_min = base_min
            final_max = base_max
        else:
            # Poor session: reduce max
            final_min = base_min
            final_max = base_min + (base_max - base_min) * 0.7  # 30% reduction on max

        return (final_min, final_max, edge_info)

    # =========================================================================
    # 🌊 STATUS AND UTILITIES
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Estado actual - compact format"""
        status = {
            'state': self._state.value,
            'can_trade': self.can_trade(),
        }

        if self._current_bos:
            bos = self._current_bos
            status['edge'] = {
                'type': bos.edge_type.value,
                'dir': bos.direction,
                'level': round(bos.bos_level, 0),
                'trades': f"{bos.trades_attempted}/{self.MAX_TRADES_PER_EDGE}",
                'burned': bos.is_burned()
            }

        status['exp'] = round(self._stats.get_expectancy_per_bos(), 3)

        return status

    def reset(self):
        """Reset para nuevo día o manual"""
        self._current_bos = None
        self._transition_history = []
        self._transition_to(EdgeState.NO_EDGE, "manual reset")

    def force_expire(self, reason: str = "manual"):
        """Forzar expiración del edge actual"""
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            self._expire_edge(reason)


# Singleton para acceso global
_edge_manager: Optional[EdgeStateManager] = None


def get_edge_manager() -> EdgeStateManager:
    """Obtiene el EdgeStateManager singleton"""
    global _edge_manager
    if _edge_manager is None:
        _edge_manager = EdgeStateManager()
    return _edge_manager


def reset_edge_manager():
    """Reset del singleton"""
    global _edge_manager
    _edge_manager = None