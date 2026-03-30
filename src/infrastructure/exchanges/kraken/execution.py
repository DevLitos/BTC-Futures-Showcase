""" 
Execution Engine - REST ONLY para órdenes
SEPARADO de WebSocket (datos)

REGLA DE ORO: WebSocket = datos, REST = ejecución
"""

import asyncio
import logging
import math
import random
from typing import Optional, Dict, Tuple, Callable, TypeVar, Any
from functools import wraps
import ccxt.async_support as ccxt
from datetime import datetime, UTC

from src.config.settings import generate_client_order_id
from src.infrastructure.storage import get_position_store

logger = logging.getLogger(__name__)


def safe_float(value, default: float = 0.0) -> float:
    """Safely convert value to float, handling None and invalid types."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# =============================================================================
# DOMAIN EXCEPTIONS
# =============================================================================

class ExecutionError(Exception):
    """Base exception for execution-related errors."""
    pass


class OrderPlacementError(ExecutionError):
    """Error placing an order on the exchange."""
    pass


class StopLossSetupError(ExecutionError):
    """Error setting up stop-loss order."""
    pass


class TakeProfitSetupError(ExecutionError):
    """Error setting up take-profit order."""
    pass


class OrderRejectedError(ExecutionError):
    """Order was rejected by the exchange."""
    pass


class ExchangeTimeoutError(ExecutionError):
    """Exchange operation timed out."""
    pass


class InsufficientMarginError(ExecutionError):
    """Insufficient margin for the operation."""
    pass


class PositionMismatchError(ExecutionError):
    """Local position state doesn't match exchange."""
    pass


# =============================================================================
# TIMEOUTS CONFIGURATION
# =============================================================================

# Timeouts para operaciones de exchange (segundos)
# These can be overridden via Settings if needed
# Reduced MEDIUM from 30s to 10s
# 30s was causing system hangs during API issues. 10s is enough for 
# order operations and with existing retry+backoff, transient failures recover.
EXCHANGE_TIMEOUT_SHORT = 5.0
EXCHANGE_TIMEOUT_MEDIUM = 10.0
EXCHANGE_TIMEOUT_LONG = 30.0


# CRITICAL: Threshold for float comparison of position sizes
# Avoids false positives from floating-point residue (e.g., 0.00000001)
MIN_POSITION_THRESHOLD = 0.00001  # 0.001% of 1 BTC

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY = 1.0  # Base delay in seconds
MAX_DELAY = 30.0  # Maximum delay in seconds

T = TypeVar('T')


