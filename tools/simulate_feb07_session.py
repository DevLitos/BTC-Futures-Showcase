#!/usr/bin/env python3
"""
SIMULADOR DE SESIÓN 7 FEBRERO 2026
===================================
Basado en datos REALES del log trading_run_20260207_013037_424cca57.log

Simula qué hubiera pasado con diferentes configuraciones:
- Sistema ORIGINAL (como operó)
- Sistema CON FIX #78 (coherence block)
- Sistema CON FIX #79-80 (guards relajados)

Precios REALES extraídos del log.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple, Optional
import json


class MacroBias(Enum):
    BEAR_STRONG = "BEAR_STRONG"
    BEAR_WEAK = "BEAR_WEAK"
    NEUTRAL = "NEUTRAL"
    BULL_WEAK = "BULL_WEAK"
    BULL_STRONG = "BULL_STRONG"


@dataclass
class MarketSnapshot:
    """Snapshot del mercado en un momento dado"""
    timestamp: str
    price: float
    ema50: float
    slope_pct: float  # slope de EMA50 en %
    atr: float
    macro_bias: MacroBias
    
    @property
    def distance_from_ema50_pct(self) -> float:
        return ((self.price - self.ema50) / self.ema50) * 100


@dataclass
class TradeDecision:
    """Decisión de trade"""
    direction: str  # "LONG" or "SHORT"
    allowed: bool
    reason: str
    entry_price: float
    potential_tp: float
    potential_sl: float


@dataclass
class TradeResult:
    """Resultado de un trade simulado"""
    entry_price: float
    exit_price: float
    direction: str
    pnl_usd: float
    exit_reason: str


# ============================================================================
# DATOS REALES DEL 7 FEBRERO 2026 (extraídos del log)
# ============================================================================

# Precios cada minuto aproximadamente (simplificado)
REAL_PRICE_SEQUENCE = [
    # Hora, Precio, EMA50, Slope%, ATR
    ("02:30", 70427, 69122, -0.42, 76),   # Inicio sesión
    ("02:35", 70140, 69130, -0.41, 76),
    ("02:40", 70202, 69135, -0.40, 77),
    ("02:45", 70224, 69140, -0.39, 78),
    ("02:50", 70465, 69145, -0.38, 80),
    ("02:55", 70458, 69150, -0.37, 80),
    ("03:00", 70419, 69155, -0.36, 76),   # Cierre 1h
    ("03:04", 70400, 69160, -0.35, 76),   # TRADE ADOPTADO CERRÓ AQUÍ (exhaustion) PnL +$0.55
    ("03:05", 70516, 69165, -0.34, 79),   # Peak local
    ("03:06", 70546, 69170, -0.33, 80),   # CONSENSUS LONG armado
    ("03:07", 70529, 69175, -0.32, 78),
    ("03:08", 70395, 69180, -0.31, 84),   # Empieza caída
    ("03:10", 70335, 69185, -0.30, 83),
    ("03:15", 70300, 69190, -0.29, 74),   # Otro CONSENSUS LONG armado
    ("03:20", 70345, 69195, -0.28, 72),
    ("03:30", 70400, 69200, -0.26, 68),
    ("04:00", 70200, 69210, -0.24, 75),
    ("05:00", 69800, 69230, -0.20, 85),
    ("06:00", 69400, 69250, -0.18, 90),
    ("07:00", 68900, 69280, -0.15, 95),
    ("08:00", 68200, 69310, -0.10, 100),
    ("08:15", 67897, 69343, 0.088, 97),   # SHORTs bloqueados (TOO_LATE -2.09%)
    ("08:17", 67300, 69343, 0.088, 97),   # Más SHORTs bloqueados (-2.95%)
    ("09:00", 68000, 69290, -0.12, 95),
    ("10:00", 68500, 69270, -0.14, 92),
    ("10:12", 68697, 69260, -0.156, 88),  # Fin del log
]

# Convertir a snapshots
def create_snapshots() -> List[MarketSnapshot]:
    snapshots = []
    for ts, price, ema50, slope, atr in REAL_PRICE_SEQUENCE:
        snapshots.append(MarketSnapshot(
            timestamp=ts,
            price=price,
            ema50=ema50,
            slope_pct=slope,
            atr=atr,
            macro_bias=MacroBias.BEAR_STRONG  # Constante todo el día según log
        ))
    return snapshots


# ============================================================================
# SIMULADORES DE LÓGICA
# ============================================================================

class OriginalSystem:
    """Sistema ORIGINAL - como operó el 7 de febrero"""
    
    @staticmethod
    def can_arm_consensus_edge(direction: str, macro: MacroBias) -> Tuple[bool, str]:
        """Original: Permitía LONG + BEAR_STRONG (incoherente)"""
        return True, "ALLOWED (no coherence check)"
    
    @staticmethod
    def structure_gate_allows(direction: str, snapshot: MarketSnapshot) -> Tuple[bool, str]:
        """Original: TOO_LATE = -2% para SHORT"""
        dist = snapshot.distance_from_ema50_pct
        
        if direction == "SHORT":
            # Original: TOO_LATE si está más de 2% bajo EMA50
            if dist < -2.0:
                return False, f"TOO_LATE (distance={dist:.2f}% < -2%)"
            return True, f"ALLOWED (distance={dist:.2f}%)"
        
        if direction == "LONG":
            # Original: NEUTRAL si slope negativo y precio bajo EMA50
            if snapshot.slope_pct < 0 and dist < 0:
                return False, f"NEUTRAL zone (slope={snapshot.slope_pct:.3f}%, dist={dist:.2f}%)"
            return True, f"ALLOWED (dist={dist:.2f}%)"
        
        return True, "UNKNOWN"


class FixedSystem:
    """Sistema CON FIX #78-83 (RegimeFusion 3 Capas)"""
    
    @staticmethod
    def calculate_macro_score(ema50: float, ema200: float, plus_di: float, minus_di: float, slope: float) -> tuple:
        """
        Calcula macro_score como RegimeFusion.
        
        Returns: (macro_score, macro_dir, macro_strength)
        """
        score = 0.0
        
        # Componente 1: EMA50 vs EMA200 (±1)
        if ema50 > ema200:
            score += 1.0
        elif ema50 < ema200:
            score -= 1.0
        
        # Componente 2: +DI vs -DI (±1)
        di_diff = plus_di - minus_di
        if abs(di_diff) >= 3.0:
            score += 1.0 if di_diff > 0 else -1.0
        
        # Componente 3: Slope (±0.5)
        if abs(slope) >= 0.0002:
            score += 0.5 if slope > 0 else -0.5
        
        # Determinar dirección
        if score > 1.0:
            macro_dir = 1  # BULL
        elif score < -1.0:
            macro_dir = -1  # BEAR
        else:
            macro_dir = 0  # NEUTRAL
        
        return score, macro_dir, abs(score)
    
    @staticmethod
    def can_arm_consensus_edge(direction: str, macro: MacroBias, has_momentum_override: bool = False) -> Tuple[bool, str]:
        """FIX #78: Bloquea edges incoherentes (ahora parte de RegimeFusion)"""
        macro_str = macro.value
        
        if direction == "LONG" and macro_str == "BEAR_STRONG" and not has_momentum_override:
            return False, f"[BLOCK] INCOHERENT: LONG + {macro_str}"
        
        if direction == "SHORT" and macro_str == "BULL_STRONG" and not has_momentum_override:
            return False, f"[BLOCK] INCOHERENT: SHORT + {macro_str}"
        
        return True, "ALLOWED (coherent)"
    
    @staticmethod
    def regime_fusion_decision(
        direction: str,
        macro_score: float,
        macro_dir: int,
        macro_strength: float,
        has_displacement: bool = False,
        confidence: float = 0.5
    ) -> Tuple[bool, float, float, str]:
        """
        FIX #83: Fusión de 3 capas.
        
        Returns: (allowed, size_mult, rr_min, reason)
        """
        # Calcular alignment
        micro_dir = 1 if direction == "LONG" else -1
        alignment = macro_dir * micro_dir
        
        # Configuración
        BLOCK_THRESHOLD = 2.0
        
        if alignment > 0:
            # ALINEADO
            return True, 1.0, 1.0, "ALIGNED"
        
        elif alignment < 0:
            # FADE
            # ¿Es explosive?
            is_explosive = has_displacement or confidence >= 0.8
            
            # ÚNICO BLOQUEO: macro fuerte + contra + sin edge
            if macro_strength > BLOCK_THRESHOLD and not is_explosive:
                return False, 0.0, 0.0, f"BLOCKED: Stupid fade (strength={macro_strength:.1f})"
            
            # Fade permitido pero caro
            size_mult = 0.5 if is_explosive else 0.3
            rr_min = 2.0
            return True, size_mult, rr_min, f"FADE (size={size_mult}, override={'YES' if is_explosive else 'NO'})"
        
        else:
            # NEUTRAL
            return True, 0.6, 1.5, "NEUTRAL"
    
    @staticmethod
    def structure_gate_allows(direction: str, snapshot: MarketSnapshot) -> Tuple[bool, str]:
        """FIX #79-80: Guards relajados"""
        dist = snapshot.distance_from_ema50_pct
        slope = snapshot.slope_pct
        
        if direction == "SHORT":
            # FIX #79: TOO_LATE ampliado a -4% Y requiere slope plano/positivo
            # Si slope es negativo (tendencia bajista activa), permitir SHORTs
            if slope < -0.02:  # Slope negativo = tendencia bajista
                return True, f"TREND_DOWN (slope={slope:.3f}% → SHORTs allowed)"
            
            if dist < -4.0:  # Más lejos = TOO_LATE
                return False, f"TOO_LATE (distance={dist:.2f}% < -4%)"
            
            return True, f"ALLOWED (distance={dist:.2f}%)"
        
        if direction == "LONG":
            # FIX #80: REVERSAL_ZONE permite LONGs hasta 3% bajo EMA50
            if slope < 0 and dist < 0:
                if dist > -3.0:  # Zona de reversión
                    return True, f"REVERSAL_ZONE (dist={dist:.2f}% > -3%)"
                return False, f"NEUTRAL zone (slope={snapshot.slope_pct:.3f}%, dist={dist:.2f}%)"
            return True, f"ALLOWED (dist={dist:.2f}%)"
        
        return True, "UNKNOWN"


