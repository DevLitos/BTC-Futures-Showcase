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
# �🎯 EDGE MODE: INSTITUTIONAL vs INTRADAY (Jan 28, 2026)
# =============================================================================
class EdgeMode(Enum):
    """
    Modo de operación del edge - determina frequencia y sizing.
    
    INSTITUTIONAL: Low frequency, high conviction (1-5/semana)
    INTRADAY: High frequency, low conviction (5-20/día)
    
    NO compiten. NO se solapan. NO se contradicen.
    """
    INSTITUTIONAL = "INSTITUTIONAL"  # BREAKOUT/RANGE extremo - Size 1.0x-1.5x
    INTRADAY = "INTRADAY"            # Micro-ineficiencias - Size 0.15x-0.30x


# =============================================================================
# 🎯 EDGE TYPE: Distinguir entre TREND y RANGE edges
# =============================================================================
class EdgeType(Enum):
    """Tipo de edge - determina reglas de activación y gestión"""
    TREND = "TREND"    # BOS + Pullback
    RANGE = "RANGE"    # Extremo + Mean Reversion
    BREAKOUT = "BREAKOUT"  # Compresión + Ruptura confirmada
    INTRADAY = "INTRADAY"  # Micro-ineficiencias intradía
    DISPLACEMENT = "DISPLACEMENT"  # 🔥 NEW (Jan 30, 2026): Strong displacement edge


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
    # =========================================================================
    MIN_TIMING = 0                   # timing >= 0 (no >= 1)
    MIN_R = 0.20                     # R >= 0.20 (no 0.30)
    MIN_ADX_5M = 25                  # ADX 5m >= 25 (bias claro)
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
    RANGE_EDGE_TIMEOUT_MIN = 10  # 10 min ventana en rango


@dataclass
class BreakoutEdgeCriteria:
    """
    Criterios para BREAKOUT edge (compresión).
    
    Un edge de breakout requiere:
    1. BB squeeze (width < percentile 10)
    2. Volume decline confirmado
    3. Ruptura del squeeze con volumen
    """
    BB_WIDTH_PERCENTILE_MAX = 10  # BB width < percentil 10
    VOLUME_DECLINE_RATIO = 0.50   # Vol actual / Vol medio < 0.5
    BREAKOUT_CONFIRMATION_CANDLES = 2  # 2 velas para confirmar ruptura
    BREAKOUT_TIMEOUT_MIN = 15  # 15 min después de compresión detectada
    
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
    PULLBACK_ENTRY_MAX_BARS = 5       # Máximo 5 velas para pullback
    PULLBACK_ENTRY_SIZE_MIN = 0.40    # Size mínimo 40% en pullback
    PULLBACK_ENTRY_SIZE_MAX = 0.60    # Size máximo 60% en pullback
    
    # ZONA 3 (bars >5) - Chase Block
    CHASE_BLOCK_AFTER_BARS = 5        # No trade después de 5 barras
    CHASE_BLOCK_EXTENSION_MULT = 1.5  # No trade si movió >1.5x displacement
    
    # Timeouts
    EDGE_TIMEOUT_MIN = 60             # 60 min ventana (movimientos fuertes duran)
    CONFIRMATION_TIMEOUT_MIN = 20     # 20 min para confirmación
    
    # 🔥 KILL LATE EDGE (Jan 30, 2026)
    # Si pasa la ventana de entrada, NO perseguir
    KILL_EDGE_AFTER_BARS = 5          # Matar edge si > 5 barras sin entrada
    KILL_EDGE_AFTER_MIN = 10          # O si > 10 min sin entrada
    
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
    edge_type: EdgeType = EdgeType.TREND  # 🔥 NEW: Tipo de edge
    
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


