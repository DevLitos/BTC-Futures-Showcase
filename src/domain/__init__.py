"""
Domain Layer - Core Business Logic (No external dependencies)

Clean Architecture Layer: DOMAIN
This layer contains:
- Entities: Core business objects (trades, positions, orders)
- Value Objects: Immutable domain concepts (Side, OrderType, MarketRegime)
- Domain Events: Business events that other layers react to
- Domain Services: Pure business logic with no I/O

Clean Architecture Rules:
- NO infrastructure imports (no ccxt, no websockets, no DB)
- NO framework imports (no fastapi, no asyncio for I/O)
- ONLY pure Python + numpy for calculations
- All dependencies point INWARD (infrastructure → domain)

Migration Status: ALIAS (re-exports from src.core and src.engine)
Target: Gradually move logic here, make legacy modules re-export from here

Submodules:
- trading/: Core trading entities and logic
- risk/: Risk management domain logic
- market/: Market analysis and regime detection
- events/: Domain event definitions
"""

# Domain Entities - re-exported from core.types
from src.domain.trading.entities import (
    Side, OrderType, OrderStatus, FillType, PositionState, TradeResult, MarketRegime,
    MarketSnapshot, FeatureVector, TradeIntent, OrderIntent, ExecutionReport,
    PositionSnapshot, TradeRecord
)

# Domain Events - re-exported from core.events
from src.domain.events.event_bus import (
    EventType, Event, EventBus, EventHandler, get_event_bus
)

# Cost Model - re-exported from core.cost_model
from src.domain.trading.cost.cost_model import (
    CostModel, CostEstimate, SlippageModel, FundingModel,
    get_cost_model, KRAKEN_FUTURES_FEES, BINANCE_FUTURES_FEES
)

__all__ = [
    # Entities
    'Side', 'OrderType', 'OrderStatus', 'FillType', 'PositionState', 'TradeResult', 'MarketRegime',
    'MarketSnapshot', 'FeatureVector', 'TradeIntent', 'OrderIntent', 'ExecutionReport',
    'PositionSnapshot', 'TradeRecord',
    # Events
    'EventType', 'Event', 'EventBus', 'EventHandler', 'get_event_bus',
    # Cost Model
    'CostModel', 'CostEstimate', 'SlippageModel', 'FundingModel',
    'get_cost_model', 'KRAKEN_FUTURES_FEES', 'BINANCE_FUTURES_FEES',
]
