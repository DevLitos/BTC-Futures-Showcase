"""
🔥 REGIME FUSION - 3 Layer Architecture (Feb 07, 2026)
======================================================

Arquitectura institucional de 3 capas:

CAPA 1 - MACRO (Dirección Base):
    - EMA50 vs EMA200
    - +DI vs -DI
    - Slope de EMA50
    → macro_score ∈ [-2.5, +2.5]

CAPA 2 - MICRO (Timing):
    - Structure
    - Displacement
    - Delta
    - Absorption
    → micro_direction, micro_confidence

CAPA 3 - FUSIÓN:
    - alignment = macro_dir × micro_dir
    - NO IF, NO BLOCK → PESO
    
    alignment > 0  → size_mult=1.0, rr_min=1.0
    alignment < 0  → size_mult=0.3, rr_min=2.0
    alignment == 0 → size_mult=0.6, rr_min=1.5
    
CUÁNDO BLOQUEAR (único caso):
    macro_strength > 2.0 AND alignment < 0 AND no_displacement
    = "Fade estúpido" → BLOCK
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class MacroRegime(str, Enum):
    """Régimen macro basado en score"""
    BULL = "BULL"      # score > +1.0
    BEAR = "BEAR"      # score < -1.0
    NEUTRAL = "NEUTRAL"  # else


@dataclass
class RegimeState:
    """
    CAPA 1 - MACRO: Estado del régimen de mercado.
    Se recalcula cada tick/minuto - NO ES LENTO.
    """
    macro_score: float = 0.0          # -2.5 a +2.5
    macro_dir: int = 0                # -1 (BEAR), 0 (NEUTRAL), +1 (BULL)
    macro_strength: float = 0.0       # abs(macro_score) → 0 a 2.5
    
    # Componentes del score
    ema_component: float = 0.0        # EMA50 > EMA200? ±1
    di_component: float = 0.0         # +DI > -DI? ±1
    slope_component: float = 0.0      # slope(EMA50) > 0? ±0.5
    
    @property
    def regime(self) -> MacroRegime:
        if self.macro_score > 1.0:
            return MacroRegime.BULL
        elif self.macro_score < -1.0:
            return MacroRegime.BEAR
        return MacroRegime.NEUTRAL
    
    @property
    def is_strong(self) -> bool:
        """Macro fuerte = abs(score) > 2.0"""
        return self.macro_strength > 2.0


@dataclass
class MicroState:
    """
    CAPA 2 - MICRO: Estado de timing intra-session.
    Señales rápidas: structure, displacement, delta, absorption.
    """
    direction: str = "NEUTRAL"        # "LONG", "SHORT", "NEUTRAL"
    confidence: float = 0.0           # 0 a 1.0
    has_displacement: bool = False    # Evento explosivo detectado
    has_structure: bool = False       # BoS/ChoCH confirmado
    has_absorption: bool = False      # Absorción detectada
    delta_signal: str = "NEUTRAL"     # "BULLISH", "BEARISH", "NEUTRAL"
    
    @property
    def dir_value(self) -> int:
        """Dirección como int: +1 LONG, -1 SHORT, 0 NEUTRAL"""
        if self.direction == "LONG":
            return 1
        elif self.direction == "SHORT":
            return -1
        return 0
    
    @property
    def is_explosive(self) -> bool:
        """Hay evento explosivo que justifica override?"""
        return self.has_displacement or self.confidence >= 0.8


@dataclass
class TradeDecision:
    """
    CAPA 3 - FUSIÓN: Decisión final de trading.
    Resultado de fusionar MACRO + MICRO.
    """
    allowed: bool = True
    size_mult: float = 1.0            # 0.3 a 1.0
    rr_min: float = 1.0               # RR mínimo requerido
    alignment: float = 0.0            # -1 a +1 (negativo = contra-trend)
    
    # Razón de la decisión
    reason: str = ""
    is_fade: bool = False             # True si estamos "fading" el macro
    is_blocked: bool = False          # True solo para fade estúpido
    
    # Override info
    override_type: Optional[str] = None  # "DISPLACEMENT", "STRUCTURE", etc.
    
    # Debug
    tags: dict = field(default_factory=dict)


class RegimeFusion:
    """
    Fusionador de Régimen + Micro para decisiones de trading.
    
    PRINCIPIO CLAVE:
        No bloquear → hacer caro equivocarte.
        Solo bloquear "fade estúpido" (macro fuerte + contra + sin edge).
    """
    
    # =========================================================================
    # CONFIGURACIÓN
    # =========================================================================
    
    # Umbrales para cálculo de macro_score
    SLOPE_STRONG_THRESHOLD = 0.0002   # Slope de EMA50 considerado fuerte
    DI_MIN_DIFF = 3.0                 # Diferencia mínima +DI/-DI para contar
    
    # Reglas de fusión
    ALIGNED_SIZE_MULT = 1.0           # Alineado con macro
    ALIGNED_RR_MIN = 1.0
    
    MISALIGNED_SIZE_MULT = 0.3        # Contra macro (fade)
    MISALIGNED_RR_MIN = 2.0
    
    NEUTRAL_SIZE_MULT = 0.6           # Macro neutral
    NEUTRAL_RR_MIN = 1.5
    
    # Umbral para bloqueo absoluto
    BLOCK_THRESHOLD = 2.0             # Solo bloquear si macro_strength > 2.0
    BLOCK_CONFIDENCE_THRESHOLD = 0.5  # Y micro_conf < 0.5 (user suggestion)
    
    def __init__(self):
        self._regime_state = RegimeState()
        self._micro_state = MicroState()
        self._last_decision = TradeDecision()
    
    # =========================================================================
    # CAPA 1: CÁLCULO DE MACRO REGIME
    # =========================================================================
    
    def update_macro(
        self,
        ema50: float,
        ema200: float,
        plus_di: float,
        minus_di: float,
        ema50_slope: float,
    ) -> RegimeState:
        """
        Actualiza el estado MACRO del régimen.
        Se debe llamar cada minuto o en cada tick relevante.
        
        Args:
            ema50: Valor actual de EMA50 (1h)
            ema200: Valor actual de EMA200 (1h)
            plus_di: +DI actual
            minus_di: -DI actual
            ema50_slope: Pendiente de EMA50 (primera derivada)
        
        Returns:
            RegimeState actualizado
        """
        state = RegimeState()
        
        # Componente 1: EMA50 vs EMA200 (±1)
        if ema50 > ema200:
            state.ema_component = 1.0
        elif ema50 < ema200:
            state.ema_component = -1.0
        else:
            state.ema_component = 0.0
        
        # Componente 2: +DI vs -DI (±1)
        di_diff = plus_di - minus_di
        if abs(di_diff) >= self.DI_MIN_DIFF:
            if di_diff > 0:
                state.di_component = 1.0
            else:
                state.di_component = -1.0
        else:
            state.di_component = 0.0
        
        # Componente 3: Slope de EMA50 (±0.5)
        if abs(ema50_slope) >= self.SLOPE_STRONG_THRESHOLD:
            if ema50_slope > 0:
                state.slope_component = 0.5
            else:
                state.slope_component = -0.5
        else:
            state.slope_component = 0.0
        
        # Calcular score total: [-2.5, +2.5]
        state.macro_score = (
            state.ema_component + 
            state.di_component + 
            state.slope_component
        )
        
        # Dirección y fuerza
        if state.macro_score > 1.0:
            state.macro_dir = 1
        elif state.macro_score < -1.0:
            state.macro_dir = -1
        else:
            state.macro_dir = 0
        
        state.macro_strength = abs(state.macro_score)
        
        self._regime_state = state
        return state
    
    # =========================================================================
    # CAPA 2: CÁLCULO DE MICRO STATE
    # =========================================================================
    
    def update_micro(
        self,
        direction: str,
        confidence: float,
        has_displacement: bool = False,
        has_structure: bool = False,
        has_absorption: bool = False,
        delta_signal: str = "NEUTRAL",
    ) -> MicroState:
        """
        Actualiza el estado MICRO de timing.
        Se llama cuando hay señal de entrada potencial.
        
        Args:
            direction: "LONG", "SHORT", o "NEUTRAL"
            confidence: Confianza en la señal (0-1)
            has_displacement: True si hay movimiento explosivo
            has_structure: True si hay BoS/ChoCH confirmado
            has_absorption: True si hay absorción detectada
            delta_signal: "BULLISH", "BEARISH", o "NEUTRAL"
        
        Returns:
            MicroState actualizado
        """
        state = MicroState(
            direction=direction,
            confidence=confidence,
            has_displacement=has_displacement,
            has_structure=has_structure,
            has_absorption=has_absorption,
            delta_signal=delta_signal,
        )
        self._micro_state = state
        return state
    
    # =========================================================================
    # CAPA 3: FUSIÓN → TRADE DECISION
    # =========================================================================
    
    def fuse(self, micro_direction: Optional[str] = None) -> TradeDecision:
        """
        Fusiona MACRO + MICRO para producir TradeDecision.
        
        LÓGICA INSTITUCIONAL:
        1. alignment = macro_dir × micro_dir
        2. alignment > 0  → full size, normal RR
        3. alignment < 0  → reduced size, high RR (caro equivocarte)
        4. alignment == 0 → medium size, medium RR
        
        ÚNICO BLOQUEO:
        macro_strength > 2.0 AND alignment < 0 AND no_displacement
        = "Fade estúpido sin edge" → BLOCK
        
        Args:
            micro_direction: Opcional, override de dirección micro
        
        Returns:
            TradeDecision con allowed, size_mult, rr_min, etc.
        """
        macro = self._regime_state
        micro = self._micro_state
        
        # Usar override si se proporciona
        if micro_direction:
            micro_dir_val = 1 if micro_direction == "LONG" else (-1 if micro_direction == "SHORT" else 0)
        else:
            micro_dir_val = micro.dir_value
        
        # =====================================================================
        # CÁLCULO DE ALIGNMENT
        # =====================================================================
        alignment = macro.macro_dir * micro_dir_val
        
        decision = TradeDecision()
        decision.alignment = alignment
        decision.tags['macro_score'] = macro.macro_score
        decision.tags['macro_dir'] = macro.macro_dir
        decision.tags['macro_strength'] = macro.macro_strength
        decision.tags['micro_dir'] = micro_dir_val
        decision.tags['micro_confidence'] = micro.confidence
        decision.tags['has_displacement'] = micro.has_displacement
        
        # =====================================================================
        # REGLAS DE FUSIÓN (NO IF-BLOCK, SOLO PESOS)
        # =====================================================================
        
        if alignment > 0:
            # ALINEADO: Micro y Macro van en la misma dirección
            decision.size_mult = self.ALIGNED_SIZE_MULT
            decision.rr_min = self.ALIGNED_RR_MIN
            decision.reason = f"ALIGNED (macro={macro.regime.value}, micro={micro.direction})"
            decision.is_fade = False
            
        elif alignment < 0:
            # MISALIGNED: Micro contra Macro (fade)
            decision.size_mult = self.MISALIGNED_SIZE_MULT  # 0.3
            decision.rr_min = max(self.MISALIGNED_RR_MIN, 1.8)  # Al menos 1.8
            decision.reason = f"FADE (macro={macro.regime.value}, micro={micro.direction})"
            decision.is_fade = True
            decision.tags['counter_weak'] = not micro.is_explosive
            
            # ========== ÚNICO BLOQUEO ==========
            # Solo bloquear si:
            # 1. Macro MUY fuerte (score > 2.0)
            # 2. Micro confianza BAJA (< 0.5) - más permisivo que antes
            # 3. Sin displacement
            is_stupid_fade = (
                macro.macro_strength > self.BLOCK_THRESHOLD and 
                micro.confidence < self.BLOCK_CONFIDENCE_THRESHOLD and
                not micro.has_displacement
            )
            
            if is_stupid_fade:
                decision.allowed = False
                decision.is_blocked = True
                decision.reason = (
                    f"BLOCKED: Stupid fade (macro_str={macro.macro_strength:.1f} > {self.BLOCK_THRESHOLD}, "
                    f"micro_conf={micro.confidence:.2f} < {self.BLOCK_CONFIDENCE_THRESHOLD}, no displacement)"
                )
                logger.warning(
                    f"☠️ FADE ESTÚPIDO BLOQUEADO: macro={macro.regime.value} "
                    f"(score={macro.macro_score:.1f}), micro={micro.direction}, "
                    f"conf={micro.confidence:.2f}"
                )
            else:
                # Fade permitido pero caro
                if micro.is_explosive:
                    decision.override_type = "DISPLACEMENT" if micro.has_displacement else "HIGH_CONFIDENCE"
                    decision.size_mult = 0.5  # Un poco más de size con edge
                    decision.tags['counter_weak'] = False
                    logger.debug(  # Changed to debug - too noisy
                        f"🌊 FADE PERMITIDO CON EDGE: {decision.override_type} | "
                        f"size={decision.size_mult}, rr_min={decision.rr_min}"
                    )
        else:
            # NEUTRAL: Macro o Micro es neutral
            decision.size_mult = self.NEUTRAL_SIZE_MULT
            decision.rr_min = self.NEUTRAL_RR_MIN
            decision.reason = f"NEUTRAL (macro={macro.regime.value}, micro={micro.direction})"
            decision.is_fade = False
        
        self._last_decision = decision
        return decision
    
    # =========================================================================
    # HELPER: Todo en uno
    # =========================================================================
    
    def evaluate_entry(
        self,
        # Macro inputs
        ema50: float,
        ema200: float,
        plus_di: float,
        minus_di: float,
        ema50_slope: float,
        # Micro inputs
        trade_direction: str,
        confidence: float,
        has_displacement: bool = False,
        has_structure: bool = False,
    ) -> TradeDecision:
        """
        Evaluación completa de entrada en una llamada.
        
        Actualiza macro, micro, y produce decisión fusionada.
        """
        # Actualizar macro
        self.update_macro(ema50, ema200, plus_di, minus_di, ema50_slope)
        
        # Actualizar micro
        self.update_micro(
            direction=trade_direction,
            confidence=confidence,
            has_displacement=has_displacement,
            has_structure=has_structure,
        )
        
        # Fusionar y decidir
        return self.fuse()
    
    # =========================================================================
    # GETTERS
    # =========================================================================
    
    @property
    def regime_state(self) -> RegimeState:
        return self._regime_state
    
    @property
    def micro_state(self) -> MicroState:
        return self._micro_state
    
    @property
    def last_decision(self) -> TradeDecision:
        return self._last_decision
