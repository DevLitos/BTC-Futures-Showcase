"""
Trade domain entities.

This module contains trade-related domain types:
- TradeIntent: High-level trade intention from strategy
- TradeRecord: Complete record of a trade from entry to exit
- TradeResult: Outcome of a completed trade
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Dict, Any
import uuid

from src.domain.common.types import Side, FeatureVector
from src.domain.trading.entities.order import OrderType


class TradeResult(Enum):
    """Outcome of a completed trade."""
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    UNKNOWN = "UNKNOWN"


@dataclass
class TradeIntent:
    """
    High-level trade intention from strategy.
    
    This is what the strategy WANTS to do. It gets converted to
    OrderIntent by the execution layer (with rounding, limits, etc).
    
    Separation of concerns:
    - Strategy: Decides WHAT to trade
    - Execution: Decides HOW to execute
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Core intent
    symbol: str = ""
    side: Side = Side.FLAT
    
    # Sizing (one of these should be set)
    size_base: float = 0.0          # Size in base currency (BTC)
    size_usd: float = 0.0           # Size in USD
    size_pct_equity: float = 0.0    # Size as % of equity
    risk_pct_equity: float = 0.0    # Risk as % of equity (for sizing)
    
    # Levels
    entry_price: float = 0.0        # Desired entry (0 = market)
    stop_loss: float = 0.0          # Hard stop loss
    take_profit_1: float = 0.0      # First TP
    take_profit_2: float = 0.0      # Second TP (if using partials)
    
    # Preferences
    order_type_preference: OrderType = OrderType.LIMIT
    allow_market_fallback: bool = True
    max_slippage_bps: float = 10.0
    
    # Metadata
    reason: str = ""                # Why this trade
    score: float = 0.0              # Strategy score
    features: Optional[FeatureVector] = None
    tags: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate intent - uses if/raise instead of assert for production safety."""
        if self.side != Side.FLAT:
            if not self.symbol:
                raise ValueError("Symbol required for non-flat intent")
            if self.stop_loss <= 0:
                raise ValueError(f"Stop loss must be > 0, got {self.stop_loss}")
            
            # Validate SL direction
            if self.side == Side.LONG:
                if not (self.stop_loss < self.entry_price or self.entry_price == 0):
                    raise ValueError(
                        f"LONG SL ({self.stop_loss}) must be below entry ({self.entry_price})"
                    )
            else:
                if not (self.stop_loss > self.entry_price or self.entry_price == 0):
                    raise ValueError(
                        f"SHORT SL ({self.stop_loss}) must be above entry ({self.entry_price})"
                    )
    
    @property
    def risk_reward_ratio(self) -> float:
        """Calculate R:R based on entry, SL, and TP1."""
        if self.entry_price == 0 or self.stop_loss == 0 or self.take_profit_1 == 0:
            return 0.0
        
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit_1 - self.entry_price)
        
        return reward / risk if risk > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'symbol': self.symbol,
            'side': self.side.value,
            'size_base': self.size_base,
            'size_usd': self.size_usd,
            'entry_price': self.entry_price,
            'stop_loss': self.stop_loss,
            'take_profit_1': self.take_profit_1,
            'take_profit_2': self.take_profit_2,
            'reason': self.reason,
            'score': self.score,
            'risk_reward_ratio': self.risk_reward_ratio,
            'tags': self.tags,
        }
        if self.features:
            result['features'] = self.features.to_dict()
        return result


@dataclass
class TradeRecord:
    """
    Complete record of a trade from entry to exit.
    
    Used for:
    - Trade journal
    - Performance analysis
    - Feature importance analysis
    - Walk-forward validation
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    
    # Entry
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    entry_price: float = 0.0
    entry_size: float = 0.0
    entry_side: Side = Side.FLAT
    entry_intent: Optional[TradeIntent] = None
    
    # Exit
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_size: float = 0.0
    exit_reason: str = ""
    
    # Costs
    total_fees: float = 0.0
    funding_paid: float = 0.0
    slippage_entry_bps: float = 0.0
    slippage_exit_bps: float = 0.0
    
    # P&L
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    
    # Risk metrics
    initial_risk: float = 0.0       # Entry - SL in price
    mfe: float = 0.0                # Max favorable excursion
    mae: float = 0.0                # Max adverse excursion
    
    # Features at entry (for ML)
    features: Optional[FeatureVector] = None
    
    # Metadata
    tags: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def result(self) -> TradeResult:
        """Determine trade result."""
        if self.exit_time is None:
            return TradeResult.UNKNOWN
        
        if self.net_pnl > self.initial_risk * 0.1:  # >10% of risk = win
            return TradeResult.WIN
        elif self.net_pnl < -self.initial_risk * 0.1:
            return TradeResult.LOSS
        else:
            return TradeResult.BREAKEVEN
    
    @property
    def r_multiple(self) -> float:
        """Calculate R multiple (PnL / initial risk)."""
        if self.initial_risk == 0:
            return 0.0
        return self.net_pnl / (self.initial_risk * self.entry_size)
    
    @property
    def duration_minutes(self) -> float:
        """Trade duration in minutes."""
        if self.exit_time is None:
            return 0.0
        return (self.exit_time - self.entry_time).total_seconds() / 60
    
    @property
    def mfe_r(self) -> float:
        """MFE in R multiples."""
        if self.initial_risk == 0:
            return 0.0
        return self.mfe / self.initial_risk
    
    @property
    def mae_r(self) -> float:
        """MAE in R multiples."""
        if self.initial_risk == 0:
            return 0.0
        return self.mae / self.initial_risk
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            'id': self.id,
            'entry_time': self.entry_time.isoformat(),
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'entry_size': self.entry_size,
            'side': self.entry_side.value,
            'exit_reason': self.exit_reason,
            'total_fees': self.total_fees,
            'funding_paid': self.funding_paid,
            'slippage_entry_bps': self.slippage_entry_bps,
            'slippage_exit_bps': self.slippage_exit_bps,
            'gross_pnl': self.gross_pnl,
            'net_pnl': self.net_pnl,
            'r_multiple': self.r_multiple,
            'mfe_r': self.mfe_r,
            'mae_r': self.mae_r,
            'duration_minutes': self.duration_minutes,
            'result': self.result.value,
            'tags': self.tags,
        }
        if self.features:
            result['features'] = self.features.to_dict()
        return result
