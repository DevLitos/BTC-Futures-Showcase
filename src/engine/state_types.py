"""
State Types - Single source of truth for strategy state definitions.

This module is the CANONICAL definition for StrategyState and related types.
All other modules MUST import from here, never define their own.

IMPORTANT: StrategyStateMachine is the ONLY component that manages state transitions.
EdgeValidator and other modules ONLY report metrics/signals - they do NOT manage state.
"""

from enum import Enum
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Optional


# ============================================================================
# EXECUTION INTEGRITY STATES (CAPA 1)
# ============================================================================

class OrderState(Enum):
    """
    State of an individual order in the execution pipeline.
    
    OPEN: Order submitted, waiting for fill
    FILLED: Order fully filled
    PARTIAL: Order partially filled (qty mismatch)
    CANCELED: Order canceled (by system or user)
    LOST: Order disappeared from exchange (timeout without confirmation)
    REJECTED: Order rejected by exchange
    """
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    LOST = "lost"
    REJECTED = "rejected"


class PositionState(Enum):
    """
    State of a position being managed.
    
    FLAT: No position (or position fully closed)
    PENDING: Entry order placed, waiting for fill
    OPEN: Position confirmed on exchange with matching qty
    PARTIAL: Position size doesn't match expected (partial fill)
    CLOSING: Exit in progress (SL/TP triggered or manual close)
    INCONSISTENT: System/exchange mismatch detected
    """
    FLAT = "flat"
    PENDING = "pending"
    OPEN = "open"
    PARTIAL = "partial"
    CLOSING = "closing"
    INCONSISTENT = "inconsistent"


# ============================================================================
# STRATEGY STATES
# ============================================================================

class StrategyState(Enum):
    """
    Operational states of the strategy.
    
    SINGLE SOURCE OF TRUTH - import from here, don't redefine.
    
    States:
        ACTIVE: Normal operation (100% position size)
        DEGRADED: Reduced operation (50% position size)
        OBSERVATION: Post-alert monitoring (50% position size, 14 days)
        DEAD: Archived permanently (0% position size)
    """
    ACTIVE = "operating"
    DEGRADED = "reduced_size_50pct"
    OBSERVATION = "post_alert_monitoring"
    DEAD = "archived"


@dataclass
class StateTransition:
    """Records a state transition event"""
    from_state: StrategyState
    to_state: StrategyState
    timestamp: datetime
    reason: str
    metrics_snapshot: dict


# Position size multipliers by state
STATE_SIZE_MULTIPLIERS = {
    StrategyState.ACTIVE: 1.0,
    StrategyState.DEGRADED: 0.5,
    StrategyState.OBSERVATION: 0.5,
    StrategyState.DEAD: 0.0,
}


def get_state_size_multiplier(state: StrategyState) -> float:
    """
    Get position size multiplier for a given state.
    
    This is the CANONICAL source for state → size mapping.
    EdgeScore also provides size multipliers, but STATE takes priority.
    
    Priority order:
    1. If state is DEAD: 0.0 (no trading)
    2. If state is DEGRADED/OBSERVATION: 0.5 (state-based reduction)
    3. If state is ACTIVE: Use EdgeScore multiplier (edge-based scaling)
    
    Returns:
        1.0 = ACTIVE (full size, may be scaled by edge score)
        0.5 = DEGRADED/OBSERVATION (fixed reduction)
        0.0 = DEAD (no trading)
    """
    return STATE_SIZE_MULTIPLIERS.get(state, 1.0)


def can_trade_in_state(state: StrategyState) -> bool:
    """Returns True if trading is allowed in this state."""
    return state != StrategyState.DEAD


# ============================================================================
# SIZING PRIORITY DOCUMENTATION
# ============================================================================
#
# There are TWO sources of position size adjustment:
#
# 1. StrategyState (via StrategyStateMachine):
#    - Based on degradation signals and recovery
#    - Multipliers: 1.0 (ACTIVE), 0.5 (DEGRADED/OBS), 0.0 (DEAD)
#    - This is the PRIMARY control - it can BLOCK trading entirely
#
# 2. EdgeScore (via ActivityRegimeAnalyzer):
#    - Based on current market activity/edge quality
#    - Multipliers: 0.0, 0.5, 0.75, 1.0 based on edge score
#    - This is SECONDARY - only applies when state allows trading
#
# COMBINED SIZING (using min for safety):
#    final_multiplier = min(state_multiplier, edge_multiplier)
#    
#    Examples:
#    - ACTIVE + edge_score 0.7 = min(1.0, 0.75) = 0.75
#    - DEGRADED + edge_score 0.7 = min(0.5, 0.75) = 0.5
#    - ACTIVE + edge_score 0.3 = min(1.0, 0.5) = 0.5
#
# ============================================================================


def compute_final_position_multiplier(state_multiplier: float, edge_multiplier: float) -> float:
    """
    Compute final position size multiplier from state and edge inputs.
    
    POLICY: Uses min() for safety - the more conservative multiplier wins.
    
    This ensures:
    - State can always limit size (DEGRADED caps at 0.5)
    - Edge can always limit size (poor edge caps at 0.5 or less)
    - DEAD state always returns 0.0 regardless of edge
    
    Args:
        state_multiplier: From StrategyStateMachine (0.0, 0.5, or 1.0)
        edge_multiplier: From EdgeScore.get_size_multiplier() (0.0-1.0)
        
    Returns:
        Final multiplier (0.0 to 1.0)
    """
    # DEAD state always blocks trading
    if state_multiplier == 0.0:
        return 0.0
    
    # Use min() - the more conservative value wins
    return min(state_multiplier, edge_multiplier)
#    - DEAD + any edge = 0.0 * any = 0.0
#
# ============================================================================