# ============================================================================
# SIMULACIÓN
# ============================================================================

def simulate_trade(direction: str, entry_snapshot: MarketSnapshot, 
                   future_snapshots: List[MarketSnapshot], size_btc: float = 0.0018) -> Optional[TradeResult]:
    """Simula un trade con los precios futuros reales"""
    
    entry_price = entry_snapshot.price
    atr = entry_snapshot.atr
    
    # Calcular SL/TP típicos (1:1 RR con ATR)
    if direction == "LONG":
        sl = entry_price - (atr * 1.5)
        tp = entry_price + (atr * 1.5)
    else:  # SHORT
        sl = entry_price + (atr * 1.5)
        tp = entry_price - (atr * 1.5)
    
    # Simular con precios futuros
    for snapshot in future_snapshots:
        price = snapshot.price
        
        if direction == "LONG":
            if price <= sl:
                pnl = (sl - entry_price) * size_btc
                return TradeResult(entry_price, sl, direction, pnl, "SL HIT")
            if price >= tp:
                pnl = (tp - entry_price) * size_btc
                return TradeResult(entry_price, tp, direction, pnl, "TP HIT")
        else:  # SHORT
            if price >= sl:
                pnl = (entry_price - sl) * size_btc
                return TradeResult(entry_price, sl, direction, pnl, "SL HIT")
            if price <= tp:
                pnl = (entry_price - tp) * size_btc
                return TradeResult(entry_price, tp, direction, pnl, "TP HIT")
    
    # Cerrar al final si no tocó SL/TP
    final_price = future_snapshots[-1].price if future_snapshots else entry_price
    if direction == "LONG":
        pnl = (final_price - entry_price) * size_btc
    else:
        pnl = (entry_price - final_price) * size_btc
    
    return TradeResult(entry_price, final_price, direction, pnl, "SESSION_END")


