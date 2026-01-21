"""
State Module - State machines and state types

Reorganized from engine/ (Jan 20, 2026)

Components:
- StateMachine: Bot state management
- BotState: Enum for bot operational states
- State types: Order, Position, Strategy states
"""

from src.engine.state.state_machine import (
    StateMachine,
    BotState,
)
from src.engine.state.state_types import (
    OrderState,
    PositionState,
    StrategyState,
    StateTransition,
    get_state_size_multiplier,
    can_trade_in_state,
    compute_final_position_multiplier,
)

__all__ = [
    'StateMachine',
    'BotState',
    'OrderState',
    'PositionState',
    'StrategyState',
    'StateTransition',
    'get_state_size_multiplier',
    'can_trade_in_state',
    'compute_final_position_multiplier',
]
