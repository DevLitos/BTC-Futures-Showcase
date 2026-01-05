"""
Main Entry Point for BTC Perpetual Trading System - ORDEN CORRECTO

Prioridades de inicialización:
1. DATOS DE MERCADO (WebSocket Kraken)
2. BUFFER MULTI-TIMEFRAME sincronizado
3. EJECUCIÓN REAL (REST + CCXT)
4. DATOS EXTERNOS (Funding, Macro)
5. PERSISTENCIA (Parquet/PostgreSQL)
6. SIGNAL ENGINE (usa todo lo anterior)
"""

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path

# Load environment variables from ~/.trad2_env
def load_env_file():
    """Load environment variables from ~/.trad2_env"""
    env_file = Path.home() / '.trad2_env'
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Handle 'export ' prefix if present
                    if line.startswith('export '):
                        line = line[7:].strip()
                    
                    if '=' in line:
                        key, value = line.split('=', 1)
                        # Remove quotes if present
                        value = value.strip()
                        if (value.startswith('"') and value.endswith('"')) or \
                           (value.startswith("'") and value.endswith("'")):
                            value = value[1:-1]
                            
                        os.environ[key.strip()] = value
        logger.info(f"Environment variables loaded from {env_file}")
    else:
        logger.warning(f"Environment file {env_file} not found")

# 1. DATOS DE MERCADO
from ingest.kraken_websocket import KrakenFuturesWebSocket
from ingest.order_book import OrderBookManager

# 2. BUFFER MULTI-TIMEFRAME
from engine.market_data import SynchronizedMultiTimeframeBuffer

# 3. EJECUCIÓN REAL
from engine.execution import RESTExecutionEngine

# 4. DATOS EXTERNOS
from engine.external_data import ExternalDataManager

# 5. PERSISTENCIA
from engine.persistence import TradeLogger, PositionReconciler

# 6. SIGNAL ENGINE
from engine.signal_engine import SignalEngine

# Legacy (mantener compatibilidad)
from output.output_manager import OutputManager
from monitoring.metrics import start_metrics_server

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('MainApp')

# Load environment variables before using them
load_env_file()

# Configuration from environment
KRAKEN_API_KEY = os.getenv('KRAKEN_API_KEY', '')
KRAKEN_SECRET = os.getenv('KRAKEN_SECRET', '')
KRAKEN_FUTURES_API_KEY = os.getenv('KRAKEN_FUTURES_API_KEY', '')
KRAKEN_FUTURES_SECRET = os.getenv('KRAKEN_FUTURES_SECRET', '')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_SECRET = os.getenv('BINANCE_SECRET', '')

# Symbol mapping based on exchange
EXCHANGE = os.getenv('EXCHANGE', 'krakenfutures')  # Default to krakenfutures for this bot

# AUTO-FIX: This is a PERPETUAL system. If config says 'kraken' (spot), it's wrong. Force futures.
if EXCHANGE == 'kraken':
    logger.warning("Configured exchange 'kraken' (Spot) does not support perpetuals. Forcing 'krakenfutures'.")
    EXCHANGE = 'krakenfutures'

# Fallback: if user put futures keys in standard KRAKEN_ variables
if EXCHANGE == 'krakenfutures':
    if not KRAKEN_FUTURES_API_KEY and KRAKEN_API_KEY:
        KRAKEN_FUTURES_API_KEY = KRAKEN_API_KEY
        KRAKEN_FUTURES_SECRET = KRAKEN_SECRET
        logger.info("Using KRAKEN_API_KEY as KRAKEN_FUTURES_API_KEY")

if EXCHANGE == 'krakenfutures':
    WS_SYMBOL = 'PF_XBTUSD'  # Kraken Futures WebSocket
    CCXT_SYMBOL = 'BTC/USD:USD'  # Kraken Futures CCXT perpetual
elif EXCHANGE == 'binance':
    WS_SYMBOL = 'btcusdt'  # Binance Futures WebSocket (lowercase)
    CCXT_SYMBOL = 'BTC/USDT:USDT'  # Binance Futures CCXT perpetual swap
elif EXCHANGE == 'kraken':
    WS_SYMBOL = 'XBT/USD'  # Kraken Spot WebSocket
    CCXT_SYMBOL = 'BTC/USD'  # Kraken Spot
else:
    WS_SYMBOL = 'BTCUSD'
    CCXT_SYMBOL = 'BTC/USD'

