"""
Event Bus - Simple async event coordination system.

Provides decoupled communication between components:
- Data layer emits MarketEvents
- Strategy emits TradeIntentEvents
- Execution emits ExecutionEvents
- Risk emits HaltEvents

This replaces tightly-coupled callbacks with a clean pub/sub pattern.

USAGE:
    from core.events import get_event_bus, EventType, Event
    
    bus = get_event_bus()
    
    # Subscribe to events
    async def on_market_update(event: Event):
        print(f"New price: {event.data['price']}")
    
    bus.subscribe(EventType.MARKET_UPDATE, on_market_update)
    
    # Emit events
    await bus.emit(Event(
        type=EventType.MARKET_UPDATE,
        data={'price': 95000.0, 'symbol': 'BTC/USD'}
    ))
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum, auto
from typing import Dict, List, Callable, Any, Optional, Awaitable
import uuid

logger = logging.getLogger(__name__)


class EventType(Enum):
    """
    Types of events in the trading system.
    
    Grouped by source:
    - MARKET_*: From data layer
    - SIGNAL_*: From strategy layer
    - ORDER_*: From execution layer
    - POSITION_*: From position management
    - RISK_*: From risk management
    - SYSTEM_*: From system management
    """
    # Market data events
    MARKET_TICK = auto()            # New tick/trade
    MARKET_CANDLE = auto()          # New candle closed
    MARKET_ORDERBOOK = auto()       # Order book update
    MARKET_UPDATE = auto()          # Generic market update
    
    # Signal/Strategy events
    SIGNAL_GENERATED = auto()       # New trade signal
    SIGNAL_INVALIDATED = auto()     # Signal no longer valid
    TRADE_INTENT = auto()           # Strategy wants to trade
    
    # Order lifecycle events
    ORDER_SUBMITTED = auto()        # Order sent to exchange
    ORDER_ACCEPTED = auto()         # Order accepted by exchange
    ORDER_FILLED = auto()           # Order fully filled
    ORDER_PARTIAL_FILL = auto()     # Order partially filled
    ORDER_CANCELED = auto()         # Order canceled
    ORDER_REJECTED = auto()         # Order rejected
    ORDER_EXPIRED = auto()          # Order expired
    
    # Position events
    POSITION_OPENED = auto()        # New position opened
    POSITION_UPDATED = auto()       # Position size/price changed
    POSITION_CLOSED = auto()        # Position closed
    POSITION_RECONCILED = auto()    # Position synced with exchange
    
    # Risk events
    RISK_ALERT = auto()             # Risk threshold approaching
    RISK_HALT = auto()              # Trading halted by risk
    RISK_RESUME = auto()            # Trading resumed
    
    # System events
    SYSTEM_STARTUP = auto()         # System starting
    SYSTEM_SHUTDOWN = auto()        # System shutting down
    SYSTEM_ERROR = auto()           # System error
    SYSTEM_HEARTBEAT = auto()       # Periodic heartbeat


@dataclass
class Event:
    """
    An event in the trading system.
    
    Events are immutable messages that flow through the system.
    They carry:
    - Type: What kind of event
    - Data: Event-specific payload
    - Metadata: Timestamp, source, correlation ID
    """
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    
    # Metadata
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = ""                # Component that emitted the event
    correlation_id: str = ""        # Links related events (e.g., order → fill)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'type': self.type.name,
            'timestamp': self.timestamp.isoformat(),
            'source': self.source,
            'correlation_id': self.correlation_id,
            'data': self.data,
        }
    
    def __str__(self) -> str:
        return f"Event({self.type.name}, source={self.source}, data={self.data})"


# Type alias for event handlers
EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """
    Async event bus for component coordination.
    
    Features:
    - Async handlers (non-blocking)
    - Multiple handlers per event type
    - Event history for debugging
    - Error isolation (one handler failure doesn't stop others)
    """
    
    def __init__(self, max_history: int = 1000):
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._history: List[Event] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()
        self._running = True
        
        logger.info("EventBus initialized")
    
    def subscribe(
        self, 
        event_type: EventType, 
        handler: EventHandler,
        name: str = ""
    ) -> None:
        """
        Subscribe to an event type.
        
        Args:
            event_type: Type of event to subscribe to
            handler: Async function to call when event occurs
            name: Optional name for debugging
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        
        self._handlers[event_type].append(handler)
        handler_name = name or handler.__name__
        logger.debug(f"Subscribed {handler_name} to {event_type.name}")
    
    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> bool:
        """
        Unsubscribe from an event type.
        
        Returns:
            True if handler was found and removed
        """
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
                return True
            except ValueError:
                pass
        return False
    
    async def emit(self, event: Event) -> int:
        """
        Emit an event to all subscribers.
        
        Args:
            event: The event to emit
            
        Returns:
            Number of handlers that processed the event
        """
        if not self._running:
            logger.warning(f"EventBus stopped, dropping event: {event.type.name}")
            return 0
        
        # Store in history
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
        
        # Get handlers
        handlers = self._handlers.get(event.type, [])
        if not handlers:
            logger.debug(f"No handlers for {event.type.name}")
            return 0
        
        # Call handlers (with error isolation)
        processed = 0
        for handler in handlers:
            try:
                await handler(event)
                processed += 1
            except Exception as e:
                logger.error(
                    f"Handler {handler.__name__} failed for {event.type.name}: {e}",
                    exc_info=True
                )
        
        return processed
    
    async def emit_and_wait(
        self, 
        event: Event, 
        timeout: float = 5.0
    ) -> List[Any]:
        """
        Emit an event and wait for all handlers to complete.
        
        This is useful when you need synchronous-like behavior.
        
        Args:
            event: The event to emit
            timeout: Maximum wait time in seconds
            
        Returns:
            Results from all handlers
        """
        handlers = self._handlers.get(event.type, [])
        if not handlers:
            return []
        
        tasks = [
            asyncio.create_task(handler(event))
            for handler in handlers
        ]
        
        done, pending = await asyncio.wait(
            tasks, 
            timeout=timeout,
            return_when=asyncio.ALL_COMPLETED
        )
        
        # Cancel any pending tasks
        for task in pending:
            task.cancel()
        
        # Collect results
        results = []
        for task in done:
            try:
                result = task.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Handler failed: {e}")
        
        return results
    
    def get_history(
        self, 
        event_type: Optional[EventType] = None,
        limit: int = 100
    ) -> List[Event]:
        """
        Get recent event history.
        
        Args:
            event_type: Filter by event type (None = all)
            limit: Maximum events to return
            
        Returns:
            List of recent events
        """
        if event_type:
            events = [e for e in self._history if e.type == event_type]
        else:
            events = self._history.copy()
        
        return events[-limit:]
    
    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()
    
    async def shutdown(self) -> None:
        """Shutdown the event bus."""
        self._running = False
        logger.info("EventBus shutdown")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get event bus statistics."""
        return {
            'running': self._running,
            'handler_counts': {
                et.name: len(handlers) 
                for et, handlers in self._handlers.items()
            },
            'history_size': len(self._history),
            'max_history': self._max_history,
        }


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================

_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """
    Get the global event bus instance.
    
    Creates a new instance if one doesn't exist.
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def reset_event_bus() -> None:
    """Reset the event bus. Only for testing."""
    global _event_bus
    if _event_bus:
        asyncio.create_task(_event_bus.shutdown())
    _event_bus = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

async def emit_market_update(
    symbol: str,
    price: float,
    bid: float = 0.0,
    ask: float = 0.0,
    source: str = "market_data"
) -> None:
    """Helper to emit market update events."""
    event = Event(
        type=EventType.MARKET_UPDATE,
        source=source,
        data={
            'symbol': symbol,
            'price': price,
            'bid': bid,
            'ask': ask,
        }
    )
    await get_event_bus().emit(event)


async def emit_order_event(
    event_type: EventType,
    order_id: str,
    symbol: str,
    side: str,
    size: float,
    price: float,
    source: str = "execution"
) -> None:
    """Helper to emit order events."""
    event = Event(
        type=event_type,
        source=source,
        correlation_id=order_id,
        data={
            'order_id': order_id,
            'symbol': symbol,
            'side': side,
            'size': size,
            'price': price,
        }
    )
    await get_event_bus().emit(event)


async def emit_position_event(
    event_type: EventType,
    symbol: str,
    side: str,
    size: float,
    entry_price: float,
    unrealized_pnl: float = 0.0,
    source: str = "position_store"
) -> None:
    """Helper to emit position events."""
    event = Event(
        type=event_type,
        source=source,
        data={
            'symbol': symbol,
            'side': side,
            'size': size,
            'entry_price': entry_price,
            'unrealized_pnl': unrealized_pnl,
        }
    )
    await get_event_bus().emit(event)


async def emit_risk_halt(reason: str, source: str = "risk_engine") -> None:
    """Helper to emit risk halt event."""
    event = Event(
        type=EventType.RISK_HALT,
        source=source,
        data={'reason': reason}
    )
    await get_event_bus().emit(event)
