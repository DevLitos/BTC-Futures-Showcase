"""
Order domain entities.

This module contains order-related domain types:
- OrderType: Types of orders (MARKET, LIMIT, etc.)
- OrderStatus: Order lifecycle states
- OrderIntent: Low-level order instruction
- ExecutionReport: Fill information from exchange
- FillType: How the order was filled
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Dict, Any
import uuid

from src.domain.common.types import Side


class OrderType(Enum):
    """Order type."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"


class OrderStatus(Enum):
    """
    Order lifecycle states.
    
    NEW → OPEN → PARTIALLY_FILLED → FILLED
                                  → CANCELED
                                  → REJECTED
                                  → EXPIRED
    """
    NEW = "NEW"                     # Created locally, not yet sent
    OPEN = "OPEN"                   # Sent to exchange, awaiting fill
    PARTIALLY_FILLED = "PARTIAL"    # Partially filled
    FILLED = "FILLED"               # Completely filled
    CANCELED = "CANCELED"           # Canceled by user or system
    REJECTED = "REJECTED"           # Rejected by exchange
    EXPIRED = "EXPIRED"             # Expired (TTL exceeded)


class FillType(Enum):
    """How the order was filled (for fee calculation)."""
    MAKER = "MAKER"   # Limit order that provided liquidity
    TAKER = "TAKER"   # Market order or limit that crossed spread
    UNKNOWN = "UNKNOWN"


@dataclass
class OrderIntent:
    """
    Low-level order instruction for execution.
    
    This is the output of the execution layer's translation of TradeIntent.
    It contains exchange-specific details like:
    - Rounded size
    - Exact price
    - Order parameters (postOnly, reduceOnly, etc.)
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    trade_intent_id: str = ""       # Links back to TradeIntent
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Order details
    symbol: str = ""
    side: Side = Side.FLAT
    order_type: OrderType = OrderType.LIMIT
    
    # Size (already rounded to exchange precision)
    size: float = 0.0
    
    # Price
    price: float = 0.0              # For limit orders
    stop_price: float = 0.0         # For stop orders
    
    # Parameters
    post_only: bool = True          # Maker only
    reduce_only: bool = False       # For closing orders
    time_in_force: str = "GTC"      # GTC, IOC, FOK
    
    # Lifecycle
    status: OrderStatus = OrderStatus.NEW
    exchange_order_id: Optional[str] = None
    
    # Execution results (filled after execution)
    filled_size: float = 0.0
    filled_price: float = 0.0       # VWAP of fills
    fill_type: FillType = FillType.UNKNOWN
    
    def is_complete(self) -> bool:
        """Check if order lifecycle is complete."""
        return self.status in (
            OrderStatus.FILLED, 
            OrderStatus.CANCELED, 
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'trade_intent_id': self.trade_intent_id,
            'timestamp': self.timestamp.isoformat(),
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'size': self.size,
            'price': self.price,
            'stop_price': self.stop_price,
            'post_only': self.post_only,
            'reduce_only': self.reduce_only,
            'status': self.status.value,
            'exchange_order_id': self.exchange_order_id,
            'filled_size': self.filled_size,
            'filled_price': self.filled_price,
            'fill_type': self.fill_type.value,
        }


@dataclass
class ExecutionReport:
    """
    Report of order execution from exchange.
    
    This is created when we receive fill information from the exchange.
    Used for:
    - Updating position state
    - Calculating slippage
    - Recording execution quality
    """
    # Identity
    order_id: str = ""
    exchange_order_id: str = ""
    exchange_trade_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Execution details
    symbol: str = ""
    side: Side = Side.FLAT
    
    # Fill information
    filled_size: float = 0.0
    filled_price: float = 0.0
    fill_type: FillType = FillType.UNKNOWN
    
    # Costs
    fee_amount: float = 0.0
    fee_currency: str = "USD"
    
    # Timing
    latency_ms: float = 0.0         # Time from order to fill
    
    # Expected vs actual (for slippage calculation)
    expected_price: float = 0.0
    
    @property
    def slippage_bps(self) -> float:
        """Calculate slippage in basis points."""
        if self.expected_price == 0:
            return 0.0
        
        if self.side == Side.LONG:
            # For longs, paying more is negative slippage
            slippage = (self.filled_price - self.expected_price) / self.expected_price
        else:
            # For shorts, selling for less is negative slippage
            slippage = (self.expected_price - self.filled_price) / self.expected_price
        
        return slippage * 10000  # Convert to bps
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'exchange_order_id': self.exchange_order_id,
            'exchange_trade_id': self.exchange_trade_id,
            'timestamp': self.timestamp.isoformat(),
            'symbol': self.symbol,
            'side': self.side.value,
            'filled_size': self.filled_size,
            'filled_price': self.filled_price,
            'fill_type': self.fill_type.value,
            'fee_amount': self.fee_amount,
            'fee_currency': self.fee_currency,
            'latency_ms': self.latency_ms,
            'expected_price': self.expected_price,
            'slippage_bps': self.slippage_bps,
        }
