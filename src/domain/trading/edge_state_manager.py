"""
EdgeStateManager - El edge como EVENTO DISCRETO, no como contexto continuo.

🔥 REFACTOR (Jan 28, 2026): "BE WATER MY FRIEND"
- Flujo fluido entre estados: NO_EDGE → ARMED → PROVISIONAL/ACTIVE → EXPIRED
- Transiciones rápidas cuando el mercado cambia
- Sin rigidez: el edge fluye con el contexto

EDGE TYPES:
- TREND: BOS + Pullback (mercado con ADX > 18)
- RANGE: Extremo 90/10 + Sweep + Rejection
- BREAKOUT: Compresión + Ruptura confirmada
- INTRADAY: Micro-ineficiencias alta frecuencia

EDGE MODES:
- INSTITUTIONAL: Low freq, high conviction (1-5/semana, 1.0x-1.5x)
- INTRADAY: High freq, low conviction (5-20/día, 0.15x-0.30x)

STATES:
- NO_EDGE: Sin oportunidad → no evaluar
- EDGE_ARMED: Setup detectado → esperando confirmación
- EDGE_PROVISIONAL: Señal válida pero incompleta → starter position (0.10x-0.20x)
- EDGE_ACTIVE: Confirmado → ventana de entrada abierta
- EDGE_EXPIRED: Consumido o expirado

PHILOSOPHY:
- EL EDGE OWNER TIENE AUTORIDAD SOBERANA
- Solo risk engine puede vetar por límites duros
- Las transiciones son FLUIDAS, no abruptas
- Máximo 2 trades por edge event
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
import logging
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# � EDGE TRANSITION SYMBOLS - Visual state tracking
# =============================================================================
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
# 🚨 DEPRECATED: EdgeMode (Feb 05, 2026)
# Mantenido por compatibilidad - el sizing ahora es por EdgeType
# =============================================================================
class EdgeMode(Enum):
    """
    ⚠️ DEPRECATED (Feb 05, 2026): Use EdgeType with sizing profiles instead.
    
    Modo de operación del edge - determina frequencia y sizing.
    Mantenido solo por compatibilidad con código legacy.
    """
    INSTITUTIONAL = "INSTITUTIONAL"  # DEPRECATED - use EdgeType sizing
    INTRADAY = "INTRADAY"            # DEPRECATED - absorbed into TREND_CONSENSUS


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
# 🔥 PROVISIONAL EDGE CONFIG (Jan 28, 2026)
# Estado intermedio: señal válida pero incompleta → starter position
# Esto existe en mesas reales y sube frecuencia 2-4× sin romper riesgo
# =============================================================================
# =============================================================================
# 🔥 PROVISIONAL METRICS TRACKING (Jan 28, 2026)
# Para saber si PROVISIONAL está bien definido o es "basurero"
# =============================================================================
@dataclass
class ProvisionalMetrics:
    """
    Métricas de PROVISIONAL para auditoría continua.
    
    Si % upgrade < 20% y PnL negativo → PROVISIONAL está mal definido.
    Si churn alto (PROV→EXP→PROV) → lateral extremo, bleeding de fees.
    """
    # Contadores principales
    total_provisional: int = 0        # Total entradas PROVISIONAL
    upgraded_to_active: int = 0       # Cuántos upgradearon a ACTIVE
    expired_without_upgrade: int = 0  # Cuántos expiraron sin upgrade
    
    # PnL tracking
    total_pnl_r: float = 0.0         # PnL total en R
    provisional_only_pnl_r: float = 0.0  # PnL de trades que NO upgradearon
    
    # Timing
    total_time_in_provisional_sec: int = 0  # Tiempo total en estado PROVISIONAL
    
    # Churn detection (PROV→EXP→PROV en lateral)
    provisional_this_hour: int = 0   # Conteo para detectar churn
    last_hour_reset: Optional[datetime] = None
    
    def get_upgrade_rate(self) -> float:
        """% de PROVISIONAL que upgradearon a ACTIVE"""
        if self.total_provisional == 0:
            return 0.0
        return (self.upgraded_to_active / self.total_provisional) * 100
    
    def get_avg_time_sec(self) -> float:
        """Tiempo medio en PROVISIONAL (segundos)"""
        if self.total_provisional == 0:
            return 0.0
        return self.total_time_in_provisional_sec / self.total_provisional
    
    def is_healthy(self) -> Tuple[bool, str]:
        """
        ¿PROVISIONAL está funcionando bien?
        
        UNHEALTHY si:
        - Upgrade rate < 20% (PROVISIONAL es basurero)
        - PnL negativo con > 5 trades (bleeding)
        """
        if self.total_provisional < 5:
            return True, "Insufficient data (<5 trades)"
        
        upgrade_rate = self.get_upgrade_rate()
        if upgrade_rate < 20 and self.total_pnl_r < 0:
            return False, f"UNHEALTHY: {upgrade_rate:.0f}% upgrade, PnL={self.total_pnl_r:.2f}R"
        
        if self.provisional_only_pnl_r < -2.0:
            return False, f"UNHEALTHY: Non-upgraded PROVISIONAL bleeding {self.provisional_only_pnl_r:.2f}R"
        
        return True, f"OK: {upgrade_rate:.0f}% upgrade, PnL={self.total_pnl_r:.2f}R"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'total': self.total_provisional,
            'upgraded': self.upgraded_to_active,
            'expired': self.expired_without_upgrade,
            'upgrade_rate_pct': round(self.get_upgrade_rate(), 1),
            'total_pnl_r': round(self.total_pnl_r, 2),
            'avg_time_sec': round(self.get_avg_time_sec(), 1),
            'this_hour': self.provisional_this_hour,
            'healthy': self.is_healthy()[0]
        }


@dataclass
class ProvisionalEdgeConfig:
    """
    Configuración para EDGE_PROVISIONAL - ENTRADA BARATA PARA TESTEAR DIRECCIÓN.
    
    🔥 REDEFINICIÓN (Jan 28, 2026):
    
    ANTES (mal): PROVISIONAL = "casi ACTIVE pero con menos convicción"
    AHORA (bien): PROVISIONAL = "entrada barata para probar dirección"
    
    El flujo correcto es:
    - Entrar pequeño (PROVISIONAL) → confirmar → añadir/upgrade (ACTIVE)
    - Si no confirma → cortar rápido con pérdida pequeña
    
    Esto es exactamente cómo operan traders profesionales intraday.
    
    Se activa si:
    - timing >= 0 (no necesita timing perfecto)
    - R >= 0.20 (no 0.30-0.35)
    - Bias claro (ADX_5m >= 25)
    - NO requiere LiquidityEvent (eso es BOOST, no gate)
    
    Características:
    - Size 0.25x-0.35x (suficiente para ganar algo)
    - SL técnico corto
    - Time stop 3-5 min
    - Puede upgrade a ACTIVE si confirma
    """
    # =========================================================================
    # 🔥 CRITERIOS RELAJADOS PARA PROVISIONAL (entrada barata)
    # 🔄 REFACTORED Feb 04: Use ADXThresholds for consistency
    # 🔥 FIX #58 (Feb 06): Reducir ADX de 25→20 para entradas más tempranas
    # =========================================================================
    MIN_TIMING = 0                   # timing >= 0 (no >= 1)
    MIN_R = 0.20                     # R >= 0.20 (no 0.30)
    # 🔥 FIX #58: ADX reducido para permitir entradas PRE-TREND
    # Antes: 25 (STRONG_TREND) - muy restrictivo, perdía compresiones
    # Ahora: 20 (MODERATE_TREND) - permite entradas con bias en desarrollo
    MIN_ADX_5M = 20                  # ADX 5m >= 20 (bias en desarrollo)
    REQUIRES_LIQUIDITY_EVENT = False # NO requiere LiquidityEvent
    
    # =========================================================================
    # SIZING - Suficiente para ganar algo, no tanto para doler
    # =========================================================================
    SIZE_MULTIPLIER = 0.30           # 30% del size normal (antes 20%)
    SIZE_MULTIPLIER_HIGH_ADX = 0.35  # 35% si ADX_5m > 35
    SIZE_MULTIPLIER_LOW_CONVICTION = 0.25  # 25% mínimo
    
    # =========================================================================
    # TARGETS - TP realista para PROVISIONAL
    # =========================================================================
    TP_R_MIN = 0.4                   # TP mínimo: 0.4R
    TP_R_MAX = 0.8                   # TP máximo: 0.8R (puede correr)
    
    # =========================================================================
    # TIME MANAGEMENT
    # =========================================================================
    TIME_STOP_MIN = 3                # Invalidar si no mueve en 3 min
    TIME_STOP_MAX = 5                # Máximo 5 min en PROVISIONAL
    
    # =========================================================================
    # 🔥 KILL SWITCH - Evita churn en lateral (PROV→EXP→PROV)
    # =========================================================================
    MAX_PROVISIONAL_PER_HOUR = 6     # 6 PROVISIONAL/hora (antes 3 - muy bajo)
    CHURN_COOLDOWN_MIN = 15          # 15 min cooldown si excede (antes 30)
    
    # =========================================================================
    # LIMITS
    # =========================================================================
    MAX_CONSECUTIVE = 2              # Máximo 2 PROVISIONAL consecutivos (antes 1)
    COOLDOWN_AFTER_LOSS_MIN = 5      # 5 min cooldown si pierde (antes 10)
    
    # =========================================================================
    # 🔥 UPGRADE CONDITIONS - Simple, no perfeccionista
    # =========================================================================
    UPGRADE_MIN_PROFIT_R = 0.15      # Si alcanza +0.15R → considerar upgrade
    UPGRADE_REQUIRES_CONFIRMATION = False  # NO necesita confirmación adicional
    UPGRADE_ON_TIMING_IMPROVE = True # Si timing mejora de 0 a 1+ → upgrade
    
    # Criteria thresholds (más relajados)
    MIN_CRITERIA_MET = 2             # Mínimo 2 de 4 criterios (antes 3 de 5)
    TOTAL_CRITERIA = 4               # Total de criterios evaluados


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
    # 🔥 FIX #230 (Feb 12, 2026): EDGE TTL RADICAL
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
    
    🔥 FIX #97 (Feb 09): Relajar criterios - anticipation acierta más de lo esperado
    """
    BB_WIDTH_PERCENTILE_MAX = 25  # 🔥 Subido de 10 a 25 - percentil 25 ya indica compresión relativa
    VOLUME_DECLINE_RATIO = 0.70   # 🔥 Subido de 0.50 a 0.70 - volumen ≤70% es suficiente
    BREAKOUT_CONFIRMATION_CANDLES = 2  # 2 velas para confirmar ruptura
    # 🔥 FIX #230 (Feb 12, 2026): EDGE TTL RADICAL
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
#
# PROBLEMA RESUELTO:
# - EXPANSION_MODE + DIRECTION_LOCK funcionaban pero NO generaban EDGE
# - Sin EDGE → sistema no puede tradear aunque detecte displacement
# - RESULTADO: 0 trades en caída de 2.3%
#
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
    # 
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
    # 🔥 FIX #230 (Feb 12, 2026): EDGE TTL RADICAL
    # CRÍTICO: El análisis brutal demostró que 90 min = LOCURA para BTC intradía.
    # Si no capturas displacement en 20 min, el movimiento ya se consumió.
    # "Confirmation-based" = LATE. Reducir TTL fuerza timing temprano.
    EDGE_TIMEOUT_MIN = 20             # 🔥 Was 90 → 20 min (TTL radical)
    CONFIRMATION_TIMEOUT_MIN = 10     # 🔥 Was 30 → 10 min (TTL radical)
    
    # 🔥 KILL LATE EDGE - FIX #230 (Feb 12, 2026)
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


# =============================================================================
# �🎯 INTRADAY EDGE CRITERIA (Jan 28, 2026)
# Edge de alta frecuencia, baja convicción - paga fees + genera curva
# 
# 🔥 REGLA CRÍTICA: INTRADAY tiene SUS PROPIOS FILTROS
# NO comparte los frenos de INSTITUTIONAL o se asfixia.
#
# ⚠️ CRÍTICO (Jan 28): R < 0.3 REQUIERE LIQUIDITY EVENT
# Sin sweep/rejection → NO TRADE. Sin negociación.
# =============================================================================

class LiquidityEventType(Enum):
    """Tipos de eventos de liquidez que validan trades de R bajo"""
    SWEEP = "SWEEP"              # Sweep de swing previo
    REJECTION = "REJECTION"      # Rejection candle fuerte
    ABSORPTION = "ABSORPTION"    # Absorción visible en book
    DELTA_DIVERGENCE = "DELTA_DIVERGENCE"  # Delta vs precio diverge
    FAILURE_TEST = "FAILURE_TEST"  # Intento fallido de romper nivel
    NONE = "NONE"                # Sin evento (trade inválido si R bajo)


@dataclass
class LiquidityEvent:
    """
    Evento de liquidez detectado - REQUERIDO para trades de R bajo.
    
    Sin liquidity event + R < 0.3 = NO TRADE.
    Esto es la diferencia entre scalping con edge y gambling.
    """
    event_type: LiquidityEventType = LiquidityEventType.NONE
    detected: bool = False
    price_level: float = 0.0
    timestamp: Optional[datetime] = None
    confidence: float = 0.0  # 0-1, basado en evidencia
    
    # Datos específicos del evento
    sweep_distance_atr: float = 0.0  # Cuánto penetró el swing
    rejection_body_pct: float = 0.0  # % cuerpo vs mecha en rejection
    delta_divergence_ratio: float = 0.0  # Ratio delta vs movimiento
    
    def is_valid(self) -> bool:
        """¿Es un evento de liquidez válido para permitir R bajo?"""
        return self.detected and self.event_type != LiquidityEventType.NONE and self.confidence >= 0.6


