"""
Entry Quality Gate - Hard Rules That Invalidate Trades Before Score

🔥 FIX (Jan 22, 2026): Three Layer Entry System

LAYER 1: GATE (Hard Rules) - This module
- Microtrend contradiction >= 85% → BLOCK
- RANGE + chase_atr > 1.3 → BLOCK for FULL, allow PROBE only with extra confirmations
- RANGE + chase_atr > 1.0 → NO market, require limit/pullback
- Session poor + ADX < 15 → REDUCE size 50%
- BOS invalidation in MANAGE → trigger exit/reduce

LAYER 2: SCORE (Soft Ranking) - entry_scorer.py
- Decides if trade is PROBE/REDUCED/FULL
- Applies penalties and bonuses

LAYER 3: EXECUTION (Timing) - execution.py
- Market vs Limit decision
- Pullback logic
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class GateDecision(Enum):
    """Gate decision outcomes"""
    ALLOW = "ALLOW"           # Full entry allowed
    PROBE_ONLY = "PROBE_ONLY" # Only probe entry (25% size) with tight SL
    LIMIT_ONLY = "LIMIT_ONLY" # No market, must use limit
    REDUCE_SIZE = "REDUCE_SIZE"  # Reduce size by factor
    BLOCK = "BLOCK"           # Hard block - no entry


@dataclass
class GateResult:
    """Result of Entry Quality Gate check"""
    decision: GateDecision
    reason: str
    
    # Modifiers when not blocked
    max_size_multiplier: float = 1.0  # Cap size at this factor
    require_limit: bool = False        # Force limit order (no market)
    sl_multiplier: float = 1.0         # Modify SL (e.g., 0.8 = 20% tighter)
    require_confirmations: int = 0     # Extra confirmations needed (RSI divergence, etc.)
    
    # Additional context
    microtrend_blocked: bool = False
    chase_blocked: bool = False
    session_blocked: bool = False


class EntryQualityGate:
    """
    Hard rules that invalidate trades BEFORE scoring.
    
    Philosophy: The score decides "which trade is better", but the gate
    decides "if any trade is allowed at all".
    
    Usage:
        gate = EntryQualityGate()
        result = gate.check(
            regime='RANGE',
            chase_atr=1.5,
            micro_trend='BULLISH',
            micro_strength=94,
            entry_direction='SHORT',
            ...
        )
        if result.decision == GateDecision.BLOCK:
            return  # Don't enter
    """
    
    # =========================================================================
    # RULE THRESHOLDS (tunable)
    # =========================================================================
    
    # Microtrend contradiction
    # 🔥 REFACTOR (Feb 05, 2026): Raised thresholds - MicroTrend should not veto easily
    MICROTREND_BLOCK_STRENGTH = 90  # Was 85 → only block at 90%+ (overwhelming evidence)
    MICROTREND_PROBE_STRENGTH = 80  # Was 70 → raised to 80%
    
    # Chase in RANGE
    RANGE_CHASE_BLOCK_FULL = 1.3     # > 1.3 ATR in RANGE = block FULL entry
    RANGE_CHASE_REQUIRE_LIMIT = 1.0  # > 1.0 ATR in RANGE = require limit, no market
    
    # Session quality
    WEAK_SESSION_ADX_THRESHOLD = 15  # ADX < 15 + poor session = reduce
    WEAK_SESSION_SIZE_FACTOR = 0.5   # Reduce to 50% size
    
    # PROBE requirements in RANGE
    RANGE_PROBE_MAX_SL_ATR = 1.6     # PROBE in RANGE: SL max 1.6x ATR
    RANGE_FULL_MAX_SL_ATR = 2.0      # FULL in RANGE: SL max 2.0x ATR
    
    def __init__(self):
        self._last_gate_reason: str = ""
    
    def check(
        self,
        regime: str,
        chase_atr: float,
        micro_trend: str,  # 'BULLISH', 'BEARISH', 'NEUTRAL'
        micro_strength: float,  # 0-100
        entry_direction: str,  # 'LONG' or 'SHORT'
        session_quality: str,  # 'good', 'moderate', 'poor'
        htf_adx: float,
        has_rsi_divergence: bool = False,
        near_vpoc: bool = False,
        near_anchor_level: bool = False,
        # 🔥 NEW (Feb 05, 2026): Edge context - MicroTrend should NOT veto active edges
        edge_active: bool = False,
        edge_type: str | None = None,  # 'DISPLACEMENT', 'BREAKOUT', 'TREND', 'RANGE'
    ) -> GateResult:
        """
        Check all hard rules and return gate decision.
        
        Order of checks:
        1. Microtrend contradiction (hardest block)
           🔥 FIX: If edge is active, MicroTrend can only WARN, not BLOCK.
        2. Chase in RANGE (block/limit)
        3. Session + ADX (size reduction)
        
        Returns GateResult with decision and modifiers.
        """
        
        # =====================================================================
        # RULE 1: MICROTREND CONTRADICTION KILL-SWITCH
        # If microtrend contradicts entry with >= 90% strength → BLOCK
        # 🔥 EXCEPTION (Feb 05, 2026): If edge is active, MicroTrend CANNOT block.
        # Rationale: The edge has "sovereign authority". MicroTrend is lagging.
        # Displacement/Breakout/Trend edges detected direction BEFORE MicroTrend caught up.
        # =====================================================================
        micro_contradicts = self._microtrend_contradicts(micro_trend, entry_direction)
        
        # 🔥 NEW (Feb 05, 2026): If edge is active, MicroTrend CANNOT hard block
        # It can only reduce size, not veto entirely
        can_hard_block = not edge_active
        
        if micro_contradicts and micro_strength >= self.MICROTREND_BLOCK_STRENGTH:
            # Check for special confirmations that might allow probe entry
            has_extra_confirmations = (
                (has_rsi_divergence and near_anchor_level) or  # RSI divergence + level
                (near_vpoc and near_anchor_level)               # VPOC + anchor confluence
            )
            
            # 🔥 NEW: Edge active = treat as if we have confirmations
            if edge_active:
                logger.info(
                    f"🛡️ EDGE PROTECTS: Microtrend {micro_strength:.0f}% contradiction BUT "
                    f"edge {edge_type or 'ACTIVE'} has sovereign authority - allowing with size reduction"
                )
                return GateResult(
                    decision=GateDecision.PROBE_ONLY,  # Allow but reduced size
                    reason=f"Microtrend contradiction {micro_strength:.0f}% but edge {edge_type} active - reduced size",
                    max_size_multiplier=0.50,  # 50% size (not 10%)
                    sl_multiplier=0.85,        # Slightly tighter SL
                    require_limit=False,       # Allow market order if needed
                    microtrend_blocked=False,  # Not really blocked
                )
            elif has_extra_confirmations:
                # Allow micro-probe with very tight SL
                logger.warning(
                    f"⚠️ GATE: Microtrend contradiction {micro_strength:.0f}% BUT "
                    f"has confirmations - allowing micro-probe"
                )
                return GateResult(
                    decision=GateDecision.PROBE_ONLY,
                    reason=f"Microtrend {micro_trend} contradicts {entry_direction} at {micro_strength:.0f}% - micro-probe only",
                    max_size_multiplier=0.1,  # 10% size (micro-probe)
                    sl_multiplier=0.7,         # 30% tighter SL
                    require_limit=True,
                    microtrend_blocked=True,
                )
            else:
                # HARD BLOCK - no entry (only if no edge active)
                logger.warning(
                    f"⛔ GATE BLOCK: Microtrend {micro_trend} contradicts {entry_direction} "
                    f"at {micro_strength:.0f}% >= {self.MICROTREND_BLOCK_STRENGTH}%"
                )
                return GateResult(
                    decision=GateDecision.BLOCK,
                    reason=f"Microtrend contradiction >= {self.MICROTREND_BLOCK_STRENGTH}%: {micro_trend} vs {entry_direction}",
                    microtrend_blocked=True,
                )
        
        elif micro_contradicts and micro_strength >= self.MICROTREND_PROBE_STRENGTH:
            # 80-90%: Allow probe only (or full if edge active)
            if edge_active:
                logger.info(
                    f"🛡️ EDGE PROTECTS: Microtrend {micro_strength:.0f}% moderate contradiction - "
                    f"edge {edge_type} allows full entry with caution"
                )
                return GateResult(
                    decision=GateDecision.ALLOW_FULL,
                    reason=f"Microtrend contradiction {micro_strength:.0f}% but edge active",
                    max_size_multiplier=0.70,  # 70% size
                )
            logger.info(
                f"⚠️ GATE: Microtrend contradiction {micro_strength:.0f}% - probe only"
            )
            return GateResult(
                decision=GateDecision.PROBE_ONLY,
                reason=f"Microtrend {micro_strength:.0f}% contradiction - probe only",
                max_size_multiplier=0.25,
                sl_multiplier=0.85,
                microtrend_blocked=True,
            )
        
        # =====================================================================
        # RULE 2: CHASE IN RANGE - GATE
        # RANGE + chase > 1.3 ATR → block FULL, probe only
        # RANGE + chase > 1.0 ATR → require limit (no market)
        # =====================================================================
        is_range = regime.upper() in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']
        
        if is_range:
            if chase_atr > self.RANGE_CHASE_BLOCK_FULL:
                # Block full entry - probe only with confirmations
                if has_rsi_divergence or near_vpoc:
                    logger.info(
                        f"⚠️ GATE: RANGE + chase {chase_atr:.2f} ATR > {self.RANGE_CHASE_BLOCK_FULL} "
                        f"- probe only (has confirmations)"
                    )
                    return GateResult(
                        decision=GateDecision.PROBE_ONLY,
                        reason=f"RANGE + high chase ({chase_atr:.1f} ATR) - probe only",
                        max_size_multiplier=0.25,
                        sl_multiplier=0.8,  # Tighter SL for chased probe
                        require_limit=True,
                        chase_blocked=True,
                    )
                else:
                    logger.warning(
                        f"⛔ GATE BLOCK: RANGE + chase {chase_atr:.2f} ATR > {self.RANGE_CHASE_BLOCK_FULL} "
                        f"without confirmations"
                    )
                    return GateResult(
                        decision=GateDecision.BLOCK,
                        reason=f"RANGE + high chase ({chase_atr:.1f} ATR) without confirmations",
                        chase_blocked=True,
                    )
            
            elif chase_atr > self.RANGE_CHASE_REQUIRE_LIMIT:
                # Allow but require limit order (no market)
                logger.info(
                    f"📊 GATE: RANGE + chase {chase_atr:.2f} ATR > {self.RANGE_CHASE_REQUIRE_LIMIT} "
                    f"- require limit order"
                )
                return GateResult(
                    decision=GateDecision.LIMIT_ONLY,
                    reason=f"RANGE + chase ({chase_atr:.1f} ATR) - limit only, no market",
                    require_limit=True,
                    chase_blocked=True,
                )
        
        # =====================================================================
        # RULE 3: SESSION QUALITY + ADX - SIZE REDUCTION
        # Poor session + ADX < 15 → reduce size 50%
        # 🔄 REFACTORED Feb 04: Use SessionQuality for consistent checks
        # =====================================================================
        from src.config.trading_config import SessionQuality
        if SessionQuality.is_quality_blocked(session_quality) and htf_adx < self.WEAK_SESSION_ADX_THRESHOLD:
            logger.info(
                f"📊 GATE: Poor session + ADX {htf_adx:.1f} < {self.WEAK_SESSION_ADX_THRESHOLD} "
                f"- reduce size to {self.WEAK_SESSION_SIZE_FACTOR:.0%}"
            )
            return GateResult(
                decision=GateDecision.REDUCE_SIZE,
                reason=f"Poor session + low ADX ({htf_adx:.1f}) - size reduced",
                max_size_multiplier=self.WEAK_SESSION_SIZE_FACTOR,
                session_blocked=True,
            )
        
        # =====================================================================
        # ALL GATES PASSED - ALLOW ENTRY
        # =====================================================================
        return GateResult(
            decision=GateDecision.ALLOW,
            reason="All quality gates passed",
        )
    
    def _microtrend_contradicts(self, micro_trend: str, entry_direction: str) -> bool:
        """Check if microtrend contradicts entry direction"""
        if micro_trend == 'NEUTRAL':
            return False
        
        # BULLISH micro + SHORT entry = contradiction
        # BEARISH micro + LONG entry = contradiction
        if entry_direction.upper() == 'SHORT' and micro_trend.upper() == 'BULLISH':
            return True
        if entry_direction.upper() == 'LONG' and micro_trend.upper() == 'BEARISH':
            return True
        
        return False
    
    def get_sl_multiplier_for_regime(self, regime: str, is_probe: bool) -> float:
        """
        Get SL ATR multiplier based on regime and entry type.
        
        RANGE:
        - FULL: max 2.0x ATR
        - PROBE: max 1.6x ATR
        
        TREND:
        - FULL: 3.0x-3.5x ATR (normal)
        - PROBE: 2.5x ATR
        """
        is_range = regime.upper() in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']
        
        if is_range:
            if is_probe:
                return self.RANGE_PROBE_MAX_SL_ATR
            else:
                return self.RANGE_FULL_MAX_SL_ATR
        else:
            # TREND regime - normal multipliers
            if is_probe:
                return 2.5
            else:
                return 3.0


# Singleton pattern
_entry_quality_gate: Optional[EntryQualityGate] = None


def get_entry_quality_gate() -> EntryQualityGate:
    """Get or create Entry Quality Gate singleton"""
    global _entry_quality_gate
    if _entry_quality_gate is None:
        _entry_quality_gate = EntryQualityGate()
    return _entry_quality_gate
