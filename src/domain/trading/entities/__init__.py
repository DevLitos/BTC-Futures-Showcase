"""
Trading Entities.

Re-exports all trading entities for easier imports.
"""

from src.domain.trading.entities.trade import (
    TradeIntent,
    TradeRecord,
    TradeResult,
)
from src.domain.trading.entities.order import (
    OrderType,
    OrderStatus,
    OrderIntent,
    ExecutionReport,
    FillType,
)
from src.domain.trading.entities.position import (
    PositionState,
    PositionSnapshot,
)
from src.domain.common.types import (
    Side,
    MarketRegime,
    MarketSnapshot,
    FeatureVector,
)

__all__ = [
    # Trade
    "TradeIntent",
    "TradeRecord",
    "TradeResult",
    # Order
    "OrderType",
    "OrderStatus",
    "OrderIntent",
    "ExecutionReport",
    "FillType",
    # Position
    "PositionState",
    "PositionSnapshot",
    # Common (re-export for backward compatibility)
    "Side",
    "MarketRegime",
    "MarketSnapshot",
    "FeatureVector",
]
