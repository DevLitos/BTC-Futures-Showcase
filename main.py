"""
Main Entry Point for BTC Perpetual Trading System - PRODUCTION GRADE

PRODUCTION FEATURES (Jan 14, 2026):
- Centralized settings with pydantic-settings validation
- Structured logging with secret redaction
- Kill switch for hot trading pause
- Health endpoints (/healthz, /readyz, /health)
- Audit logging for critical events
- Prometheus metrics for observability

Prioridades de inicialización:
1. SETTINGS & LOGGING (validated configuration)
2. DATOS DE MERCADO (WebSocket Kraken)
3. BUFFER MULTI-TIMEFRAME sincronizado
4. EJECUCIÓN REAL (REST + CCXT)
5. DATOS EXTERNOS (Funding, Macro)
6. PERSISTENCIA (Parquet/PostgreSQL)
7. SIGNAL ENGINE (usa todo lo anterior)
"""

import asyncio
import logging
import os
import signal
from datetime import datetime, UTC
from pathlib import Path

# =====================================================================
# 🔒 PHASE 1: Load env file FIRST (before any imports that might use env vars)
# =====================================================================
def load_env_file():
    """Load environment variables from .env.secrets in project directory"""
    # Load from .env.secrets in project directory
    project_dir = Path(__file__).parent
    env_files = [
        project_dir / '.env.secrets',
        project_dir / '.env.config',
    ]
    
    for env_file in env_files:
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
                            
                            # Don't override existing env vars
                            if key.strip() not in os.environ:
                                os.environ[key.strip()] = value
            # Only load first found file
            break

# Load env vars before any other imports
load_env_file()

# =====================================================================
# 🔒 PHASE 2: Initialize production-grade settings and logging
# =====================================================================
from src.config.settings import get_settings, TradingMode, validate_startup, redact_secrets
from src.bootstrap.logging import configure_logging, get_audit_logger, set_session_id
from src.config.kill_switch import init_kill_switch, get_kill_switch
from src.config.health import init_health_status, get_health_status, start_health_server

# Get validated settings (will fail fast if invalid)
try:
    settings = get_settings()
except Exception as e:
    # Minimal logging for startup failure
    logging.basicConfig(level=logging.ERROR)
    logging.error(f"❌ FATAL: Configuration validation failed: {e}")
    raise SystemExit(1)

# Configure structured logging with secret redaction
configure_logging(settings)
logger = logging.getLogger('MainApp')

# Initialize kill switch
init_kill_switch(settings.kill_switch_file)

# Initialize health status
health_status = init_health_status(
    version=settings.app_version,
    git_commit=settings.git_commit,
    run_id=settings.run_id
)

# Extract commonly used settings
WS_SYMBOL = settings.ws_symbol
CCXT_SYMBOL = settings.ccxt_symbol
EXCHANGE = settings.exchange.value
DEMO_MODE = settings.demo_mode

# =====================================================================
# 🔒 PHASE 3: Import other modules (after logging is configured)
# =====================================================================

# 1. DATOS DE MERCADO
from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.ingest.websocket.order_book import OrderBookManager

# 2. BUFFER MULTI-TIMEFRAME
from src.application.services.market_data import SynchronizedMultiTimeframeBuffer

# 3. EJECUCIÓN REAL
from src.infrastructure.exchanges.kraken.execution import RESTExecutionEngine

# 4. DATOS EXTERNOS
from src.infrastructure.external_data import ExternalDataManager

# 5. PERSISTENCIA
from src.infrastructure.storage.parquet.persistence import TradeLogger, PositionReconciler

# 6. SIGNAL ENGINE
from src.application.live.trading_app import SignalEngine

# 7. ERROR TRACKING
from src.infrastructure.monitoring.error_tracker import ErrorTracker, ErrorTrackerConfig, ErrorSeverity, init_error_tracker

# 8. NEW ARCHITECTURE - Runtime Mode & Position Store
from src.bootstrap.runtime import RuntimeMode, init_runtime, get_runtime_config
from src.infrastructure.storage import get_position_store
from src.domain.events.event_bus import get_event_bus, EventType

# 9. METRICS
from src.infrastructure.monitoring.metrics import (
    start_metrics_server,
    BUILD_INFO,
    ORDERS_PLACED_TOTAL, ORDERS_FAILED_TOTAL,
    WS_RECONNECTS_TOTAL, WS_MESSAGES_TOTAL,
    HEALTH_WS_CONNECTED, HEALTH_EXCHANGE_REACHABLE, HEALTH_TIME_DRIFT_MS,
    KILL_SWITCH_ACTIVE
)

# 10. BTC CONTEXT FEED (Jan 15, 2026)
from src.domain.market.structure.btc_context_feed import get_btc_context_feed

# 11. HEALTH BRIDGE (Jan 20, 2026) - For FastAPI integration
from src.config.health_bridge import HealthBridge, HealthBridgeWriter, get_health_bridge

# Legacy (mantener compatibilidad)
from src.interfaces.output.output_manager import OutputManager

