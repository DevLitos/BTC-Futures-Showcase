"""
Kraken Futures WebSocket - Fuente única de datos de mercado
CRÍTICO: Este proceso SOLO ingesta datos, NO trading logic
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Callable
import websockets
from collections import deque

logger = logging.getLogger(__name__)


from engine.config import KillConditions

class KrakenFuturesWebSocket:
    """
    WebSocket dedicado para Kraken Futures
    - Responsabilidades: Data streaming ONLY
    - No state management
    - No trading logic
    """
    
    def __init__(self, symbols: list = None):
        self.symbols = symbols or ['BTC/USD']
        self.ws = None
        self.is_connected = False
        self._shutdown = False  # Flag para apagado limpio
        
        # Heartbeat
        self.last_tick = time.time()
        self.heartbeat_warning_threshold = 30.0  # segundos
        self.heartbeat_kill_threshold = KillConditions.WEBSOCKET_TIMEOUT_SECONDS    # From config (75s)
        
        # Data buffers (ring buffers para eficiencia)
        self.tick_buffer = deque(maxlen=1000)  # Raw ticks
        self.trade_buffer = deque(maxlen=10000)  # Para CVD
        self.depth_buffer = deque(maxlen=500)   # Order book snapshots
        
        # Callbacks para que el bot consume
        self.on_tick: Optional[Callable] = None
        self.on_trade: Optional[Callable] = None
        self.on_depth: Optional[Callable] = None
        self.on_candle_1m: Optional[Callable] = None
        self.on_candle_5m: Optional[Callable] = None
        self.on_candle_1h: Optional[Callable] = None
        
        # Candle builders (para armar velas de Kraken ticks)
        self.candle_builders = {
            '1m': {'data': {}, 'last_close': {}},
            '5m': {'data': {}, 'last_close': {}},
            '1h': {'data': {}, 'last_close': {}}
        }
        
        logger.info(f"KrakenFuturesWebSocket initialized for {symbols}")
    
    async def connect(self):
        """Conectar y mantener conexión activa"""
        logger.info("Connecting to Kraken Futures WebSocket...")
        
        while not self._shutdown:
            try:
                async with websockets.connect(
                    'wss://futures.kraken.com/ws',
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self.ws = ws
                    self.is_connected = True
                    self.last_tick = time.time()
                    logger.info("✅ Connected to Kraken Futures")
                    
                    # Subscribe
                    await self._subscribe()
                    
                    # Main message loop
                    await self._message_loop()
                    
            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled - shutting down")
                self._shutdown = True
                self.is_connected = False
                break
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                self.is_connected = False
                if self._shutdown:
                    break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self.is_connected = False
                if self._shutdown:
                    break
            
            # Check shutdown before reconnecting
            if self._shutdown:
                break
            
            # Wait before reconnecting
            logger.info("Waiting 5s before reconnect...")
            await asyncio.sleep(5)
        
        logger.info("WebSocket connect() loop exited")
    
    async def _subscribe(self):
        """Subscribe a channels de Kraken Futures (API v1)"""
        for symbol in self.symbols:
            # Kraken Futures usa formato PF_XBTUSD (no BTCUSD)
            if symbol == 'BTC/USD':
                kraken_symbol = 'PF_XBTUSD'
            else:
                kraken_symbol = symbol.replace('/', '')  # Fallback genérico
            
            logger.info(f"🔌 Subscribing to {kraken_symbol} (from {symbol})")
            
            # 1. Trades
            trade_sub = {
                'event': 'subscribe',
                'feed': 'trade',
                'product_ids': [kraken_symbol]
            }
            logger.info(f"   Trade subscription: {trade_sub}")
            await self.ws.send(json.dumps(trade_sub))
            
            # 2. Order book depth
            book_sub = {
                'event': 'subscribe',
                'feed': 'book',
                'product_ids': [kraken_symbol]
            }
            logger.info(f"   Book subscription: {book_sub}")
            await self.ws.send(json.dumps(book_sub))
            
            # 3. Ticker (Price fallback + Velocity) - NEW!
            ticker_sub = {
                'event': 'subscribe',
                'feed': 'ticker',
                'product_ids': [kraken_symbol]
            }
            logger.info(f"   Ticker subscription: {ticker_sub}")
            await self.ws.send(json.dumps(ticker_sub))
            
            logger.info(f"✅ Subscription sent for {kraken_symbol}")
    
    async def _message_loop(self):
        """Loop principal de mensajes"""
        while self.is_connected and not self._shutdown:
            try:
                # Use kill threshold + 5s buffer for socket timeout
                socket_timeout = self.heartbeat_kill_threshold + 5.0
                msg = await asyncio.wait_for(self.ws.recv(), timeout=socket_timeout)
                data = json.loads(msg)
                
                # Update heartbeat
                self.last_tick = time.time()
                
                # Procesar por tipo
                await self._process_message(data)
                
            except asyncio.CancelledError:
                logger.info("Message loop cancelled")
                self._shutdown = True
                break
            except asyncio.TimeoutError:
                logger.warning(f"WebSocket timeout - no message in {socket_timeout}s")
                await self._check_heartbeat()
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed in message loop: {e}")
                self.is_connected = False
                break
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # Backoff para evitar busy-loop en errores repetidos
                await asyncio.sleep(1.0)
    
    async def _process_message(self, data: dict):
        """Procesar mensaje de Kraken"""
        
        # Log TODOS los mensajes para debug (pero corto)
        # logger.debug(f"📩 WS Message: {json.dumps(data)[:200]}")
        
        # Kraken v1 usa 'feed' en lugar de 'channel'
        if 'feed' not in data:
            if 'event' in data:
                event = data['event']
                if event == 'info':
                    return # Ignore info messages
                if event == 'alert':
                    logger.warning(f"⚠️ WS Alert: {data}")
                    return
                if event == 'error':
                    logger.error(f"❌ WS Error: {data}")
                    return
            return
        
        feed = data.get('feed')
        
        if feed == 'trade':
            await self._handle_trade(data)
        elif feed == 'book':
            await self._handle_book(data)
        elif feed == 'ticker':
            await self._handle_ticker(data)
    
    async def _handle_trade(self, data: dict):
        """
        Procesar trade. Soporta formatos:
        1. Lista en 'data': {'data': [[ts, side, price, size], ...]}
        2. Dict plano: {'feed': 'trade', 'price': 100, 'qty': 1, 'time': 12345, ...}
        """
        try:
            trades = []
            
            # Case 1: Standard 'data' list
            if 'data' in data and isinstance(data['data'], list):
               trades = data['data']
            # Case 2: Flat dictionary (Trade update)
            elif 'price' in data and 'qty' in data:
               trades = [data]
            
            # VALIDATION 1: Empty trade list
            if not trades:
                return 
            
            symbol = data.get('product_id')
            if not symbol:
                # Fallback if product_id is not directly in the trade message
                # Use the first subscribed symbol as a best guess.
                symbol = self.symbols[0] if self.symbols else 'PF_XBTUSD'

            for trade_item in trades:
                # Extract fields based on format (List vs Dict)
                if isinstance(trade_item, dict):
                    # Dict format (seen in logs)
                    raw_ts = trade_item.get('time', 0)
                    side = trade_item.get('side')
                    price = float(trade_item.get('price', 0))
                    size = float(trade_item.get('qty', 0))
                elif isinstance(trade_item, list) and len(trade_item) >= 4:
                    # List format (legacy?)
                    raw_ts = trade_item[0]
                    side = trade_item[1]
                    price = float(trade_item[2])
                    size = float(trade_item[3])
                else:
                    continue

                ts = float(raw_ts)
                
                # FIX: Detect if timestamp is in seconds (e.g. 1.7e9 vs 1.7e12)
                # Kraken documentation varies, but if < 1e11 it is definitely seconds
                if ts < 100000000000:
                    ts = ts * 1000
                
                # Ensure integer MS for consistency
                ts = int(ts)
                
                trade_obj = {
                    'timestamp': ts, 
                    'symbol': symbol,
                    'side': side,
                    'price': price,
                    'size': size,
                    'is_buyer_maker': side == 'sell'
                }
                
                self.trade_buffer.append(trade_obj)
                
                # Callback para bot
                if self.on_trade:
                    await self.on_trade(trade_obj)
                
                # Actualizar candles
                await self._update_candles(trade_obj)
        
        except Exception as e:
            logger.error(f"Error handling trade: {e} - Data: {str(data)[:100]}")
    
    async def _handle_book(self, data: dict):
        """Procesar order book depth"""
        try:
            # Kraken Futures book format:
            # { "feed": "book", "product_id": "PF_XBTUSD", "side": "buy"/"sell", "price": ..., "qty": ... }
            # Or snapshot: { "feed": "book_snapshot", "bids": [...], "asks": [...] }
            
            feed_type = data.get('feed', '')
            
            if feed_type == 'book_snapshot':
                # Full snapshot - process bids and asks arrays
                bids = data.get('bids', [])
                asks = data.get('asks', [])
                
                for bid in bids:
                    if len(bid) >= 2:
                        price, qty = float(bid[0]), float(bid[1])
                        depth_event = {'side': 'bid', 'price': price, 'qty': qty}
                        self.depth_buffer.append(depth_event)
                        if self.on_depth:
                            await self.on_depth(depth_event)
                
                for ask in asks:
                    if len(ask) >= 2:
                        price, qty = float(ask[0]), float(ask[1])
                        depth_event = {'side': 'ask', 'price': price, 'qty': qty}
                        self.depth_buffer.append(depth_event)
                        if self.on_depth:
                            await self.on_depth(depth_event)
            
            elif feed_type == 'book':
                # Incremental update
                side = data.get('side', '')
                price = float(data.get('price', 0))
                qty = float(data.get('qty', 0))
                
                if price > 0:
                    depth_event = {
                        'side': 'bid' if side.lower() == 'buy' else 'ask',
                        'price': price,
                        'qty': qty
                    }
                    self.depth_buffer.append(depth_event)
                    if self.on_depth:
                        await self.on_depth(depth_event)
        
        except Exception as e:
            logger.error(f"Error processing book: {e}")
    
    async def _handle_ticker(self, data: dict):
        """Procesar ticker (precio actual)"""
        try:
            # { "feed": "ticker", "product_id": "PF_XBTUSD", "bid": ..., "ask": ..., "last": ... }
            symbol = data.get('product_id')
            last = float(data.get('last', 0))
            
            if last == 0:
                return

            tick = {
                'timestamp': int(time.time() * 1000),
                'symbol': symbol,
                'price': last,
                'type': 'tick'
            }
            
            # logger.info(f"🎫 TICKER: {symbol} @ {last}")
            
            self.tick_buffer.append(tick)
            self.last_tick = time.time()  # Ticker is a good heartbeat
            
            # Callback
            if self.on_tick:
                await self.on_tick(tick)
                
            # Fallback: Update candles from ticker if trades are missing?
            # Ideally NO because we lose volume info. 
            # But maybe useful for 'close' price update if trades are sparse.
            # For now, let's stick to trades for candles.
        
        except Exception as e:
            logger.error(f"Error processing ticker: {e}")
    
    async def _update_candles(self, trade: dict):
        """
        Armar candles de 1m, 5m, 1h desde trades
        """
        ts = trade['timestamp']
        symbol = trade['symbol']
        price = trade['price']
        size = trade['size']
        
        for timeframe in ['1m', '5m', '1h']:
            candle_key = self._get_candle_key(ts, timeframe)
            
            # Initialize last_close if first time
            if symbol not in self.candle_builders[timeframe]['last_close']:
                self.candle_builders[timeframe]['last_close'][symbol] = candle_key
                # Create fresh candle
                self.candle_builders[timeframe]['data'][symbol] = {
                    'open': price, 'high': price, 'low': price, 'close': price,
                    'volume': size, 'timestamp': ts
                }
                logger.info(f"Initialized {timeframe} candle for {symbol} at key {candle_key}")
                continue

            last_key = self.candle_builders[timeframe]['last_close'][symbol]
            
            if candle_key != last_key:
                # CANDLE CLOSED
                # 1. Emit the COMPLETED candle (from buffer)
                if symbol in self.candle_builders[timeframe]['data']:
                    closed_candle = self.candle_builders[timeframe]['data'][symbol]
                    closed_candle['timeframe'] = timeframe
                    
                    # Emit
                    if timeframe == '1m' and self.on_candle_1m:
                        await self.on_candle_1m(closed_candle)
                    elif timeframe == '5m' and self.on_candle_5m:
                        await self.on_candle_5m(closed_candle)
                    elif timeframe == '1h' and self.on_candle_1h:
                        await self.on_candle_1h(closed_candle)
                        
                # 2. Start NEW candle with current trade
                self.candle_builders[timeframe]['data'][symbol] = {
                    'open': price, 'high': price, 'low': price, 'close': price,
                    'volume': size, 'timestamp': ts
                }
                
                # 3. Update key
                self.candle_builders[timeframe]['last_close'][symbol] = candle_key
                
            else:
                # UPDATE EXISTING CANDLE
                candle = self.candle_builders[timeframe]['data'][symbol]
                candle['high'] = max(candle['high'], price)
                candle['low'] = min(candle['low'], price)
                candle['close'] = price
                candle['volume'] += size
                candle['timestamp'] = ts # Keep updating ts to latest trade

    def _get_candle_key(self, timestamp: int, timeframe: str) -> int:
        """Obtener clave de candle (timestamp redondeado)"""
        ts_seconds = timestamp // 1000
        
        if timeframe == '1m':
            return (ts_seconds // 60) * 60
        elif timeframe == '5m':
            return (ts_seconds // 300) * 300
        elif timeframe == '1h':
            return (ts_seconds // 3600) * 3600
        
        return ts_seconds
    
    async def _check_heartbeat(self):
        """Verificar heartbeat"""
        now = time.time()
        silence = now - self.last_tick
        
        if silence > self.heartbeat_kill_threshold:
            logger.critical(f"HEARTBEAT KILL: WebSocket silent for {silence:.1f}s")
            self.is_connected = False
        elif silence > self.heartbeat_warning_threshold:
            logger.warning(f"Heartbeat warning: {silence:.1f}s since last tick")
    
    def get_tick_buffer(self) -> list:
        return list(self.tick_buffer)
    
    def get_trade_buffer(self) -> list:
        return list(self.trade_buffer)
    
    def get_depth_buffer(self) -> list:
         return list(self.depth_buffer)
    
    def is_heartbeat_healthy(self) -> bool:
        silence = time.time() - self.last_tick
        return silence < self.heartbeat_kill_threshold
    
    async def disconnect(self):
        logger.info("WebSocket disconnect() called")
        self._shutdown = True
        self.is_connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"Error closing websocket: {e}") 
