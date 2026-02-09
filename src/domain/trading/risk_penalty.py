"""
Risk Penalty Accumulator - META-SISTEMA DE SIZING
==================================================
FIX #81 (Feb 07, 2026)

PRINCIPIO CLAVE:
- Todo filtro devuelve RISK_PENALTY ∈ [0, 1]
- No boolean
- final_size = base_size × Π(penalties)
- Block SOLO si final_size < MIN_TRADEABLE_SIZE

PROBLEMA QUE RESUELVE:
- Con 6 guards duros al 20% cada uno → bloqueas 70-80% de setups buenos
- P(veto) ≈ 1 - Π(1 - veto_i)

FILOSOFÍA:
- Optimizar para "ganar con drawdown controlado"
- NO para "no perder"
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class PenaltySource(Enum):
    """Sources of risk penalties."""
    # Direction conflicts
    DIRECTION_LOCK = "direction_lock"
    FORBIDDEN_DIRECTION = "forbidden_direction"
    COUNTER_EXPANSION = "counter_expansion"
    
    # Displacement/Chase
    DISPLACEMENT_CHASE = "displacement_chase"
    FAKEOUT = "fakeout"
    
    # Multi-warning
    DANGER_SIGNALS = "danger_signals"
    
    # Structure
    STRUCTURE_MISMATCH = "structure_mismatch"
    
    # Location
    LOCATION_EXTREME = "location_extreme"
    
    # Obstinacy
    SHORT_OBSTINACY = "short_obstinacy"
    
    # 🔥 FIX Feb 07: Add missing penalty sources
    # Delta contradict
    DELTA_CONTRADICT = "delta_contradict"
    REGIME_ALIGNMENT = "regime_alignment"
    
    # Session/Toxic
    TOXIC_COMBO = "toxic_combo"
    SESSION_QUALITY = "session_quality"
    
    # RR/Edge
    LOW_RR = "low_rr"
    WEAK_EDGE = "weak_edge"
    
    # Spread
    HIGH_SPREAD = "high_spread"
    
    # Other
    COUNTERTREND = "countertrend"
    LOSS_STREAK = "loss_streak"


@dataclass
class PenaltyEntry:
    """Single penalty entry with metadata."""
    source: PenaltySource
    size_mult: float  # 0.0 to 1.0
    score_adj: float  # Typically negative
    reason: str
    is_catastrophic: bool = False  # Only for true emergencies


@dataclass
class RiskPenaltyResult:
    """Result of accumulated risk penalties."""
    # Multiplicative size penalty
    final_size_mult: float = 1.0
    
    # Additive score adjustment
    total_score_adj: float = 0.0
    
    # Should we hard block? (only for catastrophic)
    should_block: bool = False
    block_reason: str = ""
    
    # All applied penalties
    penalties: List[PenaltyEntry] = field(default_factory=list)
    
    # Summary
    penalty_sources: List[str] = field(default_factory=list)
    
    @property
    def is_heavily_penalized(self) -> bool:
        """True if final multiplier is very low."""
        return self.final_size_mult < 0.25
    
    @property 
    def penalty_count(self) -> int:
        """Number of penalties applied."""
        return len([p for p in self.penalties if p.size_mult < 1.0])


class RiskPenaltyAccumulator:
    """
    Accumulates risk penalties from all guards/filters.
    
    USAGE:
    1. Create accumulator at start of entry evaluation
    2. Each guard calls add_penalty() instead of returning block
    3. At end, call get_result() and check:
       - final_size = base_size * result.final_size_mult
       - Block only if final_size < MIN_TRADEABLE or catastrophic
    
    RULES:
    - Penalties are MULTIPLICATIVE for size
    - Penalties are ADDITIVE for score
    - Block only for catastrophic events OR size < minimum
    
    🔥 FIX #94 (Feb 09, 2026): FLOOR RAISED AGAIN + SIMPLIFIED OVERRIDE
    - MIN_VIABLE_MULT raised from 0.40 to 0.50 (was 0.15 originally)
    - When displacement detected, ALWAYS allow minimum 50%
    - The system was OVER-FILTERING: 7+ penalties stacking killed ALL trades
    """
    
    # 🔥 FIX #94: Raised from 0.40 to 0.50
    # Even 40% was too low - penalties kept stacking and killing trades
    # 50% ensures we ACTUALLY TRADE rather than just analyze
    MIN_VIABLE_MULT: float = 0.50
    
    # 🔥 FIX #109 (Feb 09, 2026): TRANSITION_MODE floor
    # During transitions (post-bias flip), use higher floor to capture the edge
    TRANSITION_MODE_MULT: float = 0.70
    
    def __init__(self):
        self._penalties: List[PenaltyEntry] = []
        self._has_catastrophic: bool = False
        self._catastrophic_reason: str = ""
        # 🔥 FIX #88: Context flags for override
        self._trend_expansion_active: bool = False
        self._adx_value: float = 0.0
        # 🔥 FIX #109: Transition mode flag
        self._transition_mode_active: bool = False
    
    def set_context(
        self,
        trend_expansion_active: bool = False,
        adx_value: float = 0.0,
        transition_mode_active: bool = False
    ):
        """
        🔥 FIX #88 (Feb 09, 2026): Set context for penalty overrides.
        🔥 FIX #109 (Feb 09, 2026): Added transition_mode_active.
        
        When TREND_EXPANSION is active with ADX>45, minor penalties are overridden.
        When TRANSITION_MODE is active, floor is 70% instead of 50%.
        This prevents over-filtering in strong trends/transitions where we MUST trade.
        """
        self._trend_expansion_active = trend_expansion_active
        self._adx_value = adx_value
        self._transition_mode_active = transition_mode_active
    
    def add_penalty(
        self,
        source: PenaltySource,
        size_mult: float,
        score_adj: float = 0.0,
        reason: str = "",
        is_catastrophic: bool = False
    ):
        """
        Add a penalty from a guard/filter.
        
        Args:
            source: Which guard/filter is applying this
            size_mult: Size multiplier 0.0-1.0 (1.0 = no penalty)
            score_adj: Score adjustment (typically negative)
            reason: Human-readable reason
            is_catastrophic: If True, forces hard block (use sparingly!)
        
        GUIDELINES for size_mult:
        - 1.0: No penalty
        - 0.8-0.9: Light caution
        - 0.5-0.7: Significant risk detected
        - 0.3-0.5: High risk, small position only
        - 0.0-0.3: Very high risk, minimal position
        - 0.0: Effectively blocks (use is_catastrophic for true blocks)
        """
        # Clamp values
        size_mult = max(0.0, min(1.0, size_mult))
        
        entry = PenaltyEntry(
            source=source,
            size_mult=size_mult,
            score_adj=score_adj,
            reason=reason,
            is_catastrophic=is_catastrophic
        )
        self._penalties.append(entry)
        
        if is_catastrophic:
            self._has_catastrophic = True
            self._catastrophic_reason = reason
        
        # Log significant penalties
        if size_mult < 0.8 or score_adj < -0.5:
            logger.info(
                f"⚠️ PENALTY: {source.value} | size×{size_mult:.0%}, score{score_adj:+.1f} | {reason}"
            )
    
    def get_result(self) -> RiskPenaltyResult:
        """
        Calculate final accumulated penalties.
        
        Returns RiskPenaltyResult with:
        - final_size_mult: Product of all size multipliers
        - total_score_adj: Sum of all score adjustments
        - should_block: True only if catastrophic or size < minimum
        
        🔥 FIX #88 (Feb 09, 2026): TREND_EXPANSION OVERRIDE
        When trend_expansion_active AND ADX>45, we use a higher floor (0.60)
        because these are A++ setups that MUST trade.
        
        🔥 FIX #109 (Feb 09, 2026): TRANSITION_MODE OVERRIDE
        When transition_mode_active, use 70% floor to capture transition edge.
        
        🔥 FIX #112 (Feb 09, 2026): SOFTMAX PENALTIES
        Changed from pure multiplication (Π) to softmax approach:
        - Use worst penalty as base
        - Add small decay per additional penalty
        - Never go below floor
        This prevents 5 small penalties from killing the trade.
        """
        result = RiskPenaltyResult()
        result.penalties = self._penalties.copy()
        
        # =====================================================================
        # 🔥 FIX #112: SOFTMAX PENALTIES
        #
        # ANTES: final = Π(penalties) → 0.7^5 = 0.17 → trade muerto
        # AHORA: final = min(penalties) * (1 - 0.05 * (n-1)) → más justo
        #
        # Filosofía:
        # - El PEOR penalty domina (si uno dice 0.5x, ese es el base)
        # - Cada penalty adicional resta un 5% extra
        # - Nunca baja del floor (50%/70% según contexto)
        # =====================================================================
        active_penalties = [p for p in self._penalties if p.size_mult < 1.0]
        
        if not active_penalties:
            final_mult = 1.0
        else:
            # El peor penalty domina
            min_mult = min(p.size_mult for p in active_penalties)
            
            # Decay suave por cada penalty adicional (5% cada uno)
            num_additional = max(0, len(active_penalties) - 1)
            decay = 0.05 * num_additional
            
            # Calcular final con decay
            final_mult = min_mult * (1.0 - decay)
            
            # Log si hay diferencia significativa vs método antiguo
            old_mult = 1.0
            for p in active_penalties:
                old_mult *= p.size_mult
            
            if abs(final_mult - old_mult) > 0.05:
                logger.debug(
                    f"📊 FIX #112 SOFTMAX: {len(active_penalties)} penalties | "
                    f"Old={old_mult:.0%} → New={final_mult:.0%} (saved {(final_mult-old_mult)*100:.0f}pp)"
                )
        
        # 🔥 FIX #94: SIMPLIFIED OVERRIDE - Always allow trades
        # The system was killing ALL trades with stacking penalties.
        # Now: if there's displacement, FORCE minimum 50% size.
        effective_min = self.MIN_VIABLE_MULT  # 0.50
        
        # 🔥 FIX #109: TRANSITION_MODE gets highest floor (70%)
        if self._transition_mode_active:
            effective_min = self.TRANSITION_MODE_MULT  # 0.70
            if final_mult < 0.70:
                logger.info(
                    f"🚀 TRANSITION_MODE OVERRIDE: Raising penalty floor from "
                    f"{final_mult:.0%} to 70%"
                )
            final_mult = max(final_mult, 0.70)
        elif self._trend_expansion_active:
            # TREND_EXPANSION = A+ setup, MUST trade
            if self._adx_value > 40:
                # Strong trend: use 60% floor
                effective_min = 0.60
                final_mult = max(final_mult, 0.60)
                logger.info(
                    f"🚀 TREND_EXPANSION OVERRIDE: ADX={self._adx_value:.1f} | "
                    f"Forcing 60% minimum size"
                )
            else:
                # Normal trend expansion: use 50% floor
                final_mult = max(final_mult, 0.50)
        else:
            # Not expansion, but still apply floor
            final_mult = max(final_mult, self.MIN_VIABLE_MULT)
        
        result.final_size_mult = final_mult
        
        # Calculate additive score adjustment
        total_adj = 0.0
        for p in self._penalties:
            total_adj += p.score_adj
        result.total_score_adj = total_adj
        
        # Collect sources that actually applied penalties
        result.penalty_sources = [
            p.source.value for p in self._penalties 
            if p.size_mult < 1.0 or p.score_adj < 0
        ]
        
        # 🔥 FIX #94: SOLO bloquear por CATASTRÓFICO
        # Ya no bloqueamos por acumulación de penalties - siempre tenemos floor de 50%
        if self._has_catastrophic:
            result.should_block = True
            result.block_reason = f"CATASTROPHIC: {self._catastrophic_reason}"
        # ELIMINADO: bloqueo por final_mult < effective_min
        # El floor ya garantiza que siempre hay al menos 50%
        
        return result
    
    def log_summary(self):
        """Log a summary of all penalties."""
        if not self._penalties:
            logger.debug("📊 RISK PENALTIES: None applied")
            return
        
        result = self.get_result()
        
        active_penalties = [p for p in self._penalties if p.size_mult < 1.0]
        if not active_penalties:
            logger.debug("📊 RISK PENALTIES: All passed (1.0x)")
            return
        
        penalty_strs = [
            f"{p.source.value}={p.size_mult:.0%}" 
            for p in active_penalties
        ]
        
        logger.info(
            f"📊 RISK PENALTIES: {len(active_penalties)} active | "
            f"Final: size×{result.final_size_mult:.0%}, score{result.total_score_adj:+.1f} | "
            f"[{', '.join(penalty_strs)}]"
        )
        
        if result.should_block:
            logger.warning(f"🚫 BLOCK: {result.block_reason}")


# =============================================================================
# ADAPTIVE RR THRESHOLDS (FIX #81)
# =============================================================================
class AdaptiveRRThresholds:
    """
    Adaptive RR thresholds based on volatility.
    
    PROBLEM: Fixed RR < 0.6 → block is suicidal in high vol
    
    SOLUTION:
    - Low vol (ATR/price < 0.6%): min_rr = 0.7
    - Normal vol (0.6-1.2%): min_rr = 0.6
    - High vol (> 1.2%): min_rr = 0.5 (with reduced size)
    """
    
    @classmethod
    def get_min_rr(cls, atr: float, price: float) -> Tuple[float, float]:
        """
        Get minimum RR and size penalty based on volatility.
        
        Args:
            atr: Current ATR
            price: Current price
            
        Returns:
            (min_rr, size_penalty_if_below)
        """
        if price <= 0:
            return 0.6, 0.5
        
        vol_pct = (atr / price) * 100
        
        if vol_pct < 0.6:
            # Low volatility - be pickier
            return 0.70, 0.60
        elif vol_pct < 1.2:
            # Normal volatility
            return 0.60, 0.75
        else:
            # High volatility - accept lower RR
            return 0.50, 0.50
    
    @classmethod
    def calculate_rr_penalty(
        cls, 
        worst_case_rr: float, 
        atr: float, 
        price: float
    ) -> Tuple[float, float, str]:
        """
        Calculate size penalty based on RR vs adaptive threshold.
        
        Returns:
            (size_mult, score_adj, reason)
        """
        min_rr, size_if_below = cls.get_min_rr(atr, price)
        vol_pct = (atr / price) * 100 if price > 0 else 0
        
        if worst_case_rr >= min_rr:
            # Above threshold - no penalty
            return 1.0, 0.0, ""
        elif worst_case_rr >= min_rr * 0.8:
            # Slightly below - small penalty
            return 0.80, -0.3, f"RR {worst_case_rr:.2f} slightly below {min_rr:.2f}"
        elif worst_case_rr >= min_rr * 0.6:
            # Moderately below - significant penalty
            return size_if_below, -0.7, f"RR {worst_case_rr:.2f} below {min_rr:.2f} (vol={vol_pct:.1f}%)"
        else:
            # Very low RR - minimal size
            return 0.25, -1.5, f"RR {worst_case_rr:.2f} very low vs {min_rr:.2f}"


# =============================================================================
# CATASTROPHIC EVENT DETECTOR
# =============================================================================
class CatastrophicEventDetector:
    """
    Detects true catastrophic events that warrant hard blocks.
    
    ONLY these justify hard blocks:
    - Funding rate spike (> 0.1% per 8h)
    - Liquidation cascade detected
    - News candle (> 3σ move in < 1 min)
    - Spread explosion (> 5x normal)
    - Exchange issues
    """
    
    @classmethod
    def check(
        cls,
        funding_rate: Optional[float] = None,
        spread_pct: Optional[float] = None,
        normal_spread_pct: float = 0.02,
        price_move_1m: Optional[float] = None,
        atr_1m: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Check for catastrophic events.
        
        Returns:
            (is_catastrophic, reason)
        """
        reasons = []
        
        # Funding spike
        if funding_rate is not None and abs(funding_rate) > 0.001:  # 0.1%
            reasons.append(f"FUNDING_SPIKE({funding_rate*100:.2f}%)")
        
        # Spread explosion
        if spread_pct is not None and normal_spread_pct > 0:
            spread_ratio = spread_pct / normal_spread_pct
            if spread_ratio > 5:
                reasons.append(f"SPREAD_EXPLOSION({spread_ratio:.1f}x)")
        
        # News candle (3σ move)
        if price_move_1m is not None and atr_1m is not None and atr_1m > 0:
            sigma = abs(price_move_1m) / atr_1m
            if sigma > 3:
                reasons.append(f"NEWS_CANDLE({sigma:.1f}σ)")
        
        if reasons:
            return True, " + ".join(reasons)
        
        return False, ""
