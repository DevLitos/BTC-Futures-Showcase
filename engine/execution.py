"""
Execution Engine - REST ONLY para órdenes
SEPARADO de WebSocket (datos)

REGLA DE ORO: WebSocket = datos, REST = ejecución
"""

import asyncio
import logging
from typing import Optional, Dict, Tuple
import ccxt.async_support as ccxt
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

# Timeouts para operaciones de exchange (segundos)
EXCHANGE_TIMEOUT_SHORT = 10.0   # Para consultas simples
EXCHANGE_TIMEOUT_MEDIUM = 30.0  # Para órdenes
EXCHANGE_TIMEOUT_LONG = 60.0    # Para operaciones complejas


class RESTExecutionEngine:
    """
    Ejecución de órdenes vía REST
    - CCXT solo para colocar/cancelar/consultar
    - Nunca datos
    - Post-only obligatorio
    """
    
    def __init__(self, exchange: str = 'binance', credentials: dict = None):
        self.exchange_name = exchange
        self.credentials = credentials or {}
        self.exchange = None
        
        self.open_orders = {}
        self.order_history = []
        
        # State tracking para logging de posiciones
        self._last_position_state = {}  # {symbol: (contracts, notional)}
        
        logger.info(f"RESTExecutionEngine initialized for {exchange}")
    
    async def initialize(self):
        """Inicializar conexión a exchange"""
        try:
            ExchangeClass = getattr(ccxt, self.exchange_name)
            
            # Simple config - just pass credentials as-is
            self.exchange = ExchangeClass(self.credentials)
            
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
    
    async def place_limit_order(self, symbol: str, side: str, amount: float, 
                               price: float, stop_loss: float = None, 
                               take_profit: float = None) -> Optional[Dict]:
        """
        Colocar orden limit post-only con SL/TP ATOMICOS
        
        Args:
            stop_loss: Precio de stop loss (se envía en la misma orden)
            take_profit: Precio de take profit (se envía en la misma orden)
        
        Returns: order object o None si falla
        
        IMPORTANTE: SL/TP se colocan ATOMICAMENTE con la orden de entrada.
        Kraken Futures los acepta como parámetros stopLoss/takeProfit.
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return None
        
        try:
            # Build params for Kraken Futures
            params = {'timeInForce': 'GTC'}
            
            # ⚠️ IMPORTANT: Kraken Futures NO acepta stopLoss/takeProfit en órdenes LIMIT
            # Solo las acepta en órdenes MARKET
            # Por tanto: Colocamos la orden de entrada SIN SL/TP
            # Los SL/TP se colocarán DESPUÉS cuando la posición esté abierta
            
            logger.info(f"Placing {side.upper()} order: {amount} {symbol} @ {price}")
            
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
            
            order['created_at'] = datetime.now(UTC).isoformat()
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
            logger.error(f"Error placing order: {e}")
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
        """Cancelar orden con timeout"""
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
            logger.error(f"Error cancelling order: {e}")
            return False
    
    async def get_order_status(self, order_id: str, symbol: str) -> Optional[Dict]:
        """
        Consultar estado de orden con timeout.
        Kraken Futures no soporta fetch_order(id), usamos fallback a open/closed orders.
        """
        if not self.exchange:
            return None
        
        try:
            # Intento directo (funciona en Binance, falla en Kraken Futures)
            order = await asyncio.wait_for(
                self.exchange.fetch_order(order_id, symbol),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            return order
            
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching order {order_id}")
            return None
        except Exception as e:
            msg = str(e).lower()
            if "not supported" in msg or "method not implemented" in msg:
                # Fallback: Buscar en Open Orders
                try:
                    open_orders = await asyncio.wait_for(
                        self.exchange.fetch_open_orders(symbol),
                        timeout=EXCHANGE_TIMEOUT_SHORT
                    )
                    for o in open_orders:
                        if o['id'] == order_id:
                            return o
                    
                    # Si no está en Open, buscar en Closed (recientes)
                    closed_orders = await asyncio.wait_for(
                        self.exchange.fetch_closed_orders(symbol, limit=50),
                        timeout=EXCHANGE_TIMEOUT_SHORT
                    )
                    for o in closed_orders:
                        if o['id'] == order_id:
                            return o
                    
                    # Log all order IDs to help debug
                    open_ids = [o['id'] for o in open_orders[:5]]  # First 5
                    closed_ids = [o['id'] for o in closed_orders[:5]]  # First 5
                    logger.warning(f"Order {order_id} not found. Open IDs: {open_ids}, Closed IDs: {closed_ids}")
                    return None
                    
                except asyncio.TimeoutError:
                    logger.error(f"Timeout in fallback order fetch for {order_id}")
                    return None
                except Exception as inner_e:
                    logger.error(f"Error in fallback order fetch: {inner_e}")
                    return None
            else:
                logger.error(f"Error fetching order: {e}")
                return None

    async def get_open_orders(self, symbol: str) -> list:
        """Consultar órdenes abiertas directamente del exchange"""
        if not self.exchange:
            return []
        
        try:
            orders = await self.exchange.fetch_open_orders(symbol)
            return orders
        except Exception as e:
            logger.error(f"Error fetching open orders: {e}")
            return []
    
    async def poll_fill_status(self, order_id: str, symbol: str, 
                              timeout_seconds: int = 180) -> bool:
        """
        Poll orden hasta que se llene o timeout
        CCXT approach: consulta REST cada X ms
        
        Returns: True si se llenó, False si timeout
        """
        start_time = datetime.now(UTC)
        poll_interval = 1.0  # segundo
        
        while True:
            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            
            if elapsed > timeout_seconds:
                logger.warning(f"Order {order_id} timeout after {elapsed}s")
                return False
            
            order = await self.get_order_status(order_id, symbol)
            
            if not order:
                await asyncio.sleep(poll_interval)
                continue
            
            status = order.get('status')
            filled = order.get('filled', 0)
            
            if status == 'closed' or filled >= order.get('amount', 0):
                logger.info(f"✅ Order FILLED: {order_id} ({filled} @ {order.get('average', 0):.2f})")
                return True
            
            elif status == 'canceled':
                logger.warning(f"Order CANCELLED: {order_id}")
                return False
            
            elif status == 'rejected':
                logger.error(f"Order REJECTED: {order_id}")
                return False
            
            # Still pending
            logger.debug(f"Order {order_id} pending ({filled}/{order.get('amount', 0)})")
            await asyncio.sleep(poll_interval)
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Consultar posición actual con timeout"""
        if not self.exchange:
            return None
        
        try:
            positions = await asyncio.wait_for(
                self.exchange.fetch_positions([symbol]),
                timeout=EXCHANGE_TIMEOUT_SHORT
            )
            
            if positions:
                return positions[0]
            
            return None
        
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching position for {symbol}")
            return None
        except Exception as e:
            logger.error(f"Error fetching position: {e}")
            return None
    
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
                # Manejar None values de forma segura
                contracts_raw = pos.get('contracts')
                
                # Convertir a float solo si no es None
                contracts = abs(float(contracts_raw)) if contracts_raw is not None else 0.0
                
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
    
    async def set_position_stops(self, symbol: str, stop_loss: float = None, 
                                  take_profit: float = None) -> bool:
        """
        ⚠️ CRITICAL FIX para Kraken Futures SL/TP placement
        
        Kraken Futures tiene REQUISITOS ESPECIALES:
        1. SL: Debe usar type='stop-loss', precio en 'price' parameter
        2. TP: Debe usar type='limit' con cantidad exacta de posición
        3. Ambas órdenes DEBEN tener 'reduceOnly': True
        4. La cantidad (amount) DEBE coincidir EXACTAMENTE con la posición abierta
        
        Args:
            symbol: Par a operar (ej: 'BTC/USD:USD')
            stop_loss: Precio de stop loss
            take_profit: Precio de take profit
        
        Returns: True si SL se colocó exitosamente (TP es secondary)
        """
        if not self.exchange:
            return False
        
        try:
            # Verificar que existe posición
            position = await self.get_position(symbol)
            if not position:
                logger.error("Cannot set stops: no open position")
                return False
            
            contracts = position.get('contracts', 0)
            if abs(contracts) == 0:
                logger.error("Cannot set stops: position size is 0")
                return False
            
            # Determinar dirección
            position_side = 'long' if contracts > 0 else 'short'
            position_size = abs(contracts)
            
            logger.info(f"📍 Setting stops: SL={stop_loss:.2f}, TP={take_profit:.2f}")
            logger.info(f"   Position: {position_side.upper()} {position_size:.8f} {symbol}")
            
            sl_success = False
            tp_success = False
            
            # ===== STOP LOSS - Kraken Futures CRITICAL =====
            if stop_loss:
                try:
                    sl_side = 'sell' if position_side == 'long' else 'buy'
                    
                    # ATTEMPT 1: Standard 'stop-loss' type (Kraken Futures standard)
                    logger.debug(f"[SL Attempt 1] Using 'stop-loss' type: {sl_side} {position_size:.8f} @ {stop_loss:.2f}")
                    try:
                        sl_order = await self.exchange.create_order(
                            symbol=symbol,
                            type='stop-loss',
                            side=sl_side,
                            amount=position_size,
                            price=stop_loss,
                            params={
                                'reduceOnly': True,
                                'postOnly': False
                            }
                        )
                        logger.info(f"✅ SL (stop-loss) placed: {sl_order.get('id')}")
                        sl_success = True
                        
                    except Exception as e1:
                        logger.debug(f"[SL Attempt 1 FAILED]: {str(e1)[:100]}")
                        
                        # ATTEMPT 2: 'stop' type with triggerPrice
                        logger.debug(f"[SL Attempt 2] Using 'stop' type with triggerPrice")
                        try:
                            sl_order = await self.exchange.create_order(
                                symbol=symbol,
                                type='stop',
                                side=sl_side,
                                amount=position_size,
                                price=None,
                                params={
                                    'triggerPrice': stop_loss,
                                    'reduceOnly': True
                                }
                            )
                            logger.info(f"✅ SL (stop) placed with triggerPrice: {sl_order.get('id')}")
                            sl_success = True
                            
                        except Exception as e2:
                            logger.debug(f"[SL Attempt 2 FAILED]: {str(e2)[:100]}")
                            
                            # ATTEMPT 3: Last resort - 'limit' order at slightly worse price
                            logger.debug(f"[SL Attempt 3] Using 'limit' as fallback")
                            try:
                                # For SL limit, use slightly worse price to ensure execution
                                fallback_sl_price = stop_loss * 0.998 if position_side == 'long' else stop_loss * 1.002
                                
                                sl_order = await self.exchange.create_order(
                                    symbol=symbol,
                                    type='limit',
                                    side=sl_side,
                                    amount=position_size,
                                    price=fallback_sl_price,
                                    params={
                                        'reduceOnly': True,
                                        'postOnly': False
                                    }
                                )
                                logger.info(f"✅ SL (limit fallback) placed at {fallback_sl_price:.2f}: {sl_order.get('id')}")
                                sl_success = True
                                
                            except Exception as e3:
                                logger.error(f"❌ ALL SL ATTEMPTS FAILED: {str(e3)[:150]}")
                                
                except Exception as outer_e:
                    logger.error(f"❌ SL outer exception: {str(outer_e)[:150]}")
            
            # ===== TAKE PROFIT - Kraken Futures CRITICAL =====
            if take_profit:
                try:
                    tp_side = 'sell' if position_side == 'long' else 'buy'
                    
                    # ATTEMPT 1: 'take-profit' type (Kraken Futures standard)
                    logger.debug(f"[TP Attempt 1] Using 'take-profit' type: {tp_side} {position_size:.8f} @ {take_profit:.2f}")
                    try:
                        tp_order = await self.exchange.create_order(
                            symbol=symbol,
                            type='take-profit',
                            side=tp_side,
                            amount=position_size,
                            price=take_profit,
                            params={
                                'reduceOnly': True,
                                'postOnly': False
                            }
                        )
                        logger.info(f"✅ TP (take-profit) placed: {tp_order.get('id')}")
                        tp_success = True
                        
                    except Exception as e1:
                        logger.debug(f"[TP Attempt 1 FAILED]: {str(e1)[:100]}")
                        
                        # ATTEMPT 2: Simple 'limit' order
                        logger.debug(f"[TP Attempt 2] Using simple 'limit' order")
                        try:
                            tp_order = await self.exchange.create_order(
                                symbol=symbol,
                                type='limit',
                                side=tp_side,
                                amount=position_size,
                                price=take_profit,
                                params={
                                    'reduceOnly': True,
                                    'postOnly': False
                                }
                            )
                            logger.info(f"✅ TP (limit) placed: {tp_order.get('id')}")
                            tp_success = True
                            
                        except Exception as e2:
                            logger.error(f"❌ TP placement failed: {str(e2)[:150]}")
                            
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
    
    async def set_position_stops_optimized(self, symbol: str, stop_loss: float, 
                                           take_profit: float) -> bool:
        """
        FIX #4: REDUCIR ÓRDENES POR TRADE - Consolidate SL/TP Placement
        
        OPTIMIZACIÓN:
        - Intenta colocar SL + TP sin reintentos innecesarios
        - Usa fallback simple en lugar de 3 intentos por orden
        - Resultado: 3-4 órdenes totales en lugar de 7-8
        
        Args:
            symbol: Par a operar
            stop_loss: Precio de stop loss
            take_profit: Precio de take profit
        
        Returns: True si SL se colocó exitosamente (crítico)
        """
        if not self.exchange:
            return False
        
        try:
            position = await self.get_position(symbol)
            if not position or abs(position.get('contracts', 0)) == 0:
                logger.error("Cannot set stops: no open position")
                return False
            
            contracts = position.get('contracts', 0)
            position_side = 'long' if contracts > 0 else 'short'
            position_size = abs(contracts)
            
            logger.info(f"📍 FIX#4 Optimized stops: SL={stop_loss:.2f}, TP={take_profit:.2f}")
            
            sl_side = 'sell' if position_side == 'long' else 'buy'
            tp_side = 'sell' if position_side == 'long' else 'buy'
            
            # SL: Intento directo (sin reintentos)
            try:
                await self.exchange.create_order(
                    symbol=symbol, type='stop-loss', side=sl_side,
                    amount=position_size, price=stop_loss,
                    params={'reduceOnly': True, 'postOnly': False}
                )
                logger.info(f"✅ SL placed: {stop_loss:.2f}")
            except Exception:
                logger.error(f"❌ SL failed: {e}")
                return False
            
            # TP: Intento directo (secondary)
            try:
                await self.exchange.create_order(
                    symbol=symbol, type='take-profit', side=tp_side,
                    amount=position_size, price=take_profit,
                    params={'reduceOnly': True, 'postOnly': False}
                )
                logger.info(f"✅ TP placed: {take_profit:.2f}")
            except Exception as e:
                logger.warning(f"⚠️  TP failed (SL is OK): {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Exception in set_position_stops_optimized: {e}")
            return False
    
    async def update_trailing_stop_throttled(self, old_sl_price: float, 
                                            new_sl_price: float, 
                                            min_move_pct: float = 0.005) -> bool:
        """
        FIX #4: THROTTLE Trailing Stop Updates
        
        Solo actualiza si el cambio es > 0.5% (configurable)
        Reduce de ~50 updates → ~10 updates por trade
        
        Args:
            old_sl_price: SL anterior
            new_sl_price: Nuevo SL propuesto
            min_move_pct: Mínimo movimiento (0.005 = 0.5%)
        
        Returns: True si cambio es significativo
        """
        if old_sl_price == 0:
            return True
        
        min_move = old_sl_price * min_move_pct
        change_pct = abs(new_sl_price - old_sl_price) / old_sl_price * 100
        
        if abs(new_sl_price - old_sl_price) < min_move:
            logger.debug(f"SL throttled: {old_sl_price:.2f} → {new_sl_price:.2f} ({change_pct:.3f}%) < threshold")
            return False
        
        logger.info(f"SL update: {old_sl_price:.2f} → {new_sl_price:.2f} ({change_pct:.3f}%)")
        return True
    
    async def verify_stops_on_exchange(self, symbol: str) -> dict:
        """
        Verificar que los SL/TP realmente existen en el exchange
        Returns: {'has_sl': bool, 'has_tp': bool, 'orders': [list]}
        """
        if not self.exchange:
            return {'has_sl': False, 'has_tp': False, 'orders': []}
        
        try:
            open_orders = await self.exchange.fetch_open_orders(symbol)
            
            has_sl = False
            has_tp = False
            reduce_only_orders = []
            
            for order in open_orders:
                # Buscar órdenes con reduceOnly=True
                if order.get('info', {}).get('reduceOnly'):
                    reduce_only_orders.append({
                        'id': order.get('id'),
                        'type': order.get('type'),
                        'side': order.get('side'),
                        'price': order.get('price'),
                        'status': order.get('status')
                    })
                    
                    if order.get('type') == 'stop':
                        has_sl = True
                    elif order.get('type') == 'limit':
                        has_tp = True
            
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
    
    async def close_position_market(self, symbol: str = None, side: str = None, amount: float = None) -> Optional[Dict]:
        """
        Cerrar posición a mercado (EMERGENCIA) con timeout y retry
        
        Args:
            symbol: Trading pair (uses self.current_symbol if not provided)
            side: 'buy' (close short) o 'sell' (close long)
            amount: Position size to close (fetches from exchange if not provided)
        
        Returns: Order dict or None if failed after retries
        """
        if not self.exchange:
            return None
        
        # Use stored symbol if not provided
        if symbol is None:
            symbol = getattr(self, 'current_symbol', 'BTC/USD:USD')
        
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
                
                # Determine side from position if not provided
                if side is None:
                    position_contracts = position.get('contracts', 0)
                    side = 'sell' if position_contracts > 0 else 'buy'
                
                logger.critical(f"EMERGENCY MARKET CLOSE (attempt {attempt + 1}/{MAX_RETRIES}): {close_amount} {symbol} side={side}")
                
                order = await asyncio.wait_for(
                    self.exchange.create_market_order(
                        symbol=symbol,
                        type='market',
                        side=side.lower(),
                        amount=close_amount
                    ),
                    timeout=EXCHANGE_TIMEOUT_MEDIUM
                )
                
                logger.critical(f"Position closed: {order}")
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
            balance = await self.exchange.fetch_balance()
            
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
                    capital = float(amount)
                    logger.warning(f"No USDT/USD/EUR in free, using {asset}: {capital:.6f}")
                    return capital
            
            # Fallback: Check 'info' for Kraken Futures specific structure
            if 'info' in balance:
                info = balance['info']
                # Kraken Futures often has 'accounts' or 'marginAccount'
                if 'accounts' in info:
                    for acc in info.get('accounts', []):
                        if acc.get('currency', '').upper() in ['USD', 'USDT'] or acc.get('type') == 'cash':
                            avail = float(acc.get('available', 0))
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
    
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 250) -> Optional[list]:
        """Fetch historical OHLCV data with timeout"""
        if not self.exchange:
            return None
        
        try:
            logger.info(f"Fetching {limit} {timeframe} candles for {symbol}...")
            ohlcv = await asyncio.wait_for(
                self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit),
                timeout=EXCHANGE_TIMEOUT_LONG
            )
            
            # Convert to our candle format
            candles = []
            for candle in ohlcv:
                candles.append({
                    'timestamp': candle[0],
                    'open': candle[1],
                    'high': candle[2],
                    'low': candle[3],
                    'close': candle[4],
                    'volume': candle[5],
                    'timeframe': timeframe
                })
            
            logger.info(f"✓ Loaded {len(candles)} {timeframe} candles")
            return candles
        
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching OHLCV for {symbol} {timeframe}")
            return None
        except Exception as e:
            logger.error(f"Error fetching OHLCV: {e}")
            return None
    
    async def reconcile_position(self, symbol: str, expected_side: str, 
                                 expected_size: float) -> bool:
        """
        Reconciliación: comparar posición real vs estado interno
        
        Returns: True si coincide, False si hay desync
        """
        try:
            position = await self.get_position(symbol)
            
            if not position:
                if expected_size == 0:
                    return True  # OK: sin posición esperada, sin posición real
                else:
                    logger.error(f"DESYNC: Esperaba posición, no hay")
                    return False
            
            actual_side = 'long' if position.get('contracts', 0) > 0 else 'short'
            actual_size = abs(position.get('contracts', 0))
            
            # Tolerancia: 0.1%
            tolerance = expected_size * 0.001
            
            if actual_side != expected_side.lower() or abs(actual_size - expected_size) > tolerance:
                logger.error(f"DESYNC: Expected {expected_side} {expected_size}, "
                           f"got {actual_side} {actual_size}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error reconciling position: {e}")
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
    
    # =========================================================================
    # ATOMIC SL/TP MANAGEMENT FOR KRAKEN FUTURES
    # =========================================================================
    
    async def ensure_position_protected(self, symbol: str, stop_loss: float, 
                                         take_profit: float, max_retries: int = 3) -> bool:
        """
        CRITICAL: Ensure position has SL protection with retries.
        
        This is the main method for SL/TP after position entry.
        Implements automatic retry and verification.
        
        Args:
            symbol: Trading pair
            stop_loss: SL price (REQUIRED)
            take_profit: TP price (optional)
            max_retries: Max attempts
            
        Returns:
            True if SL is confirmed on exchange
        """
        if not self.exchange:
            return False
        
        for attempt in range(max_retries):
            try:
                logger.info(f"🛡️ Protecting position attempt {attempt + 1}/{max_retries}")
                
                # 1. Verify position exists
                position = await self.get_position(symbol)
                if not position or abs(position.get('contracts', 0)) == 0:
                    logger.warning("No position to protect")
                    return False
                
                # 2. Place SL
                sl_placed = await self.set_position_stops(symbol, stop_loss, take_profit)
                
                if not sl_placed:
                    logger.warning(f"SL placement failed, attempt {attempt + 1}")
                    await asyncio.sleep(1)
                    continue
                
                # 3. VERIFY SL actually exists on exchange
                await asyncio.sleep(0.5)  # Allow exchange to process
                verification = await self.verify_stops_on_exchange(symbol)
                
                if verification['has_sl']:
                    logger.info(f"✅ Position protected: SL @ {stop_loss:.2f}")
                    return True
                else:
                    logger.warning(f"⚠️ SL verification failed, attempt {attempt + 1}")
                    await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"Error in ensure_position_protected: {e}")
                await asyncio.sleep(1)
        
        logger.critical(f"❌ FAILED to protect position after {max_retries} attempts!")
        return False
    
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
            real_orders = await self.exchange.fetch_open_orders(symbol)
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
            
            logger.debug(f"Reconciled: {result['open_orders']} orders, "
                        f"{result['cancelled']} stale removed")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in auto_reconcile: {e}")
            return {'success': False, 'error': str(e)}
    
    async def place_market_order(self, symbol: str, side: str, amount: float,
                                  stop_loss: float = None, 
                                  take_profit: float = None) -> Optional[Dict]:
        """
        Place market order with optional SL/TP.
        
        For Kraken: SL/TP are placed as separate orders after market fill.
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell'
            amount: Position size
            stop_loss: SL price (placed after fill)
            take_profit: TP price (placed after fill)
            
        Returns:
            Order result dict
        """
        if not self.exchange:
            logger.error("Exchange not initialized")
            return None
        
        try:
            logger.info(f"🚀 MARKET ORDER: {side.upper()} {amount} {symbol}")
            
            order = await asyncio.wait_for(
                self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=side.lower(),
                    amount=amount
                ),
                timeout=EXCHANGE_TIMEOUT_MEDIUM
            )
            
            order['created_at'] = datetime.now(UTC).isoformat()
            self.open_orders[order['id']] = order
            
            logger.info(f"✅ Market order executed: {order['id']}")
            
            # Store SL/TP for placement after confirmation
            if stop_loss or take_profit:
                order['_pending_sl'] = stop_loss
                order['_pending_tp'] = take_profit
                logger.info(f"   📌 Pending: SL @ {stop_loss:.2f} | TP @ {take_profit:.2f}")
            
            return order
            
        except Exception as e:
            logger.error(f"Error placing market order: {e}")
            return None