# Set build info metric
BUILD_INFO.info({
    'version': settings.app_version,
    'git_commit': settings.git_commit,
    'run_id': settings.run_id,
    'exchange': EXCHANGE
})
# NOTE: load_env_file() already called at line 62 - removed duplicate call here

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
        # Store settings reference
        self.settings = settings
        
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
        
        # 3. EJECUCIÓN - Use credentials from settings
        credentials = settings.get_credentials_dict()
        self.execution = RESTExecutionEngine(EXCHANGE, credentials)
        
        # 4. DATOS EXTERNOS
        self.external_data = ExternalDataManager(CCXT_SYMBOL, EXCHANGE)
        
        # 5. PERSISTENCIA
        self.trade_logger = TradeLogger(settings.persistence_backend, 
                                        settings.database_url.get_secret_value() if settings.database_url else None)
        self.reconciler = PositionReconciler(self.execution, symbol=CCXT_SYMBOL)
        
        # 5.5 OUTPUT MANAGER
        self.output_manager = OutputManager()
        
        # 5.6 ERROR TRACKER - 3 consecutive errors = stop + Telegram
        self.error_tracker = init_error_tracker(
            telegram_callback=self.output_manager.send_critical_alert,
            config=ErrorTrackerConfig(
                max_consecutive_errors=3,
                error_window_seconds=300,
                notify_on_stop=True
            )
        )

        # 6. SIGNAL ENGINE
        self.signal_engine = SignalEngine(market_data=self.market_data,
                                         execution=self.execution,
                                         external_data=self.external_data,
                                         trade_logger=self.trade_logger,
                                         output_manager=self.output_manager,
                                         order_book=self.order_book)
        
        # 6.1 Connect reconciler to signal engine (CRITICAL for position state sync)
        self.reconciler.set_signal_engine(self.signal_engine)
        
        # 7. HEALTH STATUS - Update with signal engine reference
        self.health_status = health_status
        
        # 8. HEALTH BRIDGE WRITER - Export health status for FastAPI
        self.health_bridge_writer = HealthBridgeWriter(
            health_status=self.health_status,
            interval_seconds=5.0,
            bridge=get_health_bridge()
        )
        
        self.tasks = []
        self.demo_mode = DEMO_MODE
        self.is_running = False
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        # Ignore SIGPIPE - prevents BrokenPipeError when piping to head/grep
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except AttributeError:
            pass  # SIGPIPE not available on Windows
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully, ignoring BrokenPipeError."""
        # Set flag FIRST - this stops the main loop
        self.is_running = False
        
        # Helper to safely log (ignore BrokenPipeError when piping to head/grep)
        def safe_log(level: str, msg: str):
            try:
                getattr(logger, level)(msg)
            except (BrokenPipeError, OSError):
                pass  # Ignore pipe errors during shutdown
        
        safe_log("info", f"Received signal {signum} - initiating graceful shutdown...")
        
        # 🔥 FIX: On second signal, force exit immediately
        if hasattr(self, '_shutdown_requested') and self._shutdown_requested:
            safe_log("critical", "🛑 FORCE EXIT - Second signal received")
            import os
            os._exit(1)
        
        self._shutdown_requested = True
        
        # Schedule shutdown task
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.shutdown()))
        except RuntimeError:
            # No running loop - just exit
            safe_log("info", "No event loop running - forcing exit")
            import os
            os._exit(0)
    
    async def _telegram_shutdown(self):
        """
        Shutdown callback for Telegram /force_stop command.
        
        This performs a clean shutdown WITHOUT closing positions.
        Positions remain on exchange with their SL/TP orders.
        """
        logger.critical("🛑 TELEGRAM /force_stop - Initiating full shutdown (positions NOT closed)")
        self.is_running = False
        self._shutdown_requested = True
        
        # Trigger shutdown with preserve_orders=True to keep SL/TP
        await self.shutdown(preserve_orders=True)
    
    async def _load_historical_data(self):
        """Load historical candles to bootstrap the system"""
        logger.info("\n📊 Loading historical data...")
        
        # 🔥 AUDIT FIX: Verify execution engine is initialized
        if not self.execution or not self.execution.exchange:
            logger.error("Cannot load historical data: execution engine not initialized")
            return
        
        try:
            # Load 1h candles (need 480+ for LocationFilter 20-day range)
            # 500 bars = ~21 days of hourly data
            candles_1h = await self.execution.fetch_ohlcv(CCXT_SYMBOL, '1h', limit=500)
            if candles_1h:
                self.market_data.load_historical_candles(candles_1h, '1h')
                logger.info(f"✓ Loaded {len(candles_1h)} 1h candles (LocationFilter needs 480)")
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
            
            logger.info("✅ Historical data loaded successfully (REST warmup)")
            logger.info("🔄 Warmup complete – switching to WS only for live data")
            
        except Exception as e:
            logger.error(f"Error loading historical data: {e}", exc_info=True)

    async def _startup_recovery_check(self):
        """
        FIX #2 (Jan 13, 2026): Complete startup recovery after crash.
        
        Checks:
        1. Open positions on exchange → create local tracking or close
        2. Pending orders → cancel stale orders
        3. Sync PnL state for the day
        4. Notify user of recovery actions
        """
        from datetime import datetime, UTC
        from src.domain.risk.risk_engine import Position
        
        logger.info("🔄 STARTUP RECOVERY: Checking for orphan positions/orders...")
        
        recovery_actions = []
        
        # Step 1: Check for open positions
        try:
            exchange_pos = await self.execution.get_position(CCXT_SYMBOL)
            
            # CRITICAL: Check for API error (not "no position")
            if exchange_pos and exchange_pos.get('_error'):
                logger.warning(f"⚠️ Cannot verify positions at startup (API error): {exchange_pos.get('_error')}")
                # Don't proceed with recovery - we don't know the true state
                recovery_actions.append("WARNING: Could not verify position state (API unavailable)")
                exchange_pos = None  # Treat as unknown
            
            if exchange_pos:
                size = abs(float(exchange_pos.get('contracts', 0) or 0))
                side = exchange_pos.get('side', '').lower()
                entry_price = float(exchange_pos.get('entryPrice', 0) or exchange_pos.get('info', {}).get('avgEntryPrice', 0) or 0)
                
                if size > 0.00001:
                    logger.warning(f"⚠️ EXISTING POSITION DETECTED: {side} {size:.6f} @ {entry_price:.2f}")
                    recovery_actions.append(f"Found existing {side} position: {size:.6f}")
                    
                    # =========================================================================
                    # DECISION: ADOPT position (don't close!)
                    # Closing on restart is WRONG - we lose money for no reason
                    # The position is legitimate, just restart the tracking
                    # =========================================================================
                    CLOSE_ORPHAN_POSITIONS = False  # NEVER close legitimate positions!
                    
                    if CLOSE_ORPHAN_POSITIONS:
                        logger.warning("🔄 CLOSING ORPHAN POSITION (safety first)...")
                        try:
                            close_side = 'sell' if side == 'long' else 'buy'
                            await self.execution.close_position_market(
                                symbol=CCXT_SYMBOL,
                                side=close_side,
                                amount=size,
                                reason="ORPHAN_CLEANUP"
                            )
                            logger.info(f"✅ Orphan position closed: {size:.6f} {side}")
                            recovery_actions.append(f"Closed orphan position: {size:.6f} {side}")
                            
                            # Send notification
                            if self.output_manager:
                                await self.output_manager.send_critical_alert(
                                    f"🔄 STARTUP RECOVERY: Closed orphan {side} position of {size:.6f} BTC"
                                )
                        except Exception as e:
                            logger.critical(f"🔴 FAILED TO CLOSE ORPHAN POSITION: {e}")
                            recovery_actions.append(f"FAILED to close orphan: {e}")
                            raise RuntimeError(f"Cannot recover: orphan position close failed: {e}")
                    else:
                        # Adopt the position into risk engine
                        logger.info("🔄 ADOPTING EXISTING POSITION into risk engine...")
                        # Create Position object for tracking
                        direction = 'LONG' if side == 'long' else 'SHORT'
                        
                        # Check for existing SL/TP orders on exchange
                        existing_sl = None
                        existing_tp = None
                        try:
                            open_orders = await self.execution.get_open_orders(CCXT_SYMBOL)
                            for order in open_orders:
                                order_type = str(order.get('type', '')).lower()
                                reduce_only = order.get('reduceOnly') or order.get('info', {}).get('reduceOnly', False)
                                
                                # Extract price from multiple possible fields
                                order_price = float(order.get('price', 0) or order.get('triggerPrice', 0) or order.get('stopPrice', 0) or 0)
                                
                                # Skip orders with zero or invalid price
                                if order_price <= 0:
                                    continue
                                
                                # 🔥 IMPROVED SL/TP detection: Use order type AND price position
                                is_stop_type = order_type in ('stop-loss', 'stop', 'stop_loss', 'stop_market')
                                is_limit_type = order_type in ('take-profit', 'take_profit', 'limit')
                                
                                if reduce_only or is_stop_type:
                                    # Determine if this is SL or TP based on price position
                                    # SL for LONG: below entry | SL for SHORT: above entry
                                    # TP for LONG: above entry | TP for SHORT: below entry
                                    if direction == 'LONG':
                                        if order_price < entry_price:
                                            # Price below entry = SL for LONG
                                            if is_stop_type or existing_sl is None:
                                                existing_sl = order_price
                                                logger.info(f"   Found existing SL order: {existing_sl:.2f} (type={order_type})")
                                        else:
                                            # Price above entry = TP for LONG
                                            if is_limit_type or existing_tp is None:
                                                existing_tp = order_price
                                                logger.info(f"   Found existing TP order: {existing_tp:.2f} (type={order_type})")
                                    else:  # SHORT
                                        if order_price > entry_price:
                                            # Price above entry = SL for SHORT
                                            if is_stop_type or existing_sl is None:
                                                existing_sl = order_price
                                                logger.info(f"   Found existing SL order: {existing_sl:.2f} (type={order_type})")
                                        else:
                                            # Price below entry = TP for SHORT
                                            if is_limit_type or existing_tp is None:
                                                existing_tp = order_price
                                                logger.info(f"   Found existing TP order: {existing_tp:.2f} (type={order_type})")
                        except Exception as e:
                            logger.warning(f"   Could not check for existing SL/TP: {e}")
                        
                        # Use existing SL or conservative estimate
                        if existing_sl and existing_sl > 0:
                            sl_to_use = existing_sl
                            sl_active = True
                        else:
                            # Conservative SL estimate (1% away)
                            sl_to_use = entry_price * 0.99 if direction == 'LONG' else entry_price * 1.01
                            sl_active = False
                            logger.warning(f"   ⚠️ No SL found - using estimate: {sl_to_use:.2f}")
                        
                        self.signal_engine.risk_engine.position = Position(
                            symbol=CCXT_SYMBOL,
                            direction=direction,
                            entry_price=entry_price,
                            size=size,
                            stop_loss=sl_to_use,
                            atr_1m=100.0,  # Conservative estimate
                            market_time=datetime.now(UTC)
                        )
                        
                        # =========================================================================
                        # 🔒 CRITICAL FIX (Jan 26, 2026): Mark as ADOPTED position
                        # This prevents WORST_TRADE_PREVENTION from closing it immediately
                        # if it's already in loss when we restart.
                        # We don't know the original thesis for adopted positions!
                        # =========================================================================
                        self.signal_engine.risk_engine.position.is_adopted = True
                        logger.info("   🏷️ Position marked as ADOPTED (WORST_TRADE_PREVENTION disabled)")
                        
                        # Mark SL status
                        if sl_active:
                            self.signal_engine.risk_engine.position.exchange_sl_active = True
                            logger.info(f"   ✅ Exchange SL is ACTIVE at {sl_to_use:.2f}")
                        else:
                            self.signal_engine.risk_engine.position.exchange_sl_active = False
                            logger.warning(f"   ⚠️ No exchange SL - will need to set one!")
                        
                        # Track TP if exists
                        if existing_tp and existing_tp > 0:
                            self.signal_engine.risk_engine.position.take_profit = existing_tp
                            logger.info(f"   ✅ TP tracked at {existing_tp:.2f}")
                        
                        # Transition signal engine to MANAGE state
                        from src.domain.trading.state.state_machine import BotState
                        if hasattr(self.signal_engine, 'state_machine'):
                            self.signal_engine.state_machine.transition_to(BotState.MANAGE, "Adopted existing position")
                            self.signal_engine._stops_set = sl_active  # Track if SL needs to be set
                            
                            # 🔥 CRITICAL FIX: Populate _last_entry_data for SL/TP retry logic
                            # Calculate proper SL/TP based on position
                            atr_estimate = entry_price * 0.01  # 1% as fallback ATR
                            if direction == 'LONG':
                                retry_sl = sl_to_use if sl_to_use else entry_price * 0.97  # 3% SL
                                retry_tp = entry_price * 1.02  # 2% TP
                            else:
                                retry_sl = sl_to_use if sl_to_use else entry_price * 1.03  # 3% SL
                                retry_tp = entry_price * 0.98  # 2% TP
                            
                            self.signal_engine._last_entry_data = {
                                'stop_loss': retry_sl,
                                'take_profit': retry_tp,
                                'entry_price': entry_price,
                                'direction': direction,
                                'adopted': True
                            }
                            logger.info(f"   📋 Retry data set: SL={retry_sl:.2f}, TP={retry_tp:.2f}")
                            
                            # 🔥 IMMEDIATE SL/TP PLACEMENT: Don't wait for main loop if no protection!
                            if not sl_active:
                                logger.warning("🔒 IMMEDIATE SL/TP PLACEMENT: Position has no protection!")
                                try:
                                    stops_placed = await self.execution.set_position_stops(
                                        symbol=CCXT_SYMBOL,
                                        stop_loss=retry_sl,
                                        take_profit=retry_tp
                                    )
                                    if stops_placed:
                                        logger.info(f"   ✅ SL/TP placed immediately: SL={retry_sl:.2f}, TP={retry_tp:.2f}")
                                        self.signal_engine._stops_set = True
                                        self.signal_engine.risk_engine.position.exchange_sl_active = True
                                    else:
                                        logger.critical("   ❌ FAILED to place SL/TP - position at risk!")
                                except Exception as sl_error:
                                    logger.critical(f"   ❌ Error placing SL/TP: {sl_error}")
                        
                        logger.info(f"✅ Position adopted: {direction} {size:.6f} @ {entry_price:.2f}")
                        recovery_actions.append(f"Adopted existing position: {direction} {size:.6f}")
                else:
                    logger.info("✓ No open position on exchange")
            else:
                logger.info("✓ No open position on exchange")
                
                # Clear any stale local state
                if self.signal_engine.risk_engine.position is not None:
                    logger.warning("   → Clearing stale local position state")
                    self.signal_engine.risk_engine.position = None
                    recovery_actions.append("Cleared stale local position state")
                    
        except Exception as e:
            logger.error(f"Error checking positions: {e}")
            raise
        
        # Step 2: Cancel stale pending orders (BUT NOT SL/TP if position exists!)
        try:
            open_orders = await self.execution.get_open_orders(CCXT_SYMBOL)
            has_position = self.signal_engine.risk_engine.position is not None
            
            if open_orders and len(open_orders) > 0:
                stale_count = 0
                protective_count = 0
                
                for order in open_orders:
                    order_id = order.get('id', 'unknown')
                    order_type = str(order.get('type', 'unknown')).lower()
                    order_side = order.get('side', 'unknown')
                    order_amount = order.get('amount', 0)
                    reduce_only = order.get('reduceOnly', order.get('info', {}).get('reduceOnly', False))
                    
                    # Check if this is a protective order (SL/TP)
                    is_protective = order_type in ('stop-loss', 'stop', 'take-profit', 'trailing-stop') or reduce_only
                    
                    if has_position and is_protective:
                        # DON'T cancel protective orders if we have a position!
                        logger.info(f"   ✅ Keeping protective order: {order_id} ({order_type})")
                        protective_count += 1
                    else:
                        # Cancel non-protective or orphan orders
                        logger.warning(f"   Cancelling stale order: {order_id} ({order_type} {order_side} {order_amount})")
                        try:
                            await self.execution.cancel_order(order_id, CCXT_SYMBOL)
                            recovery_actions.append(f"Cancelled stale order {order_id}")
                            stale_count += 1
                        except Exception as e:
                            logger.error(f"   Failed to cancel order {order_id}: {e}")
                
                if stale_count > 0:
                    logger.info(f"✅ Cancelled {stale_count} stale orders")
                    recovery_actions.append(f"Found {stale_count} stale orders")
                if protective_count > 0:
                    logger.info(f"✅ Kept {protective_count} protective orders (SL/TP)")
            else:
                logger.info("✓ No pending orders on exchange")
                
        except Exception as e:
            logger.error(f"Error checking orders: {e}")
        
        # Step 3: Sync position store (source of truth)
        try:
            position_store = get_position_store()
            sync_result = await position_store.sync_from_exchange(self.execution.exchange)
            
            if sync_result.get('discrepancies'):
                for d in sync_result['discrepancies']:
                    logger.warning(f"⚠️ Position discrepancy: {d}")
                    recovery_actions.append(f"Discrepancy: {d}")
            
            logger.info(f"✓ Position store synced: {sync_result.get('positions_synced', 0)} positions")
        except Exception as e:
            logger.error(f"Position store sync failed: {e}")
        
        # Step 4: Log recovery summary
        if recovery_actions:
            logger.info("=" * 50)
            logger.info("🔄 STARTUP RECOVERY SUMMARY:")
            for action in recovery_actions:
                logger.info(f"   • {action}")
            logger.info("=" * 50)
            
            # Only send critical alert if something BAD happened (closed position, errors)
            # Adopting an existing position is NORMAL, not an emergency
            has_critical_action = any(
                'FAILED' in a or 'Closed' in a or 'error' in a.lower() 
                for a in recovery_actions
            )
            
            if has_critical_action and self.output_manager:
                summary = "\\n".join([f"• {a}" for a in recovery_actions])
                await self.output_manager.send_critical_alert(
                    f"🔄 STARTUP RECOVERY COMPLETED:\\n{summary}"
                )
            else:
                # Normal recovery (adopted position) - just log, no telegram spam
                logger.info("✓ Recovery actions were routine - no critical alert needed")
        else:
            logger.info("✓ No recovery actions needed - clean startup")

    async def initialize(self):
        """
        Inicializar en el orden correcto
        """
        logger.info("=" * 70)
        logger.info("🚀 BTC PERPETUAL TRADING SYSTEM - INITIALIZATION (PRODUCTION GRADE)")
        logger.info("=" * 70)
        
        # Start health server for Docker healthchecks (uses new health module)
        start_health_server(self.health_status, port=settings.health_port)
        
        # Start Prometheus metrics server
        start_metrics_server(port=settings.metrics_port)
        
        if self.demo_mode:
            logger.warning("🔧 DEMO MODE - Running without API keys")
        
        # Log kill switch status
        kill_switch = get_kill_switch()
        if kill_switch.is_active:
            logger.warning(f"⚠️ KILL SWITCH ACTIVE: {kill_switch.reason}")
            KILL_SWITCH_ACTIVE.set(1)
        else:
            KILL_SWITCH_ACTIVE.set(0)
        
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
                
                # Update health status
                if self.kraken_ws.is_connected:
                    self.health_status.set_ws_connected(True)
                    HEALTH_WS_CONNECTED.labels(exchange=EXCHANGE).set(1)
            else:
                logger.info("\n[1/7] WebSocket disabled - using REST polling only")
            
            # 2. Buffer multi-timeframe (ya inicializado)
            logger.info("[2/7] Market data buffer initialized")
            
            # 3. Order book (ya inicializado)
            logger.info("[3/7] Order book manager initialized")
            
            # 4. Execution engine (REST)
            logger.info("[4/7] Initializing REST execution engine...")
            await self.execution.initialize()
            self.health_status.set_exchange_reachable(True)
            HEALTH_EXCHANGE_REACHABLE.labels(exchange=EXCHANGE).set(1)
            
            # 4.5. Load historical data to bootstrap
            if not self.demo_mode:
                await self._load_historical_data()
            else:
                logger.info("📊 Skipping historical data load (DEMO MODE)")
            
            # 5. External data (Funding, Macro)
            logger.info("[5/7] Initializing external data monitors...")
            self.tasks.append(asyncio.create_task(self.external_data.start(), name="ExternalData"))
            
            # 5.1 BTC Context Feed for correlation (Jan 15, 2026)
            logger.info("[5.1/7] Starting BTC context feed for correlation...")
            self.btc_context_feed = get_btc_context_feed(
                exchange=EXCHANGE,
                symbol=CCXT_SYMBOL,
                enabled=True
            )
            self.tasks.append(asyncio.create_task(self.btc_context_feed.start(), name="BTCContextFeed"))
            
            # FALLBACK: Start REST polling for candles (safety net si WS muere)
            logger.info("[5.5/7] Starting REST candle polling fallback...")
            self.tasks.append(asyncio.create_task(self._rest_candle_polling_loop(), name="CandlePolling"))
            
            # 6. Persistencia
            logger.info("[6/7] Initializing persistence logger...")
            await self.trade_logger.start()
            logger.info("✓ Persistence logger started (backpressure queues active)")
            
            # 6.5. POSITION RECOVERY AT STARTUP - Critical for crash recovery
            logger.info("[6.5/7] 🔄 STARTUP RECOVERY: Checking exchange state...")
            try:
                await self._startup_recovery_check()
            except Exception as e:
                logger.error(f"Startup recovery failed: {e}")
                runtime_config = get_runtime_config()
                if runtime_config.mode == RuntimeMode.LIVE:
                    logger.critical("🚨 CRITICAL: Cannot recover state in LIVE mode - stopping")
                    raise RuntimeError(f"Startup recovery required in LIVE mode: {e}")
            
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
            
            # Mark as healthy for Docker (using new health module)
            self.health_status.set_healthy(True, "initialized")
            
        except Exception as e:
            self.health_status.set_healthy(False, f"init_error: {str(e)[:100]}")
            HEALTH_EXCHANGE_REACHABLE.labels(exchange=EXCHANGE).set(0)
            if self.demo_mode:
                logger.warning(f"Initialization warning (DEMO MODE): {e}")
                # Continue in demo mode
            else:
                logger.critical(f"Failed to initialize system: {e}", exc_info=True)
                raise
    
    async def start(self):
        """Start trading"""
        logger.info("")
        logger.info("=" * 70)
        logger.info("                    FIDEI RATIO CAPITAL BTC")
        logger.info("=" * 70)
        logger.info("")
        logger.info("                               ║║║║║ ")
        logger.info("                               ║║║║║  ")
        logger.info("                               ║║║║║   ")
        logger.info("                               ║║║║║    ")
        logger.info("                               ║║║║║     ")
        logger.info("        ═══════════════════════╬╬╬╬╬═══════════════════════")
        logger.info("        ═══════════════════════╬╬╬╬╬═══════════════════════ ")
        logger.info("                               ║║║║║      ")
        logger.info("                               ║║║║║       ")
        logger.info("                               ║║║║║        ")
        logger.info("                               ║║║║║         ")
        logger.info("                               ║║║║║          ")
        logger.info("                               ║║║║║           ")
        logger.info("                               ║║║║║            ")
        logger.info("                               ║║║║║             ")
        logger.info("                               ║║║║║              ")
        logger.info("                               ║║║║║               ")
        logger.info("                               ║║║║║                ")
        logger.info("                               ║║║║║                 ")
        logger.info("                               ║║║║║                  ")
        logger.info("                               ║║║║║                   ")
        logger.info("                               ║║║║║                    ")
        logger.info("")
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
            
            # Metrics server already started in initialize()
            
            # Start kill switch monitor task
            self.tasks.append(asyncio.create_task(self._kill_switch_monitor(), name="KillSwitchMonitor"))
            
            # Event loop heartbeat (logs every minute to confirm system is alive)
            async def _event_loop_heartbeat():
                """Print heartbeat every minute to confirm event loop is alive"""
                count = 0
                while True:
                    count += 1
                    if count % 60 == 0:
                        all_tasks = [t for t in asyncio.all_tasks() if not t.done()]
                        logger.debug(f"💓 EVENT_LOOP_HEARTBEAT #{count//60}min | Active tasks: {len(all_tasks)}")
                    await asyncio.sleep(1.0)
            self.tasks.append(asyncio.create_task(_event_loop_heartbeat(), name="EventLoopHeartbeat"))
            
            # Start health bridge writer (for FastAPI integration)
            # Uses run_in_executor for non-blocking file I/O
            self.health_bridge_writer.start()
            logger.info("✓ Health bridge writer started (FastAPI can read health_status.json)")
            
            # Start Telegram command listener (/stop, /force_stop)
            if self.output_manager:
                self.output_manager.set_force_exit_callback(self.signal_engine.emergency_close_all)
                self.output_manager.set_shutdown_callback(self._telegram_shutdown)
                self.tasks.append(asyncio.create_task(self.output_manager.start_command_listener(), name="TelegramCommands"))
            
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
        if self.kraken_ws is not None and not self.kraken_ws.is_heartbeat_healthy():
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
        if self.order_book is None:
            return
        side = depth.get('side')
        price = depth.get('price')
        qty = depth.get('qty')
        
        if price is not None and qty is not None:
            if side == 'bid':
                self.order_book.update_bid(float(price), float(qty))
            else:
                self.order_book.update_ask(float(price), float(qty))
    
    async def _on_candle_1m(self, candle: dict):
        """Callback: vela 1m cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active (for fallback detection)
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        
        # 🔥 CRITICAL FIX: Increment candle count for open positions
        # This is required for TIME_EXIT and RUNNER time exit to work in LIVE
        # Previously only called in backtest - BUG since Jan 2026
        if (hasattr(self, 'signal_engine') and 
            self.signal_engine.risk_engine.position is not None):
            self.signal_engine.risk_engine.position.increment_candle_count()
        
        logger.debug(f"1m candle closed: {candle['close']:.2f}")
    
    async def _on_candle_5m(self, candle: dict):
        """Callback: vela 5m cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        # FIX #84: Reducido a DEBUG
        logger.debug(f"5m candle closed: O={candle['open']:.2f} C={candle['close']:.2f}")
    
    async def _on_candle_1h(self, candle: dict):
        """Callback: vela 1h cerrada"""
        self.market_data.add_candle(candle)
        # Mark WebSocket as active
        if not hasattr(self, '_ws_last_candle_time'):
            self._ws_last_candle_time = datetime.now()
        self._ws_last_candle_time = datetime.now()
        self.ws_healthy = True
        # FIX #84: Reducido a DEBUG
        logger.debug(f"1h candle closed: O={candle['open']:.2f} C={candle['close']:.2f}")
    
    # ===== KILL SWITCH MONITOR =====
    
    async def _kill_switch_monitor(self):
        """
        Background task to monitor kill switch state.
        Checks every 5 seconds for env var or file flag changes.
        """
        kill_switch = get_kill_switch()
        audit_logger = get_audit_logger()
        
        while self.is_running:
            try:
                await asyncio.sleep(5)
                
                # Check for changes (is_active checks file flag and env var internally)
                was_active = getattr(self, '_kill_switch_was_active', False)
                is_now_active = kill_switch.is_active
                
                # Update metrics
                KILL_SWITCH_ACTIVE.set(1 if is_now_active else 0)
                
                # If just activated, log and notify
                if is_now_active and not was_active:
                    logger.warning(f"⚠️ KILL SWITCH ACTIVATED: {kill_switch.reason}")
                    audit_logger.kill_switch_activated(reason=kill_switch.reason)
                    
                    # Pause signal engine if it has a pause method
                    if hasattr(self.signal_engine, 'pause'):
                        self.signal_engine.pause()  # pyright: ignore[reportAttributeAccessIssue]
                
                # Track state for next iteration
                self._kill_switch_was_active = is_now_active
                    
                if not is_now_active and was_active:
                    logger.info("✅ KILL SWITCH DEACTIVATED - resuming trading")
                    
                    # Resume signal engine if it has a resume method
                    if hasattr(self.signal_engine, 'resume'):
                        self.signal_engine.resume()  # pyright: ignore[reportAttributeAccessIssue]
                        
            except Exception as e:
                logger.error(f"Error in kill switch monitor: {e}")
                await asyncio.sleep(30)  # Back off on error
    
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
        """Monitoreo cada 15 minutos (FIX #84: reducido para menos spam)"""
        while self.is_running:
            try:
                await asyncio.sleep(900)  # FIX #84: 15 minutes instead of 5
                
                # Status (compact single line)
                logger.info("=" * 70)
                logger.info("📊 SYSTEM STATUS (5min report)")
                
                # Signal engine
                extra_health_data = {}
                if self.signal_engine:
                    status = self.signal_engine.get_status()
                    logger.info(f"Signal Engine: {status.get('state', 'unknown')}")
                    
                    # Collect data for health bridge
                    extra_health_data['current_regime'] = status.get('regime', 'UNKNOWN')
                    
                    # Log health monitor summary
                    if status.get('health'):
                        health = status['health']
                        extra_health_data['trades_today'] = health.get('trades_today', 0)
                        extra_health_data['win_rate_today'] = health.get('win_rate', 0.0)
                        
                        logger.info(f"Health: trades_today={health.get('trades_today', 0)}, "
                                   f"fill_rate={health.get('fill_rate', 1.0):.0%}, "
                                   f"integrity_errors={health.get('integrity_errors', 0)}, "
                                   f"avg_latency={health.get('avg_latency_ms', 0):.0f}ms")
                    if status.get('health_alert'):
                        logger.warning(f"⚠️ Health Alert: {status['health_alert']}")
                    
                    # Position info for health bridge
                    logger.debug("📊 [MON] About to check position...")
                    if self.signal_engine.risk_engine.position:
                        pos = self.signal_engine.risk_engine.position
                        logger.debug("📊 [MON] Position found, calling set_position...")
                        self.health_status.set_position(
                            symbol=pos.symbol,
                            side=pos.direction,
                            size=pos.size
                        )
                        logger.debug("📊 [MON] set_position done")
                        extra_health_data['position_entry_price'] = pos.entry_price
                        extra_health_data['position_unrealized_pnl'] = getattr(pos, 'unrealized_pnl', 0.0)
                    else:
                        self.health_status.clear_position()
                
                # Update health bridge with extra data
                logger.debug("📊 [MON] About to update health_bridge_writer...")
                if hasattr(self, 'health_bridge_writer'):
                    self.health_bridge_writer.update_extra_data(extra_health_data)
                logger.debug("📊 [MON] health_bridge_writer done")
                
                # 🔥 NEW (Jan 28, 2026): Balance info in 5min report
                try:
                    if self.signal_engine and self.signal_engine.risk_engine:
                        current_capital = self.signal_engine.risk_engine.capital
                        
                        # Track session start capital (first report sets baseline)
                        if not hasattr(self, '_session_start_capital'):
                            self._session_start_capital = current_capital
                        
                        session_pnl = current_capital - self._session_start_capital
                        session_pnl_pct = (session_pnl / self._session_start_capital * 100) if self._session_start_capital > 0 else 0
                        
                        pnl_emoji = "📈" if session_pnl >= 0 else "📉"
                        logger.info(
                            f"💰 Balance: ${current_capital:.2f} | "
                            f"Session: {pnl_emoji} ${session_pnl:+.2f} ({session_pnl_pct:+.2f}%)"
                        )
                except Exception as e:
                    logger.debug(f"Balance info error: {e}")
                
                # External data
                logger.debug("📊 [MON] About to get external_data status...")
                if self.external_data:
                    ext_status = self.external_data.get_status()
                    can_trade, reason = self.external_data.can_trade()
                    
                    # Check exchange availability (WebSocket + REST)
                    exchange_available = True
                    exchange_issues = []
                    
                    if hasattr(self.market_data, 'get_availability_status'):
                        ws_status = self.market_data.get_availability_status()  # pyright: ignore[reportAttributeAccessIssue]
                        if not ws_status.get('is_available', True):
                            exchange_available = False
                            exchange_issues.append("WS down")
                    
                    if hasattr(self.execution, 'is_exchange_available'):
                        if not self.execution.is_exchange_available:
                            exchange_available = False
                            exchange_issues.append("REST down")
                    
                    if not exchange_available:
                        can_trade = False
                        reason = f"Exchange unavailable ({', '.join(exchange_issues)})"
                    
                    logger.info(f"Can Trade: {can_trade} ({reason})")
                    logger.info(f"Funding: {ext_status['funding']['current_rate']*100:.4f}%")
                
                # Reconciliation
                logger.debug("📊 [MON] About to get reconciler status...")
                if self.reconciler:
                    recon = await self.reconciler.get_status()
                    logger.info(f"Last Reconcile: {recon['last_reconcile']}")
                logger.debug("📊 [MON] reconciler done")
                
                # Position Store sync (NEW - source of truth)
                logger.debug("📊 [MON] About to sync position store...")
                try:
                    runtime_config = get_runtime_config()
                    if runtime_config.mode == RuntimeMode.LIVE:
                        position_store = get_position_store()
                        logger.debug("📊 [MON] Calling sync_from_exchange...")
                        sync_result = await asyncio.wait_for(
                            position_store.sync_from_exchange(self.execution.exchange),
                            timeout=15.0
                        )
                        logger.debug("📊 [MON] sync_from_exchange done")
                        
                        if sync_result.get('discrepancies'):
                            for d in sync_result['discrepancies']:
                                logger.warning(f"⚠️ Position mismatch detected: {d}")
                        else:
                            pos = position_store.get_position(CCXT_SYMBOL)
                            pos_str = f"{pos.side} {pos.size:.6f}" if pos and not pos.is_flat else "FLAT"
                            logger.info(f"Position (exchange truth): {pos_str}")
                except asyncio.TimeoutError:
                    logger.error("⏰ Position store sync TIMEOUT (15s)")
                except Exception as e:
                    logger.error(f"Position sync error in monitoring: {e}")
                
                logger.info("=" * 70)
                
            except Exception as e:
                logger.error(f"Error in monitoring: {e}")
    
    # ===== SHUTDOWN =====
    
    async def shutdown(self, preserve_orders: bool = False):
        """
        Apagado graceful.
        
        Args:
            preserve_orders: If True, don't cancel SL/TP orders on exchange.
                           Used by /force_stop to keep protective orders active.
        """
        if not self.is_running:
            return
        
        logger.info("=" * 70)
        if preserve_orders:
            logger.info("🛑 GRACEFUL SHUTDOWN (preserving SL/TP orders)")
        else:
            logger.info("🛑 GRACEFUL SHUTDOWN")
        logger.info("=" * 70)
        
        self.is_running = False
        
        # Mark as shutting down
        self.health_status.set_healthy(False, "shutting_down")
        
        # Stop health bridge writer
        if hasattr(self, 'health_bridge_writer'):
            self.health_bridge_writer.stop()
        
        try:
            # 1. Stop signal engine (cierra posiciones)
            if self.signal_engine:
                logger.info("Stopping signal engine...")
                await self.signal_engine.stop()
            
            # 2. Stop external data monitors
            if self.external_data:
                logger.info("Stopping external data monitors...")
                await self.external_data.stop()
            
            # 2.1 Stop BTC context feed
            if hasattr(self, 'btc_context_feed') and self.btc_context_feed:
                logger.info("Stopping BTC context feed...")
                await self.btc_context_feed.stop()
            
            # 2.2 Stop Telegram command listener
            if self.output_manager:
                await self.output_manager.stop_command_listener()
            
            # 3. Stop reconciliation
            if self.reconciler:
                logger.info("Stopping reconciliation...")
                await self.reconciler.stop()
            
            # 4. Cancel all orders - CRITICAL for clean shutdown (unless preserve_orders)
            if self.execution and not preserve_orders:
                logger.info("Cancelling all open orders...")
                try:
                    cancelled = await self.execution.cancel_all_orders(CCXT_SYMBOL)
                    logger.info(f"✓ Cancelled {cancelled} open orders")
                    
                    # CRITICAL FIX: Verify cancellation was successful
                    await asyncio.sleep(1.0)  # Give exchange time to process
                    remaining = await self.execution.get_open_orders(CCXT_SYMBOL)
                    if remaining and len(remaining) > 0:
                        logger.critical(f"⚠️ {len(remaining)} orders still active after shutdown cancel!")
                        for order in remaining[:5]:  # Log first 5
                            logger.critical(f"   Orphan: {order.get('id')} {order.get('type')} {order.get('side')}")
                        # Try one more time
                        await self.execution.cancel_all_orders(CCXT_SYMBOL)
                except Exception as e:
                    logger.error(f"Error cancelling orders on shutdown: {e}")
            elif preserve_orders:
                logger.info("⚠️ Preserving SL/TP orders on exchange (force_stop mode)")
            
            # 5. Stop and flush trade logger
            if self.trade_logger:
                logger.info("Stopping trade logger...")
                try:
                    stats = self.trade_logger.get_stats()
                    logger.info(f"Trade logger stats: trades={stats['trades_logged']}, events={stats['events_logged']}, dropped={stats['trades_dropped']+stats['events_dropped']}")
                except (KeyError, AttributeError) as e:
                    logger.debug(f"Could not get trade logger stats: {e}")
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
    
    # =========================================================================
    # 🔒 PRODUCTION: Configure logging FIRST with structured output
    # =========================================================================
    configure_logging(settings)
    
    # Set a unique session ID for this run (use run_id from settings)
    set_session_id(settings.run_id)
    
    audit_logger = get_audit_logger()
    audit_logger.system_start(mode=settings.trading_mode.value, exchange=settings.exchange.value)
    
    # Validate startup (fail fast on misconfig)
    try:
        validation_result = await validate_startup(settings)
        if validation_result["valid"]:
            logger.info("✅ Startup validation passed")
        else:
            for error in validation_result.get("errors", []):
                logger.critical(f"❌ Startup validation error: {error}")
            return
        for warning in validation_result.get("warnings", []):
            logger.warning(f"⚠️ Startup validation warning: {warning}")
    except Exception as e:
        logger.critical(f"❌ Startup validation FAILED: {e}")
        return
    
    # Log build info and settings (redacted)
    logger.info("=" * 70)
    logger.info("🚀 BTC PERPETUAL TRADING SYSTEM v2.1.0 (PRODUCTION)")
    logger.info("=" * 70)
    logger.info(f"Settings: {redact_secrets(str(settings.model_dump()))}")
    
    # =========================================================================
    # 🔒 CRITICAL: Initialize RuntimeMode FIRST before anything else
    # This sets strict guardrails that PREVENT catastrophic errors
    # =========================================================================
    trading_mode = os.getenv('TRADING_MODE', 'PAPER').upper()
    
    if trading_mode == 'LIVE':
        runtime_mode = RuntimeMode.LIVE
    elif trading_mode == 'PAPER':
        runtime_mode = RuntimeMode.PAPER
        # ⚠️ CRITICAL WARNING: PAPER mode STILL EXECUTES REAL ORDERS on Kraken!
        # The only difference is equity tracking is simulated.
        # For TRUE paper trading (no real orders), use a testnet exchange
        # or implement a SimulatedExecutionEngine.
        logger.warning("⚠️  PAPER MODE WARNING: Real orders WILL be placed on Kraken!")
        logger.warning("   This is NOT a simulation - you WILL move real money!")
        logger.warning("   To disable real execution, implement SimulatedExecutionEngine")
    else:
        runtime_mode = RuntimeMode.BACKTEST
    
    # Initialize runtime config with strict validation
    runtime_config = init_runtime(runtime_mode)
    
    logger.info("=" * 70)
    logger.info(f"🔐 RUNTIME MODE: {runtime_config.mode.value}")
    logger.info(f"   Equity source: {runtime_config.equity_source}")
    logger.info(f"   Instant fill: {runtime_config.instant_fill}")
    logger.info(f"   Reduce-only required: {runtime_config.reduce_only_required}")
    
    if runtime_config.mode == RuntimeMode.LIVE:
        logger.warning("⚠️  LIVE MODE - REAL MONEY AT RISK!")
        logger.warning("   All positions will be synced from exchange")
        logger.warning("   SL/TP orders MUST be reduce_only")
    logger.info("=" * 70)
    
    # Initialize position store (singleton - used throughout)
    position_store = get_position_store()
    
    orchestrator = TradingSystemOrchestrator()
    
    try:
        await orchestrator.initialize()
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        audit_logger.log("SYSTEM_SHUTDOWN", reason="user_interrupt")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        audit_logger.log("SYSTEM_CRASH", error=str(e), severity="CRITICAL")
    finally:
        await orchestrator.shutdown()
        audit_logger.log("SYSTEM_STOPPED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
