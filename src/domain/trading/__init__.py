"""
Trading Domain - Core trading entities and business logic.

Clean Architecture Layer: DOMAIN
- Contains core trading entities and value objects
- Pure business logic for trading decisions
- No infrastructure dependencies

Migration Status: ALIAS (re-exports from src.core and src.engine)
Target: Move logic here, make legacy modules re-export from here

Submodules:
- entities.py: Core dataclasses (Side, OrderType, TradeIntent, etc.)
- cost_model.py: Transaction cost calculations
- scoring/: Entry scoring logic
- edge/: Edge enforcement rules
- state/: Position state machine
"""

# Re-export core entities
from src.domain.trading.entities import (
    Side, OrderType, OrderStatus, FillType, PositionState, TradeResult, MarketRegime,
    MarketSnapshot, FeatureVector, TradeIntent, OrderIntent, ExecutionReport,
    PositionSnapshot, TradeRecord
)

# Re-export cost model
from src.domain.trading.cost.cost_model import (
    CostModel, CostEstimate, SlippageModel, FundingModel,
    get_cost_model, KRAKEN_FUTURES_FEES, BINANCE_FUTURES_FEES
)

# Re-export scoring
from src.domain.trading.scoring.entry_scorer import (
    EntryScorer,
    get_entry_scorer,
)

# Re-export edge
from src.domain.trading.edge.edge_enforcement import EdgeEnforcement
from src.domain.trading.edge.edge_validator import EdgeValidator, ContextValidity

# Re-export state
from src.domain.trading.state.state_machine import StateMachine, BotState
from src.domain.trading.state.state_types import (
    PositionState as StatePositionState,
    StrategyState,
    StateTransition,
    get_state_size_multiplier,
    can_trade_in_state,
    compute_final_position_multiplier,
)

# 🎯 Edge State Manager (Jan 27, 2026)
from src.domain.trading.edge_state_manager import (
    EdgeStateManager, EdgeState, BOSEvent, EdgeStats,
    get_edge_manager, reset_edge_manager
)

__all__ = [
    # Entities
    'Side', 'OrderType', 'OrderStatus', 'FillType', 'PositionState', 'TradeResult', 'MarketRegime',
    'MarketSnapshot', 'FeatureVector', 'TradeIntent', 'OrderIntent', 'ExecutionReport',
    'PositionSnapshot', 'TradeRecord',
    # Cost Model
    'CostModel', 'CostEstimate', 'SlippageModel', 'FundingModel',
    'get_cost_model', 'KRAKEN_FUTURES_FEES', 'BINANCE_FUTURES_FEES',
    # Scoring
    'EntryScorer', 'get_entry_scorer',
    # Edge
    'EdgeEnforcement', 'EdgeValidator', 'ContextValidity',
    # Edge State Manager
    'EdgeStateManager', 'EdgeState', 'BOSEvent', 'EdgeStats',
    'get_edge_manager', 'reset_edge_manager',
    # State
    'StateMachine', 'BotState', 'StatePositionState', 'StrategyState',
    'StateTransition', 'get_state_size_multiplier', 'can_trade_in_state',
    'compute_final_position_multiplier',
]
