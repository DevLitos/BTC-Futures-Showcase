"""
Adaptive Entry System - January 24, 2026

Este módulo implementa un sistema de entrada adaptativo que:
1. Ajusta umbrales dinámicamente según condiciones de mercado
2. Usa un sistema de "trade credits" en lugar de score binario
3. Prioriza entries según calidad relativa (no absoluta)
4. Implementa "minimum activity guarantee"

FILOSOFÍA:
- En modo DISCOVERY, necesitamos DATA no protección
- Un trade malo con buen sizing es información valiosa
- Un trade rechazado es oportunidad de aprendizaje perdida
- El edge viene de la gestión (SL/TP/sizing), no de filtrar excesivamente
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum
import numpy as np
import logging

logger = logging.getLogger(__name__)


class MarketCondition(Enum):
    """Condición de mercado actual"""
    DEAD = "dead"           # BB < 0.3%, ADX < 15 → mercado muerto
    LOW_VOL = "low_vol"     # BB 0.3-0.5%, ADX 15-20 → baja volatilidad
    NORMAL = "normal"       # BB 0.5-1.0%, ADX 20-30 → mercado normal
    HIGH_VOL = "high_vol"   # BB 1.0-2.0%, ADX 30-45 → alta volatilidad
    EXTREME = "extreme"     # BB > 2% or ADX > 45 → extremo


@dataclass
class AdaptiveThresholds:
    """Umbrales adaptativos según condición de mercado"""
    min_score: float           # Score mínimo para entrar
    probe_max_score: float     # Score máximo para probe
    full_score: float          # Score mínimo para full size
    chase_tolerance_atr: float # ATR de chase permitido
    rr_floor: float           # RR mínimo aceptable
    size_cap: float           # Cap máximo de sizing
    
    @classmethod
    def for_condition(cls, condition: MarketCondition) -> 'AdaptiveThresholds':
        """Devuelve umbrales adaptativos para la condición dada"""
        configs = {
            MarketCondition.DEAD: cls(
                min_score=2.5,       # Muy permisivo - cualquier señal vale
                probe_max_score=3.5, # Todo es probe
                full_score=5.5,      # Nunca full size en mercado muerto
                chase_tolerance_atr=2.0,  # No hay chase en mercado muerto
                rr_floor=0.4,        # RR bajo aceptable
                size_cap=0.25        # Solo probe size
            ),
            MarketCondition.LOW_VOL: cls(
                min_score=3.0,       # Permisivo
                probe_max_score=4.0,
                full_score=5.0,
                chase_tolerance_atr=1.5,
                rr_floor=0.5,
                size_cap=0.5
            ),
            MarketCondition.NORMAL: cls(
                min_score=3.5,       # Normal
                probe_max_score=4.5,
                full_score=5.5,
                chase_tolerance_atr=1.0,
                rr_floor=0.6,
                size_cap=1.0
            ),
            MarketCondition.HIGH_VOL: cls(
                min_score=4.0,       # Más exigente
                probe_max_score=5.0,
                full_score=6.0,
                chase_tolerance_atr=0.8,
                rr_floor=0.8,
                size_cap=0.75
            ),
            MarketCondition.EXTREME: cls(
                min_score=5.0,       # Muy exigente
                probe_max_score=6.0,
                full_score=7.0,
                chase_tolerance_atr=0.5,
                rr_floor=1.0,
                size_cap=0.5
            ),
        }
        return configs.get(condition, configs[MarketCondition.NORMAL])


class TradeQueue:
    """
    Cola de trades candidatos con priorización por calidad relativa.
    
    En lugar de rechazar trades por score absoluto, los encolamos
    y ejecutamos el mejor disponible cada N minutos.
    """
    def __init__(self, max_queue_size: int = 5, selection_interval_minutes: int = 15):
        self.max_size = max_queue_size
        self.selection_interval = timedelta(minutes=selection_interval_minutes)
        self._queue: List[Dict] = []
        self._last_selection: datetime = None
    
    def add_candidate(self, 
                      direction: str,
                      price: float,
                      score: float,
                      rr: float,
                      tags: Dict,
                      timestamp: datetime) -> bool:
        """
        Añade un candidato a la cola.
        
        Returns:
            True si el candidato fue añadido, False si fue rechazado
        """
        candidate = {
            'direction': direction,
            'price': price,
            'score': score,
            'rr': rr,
            'tags': tags,
            'timestamp': timestamp,
            # Score combinado: score base + bonus por RR
            'combined_score': score + min(rr, 2.0) * 0.5
        }
        
        # Si la cola está llena, reemplazar el peor candidato si este es mejor
        if len(self._queue) >= self.max_size:
            worst = min(self._queue, key=lambda x: x['combined_score'])
            if candidate['combined_score'] > worst['combined_score']:
                self._queue.remove(worst)
                self._queue.append(candidate)
                logger.info(f"📋 Queue: Replaced worst candidate (score {worst['combined_score']:.1f}) with better (score {candidate['combined_score']:.1f})")
                return True
            return False
        
        self._queue.append(candidate)
        logger.info(f"📋 Queue: Added candidate {direction} @ {price:.0f}, score={score:.1f}, rr={rr:.2f}")
        return True
    
    def should_select(self, current_time: datetime) -> bool:
        """Determina si es momento de seleccionar un trade"""
        if self._last_selection is None:
            return len(self._queue) > 0
        
        time_since_last = current_time - self._last_selection
        return time_since_last >= self.selection_interval and len(self._queue) > 0
    
    def select_best(self, current_time: datetime) -> Optional[Dict]:
        """
        Selecciona el mejor candidato de la cola.
        
        Returns:
            El mejor candidato o None si la cola está vacía
        """
        if not self._queue:
            return None
        
        # Filtrar candidatos muy viejos (>30 min)
        max_age = timedelta(minutes=30)
        self._queue = [c for c in self._queue if current_time - c['timestamp'] < max_age]
        
        if not self._queue:
            return None
        
        # Seleccionar el mejor por combined_score
        best = max(self._queue, key=lambda x: x['combined_score'])
        self._queue.remove(best)
        self._last_selection = current_time
        
        logger.info(f"📋 Queue: Selected best candidate {best['direction']} @ {best['price']:.0f} (combined_score={best['combined_score']:.1f})")
        return best
    
    def clear(self):
        """Limpia la cola"""
        self._queue = []
    
    @property
    def size(self) -> int:
        return len(self._queue)


class MinimumActivityGuard:
    """
    Garantiza un mínimo de actividad de trading.
    
    Si no hay trades en X horas, fuerza la relajación de filtros
    hasta conseguir al menos un trade.
    """
    
    # Niveles de relajación
    LEVELS = {
        0: {'hours': 0, 'score_reduction': 0, 'penalty_mult': 1.0, 'name': 'NORMAL'},
        1: {'hours': 3, 'score_reduction': 0.5, 'penalty_mult': 0.8, 'name': 'MILD'},
        2: {'hours': 5, 'score_reduction': 1.0, 'penalty_mult': 0.6, 'name': 'MODERATE'},
        3: {'hours': 7, 'score_reduction': 1.5, 'penalty_mult': 0.4, 'name': 'SIGNIFICANT'},
        4: {'hours': 9, 'score_reduction': 2.0, 'penalty_mult': 0.2, 'name': 'SEVERE'},
    }
    
    def __init__(self):
        self._last_trade_time: Optional[datetime] = None
        self._current_level: int = 0
    
    def record_trade(self, timestamp: datetime):
        """Registra un trade y resetea el nivel"""
        self._last_trade_time = timestamp
        self._current_level = 0
    
    def update(self, current_time: datetime) -> Tuple[int, float, float]:
        """
        Actualiza el nivel de relajación basado en tiempo sin trades.
        
        Returns:
            (level, score_reduction, penalty_multiplier)
        """
        if self._last_trade_time is None:
            hours_since_trade = 24  # Assume worst case
        else:
            hours_since_trade = (current_time - self._last_trade_time).total_seconds() / 3600
        
        # Determinar nivel
        new_level = 0
        for level, config in sorted(self.LEVELS.items(), reverse=True):
            if hours_since_trade >= config['hours']:
                new_level = level
                break
        
        # Log cambio de nivel
        if new_level != self._current_level:
            old_name = self.LEVELS[self._current_level]['name']
            new_name = self.LEVELS[new_level]['name']
            logger.info(f"🚨 Activity Guard: {old_name} → {new_name} ({hours_since_trade:.1f}h since last trade)")
            self._current_level = new_level
        
        config = self.LEVELS[new_level]
        return new_level, config['score_reduction'], config['penalty_mult']
    
    @property
    def level(self) -> int:
        return self._current_level
    
    @property
    def level_name(self) -> str:
        return self.LEVELS[self._current_level]['name']


class AdaptiveEntrySystem:
    """
    Sistema de entrada adaptativo que combina:
    1. Umbrales adaptativos según condición de mercado
    2. Cola de trades con priorización
    3. Garantía de actividad mínima
    """
    
    def __init__(self):
        self.activity_guard = MinimumActivityGuard()
        self.trade_queue = TradeQueue()
        self._current_condition: MarketCondition = MarketCondition.NORMAL
        self._current_thresholds: AdaptiveThresholds = AdaptiveThresholds.for_condition(MarketCondition.NORMAL)
    
    def classify_market_condition(self, 
                                   bb_width_pct: float,
                                   adx: float) -> MarketCondition:
        """
        Clasifica la condición de mercado actual.
        
        Args:
            bb_width_pct: Bollinger Bands width como porcentaje
            adx: ADX actual
        """
        # BB Width como indicador primario de volatilidad
        if bb_width_pct < 0.3:
            vol_level = 0  # Dead
        elif bb_width_pct < 0.5:
            vol_level = 1  # Low
        elif bb_width_pct < 1.0:
            vol_level = 2  # Normal
        elif bb_width_pct < 2.0:
            vol_level = 3  # High
        else:
            vol_level = 4  # Extreme
        
        # ADX como confirmación
        if adx < 15:
            adx_level = 0
        elif adx < 20:
            adx_level = 1
        elif adx < 30:
            adx_level = 2
        elif adx < 45:
            adx_level = 3
        else:
            adx_level = 4
        
        # Tomar el más conservador de los dos
        combined_level = max(vol_level, adx_level)
        
        conditions = [
            MarketCondition.DEAD,
            MarketCondition.LOW_VOL,
            MarketCondition.NORMAL,
            MarketCondition.HIGH_VOL,
            MarketCondition.EXTREME
        ]
        
        condition = conditions[min(combined_level, 4)]
        
        if condition != self._current_condition:
            logger.info(f"📊 Market condition: {self._current_condition.value} → {condition.value} (BB={bb_width_pct:.2f}%, ADX={adx:.1f})")
            self._current_condition = condition
            self._current_thresholds = AdaptiveThresholds.for_condition(condition)
        
        return condition
    
    def get_effective_thresholds(self, current_time: datetime) -> AdaptiveThresholds:
        """
        Obtiene umbrales efectivos considerando:
        1. Condición de mercado
        2. Nivel de garantía de actividad
        """
        base = self._current_thresholds
        level, score_reduction, penalty_mult = self.activity_guard.update(current_time)
        
        # Aplicar relajación
        return AdaptiveThresholds(
            min_score=max(2.0, base.min_score - score_reduction),
            probe_max_score=max(2.5, base.probe_max_score - score_reduction),
            full_score=max(3.5, base.full_score - score_reduction),
            chase_tolerance_atr=base.chase_tolerance_atr / penalty_mult if penalty_mult > 0 else 3.0,
            rr_floor=max(0.3, base.rr_floor * penalty_mult),
            size_cap=min(1.0, base.size_cap * (1 + (1 - penalty_mult)))
        )
    
    def evaluate_entry(self,
                       score: float,
                       rr: float,
                       chase_atr: float,
                       current_time: datetime,
                       tags: Dict) -> Tuple[bool, float, str]:
        """
        Evalúa si un entry debe ejecutarse.
        
        Returns:
            (should_enter, size_multiplier, reason)
        """
        thresholds = self.get_effective_thresholds(current_time)
        
        # Log thresholds for debugging
        logger.debug(f"📊 Adaptive thresholds: min={thresholds.min_score:.1f}, "
                     f"probe_max={thresholds.probe_max_score:.1f}, "
                     f"chase_tol={thresholds.chase_tolerance_atr:.1f}ATR")
        
        # Check score floor
        if score < thresholds.min_score:
            return False, 0.0, f"Score {score:.1f} < adaptive floor {thresholds.min_score:.1f}"
        
        # Check RR floor
        if rr < thresholds.rr_floor:
            return False, 0.0, f"RR {rr:.2f} < adaptive floor {thresholds.rr_floor:.2f}"
        
        # Check chase tolerance
        if chase_atr > thresholds.chase_tolerance_atr:
            return False, 0.0, f"Chase {chase_atr:.2f}ATR > tolerance {thresholds.chase_tolerance_atr:.2f}ATR"
        
        # Determine size multiplier
        if score < thresholds.probe_max_score:
            size_mult = 0.25  # Probe
            reason = f"Probe entry (score={score:.1f})"
        elif score < thresholds.full_score:
            size_mult = 0.5   # Reduced
            reason = f"Reduced entry (score={score:.1f})"
        else:
            size_mult = 1.0   # Full
            reason = f"Full entry (score={score:.1f})"
        
        # Apply size cap
        size_mult = min(size_mult, thresholds.size_cap)
        
        return True, size_mult, reason
    
    def record_trade(self, timestamp: datetime):
        """Registra un trade ejecutado"""
        self.activity_guard.record_trade(timestamp)
        self.trade_queue.clear()


# Singleton
_adaptive_entry_system: Optional[AdaptiveEntrySystem] = None


def get_adaptive_entry_system() -> AdaptiveEntrySystem:
    """Obtiene la instancia singleton del sistema adaptativo"""
    global _adaptive_entry_system
    if _adaptive_entry_system is None:
        _adaptive_entry_system = AdaptiveEntrySystem()
    return _adaptive_entry_system


def reset_adaptive_entry_system():
    """Reset del sistema para testing"""
    global _adaptive_entry_system
    _adaptive_entry_system = None
