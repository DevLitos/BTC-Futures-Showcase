"""
EdgeStateManager - El edge como EVENTO DISCRETO, no como contexto continuo.

Filosofía:
- El sistema NO busca entradas continuamente
- Solo opera cuando hay un EDGE ACTIVO
- Un edge es un evento temporal con vida limitada

Estados:
- NO_EDGE: Sin oportunidad. NO SE EVALÚA NADA.
- EDGE_ARMED: BOS detectado, esperando pullback
- EDGE_ACTIVE: Pullback válido, ventana de entrada abierta
- EDGE_EXPIRED: Oportunidad consumida o expirada

Reglas:
- Máximo 2 trades por edge event
- Edge expira después de X minutos sin ejecución
- Si ambos trades fallan, edge está "quemado"
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


class EdgeState(Enum):
    """Estados del edge - el sistema VIVE en uno de estos"""
    NO_EDGE = "NO_EDGE"           # Sin oportunidad, no evaluar entradas
    EDGE_ARMED = "EDGE_ARMED"     # BOS detectado, esperando pullback
    EDGE_ACTIVE = "EDGE_ACTIVE"   # Pullback válido, ventana abierta
    EDGE_EXPIRED = "EDGE_EXPIRED" # Oportunidad consumida


@dataclass
class BOSEvent:
    """Un evento de Break of Structure"""
    timestamp: datetime
    price: float
    direction: str  # 'LONG' or 'SHORT'
    htf_bias: str   # HTF alignment
    bos_level: float  # Nivel que se rompió
    atr_at_bos: float
    
    # Tracking
    trades_attempted: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    total_r_captured: float = 0.0
    
    # Timing
    pullback_detected_at: Optional[datetime] = None
    first_entry_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    
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
            }
        }


class EdgeStateManager:
    """
    Gestor de estado del edge.
    
    El sistema SOLO puede entrar si está en EDGE_ACTIVE.
    Fuera de ese estado, NO hay evaluación de entradas.
    """
    
    # Configuración
    MAX_TRADES_PER_EDGE = 2          # Máximo 2 intentos por BOS
    EDGE_ARMED_TIMEOUT_MIN = 30      # Timeout si no hay pullback
    EDGE_ACTIVE_TIMEOUT_MIN = 15     # Ventana de entrada tras pullback
    PULLBACK_MIN_ATR = 0.3           # Mínimo pullback en ATR
    PULLBACK_MAX_ATR = 1.5           # Máximo pullback en ATR
    
    def __init__(self):
        self._state = EdgeState.NO_EDGE
        self._current_bos: Optional[BOSEvent] = None
        self._stats = EdgeStats()
        self._bos_history: List[BOSEvent] = []
        
        # Cache para evitar re-evaluación
        self._last_bos_check_ts: Optional[datetime] = None
        self._last_bos_check_price: Optional[float] = None
        
        logger.info("🎯 EdgeStateManager initialized - Edge as DISCRETE EVENT")
    
    @property
    def state(self) -> EdgeState:
        return self._state
    
    @property
    def current_bos(self) -> Optional[BOSEvent]:
        return self._current_bos
    
    @property
    def stats(self) -> EdgeStats:
        return self._stats
    
    def can_evaluate_entry(self) -> bool:
        """
        ¿Puede el sistema evaluar entradas?
        SOLO si estamos en EDGE_ACTIVE.
        """
        return self._state == EdgeState.EDGE_ACTIVE
    
    def can_trade(self) -> bool:
        """
        ¿Puede el sistema ejecutar un trade?
        Requiere EDGE_ACTIVE y que el BOS no esté quemado.
        """
        if self._state != EdgeState.EDGE_ACTIVE:
            return False
        if self._current_bos is None:
            return False
        return self._current_bos.can_trade()
    
    def get_block_reason(self) -> Optional[str]:
        """Razón por la que no se puede operar"""
        if self._state == EdgeState.NO_EDGE:
            return "NO_EDGE: Sin BOS activo, no hay oportunidad"
        elif self._state == EdgeState.EDGE_ARMED:
            return "EDGE_ARMED: BOS detectado, esperando pullback"
        elif self._state == EdgeState.EDGE_EXPIRED:
            return "EDGE_EXPIRED: Oportunidad consumida o expirada"
        elif self._state == EdgeState.EDGE_ACTIVE:
            if self._current_bos and self._current_bos.is_burned():
                return f"EDGE_BURNED: {self._current_bos.trades_attempted} intentos, {self._current_bos.trades_lost} pérdidas"
        return None
    
    # =========================================================================
    # DETECCIÓN DE BOS
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
        # 🔥 FIX: Handle None bias
        if htf_bias is None:
            return False
        
        # No detectar nuevo BOS si ya tenemos uno activo
        if self._state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False
        
        # Reset si estábamos expirados
        if self._state == EdgeState.EDGE_EXPIRED:
            self._state = EdgeState.NO_EDGE
        
        bos_detected = False
        direction = None
        bos_level = None
        
        # BOS LONG: Rompe swing high previo
        if htf_bias == 'LONG':
            if current_price > prev_swing_high:
                # Verificar que no sea una falsa ruptura
                break_distance = (current_price - prev_swing_high) / atr
                if 0.1 < break_distance < 1.5:  # Ruptura válida
                    bos_detected = True
                    direction = 'LONG'
                    bos_level = prev_swing_high
                    logger.info(f"🔥 BOS LONG detectado: Ruptura de {prev_swing_high:.2f} "
                               f"(distancia: {break_distance:.2f} ATR)")
        
        # BOS SHORT: Rompe swing low previo
        elif htf_bias == 'SHORT':
            if current_price < prev_swing_low:
                break_distance = (prev_swing_low - current_price) / atr
                if 0.1 < break_distance < 1.5:
                    bos_detected = True
                    direction = 'SHORT'
                    bos_level = prev_swing_low
                    logger.info(f"🔥 BOS SHORT detectado: Ruptura de {prev_swing_low:.2f} "
                               f"(distancia: {break_distance:.2f} ATR)")
        
        if bos_detected:
            # 🔥 FIX: Type narrowing - direction and bos_level are set when bos_detected is True
            assert direction is not None and bos_level is not None
            self._arm_edge(
                timestamp=market_time,
                price=current_price,
                direction=direction,
                htf_bias=htf_bias or 'UNKNOWN',
                bos_level=bos_level,
                atr=atr
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
        atr: float
    ):
        """Armar el edge tras detectar BOS"""
        self._current_bos = BOSEvent(
            timestamp=timestamp,
            price=price,
            direction=direction,
            htf_bias=htf_bias,
            bos_level=bos_level,
            atr_at_bos=atr
        )
        self._state = EdgeState.EDGE_ARMED
        
        # Actualizar stats
        self._stats.total_bos_events += 1
        if direction == 'LONG':
            self._stats.long_bos_events += 1
        else:
            self._stats.short_bos_events += 1
        
        logger.info(f"🎯 EDGE ARMED: {direction} | BOS level: {bos_level:.2f} | "
                   f"Esperando pullback (0.3-1.5 ATR)")
    
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
            self._expire_edge("TIMEOUT: Sin pullback en 30 min")
            return False
        
        bos = self._current_bos
        pullback_valid = False
        
        if bos.direction == 'LONG':
            # Pullback = precio retrocede hacia el nivel de BOS
            pullback_distance = (bos.price - current_price) / atr
            if self.PULLBACK_MIN_ATR <= pullback_distance <= self.PULLBACK_MAX_ATR:
                # Verificar que sigue por encima del BOS level
                if current_price > bos.bos_level:
                    pullback_valid = True
                    logger.info(f"✅ PULLBACK LONG válido: {pullback_distance:.2f} ATR "
                               f"(precio: {current_price:.2f}, BOS: {bos.bos_level:.2f})")
        
        elif bos.direction == 'SHORT':
            pullback_distance = (current_price - bos.price) / atr
            if self.PULLBACK_MIN_ATR <= pullback_distance <= self.PULLBACK_MAX_ATR:
                if current_price < bos.bos_level:
                    pullback_valid = True
                    logger.info(f"✅ PULLBACK SHORT válido: {pullback_distance:.2f} ATR "
                               f"(precio: {current_price:.2f}, BOS: {bos.bos_level:.2f})")
        
        if pullback_valid:
            self._activate_edge(market_time)
            return True
        
        return False
    
    def _activate_edge(self, market_time: datetime):
        """Activar el edge tras detectar pullback"""
        if self._current_bos:
            self._current_bos.pullback_detected_at = market_time
        self._state = EdgeState.EDGE_ACTIVE
        
        logger.info(f"🟢 EDGE ACTIVE: Ventana de entrada abierta "
                   f"(máx {self.EDGE_ACTIVE_TIMEOUT_MIN} min, "
                   f"máx {self.MAX_TRADES_PER_EDGE} trades)")
    
    # =========================================================================
    # EXPIRACIÓN DEL EDGE
    # =========================================================================
    
    def check_expiration(self, market_time: datetime) -> bool:
        """
        Verifica si el edge debe expirar.
        
        Returns: True si expiró
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False
        
        if self._current_bos is None:
            return False
        
        bos = self._current_bos
        
        # Timeout en ARMED
        if self._state == EdgeState.EDGE_ARMED:
            armed_duration = (market_time - bos.timestamp).total_seconds() / 60
            if armed_duration > self.EDGE_ARMED_TIMEOUT_MIN:
                self._expire_edge("TIMEOUT: Sin pullback")
                return True
        
        # Timeout en ACTIVE
        elif self._state == EdgeState.EDGE_ACTIVE:
            if bos.pullback_detected_at:
                active_duration = (market_time - bos.pullback_detected_at).total_seconds() / 60
                if active_duration > self.EDGE_ACTIVE_TIMEOUT_MIN:
                    self._expire_edge("TIMEOUT: Ventana de entrada cerrada")
                    return True
        
        # Edge quemado por intentos
        if bos.is_burned():
            self._expire_edge(f"BURNED: {bos.trades_attempted} intentos, {bos.trades_lost} pérdidas")
            return True
        
        return False
    
    def _expire_edge(self, reason: str):
        """Expirar el edge actual"""
        if self._current_bos:
            self._current_bos.expired_at = datetime.utcnow()
            self._bos_history.append(self._current_bos)
            
            # Mantener solo últimos 50 BOS
            if len(self._bos_history) > 50:
                self._bos_history = self._bos_history[-50:]
        
        self._state = EdgeState.EDGE_EXPIRED
        logger.info(f"⏹️ EDGE EXPIRED: {reason}")
    
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
        
        LONG: Invalidado si precio cae por debajo de BOS level
        SHORT: Invalidado si precio sube por encima de BOS level
        
        Returns: True si se invalidó
        """
        if self._state not in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            return False
        
        if self._current_bos is None:
            return False
        
        bos = self._current_bos
        invalidation_buffer = atr * 0.2  # Buffer pequeño para evitar ruido
        
        if bos.direction == 'LONG':
            if current_price < (bos.bos_level - invalidation_buffer):
                self._expire_edge(f"INVALIDATED: Precio {current_price:.2f} < BOS {bos.bos_level:.2f}")
                return True
        
        elif bos.direction == 'SHORT':
            if current_price > (bos.bos_level + invalidation_buffer):
                self._expire_edge(f"INVALIDATED: Precio {current_price:.2f} > BOS {bos.bos_level:.2f}")
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
        """
        Registra resultado de trade.
        Actualiza stats del BOS actual y globales.
        """
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
        
        logger.info(f"📊 Trade recorded: {'WIN' if won else 'LOSS'} {r_captured:+.2f}R | "
                   f"BOS trades: {bos.trades_attempted}/{self.MAX_TRADES_PER_EDGE} | "
                   f"Expectancy/BOS: {self._stats.get_expectancy_per_bos():.3f}R")
        
        # Verificar si edge está quemado
        if bos.is_burned():
            self._expire_edge(f"Máximo trades alcanzado ({bos.trades_attempted})")
    
    # =========================================================================
    # ESTADO Y DEBUG
    # =========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        """Estado actual para logging/debug"""
        status = {
            'state': self._state.value,
            'can_evaluate': self.can_evaluate_entry(),
            'can_trade': self.can_trade(),
            'block_reason': self.get_block_reason()
        }
        
        if self._current_bos:
            bos = self._current_bos
            status['current_bos'] = {
                'direction': bos.direction,
                'bos_level': bos.bos_level,
                'trades_attempted': bos.trades_attempted,
                'trades_remaining': self.MAX_TRADES_PER_EDGE - bos.trades_attempted,
                'is_burned': bos.is_burned()
            }
        
        status['stats'] = self._stats.to_dict()
        
        return status
    
    def reset(self):
        """Reset para nuevo día o manual"""
        self._state = EdgeState.NO_EDGE
        self._current_bos = None
        logger.info("🔄 EdgeStateManager reset")
    
    def force_expire(self, reason: str = "Manual expiration"):
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
