"""
Core Types - Explicit Dataclasses for Domain Objects

This module defines strict type contracts for all domain objects.
Using explicit types instead of dicts prevents:
- Missing field bugs
- Type confusion
- Silent failures

All objects are designed to be:
- Immutable where possible (frozen=True)
- Self-validating (__post_init__)
- Serializable (to_dict methods)
- Comparable (order=True where meaningful)

USAGE:
    from core.types import TradeIntent, Side, OrderType
    
    intent = TradeIntent(
        symbol='BTC/USD:USD',
        side=Side.LONG,
        size_usd=100.0,
        entry_price=95000.0,
        stop_loss=94000.0,
        take_profit=97000.0,
        reason='HTF trend + LTF trigger'
    )
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from enum import Enum, auto
from typing import Optional, Dict, Any, List, Tuple
import uuid


# =============================================================================
# ENUMS - Explicit state definitions
# =============================================================================

class Side(Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"  # No position
    
    def opposite(self) -> 'Side':
        if self == Side.LONG:
            return Side.SHORT
        elif self == Side.SHORT:
            return Side.LONG
        return Side.FLAT
    
    @classmethod
    def from_string(cls, s: str) -> 'Side':
        """Parse side from string (case insensitive)."""
        mapping = {
            'long': cls.LONG, 'buy': cls.LONG, 'bid': cls.LONG,
            'short': cls.SHORT, 'sell': cls.SHORT, 'ask': cls.SHORT,
            'flat': cls.FLAT, 'none': cls.FLAT, '': cls.FLAT,
        }
        return mapping.get(s.lower(), cls.FLAT)


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
    PARTIALLY_FILLED = "PARTIAL"   # Partially filled
    FILLED = "FILLED"               # Completely filled
    CANCELED = "CANCELED"           # Canceled by user or system
    REJECTED = "REJECTED"           # Rejected by exchange
    EXPIRED = "EXPIRED"             # Expired (TTL exceeded)


class FillType(Enum):
    """How the order was filled (for fee calculation)."""
    MAKER = "MAKER"   # Limit order that provided liquidity
    TAKER = "TAKER"   # Market order or limit that crossed spread
    UNKNOWN = "UNKNOWN"


class PositionState(Enum):
    """Position lifecycle states."""
    PENDING = "PENDING"           # Order placed, awaiting fill
    OPEN = "OPEN"                 # Position confirmed on exchange
    PARTIAL = "PARTIAL"           # Partially closed
    CLOSED = "CLOSED"             # Fully closed
    INCONSISTENT = "INCONSISTENT" # Local state differs from exchange


class TradeResult(Enum):
    """Outcome of a completed trade."""
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    UNKNOWN = "UNKNOWN"


class MarketRegime(Enum):
    """Market regime classification."""
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    DEAD = "DEAD"  # Low activity


# =============================================================================
# MARKET DATA TYPES
# =============================================================================

@dataclass(frozen=True)
class MarketSnapshot:
    """
    Immutable snapshot of market state at a point in time.
    
    This is the input to all strategy decisions. It contains:
    - Current price and spread
    - OHLCV from multiple timeframes
    - Order book state
    - Derived indicators
    """
    timestamp: datetime
    symbol: str
    
    # Current state
    price: float
    bid: float
    ask: float
    
    # Volume
    volume_24h: float = 0.0
    
    # From 1m candles (last N)
    atr_1m: float = 0.0
    
    # From 5m candles
    atr_5m: float = 0.0
    ema_20_5m: float = 0.0
    ema_50_5m: float = 0.0
    
    # From 1h candles
    atr_1h: float = 0.0
    ema_50_1h: float = 0.0
    ema_200_1h: float = 0.0
    adx_1h: float = 0.0
    adx_slope_1h: float = 0.0
    
    # Order book
    orderbook_imbalance: float = 0.5  # 0 = all bids, 1 = all asks
    spread_bps: float = 0.0
    
    # Regime
    regime: MarketRegime = MarketRegime.RANGE
    
    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2
    
    @property
    def spread_pct(self) -> float:
        if self.mid_price == 0:
            return 0.0
        return (self.ask - self.bid) / self.mid_price * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/storage."""
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        result['regime'] = self.regime.value
        return result


@dataclass(frozen=True)
class FeatureVector:
    """
    Feature vector for ML/scoring models.
    
    Contains all features used for trade decisions.
    This is saved with every trade for post-hoc analysis.
    """
    timestamp: datetime
    
    # HTF features (1h)
    htf_trend: float = 0.0         # -1 (down) to +1 (up)
    htf_strength: float = 0.0      # 0 to 1 (ADX normalized)
    htf_momentum: float = 0.0      # ADX slope
    
    # MTF features (5m)
    mtf_structure: float = 0.0     # HL/LH quality
    mtf_distance_ema: float = 0.0  # Distance from EMA in ATR units
    
    # LTF features (1m)
    ltf_trigger: float = 0.0       # Trigger quality 0-1
    ltf_volume_ratio: float = 0.0  # Relative volume
    ltf_velocity: float = 0.0      # Price velocity in ATR/candle
    
    # Context features
    session_quality: float = 0.0   # 0-1
    funding_rate: float = 0.0      # Current funding
    regime_score: float = 0.0      # Regime quality for trading
    
    # Anchor levels
    distance_to_support: float = 0.0  # In ATR units
    distance_to_resistance: float = 0.0
    
    # Computed score (from scoring model)
    raw_score: float = 0.0
    calibrated_probability: float = 0.0  # P(win|features)
    expected_r: float = 0.0  # E[R|features]
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        return result
    
    def as_array(self) -> List[float]:
        """Convert to numpy-compatible array for ML models."""
        return [
            self.htf_trend, self.htf_strength, self.htf_momentum,
            self.mtf_structure, self.mtf_distance_ema,
            self.ltf_trigger, self.ltf_volume_ratio, self.ltf_velocity,
            self.session_quality, self.funding_rate, self.regime_score,
            self.distance_to_support, self.distance_to_resistance,
        ]


# =============================================================================
# TRADE INTENT & ORDER TYPES
# =============================================================================

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
        """Validate intent."""
        if self.side != Side.FLAT:
            assert self.symbol, "Symbol required for non-flat intent"
            assert self.stop_loss > 0, "Stop loss required"
            
            # Validate SL direction
            if self.side == Side.LONG:
                assert self.stop_loss < self.entry_price or self.entry_price == 0, \
                    f"LONG SL ({self.stop_loss}) must be below entry ({self.entry_price})"
            else:
                assert self.stop_loss > self.entry_price or self.entry_price == 0, \
                    f"SHORT SL ({self.stop_loss}) must be above entry ({self.entry_price})"
    
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


# =============================================================================
# EXECUTION & POSITION TYPES
# =============================================================================

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


# =============================================================================
# TRADE RECORD (Complete trade for analysis)
# =============================================================================

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
