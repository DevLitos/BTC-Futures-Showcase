"""
Position Store - Single Source of Truth for Positions and Orders

CRITICAL: This module is the ONLY authority on position state.
- Updates ONLY from exchange fills and reconciliation
- All other components READ from here
- NEVER creates phantom positions

DESIGN PRINCIPLES:
1. Position state comes from EXCHANGE, not local calculations
2. Order state tracked with explicit lifecycle
3. Event-based updates (not timeout-based)
4. Full audit trail for every state change

USAGE:
    from src.accounting.position_store import get_position_store
    
    store = get_position_store()
    
    # Get current position (read-only)
    pos = store.get_position('BTC/USD:USD')
    if pos and pos.size != 0:
        print(f"Position: {pos.side} {pos.size} @ {pos.entry_price}")
    
    # Update from exchange (only allowed method in LIVE)
    await store.sync_from_exchange(exchange)
    
    # Record fill (updates position)
    store.record_fill(
        order_id='abc123',
        symbol='BTC/USD:USD',
        side='buy',
        size=0.01,
        price=95000.0,
        fee=0.95,
        fill_type='MAKER'
    )
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, List, Tuple, Any
from enum import Enum
import asyncio
import logging
import uuid

from src.core.runtime_mode import get_runtime_config, RuntimeMode
from src.core.types import Side, OrderStatus, FillType, PositionSnapshot, OrderIntent
from src.core.events import get_event_bus, EventType

logger = logging.getLogger(__name__)


# =============================================================================
# ORDER TRACKING
# =============================================================================

class OrderState(Enum):
    """Order lifecycle states."""
    PENDING = "PENDING"           # Created locally, not yet sent
    SENT = "SENT"                 # Sent to exchange, awaiting ack
    OPEN = "OPEN"                 # Confirmed open on exchange
    PARTIALLY_FILLED = "PARTIAL"  # Partially filled
    FILLED = "FILLED"             # Completely filled
    CANCELED = "CANCELED"         # Canceled (by user or exchange)
    REJECTED = "REJECTED"         # Rejected by exchange
    EXPIRED = "EXPIRED"           # Expired (TTL)
    ERROR = "ERROR"               # Error state


@dataclass
class TrackedOrder:
    """
    Complete order tracking with full lifecycle.
    
    Immutable once filled/canceled - create new order for new trades.
    """
    order_id: str
    client_order_id: str
    symbol: str
    side: str  # 'buy' or 'sell'
    order_type: str  # 'limit', 'market', 'stop_loss', 'take_profit'
    size: float
    price: Optional[float]
    
    # State tracking
    state: OrderState = OrderState.PENDING
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    total_fee: float = 0.0
    
    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    
    # Context
    purpose: str = ""  # 'entry', 'sl', 'tp', 'exit'
    parent_trade_id: str = ""
    reduce_only: bool = False
    post_only: bool = False
    
    # Fills history
    fills: List[Dict] = field(default_factory=list)
    
    @property
    def is_active(self) -> bool:
        """True if order is still active on exchange."""
        return self.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED, OrderState.SENT)
    
    @property
    def is_terminal(self) -> bool:
        """True if order is in terminal state."""
        return self.state in (
            OrderState.FILLED, OrderState.CANCELED, 
            OrderState.REJECTED, OrderState.EXPIRED, OrderState.ERROR
        )
    
    @property
    def remaining_size(self) -> float:
        """Size remaining to be filled."""
        return self.size - self.filled_size
    
    def to_dict(self) -> Dict:
        return {
            'order_id': self.order_id,
            'client_order_id': self.client_order_id,
            'symbol': self.symbol,
            'side': self.side,
            'order_type': self.order_type,
            'size': self.size,
            'price': self.price,
            'state': self.state.value,
            'filled_size': self.filled_size,
            'avg_fill_price': self.avg_fill_price,
            'total_fee': self.total_fee,
            'purpose': self.purpose,
            'reduce_only': self.reduce_only,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat(),
            'fills_count': len(self.fills),
        }


# =============================================================================
# POSITION TRACKING
# =============================================================================

@dataclass
class TrackedPosition:
    """
    Complete position tracking.
    
    CRITICAL: This is the SINGLE SOURCE OF TRUTH for position state.
    Updates ONLY via record_fill() or sync_from_exchange().
    """
    symbol: str
    side: str  # 'LONG', 'SHORT', 'FLAT'
    size: float  # Always positive, direction determined by side
    entry_price: float
    
    # Derived from fills (accurate)
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    funding_paid: float = 0.0
    
    # Tracking
    opened_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trade_id: str = ""  # Current trade identifier
    
    # Audit trail
    fill_count: int = 0
    last_fill_id: str = ""
    sync_count: int = 0
    last_sync_at: Optional[datetime] = None
    
    @property
    def is_flat(self) -> bool:
        """True if no position."""
        return self.size == 0 or self.side == 'FLAT'
    
    @property
    def notional_usd(self) -> float:
        """Notional value in USD."""
        return self.size * self.entry_price
    
    def to_snapshot(self, current_price: float = 0.0) -> PositionSnapshot:
        """Convert to immutable snapshot."""
        unrealized_pnl = 0.0
        if current_price > 0 and self.size > 0:
            if self.side == 'LONG':
                unrealized_pnl = self.size * (current_price - self.entry_price)
            elif self.side == 'SHORT':
                unrealized_pnl = self.size * (self.entry_price - current_price)
        
        return PositionSnapshot(
            symbol=self.symbol,
            side=Side.LONG if self.side == 'LONG' else Side.SHORT if self.side == 'SHORT' else Side.FLAT,
            size=self.size,
            entry_price=self.entry_price,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=self.realized_pnl,
            funding_paid=self.funding_paid,
            timestamp=self.updated_at,
        )
    
    def to_dict(self) -> Dict:
        return {
            'symbol': self.symbol,
            'side': self.side,
            'size': self.size,
            'entry_price': self.entry_price,
            'notional_usd': self.notional_usd,
            'realized_pnl': self.realized_pnl,
            'total_fees': self.total_fees,
            'funding_paid': self.funding_paid,
            'trade_id': self.trade_id,
            'is_flat': self.is_flat,
            'fill_count': self.fill_count,
            'sync_count': self.sync_count,
            'updated_at': self.updated_at.isoformat(),
        }


# =============================================================================
# POSITION STORE
# =============================================================================

class PositionStore:
    """
    Single Source of Truth for all positions and orders.
    
    DESIGN:
    - Position state ONLY updates from fills or exchange sync
    - Order lifecycle tracked explicitly
    - Full audit trail for debugging
    - Event bus integration for notifications
    
    CRITICAL RULES:
    1. In LIVE mode, always sync_from_exchange() before making decisions
    2. Never assume position state - always get_position()
    3. All fills recorded with record_fill()
    """
    
    def __init__(self, event_bus=None):
        self._positions: Dict[str, TrackedPosition] = {}
        self._orders: Dict[str, TrackedOrder] = {}
        self._order_history: List[TrackedOrder] = []
        self._fill_history: List[Dict] = []
        
        # State change events
        self._event_bus = event_bus or get_event_bus()
        
        # 🔥 FIX (Jan 21, 2026): Track background tasks to prevent GC and enable clean shutdown
        self._background_tasks: set = set()
        
        # Sync tracking
        self._last_sync: Optional[datetime] = None
        self._sync_count = 0
        self._reconciliation_errors: List[Dict] = []
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        logger.info("PositionStore initialized")
    
    # =========================================================================
    # EVENT HELPERS
    # =========================================================================
    
    def _emit_event(self, event_type: EventType, data: Dict[str, Any]) -> None:
        """
        Helper to emit events with proper Event wrapper.
        
        FIX (Jan 14, 2026): EventBus.emit() takes an Event object, not (type, dict).
        This helper creates the proper Event object.
        
        🔥 FIX (Jan 21, 2026): Track tasks to prevent garbage collection and enable
        proper exception handling.
        """
        from src.core.events import Event
        
        event = Event(
            type=event_type,
            source="position_store",
            data=data
        )
        
        # Schedule the async emit without blocking
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task = asyncio.create_task(self._event_bus.emit(event))
                # Track task to prevent GC and enable clean shutdown
                self._background_tasks.add(task)
                
                def _on_done(t):
                    self._background_tasks.discard(t)
                    if not t.cancelled():
                        exc = t.exception()
                        if exc:
                            logger.error(f"Event emit task failed: {exc}")
                
                task.add_done_callback(_on_done)
            else:
                # Sync context - can't emit async event
                logger.debug(f"Event {event_type.name} created (no running loop)")
        except RuntimeError:
            # No event loop - skip
            logger.debug(f"Event {event_type.name} skipped (no event loop)")
    
    # =========================================================================
    # POSITION QUERIES
    # =========================================================================
    
    def get_position(self, symbol: str) -> Optional[TrackedPosition]:
        """
        Get current position for symbol.
        
        Returns None if no position exists (not FLAT position).
        """
        return self._positions.get(symbol)
    
    def get_position_snapshot(self, symbol: str, current_price: float = 0.0) -> Optional[PositionSnapshot]:
        """Get immutable snapshot of position."""
        pos = self.get_position(symbol)
        if pos:
            return pos.to_snapshot(current_price)
        return None
    
    def get_all_positions(self) -> List[TrackedPosition]:
        """Get all tracked positions."""
        return list(self._positions.values())
    
    def get_active_positions(self) -> List[TrackedPosition]:
        """Get all non-flat positions."""
        return [p for p in self._positions.values() if not p.is_flat]
    
    def has_position(self, symbol: str) -> bool:
        """Check if there's an active position for symbol."""
        pos = self.get_position(symbol)
        return pos is not None and not pos.is_flat
    
    def get_position_side(self, symbol: str) -> str:
        """Get position side ('LONG', 'SHORT', 'FLAT')."""
        pos = self.get_position(symbol)
        return pos.side if pos else 'FLAT'
    
    # =========================================================================
    # ORDER QUERIES
    # =========================================================================
    
    def get_order(self, order_id: str) -> Optional[TrackedOrder]:
        """Get order by exchange order ID."""
        return self._orders.get(order_id)
    
    def get_order_by_client_id(self, client_order_id: str) -> Optional[TrackedOrder]:
        """Get order by client order ID."""
        for order in self._orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None
    
    def get_active_orders(self, symbol: str | None = None) -> List[TrackedOrder]:
        """Get all active orders, optionally filtered by symbol."""
        orders = [o for o in self._orders.values() if o.is_active]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders
    
    def get_orders_by_purpose(self, symbol: str, purpose: str) -> List[TrackedOrder]:
        """Get orders by purpose (e.g., 'sl', 'tp', 'entry')."""
        return [
            o for o in self._orders.values()
            if o.symbol == symbol and o.purpose == purpose and o.is_active
        ]
    
    def has_active_orders(self, symbol: str | None = None) -> bool:
        """Check if there are active orders."""
        return len(self.get_active_orders(symbol)) > 0
    
    # =========================================================================
    # ORDER LIFECYCLE
    # =========================================================================
    
    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        price: Optional[float] = None,
        purpose: str = "",
        trade_id: str = "",
        reduce_only: bool = False,
        post_only: bool = False
    ) -> TrackedOrder:
        """
        Create a new tracked order.
        
        Returns TrackedOrder in PENDING state.
        Must call mark_order_sent() after sending to exchange.
        """
        client_order_id = f"dt_{uuid.uuid4().hex[:12]}"
        
        order = TrackedOrder(
            order_id="",  # Will be set when exchange confirms
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            purpose=purpose,
            parent_trade_id=trade_id,
            reduce_only=reduce_only,
            post_only=post_only,
        )
        
        # Emit event
        self._emit_event(EventType.ORDER_CREATED, {
            'order': order.to_dict(),
            'symbol': symbol,
            'side': side,
        })
        
        logger.debug(f"Order created: {client_order_id} | {symbol} {side} {size} @ {price}")
        return order
    
    def mark_order_sent(self, order: TrackedOrder) -> None:
        """Mark order as sent to exchange."""
        order.state = OrderState.SENT
        order.sent_at = datetime.now(UTC)
    
    def mark_order_open(self, order: TrackedOrder, exchange_order_id: str) -> None:
        """Mark order as confirmed open on exchange."""
        order.order_id = exchange_order_id
        order.state = OrderState.OPEN
        order.opened_at = datetime.now(UTC)
        
        # Track by exchange order ID
        self._orders[exchange_order_id] = order
        
        self._emit_event(EventType.ORDER_PLACED, {
            'order': order.to_dict(),
            'exchange_order_id': exchange_order_id,
        })
        
        logger.info(f"📋 Order open: {exchange_order_id} | {order.symbol} {order.side} {order.size}")
    
    def mark_order_canceled(self, order_id: str, reason: str = "") -> None:
        """Mark order as canceled."""
        order = self._orders.get(order_id)
        if not order:
            logger.warning(f"Cannot cancel unknown order: {order_id}")
            return
        
        order.state = OrderState.CANCELED
        order.canceled_at = datetime.now(UTC)
        
        self._emit_event(EventType.ORDER_CANCELLED, {
            'order_id': order_id,
            'reason': reason,
            'symbol': order.symbol,
        })
        
        # Move to history
        self._order_history.append(order)
        
        # 🔒 FIX (Jan 22, 2026): Trim order history to prevent memory leak
        if len(self._order_history) > 500:
            self._order_history = self._order_history[-250:]
        
        logger.info(f"❌ Order canceled: {order_id} | reason={reason}")
    
    def mark_order_rejected(self, order: TrackedOrder, reason: str = "") -> None:
        """Mark order as rejected by exchange."""
        order.state = OrderState.REJECTED
        
        self._emit_event(EventType.ORDER_CANCELLED, {
            'order_id': order.order_id,
            'reason': f"REJECTED: {reason}",
            'symbol': order.symbol,
        })
        
        logger.error(f"🚫 Order rejected: {order.client_order_id} | reason={reason}")
    
    # =========================================================================
    # FILL RECORDING - THE HEART OF THE SYSTEM
    # =========================================================================
    
    async def record_fill(
        self,
        order_id: str,
        symbol: str | None,
        side: str,  # 'buy' or 'sell'
        size: float,
        price: float,
        fee: float = 0.0,
        fill_type: str = 'UNKNOWN',
        fill_id: str = "",
        timestamp: datetime | None = None
    ) -> None:
        """
        Record a fill and update position state.
        
        THIS IS THE ONLY WAY TO UPDATE POSITION STATE (besides sync).
        
        Args:
            order_id: Exchange order ID
            symbol: Trading symbol (or None)
            side: 'buy' or 'sell' (exchange convention)
            size: Fill size
            price: Fill price
            fee: Fee paid
            fill_type: 'MAKER', 'TAKER', or 'UNKNOWN'
            fill_id: Exchange fill ID (for dedup)
            timestamp: Fill timestamp
        """
        async with self._lock:
            ts = timestamp or datetime.now(UTC)
            
            # Guard: symbol is required
            if symbol is None:
                logger.warning(f"Fill ignored: symbol is None for order {order_id}")
                return
            
            # Check for duplicate fill
            for existing in self._fill_history[-100:]:
                if existing.get('fill_id') == fill_id and fill_id:
                    logger.debug(f"Duplicate fill ignored: {fill_id}")
                    return
            
            # Record fill
            fill_record = {
                'fill_id': fill_id or f"fill_{uuid.uuid4().hex[:8]}",
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'size': size,
                'price': price,
                'fee': fee,
                'fill_type': fill_type,
                'timestamp': ts.isoformat(),
            }
            self._fill_history.append(fill_record)
            
            # 🔒 FIX (Jan 22, 2026): Trim fill history to prevent memory leak
            if len(self._fill_history) > 1000:
                self._fill_history = self._fill_history[-500:]
            
            # Update order if tracked
            order = self._orders.get(order_id)
            if order:
                order.filled_size += size
                order.total_fee += fee
                order.fills.append(fill_record)
                
                # Recalculate avg fill price
                total_value = sum(f['size'] * f['price'] for f in order.fills)
                total_size = sum(f['size'] for f in order.fills)
                order.avg_fill_price = total_value / total_size if total_size > 0 else 0
                
                # Check if fully filled
                if order.filled_size >= order.size - 1e-9:
                    order.state = OrderState.FILLED
                    order.filled_at = ts
                    self._order_history.append(order)
                    
                    self._emit_event(EventType.ORDER_FILLED, {
                        'order': order.to_dict(),
                        'fill': fill_record,
                    })
                else:
                    order.state = OrderState.PARTIALLY_FILLED
            
            # Update position
            await self._apply_fill_to_position(symbol, side, size, price, fee, ts, fill_id)
            
            logger.info(
                f"💰 Fill recorded: {symbol} {side} {size:.6f} @ {price:.2f} | "
                f"fee=${fee:.4f} | type={fill_type}"
            )
    
    async def _apply_fill_to_position(
        self,
        symbol: str,
        side: str,  # 'buy' or 'sell'
        size: float,
        price: float,
        fee: float,
        timestamp: datetime,
        fill_id: str
    ) -> None:
        """
        Apply fill to position state.
        
        LOGIC:
        - Buy on FLAT -> LONG
        - Sell on FLAT -> SHORT
        - Buy on LONG -> increase LONG
        - Sell on LONG -> decrease LONG (or flip to SHORT)
        - Buy on SHORT -> decrease SHORT (or flip to LONG)
        - Sell on SHORT -> increase SHORT
        """
        pos = self._positions.get(symbol)
        
        if not pos or pos.is_flat:
            # Opening new position
            new_side = 'LONG' if side == 'buy' else 'SHORT'
            self._positions[symbol] = TrackedPosition(
                symbol=symbol,
                side=new_side,
                size=size,
                entry_price=price,
                total_fees=fee,
                opened_at=timestamp,
                updated_at=timestamp,
                trade_id=f"trade_{uuid.uuid4().hex[:8]}",
                fill_count=1,
                last_fill_id=fill_id,
            )
            
            self._emit_event(EventType.POSITION_OPENED, {
                'symbol': symbol,
                'side': new_side,
                'size': size,
                'entry_price': price,
            })
            
            logger.info(f"📈 Position opened: {symbol} {new_side} {size:.6f} @ {price:.2f}")
            return
        
        # Existing position
        is_same_direction = (
            (side == 'buy' and pos.side == 'LONG') or
            (side == 'sell' and pos.side == 'SHORT')
        )
        
        if is_same_direction:
            # Increasing position - weighted average entry
            new_size = pos.size + size
            pos.entry_price = (pos.entry_price * pos.size + price * size) / new_size
            pos.size = new_size
            pos.total_fees += fee
            pos.fill_count += 1
            pos.last_fill_id = fill_id
            pos.updated_at = timestamp
            
            self._emit_event(EventType.POSITION_UPDATED, {
                'symbol': symbol,
                'side': pos.side,
                'size': pos.size,
                'entry_price': pos.entry_price,
                'action': 'increase',
            })
            
            logger.info(f"📊 Position increased: {symbol} {pos.side} -> {pos.size:.6f} @ {pos.entry_price:.2f}")
        
        else:
            # Reducing or flipping position
            if size < pos.size:
                # Partial close
                close_pnl = self._calculate_close_pnl(pos, size, price)
                pos.size -= size
                pos.realized_pnl += close_pnl - fee
                pos.total_fees += fee
                pos.fill_count += 1
                pos.last_fill_id = fill_id
                pos.updated_at = timestamp
                
                self._emit_event(EventType.POSITION_UPDATED, {
                    'symbol': symbol,
                    'side': pos.side,
                    'size': pos.size,
                    'pnl': close_pnl,
                    'action': 'decrease',
                })
                
                logger.info(f"📉 Position decreased: {symbol} {pos.side} -> {pos.size:.6f} | PnL=${close_pnl:.2f}")
            
            elif size == pos.size:
                # Full close
                close_pnl = self._calculate_close_pnl(pos, size, price)
                final_pnl = pos.realized_pnl + close_pnl - fee
                
                self._emit_event(EventType.POSITION_CLOSED, {
                    'symbol': symbol,
                    'side': pos.side,
                    'size': size,
                    'entry_price': pos.entry_price,
                    'exit_price': price,
                    'pnl': final_pnl,
                    'fees': pos.total_fees + fee,
                })
                
                # Reset position
                pos.side = 'FLAT'
                pos.size = 0
                pos.entry_price = 0
                pos.realized_pnl = 0
                pos.total_fees = 0
                pos.trade_id = ""
                pos.opened_at = None
                pos.updated_at = timestamp
                
                logger.info(f"🏁 Position closed: {symbol} | PnL=${final_pnl:.2f}")
            
            else:
                # Flip position
                close_size = pos.size
                remaining_size = size - pos.size
                
                # Close existing
                close_pnl = self._calculate_close_pnl(pos, close_size, price)
                
                # Open opposite
                new_side = 'LONG' if side == 'buy' else 'SHORT'
                pos.side = new_side
                pos.size = remaining_size
                pos.entry_price = price
                pos.realized_pnl = 0
                pos.total_fees = fee
                pos.opened_at = timestamp
                pos.updated_at = timestamp
                pos.trade_id = f"trade_{uuid.uuid4().hex[:8]}"
                
                self._emit_event(EventType.POSITION_CLOSED, {
                    'symbol': symbol,
                    'side': 'LONG' if new_side == 'SHORT' else 'SHORT',
                    'pnl': close_pnl,
                })
                
                self._emit_event(EventType.POSITION_OPENED, {
                    'symbol': symbol,
                    'side': new_side,
                    'size': remaining_size,
                    'entry_price': price,
                })
                
                logger.info(
                    f"🔄 Position flipped: {symbol} -> {new_side} {remaining_size:.6f} @ {price:.2f}"
                )
    
    def _calculate_close_pnl(self, pos: TrackedPosition, close_size: float, exit_price: float) -> float:
        """Calculate PnL for closing part of a position."""
        if pos.side == 'LONG':
            return close_size * (exit_price - pos.entry_price)
        elif pos.side == 'SHORT':
            return close_size * (pos.entry_price - exit_price)
        return 0.0
    
    # =========================================================================
    # EXCHANGE SYNCHRONIZATION
    # =========================================================================
    
    async def sync_from_exchange(self, exchange) -> Dict:
        """
        Synchronize positions and orders from exchange.
        
        CRITICAL: This is the ONLY way to ensure position accuracy in LIVE mode.
        Call before any trading decision.
        
        Returns dict with sync results and any discrepancies.
        """
        async with self._lock:
            config = get_runtime_config()
            result = {
                'synced_at': datetime.now(UTC).isoformat(),
                'positions_synced': 0,
                'orders_synced': 0,
                'discrepancies': [],
            }
            
            try:
                # 🔥 FIX (Jan 20, 2026): Add timeout to prevent hanging
                try:
                    exchange_positions = await asyncio.wait_for(
                        exchange.fetch_positions(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.error("⏰ TIMEOUT fetching positions in sync_from_exchange (10s)")
                    return result  # Return empty result instead of hanging
                
                for ex_pos in exchange_positions:
                    symbol = ex_pos.get('symbol')
                    ex_size = abs(float(ex_pos.get('contracts', 0) or 0))
                    ex_side = ex_pos.get('side', '').upper()
                    ex_entry = float(ex_pos.get('entryPrice', 0) or 0)
                    ex_notional = float(ex_pos.get('notional', 0) or 0)
                    
                    local_pos = self._positions.get(symbol)
                    
                    # Check for discrepancy
                    if local_pos:
                        size_diff = abs(local_pos.size - ex_size)
                        if size_diff > 1e-8:
                            discrepancy = {
                                'type': 'position_size_mismatch',
                                'symbol': symbol,
                                'local_size': local_pos.size,
                                'exchange_size': ex_size,
                                'local_side': local_pos.side,
                                'exchange_side': ex_side,
                            }
                            result['discrepancies'].append(discrepancy)
                            self._reconciliation_errors.append(discrepancy)
                            
                            logger.warning(
                                f"⚠️ Position mismatch: {symbol} | "
                                f"local={local_pos.size} ({local_pos.side}) | "
                                f"exchange={ex_size} ({ex_side})"
                            )
                    
                    # Update/create local position from exchange
                    if ex_size > 0:
                        if not local_pos:
                            self._positions[symbol] = TrackedPosition(
                                symbol=symbol,
                                side=ex_side,
                                size=ex_size,
                                entry_price=ex_entry,
                                updated_at=datetime.now(UTC),
                                trade_id=f"synced_{uuid.uuid4().hex[:8]}",
                            )
                        else:
                            # Correct local state to match exchange
                            local_pos.size = ex_size
                            local_pos.side = ex_side if ex_side else local_pos.side
                            local_pos.entry_price = ex_entry if ex_entry > 0 else local_pos.entry_price
                            local_pos.sync_count += 1
                            local_pos.last_sync_at = datetime.now(UTC)
                            local_pos.updated_at = datetime.now(UTC)
                        
                        result['positions_synced'] += 1
                    
                    elif local_pos and not local_pos.is_flat:
                        # Exchange shows no position but we have one locally
                        discrepancy = {
                            'type': 'phantom_position',
                            'symbol': symbol,
                            'local_size': local_pos.size,
                            'local_side': local_pos.side,
                        }
                        result['discrepancies'].append(discrepancy)
                        
                        logger.error(f"🚨 Phantom position: {symbol} | local={local_pos.size} | exchange=FLAT")
                        
                        # Force flat
                        local_pos.side = 'FLAT'
                        local_pos.size = 0
                
                # =====================================================================
                # 🔥 FIX (Jan 14, 2026): Check for phantom positions
                # Positions that exist locally but are NOT in exchange response at all
                # This happens when TP/SL closes position - exchange returns empty list
                # =====================================================================
                exchange_symbols = {ex_pos.get('symbol') for ex_pos in exchange_positions}
                for symbol, local_pos in list(self._positions.items()):
                    if local_pos and not local_pos.is_flat and symbol not in exchange_symbols:
                        # Local position exists but exchange didn't return it = closed
                        logger.warning(
                            f"🔄 Position closed on exchange: {symbol} | "
                            f"local had {local_pos.side} {local_pos.size:.6f} → forcing FLAT"
                        )
                        discrepancy = {
                            'type': 'phantom_position_cleared',
                            'symbol': symbol,
                            'local_size': local_pos.size,
                            'local_side': local_pos.side,
                            'reason': 'Position not in exchange response - likely TP/SL hit'
                        }
                        result['discrepancies'].append(discrepancy)
                        
                        # Force flat
                        local_pos.side = 'FLAT'
                        local_pos.size = 0
                        local_pos.updated_at = datetime.now(UTC)
                
                # Fetch open orders - 🔥 FIX: Add timeout
                try:
                    exchange_orders = await asyncio.wait_for(
                        exchange.fetch_open_orders(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.error("⏰ TIMEOUT fetching open orders in sync_from_exchange (10s)")
                    exchange_orders = []
                
                for ex_order in exchange_orders:
                    order_id = ex_order.get('id')
                    if order_id not in self._orders:
                        # Check if this is a protective order (SL/TP) - these are expected and OK
                        order_type = str(ex_order.get('type', '')).lower()
                        reduce_only = ex_order.get('reduceOnly', ex_order.get('info', {}).get('reduceOnly', False))
                        
                        if order_type in ('stop-loss', 'stop', 'take-profit', 'trailing-stop') or reduce_only:
                            # This is a protective order (SL/TP) - normal, don't warn
                            logger.debug(f"Protective order on exchange: {order_id} (type={order_type}, reduceOnly={reduce_only})")
                        else:
                            # Unknown non-protective order - log at DEBUG to avoid spam
                            # (often happens with pending limit orders during WAIT_FILL)
                            logger.debug(f"Unknown order on exchange: {order_id} (type={order_type})")
                    result['orders_synced'] += 1
                
                self._last_sync = datetime.now(UTC)
                self._sync_count += 1
                
                # Only log at INFO if there are discrepancies, otherwise DEBUG
                if result['discrepancies']:
                    logger.info(
                        f"✅ Sync complete: {result['positions_synced']} positions, "
                        f"{result['orders_synced']} orders, "
                        f"{len(result['discrepancies'])} discrepancies"
                    )
                else:
                    logger.debug(
                        f"Sync: {result['positions_synced']} pos, {result['orders_synced']} orders, OK"
                    )
                
            except Exception as e:
                logger.error(f"Sync error: {e}")
                result['error'] = str(e)
            
            return result
    
    # =========================================================================
    # FUNDING TRACKING
    # =========================================================================
    
    def record_funding_payment(self, symbol: str, amount: float) -> None:
        """Record funding payment for position."""
        pos = self._positions.get(symbol)
        if pos:
            pos.funding_paid += abs(amount)
            pos.updated_at = datetime.now(UTC)
            
            self._emit_event(EventType.POSITION_UPDATED, {
                'symbol': symbol,
                'funding_paid': amount,
                'total_funding': pos.funding_paid,
            })
            
            logger.debug(f"Funding recorded: {symbol} ${amount:.4f} | total=${pos.funding_paid:.4f}")
    
    # =========================================================================
    # DIAGNOSTICS
    # =========================================================================
    
    def get_state_summary(self) -> Dict:
        """Get complete state summary for diagnostics."""
        return {
            'positions': {s: p.to_dict() for s, p in self._positions.items()},
            'active_orders': [o.to_dict() for o in self._orders.values() if o.is_active],
            'order_count': len(self._orders),
            'fill_count': len(self._fill_history),
            'last_sync': self._last_sync.isoformat() if self._last_sync else None,
            'sync_count': self._sync_count,
            'reconciliation_errors': self._reconciliation_errors[-10:],
        }
    
    def get_recent_fills(self, n: int = 20) -> List[Dict]:
        """Get recent fills for debugging."""
        return self._fill_history[-n:]
    
    def clear_terminal_orders(self) -> int:
        """Clean up terminal orders from active tracking."""
        to_remove = [oid for oid, o in self._orders.items() if o.is_terminal]
        for oid in to_remove:
            order = self._orders.pop(oid)
            if order not in self._order_history:
                self._order_history.append(order)
        return len(to_remove)
    
    async def cleanup_zombie_orders(self, exchange, symbol: str | None = None, max_age_minutes: int = 5) -> Dict:
        """
        🔒 FIX (Jan 17, 2026): Zombie Order Reaper
        
        Cancels orders on exchange that:
        1. Are NOT in our local order tracking
        2. Are NOT protective orders (SL/TP/reduceOnly)
        3. Are older than max_age_minutes
        
        This prevents orphan orders from prior sessions or crashes from filling unexpectedly.
        
        Args:
            exchange: CCXT exchange instance
            symbol: Symbol to check (None = all symbols)
            max_age_minutes: Only cancel orders older than this
        
        Returns:
            Dict with 'checked', 'cancelled', 'errors' counts
        """
        result = {
            'checked': 0,
            'cancelled': 0,
            'errors': 0,
            'zombie_orders': []
        }
        
        if not exchange:
            return result
        
        try:
            # Fetch all open orders - 🔥 FIX: Add timeout
            try:
                open_orders = await asyncio.wait_for(
                    exchange.fetch_open_orders(symbol) if symbol else exchange.fetch_open_orders(),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.error("⏰ TIMEOUT fetching open orders in cleanup_zombie_orders (10s)")
                return result
            
            now = datetime.now(UTC)
            
            for order in open_orders:
                result['checked'] += 1
                order_id = order.get('id')
                
                # Skip if we're tracking this order
                if order_id in self._orders:
                    continue
                
                # Skip protective orders (SL/TP)
                order_type = str(order.get('type', '')).lower()
                reduce_only = order.get('reduceOnly', order.get('info', {}).get('reduceOnly', False))
                
                if order_type in ('stop-loss', 'stop', 'take-profit', 'trailing-stop') or reduce_only:
                    continue
                
                # Check age
                timestamp = order.get('timestamp')
                if timestamp:
                    order_time = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
                    age_minutes = (now - order_time).total_seconds() / 60
                    
                    if age_minutes < max_age_minutes:
                        # Too new - might be a legitimate pending order
                        continue
                    
                    # This is a zombie order - cancel it!
                    order_symbol = order.get('symbol', symbol)
                    order_side = order.get('side', 'unknown')
                    order_price = order.get('price', 0)
                    
                    logger.warning(
                        f"🧟 ZOMBIE ORDER DETECTED: {order_id[:12]}... "
                        f"({order_side} {order_symbol} @ {order_price}) - {age_minutes:.1f}min old"
                    )
                    
                    try:
                        cancel_result = await exchange.cancel_order(order_id, order_symbol)
                        logger.info(f"✅ ZOMBIE ORDER CANCELLED: {order_id[:12]}...")
                        result['cancelled'] += 1
                        result['zombie_orders'].append({
                            'id': order_id,
                            'symbol': order_symbol,
                            'side': order_side,
                            'price': order_price,
                            'age_minutes': age_minutes,
                            'cancelled': True
                        })
                    except Exception as cancel_e:
                        logger.error(f"❌ Failed to cancel zombie order {order_id[:12]}...: {cancel_e}")
                        result['errors'] += 1
                        result['zombie_orders'].append({
                            'id': order_id,
                            'symbol': order_symbol,
                            'error': str(cancel_e)
                        })
            
            if result['cancelled'] > 0:
                logger.info(f"🧹 Zombie cleanup: {result['cancelled']} orders cancelled")
            
        except Exception as e:
            logger.error(f"Error in zombie order cleanup: {e}")
            result['errors'] += 1
        
        return result


# =============================================================================
# SINGLETON
# =============================================================================

_position_store: Optional[PositionStore] = None


def get_position_store() -> PositionStore:
    """Get the global position store instance."""
    global _position_store
    if _position_store is None:
        _position_store = PositionStore()
    return _position_store


def reset_position_store() -> None:
    """Reset position store. Only for testing."""
    global _position_store
    _position_store = None