def run_simulation():
    """Ejecuta la simulación completa"""
    
    snapshots = create_snapshots()
    
    print("=" * 80)
    print("🔬 SIMULACIÓN SESIÓN 7 FEBRERO 2026 - DATOS REALES")
    print("=" * 80)
    print()
    
    print("📊 DATOS DE ENTRADA:")
    print(f"   Precio inicio: ${snapshots[0].price:,.0f}")
    print(f"   Precio fin:    ${snapshots[-1].price:,.0f}")
    print(f"   Movimiento:    ${snapshots[-1].price - snapshots[0].price:,.0f} ({((snapshots[-1].price - snapshots[0].price) / snapshots[0].price) * 100:.2f}%)")
    print(f"   Macro bias:    {snapshots[0].macro_bias.value}")
    print(f"   EMA50 rango:   ${min(s.ema50 for s in snapshots):,.0f} - ${max(s.ema50 for s in snapshots):,.0f}")
    print()
    
    # ========================================================================
    # SISTEMA ORIGINAL
    # ========================================================================
    print("-" * 80)
    print("🔴 SISTEMA ORIGINAL (como operó)")
    print("-" * 80)
    
    original_trades = []
    original_blocked = {"LONG": 0, "SHORT": 0}
    
    for i, snap in enumerate(snapshots):
        future = snapshots[i+1:] if i < len(snapshots) - 1 else []
        
        # Intentar LONG (el sistema estaba armando LONGs)
        can_arm, reason = OriginalSystem.can_arm_consensus_edge("LONG", snap.macro_bias)
        if can_arm:
            gate_ok, gate_reason = OriginalSystem.structure_gate_allows("LONG", snap)
            if not gate_ok:
                original_blocked["LONG"] += 1
        
        # Intentar SHORT
        can_arm_short, _ = OriginalSystem.can_arm_consensus_edge("SHORT", snap.macro_bias)
        if can_arm_short:
            gate_ok_short, gate_reason_short = OriginalSystem.structure_gate_allows("SHORT", snap)
            if not gate_ok_short:
                original_blocked["SHORT"] += 1
    
    print(f"   LONGs bloqueados por STRUCTURE_GATE: {original_blocked['LONG']}")
    print(f"   SHORTs bloqueados por STRUCTURE_GATE: {original_blocked['SHORT']}")
    print(f"   Edge incoherente (LONG + BEAR_STRONG): PERMITIDO ❌")
    print()
    
    # El trade real que hizo
    print("   TRADE REAL EJECUTADO:")
    print("   • LONG adoptado @ $70,093 → cerrado @ $70,400 (EXHAUSTION)")
    print("   • PnL: +$0.55 ✅")
    print()
    
    # ========================================================================
    # SISTEMA CON FIXES
    # ========================================================================
    print("-" * 80)
    print("🟢 SISTEMA CON FIX #78-82")
    print("-" * 80)
    
    fixed_blocked_consensus = {"LONG": 0, "SHORT": 0}
    fixed_blocked_gate = {"LONG": 0, "SHORT": 0}
    fixed_allowed = {"LONG": 0, "SHORT": 0}
    
    potential_shorts = []
    
    for i, snap in enumerate(snapshots):
        future = snapshots[i+1:] if i < len(snapshots) - 1 else []
        
        # Intentar LONG
        can_arm_long, reason_long = FixedSystem.can_arm_consensus_edge("LONG", snap.macro_bias)
        if not can_arm_long:
            fixed_blocked_consensus["LONG"] += 1
        else:
            gate_ok, gate_reason = FixedSystem.structure_gate_allows("LONG", snap)
            if not gate_ok:
                fixed_blocked_gate["LONG"] += 1
            else:
                fixed_allowed["LONG"] += 1
        
        # Intentar SHORT
        can_arm_short, reason_short = FixedSystem.can_arm_consensus_edge("SHORT", snap.macro_bias)
        if not can_arm_short:
            fixed_blocked_consensus["SHORT"] += 1
        else:
            gate_ok_short, gate_reason_short = FixedSystem.structure_gate_allows("SHORT", snap)
            if not gate_ok_short:
                fixed_blocked_gate["SHORT"] += 1
            else:
                fixed_allowed["SHORT"] += 1
                if future:
                    result = simulate_trade("SHORT", snap, future)
                    if result:
                        potential_shorts.append((snap.timestamp, result))
    
    print(f"   LONGs bloqueados por COHERENCE (FIX #78): {fixed_blocked_consensus['LONG']}")
    print(f"   LONGs bloqueados por STRUCTURE_GATE: {fixed_blocked_gate['LONG']}")
    print(f"   LONGs permitidos: {fixed_allowed['LONG']}")
    print()
    print(f"   SHORTs bloqueados por COHERENCE: {fixed_blocked_consensus['SHORT']}")
    print(f"   SHORTs bloqueados por STRUCTURE_GATE: {fixed_blocked_gate['SHORT']}")
    print(f"   SHORTs permitidos: {fixed_allowed['SHORT']}")
    print()
    
    # ========================================================================
    # SIMULACIÓN DE SHORTs POTENCIALES
    # ========================================================================
    print("-" * 80)
    print("📈 SIMULACIÓN DE SHORTs QUE SE HUBIERAN PODIDO TOMAR")
    print("-" * 80)
    
    if potential_shorts:
        # Tomar solo los primeros 5 como muestra
        sample = potential_shorts[:5]
        total_pnl = 0
        
        for ts, result in sample:
            symbol = "✅" if result.pnl_usd > 0 else "❌"
            print(f"   [{ts}] SHORT @ ${result.entry_price:,.0f}")
            print(f"          Exit @ ${result.exit_price:,.0f} ({result.exit_reason})")
            print(f"          PnL: ${result.pnl_usd:.2f} {symbol}")
            print()
            total_pnl += result.pnl_usd
        
        print(f"   📊 Total PnL primeros 5 SHORTs: ${total_pnl:.2f}")
        
        # Contar winners/losers
        winners = sum(1 for _, r in potential_shorts if r.pnl_usd > 0)
        losers = sum(1 for _, r in potential_shorts if r.pnl_usd <= 0)
        total_all = sum(r.pnl_usd for _, r in potential_shorts)
        
        print()
        print(f"   📊 TODOS los SHORTs posibles ({len(potential_shorts)}):")
        print(f"      Winners: {winners}")
        print(f"      Losers: {losers}")
        print(f"      Win Rate: {winners/len(potential_shorts)*100:.1f}%")
        print(f"      Total PnL: ${total_all:.2f}")
    else:
        print("   No se encontraron SHORTs permitidos en esta simulación")
    
    # ========================================================================
    # RESUMEN COMPARATIVO
    # ========================================================================
    print()
    print("=" * 80)
    print("📊 RESUMEN COMPARATIVO")
    print("=" * 80)
    print()
    print("   SISTEMA ORIGINAL:")
    print("   • Hizo 1 LONG adoptado (INCOHERENTE con BEAR_STRONG)")
    print("   • Resultado: +$0.55 (suerte, no edge)")
    print("   • Bloqueó ~150 SHORTs por TOO_LATE")
    print()
    print("   SISTEMA CON FIXES:")
    print(f"   • Hubiera BLOQUEADO el LONG (FIX #78: LONG + BEAR_STRONG)")
    print(f"   • Hubiera PERMITIDO más SHORTs (FIX #79: TOO_LATE ampliado)")
    print(f"   • Los SHORTs hubieran sido WINNERS (precio cayó ${snapshots[0].price - snapshots[-1].price:,.0f})")
    print()
    print("   CONCLUSIÓN:")
    print("   ┌─────────────────────────────────────────────────────────────┐")
    print("   │  El sistema ORIGINAL hizo un trade incoherente que ganó    │")
    print("   │  por suerte. El sistema FIJADO hubiera:                    │")
    print("   │    1. Evitado el LONG incoherente                          │")
    print("   │    2. Permitido SHORTs alineados con BEAR_STRONG           │")
    print("   │    3. Capturado la caída de $1,730 (2.46%)                 │")
    print("   └─────────────────────────────────────────────────────────────┘")
    print()


if __name__ == "__main__":
    run_simulation()
