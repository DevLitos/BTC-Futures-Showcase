"""
Position domain entities.

This module contains position-related domain types:
- PositionState: Position lifecycle states
- PositionSnapshot: Snapshot of a position at a point in time
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Dict, Any

from src.domain.common.types import Side


class PositionState(Enum):
    """Position lifecycle states."""
    PENDING = "PENDING"           # Order placed, awaiting fill
    OPEN = "OPEN"                 # Position confirmed on exchange
    PARTIAL = "PARTIAL"           # Partially closed
    CLOSED = "CLOSED"             # Fully closed
    INCONSISTENT = "INCONSISTENT" # Local state differs from exchange


@dataclass
class PositionSnapshot:
    """
    Snapshot of a position at a point in time.
    
    This is the SINGLE SOURCE OF TRUTH for position state.
    In LIVE mode, this is populated FROM the exchange only.
    """
    # Identity
    symbol: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Position state
    side: Side = Side.FLAT
    size: float = 0.0               # In base currency
    entry_price: float = 0.0        # Average entry price
    
    # P&L
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    funding_paid: float = 0.0       # Total funding paid/received
    
    # Risk management
    stop_loss: float = 0.0
    take_profit: float = 0.0
    
    # Exchange state
    leverage: float = 1.0
    margin_used: float = 0.0
    liquidation_price: float = 0.0
    
    # Lifecycle
    state: PositionState = PositionState.CLOSED
    open_time: Optional[datetime] = None
    
    # MFE/MAE tracking
    highest_price: float = 0.0
    lowest_price: float = 0.0
    
    # Source
    source: str = "unknown"  # "exchange" or "local"
    
    def is_open(self) -> bool:
        """Check if position is open."""
        return self.size > 0 and self.side != Side.FLAT
    
    @property
    def notional_value(self) -> float:
        """Calculate notional value of position."""
        return self.size * self.entry_price
    
    @property
    def mfe_r(self) -> float:
        """Maximum Favorable Excursion in R."""
        if not self.is_open() or self.stop_loss == 0:
            return 0.0
        
        risk = abs(self.entry_price - self.stop_loss)
        if risk == 0:
            return 0.0
        
        if self.side == Side.LONG:
            mfe = self.highest_price - self.entry_price
        else:
            mfe = self.entry_price - self.lowest_price
        
        return mfe / risk
    
    @property
    def mae_r(self) -> float:
        """Maximum Adverse Excursion in R."""
        if not self.is_open() or self.stop_loss == 0:
            return 0.0
        
        risk = abs(self.entry_price - self.stop_loss)
        if risk == 0:
            return 0.0
        
        if self.side == Side.LONG:
            mae = self.entry_price - self.lowest_price
        else:
            mae = self.highest_price - self.entry_price
        
        return mae / risk
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp.isoformat(),
            'side': self.side.value,
            'size': self.size,
            'entry_price': self.entry_price,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'leverage': self.leverage,
            'state': self.state.value,
            'mfe_r': self.mfe_r,
            'mae_r': self.mae_r,
            'source': self.source,
        }