@dataclass
class IntradayEdgeCriteria:
    """
    Criterios para INTRADAY edge - micro-ineficiencias repetibles.
    
    🔥 REDEFINICIÓN (Jan 28, 2026):
    
    ANTES: LiquidityEvent como GATE binario → sistema estéril
    AHORA: LiquidityEvent como BOOST → aumenta size/permite ACTIVE directo
    
    EDGE BASE INTRADAY (ocurre varias veces al día):
    - ADX_5m >= 25 (bias claro)
    - SL técnico <= 0.5R
    - RR target 0.4-0.8R
    
    Con esto esperamos:
    - 8-15 PROVISIONAL/día
    - 3-6 ACTIVE/día
    - Winrate PROVISIONAL ~45% (da igual - son baratos)
    - Winrate ACTIVE ~55%
    """
    # =========================================================================
    # 🔥 EDGE BASE INTRADAY (Simple, repetible)
    # =========================================================================
    BASE_ADX_5M_MIN = 25             # ADX 5m >= 25 (bias claro)
    BASE_SL_R_MAX = 0.5              # SL máximo 0.5R
    BASE_RR_TARGET_MIN = 0.4         # RR target mínimo
    BASE_RR_TARGET_MAX = 0.8         # RR target máximo
    
    # =========================================================================
    # 🔥 R-BUCKET CLASSIFICATION (Relajado)
    # =========================================================================
    
    # R bajo: PERMITE trades direccionales (no solo mean reversion)
    R_LOW_MAX = 0.30                 # R < 0.30 = bajo (ajustado de 0.25)
    R_LOW_REQUIRES_LIQUIDITY = False # NO requiere liquidity (ahora es BOOST)
    
    # R normal: Trade estándar
    R_NORMAL_MIN = 0.30              # R >= 0.30 = normal
    R_NORMAL_MAX = 0.60              # R <= 0.60 = intraday
    
    # R alto: Ya no es scalp puro
    R_HIGH_MIN = 0.60                # R > 0.60 = gestión diferente
    
    # =========================================================================
    # 🔥 LIQUIDITY EVENT = BOOST (no gate)
    # =========================================================================
    
    # LiquidityEvent NO bloquea trades, pero mejora sizing
    LIQUIDITY_BLOCKS_TRADE = False   # 🔥 CAMBIO: No bloquea
    LIQUIDITY_SIZE_BOOST = 1.3       # +30% size si hay liquidity event
    LIQUIDITY_ALLOWS_DIRECT_ACTIVE = True  # Permite ACTIVE directo
    LIQUIDITY_MIN_CONFIDENCE = 0.5   # Confianza mínima para boost
    
    # Parámetros de detección de sweep (para BOOST, no para gate)
    SWEEP_MIN_PENETRATION_ATR = 0.05   # Mínimo 0.05 ATR de penetración
    SWEEP_MAX_PENETRATION_ATR = 0.40   # Máximo 0.4 ATR (si no, es BOS)
    SWEEP_REJECTION_TIME_MAX_CANDLES = 3  # Rejection debe ser rápido
    
    # Parámetros de rejection candle
    REJECTION_MIN_WICK_BODY_RATIO = 2.0  # Mecha >= 2x cuerpo
    REJECTION_MIN_WICK_ATR = 0.3         # Mecha >= 0.3 ATR
    
    # =========================================================================
    # FILTROS PROPIOS DE INTRADAY (independientes de INSTITUTIONAL)
    # =========================================================================
    
    # Session: INTRADAY NO bloquea por poor session
    SESSION_POOR_BLOCKS = False      # No bloquear por session
    SESSION_POOR_SIZE_MULT = 0.7     # Reducir size 30% en poor (antes 50%)
    
    # ADX: INTRADAY NO necesita tendencia alta
    ADX_BLOCKS = False               # No bloquear por ADX bajo
    ADX_LOW_SIZE_MULT = 0.9          # Reducir levemente si ADX < 15
    
    # RR: INTRADAY tiene threshold bajo
    RR_BLOCK_THRESHOLD = 0.15        # 0.15R mínimo absoluto
    RR_REDUCED_THRESHOLD = 0.30      # 0.30R para size completo
    
    # Score: INTRADAY NO usa scoring complejo
    USE_SCORE_FILTER = False         # No aplicar score threshold
    
    # =========================================================================
    # Requisitos de ACTIVIDAD de mercado (Relajados)
    # =========================================================================
    ATR_EXPANSION_RATIO_MIN = 1.0    # ATR actual / ATR medio >= 1.0 (antes 1.1)
    VOLUME_RATIO_MIN = 0.8           # Volumen >= 80% media (antes 100%)
    
    # Requisitos de DIRECCIÓN clara
    MIN_SWING_SEQUENCE = 1           # Mínimo 1 HH/HL o LL/LH (antes 2)
    MOMENTUM_ALIGNMENT = True        # No contra momentum inmediato
    
    # =========================================================================
    # SIZING para INTRADAY (Aumentado)
    # =========================================================================
    SIZE_MIN = 0.20                  # 20% del size normal (antes 15%)
    SIZE_MAX = 0.40                  # 40% máximo (antes 30%)
    SIZE_WITH_LIQUIDITY_BOOST = 0.50 # 50% si hay liquidity event
    
    # =========================================================================
    # 🔥 SL TÉCNICO (no fijo) - Último micro swing ± buffer
    # =========================================================================
    SL_USE_TECHNICAL = True          # Usar SL técnico, no fijo
    SL_SWING_BUFFER_ATR = 0.15       # Buffer de 0.15 ATR sobre swing
    SL_R_CAP = 0.5                   # Cap máximo 0.5R (seguridad)
    SL_R_MIN = 0.15                  # Mínimo 0.15R (evitar stops ridículos)
    
    # TP targets por R-bucket
    TP_R_TARGET = 0.5                # TP modesto: 0.5R
    TP_R_MIN = 0.3                   # Mínimo 0.3R
    TP_R_MAX = 0.8                   # Máximo 0.8R
    
    # =========================================================================
    # 🔥 TIME INVALIDATION (MUY CORTA para R bajo)
    # =========================================================================
    TIME_INVALIDATION_R_LOW_MIN = 2       # R < 0.3: Invalidar en 2 min si no mueve
    TIME_INVALIDATION_R_NORMAL_MIN = 5    # R >= 0.3: 5 min standard
    
    # =========================================================================
    # Frecuencia y timeouts (SEPARADOS de INSTITUTIONAL)
    # =========================================================================
    MAX_TRADES_PER_HOUR = 5          # Máximo 5 trades/hora
    MAX_TRADES_PER_SESSION = 15      # Máximo 15 trades/session (INTRADAY specific)
    EDGE_TIMEOUT_MIN = 5             # 5 min timeout (rápido)
    MIN_BETWEEN_TRADES_MIN = 3       # Mínimo 3 min entre trades
    
    # Session: INTRADAY puede operar más horas (no solo LONDON/NY)
    REQUIRE_ACTIVE_SESSION = False   # No requiere session activa
    PREFER_ACTIVE_SESSION = True     # Pero prefiere (mejor size)


# =============================================================================
# 🎯 INSTITUTIONAL EDGE CONFIG (Jan 28, 2026)
# Configuración centralizada para edge institucional
# =============================================================================
@dataclass
class InstitutionalEdgeConfig:
    """
    Configuración para EDGE_INSTITUTIONAL.
    
    Características:
    - Frecuencia: 1-5 trades/semana
    - Size: 1.0x - 1.5x (mayor que normal)
    - RR esperado: 2R+
    - Drawdown aceptado: mayor
    
    Tipos de edge que califican como INSTITUTIONAL:
    - BREAKOUT confirmado con compresión previa
    - RANGE en extremos 90/10 con sweep+rejection
    """
    # Size multipliers
    SIZE_MULTIPLIER_BASE = 1.0       # Size base
    SIZE_MULTIPLIER_HIGH_CONVICTION = 1.5  # Alta convicción
    
    # Criterios para HIGH_CONVICTION (size 1.5x)
    HIGH_CONVICTION_PERCENTILE_EXTREME = 95  # Percentil ≥95 o ≤5
    HIGH_CONVICTION_BB_SQUEEZE = 5           # BB width < percentil 5
    HIGH_CONVICTION_SESSION = ['LONDON', 'NY_OVERLAP']  # Sesiones óptimas
    
    # RR requirements
    MIN_RR = 2.0                     # RR mínimo para INSTITUTIONAL
    IDEAL_RR = 3.0                   # RR ideal


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
    
    def get_expectancy_by_type(self, edge_type: EdgeType) -> float:
        """Expectancy por tipo de edge"""
        if edge_type == EdgeType.TREND:
            return self.trend_r / self.trend_edges if self.trend_edges > 0 else 0.0
        elif edge_type == EdgeType.RANGE:
            return self.range_r / self.range_edges if self.range_edges > 0 else 0.0
        elif edge_type == EdgeType.BREAKOUT:
            return self.breakout_r / self.breakout_edges if self.breakout_edges > 0 else 0.0
        elif edge_type == EdgeType.DISPLACEMENT:
            return self.displacement_r / self.displacement_edges if self.displacement_edges > 0 else 0.0
        return 0.0
    
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
# 🔥 LIQUIDITY EVENT DETECTION (Jan 28, 2026)
# Funciones para detectar sweeps, rejections, y otros eventos de liquidez
# CRÍTICO: Sin liquidity event + R < 0.3 = NO TRADE
# =============================================================================

def detect_sweep(
    df: pd.DataFrame,
    swing_level: float,
    direction: str,
    atr: float,
    lookback_candles: int = 5
) -> LiquidityEvent:
    """
    Detecta sweep (liquidity grab) de un nivel de swing.
    
    Un sweep válido para INTRADAY:
    1. Precio penetra swing level brevemente (0.05-0.4 ATR)
    2. Rechazo rápido (1-3 velas)
    3. Close de vuelta al lado original
    
    Args:
        df: DataFrame con OHLC
        swing_level: Nivel del swing previo (high o low)
        direction: 'LONG' (buscamos sweep de low) o 'SHORT' (sweep de high)
        atr: ATR actual para normalizar penetración
        lookback_candles: Cuántas velas mirar hacia atrás
    
    Returns:
        LiquidityEvent con detección y confianza
    """
    if len(df) < lookback_candles or atr <= 0:
        return LiquidityEvent(event_type=LiquidityEventType.NONE)
    
    recent = df.tail(lookback_candles)
    
    # Parámetros de IntradayEdgeCriteria
    min_pen = IntradayEdgeCriteria.SWEEP_MIN_PENETRATION_ATR * atr
    max_pen = IntradayEdgeCriteria.SWEEP_MAX_PENETRATION_ATR * atr
    
    sweep_detected = False
    sweep_price = 0.0
    sweep_distance = 0.0
    confidence = 0.0
    
    for i, row in recent.iterrows():
        if direction == 'LONG':
            # Buscamos sweep de low (price baja, toca/penetra swing_low, y cierra arriba)
            penetration = swing_level - row['low']
            if penetration > min_pen and penetration < max_pen:
                # Precio penetró el swing low
                if row['close'] > swing_level:
                    # Y cerró por encima → sweep confirmado
                    sweep_detected = True
                    sweep_price = row['low']
                    sweep_distance = penetration / atr
                    
                    # Confianza basada en mecha vs cuerpo
                    lower_wick = min(row['open'], row['close']) - row['low']
                    body = abs(row['close'] - row['open'])
                    if body > 0:
                        wick_body_ratio = lower_wick / body
                        confidence = min(0.5 + (wick_body_ratio / 5.0), 1.0)  # Max 1.0
                    else:
                        confidence = 0.6
                    break
        else:
            # direction == 'SHORT': buscamos sweep de high
            penetration = row['high'] - swing_level
            if penetration > min_pen and penetration < max_pen:
                # Precio penetró el swing high
                if row['close'] < swing_level:
                    # Y cerró por debajo → sweep confirmado
                    sweep_detected = True
                    sweep_price = row['high']
                    sweep_distance = penetration / atr
                    
                    # Confianza basada en mecha superior vs cuerpo
                    upper_wick = row['high'] - max(row['open'], row['close'])
                    body = abs(row['close'] - row['open'])
                    if body > 0:
                        wick_body_ratio = upper_wick / body
                        confidence = min(0.5 + (wick_body_ratio / 5.0), 1.0)
                    else:
                        confidence = 0.6
                    break
    
    if sweep_detected:
        return LiquidityEvent(
            event_type=LiquidityEventType.SWEEP,
            detected=True,
            price_level=sweep_price,
            timestamp=datetime.now(),
            confidence=confidence,
            sweep_distance_atr=sweep_distance
        )
    
    return LiquidityEvent(event_type=LiquidityEventType.NONE)


def detect_rejection_candle(
    df: pd.DataFrame,
    direction: str,
    atr: float,
    lookback_candles: int = 3
) -> LiquidityEvent:
    """
    Detecta rejection candle fuerte (pin bar / hammer / shooting star).
    
    Rejection candle válido:
    1. Mecha >= 2x cuerpo
    2. Mecha >= 0.3 ATR
    3. Close en dirección favorable
    
    Args:
        df: DataFrame con OHLC
        direction: 'LONG' (buscamos rejection con lower wick) o 'SHORT' (upper wick)
        atr: ATR actual
        lookback_candles: Cuántas velas mirar
    
    Returns:
        LiquidityEvent con detección y confianza
    """
    if len(df) < lookback_candles or atr <= 0:
        return LiquidityEvent(event_type=LiquidityEventType.NONE)
    
    recent = df.tail(lookback_candles)
    
    min_wick_body_ratio = IntradayEdgeCriteria.REJECTION_MIN_WICK_BODY_RATIO
    min_wick_atr = IntradayEdgeCriteria.REJECTION_MIN_WICK_ATR * atr
    
    for i, row in recent.iterrows():
        body = abs(row['close'] - row['open'])
        body = max(body, 0.01)  # Evitar división por cero
        
        if direction == 'LONG':
            # Buscamos lower wick fuerte (hammer)
            lower_wick = min(row['open'], row['close']) - row['low']
            upper_wick = row['high'] - max(row['open'], row['close'])
            
            if lower_wick >= min_wick_atr and lower_wick >= min_wick_body_ratio * body:
                # Validar que lower_wick > upper_wick (rechazo genuino)
                if lower_wick > upper_wick * 1.5:
                    wick_body_ratio = lower_wick / body
                    confidence = min(0.5 + (wick_body_ratio / 6.0), 0.95)
                    
                    # Bonus si cerró en half superior
                    candle_range = row['high'] - row['low']
                    if candle_range > 0:
                        close_position = (row['close'] - row['low']) / candle_range
                        if close_position > 0.6:
                            confidence = min(confidence + 0.1, 1.0)
                    
                    return LiquidityEvent(
                        event_type=LiquidityEventType.REJECTION,
                        detected=True,
                        price_level=row['low'],
                        timestamp=datetime.now(),
                        confidence=confidence,
                        rejection_body_pct=1.0 / (1.0 + wick_body_ratio) * 100
                    )
        else:
            # direction == 'SHORT': buscamos upper wick fuerte (shooting star)
            lower_wick = min(row['open'], row['close']) - row['low']
            upper_wick = row['high'] - max(row['open'], row['close'])
            
            if upper_wick >= min_wick_atr and upper_wick >= min_wick_body_ratio * body:
                # Validar que upper_wick > lower_wick
                if upper_wick > lower_wick * 1.5:
                    wick_body_ratio = upper_wick / body
                    confidence = min(0.5 + (wick_body_ratio / 6.0), 0.95)
                    
                    # Bonus si cerró en half inferior
                    candle_range = row['high'] - row['low']
                    if candle_range > 0:
                        close_position = (row['close'] - row['low']) / candle_range
                        if close_position < 0.4:
                            confidence = min(confidence + 0.1, 1.0)
                    
                    return LiquidityEvent(
                        event_type=LiquidityEventType.REJECTION,
                        detected=True,
                        price_level=row['high'],
                        timestamp=datetime.now(),
                        confidence=confidence,
                        rejection_body_pct=1.0 / (1.0 + wick_body_ratio) * 100
                    )
    
    return LiquidityEvent(event_type=LiquidityEventType.NONE)


def detect_failure_test(
    df: pd.DataFrame,
    test_level: float,
    direction: str,
    atr: float,
    lookback_candles: int = 5
) -> LiquidityEvent:
    """
    Detecta failure test (intento fallido de romper un nivel).
    
    Failure test:
    1. Precio se acerca al nivel
    2. No logra romper con momentum
    3. Reversión rápida
    
    Similar a sweep pero no necesita penetración, solo rechazo del nivel.
    
    Args:
        df: DataFrame con OHLC
        test_level: Nivel siendo testeado
        direction: 'LONG' (testeando soporte) o 'SHORT' (testeando resistencia)
        atr: ATR actual
        lookback_candles: Velas a mirar
    
    Returns:
        LiquidityEvent
    """
    if len(df) < lookback_candles or atr <= 0:
        return LiquidityEvent(event_type=LiquidityEventType.NONE)
    
    recent = df.tail(lookback_candles)
    proximity_threshold = 0.1 * atr  # Dentro de 0.1 ATR del nivel
    
    test_detected = False
    confidence = 0.0
    test_price = 0.0
    
    for idx, (i, row) in enumerate(recent.iterrows()):
        if direction == 'LONG':
            # Testeando soporte: low cerca del nivel, pero close arriba
            distance_to_level = abs(row['low'] - test_level)
            if distance_to_level < proximity_threshold:
                # ¿Rechazó con fuerza?
                close_distance = row['close'] - test_level
                if close_distance > 0.2 * atr:  # Cerró 0.2 ATR arriba
                    test_detected = True
                    test_price = row['low']
                    # Confianza basada en qué tan fuerte fue el rechazo
                    confidence = min(0.5 + (close_distance / atr) * 0.5, 0.85)
                    break
        else:
            # Testeando resistencia: high cerca del nivel, pero close abajo
            distance_to_level = abs(row['high'] - test_level)
            if distance_to_level < proximity_threshold:
                # ¿Rechazó con fuerza?
                close_distance = test_level - row['close']
                if close_distance > 0.2 * atr:
                    test_detected = True
                    test_price = row['high']
                    confidence = min(0.5 + (close_distance / atr) * 0.5, 0.85)
                    break
    
    if test_detected:
        return LiquidityEvent(
            event_type=LiquidityEventType.FAILURE_TEST,
            detected=True,
            price_level=test_price,
            timestamp=datetime.now(),
            confidence=confidence
        )
    
    return LiquidityEvent(event_type=LiquidityEventType.NONE)


