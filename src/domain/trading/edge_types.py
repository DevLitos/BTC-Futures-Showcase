"""
Edge types, enums, and data classes for the edge state management system.

Contains: EdgePriority, TrendEdgePath, EdgeType, EdgeState, RangeEdgeCriteria,
BreakoutEdgeCriteria, DisplacementEdgeCriteria, BOSEvent, EdgeStats, EdgeTransition,
and related helper functions/constants.

Extracted from edge_state_manager.py for maintainability.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from enum import Enum
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


_STATE_SYMBOLS = {
    "NO_EDGE": "○",        # Empty - no opportunity
    "EDGE_ARMED": "◐",     # Half - setup detected, waiting
    "EDGE_PROVISIONAL": "◑",  # Partial - valid but incomplete
    "EDGE_ACTIVE": "●",    # Full - confirmed, trade window open
    "EDGE_EXPIRED": "◌",   # Expired - consumed
}

_TYPE_SYMBOLS = {
    "TREND": "📈",
    "RANGE": "↔️",
    "BREAKOUT": "💥",
    "INTRADAY": "⚡",
    "DISPLACEMENT": "🌊",  # 🔥 NEW: Displacement edge symbol
}

_MODE_SYMBOLS = {
    "INSTITUTIONAL": "🏛️",
    "INTRADAY": "⚡",
}


def _edge_log(state: str, edge_type: str, direction: str, msg: str, level: str = "info"):
    """
    Unified edge logging - 1 line, maximum signal, minimum noise.
    
    Format: [STATE_SYMBOL] TYPE_SYMBOL DIRECTION | compact_message
    Example: [●] 💥 LONG | BB breakout confirmed +0.8ATR
    """
    state_sym = _STATE_SYMBOLS.get(state, "?")
    type_sym = _TYPE_SYMBOLS.get(edge_type, "?")
    log_line = f"[{state_sym}] {type_sym} {direction} | {msg}"
    
    log_fn = getattr(logger, level, logger.info)
    log_fn(log_line)


# =============================================================================
# =============================================================================
# 🎯 EDGE PRIORITY SYSTEM (Feb 05, 2026)
# Jerarquía clara de edges - el de mayor prioridad SIEMPRE gana
# =============================================================================
class EdgePriority:
    """
    Prioridades de edges - mayor número = mayor prioridad.
    
    Un edge de mayor prioridad puede OVERRIDE uno de menor prioridad.
    Esto soluciona el problema de BREAKOUT bloqueado por CONSENSUS.
    """
    DISPLACEMENT = 100   # Sovereign authority - bypasses everything
    BREAKOUT = 80        # Pre-expansion - alta convicción
    RANGE = 60           # Mean reversion en extremos
    TREND_BOS = 40       # Break of Structure - espera pullback
    TREND_CONSENSUS = 20 # Alineación directa - menor convicción
    
    @classmethod
    def get_priority(cls, edge_type: 'EdgeType', trend_path: 'TrendEdgePath | None' = None) -> int:
        """Obtiene la prioridad de un edge type"""
        if edge_type == EdgeType.DISPLACEMENT:
            return cls.DISPLACEMENT
        elif edge_type == EdgeType.BREAKOUT:
            return cls.BREAKOUT
        elif edge_type == EdgeType.RANGE:
            return cls.RANGE
        elif edge_type == EdgeType.TREND:
            if trend_path == TrendEdgePath.BOS_PULLBACK:
                return cls.TREND_BOS
            else:
                return cls.TREND_CONSENSUS
        return 0


# =============================================================================
# 🎯 TREND EDGE PATH (Feb 05, 2026)
# Dos caminos para TREND edge - BOS es mejor que CONSENSUS
# =============================================================================
class TrendEdgePath(Enum):
    """
    Path de entrada para TREND edge.
    
    BOS_PULLBACK: BOS detectado → espera pullback → mejor entry, mayor size
    CONSENSUS_DIRECT: Solo alineación → entrada directa → peor entry, menor size
    """
    BOS_PULLBACK = "BOS_PULLBACK"          # Size 0.60-0.80x
    CONSENSUS_DIRECT = "CONSENSUS_DIRECT"  # Size 0.40-0.60x


# =============================================================================
# 🎯 EDGE TYPE: 4 tipos principales (simplificado Feb 05, 2026)
# =============================================================================
class EdgeType(Enum):
    """
    Tipo de edge - determina reglas de activación, gestión y sizing.
    
    Jerarquía de prioridad: DISPLACEMENT > BREAKOUT > RANGE > TREND
    
    Cada tipo tiene su propio sizing profile:
    - DISPLACEMENT: 0.30-0.60x (adaptativo por zona de entrada)
    - BREAKOUT: 0.50-0.80x (alta convicción, pre-expansion)
    - RANGE: 0.50-0.80x (mean reversion en extremos)
    - TREND: 0.40-0.80x (depende del path: BOS vs CONSENSUS)
    """
    DISPLACEMENT = "DISPLACEMENT"  # Movimiento fuerte ≥1.5x ATR - PRIORIDAD 1
    BREAKOUT = "BREAKOUT"          # Compresión BB + ruptura - PRIORIDAD 2
    RANGE = "RANGE"                # Extremo 85/15% + sweep - PRIORIDAD 3
    TREND = "TREND"                # BOS/Consensus alignment - PRIORIDAD 4 (fallback)
    
    # 🚨 DEPRECATED - Mantener por compatibilidad, nunca usar para nuevo código
    INTRADAY = "INTRADAY"  # ⚠️ DEPRECATED: Absorbed into TREND with CONSENSUS_DIRECT path


class EdgeState(Enum):
    """Estados del edge - el sistema VIVE en uno de estos"""
    NO_EDGE = "NO_EDGE"                 # Sin oportunidad, no evaluar entradas
    EDGE_ARMED = "EDGE_ARMED"           # BOS detectado (TREND) o setup potencial (RANGE)
    EDGE_PROVISIONAL = "EDGE_PROVISIONAL"  # 🔥 NEW: Señal válida pero incompleta (starter position)
    EDGE_ACTIVE = "EDGE_ACTIVE"         # Condiciones confirmadas, ventana abierta
    EDGE_EXPIRED = "EDGE_EXPIRED"       # Oportunidad consumida


# =============================================================================
# 🎯 RANGE EDGE CRITERIA (Jan 27, 2026)
# Criterios para activar edge en mercados de rango
# =============================================================================
@dataclass
class RangeEdgeCriteria:
    """
    Criterios para RANGE edge.
    
    🔥 AJUSTE (Jan 28, 2026): Criterios BALANCEADOS
    
    Un edge de rango requiere:
    1. Estar en EXTREMO del rango (≥85% o ≤15% - ampliado de 90/10)
    2. PROBING HABILITADO con size mínimo cuando hay alineación
    
    BALANCE:
    - Extremos ampliados de 90/10 → 85/15 (más oportunidades)
    - PROBING permitido pero con size muy reducido (25%)
    - PROBING requiere alineación de bias (no ciego)
    """
    # Extremos AMPLIADOS para más oportunidades
    EXTREME_HIGH_PCT = 85.0   # ≥85% = extremo alto (SHORT edge) - antes 90
    EXTREME_LOW_PCT = 15.0    # ≤15% = extremo bajo (LONG edge) - antes 10
    
    # 🔥 PROBING HABILITADO con restricciones
    PROBING_ENABLED = True    # Permitir probing cerca de VWAP/VPOC
    PROBING_VPOC_ATR_MAX = 0.8  # Cerca de VPOC (0.8 ATR)
    PROBING_VWAP_ATR_MAX = 1.0  # Cerca de VWAP (1.0 ATR)
    PROBING_MIN_SCORE = 4       # Score mínimo para probing
    PROBING_SIZE_MULT = 0.25    # Size reducido al 25%
    
    # Probing requiere alineación (no ciego)
    PROBING_REQUIRE_BIAS_ALIGN = True  # Debe coincidir con HTF bias
    
    # Size máximo en RANGE (conservador)
    RANGE_SIZE_MAX = 0.30     # 30% size en rango (subido de 25%)
    
    # Timeouts
    # EDGE TTL RADICAL
    # BTC intradía = edges mueren rápido. 20 min = eternidad.
    # Si no confirma en 10 min, el edge murió.
    RANGE_EDGE_TIMEOUT_MIN = 10  # 🔥 Was 20 → 10 min (TTL radical)


@dataclass
class BreakoutEdgeCriteria:
    """
    Criterios para BREAKOUT edge (compresión).
    
    Un edge de breakout requiere:
    1. BB squeeze (width < percentile 10)
    2. Volume decline confirmado
    3. Ruptura del squeeze con volumen
    
    Relajar criterios - anticipation acierta más de lo esperado
    """
    BB_WIDTH_PERCENTILE_MAX = 25  # 🔥 Subido de 10 a 25 - percentil 25 ya indica compresión relativa
    VOLUME_DECLINE_RATIO = 0.70   # 🔥 Subido de 0.50 a 0.70 - volumen ≤70% es suficiente
    BREAKOUT_CONFIRMATION_CANDLES = 2  # 2 velas para confirmar ruptura
    # EDGE TTL RADICAL
    # Breakout que no rompe en 10 min = squeeze fallido = NO TRADE
    BREAKOUT_TIMEOUT_MIN = 10  # 🔥 Was 30 → 10 min (TTL radical)
    
    # 🔥 NEW (Jan 28, 2026): Criterios R mínimo y invalidación
    MIN_TARGET_R = 2.0   # BREAKOUT debe apuntar a mínimo 2.0R
    IDEAL_TARGET_R = 3.0 # Ideal 3.0R para pagar winrate bajo
    
    # Invalidación lógica (distinta del SL monetario)
    INVALIDATION_CLOSE_INSIDE_BB = True  # Close de vuelta dentro de BB invalida
    INVALIDATION_LOSS_OF_BREAKOUT_LEVEL_ATR = 0.3  # Pérdida de nivel + 0.3 ATR

# =============================================================================
# � DISPLACEMENT EDGE CRITERIA (Jan 30, 2026)
# Edge de movimiento fuerte - EL MERCADO YA DECIDIÓ

# PROBLEMA RESUELTO:
# - EXPANSION_MODE + DIRECTION_LOCK funcionaban pero NO generaban EDGE
# - Sin EDGE → sistema no puede tradear aunque detecte displacement
# - RESULTADO: 0 trades en caída de 2.3%

# SOLUCIÓN:
# - DISPLACEMENT_EDGE se activa AUTOMÁTICAMENTE con EXPANSION_MODE
# - No necesita BOS, sweep, ni compresión - el displacement ES el edge
# - El momentum institucional confirmó la dirección
# =============================================================================
@dataclass
class DisplacementEdgeCriteria:
    """
    Criterios para DISPLACEMENT edge (movimiento fuerte direccional).
    
    🔥 FILOSOFÍA: El displacement ES el edge
    - No esperar BOS (ya pasó)
    - No esperar sweep/rejection (no aplica)
    - No esperar compresión (opuesto - es expansión)
    
    Un edge de displacement se activa cuando:
    1. STRONG_DISPLACEMENT detectado (≥1.5x ATR_5m)
    2. EXPANSION_MODE activado
    3. Continuidad del movimiento (no reversal inmediato)
    
    CONFIRMACIÓN para pasar de ARMED → ACTIVE:
    - Pullback pequeño (0.2-0.5x displacement) SIN romper estructura
    - O continuación con follow-through
    - 🔥 O ENTRADA DIRECTA en las primeras 3 velas (Jan 30)
    """
    # Thresholds de activación (heredados de trading_app)
    MIN_DISPLACEMENT_ATR_MULT = 1.2   # Mínimo para detectar displacement
    STRONG_DISPLACEMENT_MULT = 1.5    # Para activar EXPANSION_MODE
    
    # =========================================================================
    # 🔥 ENTRY WINDOW (Jan 30, 2026) - CRÍTICO PARA CRASHES

    # En displacement violento NO hay pullback limpio.
    # El sistema DEBE entrar temprano o no entrar.
    # "No perseguir" = regla de oro institucional
    # =========================================================================
    MAX_ENTRY_WINDOW_BARS = 5         # Máximo 5 velas 1m para entrada (ZONA 2 límite)
    MAX_ENTRY_WINDOW_MIN = 10         # Máximo 10 minutos para entrada
    IMMEDIATE_ENTRY_SIZE = 0.30       # 30% size para entrada inmediata
    LATE_ENTRY_PENALTY = 0.70         # Penalización 30% si entra después de ZONA 1
    
    # 🔥 CONFIRMACIÓN: Pasar de ARMED → ACTIVE
    # Pullback es OPORTUNIDAD, no invalidación
    PULLBACK_MIN_PCT = 0.15           # Mínimo 15% del displacement para pullback válido
    PULLBACK_MAX_PCT = 0.45           # Máximo 45% (antes 50% - más estricto)
    CONTINUATION_THRESHOLD_PCT = 0.10 # Si continúa 10%+ más = ACTIVE sin pullback
    
    # =========================================================================
    # 🔥 ZONAS DE ENTRADA (Jan 30, 2026):
    # ZONA 1 - EARLY IMPULSE (bars 0-1): Entry DIRECTO FAST PATH
    # ZONA 2 - PULLBACK (bars 2-5): Entry en pullback 15-45%, size 0.40-0.60
    # ZONA 3 - NO TRADE (bars >5): CHASE BLOCK - NO PERSEGUIR
    # =========================================================================
    FORCE_DIRECT_ENTRY_BARS = 1       # 🔥 CHANGED: Solo bar 0-1 = FAST PATH (era 2)
    DIRECT_ENTRY_SIZE = 0.30          # 30% size para entrada directa
    
    # ZONA 1 (bars 0-1) - Early Impulse Entry (FAST PATH)
    # 🔥 CRITICAL: Si no entras en 0-1 velas, no es trade de displacement
    EARLY_IMPULSE_MAX_BARS = 1        # 🔥 CHANGED: Máximo 1 vela para fast path (era 2)
    EARLY_IMPULSE_SIZE_MIN = 0.25     # Size mínimo 25%
    EARLY_IMPULSE_SIZE_MAX = 0.40     # Size máximo 40%
    
    # ZONA 2 (bars 2-5) - Pullback Entry (si perdiste FAST PATH)
    PULLBACK_ENTRY_MIN_BARS = 2       # Mínimo 2 velas para considerar pullback
    PULLBACK_ENTRY_MAX_BARS = 10      # 🔥 Was 5 → extended to allow more pullback time
    PULLBACK_ENTRY_SIZE_MIN = 0.40    # Size mínimo 40% en pullback
    PULLBACK_ENTRY_SIZE_MAX = 0.60    # Size máximo 60% en pullback
    
    # ZONA 3 (bars >10) - Chase Block
    # 🔥 REFACTOR (Feb 05, 2026): Extended chase window
    # In BTC, trends can persist for 20-30 minutes before pullback.
    # Killing edge after 5 bars was destroying valid setups.
    CHASE_BLOCK_AFTER_BARS = 15       # Was 5 → extended to 15 bars (~15 min on 1m)
    CHASE_BLOCK_EXTENSION_MULT = 2.0  # Was 1.5 → allow more extension before chase block
    
    # Timeouts
    # EDGE TTL RADICAL
    # CRÍTICO: El análisis brutal demostró que 90 min = LOCURA para BTC intradía.
    # Si no capturas displacement en 20 min, el movimiento ya se consumió.
    # "Confirmation-based" = LATE. Reducir TTL fuerza timing temprano.
    EDGE_TIMEOUT_MIN = 20             # 🔥 Was 90 → 20 min (TTL radical)
    CONFIRMATION_TIMEOUT_MIN = 10     # 🔥 Was 30 → 10 min (TTL radical)
    
    # KILL LATE EDGE
    # Displacement que no entró en 10 min = CHASE = NO TRADE.
    # El impulso ya se consumió, entrar ahora = vender al perdedor.
    KILL_EDGE_AFTER_BARS = 10         # 🔥 Was 20 → 10 bars
    KILL_EDGE_AFTER_MIN = 10          # 🔥 Was 30 → 10 min
    
    # Size multiplier (agresivo en momentum)
    SIZE_MULTIPLIER_BASE = 0.8        # 80% size base (conservador inicio)
    SIZE_MULTIPLIER_CONFIRMED = 1.2   # 120% si confirma con follow-through
    
    # RR expectations
    MIN_TARGET_R = 1.5                # Mínimo 1.5R
    IDEAL_TARGET_R = 2.5              # Ideal 2.5R (tendencias)
    
    # Invalidación (muy difícil en displacement)
    INVALIDATION_OPPOSITE_DISPLACEMENT_MULT = 0.6  # 60% del displacement en contra


@dataclass
class BOSEvent:
    """Un evento de Break of Structure (TREND edge)"""
    timestamp: datetime
    price: float
    direction: str  # 'LONG' or 'SHORT'
    htf_bias: str | None  # HTF alignment (None if not determined)
    bos_level: float  # Nivel que se rompió
    atr_at_bos: float
    edge_type: EdgeType = EdgeType.TREND  # Tipo de edge
    
    # 🔥 NEW (Feb 05, 2026): Path para TREND edge
    trend_path: Optional['TrendEdgePath'] = None  # BOS_PULLBACK o CONSENSUS_DIRECT
    
    # Tracking
    trades_attempted: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    total_r_captured: float = 0.0
    
    # Timing
    pullback_detected_at: Optional[datetime] = None
    first_entry_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    
    # 🔥 NEW: Datos específicos de RANGE edge
    percentile_60d: Optional[float] = None  # Posición en rango
    vpoc_distance_atr: Optional[float] = None
    vwap_distance_atr: Optional[float] = None
    probing_score: Optional[int] = None
    
    # 🔥 NEW: Datos específicos de BREAKOUT edge
    bb_width_percentile: Optional[float] = None
    volume_ratio: Optional[float] = None
    
    # 🔥 NEW (Jan 28, 2026): Datos para invalidación y tracking
    breakout_confirmation_price: Optional[float] = None  # Precio al confirmar breakout
    breakout_bb_upper: Optional[float] = None  # BB upper al confirmar
    breakout_bb_lower: Optional[float] = None  # BB lower al confirmar
    context_cooldown_until: Optional[datetime] = None  # Cooldown por contexto
    
    # =========================================================================
    # 🌊 NEW (Jan 30, 2026): Datos específicos de DISPLACEMENT edge
    # =========================================================================
    origin_price: Optional[float] = None              # Precio ORIGEN del displacement (el high/low desde donde cayó/subió)
    initial_displacement_amount: Optional[float] = None  # Cantidad inicial del displacement
    max_favorable_excursion: float = 0.0              # MFE desde entrada (tracking)
    bars_since_displacement: int = 0                  # Velas transcurridas desde displacement
    entry_window_open: bool = True                    # Si aún estamos en ventana de entrada
    force_direct_entry: bool = False                  # Si se debe forzar entrada directa (primeras barras)
    
    # =========================================================================
    # 🔥 FIX Feb 06: Signal Timing Classification
    # =========================================================================
    move_at_detection_pct: float = 0.0                # % price moved before we detected the signal
    setup_origin_price: Optional[float] = None        # Price when the setup pattern started forming
    
    def get_priority(self) -> int:
        """Obtiene la prioridad de este edge usando EdgePriority"""
        return EdgePriority.get_priority(self.edge_type, self.trend_path)
    
    def is_burned(self) -> bool:
        """Edge quemado si ya se intentaron 2 trades o 2 pérdidas"""
        return self.trades_attempted >= 2 or self.trades_lost >= 2
    
    def can_trade(self) -> bool:
        """Puede operar si no está quemado"""
        return not self.is_burned()
    
    def record_trade(self, won: bool, r_captured: float):
        """Registra resultado de trade en este edge"""
        self.trades_attempted += 1
        if won:
            self.trades_won += 1
        else:
            self.trades_lost += 1
        self.total_r_captured += r_captured


@dataclass
class EdgeStats:
    """Estadísticas por edge para calcular expectancy real"""
    total_bos_events: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_r_captured: float = 0.0
    
    # Por dirección
    long_bos_events: int = 0
    long_trades: int = 0
    long_r: float = 0.0
    
    short_bos_events: int = 0
    short_trades: int = 0
    short_r: float = 0.0
    
    # 🔥 NEW: Por tipo de edge
    trend_edges: int = 0
    trend_r: float = 0.0
    range_edges: int = 0
    range_r: float = 0.0
    breakout_edges: int = 0
    breakout_r: float = 0.0
    displacement_edges: int = 0  # 🔥 NEW (Jan 30, 2026)
    displacement_r: float = 0.0  # 🔥 NEW (Jan 30, 2026)
    
    def get_expectancy_per_bos(self) -> float:
        """Expectancy promedio por evento BOS"""
        if self.total_bos_events == 0:
            return 0.0
        return self.total_r_captured / self.total_bos_events
    
    def get_expectancy_per_trade(self) -> float:
        """Expectancy promedio por trade"""
        if self.total_trades == 0:
            return 0.0
        return self.total_r_captured / self.total_trades
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_bos_events': self.total_bos_events,
            'total_trades': self.total_trades,
            'win_rate': self.total_wins / self.total_trades if self.total_trades > 0 else 0,
            'total_r': round(self.total_r_captured, 2),
            'expectancy_per_bos': round(self.get_expectancy_per_bos(), 3),
            'expectancy_per_trade': round(self.get_expectancy_per_trade(), 3),
            'long': {
                'events': self.long_bos_events,
                'trades': self.long_trades,
                'r': round(self.long_r, 2)
            },
            'short': {
                'events': self.short_bos_events,
                'trades': self.short_trades,
                'r': round(self.short_r, 2)
            },
            'by_type': {
                'trend': {'edges': self.trend_edges, 'r': round(self.trend_r, 2)},
                'range': {'edges': self.range_edges, 'r': round(self.range_r, 2)},
                'breakout': {'edges': self.breakout_edges, 'r': round(self.breakout_r, 2)},
                'displacement': {'edges': self.displacement_edges, 'r': round(self.displacement_r, 2)}
            }
        }


# =============================================================================
# 🌊 EDGE TRANSITION RULES (BE WATER MY FRIEND)
# =============================================================================
@dataclass
class EdgeTransition:
    """
    Transición de estado - el edge FLUYE, no salta.
    
    Valid transitions:
    - NO_EDGE → ARMED (setup detected)
    - ARMED → PROVISIONAL (partial confirmation)
    - ARMED → ACTIVE (full confirmation)
    - PROVISIONAL → ACTIVE (upgrade on confirmation)
    - PROVISIONAL → EXPIRED (timeout or invalidation)
    - ACTIVE → EXPIRED (consumed, timeout, invalidation)
    - Any → NO_EDGE (reset)
    """
    from_state: EdgeState
    to_state: EdgeState
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    edge_type: Optional[EdgeType] = None
    direction: Optional[str] = None
    
    # Valid state transitions
    # 🔥 CRITICAL (Jan 28): NO_EDGE → ACTIVE solo con condiciones estrictas
    # Normalmente debe pasar por ARMED o PROVISIONAL
    VALID_TRANSITIONS = {
        EdgeState.NO_EDGE: [EdgeState.EDGE_ARMED, EdgeState.EDGE_PROVISIONAL, EdgeState.EDGE_ACTIVE],  # ACTIVE requires strict validation
        EdgeState.EDGE_ARMED: [EdgeState.EDGE_PROVISIONAL, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_EXPIRED],
        EdgeState.EDGE_PROVISIONAL: [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_EXPIRED],
        EdgeState.EDGE_ACTIVE: [EdgeState.EDGE_EXPIRED],
        EdgeState.EDGE_EXPIRED: [EdgeState.NO_EDGE],
    }
    
    def is_valid(self) -> bool:
        """Check if this transition is valid"""
        allowed = self.VALID_TRANSITIONS.get(self.from_state, [])
        return self.to_state in allowed or self.to_state == EdgeState.NO_EDGE
    
    def log(self):
        """
        Log transition with unified format.
        
        INTELLIGENT LOGGING
        - Only INFO for SIGNIFICANT transitions (direction/type changes, new edges)
        - DEBUG for redundant transitions (re-arming same edge repeatedly)
        """
        edge_type_str = self.edge_type.value if self.edge_type else "UNKNOWN"
        direction_str = self.direction or "?"
        
        # 🔥 SIGNIFICANT transitions → INFO
        # Everything else → DEBUG (reduces log spam 90%+)
        is_significant = (
            # New edge armed or activated
            self.to_state in (EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE) and 
            self.from_state == EdgeState.NO_EDGE
        ) or (
            # Edge expired due to price invalidation (not location)
            self.to_state == EdgeState.EDGE_EXPIRED and
            "location" not in self.reason.lower()
        ) or (
            # Trade taken
            "trade" in self.reason.lower()
        )
        
        level = "info" if is_significant else "debug"
        
        _edge_log(
            self.to_state.value,
            edge_type_str,
            direction_str,
            f"{self.from_state.value}→{self.to_state.value} | {self.reason}",
            level=level
        )