PERSISTENCE = os.getenv('PERSISTENCE', 'parquet')  # parquet, postgresql
DB_URL = os.getenv('DATABASE_URL', None)
DEMO_MODE = os.getenv('DEMO_MODE', 'false').lower() == 'true'  # Run without API keys


class TradingSystemOrchestrator:
    """
    Orquesta el sistema en el orden CORRECTO
    
    Orden de inicialización:
    1. WebSocket datos (Kraken)
    2. Buffer multi-timeframe sincronizado
    3. Order book
    4. Execution (REST)
    5. External data (Funding, Macro)
    6. Persistencia
    7. Signal engine
    8. FALLBACK: REST polling para velas si WebSocket falla
    """
    
    def __init__(self):
        # 1. DATOS DE MERCADO (WebSocket solo para Kraken Futures por ahora)
        if EXCHANGE == 'krakenfutures':
            self.kraken_ws = KrakenFuturesWebSocket([WS_SYMBOL])
            self.order_book = OrderBookManager(WS_SYMBOL)
        else:
            self.kraken_ws = None
            self.order_book = None
            logger.warning(f"WebSocket not implemented for {EXCHANGE} - using REST polling only")
        
        # FALLBACK: Flag para monitorear si WebSocket está activo
        self.ws_healthy = False
        self.last_ws_tick = None
        
        # 2. BUFFER SINCRONIZADO
        self.market_data = SynchronizedMultiTimeframeBuffer(CCXT_SYMBOL)
        
        # 3. EJECUCIÓN
        if EXCHANGE == 'krakenfutures':
            credentials = {
                'enableRateLimit': True,
            }
            if KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_SECRET and not DEMO_MODE:
                credentials['apiKey'] = KRAKEN_FUTURES_API_KEY
                credentials['secret'] = KRAKEN_FUTURES_SECRET
        elif EXCHANGE == 'kraken':
            credentials = {
                'enableRateLimit': True,
            }
            if KRAKEN_API_KEY and KRAKEN_SECRET and not DEMO_MODE:
                credentials['apiKey'] = KRAKEN_API_KEY
                credentials['secret'] = KRAKEN_SECRET
        elif EXCHANGE == 'binance':
            credentials = {
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            }
            if BINANCE_API_KEY and BINANCE_SECRET and not DEMO_MODE:
                credentials['apiKey'] = BINANCE_API_KEY
                credentials['secret'] = BINANCE_SECRET
        else:
            credentials = {'enableRateLimit': True}
            
        self.execution = RESTExecutionEngine(EXCHANGE, credentials)
        
        # 4. DATOS EXTERNOS
        self.external_data = ExternalDataManager(CCXT_SYMBOL, EXCHANGE)
        
        # 5. PERSISTENCIA
        self.trade_logger = TradeLogger(PERSISTENCE, DB_URL)
        self.reconciler = PositionReconciler(self.execution)
        
        # 6. SIGNAL ENGINE
        self.signal_engine = SignalEngine(market_data=self.market_data,
                                         execution=self.execution,
                                         external_data=self.external_data,
                                         trade_logger=self.trade_logger)
        
        # Legacy
        self.output_manager = OutputManager()
        
        self.tasks = []
        self.demo_mode = DEMO_MODE
        self.is_running = False
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum} - initiating graceful shutdown...")
        asyncio.create_task(self.shutdown())
    
    async def _load_historical_data(self):
        """Load historical candles to bootstrap the system"""
        logger.info("\n📊 Loading historical data...")
        
        try:
            # Load 1h candles (need 200+ for EMA 200)
            candles_1h = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '1h', limit=250)
            if candles_1h:
                self.market_data.load_historical_candles(candles_1h, '1h')
            else:
                logger.warning("Failed to load 1h historical data")
            
            # Load 5m candles (need 50+ for EMA 50)
            candles_5m = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '5m', limit=100)
            if candles_5m:
                self.market_data.load_historical_candles(candles_5m, '5m')
            else:
                logger.warning("Failed to load 5m historical data")
            
            # Load 1m candles (need 20+ for volume MA)
            candles_1m = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '1m', limit=50)
            if candles_1m:
                self.market_data.load_historical_candles(candles_1m, '1m')
            else:
                logger.warning("Failed to load 1m historical data")
            
            logger.info("✓ Historical data loaded successfully")
            
        except Exception as e:
            logger.error(f"Error loading historical data: {e}", exc_info=True)
    
    async def initialize(self):
        """
        Inicializar en el orden correcto
        """
        logger.info("=" * 70)
        logger.info("🚀 BTC PERPETUAL TRADING SYSTEM - INITIALIZATION (CORRECT ORDER)")
        logger.info("=" * 70)
        
        if self.demo_mode:
            logger.warning("🔧 DEMO MODE - Running without API keys")
        
        try:
            # 1. WebSocket (solo Kraken por ahora)
            if self.kraken_ws:
                logger.info("\n[1/7] Initializing Kraken WebSocket...")
                
                # Callbacks para procesar datos
                self.kraken_ws.on_tick = self._on_tick
                self.kraken_ws.on_trade = self._on_trade
                self.kraken_ws.on_depth = self._on_depth
                self.kraken_ws.on_candle_1m = self._on_candle_1m
                self.kraken_ws.on_candle_5m = self._on_candle_5m
                self.kraken_ws.on_candle_1h = self._on_candle_1h
                
                self.tasks.append(asyncio.create_task(self.kraken_ws.connect(), name="KrakenWS"))
                await asyncio.sleep(2)  # Esperar a conectar
            else:
                logger.info("\n[1/7] WebSocket disabled - using REST polling only")
            
            # 2. Buffer multi-timeframe (ya inicializado)
            logger.info("[2/7] Market data buffer initialized")
            
            # 3. Order book (ya inicializado)
            logger.info("[3/7] Order book manager initialized")
            
            # 4. Execution engine (REST)
            logger.info("[4/7] Initializing REST execution engine...")
            await self.execution.initialize()
            
            # 4.5. Load historical data to bootstrap
            if not self.demo_mode:
                await self._load_historical_data()
            else:
                logger.info("📊 Skipping historical data load (DEMO MODE)")
            
            # 5. External data (Funding, Macro)
            logger.info("[5/7] Initializing external data monitors...")
            self.tasks.append(asyncio.create_task(self.external_data.start(), name="ExternalData"))
            
            # FALLBACK: Start REST polling for candles (safety net si WS muere)
            logger.info("[5.5/7] Starting REST candle polling fallback...")
            self.tasks.append(asyncio.create_task(self._rest_candle_polling_loop(), name="CandlePolling"))
            
            # 6. Persistencia
            logger.info("[6/7] Initializing persistence logger...")
            await self.trade_logger.start()
            logger.info("✓ Persistence logger started (backpressure queues active)")
            
            # 7. Signal Engine (fetch balance + set leverage)
            logger.info("[7/7] Initializing signal engine (balance + leverage)...")
            leverage = int(os.getenv('LEVERAGE', '10'))  # Default 10x
            
            if self.demo_mode:
                # In demo mode, set a fake capital
                logger.warning("💰 Using demo capital: $10,000")
                self.signal_engine.risk_engine.capital = 10000.0
                logger.info("✓ Signal engine initialized (DEMO MODE)")
            else:
                await self.signal_engine.initialize(symbol=CCXT_SYMBOL, leverage=leverage)
            
            logger.info("\n✅ All components initialized successfully")
            logger.info("=" * 70)
            
        except Exception as e:
            if self.demo_mode:
                logger.warning(f"Initialization warning (DEMO MODE): {e}")
                # Continue in demo mode
            else:
                logger.critical(f"Failed to initialize system: {e}", exc_info=True)
                raise
    
    async def start(self):
        """Start trading"""
        logger.info("=" * 70)
        logger.info("▶️  STARTING TRADING SYSTEM")
        logger.info("=" * 70)
        
        try:
            self.is_running = True
            
            # Start signal engine
            logger.info("Starting signal engine...")
            self.tasks.append(asyncio.create_task(self.signal_engine.start(), name="SignalEngine"))
            
            # Start reconciliation
            logger.info("Starting position reconciliation...")
            self.tasks.append(asyncio.create_task(self.reconciler.start(), name="Reconciler"))
            
            # Start monitoring
            self.tasks.append(asyncio.create_task(self._monitoring_loop(), name="Monitor"))
            
            # Start metrics
            start_metrics_server()
            
            logger.info("=" * 70)
            logger.info("✅ SYSTEM READY")
            logger.info(f"   Trading: {CCXT_SYMBOL} (WS: {WS_SYMBOL})")
            logger.info(f"   Exchange: {EXCHANGE}")
            logger.info(f"   Persistence: {PERSISTENCE}")
            if self.demo_mode:
                logger.info(f"   Mode: DEMO (No real trading)")
            logger.info("=" * 70)
            
            # Wait for tasks
            await asyncio.gather(*self.tasks, return_exceptions=True)
            
        except asyncio.CancelledError:
            logger.info("System cancelled")
        except Exception as e:
            logger.critical(f"Critical error: {e}", exc_info=True)
        finally:
            await self.shutdown()
    
    # ===== DATA CALLBACKS =====
    
    async def _on_tick(self, tick: dict):
        """Callback: nuevo tick de precio"""
        self.market_data.add_tick(tick)
        
        # Chequear heartbeat
        if not self.kraken_ws.is_heartbeat_healthy():
            logger.critical("WebSocket heartbeat dead - EMERGENCY EXIT")
            await self.signal_engine.emergency_close_all()
            self.is_running = False
    
    async def _on_trade(self, trade: dict):
        """Callback: nuevo trade (para CVD real)"""
        self.market_data.add_trade(trade)
        
        # Log persistencia
        await self.trade_logger.log_event('trade', trade)
    
    async def _on_depth(self, depth: dict):
        """Callback: update order book"""
        side = depth.get('side')
        price = depth.get('price')
        qty = depth.get('qty')
        
        if side == 'bid':
            self.order_book.update_bid(price, qty)
        else:
            self.order_book.update_ask(price, qty)
    
    async def _on_candle_1m(self, candle: dict):
        """Callback: vela 1m cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active (for fallback detection)
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        logger.debug(f"1m candle closed: {candle['close']:.2f}")
    
    async def _on_candle_5m(self, candle: dict):
        """Callback: vela 5m cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        logger.info(f"5m candle closed: O={candle['open']:.2f} C={candle['close']:.2f}")
    
    async def _on_candle_1h(self, candle: dict):
        """Callback: vela 1h cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        self.ws_healthy = True
        logger.info(f"1h candle closed: O={candle['open']:.2f} C={candle['close']:.2f}")
    
    # ===== FALLBACK REST POLLING (si WebSocket falla) =====
    
    async def _rest_candle_polling_loop(self):
        """
        Fallback: Si WebSocket está muerto, hacer polling de velas cada minuto
        Esto GARANTIZA que el sistema siempre tenga datos actualizados
        """
        logger.info("Starting REST candle polling fallback loop...")
        
        last_1m_candles = {}  # tracking de last close timestamp por timeframe
        last_5m_candles = {}
        last_1h_candles = {}
        last_candle_from_ws = None  # Track última vela que llegó del WS
        
        while self.is_running:
            try:
                # Esperar un poco antes de chequear
                await asyncio.sleep(10)
                
                # Check if WebSocket received candles recently
                if hasattr(self, '_ws_last_candle_time') and self._ws_last_candle_time:
                    seconds_since_ws = (datetime.now() - self._ws_last_candle_time).total_seconds()
                    ws_is_healthy = seconds_since_ws < 120  # 2 minutos
                else:
                    ws_is_healthy = False
                
                if ws_is_healthy and not hasattr(self, '_logged_ws_active'):
                    logger.info("✓ WebSocket is active and delivering candles - REST fallback on standby")
                    self._logged_ws_active = True
                
                if not ws_is_healthy and hasattr(self, '_logged_ws_active'):
                    logger.warning("⚠ WebSocket inactive - REST fallback activated")
                    delattr(self, '_logged_ws_active')
                
                # Solo activar polling si WebSocket está muerto
                if not ws_is_healthy:
                    # Fetch 1m candles
                    try:
                        candles_1m = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '1m', limit=1)
                        if candles_1m:
                            c = candles_1m[0]
                            close_time = c.get('timestamp', 0)
                            
                            # Solo agregar si es una vela nueva
                            if close_time != last_1m_candles.get('timestamp'):
                                self.market_data.add_candle(c)
                                last_1m_candles = {'timestamp': close_time}
                                logger.debug(f"📊 REST 1m fallback: {c['close']:.2f}")
                    except Exception as e:
                        logger.error(f"Error in 1m polling: {e}")
                    
                    # Fetch 5m candles
                    try:
                        candles_5m = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '5m', limit=1)
                        if candles_5m:
                            c = candles_5m[0]
                            close_time = c.get('timestamp', 0)
                            
                            if close_time != last_5m_candles.get('timestamp'):
                                self.market_data.add_candle(c)
                                last_5m_candles = {'timestamp': close_time}
                                logger.debug(f"📊 REST 5m fallback: {c['close']:.2f}")
                    except Exception as e:
                        logger.error(f"Error in 5m polling: {e}")
                    
                    # Fetch 1h candles
                    try:
                        candles_1h = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '1h', limit=1)
                        if candles_1h:
                            c = candles_1h[0]
                            close_time = c.get('timestamp', 0)
                            
                            if close_time != last_1h_candles.get('timestamp'):
                                self.market_data.add_candle(c)
                                last_1h_candles = {'timestamp': close_time}
                                logger.debug(f"📊 REST 1h fallback: {c['close']:.2f}")
                    except Exception as e:
                        logger.error(f"Error in 1h polling: {e}")
                    
                    # Log data status
                    status = self.market_data.get_candle_completion_status()
                    if status['1m'] >= 20 and status['5m'] >= 50 and status['1h'] >= 50:
                        logger.info(f"✓ REST fallback: System has complete data ({status['1m']}m, {status['5m']}5m, {status['1h']}h)")
                
            except Exception as e:
                logger.error(f"Error in REST polling loop: {e}")
    
    # ===== MONITORING =====
    
    async def _monitoring_loop(self):
        """Monitoreo cada minuto"""
        while self.is_running:
            try:
                await asyncio.sleep(60)
                
                # Status
                logger.info("=" * 70)
                logger.info("📊 SYSTEM STATUS")
                
                # Signal engine
                if self.signal_engine:
                    status = self.signal_engine.get_status()
                    logger.info(f"Signal Engine: {status.get('state', 'unknown')}")
                
                # External data
                if self.external_data:
                    ext_status = self.external_data.get_status()
                    can_trade, reason = self.external_data.can_trade()
                    logger.info(f"Can Trade: {can_trade} ({reason})")
                    logger.info(f"Funding: {ext_status['funding']['current_rate']*100:.4f}%")
                
                # Reconciliation
                if self.reconciler:
                    recon = await self.reconciler.get_status()
                    logger.info(f"Last Reconcile: {recon['last_reconcile']}")
                
                logger.info("=" * 70)
                
            except Exception as e:
                logger.error(f"Error in monitoring: {e}")
    
    # ===== SHUTDOWN =====
    
    async def shutdown(self):
        """Apagado graceful"""
        if not self.is_running:
            return
        
        logger.info("=" * 70)
        logger.info("🛑 GRACEFUL SHUTDOWN")
        logger.info("=" * 70)
        
        self.is_running = False
        
        try:
            # 1. Stop signal engine (cierra posiciones)
            if self.signal_engine:
                logger.info("Stopping signal engine...")
                await self.signal_engine.stop()
            
            # 2. Stop external data monitors
            if self.external_data:
                logger.info("Stopping external data monitors...")
                await self.external_data.stop()
            
            # 3. Stop reconciliation
            if self.reconciler:
                logger.info("Stopping reconciliation...")
                await self.reconciler.stop()
            
            # 4. Cancel all orders
            if self.execution:
                logger.info("Cancelling all orders...")
                # TODO: implement in execution engine
            
            # 5. Stop and flush trade logger
            if self.trade_logger:
                logger.info("Stopping trade logger...")
                try:
                    stats = self.trade_logger.get_stats()
                    logger.info(f"Trade logger stats: trades={stats['trades_logged']}, events={stats['events_logged']}, dropped={stats['trades_dropped']+stats['events_dropped']}")
                except Exception:
                    pass
                await self.trade_logger.stop()
                logger.info("✓ Trade logger stopped and flushed")
            
            # 6. Close WebSocket
            if self.kraken_ws:
                logger.info("Closing WebSocket...")
                await self.kraken_ws.disconnect()
            
            # 7. Close execution
            if self.execution:
                logger.info("Closing execution engine...")
                await self.execution.close()
            
            # 8. Cancel remaining tasks
            logger.info("Cancelling remaining tasks...")
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("Some tasks did not stop gracefully")
            
            logger.info("=" * 70)
            logger.info("✅ SHUTDOWN COMPLETE")
            logger.info("=" * 70)
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)


async def main():
    """Main entry"""
    orchestrator = TradingSystemOrchestrator()
    
    try:
        await orchestrator.initialize()
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        await orchestrator.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