def detect_any_liquidity_event(
    df: pd.DataFrame,
    direction: str,
    atr: float,
    swing_level: Optional[float] = None,
    test_level: Optional[float] = None,
    lookback_candles: int = 5
) -> LiquidityEvent:
    """
    Detecta cualquier tipo de liquidity event válido.
    
    Orden de prioridad (mayor evidencia primero):
    1. Sweep (mayor confianza)
    2. Rejection candle
    3. Failure test
    
    Args:
        df: DataFrame con OHLC
        direction: 'LONG' o 'SHORT'
        atr: ATR actual
        swing_level: Nivel de swing para sweep detection (opcional)
        test_level: Nivel para failure test (opcional)
        lookback_candles: Velas a mirar
    
    Returns:
        LiquidityEvent del mejor evento encontrado
    """
    best_event = LiquidityEvent(event_type=LiquidityEventType.NONE)
    
    # 1. Intentar detectar sweep (mayor confianza)
    if swing_level is not None:
        sweep = detect_sweep(df, swing_level, direction, atr, lookback_candles)
        if sweep.is_valid() and sweep.confidence > best_event.confidence:
            best_event = sweep
            logger.debug(f"[LIQUIDITY] Sweep detected: {sweep.confidence:.2f} confidence")
    
    # 2. Intentar rejection candle
    rejection = detect_rejection_candle(df, direction, atr, lookback_candles)
    if rejection.is_valid() and rejection.confidence > best_event.confidence:
        best_event = rejection
        logger.debug(f"[LIQUIDITY] Rejection detected: {rejection.confidence:.2f} confidence")
    
    # 3. Intentar failure test
    if test_level is not None:
        failure = detect_failure_test(df, test_level, direction, atr, lookback_candles)
        if failure.is_valid() and failure.confidence > best_event.confidence:
            best_event = failure
            logger.debug(f"[LIQUIDITY] Failure test detected: {failure.confidence:.2f} confidence")
    
    return best_event