def validate_intraday_r_with_liquidity(
    estimated_r: float,
    liquidity_event: Optional[LiquidityEvent],
    direction: str
) -> Tuple[bool, str, float]:
    """
    🔥 VALIDACIÓN INTRADAY - LiquidityEvent como BOOST, no GATE.
    
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
        """Log transition with unified format"""
        edge_type_str = self.edge_type.value if self.edge_type else "UNKNOWN"
        direction_str = self.direction or "?"
        _edge_log(
            self.to_state.value,
            edge_type_str,
            direction_str,
            f"{self.from_state.value}→{self.to_state.value} | {self.reason}"
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
    
    def is_institutional_active(self) -> bool:
        """¿Hay un edge INSTITUTIONAL activo? Si sí, INTRADAY debe estar OFF"""
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._current_bos and self._current_bos.edge_type in [EdgeType.BREAKOUT, EdgeType.RANGE, EdgeType.TREND]:
                return True
        return False
    
    def is_provisional(self) -> bool:
        """¿Estamos en estado PROVISIONAL (starter position)?"""
        return self._state == EdgeState.EDGE_PROVISIONAL
    
    def can_evaluate_entry(self) -> bool:
        """
        ¿Puede el sistema evaluar entradas?
        ACTIVE o PROVISIONAL permiten evaluación.
        """
        return self._state in [EdgeState.EDGE_ACTIVE, EdgeState.EDGE_PROVISIONAL]
    
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
        """Razón por la que no se puede operar - compact format"""
        if self._state == EdgeState.NO_EDGE:
            return "NO_EDGE"
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
            return False, None
        
        if market_time >= cooldown_until:
            # Cooldown expirado, limpiar
            del self._direction_cooldown[key]
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
    # 🔥 INTRADAY BASE EDGE - Simple, repetible, frecuente
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
        🔥 EDGE BASE INTRADAY - Simple, repetible, ocurre varias veces al día.
        
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
    COMPRESSION_DISABLES_RANGE_PCT = 0  # Disabled - PROBE allowed in compression
    
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
        # 🔥 MUTUAL EXCLUSION: Si PROVISIONAL INTRADAY activo → RANGE bloqueado
        # RANGE en chop + PROVISIONAL = muerte lenta por correlación oculta
        # =================================================================
        if self._state == EdgeState.EDGE_PROVISIONAL and self._current_bos:
            if self._current_bos.edge_type == EdgeType.INTRADAY:
                logger.debug(
                    "[RANGE_BLOCKED] PROVISIONAL INTRADAY activo - RANGE bloqueado (correlación oculta)"
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
        market_time: datetime
    ) -> bool:
        """
        Detecta edge de BREAKOUT (compresión).
        
        Llamado cuando Anticipation detecta COMPRESSION_BREAKOUT.
        Arma el edge, esperando confirmación de ruptura.
        
        Returns: True si se armó edge
        """
        # Check context cooldown
        if self.is_context_in_cooldown(EdgeType.BREAKOUT, market_time):
            return False
        
        # No crear nuevo edge si ya hay uno
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False
        
        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)
        
        # Verificar criterios de compresión
        is_squeezed = bb_width_percentile <= BreakoutEdgeCriteria.BB_WIDTH_PERCENTILE_MAX
        vol_declining = volume_ratio <= BreakoutEdgeCriteria.VOLUME_DECLINE_RATIO
        
        if is_squeezed and vol_declining:
            # Armar edge de breakout (esperar confirmación)
            self._current_bos = BOSEvent(
                timestamp=market_time,
                price=current_price,
                direction=direction,
                htf_bias=direction,
                bos_level=current_price,
                atr_at_bos=atr,
                edge_type=EdgeType.BREAKOUT,
                bb_width_percentile=bb_width_percentile,
                volume_ratio=volume_ratio
            )
            
            # Stats
            self._stats.total_bos_events += 1
            self._stats.breakout_edges += 1
            
            # Transición fluida
            self._transition_to(
                EdgeState.EDGE_ARMED,
                f"BB%={bb_width_percentile:.0f} Vol={volume_ratio:.2f}",
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
        
        Criterios:
        1. Close FUERA de Bollinger Bands (no solo mecha)
        2. Dirección coincide con anticipation
        3. Expansión de BB comenzando (ratio > 1.0)
        """
        if self._state != EdgeState.EDGE_ARMED:
            return False
        
        if self._current_bos is None or self._current_bos.edge_type != EdgeType.BREAKOUT:
            return False
        
        # Verificar que dirección coincide
        if breakout_direction != self._current_bos.direction:
            self._expire_edge(f"dir mismatch: {breakout_direction} ≠ {self._current_bos.direction}")
            return False
        
        # CRITERIO 1: Close FUERA de Bollinger Bands
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
        
        # CRITERIO 2: Expansión de BB (optional)
        # ratio > 1.0 = BB expanding = real breakout (logged but not blocking)
        
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
        
        Invalidación:
        1. Close de vuelta DENTRO de BB (fake breakout)
        2. Pérdida del nivel de breakout + buffer ATR
        
        Returns: (invalidated, reason)
        """
        if self._state != EdgeState.EDGE_ACTIVE:
            return False, ""
        
        if self._current_bos is None or self._current_bos.edge_type != EdgeType.BREAKOUT:
            return False, ""
        
        bos = self._current_bos
        direction = bos.direction
        
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
        initial_price: float = 0.0
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
        
        Returns: True si se armó el edge
        """
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
        # =====================================================================
        if bos.bars_since_displacement > DisplacementEdgeCriteria.KILL_EDGE_AFTER_BARS:
            if bos.trades_attempted == 0:  # Solo si no ha entrado
                return True, f"LATE DISPLACEMENT: {bos.bars_since_displacement} bars > {DisplacementEdgeCriteria.KILL_EDGE_AFTER_BARS} - NO CHASE"
        
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
        break_distance: float = 0.0
    ):
        """Armar el edge tras detectar BOS"""
        self._current_bos = BOSEvent(
            timestamp=timestamp,
            price=price,
            direction=direction,
            htf_bias=htf_bias,
            bos_level=bos_level,
            atr_at_bos=atr,
            edge_type=edge_type
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
        self._transition_to(
            EdgeState.EDGE_ARMED,
            f"BOS@{bos_level:.0f} dist={break_distance:.2f}ATR",
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
        """
        edge_type_str = "UNKNOWN"
        direction = "-"
        
        if self._current_bos:
            self._current_bos.expired_at = datetime.utcnow()
            self._bos_history.append(self._current_bos)
            edge_type_str = self._current_bos.edge_type.value
            direction = self._current_bos.direction
            
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
        atr: float
    ) -> bool:
        """
        Verifica si el edge se ha invalidado por precio.
        
        TREND: Invalidado si precio cruza BOS level
        RANGE: INVALIDACIÓN BINARIA - si rompe el sweep level, tesis rota
        BREAKOUT: Invalidado si precio vuelve al squeeze
        DISPLACEMENT: Invalidado si precio vuelve >60% del displacement al origen
        PROVISIONAL: Usa mismas reglas que su edge_type subyacente
        
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
        
        # TREND y BREAKOUT: invalidación con buffer pequeño
        invalidation_buffer = atr * 0.2
        
        if bos.direction == 'LONG':
            if current_price < (bos.bos_level - invalidation_buffer):
                self._expire_edge(f"invalidated: price {current_price:.0f} < level {bos.bos_level:.0f}")
                return True
        elif bos.direction == 'SHORT':
            if current_price > (bos.bos_level + invalidation_buffer):
                self._expire_edge(f"invalidated: price {current_price:.0f} > level {bos.bos_level:.0f}")
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
    # INTRADAY EDGE DETECTION - High freq, low conviction
    # =========================================================================
    
    def check_for_intraday_edge(
        self,
        current_price: float,
        atr_current: float,
        atr_average: float,
        volume_current: float,
        volume_average: float,
        swing_direction: str,  # 'BULLISH' (HH/HL) or 'BEARISH' (LL/LH)
        swing_count: int,
        session_quality: str,  # 'good', 'moderate', 'poor'
        market_time: datetime
    ) -> bool:
        """
        Detecta edge INTRADAY - micro-ineficiencias cuando mercado activo.
        
        NO se activa si hay INSTITUTIONAL edge.
        
        Criterios:
        1. Mercado ACTIVO: ATR expanding + Volume > avg
        2. Dirección clara: Swing sequence (HH/HL or LL/LH)
        3. Session no poor
        """
        # EXCLUSIÓN MUTUA: Si INSTITUTIONAL activo → INTRADAY OFF
        if self.is_institutional_active():
            return False
        
        # No crear nuevo edge si ya hay uno
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False
        
        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._transition_to(EdgeState.NO_EDGE, "reset from expired", market_time)
        
        # LIMITES DE FRECUENCIA
        if self._last_hour_reset is None or (market_time - self._last_hour_reset).total_seconds() >= 3600:
            self._intraday_trades_this_hour = 0
            self._last_hour_reset = market_time
        
        if self._intraday_trades_this_hour >= IntradayEdgeCriteria.MAX_TRADES_PER_HOUR:
            return False
        
        if self._last_intraday_trade_time:
            minutes_since_last = (market_time - self._last_intraday_trade_time).total_seconds() / 60
            if minutes_since_last < IntradayEdgeCriteria.MIN_BETWEEN_TRADES_MIN:
                return False
        
        # CRITERIO 1: Session no poor
        if IntradayEdgeCriteria.REQUIRE_ACTIVE_SESSION and session_quality == 'poor':
            return False
        
        # CRITERIO 2: Mercado ACTIVO
        atr_ratio = atr_current / atr_average if atr_average > 0 else 0
        volume_ratio = volume_current / volume_average if volume_average > 0 else 0
        
        if atr_ratio < IntradayEdgeCriteria.ATR_EXPANSION_RATIO_MIN:
            return False
        
        if volume_ratio < IntradayEdgeCriteria.VOLUME_RATIO_MIN:
            return False
        
        # CRITERIO 3: Dirección clara
        if swing_count < IntradayEdgeCriteria.MIN_SWING_SEQUENCE:
            return False
        
        if swing_direction not in ['BULLISH', 'BEARISH']:
            return False
        
        # ACTIVAR INTRADAY EDGE - directo a ACTIVE (no ARMED)
        direction = 'LONG' if swing_direction == 'BULLISH' else 'SHORT'
        
        self._current_bos = BOSEvent(
            timestamp=market_time,
            price=current_price,
            direction=direction,
            htf_bias=direction,
            bos_level=current_price,
            atr_at_bos=atr_current,
            edge_type=EdgeType.INTRADAY
        )
        
        self._current_edge_mode = EdgeMode.INTRADAY
        
        # Transición directa NO_EDGE → ACTIVE (INTRADAY no pasa por ARMED)
        self._transition_to(
            EdgeState.EDGE_ACTIVE,
            f"ATR={atr_ratio:.2f} Vol={volume_ratio:.2f} swings={swing_count}",
            market_time
        )
        return True
    
    def record_intraday_trade(self, market_time: datetime):
        """Registra trade intraday para control de frecuencia"""
        self._intraday_trades_this_hour += 1
        self._last_intraday_trade_time = market_time
    
    def get_intraday_size_multiplier(
        self,
        atr_expansion_ratio: float = 1.0,
        volume_ratio: float = 1.0
    ) -> float:
        """
        Retorna multiplicador de size para INTRADAY: 0.15x - 0.30x
        Mayor ATR/Vol → más hacia 0.30x
        """
        base = IntradayEdgeCriteria.SIZE_MIN
        max_size = IntradayEdgeCriteria.SIZE_MAX
        activity_factor = min(atr_expansion_ratio, 2.0) * min(volume_ratio, 2.0) / 4.0
        return min(base + (max_size - base) * activity_factor, max_size)
    
    def get_institutional_size_multiplier(
        self,
        percentile: float = 50.0,
        bb_percentile: float = 50.0,
        session_quality: str = 'moderate'
    ) -> float:
        """
        Retorna multiplicador de size para INSTITUTIONAL: 1.0x - 1.5x
        
        HIGH_CONVICTION (1.5x) si 2+ de:
        - Percentil extremo (≥95 o ≤5)
        - BB squeeze extremo (<5%)
        - Session óptima (LONDON, NY_OVERLAP)
        """
        high_conviction_count = 0
        
        if percentile >= InstitutionalEdgeConfig.HIGH_CONVICTION_PERCENTILE_EXTREME or \
           percentile <= (100 - InstitutionalEdgeConfig.HIGH_CONVICTION_PERCENTILE_EXTREME):
            high_conviction_count += 1
        
        if bb_percentile <= InstitutionalEdgeConfig.HIGH_CONVICTION_BB_SQUEEZE:
            high_conviction_count += 1
        
        if session_quality == 'good':
            high_conviction_count += 1
        
        if high_conviction_count >= 2:
            return InstitutionalEdgeConfig.SIZE_MULTIPLIER_HIGH_CONVICTION  # 1.5x
        
        return InstitutionalEdgeConfig.SIZE_MULTIPLIER_BASE  # 1.0x
    
    # =========================================================================
    # 🌊 STATUS AND UTILITIES
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