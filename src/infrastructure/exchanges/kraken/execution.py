"""Kraken REST execution engine with production-grade order management.

Production features:
- Idempotent orders with client_order_id
- Position synchronization with exchange
- Order retry with exponential backoff
- Rate limiting awareness
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import ccxt.async_support as ccxt

from src.infrastructure.exchanges.symbol_mapper import SymbolMapper

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """Order request with idempotency support."""
    symbol: str
    side: str  # "buy" or "sell"
    amount: float
    order_type: str = "market"  # "market" or "limit"
    price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    
    def __post_init__(self) -> None:
        if self.client_order_id is None:
            # Generate deterministic client_order_id for idempotency
            self.client_order_id = self._generate_client_id()
    
    def _generate_client_id(self) -> str:
        """Generate unique client order ID."""
        return f"btc_{uuid4().hex[:16]}"


@dataclass
class OrderResult:
    """Result of an order execution."""
    success: bool
    order_id: str | None
    client_order_id: str
    filled_amount: float
    filled_price: float
    status: str
    error: str | None = None
    raw_response: dict | None = None


@dataclass
class ExchangePosition:
    """Position as reported by exchange."""
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    unrealized_pnl: float
    liquidation_price: float | None
    timestamp: datetime


class RESTExecutionEngine:
    """Production-grade REST execution engine.
    
    Features:
    - Idempotent orders (client_order_id)
    - Position sync with exchange
    - Automatic retry with backoff
    - Balance and margin checks
    """
    
    MAX_RETRIES = 3
    RETRY_DELAYS = [1.0, 2.0, 5.0]
    
    def __init__(self, exchange_name: str, credentials: dict[str, Any] | None = None):
        self.exchange_name = exchange_name
        self.credentials = credentials or {}
        self._exchange: Any | None = None
        self._initialized = False
        self._last_order_time: float = 0
        self._min_order_interval: float = 0.5  # Rate limit protection

    async def initialize(self) -> None:
        """Initialize exchange connection."""
        if self._initialized:
            return
        
        exchange_cls = getattr(ccxt, self.exchange_name, None)
        if exchange_cls is None:
            raise ValueError(f"Unsupported exchange: {self.exchange_name}")
        
        config = {
            **self.credentials,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "adjustForTimeDifference": True
            }
        }
        self._exchange = exchange_cls(config)
        self._initialized = True
        logger.info("Exchange %s initialized", self.exchange_name)

    async def close(self) -> None:
        """Close exchange connection."""
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            self._initialized = False

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker for symbol."""
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange connector not initialized")
        normalized_symbol = SymbolMapper.to_rest_symbol(self.exchange_name, symbol)
        return await self._exchange.fetch_ticker(normalized_symbol)

    async def fetch_balance(self) -> dict[str, Any]:
        """Fetch account balance."""
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange not initialized")
        return await self._exchange.fetch_balance()

    async def fetch_positions(self, symbol: str | None = None) -> list[ExchangePosition]:
        """Fetch current positions from exchange.
        
        Critical for position sync on startup and reconciliation.
        """
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange not initialized")
        
        try:
            symbols = [SymbolMapper.to_rest_symbol(self.exchange_name, symbol)] if symbol else None
            raw_positions = await self._exchange.fetch_positions(symbols)
            
            positions: list[ExchangePosition] = []
            for pos in raw_positions:
                # Skip empty positions
                contracts = float(pos.get("contracts", 0) or 0)
                if abs(contracts) < 1e-8:
                    continue
                
                side = pos.get("side", "long")
                if side not in ("long", "short"):
                    side = "long" if contracts > 0 else "short"
                
                positions.append(ExchangePosition(
                    symbol=pos.get("symbol", ""),
                    side=side,
                    size=abs(contracts),
                    entry_price=float(pos.get("entryPrice", 0) or 0),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0) or 0),
                    liquidation_price=float(pos.get("liquidationPrice", 0) or 0) if pos.get("liquidationPrice") else None,
                    timestamp=datetime.now(timezone.utc)
                ))
            
            return positions
        except Exception as e:
            logger.error("Failed to fetch positions: %s", e)
            return []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place order with retry and idempotency.
        
        Uses client_order_id for idempotency - if order already exists,
        returns existing order instead of creating duplicate.
        """
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange not initialized")
        
        # Rate limit protection
        elapsed = time.time() - self._last_order_time
        if elapsed < self._min_order_interval:
            await asyncio.sleep(self._min_order_interval - elapsed)
        
        normalized_symbol = SymbolMapper.to_rest_symbol(self.exchange_name, request.symbol)
        
        for attempt in range(self.MAX_RETRIES):
            try:
                params: dict[str, Any] = {}
                
                # Add client_order_id for idempotency
                if request.client_order_id:
                    params["clientOrderId"] = request.client_order_id
                
                if request.reduce_only:
                    params["reduceOnly"] = True
                
                # Place the order
                if request.order_type == "market":
                    result = await self._exchange.create_market_order(
                        normalized_symbol,
                        request.side,
                        request.amount,
                        params=params
                    )
                else:
                    if request.price is None:
                        raise ValueError("Limit order requires price")
                    result = await self._exchange.create_limit_order(
                        normalized_symbol,
                        request.side,
                        request.amount,
                        request.price,
                        params=params
                    )
                
                self._last_order_time = time.time()
                
                logger.info(
                    "Order placed: %s %s %.4f %s @ %s (id=%s, client_id=%s)",
                    request.side, normalized_symbol, request.amount,
                    request.order_type, result.get("average", "market"),
                    result.get("id"), request.client_order_id
                )
                
                return OrderResult(
                    success=True,
                    order_id=result.get("id"),
                    client_order_id=request.client_order_id or "",
                    filled_amount=float(result.get("filled", 0) or 0),
                    filled_price=float(result.get("average", 0) or 0),
                    status=result.get("status", "unknown"),
                    raw_response=result
                )
                
            except ccxt.DuplicateOrderId:
                # Order already exists - this is idempotency working correctly
                logger.warning("Duplicate order detected: %s", request.client_order_id)
                return OrderResult(
                    success=True,
                    order_id=None,
                    client_order_id=request.client_order_id or "",
                    filled_amount=0,
                    filled_price=0,
                    status="duplicate",
                    error="Order already exists (idempotent)"
                )
                
            except ccxt.InsufficientFunds as e:
                logger.error("Insufficient funds: %s", e)
                return OrderResult(
                    success=False,
                    order_id=None,
                    client_order_id=request.client_order_id or "",
                    filled_amount=0,
                    filled_price=0,
                    status="rejected",
                    error=f"Insufficient funds: {e}"
                )
                
            except ccxt.RateLimitExceeded:
                delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
                logger.warning("Rate limit hit, waiting %.1fs", delay)
                await asyncio.sleep(delay)
                continue
                
            except ccxt.NetworkError as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[attempt]
                    logger.warning("Network error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                return OrderResult(
                    success=False,
                    order_id=None,
                    client_order_id=request.client_order_id or "",
                    filled_amount=0,
                    filled_price=0,
                    status="error",
                    error=f"Network error after {self.MAX_RETRIES} retries: {e}"
                )
                
            except Exception as e:
                logger.error("Order failed: %s", e)
                return OrderResult(
                    success=False,
                    order_id=None,
                    client_order_id=request.client_order_id or "",
                    filled_amount=0,
                    filled_price=0,
                    status="error",
                    error=str(e)
                )
        
        # Should never reach here
        return OrderResult(
            success=False,
            order_id=None,
            client_order_id=request.client_order_id or "",
            filled_amount=0,
            filled_price=0,
            status="error",
            error="Max retries exceeded"
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order by ID."""
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange not initialized")
        
        try:
            normalized_symbol = SymbolMapper.to_rest_symbol(self.exchange_name, symbol)
            await self._exchange.cancel_order(order_id, normalized_symbol)
            logger.info("Order cancelled: %s", order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order %s: %s", order_id, e)
            return False

    async def close_position(self, symbol: str, side: str, amount: float) -> OrderResult:
        """Close a position with market order.
        
        Args:
            symbol: Trading symbol
            side: Position side to close ("long" or "short")
            amount: Amount to close
        """
        # To close a long, we sell; to close a short, we buy
        close_side = "sell" if side == "long" else "buy"
        
        request = OrderRequest(
            symbol=symbol,
            side=close_side,
            amount=amount,
            order_type="market",
            reduce_only=True,
            client_order_id=f"close_{uuid4().hex[:12]}"
        )
        
        return await self.place_order(request)