# ⚠️ DEPRECATED (Feb 05, 2026): This function is never called - INTRADAY absorbed into TREND
def validate_intraday_r_with_liquidity(
    estimated_r: float,
    liquidity_event: Optional[LiquidityEvent],
    direction: str
) -> Tuple[bool, str, float]:
    """
    🔥 DEPRECATED (Feb 05, 2026): INTRADAY mode absorbed into TREND with CONSENSUS path.
    This function is preserved for backwards compatibility but is not called.
    
    FILOSOFÍA (Jan 28, 2026):
    - LiquidityEvent NO bloquea trades, pero mejora sizing
    - R bajo (< 0.30) permitido sin LiquidityEvent → entra como PROVISIONAL
    - Con LiquidityEvent → boost de size (+30%)
    
    Args:
        estimated_r: R estimado del trade
        liquidity_event: Evento de liquidez detectado (puede ser None)
        direction: 'LONG' o 'SHORT'
    
    Returns:
        (is_valid, reason, size_multiplier)
    """
    cfg = IntradayEdgeCriteria()
    
    # Mínimo absoluto de R
    if estimated_r < cfg.RR_BLOCK_THRESHOLD:
        return False, f"R={estimated_r:.2f} < {cfg.RR_BLOCK_THRESHOLD} mínimo absoluto", 0.0
    
    # Size base
    size_mult = cfg.SIZE_MIN
    
    # R normal o alto → size estándar
    if estimated_r >= cfg.RR_REDUCED_THRESHOLD:
        size_mult = cfg.SIZE_MAX
    
    # 🔥 LiquidityEvent = BOOST (no gate)
    if liquidity_event and liquidity_event.is_valid():
        # Aumentar size
        size_mult = min(size_mult * cfg.LIQUIDITY_SIZE_BOOST, cfg.SIZE_WITH_LIQUIDITY_BOOST)
        return True, f"R={estimated_r:.2f} OK + LiqEvent boost ({liquidity_event.event_type.value})", size_mult
    
    # Sin LiquidityEvent pero R OK → permitido (entrará como PROVISIONAL)
    return True, f"R={estimated_r:.2f} OK (no LiqEvent → PROVISIONAL)", size_mult


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
    timestamp: datetime = field(default_factory=datetime.utcnow)
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
    
    # 🔥 Requisitos ESTRICTOS para NO_EDGE → ACTIVE (evita skip de validación)
    DIRECT_ACTIVE_MIN_R = 0.35           # R mínimo para direct activation
    DIRECT_ACTIVE_MIN_TIMING = 1         # timing >= 1 obligatorio
    DIRECT_ACTIVE_REQUIRES_LIQUIDITY = True  # Necesita LiquidityEvent confirmado
    
    def is_valid(self) -> bool:
        """Check if this transition is valid"""
        allowed = self.VALID_TRANSITIONS.get(self.from_state, [])
        return self.to_state in allowed or self.to_state == EdgeState.NO_EDGE
    
    @classmethod
    def validate_direct_activation(
        cls,
        estimated_r: float,
        timing_score: int,
        liquidity_event: Optional['LiquidityEvent'] = None
    ) -> Tuple[bool, str]:
        """
        🔥 Valida si una transición directa NO_EDGE → ACTIVE está permitida.
        
        Esta transición es PELIGROSA porque:
        - Oculta calidad de entrada
        - Mezcla trades buenos con mediocres
        - Rompe métricas
        
        Solo se permite si:
        - LiquidityEvent confirmado
        - R >= 0.35
        - Timing >= 1
        
        Si no cumple todo → debe pasar por PROVISIONAL.
        
        Returns: (is_valid, reason)
        """
        reasons = []
        
        # Check R mínimo
        if estimated_r < cls.DIRECT_ACTIVE_MIN_R:
            reasons.append(f"R={estimated_r:.2f} < {cls.DIRECT_ACTIVE_MIN_R}")
        
        # Check timing
        if timing_score < cls.DIRECT_ACTIVE_MIN_TIMING:
            reasons.append(f"timing={timing_score} < {cls.DIRECT_ACTIVE_MIN_TIMING}")
        
        # Check liquidity event
        if cls.DIRECT_ACTIVE_REQUIRES_LIQUIDITY:
            if liquidity_event is None or not liquidity_event.is_valid():
                reasons.append("no valid LiquidityEvent")
        
        if reasons:
            return False, f"Direct ACTIVE blocked: {', '.join(reasons)} → use PROVISIONAL"
        
        return True, "Direct ACTIVE allowed: all criteria met"
    
    def log(self):
        """
        Log transition with unified format.
        
        🔥 FIX (Feb 05, 2026): INTELLIGENT LOGGING
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
    EDGE_ACTIVE_TIMEOUT_MIN = 15     # Ventana de entrada tras pullback
    PULLBACK_MIN_ATR = 0.3           # Mínimo pullback en ATR
    PULLBACK_MAX_ATR = 1.5           # Máximo pullback en ATR
    
    # Configuración RANGE
    RANGE_EDGE_TIMEOUT_MIN = 10      # Ventana más corta en rango
    
    # 🔥 Cooldown por contexto - evita pérdidas correlacionadas
    CONTEXT_COOLDOWN_MINUTES = 15
    
    # 🔥 Cooldown por DIRECCIÓN tras pérdida (evita revenge trading algorítmico)
    DIRECTION_COOLDOWN_AFTER_LOSS_MIN = 10  # 10 min cooldown en misma dirección tras pérdida
    
    def __init__(self):
        self._state = EdgeState.NO_EDGE
        self._current_bos: Optional[BOSEvent] = None
        self._stats = EdgeStats()
        self._bos_history: List[BOSEvent] = []
        
        # 🔧 FIX #128 (Feb 09, 2026): Track reason for NO_EDGE state
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
        
        # 🔥 Cooldown por CONTEXTO + DIRECCIÓN (evita revenge trading algorítmico)
        # Key = "BREAKOUT_LONG" o "TREND_SHORT", Value = datetime hasta cuando está en cooldown
        self._direction_cooldown: Dict[str, datetime] = {}
        
        # Edge Mode - INSTITUTIONAL vs INTRADAY
        self._current_edge_mode: EdgeMode = EdgeMode.INTRADAY
        self._intraday_trades_this_hour: int = 0
        self._last_intraday_trade_time: Optional[datetime] = None
        self._last_hour_reset: Optional[datetime] = None
        
        # 🔥 PROVISIONAL metrics & kill switch
        self._provisional_metrics = ProvisionalMetrics()
        self._provisional_blocked_until: Optional[datetime] = None  # Kill switch
        self._provisional_start_time: Optional[datetime] = None  # Para calcular duración
        
        # Current market regime
        self._current_regime: str = "UNKNOWN"
        self._current_adx: float = 20.0
        
        # 🔥 FIX (Feb 05, 2026): Location extreme cooldown
        # Prevents infinite edge re-arm cycle when at price extreme
        # When edge expires due to location_extreme, don't re-arm until cooldown expires
        self._location_extreme_cooldown_until: Optional[datetime] = None
        self._location_extreme_cooldown_direction: Optional[str] = None  # 'SHORT' or 'LONG'
        
        # =========================================================================
        # 🔥 FIX #159 (Feb 10, 2026): POST-DISPLACEMENT COOLDOWN
        # 
        # PROBLEMA: Cuando un DISPLACEMENT expira (edge_type=DISPLACEMENT),
        # el sistema crea inmediatamente un nuevo edge (CONSENSUS/TREND) y entra
        # tarde cuando el momentum ya murió.
        # 
        # CASO REAL (Feb 10, 2026):
        #   15:39 - DISPLACEMENT $919 detectado → STALE/CHASE BLOCK
        #   15:45 - LATE DISPLACEMENT: 21 bars > 20 - NO CHASE → edge expira
        #   15:46 - Nuevo TREND_EXPANSION_LOCK edge → permite entrar
        #   15:50 - Entrada SHORT a $68,000 → precio rebota → PIERDE
        #
        # SOLUCIÓN: Cuando un DISPLACEMENT expira, activar cooldown de 10 min
        # para NO crear nuevos edges CONSENSUS en esa dirección.
        # El mercado necesita mostrar NUEVA estructura, no la vieja.
        # =========================================================================
        self._post_displacement_cooldown_until: Optional[datetime] = None
        self._post_displacement_cooldown_direction: Optional[str] = None  # 'SHORT' or 'LONG'
        
        # =========================================================================
        # 🔥 FIX #59 (Feb 06, 2026): Direction staleness tracking
        # 
        # PROBLEMA: Cuando edge expira, _active_direction en trading_app queda
        # "congelada" y puede bloquear nuevas señales ANTICIPATION válidas.
        # 
        # SOLUCIÓN: Marcar cuándo la dirección necesita refresh.
        # trading_app.py debe consultar este flag y resetear _active_direction.
        # =========================================================================
        self._direction_stale_since: Optional[datetime] = None  # Cuando se volvió stale
        self._last_valid_direction: Optional[str] = None  # Última dirección válida
        
        # =========================================================================
        # 🔥 FIX #171 (Feb 11, 2026): SIGNAL AGE TRACKING
        # 
        # PROBLEMA: El sistema VE el movimiento al inicio pero ESPERA.
        # Mientras el precio avanza, el sistema "perfecciona" sus filtros.
        # Cuando todo está "perfecto", el movimiento ya terminó y entra al FINAL.
        # 
        # SOLUCIÓN: Trackear cuándo se activó el edge (EDGE_ACTIVE).
        # - Edge FRESH (< 3 min): Listo para entrar, no penalizar
        # - Edge STALE (> 5 min): Señal vieja, el movimiento probablemente ya pasó
        #   → Aplicar penalty de -1.5 al score para RECHAZAR entradas tardías
        # 
        # Esto hace que el sistema:
        # - Entre MÁS RÁPIDO al inicio del movimiento (antes de acumular chase)
        # - RECHACE entradas cuando el edge lleva demasiado tiempo sin ejecutarse
        # =========================================================================
        self._edge_activated_at: Optional[datetime] = None  # Timestamp de activación
        self._edge_origin_price: Optional[float] = None  # 🔥 FIX #210: Price at edge activation
        
        # =========================================================================
        # 🔥 FIX #184 (Feb 11, 2026): CHASE DETECTION FOR DISPLACEMENT
        # 
        # PROBLEMA: Un edge se arma a precio X, queda bloqueado por CONSENSUS,
        # el precio se mueve 2% en la dirección del edge, luego DISPLACEMENT
        # detecta el movimiento y BYPASSES el bloqueo → entrada CRIMINAL al tope.
        # 
        # SOLUCIÓN: Trackear el precio al que se armó el edge.
        # Cuando DISPLACEMENT intenta armar un nuevo edge:
        # - Si había edge previo en la misma dirección
        # - Y el precio se movió >1.5 ATR en esa dirección desde que se armó
        # - → RECHAZAR el DISPLACEMENT (el movimiento ya pasó)
        # =========================================================================
        self._edge_armed_at_price: Optional[float] = None  # Precio cuando se armó el edge
        self._edge_armed_direction: Optional[str] = None   # Dirección del edge armado
        
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
            timestamp=market_time or datetime.utcnow(),
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
        # 🔥 FIX #59 (Feb 06, 2026): Mark direction as stale on expiry
        # 
        # Cuando el edge expira o se resetea, la dirección asociada se vuelve
        # "stale" (obsoleta). trading_app.py debe consultar is_direction_stale()
        # y considerar resetear _active_direction para permitir nuevas señales.
        # =========================================================================
        if new_state in [EdgeState.EDGE_EXPIRED, EdgeState.NO_EDGE]:
            if direction:  # Solo si había dirección
                self._direction_stale_since = market_time or datetime.utcnow()
                self._last_valid_direction = direction
            # 🔥 FIX #171: Clear activation timestamp on expiry
            self._edge_activated_at = None
            # 🔥 FIX #210: Clear origin price on expiry
            self._edge_origin_price = None
            # 🔥 FIX #184: Clear armed price tracking on expiry
            self._edge_armed_at_price = None
            self._edge_armed_direction = None
        elif new_state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            # Nueva edge activa - dirección ya no es stale
            self._direction_stale_since = None
            # 🔥 FIX #171: Track when edge was activated (always naive UTC)
            if new_state == EdgeState.EDGE_ACTIVE and self._edge_activated_at is None:
                ts = market_time or datetime.utcnow()
                # 🔥 FIX #171b: Force naive UTC to avoid timezone comparison issues
                if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                self._edge_activated_at = ts
                logger.info(f"🎯 FIX #171: Edge activated at {self._edge_activated_at.strftime('%H:%M:%S')}")
            # 🔥 FIX #184: Track price at edge activation for chase detection
            if self._current_bos and self._edge_armed_at_price is None:
                self._edge_armed_at_price = self._current_bos.price
                self._edge_armed_direction = self._current_bos.direction
                # 🔥 FIX #210: Also store origin price for distance-based staleness
                self._edge_origin_price = self._current_bos.price
                logger.debug(f"🔥 FIX #184: Edge armed at price ${self._edge_armed_at_price:.0f} dir={self._edge_armed_direction}")
        
        # Keep history bounded
        if len(self._transition_history) > 100:
            self._transition_history = self._transition_history[-100:]
        
        # Log the transition
        transition.log()
        
        return True
    
    def set_market_context(self, regime: str, adx: float):
        """Update market context for edge type decisions"""
        old_regime = self._current_regime
        self._current_regime = regime
        self._current_adx = adx
        
        # Only log if regime changed
        if old_regime != regime and old_regime != "UNKNOWN":
            edge_type = self._current_bos.edge_type.value if self._current_bos else "NONE"
            direction = self._current_bos.direction if self._current_bos else "-"
            _edge_log(
                self._state.value, edge_type, direction,
                f"Market context: {old_regime}→{regime} (ADX: {adx:.1f})"
            )
    
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
    def current_edge_mode(self) -> EdgeMode:
        """Modo actual: INSTITUTIONAL o INTRADAY"""
        return self._current_edge_mode
    
    @property
    def direction(self) -> Optional[str]:
        """
        🔥 FIX #194 (Feb 11, 2026): Propiedad direction para compatibilidad.
        
        Devuelve la dirección del edge actual (LONG/SHORT) o None si no hay edge.
        Esto evita el AttributeError cuando código externo accede a _edge_manager.direction
        """
        if self._current_bos:
            return self._current_bos.direction
        return None
    
    # =========================================================================
    # 🔥 FIX #59 (Feb 06, 2026): Direction staleness API
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
        
        from datetime import timezone
        now_aware = datetime.now(timezone.utc)
        stale_since = self._direction_stale_since
        # Ensure both are timezone-aware to avoid naive vs aware error
        if stale_since.tzinfo is None:
            stale_since = stale_since.replace(tzinfo=timezone.utc)
        stale_duration = now_aware - stale_since
        return stale_duration > timedelta(minutes=stale_threshold_minutes)
    
    def get_stale_direction_info(self) -> Dict[str, Any]:
        """Retorna info sobre dirección stale para debugging"""
        return {
            'is_stale': self.is_direction_stale(),
            'stale_since': self._direction_stale_since.isoformat() if self._direction_stale_since else None,
            'last_valid_direction': self._last_valid_direction,
            'current_state': self._state.value
        }
    
    def clear_stale_direction(self):
        """Limpia el flag de dirección stale (llamado cuando se toma nueva dirección)"""
        self._direction_stale_since = None
        self._last_valid_direction = None
    
    def is_provisional(self) -> bool:
        """¿Estamos en estado PROVISIONAL (starter position)?"""
        return self._state == EdgeState.EDGE_PROVISIONAL
    
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
    # 🔥 FIX #171: SIGNAL AGE METHODS (Feb 11, 2026)
    # 
    # Permite calcular cuánto tiempo lleva activo el edge para aplicar
    # penalty a entradas tardías ("stale signal penalty")
    # =========================================================================
    
    def get_edge_age_minutes(self, now: Optional[datetime] = None) -> float:
        """
        Calcula cuántos minutos lleva el edge en estado EDGE_ACTIVE.
        
        Returns:
            float: Minutos desde activación, o 0.0 si no está activo
        """
        # 🔥 FIX #171b: Defensive checks FIRST
        try:
            if self._edge_activated_at is None:
                return 0.0
            if self._state not in [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
                return 0.0
            
            # Get current time - always use naive UTC
            current = now or datetime.utcnow()
            
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
        🔥 FIX #210 (Feb 12, 2026): Get the price where edge was activated.
        Used for distance-based staleness calculation.
        """
        return self._edge_origin_price
    
    def refresh_edge_origin(self, new_origin_price: float, market_time: Optional[datetime] = None) -> None:
        """
        🔥 FIX #241 (Feb 15, 2026): Refresh edge origin to a closer BOS level.
        
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
        ts = market_time or datetime.utcnow()
        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        self._edge_activated_at = ts
        
        logger.info(
            f"🔄 FIX #241 EDGE REFRESH: origin ${old_origin:.0f} → ${new_origin_price:.0f} | "
            f"Edge timer reset at {ts.strftime('%H:%M:%S')}"
        )
    
    def get_distance_from_origin(self, current_price: float) -> float:
        """
        🔥 FIX #210 (Feb 12, 2026): Calculate distance from edge origin in absolute terms.
        
        Returns:
            float: Absolute distance in price units, or 0.0 if no origin
        """
        if self._edge_origin_price is None:
            return 0.0
        return abs(current_price - self._edge_origin_price)
    
    def get_distance_from_origin_atr(self, current_price: float, atr: float) -> float:
        """
        🔥 FIX #210 (Feb 12, 2026): Calculate distance from edge origin in ATR units.
        
        This is the CORE metric for distance-based staleness.
        
        Args:
            current_price: Current market price
            atr: Current ATR (1m or whatever timeframe is relevant)
            
        Returns:
            float: Distance in ATR units (e.g., 0.5 = half an ATR)
        """
        if self._edge_origin_price is None or atr <= 0:
            return 0.0
        distance = abs(current_price - self._edge_origin_price)
        return distance / atr
    
    def get_signal_freshness_penalty(
        self, 
        now: Optional[datetime] = None,
        current_price: Optional[float] = None,
        atr: Optional[float] = None
    ) -> float:
        """
        🔥 FIX #210 (Feb 12, 2026): DISTANCE-BASED STALENESS (COMPLETE REWRITE)
        
        PROBLEMA ANTERIOR: Penalty basado en TIEMPO.
        - Un edge que viajó 0.3 ATR en 10 min seguía válido pero recibía -3.0 penalty
        - Un edge que viajó 1.5 ATR en 2 min estaba "gastado" pero no recibía penalty
        
        NUEVA LÓGICA: Penalty basado en DISTANCIA recorrida (ATR units).
        - Si el precio se movió mucho desde el origen → el movimiento ya pasó → STALE
        - Si el precio NO se movió → el setup sigue intacto → FRESH
        
        CURVA DE PENALTY (distance-based, primary):
        - Distancia < 0.3 ATR: FRESH → 0.0 penalty
        - Distancia 0.3-0.7 ATR: WARNING → -0.5 penalty  
        - Distancia 0.7-1.2 ATR: STALE → -1.0 to -2.0 progressive
        - Distancia > 1.2 ATR: EXHAUSTED → -4.0 (move already happened)
        
        FALLBACK TEMPORAL (secondary, si no hay current_price/atr):
        - Usa el viejo sistema basado en minutos
        
        TIME SAFETY NET (applies on top of distance):
        - > 15 min: Add extra -2.0 (even if distance is small, something's wrong)
        
        Args:
            now: Current time (optional)
            current_price: Current market price (optional, enables distance mode)
            atr: Current ATR (optional, enables distance mode)
            
        Returns:
            float: Penalty (negative) or 0.0 if fresh
        """
        # =========================================================================
        # DISTANCE-BASED PENALTY (primary mode when price/atr available)
        # =========================================================================
        if current_price is not None and atr is not None and atr > 0 and self._edge_origin_price is not None:
            distance_atr = abs(current_price - self._edge_origin_price) / atr
            
            # Calculate distance-based penalty
            if distance_atr < 0.3:
                distance_penalty = 0.0  # FRESH: Price hasn't moved much
            elif distance_atr < 0.7:
                distance_penalty = -0.5  # WARNING: Minor movement
            elif distance_atr < 1.2:
                # STALE: Progressive penalty -1.0 to -2.0
                distance_penalty = -1.0 - (distance_atr - 0.7) * 2.0
            else:
                # EXHAUSTED: Move already happened
                distance_penalty = -4.0
            
            # TIME SAFETY NET: If edge is REALLY old, add extra penalty
            age_min = self.get_edge_age_minutes(now)
            time_safety_penalty = 0.0
            if age_min > 15.0:
                time_safety_penalty = -2.0  # Something's wrong if 15+ min without trade
            elif age_min > 12.0:
                time_safety_penalty = -1.0  # Approaching dead zone
            
            total_penalty = distance_penalty + time_safety_penalty
            
            # Log for transparency (rate-limited in caller)
            if total_penalty < -0.5:
                logger.debug(
                    f"🔥 FIX #210: Distance-based penalty | dist={distance_atr:.2f} ATR → {distance_penalty:.1f} | "
                    f"age={age_min:.1f}min → time_safety={time_safety_penalty:.1f} | total={total_penalty:.1f}"
                )
            
            return total_penalty
        
        # =========================================================================
        # FALLBACK: TIME-BASED PENALTY (when no price/atr available)
        # =========================================================================
        age_min = self.get_edge_age_minutes(now)
        
        if age_min < 3.0:
            return 0.0  # FRESH
        elif age_min < 5.0:
            return -0.5  # WARNING
        elif age_min < 8.0:
            # STALE: Progressive penalty -1.0 to -1.75
            return -1.0 - (age_min - 5.0) * 0.25
        elif age_min < 12.0:
            # VERY STALE: Progressive penalty -1.75 to -2.5
            return -1.75 - (age_min - 8.0) * 0.1875
        else:
            # 🔥 FIX #236 (Feb 15, 2026): Capped from -8.0 to -2.5
            # PROBLEM: -8.0 was overkill. With a base score of 8.0, the penalty
            # would result in score 0.0 → guaranteed block. The distance-based
            # penalty (primary path) already caps at -4.0. This fallback should
            # not be more aggressive.
            return -2.5
    
    def is_signal_stale(self, now: Optional[datetime] = None) -> bool:
        """Returns True if the edge has been active for > 5 minutes (penalty zone)"""
        return self.get_edge_age_minutes(now) > 5.0
    
    def is_signal_dead(self, now: Optional[datetime] = None) -> bool:
        """
        🔥 FIX #186b: Returns True if edge is DEAD (> 12 min).
        🔥 FIX #200 (Feb 12): Reduced from 15→12min to match MAX_EDGE_AGE_DEAD.
        A DEAD edge should NEVER be traded, regardless of score.
        """
        return self.get_edge_age_minutes(now) > 12.0
    
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
        
        🔧 FIX #128 (Feb 09, 2026): Added detailed NO_EDGE reason
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
    # 🔥 LOCATION EXTREME COOLDOWN (Feb 05, 2026)
    # Prevents infinite edge re-arm cycle when at price extreme
    # =========================================================================
    
    def set_location_extreme_cooldown(
        self,
        direction: str,
        market_time: datetime,
        cooldown_minutes: int = 1
    ) -> None:
        """
        Set SHORT cooldown to reduce log spam when at location extreme.
        
        🌊 BE WATER MY FRIEND: This is NOT a hard block - just prevents
        the infinite ARM → BLOCK → ARM → BLOCK log spam cycle.
        
        The cooldown is SHORT (1 min) - just enough to:
        - Reduce log spam from every tick to once per minute
        - Let the system "breathe" and wait for price to move
        - NOT prevent legitimate re-evaluation when conditions change
        
        Args:
            direction: 'SHORT' or 'LONG' - the direction that was blocked
            market_time: Current timestamp
            cooldown_minutes: How long to wait (default 1 min - VERY short)
        """
        self._location_extreme_cooldown_until = market_time + timedelta(minutes=cooldown_minutes)
        self._location_extreme_cooldown_direction = direction
        # Only log at DEBUG - this is noise reduction, not a significant event
        logger.debug(
            f"🔒 LOCATION EXTREME: {direction} muted for {cooldown_minutes}min | "
            f"Waiting for price to leave extreme zone"
        )
    
    def is_location_extreme_cooldown_active(self, direction: str, market_time: datetime) -> bool:
        """
        Check if location extreme cooldown is active for given direction.
        
        Returns True if we should NOT arm an edge in this direction.
        """
        if self._location_extreme_cooldown_until is None:
            return False
        if self._location_extreme_cooldown_direction != direction:
            return False
        if market_time >= self._location_extreme_cooldown_until:
            # Cooldown expired - clear it
            self._location_extreme_cooldown_until = None
            self._location_extreme_cooldown_direction = None
            logger.debug(f"🔓 LOCATION EXTREME COOLDOWN expired for {direction}")
            return False
        return True
    
    def clear_location_extreme_cooldown(self, direction: str) -> None:
        """
        Clear location extreme cooldown (called when price leaves extreme zone).
        """
        if self._location_extreme_cooldown_direction == direction:
            self._location_extreme_cooldown_until = None
            self._location_extreme_cooldown_direction = None
            logger.debug(f"🔓 LOCATION EXTREME COOLDOWN manually cleared for {direction}")
    
    # =========================================================================
    # 🔥 FIX #159: POST-DISPLACEMENT COOLDOWN METHODS
    # =========================================================================
    
    def set_post_displacement_cooldown(
        self, 
        direction: str, 
        market_time: datetime,
        cooldown_minutes: int = 10
    ) -> None:
        """
        Set cooldown after DISPLACEMENT edge expires.
        
        🔥 FIX #159 (Feb 10, 2026): Prevents creating new CONSENSUS edge
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
            f"🔥 FIX #159: POST-DISPLACEMENT COOLDOWN | {direction} blocked for {cooldown_minutes}min | "
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
        if market_time >= self._post_displacement_cooldown_until:
            # Cooldown expired - clear it
            self._post_displacement_cooldown_until = None
            self._post_displacement_cooldown_direction = None
            logger.info(f"🔓 POST-DISPLACEMENT COOLDOWN expired for {direction} | New edges allowed")
            return False
        return True
    
    def clear_post_displacement_cooldown(self, direction: str) -> None:
        """
        Clear post-displacement cooldown (e.g., if opposite displacement happens).
        """
        if self._post_displacement_cooldown_direction == direction:
            self._post_displacement_cooldown_until = None
            self._post_displacement_cooldown_direction = None
            logger.debug(f"🔓 POST-DISPLACEMENT COOLDOWN manually cleared for {direction}")

    # =========================================================================
    # 🌊 PROVISIONAL STATE METHODS - Starter positions
    # =========================================================================
    
    def _check_provisional_kill_switch(self, market_time: datetime) -> Tuple[bool, str]:
        """
        🔥 KILL SWITCH: Bloquea PROVISIONAL si hay churn excesivo.
        
        Si el sistema hace PROVISIONAL → EXPIRED → PROVISIONAL repetidamente
        en lateral extremo, estamos pagando fees sin edge real.
        
        Regla: max_provisional_per_hour = 3
        Si se excede → NO_EDGE hard por 30 min
        
        Returns: (is_blocked, reason)
        """
        config = ProvisionalEdgeConfig()
        
        # ¿Estamos en período de bloqueo?
        if self._provisional_blocked_until and market_time < self._provisional_blocked_until:
            remaining = (self._provisional_blocked_until - market_time).seconds // 60
            return True, f"PROVISIONAL blocked (churn), {remaining}min remaining"
        else:
            self._provisional_blocked_until = None
        
        # Reset contador si cambió la hora
        metrics = self._provisional_metrics
        if metrics.last_hour_reset is None or (market_time - metrics.last_hour_reset).seconds >= 3600:
            metrics.provisional_this_hour = 0
            metrics.last_hour_reset = market_time
        
        # ¿Excedimos el límite?
        if metrics.provisional_this_hour >= config.MAX_PROVISIONAL_PER_HOUR:
            # Activar kill switch
            self._provisional_blocked_until = market_time + timedelta(minutes=config.CHURN_COOLDOWN_MIN)
            _edge_log(
                "NO_EDGE", "⚡", "-",
                f"🛑 KILL SWITCH: {metrics.provisional_this_hour} PROVISIONAL/hora > {config.MAX_PROVISIONAL_PER_HOUR} max | "
                f"Blocked {config.CHURN_COOLDOWN_MIN}min",
                level="warning"
            )
            return True, f"PROVISIONAL churn limit ({config.MAX_PROVISIONAL_PER_HOUR}/hr) exceeded"
        
        return False, "OK"
    
    def arm_provisional(
        self,
        direction: str,
        edge_type: EdgeType,
        reason: str,
        current_price: float,
        atr: float,
        market_time: datetime
    ) -> Tuple[bool, str]:
        """
        Armar un edge PROVISIONAL - señal válida pero incompleta.
        
        Se usa cuando:
        - timing=0 pero resto OK
        - R < 0.30 sin liquidity pero >= 0.25
        - 3 de 5 criterios cumplidos (no 5 de 5)
        
        🔥 KILL SWITCH: Si hay churn excesivo (>3/hora), bloquea PROVISIONAL.
        🔥 MUTUAL EXCLUSION: PROVISIONAL INTRADAY bloquea RANGE (correlación oculta).
        
        Returns: (success, reason)
        """
        # 🔥 Check kill switch PRIMERO
        is_blocked, block_reason = self._check_provisional_kill_switch(market_time)
        if is_blocked:
            return False, block_reason
        
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False, "Edge already armed/active"
        
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)
        
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,
            bos_level=current_price,
            atr_at_bos=atr,
            edge_type=edge_type
        )
        
        # 🔥 Actualizar métricas ANTES de transición
        self._provisional_metrics.total_provisional += 1
        self._provisional_metrics.provisional_this_hour += 1
        self._provisional_start_time = market_time
        
        # Transición a PROVISIONAL
        self._transition_to(
            EdgeState.EDGE_PROVISIONAL,
            reason,
            market_time
        )
        return True, "PROVISIONAL armed"
    
    def upgrade_provisional_to_active(
        self,
        reason: str,
        market_time: datetime
    ) -> bool:
        """
        Upgrade de PROVISIONAL → ACTIVE cuando se confirma el setup.
        
        Se activa cuando:
        - Precio en profit (> +0.2R)
        - Se confirma liquidity event
        - timing mejora de 0 a >0
        
        Returns: True si se hizo upgrade
        """
        if self._state != EdgeState.EDGE_PROVISIONAL:
            return False
        
        # 🔥 Track métricas del upgrade
        self._provisional_metrics.upgraded_to_active += 1
        if self._provisional_start_time:
            duration = (market_time - self._provisional_start_time).seconds
            self._provisional_metrics.total_time_in_provisional_sec += duration
        
        if self._current_bos:
            self._current_bos.pullback_detected_at = market_time
        
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"upgrade: {reason}",
            market_time
        )
        return True
    
    def expire_provisional(
        self,
        reason: str,
        pnl_r: float,
        market_time: datetime
    ):
        """
        Expira un PROVISIONAL sin upgrade.
        
        🔥 Trackea métricas para auditoría.
        """
        if self._state != EdgeState.EDGE_PROVISIONAL:
            return
        
        # Track métricas
        self._provisional_metrics.expired_without_upgrade += 1
        self._provisional_metrics.provisional_only_pnl_r += pnl_r
        self._provisional_metrics.total_pnl_r += pnl_r
        
        if self._provisional_start_time:
            duration = (market_time - self._provisional_start_time).seconds
            self._provisional_metrics.total_time_in_provisional_sec += duration
        
        # Transición a EXPIRED
        self._transition_to(EdgeState.EDGE_EXPIRED, reason, market_time)
    
    def get_provisional_metrics(self) -> ProvisionalMetrics:
        """Retorna métricas de PROVISIONAL para auditoría"""
        return self._provisional_metrics
    
    # =========================================================================
    # 🔥 CONTEXT + DIRECTION COOLDOWN - Evita revenge trading algorítmico
    # =========================================================================
    
    def set_direction_cooldown(
        self,
        edge_type: EdgeType,
        direction: str,
        market_time: datetime,
        cooldown_minutes: Optional[int] = None
    ):
        """
        Activa cooldown por contexto + dirección tras pérdida.
        
        Ejemplo: BREAKOUT LONG falla → bloquear TREND LONG por X minutos.
        Esto evita revenge trading algorítmico (mismo bias, mismo error).
        
        Args:
            edge_type: Tipo de edge que falló
            direction: LONG o SHORT
            market_time: Hora actual
            cooldown_minutes: Override del cooldown (default: DIRECTION_COOLDOWN_AFTER_LOSS_MIN)
        """
        cooldown_min = cooldown_minutes or self.DIRECTION_COOLDOWN_AFTER_LOSS_MIN
        key = f"{edge_type.value}_{direction}"
        self._direction_cooldown[key] = market_time + timedelta(minutes=cooldown_min)
        
        _edge_log(
            self._state.value,
            edge_type.value,
            direction,
            f"⚠️ DIRECTION COOLDOWN: {key} blocked {cooldown_min}min (loss protection)",
            level="warning"
        )
        
        # 🔥 También bloquear contextos relacionados (BREAKOUT falla → TREND LONG también)
        # BREAKOUT y TREND comparten dirección/contexto
        if edge_type == EdgeType.BREAKOUT:
            trend_key = f"{EdgeType.TREND.value}_{direction}"
            self._direction_cooldown[trend_key] = market_time + timedelta(minutes=cooldown_min)
            logger.debug(f"[COOLDOWN_PROPAGATE] {edge_type.value} falla → {trend_key} también en cooldown")
        
        # TREND falla → bloquear también dirección en BREAKOUT
        elif edge_type == EdgeType.TREND:
            breakout_key = f"{EdgeType.BREAKOUT.value}_{direction}"
            self._direction_cooldown[breakout_key] = market_time + timedelta(minutes=cooldown_min)
            logger.debug(f"[COOLDOWN_PROPAGATE] {edge_type.value} falla → {breakout_key} también en cooldown")
    
    def is_direction_in_cooldown(
        self,
        edge_type: EdgeType,
        direction: str,
        market_time: datetime
    ) -> Tuple[bool, Optional[int]]:
        """
        ¿Está esta combinación edge_type+direction en cooldown?
        
        Returns: (is_in_cooldown, minutes_remaining)
        """
        key = f"{edge_type.value}_{direction}"
        cooldown_until = self._direction_cooldown.get(key)
        
        if cooldown_until is None:
            # 🔥 FIX #194c: También verificar cooldown simple por dirección
            # (usado cuando DEAD_EDGE expira)
            cooldown_until = self._direction_cooldown.get(direction)
        
        if cooldown_until is None:
            return False, None
        
        if market_time >= cooldown_until:
            # Cooldown expirado, limpiar
            if key in self._direction_cooldown:
                del self._direction_cooldown[key]
            if direction in self._direction_cooldown:
                del self._direction_cooldown[direction]
            return False, None
        
        remaining = int((cooldown_until - market_time).seconds / 60)
        return True, remaining
    
    def is_simple_direction_cooldown_active(
        self,
        direction: str,
        market_time: datetime
    ) -> Tuple[bool, Optional[int]]:
        """
        🔥 FIX #194c (Feb 11): Verifica cooldown simple por dirección.
        
        Este cooldown se activa cuando un edge expira por DEAD_EDGE (>15 min).
        Bloquea TODOS los edge types en esa dirección temporalmente.
        
        Returns: (is_active, minutes_remaining)
        """
        cooldown_until = self._direction_cooldown.get(direction)
        
        if cooldown_until is None:
            return False, None
        
        if market_time >= cooldown_until:
            del self._direction_cooldown[direction]
            return False, None
        
        remaining = int((cooldown_until - market_time).seconds / 60)
        return True, remaining
    
    def record_trade_loss(
        self,
        edge_type: EdgeType,
        direction: str,
        pnl_r: float,
        market_time: datetime
    ):
        """
        Registra una pérdida y activa cooldown por dirección si aplica.
        
        🔥 Esto es lo que evita revenge trading algorítmico:
        - BREAKOUT LONG falla con -1R
        - Sistema intenta TREND LONG en misma zona
        - = Misma información, misma dirección → pérdida correlacionada
        
        Con cooldown: TREND LONG bloqueado X minutos
        """
        if pnl_r >= 0:
            return  # No es pérdida
        
        # Activar cooldown por dirección
        self.set_direction_cooldown(edge_type, direction, market_time)
        
        # Track en métricas si es PROVISIONAL
        if self._state == EdgeState.EDGE_PROVISIONAL:
            self._provisional_metrics.total_pnl_r += pnl_r
            self._provisional_metrics.provisional_only_pnl_r += pnl_r
    
    def clear_direction_cooldowns(self):
        """Limpia todos los cooldowns (para testing o reset)"""
        self._direction_cooldown.clear()
    
    def get_active_cooldowns(self, market_time: datetime) -> Dict[str, int]:
        """
        Retorna cooldowns activos con minutos restantes.
        
        Útil para debugging y status.
        """
        active = {}
        for key, until in list(self._direction_cooldown.items()):
            if market_time < until:
                remaining = int((until - market_time).seconds / 60)
                active[key] = remaining
            else:
                del self._direction_cooldown[key]
        return active

    def get_provisional_config(self) -> ProvisionalEdgeConfig:
        """Retorna la configuración de PROVISIONAL para sizing/TP"""
        return ProvisionalEdgeConfig()
    
    # =========================================================================
    # ⚠️ DEPRECATED (Feb 05, 2026): INTRADAY absorbed into TREND with CONSENSUS path
    # This method is preserved for backwards compatibility but is not called
    # =========================================================================
    
    def check_intraday_base_edge(
        self,
        direction: str,
        adx_5m: float,
        estimated_r: float,
        sl_r: float,
        current_price: float,
        atr: float,
        market_time: datetime,
        timing_score: int = 0,
        liquidity_event: Optional[LiquidityEvent] = None
    ) -> Tuple[bool, str, float]:
        """
        ⚠️ DEPRECATED (Feb 05, 2026): This method is never called.
        INTRADAY mode has been absorbed into TREND with CONSENSUS_DIRECT path.
        
        Criterios simples:
        - ADX_5m >= 25 (bias claro)
        - SL técnico <= 0.5R
        - R estimado >= 0.20
        
        Flujo:
        - Si cumple base → PROVISIONAL (entrada barata para testear)
        - Si tiene LiquidityEvent → puede ir directo a ACTIVE
        
        Args:
            direction: 'LONG' o 'SHORT'
            adx_5m: ADX del timeframe 5 minutos
            estimated_r: R estimado del trade
            sl_r: SL en R
            current_price: Precio actual
            atr: ATR actual
            market_time: Hora del mercado
            timing_score: Score de timing (0-3)
            liquidity_event: Evento de liquidez detectado (opcional, BOOST)
        
        Returns:
            (success, reason, size_multiplier)
        """
        cfg_prov = ProvisionalEdgeConfig()
        cfg_intra = IntradayEdgeCriteria()
        
        # =====================================================================
        # CRITERIOS BASE (simple, no perfeccionista)
        # =====================================================================
        
        reasons_fail = []
        reasons_pass = []  # Track what passed
        size_mult = cfg_prov.SIZE_MULTIPLIER
        
        # 1. ADX 5m >= 25 (bias claro)
        if adx_5m < cfg_intra.BASE_ADX_5M_MIN:
            reasons_fail.append(f"ADX_5m={adx_5m:.1f}<{cfg_intra.BASE_ADX_5M_MIN}")
        else:
            reasons_pass.append(f"ADX_5m={adx_5m:.1f}✓")
            if adx_5m > 35:
                # ADX alto = más convicción → más size
                size_mult = cfg_prov.SIZE_MULTIPLIER_HIGH_ADX
        
        # 2. SL técnico <= 0.5R
        if sl_r > cfg_intra.BASE_SL_R_MAX:
            reasons_fail.append(f"SL_R={sl_r:.2f}>{cfg_intra.BASE_SL_R_MAX}")
        else:
            reasons_pass.append(f"SL_R={sl_r:.2f}✓")
        
        # 3. R estimado >= 0.20
        if estimated_r < cfg_prov.MIN_R:
            reasons_fail.append(f"R={estimated_r:.2f}<{cfg_prov.MIN_R}")
        else:
            reasons_pass.append(f"R={estimated_r:.2f}✓")
        
        # =====================================================================
        # Check cooldown por dirección
        # =====================================================================
        in_cooldown, remaining = self.is_direction_in_cooldown(
            EdgeType.INTRADAY, direction, market_time
        )
        if in_cooldown:
            reasons_fail.append(f"INTRADAY_{direction} cooldown {remaining}min")
        
        # =====================================================================
        # LOGGING: Show what passed and what failed
        # =====================================================================
        if reasons_fail:
            logger.info(
                f"❌ INTRADAY_BASE_EDGE [{direction}] FAIL: {', '.join(reasons_fail)} | "
                f"Passed: {', '.join(reasons_pass) if reasons_pass else 'none'}"
            )
            reason = f"BASE_EDGE_FAIL: {', '.join(reasons_fail)}"
            return False, reason, 0.0
        
        # Log success
        logger.info(
            f"✅ INTRADAY_BASE_EDGE [{direction}] PASS: {', '.join(reasons_pass)} | "
            f"timing={timing_score} | price=${current_price:.2f}"
        )
        
        # =====================================================================
        # 🔥 LIQUIDITY EVENT = BOOST (no gate)
        # =====================================================================
        direct_to_active = False
        if liquidity_event and liquidity_event.is_valid():
            # LiquidityEvent presente → boost de size
            size_mult = cfg_intra.SIZE_WITH_LIQUIDITY_BOOST
            
            # Si R >= 0.35 y timing >= 1 y LiquidityEvent → ACTIVE directo
            if estimated_r >= 0.35 and timing_score >= 1:
                direct_to_active = True
        
        # =====================================================================
        # ARMAR EDGE
        # =====================================================================
        
        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)
        
        # Si ya hay edge activo, no crear nuevo
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False, "Edge already active", 0.0
        
        # Crear BOSEvent para el edge
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,
            bos_level=current_price,
            atr_at_bos=atr,
            edge_type=EdgeType.INTRADAY
        )
        
        if direct_to_active and liquidity_event is not None:
            # ACTIVE directo (con LiquidityEvent + R >= 0.35 + timing >= 1)
            self._transition_to(
                EdgeState.EDGE_ACTIVE,
                f"INTRADAY_BASE direct: ADX={adx_5m:.0f} R={estimated_r:.2f} +LiqEvent",
                market_time
            )
            reason = f"ACTIVE_DIRECT: ADX={adx_5m:.0f} R={estimated_r:.2f} LiqEvent={liquidity_event.event_type.value}"
        else:
            # PROVISIONAL (entrada barata para testear)
            is_blocked, block_reason = self._check_provisional_kill_switch(market_time)
            if is_blocked:
                return False, block_reason, 0.0
            
            self._provisional_metrics.total_provisional += 1
            self._provisional_metrics.provisional_this_hour += 1
            self._provisional_start_time = market_time
            
            self._transition_to(
                EdgeState.EDGE_PROVISIONAL,
                f"INTRADAY_BASE: ADX={adx_5m:.0f} R={estimated_r:.2f} timing={timing_score}",
                market_time
            )
            reason = f"PROVISIONAL: ADX={adx_5m:.0f} R={estimated_r:.2f} timing={timing_score}"
        
        return True, reason, size_mult
    
    def should_upgrade_provisional(
        self,
        current_pnl_r: float,
        timing_score: int,
        initial_timing: int = 0,
        liquidity_event: Optional[LiquidityEvent] = None
    ) -> Tuple[bool, str]:
        """
        🔥 ¿Debemos upgrader PROVISIONAL → ACTIVE?
        
        Criterios SIMPLES (no perfeccionistas):
        - Precio a favor >= 0.15R
        - O timing mejora de 0 a >=1
        - O aparece LiquidityEvent válido
        
        NO se necesitan todos, con UNO basta.
        
        Returns:
            (should_upgrade, reason)
        """
        if self._state != EdgeState.EDGE_PROVISIONAL:
            return False, "Not in PROVISIONAL state"
        
        cfg = ProvisionalEdgeConfig()
        reasons_upgrade = []
        
        # 1. Precio a favor >= 0.15R
        if current_pnl_r >= cfg.UPGRADE_MIN_PROFIT_R:
            reasons_upgrade.append(f"profit +{current_pnl_r:.2f}R")
        
        # 2. Timing mejora
        if cfg.UPGRADE_ON_TIMING_IMPROVE and timing_score > initial_timing and timing_score >= 1:
            reasons_upgrade.append(f"timing {initial_timing}→{timing_score}")
        
        # 3. LiquidityEvent aparece
        if liquidity_event and liquidity_event.is_valid():
            reasons_upgrade.append(f"LiqEvent={liquidity_event.event_type.value}")
        
        if reasons_upgrade:
            return True, " + ".join(reasons_upgrade)
        
        return False, "No upgrade criteria met"
    
    # =========================================================================
    # RANGE EDGE DETECTION - CON SWEEP + REJECTION
    # =========================================================================
    
    # 🔥 AJUSTE (Jan 28): Compresión extrema NO bloquea PROBE
    # Antes: COMPRESSION_DISABLES_RANGE_PCT = 5 → bloqueaba PROBE
    # Ahora: Dejamos que ProbeValidator decida rentabilidad en compresión
    # 🔥 FIX #83 (Feb 07): Usar -1 para deshabilitar completamente
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
        
        # =================================================================
        # ⚠️ DEPRECATED (Feb 05, 2026): INTRADAY no longer used, this check is dead code
        # =================================================================
        # if self._state == EdgeState.EDGE_PROVISIONAL and self._current_bos:
        #     if self._current_bos.edge_type == EdgeType.INTRADAY:
        #         logger.debug(...)
        #         return False
        
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
        structure_level: float = 0.0  # 🔥 FIX #29: Structure level for trigger
    ) -> bool:
        """
        Detecta edge de BREAKOUT (compresión).
        
        Llamado cuando Anticipation detecta COMPRESSION_BREAKOUT.
        Arma el edge, esperando confirmación de ruptura.
        
        🔥 FIX #29 (Feb 04): structure_level is the "Building X from Y" level.
        When price breaks this level, the edge is confirmed.
        
        🔥 FIX #31 (Feb 05): BREAKOUT tiene PRIORIDAD sobre TREND edge.
        Si hay un TREND edge activo, BREAKOUT puede OVERRIDE porque tiene
        mayor convicción (compresión + anticipation vs solo consensus).
        
        Returns: True si se armó edge
        """
        # Check context cooldown
        if self.is_context_in_cooldown(EdgeType.BREAKOUT, market_time):
            return False
        
        # =====================================================================
        # 🔥 FIX #31 (Feb 05): BREAKOUT OVERRIDE LOGIC
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
        
        # 🔥 FIX #29: More relaxed criteria when we have structure_level
        # With structure level, we already have a clear breakout level to watch
        has_structure = structure_level > 0
        
        # Verificar criterios de compresión
        is_squeezed = bb_width_percentile <= BreakoutEdgeCriteria.BB_WIDTH_PERCENTILE_MAX
        vol_declining = volume_ratio <= BreakoutEdgeCriteria.VOLUME_DECLINE_RATIO
        
        # 🔥 FIX #29: Allow edge with structure_level even if volume not declining
        criteria_met = (is_squeezed and vol_declining) or (has_structure and is_squeezed)
        
        if criteria_met:
            # 🔥 FIX #29: Use structure_level as bos_level if available
            bos_level = structure_level if has_structure else current_price
            
            # Armar edge de breakout (esperar confirmación)
            self._current_bos = BOSEvent(
                timestamp=market_time,
                price=current_price,
                direction=direction,
                htf_bias=direction,
                bos_level=bos_level,  # 🔥 FIX #29: Structure level as trigger
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
        
        🔥 FIX #29: ANTICIPATION EDGE TRIGGER
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
        
        # 🔥 FIX #29: PRIMARY TRIGGER - Structure level break
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
    # BREAKOUT INVALIDATION (logical, not SL)
    # =========================================================================
    
    def check_breakout_invalidation(
        self,
        current_price: float,
        close_price: float,
        bb_upper: float,
        bb_lower: float,
        atr: float
    ) -> tuple[bool, str]:
        """
        Verifica si el breakout se ha invalidado LÓGICAMENTE.
        
        Diferencia: SL protege capital, invalidación protege expectancy.
        
        🔥 FIX #29: ANTICIPATION EDGE INVALIDATION
        Invalidación:
        1. Price moves against direction by > 0.3 ATR from structure_level
        2. Close de vuelta DENTRO de BB (fake breakout) - fallback
        3. Pérdida del nivel de breakout + buffer ATR - fallback
        
        Returns: (invalidated, reason)
        """
        if self._state != EdgeState.EDGE_ACTIVE:
            return False, ""
        
        if self._current_bos is None or self._current_bos.edge_type != EdgeType.BREAKOUT:
            return False, ""
        
        bos = self._current_bos
        direction = bos.direction
        
        # =================================================================
        # 🔥 FIX #29: ANTICIPATION INVALIDATION - Structure level reversal
        # If edge was armed via structure_level, invalidate if price reverses
        # =================================================================
        structure_level = bos.bos_level
        squeeze_level = bos.price
        has_structure = abs(structure_level - squeeze_level) > 0.3 * atr
        
        if has_structure:
            # Edge was armed with ANTICIPATION structure_level
            # Invalidate if price reverses past structure_level
            if direction == 'SHORT' and current_price > structure_level + 0.3 * atr:
                return True, f"ANTICIPATION INVALID: Price ${current_price:.0f} > structure ${structure_level:.0f} + 0.3ATR"
            elif direction == 'LONG' and current_price < structure_level - 0.3 * atr:
                return True, f"ANTICIPATION INVALID: Price ${current_price:.0f} < structure ${structure_level:.0f} - 0.3ATR"
        
        # =================================================================
        # INVALIDACIÓN 1: Close de vuelta dentro de BB
        # =================================================================
        if BreakoutEdgeCriteria.INVALIDATION_CLOSE_INSIDE_BB:
            if bb_upper > 0 and bb_lower > 0 and close_price > 0:
                if direction == 'LONG' and close_price < bb_upper:
                    # Close ya no está fuera de BB superior
                    return True, f"INVALIDATION: Close {close_price:.2f} < BB_upper {bb_upper:.2f} (vuelta dentro)"
                elif direction == 'SHORT' and close_price > bb_lower:
                    return True, f"INVALIDATION: Close {close_price:.2f} > BB_lower {bb_lower:.2f} (vuelta dentro)"
        
        # =================================================================
        # INVALIDACIÓN 2: Pérdida del nivel de breakout
        # =================================================================
        breakout_level = bos.breakout_confirmation_price or bos.price
        buffer = BreakoutEdgeCriteria.INVALIDATION_LOSS_OF_BREAKOUT_LEVEL_ATR * atr
        
        if direction == 'LONG':
            invalidation_level = breakout_level - buffer
            if current_price < invalidation_level:
                return True, f"INVALIDATION: Precio {current_price:.2f} < nivel breakout {breakout_level:.2f} - {buffer:.2f}"
        else:
            invalidation_level = breakout_level + buffer
            if current_price > invalidation_level:
                return True, f"INVALIDATION: Precio {current_price:.2f} > nivel breakout {breakout_level:.2f} + {buffer:.2f}"
        
        return False, ""
    
    # =========================================================================
    # 🌊 DISPLACEMENT EDGE DETECTION (Jan 30, 2026)
    # 
    # PROBLEMA RESUELTO:
    # - Sistema detectaba EXPANSION_MODE + DIRECTION_LOCK correctamente
    # - Pero NO se armaba EDGE → 0 trades en caída de 2.3%
    # - RANGE_EDGE solo permite LONG en pct bajo, SHORT en pct alto
    # - TREND_EDGE necesita BOS + pullback (no había BOS)
    # 
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
        
        🔥 FIX (Feb 4, 2026): STALE DISPLACEMENT BLOCKING
        - Si is_stale=True, NO armar edge (momentum exhausted)
        - STALE = precio cerca del extremo reciente (< 0.5 ATR)
        
        Args:
            is_stale: True si el displacement está agotado (precio cerca del extremo)
            stale_distance: Distancia al extremo (para logging)
        
        Returns: True si se armó el edge
        """
        # 🔥 FIX (Feb 4, 2026): STALE CHECK FIRST
        # If displacement is stale (price near extreme), do NOT arm edge
        if is_stale:
            logger.warning(
                f"🚫 DISPLACEMENT EDGE BLOCKED (STALE): {direction} | "
                f"Price only ${stale_distance:.0f} from extreme (< 0.5 ATR) | "
                f"Momentum exhausted → NOT arming"
            )
            return False
        
        # =====================================================================
        # 🔥 FIX #184 (Feb 11, 2026): CHASE-AFTER-BLOCK DETECTION
        # 
        # PROBLEMA: Un edge se arma a precio X, queda bloqueado por CONSENSUS
        # durante 30+ minutos mientras el precio sube 2%. Luego DISPLACEMENT
        # detecta el movimiento y BYPASSES el bloqueo → entrada CRIMINAL al tope.
        # 
        # SOLUCIÓN: Si había un edge previo en la misma dirección y el precio
        # se ha movido >1.5 ATR en esa dirección desde que se armó el edge,
        # RECHAZAR el DISPLACEMENT porque el movimiento ya pasó.
        # 
        # Ejemplo del log:
        # - 14:32:05: Edge LONG armado a $67,218
        # - 15:11:03: Precio en $68,507, DISPLACEMENT intenta armar
        # - Movimiento: $68,507 - $67,218 = $1,289 > 1.5 ATR (~$200)
        # - → RECHAZAR: El movimiento de 2% ya terminó
        # =====================================================================
        CHASE_THRESHOLD_ATR_MULT = 1.5  # Movimiento > 1.5 ATR = chase peligroso
        
        # 🔥 FIX #185b: Diagnostic log for FIX #184
        logger.info(
            f"🔍 FIX #184 CHECK: armed_price={self._edge_armed_at_price}, armed_dir={self._edge_armed_direction}, "
            f"current={current_price:.0f}, dir={direction}, atr={atr:.0f}"
        )
        
        if self._edge_armed_at_price is not None and atr > 0:
            chase_threshold = atr * CHASE_THRESHOLD_ATR_MULT
            
            if direction == 'LONG' and self._edge_armed_direction == 'LONG':
                # Para LONG: chase si el precio subió significativamente desde el armado
                price_move_since_armed = current_price - self._edge_armed_at_price
                if price_move_since_armed > chase_threshold:
                    edge_age_min = self.get_edge_age_minutes(market_time)
                    logger.warning(
                        f"🚫 FIX #184 CHASE-AFTER-BLOCK: LONG displacement BLOCKED | "
                        f"Price moved ${price_move_since_armed:.0f} since edge armed at ${self._edge_armed_at_price:.0f} "
                        f"(> {chase_threshold:.0f} = 1.5 ATR) | Edge age: {edge_age_min:.1f}min | "
                        f"Movement already happened while blocked → NOT arming"
                    )
                    return False
                    
            elif direction == 'SHORT' and self._edge_armed_direction == 'SHORT':
                # Para SHORT: chase si el precio bajó significativamente desde el armado
                price_move_since_armed = self._edge_armed_at_price - current_price
                if price_move_since_armed > chase_threshold:
                    edge_age_min = self.get_edge_age_minutes(market_time)
                    logger.warning(
                        f"🚫 FIX #184 CHASE-AFTER-BLOCK: SHORT displacement BLOCKED | "
                        f"Price moved ${price_move_since_armed:.0f} since edge armed at ${self._edge_armed_at_price:.0f} "
                        f"(> {chase_threshold:.0f} = 1.5 ATR) | Edge age: {edge_age_min:.1f}min | "
                        f"Movement already happened while blocked → NOT arming"
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
        # 
        # FIX (Feb 1, 2026): Expirar edge después de KILL threshold
        # SIEMPRE, no solo si trades_attempted == 0.
        # Si ya hubo 1+ trade y estamos >5 barras, el edge está muerto.
        #
        # FIX (Feb 3, 2026): Si entry_window_open = False, el edge está funcionalmente
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
    
    def get_displacement_entry_size_multiplier(self) -> float:
        """
        Retorna el multiplicador de size para entrada en DISPLACEMENT.
        
        🔥 ZONAS DE ENTRADA (Jan 30, 2026):
        - ZONA 1 (bars 0-1): EARLY IMPULSE - Entry directa FAST PATH, size 0.25-0.40
        - ZONA 2 (bars 2-5): PULLBACK - Entry en pullback, size 0.40-0.60
        - ZONA 3 (bars >5):  CHASE BLOCK - NO TRADE (return 0.0)
        """
        if self._current_bos is None or self._current_bos.edge_type != EdgeType.DISPLACEMENT:
            return 1.0
        
        bos = self._current_bos
        bars = bos.bars_since_displacement
        
        # =========================================================================
        # ZONA 3 - CHASE BLOCK (bars > 5): NO TRADE
        # =========================================================================
        if bars > DisplacementEdgeCriteria.CHASE_BLOCK_AFTER_BARS:
            logger.debug(f"⛔ ZONA 3 (CHASE BLOCK): bars={bars} > {DisplacementEdgeCriteria.CHASE_BLOCK_AFTER_BARS} - NO TRADE")
            return 0.0  # Kill the trade
        
        # =========================================================================
        # ZONA 1 - EARLY IMPULSE (bars 0-1): Entry DIRECTA FAST PATH
        # =========================================================================
        if bars <= DisplacementEdgeCriteria.EARLY_IMPULSE_MAX_BARS:
            # Escalar size: bars=0 → 0.40 (max), bars=2 → 0.25 (min)
            # Linear interpolation
            t = bars / max(DisplacementEdgeCriteria.EARLY_IMPULSE_MAX_BARS, 1)
            size = DisplacementEdgeCriteria.EARLY_IMPULSE_SIZE_MAX - (
                (DisplacementEdgeCriteria.EARLY_IMPULSE_SIZE_MAX - DisplacementEdgeCriteria.EARLY_IMPULSE_SIZE_MIN) * t
            )
            logger.debug(f"🔥 ZONA 1 (EARLY IMPULSE): bars={bars}, size={size:.2f}")
            return size
        
        # =========================================================================
        # ZONA 2 - PULLBACK ENTRY (bars 2-5)
        # =========================================================================
        if bars <= DisplacementEdgeCriteria.PULLBACK_ENTRY_MAX_BARS:
            # Escalar size: bars=2 → 0.60 (max con pullback), bars=5 → 0.40 (min)
            bars_in_zone = bars - DisplacementEdgeCriteria.EARLY_IMPULSE_MAX_BARS
            max_bars_in_zone = DisplacementEdgeCriteria.PULLBACK_ENTRY_MAX_BARS - DisplacementEdgeCriteria.EARLY_IMPULSE_MAX_BARS
            t = bars_in_zone / max(max_bars_in_zone, 1)
            size = DisplacementEdgeCriteria.PULLBACK_ENTRY_SIZE_MAX - (
                (DisplacementEdgeCriteria.PULLBACK_ENTRY_SIZE_MAX - DisplacementEdgeCriteria.PULLBACK_ENTRY_SIZE_MIN) * t
            )
            logger.debug(f"⏳ ZONA 2 (PULLBACK): bars={bars}, size={size:.2f}")
            return size
        
        # Fallback - no debería llegar aquí
        logger.warning(f"⚠️ ZONA UNDEFINED: bars={bars} - NO TRADE")
        return 0.0
    
    def check_displacement_invalidation(
        self,
        current_price: float,
        initial_displacement: float,
        initial_direction: str,
        atr: float
    ) -> tuple[bool, str]:
        """
        Verifica si el DISPLACEMENT_EDGE se ha invalidado.
        
        Invalidación (MUY difícil en displacement real):
        - Movimiento opuesto ≥60% del displacement inicial
        
        En displacement real, el mercado no retrocede tanto.
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False, ""
        
        if self._current_bos is None or self._current_bos.edge_type != EdgeType.DISPLACEMENT:
            return False, ""
        
        bos = self._current_bos
        invalidation_threshold = initial_displacement * DisplacementEdgeCriteria.INVALIDATION_OPPOSITE_DISPLACEMENT_MULT
        
        if bos.direction == 'LONG':
            # En LONG displacement, si cae mucho = invalidación
            if current_price < bos.bos_level:
                return True, f"DISPLACEMENT INVALIDATED: Price {current_price:.0f} < level {bos.bos_level:.0f}"
        else:
            # En SHORT displacement, si sube mucho = invalidación
            if current_price > bos.bos_level:
                return True, f"DISPLACEMENT INVALIDATED: Price {current_price:.0f} > level {bos.bos_level:.0f}"
        
        return False, ""
    
    # =========================================================================
    # 🔥 CONSENSUS EDGE (Feb 05, 2026)
    # Cuando hay consenso entre structural y active direction en TREND mode,
    # creamos un edge directamente sin necesitar BOS.
    # 
    # FILOSOFÍA: Si dos fuentes independientes (HTF structure + momentum)
    # coinciden en dirección, hay suficiente evidencia para tradear.
    #
    # 🔥 FIX #31 (Feb 05, 2026): CONSENSUS es el fallback de menor prioridad
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
        forbidden_direction: str | None = None  # 🔥 FIX #191
    ) -> bool:
        """
        Arma un CONSENSUS_EDGE cuando structural == active en TREND mode.
        
        🔥 FIX #31 (Feb 05): CONSENSUS tiene la MENOR prioridad (20).
        🔥 FIX #78 (Feb 07): MACRO BIAS COHERENCE CHECK
        🔥 FIX #191 (Feb 11): FORBIDDEN DIRECTION CHECK
        
        Este edge NO requiere:
        - BOS (el consenso ES la señal)
        - Sweep/rejection (no es mean reversion)
        - Compresión (no es breakout)
        
        REQUIERE (FIX #78):
        - structural_bias == active_direction
        - ADX >= 20 (confirmado por trading_mode='TREND')
        - macro_bias_strength coherente con direction:
          * BEAR_STRONG + LONG = INCOHERENTE → BLOCKED (unless momentum_override)
          * BULL_STRONG + SHORT = INCOHERENTE → BLOCKED (unless momentum_override)
        
        🔥 FIX #191: NO puede armar edge si direction == forbidden_direction
        Sizing: 0.40-0.60x (menor que BOS_PULLBACK por ser entrada directa)
        
        Returns: True si se armó el edge
        """
        # =====================================================================
        # 🔥 FIX #191 (Feb 11, 2026): FORBIDDEN DIRECTION CHECK
        #
        # PROBLEMA: El log mostró que cuando hay BIAS FLIP (ej: "FORBID SHORT,
        # LONG detection ENABLED"), el sistema seguía creando edges SHORT porque
        # arm_consensus_edge() no verificaba forbidden_direction.
        #
        # SOLUCIÓN: Si direction == forbidden_direction, BLOQUEAR el edge.
        # Esto es PRIORITARIO sobre todo lo demás (incluso momentum override).
        # =====================================================================
        if forbidden_direction and direction == forbidden_direction:
            logger.warning(
                f"🚫 FIX #191: CONSENSUS BLOCKED by FORBIDDEN_DIRECTION | "
                f"Cannot create {direction} edge while {direction} is forbidden | "
                f"reason={reason}"
            )
            return False
        
        # =====================================================================
        # 🔥 FIX #78: MACRO BIAS COHERENCE CHECK
        # 
        # PROBLEMA: El log mostró TREND_CONSENSUS LONG con macro=BEAR_STRONG
        # Eso es INCOHERENTE. No deberíamos armar un LONG consensus en BEAR_STRONG.
        # 
        # EXCEPCIÓN: Momentum override (DISPLACEMENT, EXPANSION) puede ignorar esto
        # porque el price action ha demostrado que el macro se está rompiendo.
        # =====================================================================
        if macro_bias_strength and not has_momentum_override:
            is_incoherent = (
                (macro_bias_strength == 'BEAR_STRONG' and direction == 'LONG') or
                (macro_bias_strength == 'BULL_STRONG' and direction == 'SHORT')
            )
            if is_incoherent:
                logger.warning(
                    f"🚫 CONSENSUS BLOCKED: {direction} incoherent with macro={macro_bias_strength} | "
                    f"reason={reason} | momentum_override={has_momentum_override}"
                )
                return False
        # =====================================================================
        # 🔥 FIX #31: CONSENSUS es el FALLBACK más bajo
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
        # 🔥 FIX #51 (Feb 05): Location extreme cooldown check
        # If edge was recently invalidated by location_extreme, don't re-arm
        # This prevents the infinite cycle: ARM → BLOCK → ARM → BLOCK...
        # =====================================================================
        if self.is_location_extreme_cooldown_active(direction, market_time):
            logger.debug(
                f"[CONSENSUS_BLOCKED] Location extreme cooldown active for {direction} | "
                f"Expires: {self._location_extreme_cooldown_until.strftime('%H:%M:%S') if self._location_extreme_cooldown_until else 'N/A'}"
            )
            return False
        
        # =====================================================================
        # 🔥 FIX #159 (Feb 10): Post-displacement cooldown check
        # If a DISPLACEMENT edge recently expired, don't create new CONSENSUS edge
        # The market needs to show NEW structure, not re-arm on stale momentum
        # =====================================================================
        if self.is_post_displacement_cooldown_active(direction, market_time):
            cooldown_remaining = (self._post_displacement_cooldown_until - market_time).seconds // 60 if self._post_displacement_cooldown_until else 0
            logger.warning(
                f"🔥 FIX #159: CONSENSUS BLOCKED by POST-DISPLACEMENT COOLDOWN | "
                f"{direction} | ~{cooldown_remaining}min remaining | "
                f"Wait for NEW structure after displacement expired"
            )
            return False
        
        # =====================================================================
        # 🔥 FIX #194c (Feb 11): DEAD_EDGE cooldown check
        # If an edge recently expired because it was DEAD (>15 min), don't immediately
        # create a new edge in the same direction. The market needs NEW momentum.
        #
        # 🔥 FIX #223 (Feb 12): BYPASS COOLDOWN en TREND_BIRTH
        # PROBLEMA: Cooldown impide entrar al inicio de NUEVA tendencia
        # El momentum es NUEVO - no es el mismo edge stale
        # SOLUCIÓN: Si TrendPhase = TREND_BIRTH, ignorar cooldown
        # =====================================================================
        is_simple_cooldown, simple_remaining = self.is_simple_direction_cooldown_active(direction, market_time)
        
        # 🔥 FIX #223: Check for TREND_BIRTH bypass
        from src.domain.market.regime import TrendPhase
        trend_phase = getattr(self, '_trend_phase_from_engine', None)
        is_trend_birth = trend_phase == TrendPhase.TREND_BIRTH if trend_phase else False
        
        if is_simple_cooldown:
            if is_trend_birth:
                logger.warning(
                    f"🚀 FIX #223: BYPASSING DEAD_EDGE COOLDOWN for TREND_BIRTH | "
                    f"{direction} | TrendPhase={trend_phase.value if trend_phase else 'N/A'} | "
                    f"New trend momentum detected - cooldown not applicable"
                )
                # Clear the cooldown since new trend started
                if direction in self._direction_cooldown:
                    del self._direction_cooldown[direction]
            else:
                logger.warning(
                    f"🔥 FIX #194c: CONSENSUS BLOCKED by DEAD_EDGE COOLDOWN | "
                    f"{direction} | ~{simple_remaining}min remaining | "
                    f"Wait for fresh momentum after stale edge expired"
                )
                return False
        
        # 🔥 FIX #36 (Feb 05): Reset from EXPIRED state to allow re-arming
        # Without this, once an edge expires, CONSENSUS can never arm again
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired for consensus", market_time)
        
        # Crear BOSEvent para consensus con trend_path=CONSENSUS_DIRECT
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,  # En consensus, htf ya está alineado
            bos_level=current_price,  # Nivel de entrada
            atr_at_bos=atr,
            edge_type=EdgeType.TREND,
            trend_path=TrendEdgePath.CONSENSUS_DIRECT,  # 🔥 Path específico
            # Consensus-specific: entrada directa permitida
            force_direct_entry=True,
            entry_window_open=True
        )
        
        # Stats
        self._stats.total_bos_events += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1
        
        # Transición directa a ACTIVE (como displacement)
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"CONSENSUS_DIRECT {direction} | {reason} | Size 0.40-0.60x",
            market_time
        )
        
        _edge_log(
            "EDGE_ACTIVE", "TREND", direction,
            f"Consensus edge (path=DIRECT) | {reason} | Size reduced to 0.40-0.60x"
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
        setup_origin_price: Optional[float] = None   # 🔥 FIX Feb 06: Price when setup started
    ):
        """
        Armar el edge tras detectar BOS.
        
        🔥 FIX #31 (Feb 05): Para TREND edge, especificar el path:
        - BOS_PULLBACK: BOS detectado, esperar pullback (mejor entry)
        - CONSENSUS_DIRECT: Solo alineación, entrada directa
        
        🔥 FIX Feb 06: setup_origin_price for timing classification
        """
        # 🔥 Para BOS, siempre usar BOS_PULLBACK path (mejor sizing)
        if edge_type == EdgeType.TREND and trend_path is None:
            trend_path = TrendEdgePath.BOS_PULLBACK
        
        # 🔥 FIX Feb 06: Calculate move at detection
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
            # 🔥 FIX Feb 06: Timing fields
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
    
    def check_expiration(self, market_time: datetime) -> bool:
        """
        Verifica si el edge debe expirar.
        
        Returns: True si expiró
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            return False
        
        if self._current_bos is None:
            return False
        
        # 🔥 FIX #189 (Feb 11, 2026): EXPIRE DEAD EDGES AUTOMATICALLY
        # PROBLEMA: Edges con >35 min no expiraban, solo se bloqueaban con penalty -99.
        # El edge quedaba en EDGE_ACTIVE eternamente, nunca detectando un nuevo edge.
        # SOLUCIÓN: Expirar automáticamente edges DEAD (>15 min) antes de otros timeouts.
        # 
        # 🔥 FIX #194c (Feb 11, 2026): COOLDOWN TRAS DEAD_EDGE
        # PROBLEMA: Cuando edge expira por DEAD_EDGE, el sistema inmediatamente
        # crea un nuevo edge igual y el ciclo continúa (cycling):
        #   EDGE_ACTIVE → DEAD_EDGE expired → EDGE_ACTIVE → DEAD_EDGE expired → ...
        # SOLUCIÓN: Activar cooldown de 5 min tras expiración por DEAD_EDGE.
        # El mercado debe mostrar NUEVO momentum, no detectar el mismo edge muerto.
        #
        # 🔥 FIX #200 (Feb 12, 2026): REDUCE EDGE DEATH SPIRAL
        # PROBLEMA: Edge cycling paralizaba sistema por horas:
        #   - Edge vive 15min con STALE penalty creciente
        #   - Muere, cooldown 5min
        #   - Nuevo edge igual → ciclo infinito = 0 trades
        # SOLUCIÓN: 
        #   - Reducir MAX_AGE de 15→12min (menos penalty acumulado)
        #   - Reducir cooldown de 5→2min (reintento más rápido)
        MAX_EDGE_AGE_DEAD = 12.0  # 🔥 FIX #200: Reduced from 15.0 to allow less penalty buildup
        edge_age_min = self.get_edge_age_minutes(market_time)
        if edge_age_min > MAX_EDGE_AGE_DEAD:
            direction = self._current_bos.direction if self._current_bos else None
            self._expire_edge(f"🔥 FIX #189 DEAD_EDGE: age {edge_age_min:.1f}min > {MAX_EDGE_AGE_DEAD}min")
            # 🔥 FIX #194c + #200: Activar cooldown reducido para evitar cycling
            cooldown_minutes = 2  # 🔥 FIX #200: Reduced from 5→2min for faster retry
            if direction:
                cooldown_until = market_time + timedelta(minutes=cooldown_minutes)
                self._direction_cooldown[direction] = cooldown_until
                logger.warning(
                    f"🔥 FIX #194c: DEAD_EDGE cooldown {cooldown_minutes}min for {direction} | "
                    f"Wait for fresh momentum before new edge"
                )
            logger.warning(
                f"🔥 FIX #189: Expired DEAD edge after {edge_age_min:.1f}min. "
                f"Cooldown active for {cooldown_minutes}min."
            )
            return True
        
        bos = self._current_bos
        edge_type = bos.edge_type
        
        # Timeouts diferentes según tipo de edge
        if edge_type == EdgeType.RANGE:
            armed_timeout = self.RANGE_EDGE_TIMEOUT_MIN
            active_timeout = self.RANGE_EDGE_TIMEOUT_MIN
            provisional_timeout = ProvisionalEdgeConfig.TIME_STOP_MAX  # 5 min
        elif edge_type == EdgeType.BREAKOUT:
            armed_timeout = BreakoutEdgeCriteria.BREAKOUT_TIMEOUT_MIN
            active_timeout = self.EDGE_ACTIVE_TIMEOUT_MIN
            provisional_timeout = ProvisionalEdgeConfig.TIME_STOP_MAX
        elif edge_type == EdgeType.INTRADAY:
            armed_timeout = IntradayEdgeCriteria.EDGE_TIMEOUT_MIN  # 5 min
            active_timeout = IntradayEdgeCriteria.EDGE_TIMEOUT_MIN
            provisional_timeout = ProvisionalEdgeConfig.TIME_STOP_MIN  # 3 min
        else:  # TREND
            armed_timeout = self.EDGE_ARMED_TIMEOUT_MIN
            active_timeout = self.EDGE_ACTIVE_TIMEOUT_MIN
            provisional_timeout = ProvisionalEdgeConfig.TIME_STOP_MAX
        
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
    
    def _expire_edge(self, reason: str, set_context_cooldown: bool = True):
        """
        Expirar el edge actual.
        
        Cooldown por contexto si hay pérdidas:
        - BREAKOUT falla → TREND cooldown 15min
        - TREND falla → BREAKOUT cooldown 15min
        - RANGE no afecta otros (contexto independiente)
        
        🔥 FIX #159b (Feb 10): Si es DISPLACEMENT, SIEMPRE activar post-displacement cooldown
        para evitar que CONSENSUS entre después de momentum muerto.
        """
        edge_type_str = "UNKNOWN"
        direction = "-"
        
        if self._current_bos:
            self._current_bos.expired_at = datetime.utcnow()
            self._bos_history.append(self._current_bos)
            edge_type_str = self._current_bos.edge_type.value
            direction = self._current_bos.direction
            
            # =====================================================================
            # 🔥 FIX #159b (Feb 10): POST-DISPLACEMENT COOLDOWN AUTOMÁTICO
            # 
            # Si expira un DISPLACEMENT por CUALQUIER razón (timeout, flip, chase, etc),
            # SIEMPRE activar cooldown para evitar que CONSENSUS entre después.
            # Esto centraliza la protección y cierra TODOS los paths de expiración.
            # =====================================================================
            if self._current_bos.edge_type == EdgeType.DISPLACEMENT:
                logger.warning(
                    f"🔥 FIX #159b: DISPLACEMENT expired ({reason}) → "
                    f"Activating 10min POST-DISPLACEMENT COOLDOWN for {direction}"
                )
                self.set_post_displacement_cooldown(
                    direction=direction,
                    market_time=datetime.utcnow(),
                    cooldown_minutes=10
                )
            
            # =====================================================================
            # 🔥 FIX #248 (Feb 16, 2026): CONSENSUS EXPIRY DIRECTION COOLDOWN
            # 
            # PROBLEMA: Cuando un CONSENSUS_DIRECT edge expira (por invalidación
            # de precio, timeout, o cualquier razón), el sistema inmediatamente
            # re-arma un nuevo edge en la MISMA dirección. Loop infinito:
            #   EDGE_ACTIVE SHORT → invalidated → EDGE_ACTIVE SHORT → invalidated
            # 
            # LOG EVIDENCIA (Feb 12):
            # - 16:16:46: Edge SHORT invalidated (price $67,595 > BOS $67,278)
            # - 16:16:46: Immediately re-armed SHORT CONSENSUS at same pct=24%
            # - Zero trades in 20+ minutes of this cycling
            #
            # SOLUCIÓN: Activar direction cooldown de 5 min tras expiración de
            # CONSENSUS edges. Fuerza al sistema a esperar momentum NUEVO.
            # NOTA: El dead-edge cooldown en check_expiration() sobreescribe
            # a 2min para ese caso específico (comportamiento intencional).
            # =====================================================================
            if (hasattr(self._current_bos, 'trend_path') and 
                self._current_bos.trend_path == TrendEdgePath.CONSENSUS_DIRECT):
                cooldown_minutes = 5
                cooldown_until = datetime.utcnow() + timedelta(minutes=cooldown_minutes)
                self._direction_cooldown[direction] = cooldown_until
                logger.warning(
                    f"🔥 FIX #248: CONSENSUS {direction} cooldown {cooldown_minutes}min | "
                    f"Reason: {reason} | Wait for fresh momentum before re-arm"
                )
            
            # COOLDOWN POR CONTEXTO: bloquear edges relacionados
            if set_context_cooldown and self._current_bos.trades_lost > 0:
                edge_type = self._current_bos.edge_type
                cooldown_until = datetime.utcnow() + timedelta(minutes=self.CONTEXT_COOLDOWN_MINUTES)
                
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
        swing_range: float = 0.0  # 🔥 FIX #77b: swing_high - swing_low for adaptive buffer
    ) -> bool:
        """
        Verifica si el edge se ha invalidado por precio.
        
        TREND: Invalidado si precio cruza BOS level
        RANGE: INVALIDACIÓN BINARIA - si rompe el sweep level, tesis rota
        BREAKOUT: Invalidado si precio vuelve al squeeze
        DISPLACEMENT: Invalidado si precio vuelve >60% del displacement al origen
        PROVISIONAL: Usa mismas reglas que su edge_type subyacente
        
        🔥 FIX #77b: swing_range permite buffer adaptativo al contexto actual
        
        Returns: True si se invalidó
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            return False
        
        if self._current_bos is None:
            return False
        
        bos = self._current_bos
        
        # RANGE: INVALIDACIÓN BINARIA (sweep level) - no buffer
        if bos.edge_type == EdgeType.RANGE:
            if bos.direction == 'LONG' and current_price < bos.bos_level:
                self._expire_edge(f"binary: price {current_price:.0f} < sweep {bos.bos_level:.0f}")
                return True
            elif bos.direction == 'SHORT' and current_price > bos.bos_level:
                self._expire_edge(f"binary: price {current_price:.0f} > sweep {bos.bos_level:.0f}")
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
        # 🔥 FIX #77b (Feb 07, 2026): BUFFER ADAPTATIVO CON SWING
        # 
        # OLD: invalidation_buffer = atr * 0.6 (estático)
        # NEW: invalidation_buffer = max(atr * 0.6, swing_range * 0.3)
        # 
        # WHY ADAPTIVE:
        # - En swings pequeños (mercado tranquilo): usa 0.6× ATR
        # - En swings grandes (volatilidad): usa 30% del swing range
        # - Esto adapta automáticamente al contexto del mercado
        # 
        # EXAMPLE:
        # - ATR = $500, swing = $1200 → max(300, 360) = $360 buffer
        # - ATR = $500, swing = $600 → max(300, 180) = $300 buffer
        # - ATR = $500, swing = $2000 → max(300, 600) = $600 buffer
        #
        # 🔥 FIX #187 (Feb 11, 2026): CAP swing_buffer to prevent absurd SL
        # PROBLEMA: swing_range de $5,000+ → swing_buffer de $1,500+
        # Esto hace que 1R = $1,500, y cualquier movimiento pequeño sea -0.3R
        # Trade real: SL a $1,197 → movimiento de $500 = -0.40R → TIME_INVALIDATION
        # SOLUCIÓN: Cap swing_buffer a max 2× ATR o $600, lo que sea menor
        # =====================================================================
        atr_buffer = atr * 0.6
        swing_buffer_raw = swing_range * 0.3 if swing_range > 0 else 0.0
        # 🔥 FIX #187: Cap swing_buffer to prevent absurd SL distances
        MAX_SWING_BUFFER_ATR_MULT = 2.0  # Max 2× ATR
        MAX_SWING_BUFFER_ABS = 600.0     # Max $600 absolute
        swing_buffer_cap = min(atr * MAX_SWING_BUFFER_ATR_MULT, MAX_SWING_BUFFER_ABS)
        swing_buffer = min(swing_buffer_raw, swing_buffer_cap)
        
        if swing_buffer_raw > swing_buffer_cap:
            logger.debug(
                f"🔥 FIX #187: Capped swing_buffer ${swing_buffer_raw:.0f} → ${swing_buffer:.0f} "
                f"(max: {MAX_SWING_BUFFER_ATR_MULT}× ATR or ${MAX_SWING_BUFFER_ABS})"
            )
        
        invalidation_buffer = max(atr_buffer, swing_buffer)
        
        # Log if using swing-based buffer (significant difference) - THROTTLED
        # Solo loggear la primera vez para reducir spam
        if swing_buffer > atr_buffer * 1.2 and not getattr(self, '_swing_buffer_logged', False):
            _edge_log(
                self._state.value, bos.edge_type.value, bos.direction,
                f"FIX #77b: Using swing buffer ${swing_buffer:.0f} > ATR buffer ${atr_buffer:.0f}"
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
    
    def check_crash_invalidation(
        self,
        is_panic_down: bool,
        is_meltup: bool,
        crash_confidence: float = 0.0
    ) -> bool:
        """
        🔥 FIX A3 (Feb 07, 2026): Invalidar edge cuando crash en dirección contraria.
        🔥 FIX #81 (Feb 07): STATS TRACKING for Crash Invalidation
        
        PROBLEMA:
        - Edge LONG armado/activo
        - Mercado entra en PANIC_DOWN (crash)
        - El edge LONG sigue activo, esperando pullback que nunca viene
        - Sistema pierde oportunidades de SHORT
        
        SOLUCIÓN:
        - Si edge es LONG y hay PANIC_DOWN → invalidar edge inmediatamente
        - Si edge es SHORT y hay MELTUP → invalidar edge inmediatamente
        
        VALIDACIÓN REQUERIDA (USER FEEDBACK):
        - "Si <50 PANIC_DOWN reales → esto es ajuste fino peligroso (overfitting)"
        - Trackear cuántas veces se usa para validar utilidad
        
        Args:
            is_panic_down: True si crash detector detecta pánico bajista
            is_meltup: True si crash detector detecta subida parabólica
            crash_confidence: 0-1 confianza del detector (usar si > 0.6)
        
        Returns: True si se invalidó el edge
        """
        # 🔥 FIX #81: Initialize stats tracker if not exists
        if not hasattr(self, '_crash_invalidation_stats'):
            self._crash_invalidation_stats = {
                'panic_down_detected': 0,
                'meltup_detected': 0,
                'long_invalidated': 0,
                'short_invalidated': 0,
                'skipped_no_edge': 0,
                'skipped_low_confidence': 0,
            }
        
        # Track all detections (for stats validation)
        if is_panic_down:
            self._crash_invalidation_stats['panic_down_detected'] += 1
        if is_meltup:
            self._crash_invalidation_stats['meltup_detected'] += 1
        
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]:
            if is_panic_down or is_meltup:
                self._crash_invalidation_stats['skipped_no_edge'] += 1
            return False
        
        if not self._current_bos:
            return False
        
        bos = self._current_bos
        direction = bos.direction
        
        # Check confidence threshold
        if crash_confidence < 0.6:
            if is_panic_down or is_meltup:
                self._crash_invalidation_stats['skipped_low_confidence'] += 1
            return False
        
        # Edge LONG durante crash → invalidar
        if direction == 'LONG' and is_panic_down:
            self._crash_invalidation_stats['long_invalidated'] += 1
            self._expire_edge(
                f"🔥 CRASH_INVALIDATION: LONG edge killed by PANIC_DOWN (conf={crash_confidence:.0%})",
                set_context_cooldown=True
            )
            logger.warning(
                f"🔥 FIX A3: Edge {bos.edge_type.value} LONG invalidated by CRASH | "
                f"Confidence: {crash_confidence:.0%} | "
                f"Stats: {self._crash_invalidation_stats}"
            )
            return True
        
        # Edge SHORT durante melt-up → invalidar
        if direction == 'SHORT' and is_meltup:
            self._crash_invalidation_stats['short_invalidated'] += 1
            self._expire_edge(
                f"🔥 CRASH_INVALIDATION: SHORT edge killed by MELTUP (conf={crash_confidence:.0%})",
                set_context_cooldown=True
            )
            logger.warning(
                f"🔥 FIX A3: Edge {bos.edge_type.value} SHORT invalidated by MELTUP | "
                f"Confidence: {crash_confidence:.0%} | "
                f"Stats: {self._crash_invalidation_stats}"
            )
            return True
        
        return False
    
    def get_crash_invalidation_stats(self) -> dict:
        """Get stats for crash invalidation to validate usefulness."""
        if not hasattr(self, '_crash_invalidation_stats'):
            return {'error': 'Stats not initialized'}
        return self._crash_invalidation_stats.copy()
    
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
            return (0.50, 0.70, "NO_EDGE")  # 🔥 FIX #121b: 0.40-0.60 → 0.50-0.70
        
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
                edge_info = "TREND_BOS"  # 🔥 FIX #121b: 0.60-0.80 → 0.70-0.90
            else:  # CONSENSUS_DIRECT or None
                # No BOS, just alignment - 🔥 FIX #121b: Use more capital
                base_min, base_max = 0.60, 0.80
                edge_info = "TREND_CONSENSUS"  # 🔥 FIX #121b: 0.40-0.60 → 0.60-0.80
                
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
    
    def get_edge_info_for_sizing(self) -> dict:
        """
        Returns comprehensive edge info for sizing decisions.
        Used by entry_orchestrator to determine sizing parameters.
        """
        if self._current_bos is None:
            return {
                'has_edge': False,
                'edge_type': 'NONE',
                'trend_path': None,
                'priority': 0,
                'direction': None,
                'can_trade': False
            }
        
        return {
            'has_edge': True,
            'edge_type': self._current_bos.edge_type.value,
            'trend_path': getattr(self._current_bos, 'trend_path', None),
            'priority': self._current_bos.get_priority(),
            'direction': self._current_bos.direction,
            'can_trade': self.can_trade(),
            'trades_remaining': self.MAX_TRADES_PER_EDGE - self._current_bos.trades_attempted
        }
    
    # =========================================================================
    # 🌊 STATUS AND UTILITIES
    # =========================================================================
    # 🔥 FIX Feb 06: Signal Timing Classification
    # =========================================================================
    
    def calculate_signal_timing(
        self,
        current_price: float,
        direction: str
    ) -> tuple[str, float]:
        """
        Calculate signal timing classification.
        
        Compares current price with the setup_origin_price or origin_price
        to determine if we're detecting EARLY or LATE.
        
        Returns:
            (timing_category: str, move_pct: float)
            - timing_category: 'early' (<0.3%), 'on_time' (<0.8%), 'late' (<1.5%), 'very_late' (>1.5%)
            - move_pct: percentage price has moved since setup origin
        """
        if self._current_bos is None:
            return 'unknown', 0.0
        
        # Use setup_origin_price if available, else origin_price, else bos_level
        origin = (
            self._current_bos.setup_origin_price or 
            self._current_bos.origin_price or 
            self._current_bos.bos_level
        )
        
        if origin is None or origin <= 0:
            return 'unknown', 0.0
        
        # Calculate move percentage
        if direction == 'LONG':
            move_pct = ((current_price - origin) / origin) * 100
        else:  # SHORT
            move_pct = ((origin - current_price) / origin) * 100
        
        # Classify timing
        # Thresholds based on typical BTC volatility
        if move_pct < 0.3:
            timing = 'early'
        elif move_pct < 0.8:
            timing = 'on_time'
        elif move_pct < 1.5:
            timing = 'late'
        else:
            timing = 'very_late'
        
        return timing, abs(move_pct)
    
    def get_timing_for_missed_tracker(
        self,
        current_price: float,
        direction: str
    ) -> dict:
        """
        Get timing info formatted for MissedOpportunityTracker.
        
        Returns dict with:
            - timing_category: SignalTimingCategory enum value
            - move_at_detection_pct: float
            - setup_price: float
        """
        from src.infrastructure.monitoring.opportunity_tracker import SignalTimingCategory
        
        timing_str, move_pct = self.calculate_signal_timing(current_price, direction)
        
        # Map string to enum
        timing_map = {
            'early': SignalTimingCategory.EARLY,
            'on_time': SignalTimingCategory.ON_TIME,
            'late': SignalTimingCategory.LATE,
            'very_late': SignalTimingCategory.VERY_LATE,
            'unknown': SignalTimingCategory.UNKNOWN
        }
        
        setup_price = 0.0
        if self._current_bos:
            setup_price = (
                self._current_bos.setup_origin_price or 
                self._current_bos.origin_price or 
                self._current_bos.bos_level or
                0.0
            )
        
        return {
            'timing_category': timing_map.get(timing_str, SignalTimingCategory.UNKNOWN),
            'move_at_detection_pct': move_pct,
            'setup_price': setup_price
        }
    
    # =========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        """Estado actual - compact format"""
        status = {
            'state': self._state.value,
            'mode': self._current_edge_mode.value,
            'can_trade': self.can_trade(),
            'trades_hour': self._intraday_trades_this_hour
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
    
    def get_transition_history(self, last_n: int = 10) -> List[Dict[str, Any]]:
        """Get recent transition history for debugging"""
        return [
            {
                'from': t.from_state.value,
                'to': t.to_state.value,
                'reason': t.reason,
                'type': t.edge_type.value if t.edge_type else None,
                'dir': t.direction,
                'time': t.timestamp.isoformat() if t.timestamp else None
            }
            for t in self._transition_history[-last_n:]
        ]
    
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