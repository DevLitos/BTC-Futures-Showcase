"""
Order Manager for BTC Perpetual Trading System
Handles limit post-only order execution and management

⚠️ DEPRECATED (Jan 4, 2026):
This module is NOT actively used in the current trading system.
All order management is handled by:
  - engine/execution.py (RESTExecutionEngine) for live trading
  - engine/signal_engine.py for order lifecycle

This file is kept for reference only. Do not use in new code.
If you need order management, use RESTExecutionEngine from execution.py.
"""

import asyncio
import logging
from datetime import datetime, UTC
from typing import Optional, Dict
from enum import Enum
import uuid
import warnings

# Emit deprecation warning when module is imported
warnings.warn(
    "order_manager.py is deprecated. Use execution.py (RESTExecutionEngine) instead.",
    DeprecationWarning,
    stacklevel=2
)

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Order status enumeration"""
    PENDING = "PENDING"         # Order created but not sent
    PLACED = "PLACED"           # Order sent to exchange
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"           # Order completely filled
    CANCELLED = "CANCELLED"     # Order cancelled
    REJECTED = "REJECTED"       # Order rejected by exchange
    TIMEOUT = "TIMEOUT"         # Order timeout


class Order:
    """Represents a trading order"""
    
    def __init__(self, symbol: str, direction: str, price: float, size: float):
        self.order_id = str(uuid.uuid4())[:8]
        self.symbol = symbol
        self.direction = direction  # 'LONG' or 'SHORT'
        self.order_type = 'LIMIT'
        self.post_only = True
        self.price = price
        self.size = size
        self.filled_size = 0.0
        self.status = OrderStatus.PENDING
        self.created_at = datetime.now()
        self.placed_at: Optional[datetime] = None
        self.filled_at: Optional[datetime] = None
        self.exchange_order_id: Optional[str] = None
        
        logger.info(f"Order created: {self.order_id} | {direction} {size} {symbol} @ {price:.2f}")
    
    def mark_placed(self, exchange_order_id: str):
        """Mark order as placed on exchange"""
        self.status = OrderStatus.PLACED
        self.placed_at = datetime.now()
        self.exchange_order_id = exchange_order_id
        logger.info(f"Order {self.order_id} placed on exchange: {exchange_order_id}")
    
    def mark_filled(self, filled_size: float):
        """Mark order as filled"""
        self.filled_size += filled_size
        
        if self.filled_size >= self.size:
            self.status = OrderStatus.FILLED
            self.filled_at = datetime.now()
            latency = (self.filled_at - self.created_at).total_seconds()
            logger.info(f"Order {self.order_id} FILLED completely | Latency: {latency:.2f}s")
        else:
            self.status = OrderStatus.PARTIALLY_FILLED
            logger.info(f"Order {self.order_id} partially filled: {self.filled_size}/{self.size}")
    
    def mark_cancelled(self):
        """Mark order as cancelled"""
        self.status = OrderStatus.CANCELLED
        logger.info(f"Order {self.order_id} CANCELLED")
    
    def mark_rejected(self, reason: str = ""):
        """Mark order as rejected"""
        self.status = OrderStatus.REJECTED
        logger.warning(f"Order {self.order_id} REJECTED: {reason}")
    
    def mark_timeout(self):
        """Mark order as timed out"""
        self.status = OrderStatus.TIMEOUT
        logger.warning(f"Order {self.order_id} TIMEOUT")
    
    def is_active(self) -> bool:
        """Check if order is still active"""
        return self.status in [OrderStatus.PENDING, OrderStatus.PLACED, OrderStatus.PARTIALLY_FILLED]
    
    def is_filled(self) -> bool:
        """Check if order is completely filled"""
        return self.status == OrderStatus.FILLED
    
    def get_age_seconds(self) -> float:
        """Get order age in seconds"""
        return (datetime.now() - self.created_at).total_seconds()


class OrderManager:
    """
    Manages order lifecycle: creation, placement, monitoring, cancellation
    Implements post-only limit orders with timeout
    """
    
    def __init__(self, exchange_connector=None):
        """
        exchange_connector: connector to exchange API (CCXT, custom, etc.)
        """
        self.exchange_connector = exchange_connector
        self.active_orders: Dict[str, Order] = {}
        self.order_history: list = []
        self._lock = asyncio.Lock()
    
    async def create_limit_order(self, symbol: str, direction: str, 
                                 price: float, size: float) -> Optional[Order]:
        """
        Create and place a limit post-only order
        Returns: Order object if successful, None otherwise
        """
        async with self._lock:
            # Create order object
            order = Order(symbol, direction, price, size)
            
            # Validate order
            if not self._validate_order(order):
                order.mark_rejected("Failed validation")
                self.order_history.append(order)
                return None
            
            # Place order on exchange
            if self.exchange_connector:
                success, exchange_id = await self._place_on_exchange(order)
                if not success:
                    order.mark_rejected("Exchange rejected")
                    self.order_history.append(order)
                    return None
                
                order.mark_placed(exchange_id)
            else:
                # Simulation mode
                order.mark_placed(f"SIM-{order.order_id}")
                logger.warning("No exchange connector - simulating order placement")
            
            # Track active order
            self.active_orders[order.order_id] = order
            
            return order
    
    def _validate_order(self, order: Order) -> bool:
        """Validate order before placement"""
        # Check minimum size
        if order.size <= 0:
            logger.error(f"Invalid order size: {order.size}")
            return False
        
        # Check price
        if order.price <= 0:
            logger.error(f"Invalid order price: {order.price}")
            return False
        
        # Additional validations can be added here
        return True
    
    async def _place_on_exchange(self, order: Order) -> tuple[bool, Optional[str]]:
        """
        Place order on exchange
        Returns: (success: bool, exchange_order_id: str)
        """
        try:
            if not self.exchange_connector:
                return False, None
            
            # Convert direction to exchange format
            side = 'buy' if order.direction == 'LONG' else 'sell'
            
            # Place limit post-only order
            exchange_order = await self.exchange_connector.create_order(
                symbol=order.symbol,
                type='limit',
                side=side,
                amount=order.size,
                price=order.price,
                params={'postOnly': True}
            )
            
            return True, exchange_order.get('id', 'UNKNOWN')
            
        except Exception as e:
            logger.error(f"Error placing order on exchange: {e}")
            return False, None
    
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an active order
        Returns: True if cancelled successfully
        """
        async with self._lock:
            order = self.active_orders.get(order_id)
            if not order:
                logger.warning(f"Order {order_id} not found in active orders")
                return False
            
            if not order.is_active():
                logger.warning(f"Order {order_id} is not active (status: {order.status.value})")
                return False
            
            # Cancel on exchange
            if self.exchange_connector and order.exchange_order_id:
                try:
                    await self.exchange_connector.cancel_order(
                        order.exchange_order_id,
                        order.symbol
                    )
                except Exception as e:
                    logger.error(f"Error cancelling order on exchange: {e}")
                    return False
            
            # Mark as cancelled
            order.mark_cancelled()
            
            # Move to history
            self.order_history.append(order)
            del self.active_orders[order_id]
            
            return True
    
    async def update_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """
        Update order status from exchange
        Returns: current OrderStatus or None if order not found
        """
        order = self.active_orders.get(order_id)
        if not order:
            return None
        
        if not self.exchange_connector or not order.exchange_order_id:
            # Simulation mode - simulate fill after random delay
            if order.status == OrderStatus.PLACED and order.get_age_seconds() > 5:
                order.mark_filled(order.size)
                if order.is_filled():
                    self.order_history.append(order)
                    del self.active_orders[order_id]
            return order.status
        
        try:
            # Fetch order status from exchange
            exchange_order = await self.exchange_connector.fetch_order(
                order.exchange_order_id,
                order.symbol
            )
            
            status = exchange_order.get('status', 'unknown')
            filled = exchange_order.get('filled', 0)
            
            if status == 'closed' or status == 'filled':
                order.mark_filled(filled)
                if order.is_filled():
                    self.order_history.append(order)
                    del self.active_orders[order_id]
            
            elif status == 'canceled' or status == 'cancelled':
                order.mark_cancelled()
                self.order_history.append(order)
                del self.active_orders[order_id]
            
            elif status == 'rejected':
                order.mark_rejected()
                self.order_history.append(order)
                del self.active_orders[order_id]
            
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
        
        return order.status
    
    async def monitor_order_timeout(self, order_id: str, timeout_seconds: float):
        """
        Monitor order and cancel if timeout exceeded
        """
        await asyncio.sleep(timeout_seconds)
        
        order = self.active_orders.get(order_id)
        if order and order.is_active():
            logger.warning(f"Order {order_id} timeout - cancelling")
            order.mark_timeout()
            await self.cancel_order(order_id)
    
    async def cancel_all_orders(self):
        """Cancel all active orders"""
        logger.warning("Cancelling all active orders")
        
        order_ids = list(self.active_orders.keys())
        for order_id in order_ids:
            await self.cancel_order(order_id)
    
    def get_active_order_count(self) -> int:
        """Get number of active orders"""
        return len(self.active_orders)
    
    def get_order_status_summary(self) -> dict:
        """Get summary of order statuses"""
        summary = {
            'active': len(self.active_orders),
            'history_total': len(self.order_history)
        }
        
        # Count historical orders by status
        for order in self.order_history:
            status_key = order.status.value.lower()
            summary[status_key] = summary.get(status_key, 0) + 1
        
        return summary