def with_retry(
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
    max_delay: float = MAX_DELAY,
    retryable_exceptions: tuple = (
        asyncio.TimeoutError,
        ccxt.NetworkError,
        ccxt.ExchangeNotAvailable,
        ccxt.RequestTimeout,
        ccxt.DDoSProtection,
    )
):
    """
    Decorator for async functions with exponential backoff retry.
    
    Uses exponential backoff with jitter to avoid thundering herd.
    Only retries on transient errors, not on business logic errors.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap
        retryable_exceptions: Tuple of exceptions to retry on
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception: Exception | None = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)  # pyright: ignore[reportReturnType, reportGeneralTypeIssues]
                except retryable_exceptions as e:
                    last_exception = e
                    
                    if attempt == max_retries:
                        logger.error(
                            f"🔄 {func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise
                    
                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = delay * 0.1 * random.random()
                    actual_delay = delay + jitter
                    
                    logger.warning(
                        f"🔄 {func.__name__} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                        f"Retrying in {actual_delay:.1f}s..."
                    )
                    await asyncio.sleep(actual_delay)
            
            # Should not reach here, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected: no exception but loop finished")  # pyright: ignore[reportUnreachable]
                
        return wrapper  # pyright: ignore[reportReturnType]
    return decorator  # pyright: ignore[reportReturnType]

# =========================================================================
# SIZE PRECISION CONSTANTS (Kraken Futures BTC/USD:USD)
# =========================================================================
# These MUST match exchange requirements to prevent size desync
KRAKEN_BTC_MIN_SIZE = 0.0001      # Minimum order size in BTC
KRAKEN_BTC_STEP_SIZE = 0.0001     # Size increment step
KRAKEN_BTC_PRECISION = 4          # Decimal places for size


def round_size_to_exchange(size: float, min_size: float = KRAKEN_BTC_MIN_SIZE, 
                           step_size: float = KRAKEN_BTC_STEP_SIZE,
                           precision: int = KRAKEN_BTC_PRECISION) -> float:
    """
    Round size DOWN to exchange precision to prevent desync.
    
    CRITICAL: Always round DOWN (floor) to ensure we don't exceed available margin.
    The exchange will reject if we round up and exceed limits.
    
    Args:
        size: Desired position size
        min_size: Exchange minimum order size
        step_size: Exchange size increment step
        precision: Decimal places for rounding
        
    Returns:
        Rounded size (or 0 if below minimum)
    """
    if size < min_size:
        logger.warning(f"⚠️ Size {size:.8f} below exchange minimum {min_size} → 0")
        return 0.0
    
    # Round down to step size
    steps = math.floor(size / step_size)
    rounded = steps * step_size
    
    # Apply precision rounding (in case of float artifacts)
    rounded = round(rounded, precision)
    
    if rounded != size:
        logger.debug(f"📊 Size rounded: {size:.8f} → {rounded:.8f} (step={step_size})")
    
    return rounded


class RESTExecutionEngine:
    """
    Ejecución de órdenes vía REST
    - CCXT solo para colocar/cancelar/consultar
    - Nunca datos
    - Post-only obligatorio
    """
    
    def __init__(self, exchange: str = 'binance', credentials: dict | None = None):
        self.exchange_name = exchange
        self.credentials = credentials or {}
        self.exchange = None
        
        # Lock to serialize CCXT access
        # CCXT async can deadlock when multiple coroutines access the same exchange
        self._exchange_lock = asyncio.Lock()
        
        # 🔥 DEBUG (Jan 21, 2026): Track concurrent access attempts
        self._active_ccxt_call = None
        self._ccxt_call_count = 0
        
        self.open_orders = {}
        self.order_history = []
        
        # State tracking para logging de posiciones
        self._last_position_state = {}  # {symbol: (contracts, notional)}
        
        # EXCHANGE AVAILABILITY TRACKING with circuit breaker
        self.is_exchange_available = True
        self._consecutive_rest_failures = 0
        self._last_rest_failure_time: float = 0
        self._rest_failure_log_count = 0  # For log throttling
        self._last_successful_rest_call: float = 0
        
        # 🔒 CIRCUIT BREAKER (Jan 22, 2026): Exponential backoff to avoid hammering
        self._circuit_breaker_until: float = 0  # Timestamp when we can retry
        self._backoff_seconds = 2.0  # Initial backoff
        self._max_backoff_seconds = 60.0  # Cap at 60 seconds
        
        logger.info(f"RESTExecutionEngine initialized for {exchange}")
    
    async def _reset_exchange_session(self):
        """Reset CCXT exchange session to recover from stuck states."""
        try:
            if self.exchange:
                await self.exchange.close()
        except Exception:
            pass
        
        ExchangeClass = getattr(ccxt, self.exchange_name)
        config = dict(self.credentials)
        config['timeout'] = 5000  # 5 second internal timeout
        self.exchange = ExchangeClass(config)
        await self.exchange.load_markets()
        logger.info("🔄 Exchange session reset")
    
    @with_retry(max_retries=3, base_delay=2.0)
    async def initialize(self):
        """Inicializar conexión a exchange con retry automático"""
        try:
            ExchangeClass = getattr(ccxt, self.exchange_name)
            
            # Simple config - just pass credentials as-is
            # Add explicit timeout to CCXT
            config = dict(self.credentials)
            config['timeout'] = 5000  # 5 second internal timeout
            self.exchange = ExchangeClass(config)
            
            await asyncio.wait_for(
                self.exchange.load_markets(),
                timeout=EXCHANGE_TIMEOUT_MEDIUM
            )
            logger.info(f"✅ Connected to {self.exchange_name}")
            
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to {self.exchange_name}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize exchange: {e}")
            raise
    
    @with_retry(max_retries=3, base_delay=2.0)
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        """Fetch OHLCV candles from exchange, returns list of dicts"""
        try:
            raw = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not raw:
                return []
            candles = []
            for row in raw:
                candles.append({
                    'timestamp': row[0],
                    'open': float(row[1]),
                    'high': float(row[2]),
                    'low': float(row[3]),
                    'close': float(row[4]),
                    'volume': float(row[5]) if len(row) > 5 else 0.0,
                    'timeframe': timeframe,
                })
            return candles
        except Exception as e:
            logger.error(f"Error fetching OHLCV {timeframe}: {e}")
            raise
    
    async def place_limit_order(self, symbol: str, side: str, amount: float, 
                               price: float, stop_loss: float | None = None, 
                               take_profit: float | None = None,
                               client_order_id: str | None = None) -> Optional[Dict]:
        """
        Colocar orden limit post-only con SL/TP ATOMICOS
        
        Args:
            stop_loss: Precio de stop loss (se envía en la misma orden)
            take_profit: Precio de take profit (se envía en la misma orden)
            client_order_id: Idempotent client-generated order ID for retry safety
        
        Returns: order object o None si falla
        
        IMPORTANTE: SL/TP se colocan ATOMICAMENTE con la orden de entrada.
        Kraken Futures los acepta como parámetros stopLoss/takeProfit.
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return None
        
        # 🔥 PRE-ROUND SIZE to exchange precision to prevent desync
        original_amount = amount
        amount = round_size_to_exchange(amount)
        if amount == 0 or amount < KRAKEN_BTC_MIN_SIZE:
            logger.error(f"⛔ Cannot place limit order: size {original_amount:.8f} → {amount:.8f} (min: {KRAKEN_BTC_MIN_SIZE})")
            return None
        if amount != original_amount:
            logger.info(f"📊 Size pre-rounded: {original_amount:.8f} → {amount:.8f}")
        
        # Generate idempotent client order ID if not provided
        if client_order_id is None:
            client_order_id = generate_client_order_id(
                symbol=symbol,
                side=side,
                timestamp=datetime.now(UTC),
                strategy_id="limit"
            )
        
        try:
            # Build params for Kraken Futures
            params = {
                'timeInForce': 'GTC',
                'clientOrderId': client_order_id,
                # PASO 2 — MAKER ONLY REAL (SIN EXCEPCIONES)
                # Ensure entry is strictly post-only.
                'postOnly': True,
            }
            
            # ⚠️ IMPORTANT: Kraken Futures NO acepta stopLoss/takeProfit en órdenes LIMIT
            # Solo las acepta en órdenes MARKET
            # Por tanto: Colocamos la orden de entrada SIN SL/TP
            # Los SL/TP se colocarán DESPUÉS cuando la posición esté abierta
            
            logger.info(f"Placing {side.upper()} order: {amount} {symbol} @ {price} (clientId={client_order_id})")
            
            # Use lock to prevent concurrent CCXT access
            async with self._exchange_lock:
                try:
                    order = await asyncio.wait_for(
                        self.exchange.create_order(
                            symbol=symbol,
                            type='limit',
                            side=side.lower(),
                            amount=amount,
                            price=price,
                            params=params
                        ),
                        timeout=EXCHANGE_TIMEOUT_MEDIUM
                    )
                except asyncio.TimeoutError:
                    logger.error(f"⏰ TIMEOUT placing order (30s)")
                    return None
            
            order['created_at'] = datetime.now(UTC).isoformat()
            order['clientOrderId'] = client_order_id
            self.open_orders[order['id']] = order
            
            # Store SL/TP for later placement (when position opens)
            if stop_loss or take_profit:
                order['_pending_sl'] = stop_loss
                order['_pending_tp'] = take_profit
                logger.info(f"   📌 SL @ {stop_loss:.2f} | 🎯 TP @ {take_profit:.2f} (will be set after fill)")
            
            logger.info(f"✅ Order placed: {order['id']}")
            logger.debug(f"Order details: {order}")
            return order
            
        except ccxt.errors.OrderNotFound:
            logger.error("Order not found on exchange")
            return None
        except ccxt.InsufficientFunds:
            logger.error("Insufficient balance")
            return None
        except ccxt.errors.DDoSProtection:
            logger.warning("Exchange rate limit - waiting 30s")
            await asyncio.sleep(30)
            return None
        except Exception as e:
            error_str = str(e).lower()
            # postWouldExecute = limit order would cross spread (act as taker)
            # This is EXPECTED with postOnly=True - means price moved too fast
            if 'postwouldexecute' in error_str:
                logger.warning(
                    f"🚫 POST-ONLY REJECTED: Price moved - would execute as taker | "
                    f"CORRECT: Not chasing market"
                )
            else:
                logger.error(f"Error placing order: {e}")
            return None
    
    async def place_aggressive_order(self, symbol: str, side: str, amount: float,
                                     price: float, stop_loss: float | None = None,
                                     take_profit: float | None = None,
                                     client_order_id: str | None = None) -> Optional[Dict]:
        """
        🔥 DISPLACEMENT AGGRESSIVE ENTRY (Jan 30, 2026)
        
        Place order with IOC (Immediate-Or-Cancel) instead of post-only.
        Used during DISPLACEMENT DIRECT ENTRY WINDOW when:
        - Market is moving fast
        - Post-only would reject
        - Better to fill as taker than miss the trade
        
        Key differences from place_limit_order:
        - postOnly=False (allows taker)
        - timeInForce='IOC' (fill immediately or cancel)
        - Price offset: +0.1% for BUY, -0.1% for SELL (crosses spread)
        
        Args:
            Similar to place_limit_order
            
        Returns:
            order object or None if fails/not filled
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return None
        
        # Pre-round size
        original_amount = amount
        amount = round_size_to_exchange(amount)
        if amount == 0 or amount < KRAKEN_BTC_MIN_SIZE:
            logger.error(f"⛔ Cannot place aggressive order: size {original_amount:.8f} → {amount:.8f} (min: {KRAKEN_BTC_MIN_SIZE})")
            return None
        if amount != original_amount:
            logger.info(f"📊 Size pre-rounded: {original_amount:.8f} → {amount:.8f}")
        
        # Generate idempotent client order ID
        if client_order_id is None:
            client_order_id = generate_client_order_id(
                symbol=symbol,
                side=side,
                timestamp=datetime.now(UTC),
                strategy_id="aggro"
            )
        
        # 🔥 AGGRESSIVE PRICE: Cross the spread to ensure fill
        # For BUY: price + 0.1% (willing to pay more)
        # For SELL: price - 0.1% (willing to accept less)
        AGGRO_OFFSET_PCT = 0.001  # 0.1%
        if side.lower() == 'buy':
            aggressive_price = price * (1 + AGGRO_OFFSET_PCT)
        else:
            aggressive_price = price * (1 - AGGRO_OFFSET_PCT)
        
        try:
            params = {
                'timeInForce': 'IOC',  # Immediate-Or-Cancel
                'clientOrderId': client_order_id,
                'postOnly': False,  # Allow taker execution
            }
            
            logger.warning(
                f"🚀 AGGRESSIVE {side.upper()}: {amount} {symbol} @ {aggressive_price:.2f} "
                f"(original: {price:.2f}, offset: {AGGRO_OFFSET_PCT*100:.1f}%) [IOC]"
            )
            
            async with self._exchange_lock:
                try:
                    order = await asyncio.wait_for(
                        self.exchange.create_order(
                            symbol=symbol,
                            type='limit',
                            side=side.lower(),
                            amount=amount,
                            price=aggressive_price,
                            params=params
                        ),
                        timeout=EXCHANGE_TIMEOUT_MEDIUM
                    )
                except asyncio.TimeoutError:
                    logger.error(f"⏰ TIMEOUT placing aggressive order (30s)")
                    return None
            
            # Check if IOC was filled
            filled_amount = order.get('filled', 0)
            order_status = order.get('status', 'unknown')
            
            if filled_amount > 0:
                order['created_at'] = datetime.now(UTC).isoformat()
                order['clientOrderId'] = client_order_id
                order['_aggressive'] = True
                self.open_orders[order['id']] = order
                
                if stop_loss or take_profit:
                    order['_pending_sl'] = stop_loss
                    order['_pending_tp'] = take_profit
                    logger.info(f"   📌 SL @ {stop_loss:.2f} | 🎯 TP @ {take_profit:.2f} (will be set after fill)")
                
                logger.info(
                    f"🚀 AGGRESSIVE ORDER FILLED: {order['id']} | "
                    f"Filled: {filled_amount}/{amount} @ {order.get('average', aggressive_price):.2f}"
                )
                return order
            else:
                logger.warning(
                    f"🚫 AGGRESSIVE ORDER NOT FILLED: IOC expired | "
                    f"Status: {order_status} | Price gap too large or no liquidity"
                )
                return None
                
        except ccxt.errors.OrderNotFound:
            logger.error("Aggressive order not found on exchange")
            return None
        except ccxt.InsufficientFunds:
            logger.error("Insufficient balance for aggressive order")
            return None
        except ccxt.errors.DDoSProtection:
            logger.warning("Exchange rate limit - waiting 30s")
            await asyncio.sleep(30)
            return None
        except Exception as e:
            logger.error(f"Error placing aggressive order: {e}")
            return None
    
    async def cancel_all_orders(self, symbol: str) -> int:
        """
        Cancelar TODAS las órdenes abiertas para un símbolo
        Returns: cantidad de órdenes canceladas
        """
        if not self.exchange:
            return 0
        
        try:
            open_orders = await asyncio.wait_for(
                self.exchange.fetch_open_orders(symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            cancelled_count = 0
            
            for order in open_orders:
                try:
                    await asyncio.wait_for(
                        self.exchange.cancel_order(order['id'], symbol),
                        timeout=EXCHANGE_TIMEOUT_SHORT
                    )
                    logger.info(f"✅ Cancelled order: {order['id']} ({order.get('type')} {order.get('side')})")
                    cancelled_count += 1
                except asyncio.TimeoutError:
                    logger.error(f"Timeout cancelling order {order['id']}")
                except Exception as e:
                    logger.error(f"Failed to cancel order {order['id']}: {e}")
            
            return cancelled_count
        
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching open orders for {symbol}")
            return 0
        except Exception as e:
            logger.error(f"Error cancelling all orders: {e}")
            return 0
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """
        Cancelar orden con timeout.
        
        Detecta ordenes que ya no existen (cancelled/filled externally)
        y las trata como éxito para evitar loops infinitos de reintento.
        
        Returns:
            True si orden cancelada o ya no existe (safe to proceed)
            False solo si hay error real de red/exchange
        """
        if not self.exchange:
            return False
        
        try:
            await asyncio.wait_for(
                self.exchange.cancel_order(order_id, symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            
            if order_id in self.open_orders:
                order = self.open_orders.pop(order_id)
                order['cancelled_at'] = datetime.now(UTC).isoformat()
                self.order_history.append(order)
            
            logger.info(f"✅ Order cancelled: {order_id}")
            return True
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Order doesn't exist = already cancelled/filled/expired
            # This is NOT an error - the order is gone, which is what we wanted
            if 'notfound' in error_str or 'not found' in error_str or 'unknown order' in error_str:
                logger.warning(f"⚠️ Order {order_id[:12]}... not found - already gone (cancelled/filled/expired)")
                
                # Clean up local tracking if exists
                if order_id in self.open_orders:
                    order = self.open_orders.pop(order_id)
                    order['cancelled_at'] = datetime.now(UTC).isoformat()
                    order['cancel_reason'] = 'NOT_FOUND_ON_EXCHANGE'
                    self.order_history.append(order)
                
                return True  # 🔥 Return True - order is gone, mission accomplished
            
            logger.error(f"Error cancelling order: {e}")
            return False
    
    async def get_open_orders(self, symbol: str) -> list:
        """Consultar órdenes abiertas directamente del exchange"""
        if not self.exchange:
            return []
        
        # 🔒 CIRCUIT BREAKER: Don't spam if in backoff
        if self.is_circuit_breaker_open():
            logger.debug(f"Circuit breaker open, skipping get_open_orders (wait {self.get_circuit_breaker_wait_time():.0f}s)")
            return []
        
        try:
            # 🔥 AUDIT FIX: Add timeout to prevent hanging
            orders = await asyncio.wait_for(
                self.exchange.fetch_open_orders(symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            # Success - reset circuit breaker (but don't log recovery for routine calls)
            if not self.is_exchange_available:
                self._reset_circuit_breaker()
            return orders
        except asyncio.TimeoutError:
            self._handle_rest_error(Exception("Timeout fetching open orders"), "get_open_orders")
            return []
        except Exception as e:
            self._handle_rest_error(e, "get_open_orders")
            return []
    
    async def get_order_status(self, order_id: str, symbol: str) -> Optional[Dict]:
        """
        Fetch a single order by ID from the exchange.
        
        Called by poll_fill_status() to check order fill progress.
        Uses ccxt fetch_order (REST).
        
        Returns:
            Dict with order info if found, None otherwise.
        """
        if not self.exchange:
            return None
        
        if self.is_circuit_breaker_open():
            logger.debug("Circuit breaker open, skipping get_order_status")
            return None
        
        try:
            order = await asyncio.wait_for(
                self.exchange.fetch_order(order_id, symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            if not self.is_exchange_available:
                self._reset_circuit_breaker()
            return order
        except asyncio.TimeoutError:
            self._handle_rest_error(Exception(f"Timeout fetching order {order_id}"), "get_order_status")
            return None
        except Exception as e:
            # Order not found is common for IOC fills — don't trigger circuit breaker
            err_str = str(e).lower()
            if 'not found' in err_str or 'unknown order' in err_str:
                logger.debug(f"Order {order_id} not found on exchange: {e}")
                return None
            self._handle_rest_error(e, "get_order_status")
            return None
    
    async def poll_fill_status(self, order_id: str, symbol: str, 
                              timeout_seconds: int = 180) -> dict:
        """
        Poll orden hasta que se llene o timeout
        CCXT approach: consulta REST cada X ms
        
        Returns: dict with 'filled': bool, 'amount': float (actual filled amount)
        
        Uses monotonic deadline to guarantee exit even if individual calls hang.
        """
        import time
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 2.5  # seconds (reduced polling frequency)
        result = {'filled': False, 'amount': 0.0, 'average_price': 0.0}
        not_found_count = 0
        max_not_found = 3  # After 3 consecutive not-found, check position before assuming rejected
        consecutive_errors = 0
        max_consecutive_errors = 5  # Bail out after 5 consecutive errors
        
        while time.monotonic() < deadline:
            # 🔒 Check deadline at start of loop body too
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                break
            
            try:
                # Use wait_for to prevent get_order_status from hanging forever
                order = await asyncio.wait_for(
                    self.get_order_status(order_id, symbol),
                    timeout=min(10.0, max(1.0, remaining_time))  # Min 1s, max 10s
                )
            
                if not order:
                    not_found_count += 1
                    if not_found_count >= max_not_found:
                        # Before assuming rejection, check multiple sources:
                        # 1. Check if position exists (instant fill case)
                        # 2. Check recent trades (fills)
                        
                        # Method 1: Position check
                        for pos_attempt in range(3):
                            try:
                                if pos_attempt > 0:
                                    await asyncio.sleep(0.5)
                                position = await self.get_position(symbol)
                                if position and abs(position.get('contracts', 0)) > 0:
                                    contracts = abs(position.get('contracts', 0))
                                    entry_price = position.get('entryPrice', 0)
                                    logger.info(f"✅ Order {order_id} INSTANT FILL detected via position (attempt {pos_attempt+1}): {contracts} @ {entry_price}")
                                    result = {
                                        'filled': True,
                                        'amount': contracts,
                                        'average_price': entry_price,
                                        'instant_fill': True
                                    }
                                    return result
                            except Exception as e:
                                logger.debug(f"Position check attempt {pos_attempt+1} failed: {e}")
                        
                        # Method 2: Check recent trades for this order
                        try:
                            if self.exchange is None:
                                raise RuntimeError("Exchange not initialized")
                            trades = await asyncio.wait_for(
                                self.exchange.fetch_my_trades(symbol, limit=20),
                                timeout=EXCHANGE_TIMEOUT_SHORT
                            )
                            for trade in trades:
                                if trade.get('order') == order_id:
                                    filled_amount = trade.get('amount', 0)
                                    avg_price = trade.get('price', 0)
                                    logger.info(f"✅ Order {order_id} FILL detected via trades: {filled_amount} @ {avg_price}")
                                    result = {
                                        'filled': True,
                                        'amount': filled_amount,
                                        'average_price': avg_price,
                                        'via_trades': True
                                    }
                                    return result
                        except Exception as e:
                            logger.debug(f"Trades check failed: {e}")
                        
                        # No position or trades found - order was truly rejected
                        logger.warning(f"🚫 Order {order_id} not found after {not_found_count} attempts and no position/trades - REJECTED by exchange")
                        result['rejected'] = True
                        return result
                    await asyncio.sleep(poll_interval)
                    continue
                
                # Reset counter if we found order
                not_found_count = 0
                
                status = order.get('status')
                filled = order.get('filled', 0)
                amount_requested = order.get('amount', 0)
                
                if status == 'closed' or filled >= amount_requested:
                    result = {
                        'filled': True, 
                        'amount': filled, 
                        'average_price': order.get('average', 0)
                    }
                    logger.info(f"✅ Order FILLED: {order_id} ({filled} @ {result['average_price']:.2f})")
                    return result
                
                elif status == 'canceled':
                    # PARTIAL FILL HANDLING: Return what was filled
                    if filled > 0:
                        result = {
                            'filled': True,  # Partial is still a fill
                            'amount': filled,
                            'average_price': order.get('average', 0),
                            'partial': True
                        }
                        logger.warning(f"⚠️ Order PARTIAL FILL: {order_id} ({filled}/{amount_requested} @ {result['average_price']:.2f})")
                        return result
                    logger.warning(f"Order CANCELLED: {order_id}")
                    return result
                
                elif status == 'rejected':
                    logger.error(f"Order REJECTED: {order_id}")
                    return result
                
                # Still pending - reset error counter on successful iteration
                consecutive_errors = 0
                logger.debug(f"Order {order_id} pending ({filled}/{amount_requested})")
                await asyncio.sleep(poll_interval)
            
            except asyncio.TimeoutError:
                consecutive_errors += 1
                logger.warning(f"Timeout in poll_fill_status get_order_status (attempt {consecutive_errors}/{max_consecutive_errors})")
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"🚫 Bailing out of poll_fill_status after {consecutive_errors} consecutive timeouts for order {order_id}")
                    result['error'] = 'timeout'
                    return result
                
                await asyncio.sleep(poll_interval)
            
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in poll_fill_status loop (attempt {consecutive_errors}/{max_consecutive_errors}): {e}")
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"🚫 Bailing out of poll_fill_status after {consecutive_errors} consecutive errors for order {order_id}")
                    result['error'] = str(e)
                    return result
                
                # Wait before retrying
                await asyncio.sleep(poll_interval)
        
        # Loop exited due to deadline - timeout
        logger.warning(f"Order {order_id} poll_fill_status deadline exceeded after {timeout_seconds}s")
        return result
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """
        Consultar posición actual con timeout.
        
        Returns:
            Dict with position info if exists
            None if no position
            {'_error': str} if API error (caller MUST check for '_error' key)
            {'_timeout': True, '_error': 'timeout'} if timeout
        """
        if not self.exchange:
            return None
        
        # 🔒 CIRCUIT BREAKER: Don't spam if in backoff
        if self.is_circuit_breaker_open():
            logger.debug(f"Circuit breaker open, skipping get_position (wait {self.get_circuit_breaker_wait_time():.0f}s)")
            return {'_error': 'circuit_breaker', '_circuit_breaker': True}
        
        try:
            async with self._exchange_lock:
                # Yield before CCXT call to prevent event loop starvation
                await asyncio.sleep(0)
                
                positions = await asyncio.wait_for(
                    self.exchange.fetch_positions([symbol]),
                    timeout=EXCHANGE_TIMEOUT_SHORT
                )
            
            if positions:
                # Mark exchange as available on success
                self._reset_circuit_breaker()
                return positions[0]
            
            return None
        
        except asyncio.TimeoutError:
            self._handle_rest_error(Exception("Timeout fetching position"), "get_position")
            # CRITICAL: Return sentinel value to distinguish timeout from "no position"
            return {'_timeout': True, '_error': 'timeout'}
        except Exception as e:
            self._handle_rest_error(e, "get_position")
            # CRITICAL: Return sentinel value to distinguish error from "no position"
            return {'_error': str(e)}
    
    async def has_open_position(self, symbol: str) -> bool:
        """
        Verificar si hay posición abierta en el exchange (TRUTH SOURCE)
        Returns: True si hay posición abierta, False si no
        """
        if not self.exchange:
            return False
        
        try:
            positions = await asyncio.wait_for(
                self.exchange.fetch_positions([symbol]),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            
            if not positions:
                return False
            
            # Verificar si la posición tiene contratos abiertos
            # CRITICAL: Solo confiar en contracts, notional es 0.0 en Kraken
            for pos in positions:
                # Manejar None values y tipos inválidos de forma segura
                contracts_raw = pos.get('contracts')
                
                # Convertir usando safe_float para evitar crashes por API responses inesperadas
                contracts = abs(safe_float(contracts_raw, 0.0))
                
                # THRESHOLD: Solo considerar posición real si contracts >= 0.0001
                # Esto evita detectar órdenes pendientes o residuos
                MIN_POSITION_SIZE = 0.0001
                
                # State tracking: solo loguear cuando cambia
                current_state = contracts
                last_state = self._last_position_state.get(symbol)
                
                if contracts >= MIN_POSITION_SIZE:
                    # Hay posición REAL - loguear solo si cambió o es nueva
                    if last_state != current_state:
                        logger.debug(f"Exchange position: {contracts:.6f} BTC")
                        self._last_position_state[symbol] = current_state
                    return True
                else:
                    # No hay posición - loguear solo si teníamos una antes
                    if last_state is not None and last_state >= MIN_POSITION_SIZE:
                        logger.info(f"✅ Exchange position closed (was: {last_state:.6f} BTC)")
                        self._last_position_state[symbol] = 0.0
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking open positions: {e}")
            # En caso de error, asumir que NO hay posición (menos disruptivo)
            return False
    
    async def set_position_stops(self, symbol: str, stop_loss: float | None = None, 
                                  take_profit: float | None = None) -> bool:
        """
        ⚠️ CRITICAL FIX para Kraken Futures SL/TP placement
        
        Kraken Futures tiene REQUISITOS ESPECIALES:
        1. SL: Debe usar type='stop-loss', precio en 'price' parameter
        2. TP: Debe usar type='limit' con cantidad exacta de posición
        3. Ambas órdenes DEBEN tener 'reduceOnly': True
        4. La cantidad (amount) DEBE coincidir EXACTAMENTE con la posición abierta
        
        🔥 CRITICAL CHECK EXISTING ORDERS FIRST
        Before placing new SL/TP, check if they already exist on exchange!
        This prevents duplicate orders that accumulate on every restart.
        
        Args:
            symbol: Par a operar (ej: 'BTC/USD:USD')
            stop_loss: Precio de stop loss
            take_profit: Precio de take profit
        
        Returns: True si SL se colocó exitosamente (TP es secondary)
        """
        if not self.exchange:
            return False
        
        try:
            # ================================================================
            # 🔥 STEP 0: CHECK EXISTING ORDERS - PREVENT DUPLICATES
            # Correct price extraction + cleanup duplicates
            # ================================================================
            existing_orders = await self.get_open_orders(symbol)
            existing_sl = None
            existing_tp = None
            duplicate_sl_ids = []  # Track duplicates to cancel
            duplicate_tp_ids = []
            
            if existing_orders:
                for order in existing_orders:
                    order_type = order.get('type', '').lower()
                    order_info = order.get('info', {})
                    order_type_kraken = order_info.get('orderType', '').lower() if order_info else ''
                    order_id = order.get('id')
                    
                    # Detect SL orders
                    if order_type in ('stop', 'stop-loss', 'stop_loss') or order_type_kraken in ('stp', 'stop'):
                        # Priority to triggerPrice from order_info (Kraken Futures)
                        # order.get('price') returns 0 for stop orders, so check info first!
                        sl_price = 0.0
                        if order_info and order_info.get('triggerPrice'):
                            sl_price = float(order_info.get('triggerPrice'))
                        elif order.get('stopPrice'):
                            sl_price = float(order.get('stopPrice'))
                        elif order.get('triggerPrice'):
                            sl_price = float(order.get('triggerPrice'))
                        # Only use price as last resort if > 0
                        elif order.get('price') and float(order.get('price', 0)) > 0:
                            sl_price = float(order.get('price'))
                        
                        if sl_price > 0:
                            if existing_sl is None:
                                existing_sl = order
                                logger.info(f"✅ EXISTING SL found at {sl_price:.2f} - will NOT place duplicate")
                            else:
                                # Track duplicate SL for cleanup
                                duplicate_sl_ids.append(order_id)
                                logger.warning(f"⚠️ DUPLICATE SL detected at {sl_price:.2f} (id={order_id}) - will cancel")
                        else:
                            logger.warning(f"⚠️ Found SL order but price is INVALID ({sl_price}) - will place new SL")
                    
                    # Detect TP orders (limit orders that are reduce-only)
                    elif order_type in ('limit', 'take-profit', 'take_profit'):
                        order_reduce_only = order_info.get('reduceOnly', False) if order_info else False
                        if order_reduce_only:
                            tp_price = float(order.get('price') or 0)
                            if tp_price > 0:
                                if existing_tp is None:
                                    existing_tp = order
                                    logger.info(f"✅ EXISTING TP found at {tp_price:.2f} - will NOT place duplicate")
                                else:
                                    # Track duplicate TP for cleanup
                                    duplicate_tp_ids.append(order_id)
                                    logger.warning(f"⚠️ DUPLICATE TP detected at {tp_price:.2f} (id={order_id}) - will cancel")
                            else:
                                logger.warning(f"⚠️ Found TP order but price is INVALID ({tp_price}) - will place new TP")
            
            # Cancel duplicate orders
            for dup_id in duplicate_sl_ids + duplicate_tp_ids:
                try:
                    await self.exchange.cancel_order(dup_id, symbol)
                    logger.info(f"🗑️ Cancelled duplicate order: {dup_id}")
                except Exception as cancel_err:
                    logger.warning(f"⚠️ Failed to cancel duplicate {dup_id}: {cancel_err}")
            
            # If both exist, we're done!
            if existing_sl and existing_tp:
                logger.info("✅ Both SL and TP already exist on exchange - no action needed")
                return True
            
            # If SL exists but TP doesn't (or vice versa), only place the missing one
            need_sl = stop_loss and not existing_sl
            need_tp = take_profit and not existing_tp
            
            if not need_sl and not need_tp:
                logger.info("✅ Required stops already exist on exchange")
                return existing_sl is not None  # Return True if SL exists
            
            # ================================================================
            # STEP 1: Verificar que existe posición
            # ================================================================
            position = await self.get_position(symbol)
            if not position:
                logger.error("Cannot set stops: no open position")
                return False
            
            contracts = position.get('contracts', 0)
            if abs(contracts) < MIN_POSITION_THRESHOLD:
                logger.error("Cannot set stops: position size is effectively 0")
                return False
            
            # Determinar dirección - CRITICAL: Use 'side' field, not contracts sign
            # Kraken Futures returns contracts always positive, side in 'side' field
            raw_side = position.get('side', '').lower()
            if raw_side in ('long', 'short'):
                position_side = raw_side
            else:
                # 🔒 Don't fallback to contracts sign for Kraken
                # Kraken always returns positive contracts, so sign is unreliable
                logger.error(f"❌ Cannot determine position side (raw_side='{raw_side}') - aborting SL/TP placement")
                logger.error(f"   Position data: contracts={contracts}, side={position.get('side')}")
                return False
            position_size = abs(contracts)
            
            # 🔒 Validate position size before setting SL/TP
            if position_size < KRAKEN_BTC_MIN_SIZE:
                logger.error(f"Cannot set stops: position size {position_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                return False
            
            logger.info(f"📍 Setting stops: SL={stop_loss}, TP={take_profit}")
            logger.info(f"   Position: {position_side.upper()} {position_size:.8f} {symbol}")
            
            # 🔥 VALIDATION: Ensure SL/TP prices are valid
            if stop_loss and stop_loss <= 0:
                logger.error(f"Invalid stop_loss price: {stop_loss}")
                stop_loss = None
            if take_profit and take_profit <= 0:
                logger.error(f"Invalid take_profit price: {take_profit}")
                take_profit = None
            
            if not need_sl and not need_tp:
                logger.info("✅ All required stops already exist on exchange")
                return existing_sl is not None
            
            sl_success = existing_sl is not None  # Already exists = success
            tp_success = existing_tp is not None  # Already exists = success
            
            # ===== STOP LOSS - Kraken Futures CRITICAL =====
            if need_sl and stop_loss:
                try:
                    sl_side = 'sell' if position_side == 'long' else 'buy'
                    
                    # ATTEMPT 1: Standard 'stop-loss' type (Kraken Futures standard)
                    # Added timeout to all exchange calls
                    logger.debug(f"[SL Attempt 1] Using 'stop-loss' type: {sl_side} {position_size:.8f} @ {stop_loss:.2f}")
                    try:
                        sl_order = await asyncio.wait_for(
                            self.exchange.create_order(
                                symbol=symbol,
                                type='stop-loss',
                                side=sl_side,
                                amount=position_size,
                                price=stop_loss,
                                params={
                                    'reduceOnly': True,
                                    'postOnly': False
                                }
                            ),
                            timeout=EXCHANGE_TIMEOUT_MEDIUM
                        )
                        logger.info(f"✅ SL (stop-loss) placed: {sl_order.get('id')}")
                        sl_success = True
                        
                    except asyncio.TimeoutError:
                        logger.debug(f"[SL Attempt 1 TIMEOUT]")
                        raise ExchangeTimeoutError("Timeout on stop-loss attempt 1")
                    except Exception as e1:
                        logger.debug(f"[SL Attempt 1 FAILED]: {str(e1)[:100]}")
                        
                        # ATTEMPT 2: 'stop' type with triggerPrice
                        logger.debug(f"[SL Attempt 2] Using 'stop' type with triggerPrice")
                        try:
                            sl_order = await asyncio.wait_for(
                                self.exchange.create_order(
                                    symbol=symbol,
                                    type='stop',
                                    side=sl_side,
                                    amount=position_size,
                                    price=None,
                                    params={
                                        'triggerPrice': stop_loss,
                                        'reduceOnly': True
                                    }
                                ),
                                timeout=EXCHANGE_TIMEOUT_MEDIUM
                            )
                            logger.info(f"✅ SL (stop) placed with triggerPrice: {sl_order.get('id')}")
                            sl_success = True
                            
                        except asyncio.TimeoutError:
                            logger.debug(f"[SL Attempt 2 TIMEOUT]")
                            raise ExchangeTimeoutError("Timeout on stop attempt 2")
                        except Exception as e2:
                            logger.debug(f"[SL Attempt 2 FAILED]: {str(e2)[:100]}")
                            
                            # ATTEMPT 3: Last resort - 'limit' order at slightly worse price
                            logger.debug(f"[SL Attempt 3] Using 'limit' as fallback")
                            try:
                                # For SL limit, use slightly worse price to ensure execution
                                fallback_sl_price = stop_loss * 0.998 if position_side == 'long' else stop_loss * 1.002
                                
                                sl_order = await asyncio.wait_for(
                                    self.exchange.create_order(
                                        symbol=symbol,
                                        type='limit',
                                        side=sl_side,
                                        amount=position_size,
                                        price=fallback_sl_price,
                                        params={
                                            'reduceOnly': True,
                                            'postOnly': False
                                        }
                                    ),
                                    timeout=EXCHANGE_TIMEOUT_MEDIUM
                                )
                                logger.info(f"✅ SL (limit fallback) placed at {fallback_sl_price:.2f}: {sl_order.get('id')}")
                                sl_success = True
                                
                            except asyncio.TimeoutError:
                                logger.error(f"❌ SL Attempt 3 TIMEOUT")
                            except Exception as e3:
                                logger.error(f"❌ ALL SL ATTEMPTS FAILED: {str(e3)[:150]}")
                                
                except Exception as outer_e:
                    logger.error(f"❌ SL outer exception: {str(outer_e)[:150]}")
            
            # ===== TAKE PROFIT - Kraken Futures CRITICAL =====
            # Only place TP if it doesn't already exist
            if need_tp and take_profit:
                try:
                    tp_side = 'sell' if position_side == 'long' else 'buy'

                    # PASO 2 — MAKER ONLY REAL (SIN EXCEPCIONES)
                    # SALIDA (TP): SIEMPRE LIMIT + post-only (resting), reduceOnly=True
                    logger.debug(f"[TP] Placing LIMIT post-only TP: {tp_side} {position_size:.8f} @ {take_profit:.2f}")
                    try:
                        tp_order = await asyncio.wait_for(
                            self.exchange.create_order(
                                symbol=symbol,
                                type='limit',
                                side=tp_side,
                                amount=position_size,
                                price=take_profit,
                                params={
                                    'reduceOnly': True,
                                    'postOnly': True,
                                }
                            ),
                            timeout=EXCHANGE_TIMEOUT_MEDIUM
                        )
                        logger.info(f"✅ TP (limit post-only) placed: {tp_order.get('id')}")
                        tp_success = True
                    except asyncio.TimeoutError:
                        logger.error("❌ TP placement TIMEOUT")
                    except Exception as e_tp:
                        logger.error(f"❌ TP placement failed: {str(e_tp)[:150]}")
                            
                except Exception as outer_e:
                    logger.error(f"❌ TP outer exception: {str(outer_e)[:150]}")
            
            # CRITICAL: Retornar True SOLO si SL se colocó (TP es secondary)
            if sl_success:
                logger.info(f"✅ SL CONFIGURED: {stop_loss:.2f} ✓ | TP: {take_profit:.2f} {' ✓' if tp_success else ' ⚠️'}")
                return True
            else:
                logger.critical("❌ CRITICAL: SL placement failed - POSITION AT RISK!")
                return False
            
        except Exception as e:
            logger.error(f"Exception in set_position_stops: {e}")
            logger.debug(f"Exception details: {e}", exc_info=True)
            return False
    
    async def update_stop_loss_on_exchange(
        self,
        symbol: str,
        new_sl_price: float,
        last_sent_sl: float,
        atr: float,
        spread: float,
        initial_risk: float,
        direction: str = 'LONG'
    ) -> Tuple[bool, float]:
        """
        Update trailing stop on exchange with smart thresholds.
        
        Implements:
        1. Idempotent updates (same SL = no action)
        2. Minimum movement threshold to prevent spam
        3. Watchdog sync on restart
        
        Args:
            symbol: Trading pair
            new_sl_price: New proposed SL price
            last_sent_sl: Last SL sent to exchange
            atr: Current ATR (for threshold calculation)
            spread: Current spread (for threshold calculation)
            initial_risk: Initial risk distance (entry - SL)
            direction: 'LONG' or 'SHORT'
        
        Returns:
            Tuple of (success: bool, new_last_sent_sl: float)
        """
        if not self.exchange:
            return False, last_sent_sl
        
        # Import config
        from src.config.trading_config import ExchangeSLConfig
        
        # =====================================================================
        # THRESHOLD CALCULATION
        # Use MAX of: 0.3R, 0.5*spread, 0.2*ATR
        # =====================================================================
        threshold_r = initial_risk * ExchangeSLConfig.MIN_MOVE_R
        threshold_spread = spread * ExchangeSLConfig.MIN_MOVE_SPREAD_MULT
        threshold_atr = atr * ExchangeSLConfig.MIN_MOVE_ATR_MULT
        
        min_threshold = max(threshold_r, threshold_spread, threshold_atr)
        
        # Calculate movement
        sl_movement = abs(new_sl_price - last_sent_sl)
        
        # =====================================================================
        # IDEMPOTENT CHECK: Skip if movement below threshold
        # =====================================================================
        if sl_movement < min_threshold:
            logger.debug(
                f"SL update skipped: movement {sl_movement:.2f} < threshold {min_threshold:.2f} "
                f"(R:{threshold_r:.2f}, spread:{threshold_spread:.2f}, ATR:{threshold_atr:.2f})"
            )
            return True, last_sent_sl  # Success but no update needed
        
        # =====================================================================
        # VALIDATE SL DIRECTION
        # For LONG: new SL should be >= last SL (trailing up)
        # For SHORT: new SL should be <= last SL (trailing down)
        # =====================================================================
        if direction.upper() == 'LONG' and new_sl_price < last_sent_sl:
            logger.warning(f"⚠️ LONG SL going DOWN: {last_sent_sl:.2f} → {new_sl_price:.2f} - skipped")
            return True, last_sent_sl
        if direction.upper() == 'SHORT' and new_sl_price > last_sent_sl:
            logger.warning(f"⚠️ SHORT SL going UP: {last_sent_sl:.2f} → {new_sl_price:.2f} - skipped")
            return True, last_sent_sl
        
        # =====================================================================
        # PLACE NEW SL FIRST, THEN CANCEL OLD ONE (Race-condition safe)
        # Reversed order - place new SL before canceling old
        # This ensures position is ALWAYS protected, even if cancel/place fails
        # =====================================================================
        try:
            # 1. Get position info
            position = await self.get_position(symbol)
            if not position or abs(position.get('contracts', 0)) < MIN_POSITION_THRESHOLD:
                logger.warning("No position found for SL update")
                return False, last_sent_sl
            
            contracts = position.get('contracts', 0)
            raw_side = position.get('side', '').lower()
            if raw_side in ('long', 'short'):
                position_side = raw_side
            else:
                position_side = 'long' if contracts > 0 else 'short'
            position_size = abs(contracts)
            
            # 2. Place NEW SL first (position stays protected)
            # Added 3-attempt retry pattern matching place_initial_sl_tp
            sl_side = 'sell' if position_side == 'long' else 'buy'
            
            sl_order = None
            new_sl_order_id = None
            
            # ATTEMPT 1: Standard 'stop-loss' type (Kraken Futures standard)
            logger.debug(f"[SL Update Attempt 1] Using 'stop-loss' type: {sl_side} {position_size:.8f} @ {new_sl_price:.2f}")
            try:
                sl_order = await asyncio.wait_for(
                    self.exchange.create_order(
                        symbol=symbol,
                        type='stop-loss',
                        side=sl_side,
                        amount=position_size,
                        price=new_sl_price,
                        params={
                            'reduceOnly': True,
                            'postOnly': False
                        }
                    ),
                    timeout=EXCHANGE_TIMEOUT_MEDIUM
                )
                new_sl_order_id = sl_order.get('id')
                logger.info(f"✅ New SL (stop-loss) placed: {new_sl_price:.2f} (order: {new_sl_order_id})")
                
            except asyncio.TimeoutError:
                logger.debug("[SL Update Attempt 1 TIMEOUT]")
                raise
            except Exception as e1:
                logger.debug(f"[SL Update Attempt 1 FAILED]: {str(e1)[:100]}")
                
                # ATTEMPT 2: 'stop' type with triggerPrice
                logger.debug(f"[SL Update Attempt 2] Using 'stop' type with triggerPrice")
                try:
                    sl_order = await asyncio.wait_for(
                        self.exchange.create_order(
                            symbol=symbol,
                            type='stop',
                            side=sl_side,
                            amount=position_size,
                            price=None,
                            params={
                                'triggerPrice': new_sl_price,
                                'reduceOnly': True
                            }
                        ),
                        timeout=EXCHANGE_TIMEOUT_MEDIUM
                    )
                    new_sl_order_id = sl_order.get('id')
                    logger.info(f"✅ New SL (stop) placed with triggerPrice: {new_sl_price:.2f} (order: {new_sl_order_id})")
                    
                except asyncio.TimeoutError:
                    logger.debug("[SL Update Attempt 2 TIMEOUT]")
                    raise
                except Exception as e2:
                    logger.debug(f"[SL Update Attempt 2 FAILED]: {str(e2)[:100]}")
                    
                    # ATTEMPT 3: Last resort - 'limit' order at slightly worse price
                    logger.debug(f"[SL Update Attempt 3] Using 'limit' as fallback")
                    try:
                        # For SL limit, use slightly worse price to ensure execution
                        fallback_sl_price = new_sl_price * 0.998 if position_side == 'long' else new_sl_price * 1.002
                        
                        sl_order = await asyncio.wait_for(
                            self.exchange.create_order(
                                symbol=symbol,
                                type='limit',
                                side=sl_side,
                                amount=position_size,
                                price=fallback_sl_price,
                                params={
                                    'reduceOnly': True,
                                    'postOnly': False
                                }
                            ),
                            timeout=EXCHANGE_TIMEOUT_MEDIUM
                        )
                        new_sl_order_id = sl_order.get('id')
                        logger.info(f"✅ New SL (limit fallback) placed at {fallback_sl_price:.2f}: {new_sl_order_id}")
                        
                    except asyncio.TimeoutError:
                        logger.error("❌ SL Update Attempt 3 TIMEOUT")
                        raise
                    except Exception as e3:
                        logger.error(f"❌ ALL SL UPDATE ATTEMPTS FAILED: {str(e3)[:150]}")
                        raise
            
            if not new_sl_order_id:
                raise RuntimeError("SL order placement failed - no order ID received")
            
            # 3. NOW cancel old SL orders (except the one we just placed)
            open_orders = await self.get_open_orders(symbol)
            for order in open_orders:
                if order.get('id') == new_sl_order_id:
                    continue  # Don't cancel the one we just placed!
                order_type = order.get('type', '').lower()
                is_reduce = order.get('info', {}).get('reduceOnly', False)
                if is_reduce and order_type in ('stop', 'stop-loss', 'stop_loss'):
                    try:
                        await self.cancel_order(order['id'], symbol)
                        logger.info(f"🗑️ Cancelled old SL order: {order['id']}")
                    except Exception as cancel_e:
                        logger.warning(f"Failed to cancel old SL: {cancel_e}")
            
            logger.info(
                f"✅ SL updated on exchange: {last_sent_sl:.2f} → {new_sl_price:.2f} "
                f"(moved {sl_movement:.2f})"
            )
            
            return True, new_sl_price
            
        except Exception as e:
            logger.error(f"❌ Failed to update SL on exchange: {e}")
            return False, last_sent_sl
    
    async def verify_stops_on_exchange(self, symbol: str) -> dict:
        """
        Verificar que los SL/TP realmente existen en el exchange
        Returns: {'has_sl': bool, 'has_tp': bool, 'orders': [list]}
        """
        if not self.exchange:
            return {'has_sl': False, 'has_tp': False, 'orders': []}
        
        try:
            # Add timeout to prevent blocking
            open_orders = await asyncio.wait_for(
                self.exchange.fetch_open_orders(symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            
            has_sl = False
            has_tp = False
            reduce_only_orders = []
            
            for order in open_orders:
                # Buscar órdenes con reduceOnly=True
                reduce_only = order.get('reduceOnly') or order.get('info', {}).get('reduceOnly')
                if reduce_only:
                    order_type = str(order.get('type', '')).lower()
                    reduce_only_orders.append({
                        'id': order.get('id'),
                        'type': order_type,
                        'side': order.get('side'),
                        'price': order.get('price') or order.get('triggerPrice') or order.get('stopPrice'),
                        'status': order.get('status')
                    })
                    
                    # 🔥 FIX: Detect SL by multiple type names (Kraken uses 'stop-loss', others use 'stop')
                    if order_type in ('stop', 'stop-loss', 'stop_loss', 'stop_market'):
                        has_sl = True
                        logger.debug(f"Found SL order: type={order_type}, id={order.get('id')}")
                    elif order_type in ('take-profit', 'take_profit', 'limit'):
                        has_tp = True
                        logger.debug(f"Found TP order: type={order_type}, id={order.get('id')}")
            
            result = {
                'has_sl': has_sl,
                'has_tp': has_tp,
                'orders': reduce_only_orders
            }
            
            logger.info(f"✅ Verified stops: SL={'✓' if has_sl else '✗'}, TP={'✓' if has_tp else '✗'} | {len(reduce_only_orders)} reduce-only orders")
            
            return result
            
        except Exception as e:
            logger.error(f"Error verifying stops on exchange: {e}")
            return {'has_sl': False, 'has_tp': False, 'orders': []}
    
    async def close_position_market(self, symbol: str | None = None, side: str | None = None, amount: float | None = None, reason: str = "EMERGENCY") -> Optional[Dict]:
        """
        Cerrar posición a mercado con timeout y retry
        
        Args:
            symbol: Trading pair (uses self.current_symbol if not provided)
            side: 'buy' (close short) o 'sell' (close long)
            amount: Position size to close (fetches from exchange if not provided)
            reason: Context for logging - 'PARTIAL', 'FULL_CLOSE', 'EMERGENCY', 'SL_HIT', etc.
        
        Returns: Order dict or None if failed after retries
        """
        if not self.exchange:
            return None
        
        # Use stored symbol if not provided
        if symbol is None:
            current_sym = getattr(self, 'current_symbol', None)
            if current_sym:
                symbol = current_sym
            else:
                logger.error("Emergency close called without symbol and no current_symbol set")
                # Use default as last resort but log it clearly
                symbol = 'BTC/USD:USD'
                logger.warning(f"Using fallback symbol: {symbol}")
        
        # At this point symbol must be a string
        assert symbol is not None, "Symbol must be set by now"
        
        MAX_RETRIES = 3
        RETRY_DELAY = 2.0
        
        for attempt in range(MAX_RETRIES):
            try:
                position = await self.get_position(symbol)
                
                if not position:
                    logger.warning("No position to close")
                    return None
                
                # Use provided amount or get from position
                close_amount = amount if amount else abs(position.get('contracts', 0))
                
                if close_amount == 0:
                    logger.warning("Position amount is 0, nothing to close")
                    return None
                
                # 🔒 Validate minimum order size
                if close_amount < KRAKEN_BTC_MIN_SIZE:
                    logger.warning(f"⚠️ Close amount {close_amount:.6f} < min {KRAKEN_BTC_MIN_SIZE}, cannot execute")
                    return None
                
                # Round to exchange precision
                close_amount = round_size_to_exchange(close_amount)
                if close_amount < KRAKEN_BTC_MIN_SIZE:
                    logger.warning(f"⚠️ Rounded close amount {close_amount:.6f} < min, cannot execute")
                    return None
                
                # Determine side from position if not provided
                if side is None:
                    # Use CCXT unified 'side' field (more reliable than contracts sign)
                    position_side = position.get('side', '').lower()
                    if position_side == 'long':
                        side = 'sell'  # Close LONG by selling
                    elif position_side == 'short':
                        side = 'buy'   # Close SHORT by buying
                    else:
                        # Fallback to contracts sign
                        position_contracts = position.get('contracts', 0)
                        side = 'sell' if position_contracts > 0 else 'buy'
                        logger.warning(f"Position 'side' not available, using contracts sign: {side}")
                
                logger.critical(f"📤 {reason} MARKET CLOSE (attempt {attempt + 1}/{MAX_RETRIES}): {close_amount} {symbol} side={side}")
                
                # =====================================================================
                # 🔒 ALWAYS USE reduceOnly=True FOR CLOSE ORDERS
                # This prevents position flipping if local state is wrong.
                # If we think we're closing but we're flat, order will be rejected
                # instead of opening a new position in the wrong direction.
                # =====================================================================
                order = await asyncio.wait_for(
                    self.exchange.create_market_order(
                        symbol,
                        side.lower(),
                        close_amount,
                        params={'reduceOnly': True}
                    ),
                    timeout=EXCHANGE_TIMEOUT_MEDIUM
                )
                
                logger.critical(f"Position closed (reduceOnly=True): {order}")
                
                # =====================================================================
                # IMMEDIATELY update position_store after close
                # This prevents phantom position discrepancies in the next sync.
                # Previously, we waited for sync_from_exchange which caused warnings.
                # =====================================================================
                try:
                    if order and order.get('status') == 'closed' and order.get('filled', 0) > 0:
                        position_store = get_position_store()
                        fill_price = order.get('average') or order.get('price', 0)
                        fill_size = order.get('filled', close_amount)
                        order_id = order.get('id', '')
                        
                        # Get fill ID from trades if available
                        trades = order.get('trades', [])
                        fill_id = trades[0].get('id', '') if trades else order_id
                        
                        await position_store.record_fill(
                            order_id=order_id,
                            symbol=symbol,
                            side=side.lower(),
                            size=fill_size,
                            price=fill_price,
                            fee=0.0,  # Fee is deducted from balance, not tracked here
                            fill_type='TAKER',
                            fill_id=fill_id,
                        )
                        logger.info(f"✅ Position store updated: {side} {fill_size:.6f} @ {fill_price:.2f}")
                except Exception as ps_error:
                    logger.error(f"⚠️ Failed to update position_store after close: {ps_error}")
                    # Don't fail the close operation, sync will catch it later
                
                return order
            
            except asyncio.TimeoutError:
                logger.critical(f"TIMEOUT closing position (attempt {attempt + 1}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"Retrying in {RETRY_DELAY}s...")
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.critical("❌ FAILED TO CLOSE POSITION AFTER ALL RETRIES - MANUAL INTERVENTION REQUIRED!")
                    return None
            except Exception as e:
                logger.error(f"Error closing position (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    return None
        
        return None
    
    async def get_account_balance(self) -> Optional[Dict]:
        """Obtener balance de cuenta con timeout"""
        if not self.exchange:
            return None
        
        try:
            balance = await asyncio.wait_for(
                self.exchange.fetch_balance(),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            return balance
        
        except asyncio.TimeoutError:
            logger.error("Timeout fetching balance")
            return None
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return None
    
    async def get_ticker(self, symbol: str) -> Optional[Dict]:
        """
        Obtener ticker (precio actual) del símbolo.
        
        Returns:
            Dict con 'last', 'bid', 'ask' o None si falla
        """
        if not self.exchange:
            return None
        
        try:
            async with self._exchange_lock:
                ticker = await asyncio.wait_for(
                    self.exchange.fetch_ticker(symbol),
                    timeout=EXCHANGE_TIMEOUT_SHORT
                )
            return ticker
        
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching ticker for {symbol}")
            return None
        except Exception as e:
            logger.error(f"Error fetching ticker: {e}")
            return None
    
    async def get_available_capital(self) -> float:
        """
        Obtener capital DISPONIBLE (free, no locked) en la moneda base.
        Busca USDT > USD > EUR en orden de preferencia.
        IMPORTANTE: Usa 'free' NO 'total' porque Kraken necesita fondos sin lock
        
        Returns:
            float: capital disponible para trading (free balance)
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return 0.0
        
        try:
            # Use lock to prevent concurrent access + timeout
            async with self._exchange_lock:
                try:
                    balance = await asyncio.wait_for(
                        self.exchange.fetch_balance(),
                        timeout=EXCHANGE_TIMEOUT_SHORT
                    )
                except asyncio.TimeoutError:
                    logger.error("⏰ TIMEOUT fetching balance in get_available_capital (10s)")
                    return 0.0
            
            if not balance or not isinstance(balance, dict):
                logger.error("Invalid balance response from exchange")
                return 0.0
            
            # Use 'free' balance (not locked in orders)
            free = balance.get('free', {})
            
            # Search in order of preference: USDT -> USD -> EUR
            for currency in ['USDT', 'USD', 'EUR']:
                if currency in free and free[currency] > 0:
                    capital = float(free[currency])
                    logger.info(f"💰 Capital disponible (FREE): {capital:.2f} {currency}")
                    return capital
            
            # Fallback: return first non-zero asset from free
            for asset, amount in free.items():
                if amount and amount > 0:
                    capital = safe_float(amount, 0.0)
                    logger.warning(f"No USDT/USD/EUR in free, using {asset}: {capital:.6f}")
                    return capital
            
            # Fallback: Check 'info' for Kraken Futures specific structure
            if 'info' in balance:
                info = balance['info']
                # Kraken Futures often has 'accounts' or 'marginAccount'
                if 'accounts' in info:
                    for acc in info.get('accounts', []):
                        if acc.get('currency', '').upper() in ['USD', 'USDT'] or acc.get('type') == 'cash':
                            avail = safe_float(acc.get('available', 0), 0.0)
                            if avail > 0:
                                logger.info(f"💰 Kraken Futures Balance (info): ${avail:.2f}")
                                return avail
            
            logger.warning("No balance available > 0")
            return 0.0
            
        except Exception as e:
            logger.error(f"Error fetching available capital: {e}")
            return 0.0
    
    async def set_leverage(self, leverage: int, symbol: str) -> bool:
        """Configure leverage for symbol with timeout"""
        if not self.exchange:
            return False
        
        try:
            # Check if exchange supports leverage
            if hasattr(self.exchange, 'set_leverage'):
                await asyncio.wait_for(
                    self.exchange.set_leverage(leverage, symbol),
                    timeout=EXCHANGE_TIMEOUT_SHORT
                )
                logger.info(f"Leverage set to {leverage}x for {symbol}")
                return True
            else:
                logger.warning(f"Exchange {self.exchange_name} does not support set_leverage")
                return False
        
        except asyncio.TimeoutError:
            logger.error(f"Timeout setting leverage for {symbol}")
            return False
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False
    
    async def close(self):
        """Close exchange connection with timeout"""
        if self.exchange:
            try:
                await asyncio.wait_for(
                    self.exchange.close(),
                    timeout=EXCHANGE_TIMEOUT_SHORT
                )
                logger.info("Exchange connection closed")
            except asyncio.TimeoutError:
                logger.warning("Timeout closing exchange connection")
            except Exception as e:
                logger.error(f"Error closing exchange: {e}")
    
    async def auto_reconcile_orders(self, symbol: str, interval_seconds: int = 60) -> dict:
        """
        Automatic order reconciliation.
        
        Cleans up orphaned orders and syncs internal state.
        Should be called periodically (every 60s recommended).
        
        Args:
            symbol: Trading pair
            interval_seconds: Time since last reconcile (for logging)
            
        Returns:
            Dict with reconciliation results
        """
        if not self.exchange:
            return {'success': False, 'error': 'Exchange not initialized'}
        
        try:
            result = {
                'success': True,
                'open_orders': 0,
                'cancelled': 0,
                'position_synced': False
            }
            
            # 1. Fetch real open orders
            # Add timeout to prevent blocking
            real_orders = await asyncio.wait_for(
                self.exchange.fetch_open_orders(symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            result['open_orders'] = len(real_orders)
            
            # 2. Sync internal tracking
            real_order_ids = set(o['id'] for o in real_orders)
            
            # Remove orders that no longer exist
            stale_ids = [oid for oid in self.open_orders if oid not in real_order_ids]
            for oid in stale_ids:
                del self.open_orders[oid]
                result['cancelled'] += 1
                logger.debug(f"Removed stale order from tracking: {oid}")
            
            # Add orders we didn't know about
            for order in real_orders:
                if order['id'] not in self.open_orders:
                    self.open_orders[order['id']] = order
            
            # 3. Verify position state
            position = await self.get_position(symbol)
            result['has_position'] = position and abs(position.get('contracts', 0)) > 0
            result['position_synced'] = True
            
            # Mark exchange as available on success
            self._reset_circuit_breaker()
            
            logger.debug(f"Reconciled: {result['open_orders']} orders, "
                        f"{result['cancelled']} stale removed")
            
            return result
            
        except Exception as e:
            self._handle_rest_error(e, "auto_reconcile")
            return {'success': False, 'error': str(e), 'exchange_available': self.is_exchange_available}
    
    def _handle_rest_error(self, error: Exception, operation: str):
        """Handle REST API errors with availability tracking, log throttling and circuit breaker"""
        error_str = str(error)
        self._consecutive_rest_failures += 1
        current_time = asyncio.get_event_loop().time()
        self._last_rest_failure_time = current_time
        self._rest_failure_log_count += 1
        
        # Detect exchange outage (503, unavailable, timeout, etc.)
        # CRITICAL: Timeout is also an outage indicator!
        # Also detect DNS failures
        is_outage = any(x in error_str.lower() for x in [
            '503', 'service unavailable', 'unavailable', 'try again later',
            'timeout', 'timed out', 'connection', 'dns', 'name resolution',
            'errno -3', 'temporary failure'
        ])
        
        if is_outage:
            # 🔒 CIRCUIT BREAKER: Set exponential backoff
            self._backoff_seconds = min(self._backoff_seconds * 2, self._max_backoff_seconds)
            self._circuit_breaker_until = current_time + self._backoff_seconds
            
            if self.is_exchange_available:
                logger.warning(f"⚠️ KRAKEN REST API OUTAGE - {operation} failed: {error_str[:100]}")
                logger.warning(f"🔒 Circuit breaker active: backing off {self._backoff_seconds:.0f}s")
                self.is_exchange_available = False
            elif self._rest_failure_log_count <= 3 or self._rest_failure_log_count % 20 == 0:
                # Throttle repeated outage logs
                logger.warning(f"⚠️ Exchange REST still down ({self._consecutive_rest_failures} failures, backoff={self._backoff_seconds:.0f}s)")
        else:
            # Regular error - throttle after first few
            if self._rest_failure_log_count <= 5 or self._rest_failure_log_count % 30 == 0:
                logger.error(f"Error in {operation}: {error_str}")
    
    def is_circuit_breaker_open(self) -> bool:
        """Check if circuit breaker is blocking calls (in backoff period)"""
        if self._circuit_breaker_until == 0:
            return False
        return asyncio.get_event_loop().time() < self._circuit_breaker_until
    
    def get_circuit_breaker_wait_time(self) -> float:
        """Return seconds remaining in circuit breaker backoff."""
        if self._circuit_breaker_until == 0:
            return 0.0
        remaining = self._circuit_breaker_until - asyncio.get_event_loop().time()
        return max(0.0, remaining)
    
    def _reset_circuit_breaker(self):
        """Reset circuit breaker on successful call"""
        self._circuit_breaker_until = 0
        self._backoff_seconds = 2.0  # Reset to initial
        self._consecutive_rest_failures = 0
        self._rest_failure_log_count = 0
        if not self.is_exchange_available:
            logger.info("✅ Exchange REST API recovered")
        self.is_exchange_available = True
        self._last_successful_rest_call = asyncio.get_event_loop().time()
    
    def get_availability_status(self) -> dict:
        """Get REST API availability status"""
        return {
            'is_available': self.is_exchange_available,
            'consecutive_failures': self._consecutive_rest_failures,
            'seconds_since_failure': asyncio.get_event_loop().time() - self._last_rest_failure_time if self._last_rest_failure_time else 0,
            'seconds_since_success': asyncio.get_event_loop().time() - self._last_successful_rest_call if self._last_successful_rest_call else 0,
            'circuit_breaker_open': self.is_circuit_breaker_open(),
            'circuit_breaker_wait': self.get_circuit_breaker_wait_time()
        }
    
    async def place_market_order(self, symbol: str, side: str, amount: float,
                                  stop_loss: float | None = None, 
                                  take_profit: float | None = None,
                                  client_order_id: str | None = None) -> Optional[Dict]:
        """
        Place market order with optional SL/TP.
        
        For Kraken: SL/TP are placed as separate orders after market fill.
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell'
            amount: Position size
            stop_loss: SL price (placed after fill)
            take_profit: TP price (placed after fill)
            client_order_id: Idempotent client-generated order ID for retry safety
            
        Returns:
            Order result dict
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return None
        
        # 🔥 PRE-ROUND SIZE to exchange precision to prevent desync
        original_amount = amount
        amount = round_size_to_exchange(amount)
        if amount == 0 or amount < KRAKEN_BTC_MIN_SIZE:
            logger.error(f"⛔ Cannot place market order: size {original_amount:.8f} → {amount:.8f} (min: {KRAKEN_BTC_MIN_SIZE})")
            return None
        if amount != original_amount:
            logger.info(f"📊 Size pre-rounded: {original_amount:.8f} → {amount:.8f}")
        
        # Generate idempotent client order ID if not provided
        if client_order_id is None:
            client_order_id = generate_client_order_id(
                symbol=symbol,
                side=side,
                timestamp=datetime.now(UTC),
                strategy_id="market"
            )
        
        try:
            logger.info(f"🚀 MARKET ORDER: {side.upper()} {amount} {symbol} (clientId={client_order_id})")
            
            order = await asyncio.wait_for(
                self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=side.lower(),
                    amount=amount,
                    params={'clientOrderId': client_order_id}
                ),
                timeout=EXCHANGE_TIMEOUT_MEDIUM
            )
            
            order['created_at'] = datetime.now(UTC).isoformat()
            order['clientOrderId'] = client_order_id
            self.open_orders[order['id']] = order
            
            logger.info(f"✅ Market order executed: {order['id']}")
            
            # =====================================================================
            # Update position_store after market order fill
            # This keeps position_store in sync with exchange reality.
            # Critical for ADD_POSITION scale-ins that were not being tracked.

            # Use position.entryPrice as source of truth
            # order.average can differ from position.entryPrice due to how Kraken
            # reports fills vs position entries. Position is the truth.
            # =====================================================================
            try:
                if order.get('status') == 'closed' and order.get('filled', 0) > 0:
                    position_store = get_position_store()
                    fill_size = order.get('filled', amount)
                    order_id = order.get('id', '')
                    
                    # 🔥 Get actual entry price from exchange position (SOURCE OF TRUTH)
                    # order.average may not match position.entryPrice on Kraken
                    fill_price = order.get('average') or order.get('price', 0)
                    try:
                        # Short delay to let exchange update position
                        await asyncio.sleep(0.3)
                        positions = await self.exchange.fetch_positions([symbol])
                        if positions:
                            for pos in positions:
                                if pos.get('symbol') == symbol and abs(float(pos.get('contracts', 0))) > 0:
                                    real_entry = float(pos.get('entryPrice', 0))
                                    if real_entry > 0:
                                        if abs(real_entry - fill_price) > 1:  # Log if differs by more than $1
                                            logger.info(f"📊 Using position.entryPrice {real_entry:.2f} (order.average was {fill_price:.2f})")
                                        fill_price = real_entry
                                    break
                    except Exception as pos_err:
                        logger.debug(f"Could not fetch position for entry price: {pos_err}")
                    
                    # Get fill ID from trades if available
                    trades = order.get('trades', [])
                    fill_id = trades[0].get('id', '') if trades else order_id
                    
                    await position_store.record_fill(
                        order_id=order_id,
                        symbol=symbol,
                        side=side.lower(),
                        size=fill_size,
                        price=fill_price,
                        fee=0.0,  # Fee is deducted from balance
                        fill_type='TAKER',
                        fill_id=fill_id,
                    )
                    logger.info(f"✅ Position store updated: {side} {fill_size:.6f} @ {fill_price:.2f}")
            except Exception as ps_error:
                logger.warning(f"⚠️ Could not update position_store after market order: {ps_error}")
            
            # Store SL/TP for placement after confirmation
            if stop_loss or take_profit:
                order['_pending_sl'] = stop_loss
                order['_pending_tp'] = take_profit
                logger.info(f"   📌 Pending: SL @ {stop_loss:.2f} | TP @ {take_profit:.2f}")
            
            return order
            
        except Exception as e:
            logger.error(f"Error placing market order: {e}")
            return None

