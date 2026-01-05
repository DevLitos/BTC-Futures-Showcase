"""
Signal Engine for BTC Perpetual Trading System
Implements complete strategy logic with HTF, MTF, LTF filters
Integrated with Kraken WebSocket, REST execution, and external data

NOTE: Position sizing now uses centralized PositionSizer from core_utils.
Edge and state multipliers are coordinated through state_types module.
"""

import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, UTC, timedelta
from prometheus_client import Counter
import logging

from engine.state_machine import StateMachine, BotState
from engine.filters import (
    KillConditionsFilter, BlackSwanFilter, HTFFilter,
    LTFTriggerFilter
    # MTFStructureFilter removed (FIX #1: Enable earlier entries)
)
from engine.risk_engine import RiskEngine
from engine.indicators import (
    calculate_ema, calculate_atr, calculate_adx
)
from engine.config import HTFFilters  # For logging thresholds
from engine.advanced_filters import (
    check_funding_rate_safe,
    should_exit_on_funding,
    check_session_quality,
    check_htf_divergence,
    calculate_vpoc,
    check_vpoc_validity,
    check_btc_trend
)
from engine.edge_regime_classifier import EdgeRegimeClassifier
from engine.edge_enforcement import EdgeEnforcement
from engine.edge_validator import ContextValidity
from engine.core_utils import PositionSizer, get_position_sizer
from engine.time_provider import get_time_provider, TimeProvider

logger = logging.getLogger(__name__)

SIGNALS_GENERATED = Counter(
    'trading_signals_generated_total',
    'Total trading signals generated',
    ['symbol', 'direction']
)

# Configuration
ENTRY_TIMEOUT_MINUTES = 3  # 3 candles at 1m = 3 minutes
POSITION_CHECK_INTERVAL = 5  # Check position every 5 seconds





class SignalEngine:
    """
    Main trading engine implementing the complete strategy
    Orchestrates all filters, state machine, risk management, and execution
    
    Integrated components:
    - market_data: SynchronizedMultiTimeframeBuffer (candle history)
    - order_book: OrderBookManager (depth, imbalance, spread)
    - execution: RESTExecutionEngine (CCXT orders)
    - external_data: ExternalDataManager (funding, macro events)
    - trade_logger: TradeLogger (persistence)
    """
    
    def __init__(self, market_data, execution, external_data, trade_logger, enforcement_mode: str = 'forward'):
        """
        Initialize SignalEngine.
        
        Args:
            market_data: SynchronizedMultiTimeframeBuffer
            execution: Execution engine (real or mock)
            external_data: External data manager
            trade_logger: Trade logger
            enforcement_mode: Edge enforcement mode ('backtest', 'forward', 'live')
        """
        self.market_data = market_data
        self.execution = execution
        self.external_data = external_data
        self.trade_logger = trade_logger
        
        # Store enforcement mode for edge enforcement initialization
        self._enforcement_mode = enforcement_mode
        self._is_backtest = enforcement_mode == 'backtest'
        
        # Core components
        self.state_machine = StateMachine(backtest_mode=self._is_backtest)
        self.risk_engine = RiskEngine(capital=0.0)  # Will be updated from exchange
        
        # Symbol (set during initialization)
        self.symbol = None
        
        # Filters - ALL use market_time, backtest_mode flag for relaxed validation
        self.kill_filter = KillConditionsFilter(self.state_machine, capital=0.0, risk_engine=self.risk_engine, backtest_mode=self._is_backtest)
        self.black_swan_filter = BlackSwanFilter()
        self.htf_filter = HTFFilter(backtest_mode=self._is_backtest)
        # self.mtf_filter = MTFStructureFilter()  # REMOVED (FIX #1: Enable earlier entries)
        self.ltf_filter = LTFTriggerFilter(backtest_mode=self._is_backtest)
        
        # Regime classifier for volume-aware filtering
        self.regime_classifier = EdgeRegimeClassifier()
        
        # Edge enforcement - pre-trade validation & degradation monitoring
        # Uses Activity Regime model (not session-based) for 24/7 crypto markets
        self.edge_enforcement = EdgeEnforcement(mode=self._enforcement_mode)
        
        # Centralized position sizer for consistent sizing calculations
        self._position_sizer = get_position_sizer(
            risk_pct=0.01,  # 1% risk per trade
            max_position_pct=0.10,  # Max 10% of capital
            leverage=10
        )
        
        # Edge context (updated periodically)
        self._edge_context: ContextValidity = None
        self._last_context_update = None
        self._context_update_interval = 300  # Update every 5 minutes
        
        # Trade tracking for edge enforcement
        self._trade_counter = 0
        self._current_trade_entry_time = None
        self._current_trade_entry_price = 0.0
        self._current_trade_mfe = 0.0
        self._current_trade_mae = 0.0
        self._reached_tp1 = False
        self._reached_tp2 = False
        self._is_be_stop = False
        
        # Trade tracking
        self.current_bias: str = None  # 'LONG' or 'SHORT'
        self.impulse_high: float = 0.0
        self.impulse_low: float = 0.0
        self.pending_order_id: str = None
        self.entry_order_time: datetime = None
        
        # STATE MACHINE para evitar busy loops
        self.setup_state = None  # IDLE, WAIT_LTF, EXECUTING
        self.setup_start_time = None  # Cuándo entró en WAIT_LTF
        self.ltf_wait_timeout = 5 * 60  # 5 minutos (300s) = ~5 candles 1m
        self.setup_frozen_htf = False  # Freeze HTF evaluation
        # self.setup_frozen_mtf = False  # REMOVED (no MTF filter)
        self.last_setup_discard_time = None  # Cuándo se descartó el último setup
        self.setup_discard_cooldown = 10  # Cooldown reducido: solo 10s para evitar thrashing
        # Position management flags
        self._stops_set = False  # Flag para evitar intentar establecer SL/TP múltiples veces
        
        # =====================================================================
        # LOSS STREAK COOLDOWN - Protection against chop regimes
        # =====================================================================
        self._consecutive_losses = 0  # Tracks consecutive SL hits
        self._cooldown_until = None  # Timestamp when cooldown ends
        self._cooldown_minutes = 90  # Cooldown duration after 3 consecutive losses
        self._max_consecutive_losses = 3  # Trigger cooldown after N losses
        
        # Evaluation counter (para debug - verificar que evalúa continuamente)
        self._idle_eval_count = 0
        
        # Control
        self.is_running = False
        
        # Log state tracking (para evitar spam)
        self._last_htf_state = None
        self._last_ltf_state = None
        
        logger.info("Signal Engine initialized")
    
    def _reset_trade_state(self, reason: str = "State reset"):
        """Limpia COMPLETAMENTE el estado de trading - CRITICAL para prevenir órdenes múltiples"""
        logger.info(f"🔄 Resetting trade state: {reason}")
        
        # Limpia tracking de orden
        self.pending_order_id = None
        self.entry_order_time = None
        
        # Limpia datos de entrada
        self._last_entry_data = None
        
        # Limpia flags de posición
        self._stops_set = False
        
        # Limpia bias y setup
        self.current_bias = None
        self.impulse_high = 0.0
        self.impulse_low = 0.0
        
        # Limpia estado de setup
        self.setup_state = None
        self.setup_start_time = None
        self.setup_frozen_htf = False
        
        # Reset log state tracking (para mostrar nuevo setup)
        self._last_htf_state = None
        self._last_ltf_state = None
    
    async def _reconcile_orders(self):
        """
        Reconciliar órdenes con exchange (elimina órdenes fantasma)
        Llamar después de reconexiones de WebSocket o timeouts
        """
        try:
            # Consultar órdenes reales en exchange
            real_orders = await self.execution.get_open_orders(self.symbol)
            
            # Limpiar tracking local
            self.execution.open_orders.clear()
            
            # Re-sincronizar solo órdenes que existen
            if real_orders:
                for order in real_orders:
                    order_id = order.get('id')
                    self.execution.open_orders[order_id] = order
                    logger.debug(f"Reconciled order: {order_id}")
            else:
                logger.debug("No open orders on exchange - state clean")
                
        except Exception as e:
            logger.error(f"Error reconciling orders: {e}")
        
        # Limpia estado de setup
        self.setup_state = None
        self.setup_start_time = None
        self.setup_frozen_htf = False
    
    async def initialize(self, symbol: str = 'BTC/USDT:USDT', leverage: int = 10):
        """Initialize signal engine - fetch balance and configure leverage"""
        logger.info("Initializing Signal Engine...")
        
        # Store symbol
        self.symbol = symbol
        
        # 1. Set leverage on exchange
        leverage_set = await self.execution.set_leverage(leverage, symbol)
        if leverage_set:
            logger.info(f"✓ Leverage configured: {leverage}x")
        else:
            logger.warning(f"✗ Could not set leverage (exchange may not support it)")
        
        # 2. Fetch account balance using dedicated method
        logger.info("Fetching account balance from exchange...")
        
        # Try new method first (preferred)
        capital = await self.execution.get_available_capital()
        
        if capital > 0:
            self.risk_engine.capital = capital
            logger.info(f"✅ Capital loaded via get_available_capital: ${self.risk_engine.capital:.2f}")
        else:
            # Fallback to old method
            logger.warning("Trying fallback balance fetch...")
            balance = await self.execution.get_account_balance()
            if balance:
                updated = await self.risk_engine.update_capital_from_balance(balance)
                if updated:
                    logger.info(f"✓ Capital loaded via update_capital_from_balance: ${self.risk_engine.capital:.2f}")
                else:
                    logger.error("✗ Failed to update capital from balance")
            else:
                logger.error("✗ Failed to fetch account balance")
        
        # 3. Verify minimum capital
        if self.risk_engine.capital < 10:
            logger.warning(f"⚠ Insufficient capital: ${self.risk_engine.capital:.2f} (minimum: $10) - Trading might be restricted")
        
        # 4. Initial order reconciliation
        logger.info("Performing initial order reconciliation...")
        await self._reconcile_orders()
            
        logger.info(f"✅ Signal Engine initialized and ready | Capital: ${self.risk_engine.capital:.2f}")
    
    async def start(self):
        """Start the signal engine main loop"""
        logger.info("Starting Signal Engine...")
        self.is_running = True
        
        # Main loop
        await self._main_loop()
        
        logger.info("Signal Engine stopped")
    
    async def stop(self):
        """Stop the signal engine"""
        logger.info("Stopping Signal Engine...")
        self.is_running = False
        
        # Cancel pending orders
        if self.pending_order_id:
            try:
                await self.execution.cancel_order(self.pending_order_id)
            except Exception as e:
                logger.error(f"Error cancelling pending order: {e}")
        
        # Close position if open
        if self.risk_engine.has_open_position():
            try:
                await self.emergency_close_all()
            except Exception as e:
                logger.error(f"Error in emergency close: {e}")
    
    # ===== MAIN LOOP =====
    
    async def _main_loop(self):
        """Main trading loop - process ticks every second"""
        last_reconcile_time = datetime.now(UTC)
        
        while self.is_running:
            try:
                # Periodic order reconciliation (every 60s)
                time_since_reconcile = (datetime.now(UTC) - last_reconcile_time).total_seconds()
                if time_since_reconcile > 60:
                    # Use enhanced reconciliation
                    if hasattr(self.execution, 'auto_reconcile_orders'):
                        await self.execution.auto_reconcile_orders(self.symbol)
                    else:
                        await self._reconcile_orders()
                    last_reconcile_time = datetime.now(UTC)
                
                await self._process_tick()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(1)
    
    async def _process_tick(self):
        """Process one tick - implements strategy logic"""
        
        # UPDATE MARKET TIME in state machine (critical for cooldown/observation)
        market_time = self.market_data.get_market_time()
        self.state_machine.set_market_time(market_time)
        
        # Check if we have sufficient data
        has_data = self.market_data.has_complete_data()
        
        if not has_data:
            # Log state of data buffers (pero no cada iteración para evitar spam)
            if not hasattr(self, '_last_incomplete_log'):
                self._last_incomplete_log = market_time
            
            time_since_log = (market_time - self._last_incomplete_log).total_seconds()
            if time_since_log > 10:  # Log cada 10 segundos si falta data
                status = self.market_data.get_candle_completion_status()
                logger.warning(f"⏳ Waiting for sufficient data: 1m={status['1m']}/20, "
                             f"5m={status['5m']}/50, 1h={status['1h']}/50")
                self._last_incomplete_log = market_time
            return
        
        # Get current price (REAL-TIME, not just closed candle)
        current_price = self.market_data.get_current_price()
        
        # Get candles for indicators (ATR, etc)
        candles_1m = self.market_data.get_candles('1m')
        if not candles_1m or len(candles_1m) < 14:
            return
        
        # Calculate ATR on CLOSED candles (standard practice)
        highs = np.array([c['high'] for c in candles_1m])
        lows = np.array([c['low'] for c in candles_1m])
        closes = np.array([c['close'] for c in candles_1m])
        
        # Calculate ATR 1m
        atr_1m = calculate_atr(highs, lows, closes, 14)[-1]
        
        # ===== BLOQUE 0: KILL CONDITIONS =====
        should_kill, kill_reason = self.kill_filter.check(current_price, atr_1m, market_time)
        if should_kill:
            logger.critical(f"KILL CONDITION MET: {kill_reason}")
            await self.emergency_close_all()
            self.is_running = False
            return
        
        # Check black swan (IMPROVED: pass full candle data for wick detection)
        price_change = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0
        last_candle = candles_1m[-1] if candles_1m else {}
        is_black_swan, bs_reason = self.black_swan_filter.check(
            price_change=price_change,
            atr=atr_1m,
            market_time=market_time,
            candle_high=last_candle.get('high'),
            candle_low=last_candle.get('low'),
            candle_open=last_candle.get('open'),
            candle_close=last_candle.get('close')
        )
        
        if is_black_swan:
            logger.warning(f"🦢 BLACK SWAN: {bs_reason}")
            if self.pending_order_id:
                try:
                    await self.execution.cancel_order(self.pending_order_id)
                except Exception as e:
                    logger.error(f"Error cancelling pending order during black swan: {e}")
            
            # Limpieza completa del estado
            self._reset_trade_state("Black swan event")
            self.state_machine.start_cooldown(8)  # Reduced from 30 to 8 min
            return
        
        # Log defensive mode if active
        if self.black_swan_filter.is_defensive(market_time):
            logger.info(f"⚠️ Defensive mode active - size multiplier: {self.black_swan_filter.get_size_multiplier(market_time)}")
        
        # Update heartbeat with market time
        self.kill_filter.update_heartbeat(market_time)
        
        # ===== EDGE ENFORCEMENT UPDATES =====
        # Update edge context periodically
        await self._update_edge_context()
        # Update regime classifier with market data
        await self._update_regime_classifier(current_price)
        
        # ===== STATE MACHINE LOGIC =====
        
        if self.state_machine.state == BotState.IDLE:
            await self._handle_idle_state(current_price, atr_1m, market_time)
        
        elif self.state_machine.state == BotState.WAIT_FILL:
            await self._handle_wait_fill_state(market_time)
        
        elif self.state_machine.state == BotState.MANAGE:
            await self._handle_manage_state(current_price, atr_1m, market_time)
        
        elif self.state_machine.state == BotState.OBSERVATION:
            await self._handle_observation_state(current_price, atr_1m, market_time)
    
    # ===== OBSERVATION STATE - MONITOR ONLY =====
    
    async def _handle_observation_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """
        OBSERVATION state: Monitor market without trading
        Intenta recuperarse cuando las métricas mejoran
        """
        # Intentar salir de observation mode
        self.state_machine.try_exit_observation_mode()
        
        # Si aún estamos en observation, solo log estado
        if self.state_machine.state == BotState.OBSERVATION:
            elapsed = (market_time - self.state_machine.observation_entry_time).total_seconds() / 60 if self.state_machine.observation_entry_time else 0
            logger.debug(f"👁️ OBSERVATION: {elapsed:.0f}min elapsed | Trades: {self.state_machine.trades_in_observation} | Wins: {self.state_machine.wins_in_observation}")
        
    # ===== IDLE STATE - LOOK FOR SETUPS =====
    
    async def _handle_idle_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """IDLE state: look for trade setups with proper state management"""
        
        # PROTECCIÓN: Si estamos en OBSERVATION, no entrar en trades nuevas
        if self.state_machine.state == BotState.OBSERVATION:
            logger.debug("In OBSERVATION state, skipping entry evaluation")
            return
        
        # Contador de evaluaciones (log cada 30 iteraciones para verificar actividad)
        self._idle_eval_count += 1
        if self._idle_eval_count % 30 == 0:
            logger.info(f"📊 IDLE: Evaluated {self._idle_eval_count} ticks | Price: ${current_price:.2f} | ATR: {atr_1m:.2f}")
        
        # 🔒 CRITICAL: Verificar que NO hay orden pendiente o posición abierta
        if self.pending_order_id:
            logger.critical(f"⛔ IDLE STATE ABORT: Pending order {self.pending_order_id} exists - transitioning to WAIT_FILL")
            self.state_machine.transition_to(BotState.WAIT_FILL, "Recovered from IDLE with pending order")
            return
        
        if self.risk_engine.has_open_position():
            logger.critical(f"⛔ IDLE STATE ABORT: Open position exists - transitioning to MANAGE")
            self.state_machine.transition_to(BotState.MANAGE, "Recovered from IDLE with open position")
            return
        
        # 🔥 PERIODIC EXCHANGE SYNC: Verificar cada 30 segundos si hay posición en exchange
        if not hasattr(self, '_last_exchange_sync'):
            self._last_exchange_sync = market_time
        
        time_since_sync = (market_time - self._last_exchange_sync).total_seconds()
        if time_since_sync > 30:  # Sync cada 30s
            try:
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                if exchange_has_position:
                    logger.critical("🔄 IDLE but exchange has position - syncing state...")
                    
                    # Intentar recuperar y sincronizar posición
                    exchange_position = await self.execution.get_position(self.symbol)
                    if exchange_position:
                        await self.risk_engine.update_from_exchange_position(exchange_position)
                    
                    self.state_machine.transition_to(BotState.MANAGE, "Synced with exchange position")
                    return
                    
                self._last_exchange_sync = market_time
                
            except Exception as e:
                logger.error(f"Error during exchange sync in IDLE: {e}")
        
        # ===== CHECK IF IN WAIT_LTF STATE =====
        if self.setup_state == "WAIT_LTF":
            elapsed = (market_time - self.setup_start_time).total_seconds()
            
            # 1. Check timeout
            if elapsed > self.ltf_wait_timeout:
                logger.warning(f"WAIT_LTF timeout after {elapsed:.0f}s - Discarding setup")
                self.setup_state = "IDLE"
                self.setup_frozen_htf = False
                self.current_bias = None
                # NO establecer cooldown - permitir re-evaluación inmediata
                return
            
            # 2. Check invalidation conditions
            # EMA50 break = setup invalid
            candles_5m = self.market_data.get_candles('5m')
            if candles_5m and len(candles_5m) >= 20:
                df_5m = pd.DataFrame(candles_5m)
                closes_5m = df_5m['close'].values
                ema_50 = calculate_ema(closes_5m, 50)[-1]
                
                if self.current_bias == "LONG" and current_price < ema_50:
                    logger.warning(f"Setup invalidated: Price broke EMA50 down during WAIT_LTF")
                    self.setup_state = "IDLE"
                    self.setup_frozen_htf = False
                    self.current_bias = None
                    # NO cooldown - permitir detección inmediata si precio vuelve a subir
                    return
                
                if self.current_bias == "SHORT" and current_price > ema_50:
                    logger.warning(f"Setup invalidated: Price broke EMA50 up during WAIT_LTF")
                    self.setup_state = "IDLE"
                    self.setup_frozen_htf = False
                    self.current_bias = None
                    # NO cooldown - permitir detección inmediata si precio vuelve a bajar
                    return
            
            # 3. Try LTF trigger
            logger.debug(f"WAIT_LTF state: Checking LTF trigger ({elapsed:.0f}s/{self.ltf_wait_timeout}s)")
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 20:
                df_1m = pd.DataFrame(candles_1m)
                
                # Get regime info for volume-aware filtering
                regime_value = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else None
                regime_conf = self.regime_classifier.regime_confidence
                
                ltf_passed, ltf_reason = self.ltf_filter.check(
                    df_1m,
                    self.current_bias,
                    {},
                    regime=regime_value,
                    regime_confidence=regime_conf
                )
                
                if ltf_passed:
                    logger.info(f"✅ LTF TRIGGER FIRED - Executing {self.current_bias}")
                    self.setup_state = "EXECUTING"
                    # Proceed to entry execution
                    await self._execute_entry(current_price, atr_1m, market_time)
                    return
                else:
                    logger.debug(f"LTF not ready yet: {ltf_reason}")
                    return  # Stay in WAIT_LTF
            
            return  # Stay in WAIT_LTF, waiting for LTF trigger
        
        # ===== IDLE STATE - FULL EVALUATION =====
        # NO cooldown - los filtros ya previenen thrashing
        # Si las condiciones vuelven a ser válidas, el bot debe detectarlo inmediatamente
        
        # ===== IDLE STATE - FULL EVALUATION =====
        # Check if can trade
        can_trade, reason = self.state_machine.can_trade()
        if not can_trade:
            # Log at INFO level occasionally (every 100 evals) for diagnosis
            if self._idle_eval_count % 100 == 0:
                logger.info(f"⚠️ Cannot trade: {reason}")
            return
        
        # Refresh balance before looking for trade (every ~5 minutes in IDLE)
        if not hasattr(self, '_last_balance_check'):
            self._last_balance_check = market_time
        
        if (market_time - self._last_balance_check).total_seconds() > 300:  # 5 min
            try:
                balance = await self.execution.get_account_balance()
                if balance:
                    await self.risk_engine.update_capital_from_balance(balance)
                self._last_balance_check = market_time
            except Exception as e:
                logger.warning(f"Balance check error: {e}")
        
        # Check external data (funding, macro)
        ext_can_trade, ext_reason = self.external_data.can_trade()
        if not ext_can_trade:
            logger.debug(f"External data blocks trading: {ext_reason}")
            return
        
        # ===== BLOQUE 1: HTF FILTER (1h) =====
        candles_1h = self.market_data.get_candles('1h')
        if not candles_1h or len(candles_1h) < 50:
            return
        
        # Convert to DataFrame for filters
        df_1h = pd.DataFrame(candles_1h)
        
        htf_passed, bias, htf_reason = self.htf_filter.check(
            df_1h,
            current_price,
            correlation_data=None
        )
        
        # IMPROVED: Allow counter-trend trading in defensive mode
        # When market is volatile, both directions can present opportunities
        allow_countertrend = False
        if not htf_passed and HTFFilters.ALLOW_COUNTERTREND_DEFENSIVE:
            if self.black_swan_filter.is_defensive(market_time):
                # In defensive mode, allow trading against HTF with reduced size
                # Derive counter-trend bias from EMA relationship
                ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                
                if ema_50 > ema_200:
                    counter_bias = 'SHORT'  # Counter to bullish HTF
                else:
                    counter_bias = 'LONG'   # Counter to bearish HTF
                
                logger.info(f"⚠️ HTF rejected but DEFENSIVE MODE active - allowing counter-trend {counter_bias}")
                allow_countertrend = True
                bias = counter_bias
                htf_passed = True  # Override for this trade
        
        if not htf_passed:
            # HTF filter rechazado - loguear cada 5 minutos (cada vela 5m cerrada)
            if not hasattr(self, '_last_htf_reject_log_time'):
                self._last_htf_reject_log_time = market_time
            
            seconds_since_log = (market_time - self._last_htf_reject_log_time).total_seconds()
            
            # Log cada 5 minutos (300 segundos) o si cambió de razón
            should_log = (seconds_since_log >= 300) or (self._last_htf_state != 'rejected')
            
            if should_log:
                logger.info(f"⛔ HTF filter rejected: {htf_reason}")
                
                # Show detailed HTF values
                ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                adx_vals, _ = calculate_adx(df_1h['high'].values, df_1h['low'].values, df_1h['close'].values, 14)
                adx = adx_vals[-1] if len(adx_vals) > 0 else 0
                adx_slope = (adx_vals[-1] - adx_vals[-2]) if len(adx_vals) > 1 else 0
                
                logger.info(f"   📊 Price: ${current_price:.2f} | EMA50: ${ema_50:.2f} | EMA200: ${ema_200:.2f}")
                logger.info(f"   📈 ADX: {adx:.2f} | Slope: {adx_slope:.4f}")
                
                self._last_htf_reject_log_time = market_time
                self._last_htf_state = 'rejected'
            return
        
        self.current_bias = bias
        bias_label = f'{bias} (counter-trend)' if allow_countertrend else bias
        if self._last_htf_state != f'passed_{bias}':
            logger.info(f"✅ HTF filter passed - Bias: {bias_label}")
            self._last_htf_state = f'passed_{bias}'
        
        # ===== BLOQUE 2: SKIPPED - MTF FILTER REMOVED =====
        # OPTIMIZATION FIX #1 (Jan 3, 2026):
        # Removed MTF (5m) Structure filter to enable EARLIER ENTRIES
        # Benefits:
        #   - Eliminates 10-15 minute delay
        #   - Increases R from 0.6R → 1.0-1.2R naturally
        #   - Adds 15-25% more trades
        # Trade-off: +5-10% more false entries (mitigated by tighter LTF filter)
        # 
        # BEFORE: HTF (1h) → MTF (5m) → LTF (1m) = 3 filters, slow entry
        # NOW:    HTF (1h) → LTF (1m) = 2 filters, fast entry
        
        # Find impulse high/low from last 10 1m candles (for reference only)
        candles_1m_for_impulse = self.market_data.get_candles('1m')
        if candles_1m_for_impulse and len(candles_1m_for_impulse) >= 10:
            self.impulse_high = max([c['high'] for c in candles_1m_for_impulse[-10:]])
            self.impulse_low = min([c['low'] for c in candles_1m_for_impulse[-10:]])
        else:
            self.impulse_high = current_price
            self.impulse_low = current_price
        
        logger.info(f"⚡ BLOQUE 2 SKIPPED (MTF removed) | Impulse High: ${self.impulse_high:.2f} | Low: ${self.impulse_low:.2f}")
        
        # ===== BLOQUE 3: LTF TRIGGER (1m) =====
        logger.debug("Evaluating LTF trigger (1m)...")
        candles_1m = self.market_data.get_candles('1m')
        df_1m = pd.DataFrame(candles_1m)
        
        # Get regime info for volume-aware filtering
        regime_value = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else None
        regime_conf = self.regime_classifier.regime_confidence
        
        ltf_passed, ltf_reason = self.ltf_filter.check(
            df_1m,
            bias,
            {},  # Order book would go here but we'll use execution for real book
            regime=regime_value,
            regime_confidence=regime_conf
        )
        
        if not ltf_passed:
            if self._last_ltf_state != f'rejected_{ltf_reason}':
                logger.info(f"⛔ LTF filter rejected: {ltf_reason}")
                
                # Show detailed LTF values
                ema_9_ltf = df_1m['close'].ewm(span=9, adjust=False).mean().iloc[-1]
                ema_20_ltf = df_1m['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                
                logger.info(f"   📊 Price: ${current_price:.2f} | EMA9: ${ema_9_ltf:.2f} | EMA20: ${ema_20_ltf:.2f}")
                logger.info(f"   🎯 Bias: {bias} | Impulse High: ${self.impulse_high:.2f} | Low: ${self.impulse_low:.2f}")
                
                self._last_ltf_state = f'rejected_{ltf_reason}'
            return
        
        if self._last_ltf_state != 'passed':
            logger.info(f"✅ LTF filter passed")
            self._last_ltf_state = 'passed'
        
        # ===== ALL FILTERS PASSED - EXECUTE ENTRY =====
        logger.info(f"🎯 SETUP COMPLETE - {bias} | HTF✓ MTF✓ LTF✓")
        try:
            await self._execute_entry(current_price, atr_1m, market_time)
        except Exception as e:
            logger.error(f"⛔ _execute_entry EXCEPTION: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _execute_entry(self, current_price: float, atr_1m: float, market_time: datetime):
        """Execute entry with strict safety checks"""
        # ==================================================================
        # 🔒 FINAL GATEKEEPER - STRICT CONCURRENCY CHECK
        # ==================================================================
        
        # 1. Check internal pending order
        if self.pending_order_id:
            logger.critical(f"⛔ EXECUTION ABORTED: Pending order {self.pending_order_id} already exists")
            self.state_machine.transition_to(BotState.WAIT_FILL, "Recovered pending order state")
            return

        # 2. Check risk engine position
        if self.risk_engine.has_open_position():
            logger.critical("⛔ EXECUTION ABORTED: Risk Engine reports open position")
            self.state_machine.transition_to(BotState.MANAGE, "Recovered open position state")
            return

        # 3. Check execution engine open orders (Double Check)
        # NOTE: Exclude SL/TP orders (protective orders on existing positions)
        if hasattr(self.execution, 'open_orders') and self.execution.open_orders:
            # Filter out protective orders (stop-loss, take-profit types)
            non_protective_orders = {
                oid: order for oid, order in self.execution.open_orders.items()
                if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
            }
            
            if non_protective_orders:
                logger.critical(f"⛔ EXECUTION ABORTED: Execution Engine reports {len(non_protective_orders)} open entry orders")
                logger.info("🔄 Attempting order reconciliation...")
                
                # Intentar reconciliar automáticamente
                await self._reconcile_orders()
                
                # Verificar nuevamente después de reconciliación (re-filter)
                non_protective_orders_after = {
                    oid: order for oid, order in self.execution.open_orders.items()
                    if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                }
                
                if non_protective_orders_after:
                    logger.critical(f"⛔ Still {len(non_protective_orders_after)} entry orders after reconciliation - aborting")
                    return
                else:
                    logger.info("✅ Orders cleaned - proceeding with entry")
            else:
                logger.debug(f"ℹ️ {len(self.execution.open_orders)} protective orders (SL/TP) detected - OK to proceed")
            
        # 4. Check State Machine (Must be IDLE to enter)
        if self.state_machine.state != BotState.IDLE:
             logger.critical(f"⛔ EXECUTION ABORTED: State is {self.state_machine.state.name}, not IDLE")
             return

        # 5. STRICT EXCHANGE CHECK (Network call - ONLY FOR LIVE)
        # In backtest mode, skip this check entirely - MockExchange handles it
        if not self._is_backtest:
            try:
                logger.info("🔒 Performing STRICT exchange check before order placement...")
                
                # Check open orders - but EXCLUDE protective orders (SL/TP)
                open_orders = await self.execution.get_open_orders(self.symbol)
                if open_orders and len(open_orders) > 0:
                    # Filter out protective orders
                    non_protective_orders = [
                        order for order in open_orders
                        if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                    ]
                    
                    if non_protective_orders:
                        logger.critical(f"⛔ EXECUTION ABORTED: Exchange reports {len(non_protective_orders)} open entry orders")
                        return
                    else:
                        logger.debug(f"ℹ️ Exchange has {len(open_orders)} protective orders (SL/TP) - OK to proceed")

                # Check position
                position = await self.execution.get_position(self.symbol)
                if position and float(position.get('contracts', 0)) != 0:
                    logger.critical(f"⛔ EXECUTION ABORTED: Exchange reports open position: {position.get('contracts')} contracts")
                    self.state_machine.transition_to(BotState.MANAGE, "Recovered open position from exchange")
                    await self.risk_engine.update_from_exchange_position(position)
                    return
                    
            except Exception as e:
                 logger.error(f"Error during strict exchange check: {e}")
                 # If check fails, we abort for safety
                 logger.critical("⛔ EXECUTION ABORTED: Failed to verify exchange state")
                 return
        else:
            logger.debug("📋 [BACKTEST] Skipping STRICT exchange check")

        # ==================================================================
        # 🛡️ EDGE ENFORCEMENT - PRE-TRADE VALIDATION
        # ==================================================================
        
        # Update edge context if needed
        await self._update_edge_context()
        
        if self._edge_context is not None:
            # Calculate current drawdown from RiskEngine (source of truth for equity/drawdown)
            current_drawdown = self.risk_engine.get_drawdown_pct() / 100.0 if hasattr(self.risk_engine, 'get_drawdown_pct') else 0.0
            
            # Validate pre-trade with edge enforcement (simplified method)
            can_trade, blockers = self.edge_enforcement.validate_pre_trade_simple(
                context=self._edge_context,
                current_drawdown=current_drawdown
            )
            
            if not can_trade:
                logger.warning(f"⛔ EDGE ENFORCEMENT BLOCKED TRADE: {blockers}")
                return
            
            # Apply position size multiplier from edge enforcement
            edge_size_multiplier = self.edge_enforcement.get_position_size_multiplier()
            if edge_size_multiplier < 1.0:
                logger.info(f"📉 Edge enforcement reducing position size to {edge_size_multiplier*100:.0f}%")
        else:
            edge_size_multiplier = 1.0
            logger.debug("Edge context not available - using full size")
        
        # ==================================================================
        
        bias = self.current_bias
        
        # Get market time for cooldown checks
        market_time = self.market_data.get_market_time()
        
        # Get 5m data for support/resistance
        candles_5m = self.market_data.get_candles('5m')
        if not candles_5m or len(candles_5m) < 20:
            logger.warning("Not enough 5m data for entry execution")
            return
        df_5m = pd.DataFrame(candles_5m)
        
        # Get 1m data for volatility check
        candles_1m = self.market_data.get_candles('1m')
        if not candles_1m or len(candles_1m) < 200:
            logger.debug("Not enough 1m data for volatility filter")
            df_1m = pd.DataFrame(candles_1m) if candles_1m else None
        else:
            df_1m = pd.DataFrame(candles_1m)
        
        # ==================================================================
        # 🛡️ LOSS COOLDOWN - Block trades after consecutive losses
        # ==================================================================
        # 🛡️ LOSS COOLDOWN - Block trades after consecutive losses
        # TEMPORARILY DISABLED - Testing baseline
        # ==================================================================
        # cooldown_ok, cooldown_reason = self._check_loss_cooldown(market_time)
        # if not cooldown_ok:
        #     logger.warning(f"⛔ {cooldown_reason}")
        #     self._reset_trade_state("Loss cooldown active")
        #     return
        
        # ==================================================================
        # 🛡️ VOLATILITY MINIMUM - Block trades in low-vol chop regimes
        # TEMPORARILY DISABLED - Testing if this kills good trades in P1/P2
        # ==================================================================
        # if df_1m is not None and len(df_1m) >= 200:
        #     vol_ok, vol_reason = self._check_volatility_minimum(df_1m)
        #     if not vol_ok:
        #         logger.warning(f"⛔ {vol_reason}")
        #         self._reset_trade_state("Low volatility regime")
        #         return
        #     logger.info(f"✅ {vol_reason}")
        
        # ==================================================================
        # 🛡️ ATR REGIME FILTER - Block entries when ATR already expanded
        # ==================================================================
        atr_regime_ok, atr_regime_reason = self._check_atr_regime_allows_entry(df_5m)
        if not atr_regime_ok:
            logger.warning(f"⛔ ATR REGIME BLOCKED: {atr_regime_reason}")
            self._reset_trade_state("ATR regime filter")
            return
        logger.info(f"✅ {atr_regime_reason}")
        
        # ==================================================================
        # 🛡️ ANTI-IMPULSE FILTER - Block entries after impulsive candles
        # ==================================================================
        candles_1m = self.market_data.get_candles('1m')
        if candles_1m and len(candles_1m) >= 5:
            df_1m = pd.DataFrame(candles_1m)
            impulse_ok, impulse_reason = self._check_not_post_impulse(df_1m, atr_1m)
            if not impulse_ok:
                logger.warning(f"⛔ IMPULSE BLOCKED: {impulse_reason}")
                self._reset_trade_state("Post-impulse filter")
                return
            logger.info(f"✅ {impulse_reason}")
        
        # ==================================================================
        # 🛡️ SWEEP FILTER - Entry only if liquidity sweep detected
        # ==================================================================
        sweep_ok, sweep_reason = self._check_sweep_occurred(df_5m, bias)
        if not sweep_ok:
            logger.warning(f"⛔ SWEEP BLOCKED: {sweep_reason}")
            self._reset_trade_state("No sweep detected")
            return
        logger.info(f"✅ {sweep_reason}")
        
        # FIX: Use ATR from 5m timeframe for SL (more stable, survives noise)
        # ATR 1m is too small for BTC intraday - gets stopped out in noise
        atr_5m = self._calculate_atr_5m(df_5m)
        if atr_5m > 0:
            atr_for_sl = atr_5m  # Use 5m ATR for stop loss
            logger.info(f"📊 Using ATR 5m for SL: ${atr_for_sl:.2f} (vs 1m ATR: ${atr_1m:.2f})")
        else:
            atr_for_sl = atr_1m * 2.5  # Fallback: scale up 1m ATR
            logger.warning(f"⚠️ ATR 5m not available, using scaled 1m: ${atr_for_sl:.2f}")
        
        support_resistance = self._find_support_resistance(bias, df_5m)
        stop_loss = self.risk_engine.calculate_stop_loss(
            current_price,
            bias,
            support_resistance,
            atr_for_sl  # Use 5m ATR instead of 1m
        )
        
        # Calculate take profit - Balance between achievable and profitable
        # SL = 2.0 ATR, TP = 1.8R = 3.6 ATR expansion
        risk = abs(current_price - stop_loss)
        tp_multiplier = 1.8  # TP at 1.8R
        
        if bias == 'LONG':
            take_profit = current_price + (risk * tp_multiplier)
        else:
            take_profit = current_price - (risk * tp_multiplier)
        
        logger.info(f"Levels: Entry={current_price:.2f} | SL={stop_loss:.2f} | TP={take_profit:.2f} | Risk={risk:.2f} | RR={tp_multiplier}R")
        
        # ==================================================================
        # 🎯 EXPECTED R FILTER - THE CRITICAL FILTER
        # Only enter if potential reward >= 1.5R based on HTF structure
        # This kills P3-type trades (middle of range, no asymmetry)
        # ==================================================================
        candles_1h = self.market_data.get_candles('1h')
        if candles_1h and len(candles_1h) >= 20:
            df_1h = pd.DataFrame(candles_1h)
            expected_r_ok, expected_r_reason = self._check_expected_r(current_price, risk, bias, df_1h)
            if not expected_r_ok:
                logger.warning(f"⛔ EXPECTED R BLOCKED: {expected_r_reason}")
                self._reset_trade_state("Expected R too low")
                return
            logger.info(f"✅ {expected_r_reason}")
        
        # 🔥 REFRESH CAPITAL BEFORE POSITION SIZING
        # This ensures we use ACTUAL free margin, not stale capital value
        try:
            fresh_capital = await self.execution.get_available_capital()
            if fresh_capital > 0:
                self.risk_engine.capital = fresh_capital
                logger.debug(f"Refreshed capital: ${fresh_capital:.2f} free")
            else:
                logger.error("⛔ Failed to refresh capital - aborting entry")
                self._reset_trade_state("Capital refresh failed")
                return
        except Exception as e:
            logger.error(f"Error refreshing capital: {e}")
            self._reset_trade_state("Capital refresh error")
            return
        
        # ==================================================================
        # CENTRALIZED POSITION SIZING (Using PositionSizer)
        # ==================================================================
        
        # Get state multiplier from operational state machine
        state_multiplier = self.state_machine.get_size_multiplier()
        
        # Get black swan defensive multiplier
        black_swan_multiplier = self.black_swan_filter.get_size_multiplier(market_time)
        
        # Calculate position size using centralized sizer
        position_size, size_breakdown = self._position_sizer.calculate_position_size(
            capital=self.risk_engine.capital,
            entry_price=current_price,
            stop_loss=stop_loss,
            state_multiplier=state_multiplier,
            edge_multiplier=edge_size_multiplier,
            defensive_multiplier=black_swan_multiplier,
            leverage=10
        )
        
        # Log sizing breakdown
        logger.info(f"📊 Position sizing: base={size_breakdown['base']:.6f} × state={size_breakdown['state']:.2f} × edge={size_breakdown['edge']:.2f} × defensive={size_breakdown['defensive']:.2f} = {position_size:.6f}")
        
        if size_breakdown.get('blocked_by'):
            logger.warning(f"⛔ Sizing blocked by: {size_breakdown['blocked_by']}")
        
        if position_size <= 0:
            logger.warning("Position size is zero - skipping entry")
            return

        # ==================================================================
        # TIER 2 INTEGRATION (FIX #6, #9-#12): ADVANCED ENTRY VALIDATIONS
        # Run AFTER sizing and BEFORE order placement.
        # ==================================================================

        # Get 1H data for session/divergence/VPOC filters
        candles_1h = self.market_data.get_candles('1h')
        df_1h = pd.DataFrame(candles_1h) if candles_1h else None

        # =========================================================
        # TIER 2 FILTERS - SKIP IN BACKTEST MODE
        # These filters require external data or are too restrictive
        # for backtest with limited historical data
        # =========================================================
        if not self._is_backtest:
            # FIX #6: Funding rate control (block unsafe LONG entries)
            funding_ok, funding_reason = await check_funding_rate_safe(self.external_data, self.symbol, bias)
            if not funding_ok:
                logger.warning(f"⛔ Entry blocked (Funding): {funding_reason}")
                return

            # FIX #9: Session quality (ADX + volatility)
            adx_1h = 0.0
            volatility_bps = 0.0
            if df_1h is not None and len(df_1h) >= 20:
                try:
                    adx_vals, _ = calculate_adx(df_1h['high'].values, df_1h['low'].values, df_1h['close'].values, 14)
                    adx_1h = float(adx_vals[-1]) if len(adx_vals) > 0 else 0.0

                    last = df_1h.iloc[-1]
                    close = float(last.get('close', 0) or 0)
                    high = float(last.get('high', 0) or 0)
                    low = float(last.get('low', 0) or 0)
                    volatility_bps = ((high - low) / close * 10000.0) if close > 0 else 0.0
                except Exception as e:
                    logger.debug(f"Session metrics calc failed: {e}")

            session_ok, session_reason = check_session_quality(adx_1h, volatility_bps)
            if not session_ok:
                logger.warning(f"⛔ Entry blocked (Session): {session_reason}")
                return

            # FIX #10: HTF divergence (block reversal risk)
            if df_1h is not None:
                divergence_detected, divergence_reason = check_htf_divergence(df_1h, bias)
                if divergence_detected:
                    logger.warning(f"⛔ Entry blocked (Divergence): {divergence_reason}")
                    return

            # FIX #11: VPOC validity (avoid chasing away from value)
            if df_1h is not None:
                vpoc = calculate_vpoc(df_1h)
                vpoc_ok, vpoc_reason = check_vpoc_validity(current_price, vpoc, bias)
                if not vpoc_ok:
                    logger.warning(f"⛔ Entry blocked (VPOC): {vpoc_reason}")
                    return

            # FIX #12: BTC trend correlation (avoid LONGs when BTC is down)
            btc_ok, btc_reason = await check_btc_trend(self.external_data, bias)
            if not btc_ok:
                logger.warning(f"⛔ Entry blocked (BTC Trend): {btc_reason}")
                return
        else:
            logger.debug("📋 [BACKTEST] Skipping TIER2 filters (Funding, Session, Divergence, VPOC, BTC)")
        
        # ===== FIX #2: DETERMINE ORDER TYPE BASED ON MOMENTUM =====
        # Calculate price velocity from last 2 candles (1m)
        candles_1m = self.market_data.get_candles('1m')
        use_market_order = False
        price_velocity = 0.0
        
        if candles_1m and len(candles_1m) >= 2:
            from engine.indicators import calculate_atr
            
            # Last 2 candles (1m)
            last_close = candles_1m[-1]['close']
            prev_close = candles_1m[-2]['close']
            
            # Price change
            price_change = abs(last_close - prev_close)
            
            # ATR(1m)
            closes_1m = [c['close'] for c in candles_1m[-14:]]
            highs_1m = [c['high'] for c in candles_1m[-14:]]
            lows_1m = [c['low'] for c in candles_1m[-14:]]
            
            if len(closes_1m) >= 14:
                atr_1m_vals = calculate_atr(
                    np.array(highs_1m, dtype=float),
                    np.array(lows_1m, dtype=float),
                    np.array(closes_1m, dtype=float),
                    14
                )
                atr_1m_current = atr_1m_vals[-1] if len(atr_1m_vals) > 0 else atr_1m
                
                # Velocity = price change / ATR
                price_velocity = price_change / atr_1m_current if atr_1m_current > 0 else 0
                
                # Use market if velocity > 2x ATR
                from engine.config import OrderExecution
                use_market_order = (
                    OrderExecution.USE_MARKET_ON_MOMENTUM and 
                    price_velocity > OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER
                )
                
                if use_market_order:
                    logger.info(f"⚡ HIGH MOMENTUM: velocity={price_velocity:.2f}ATR > {OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER}ATR → Using MARKET order")
                else:
                    logger.debug(f"Normal momentum: velocity={price_velocity:.2f}ATR < {OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER}ATR → Using LIMIT order")
        
        # ===== PLACE ORDER (MARKET or LIMIT based on momentum) =====
        try:
            if use_market_order:
                # Market order for momentum
                logger.info(f"🚀 Placing MARKET order | {bias} {position_size} @ market | Velocity: {price_velocity:.2f}ATR")
                order_result = await self.execution.place_market_order(
                    symbol=self.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=position_size,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
            else:
                # Limit post-only order (default)
                logger.info(f"📊 Placing LIMIT post-only | {bias} {position_size} @ {current_price:.2f} | Velocity: {price_velocity:.2f}ATR")
                order_result = await self.execution.place_limit_order(
                    symbol=self.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=position_size,
                    price=current_price,
                    stop_loss=stop_loss,      # Stored for later placement
                    take_profit=take_profit   # Stored for later placement
                )
            
            if order_result:
                self.pending_order_id = order_result.get('id')
                self.entry_order_time = market_time  # Use market_time, not datetime.now()
                
                # Store entry data for position creation upon fill
                self._last_entry_data = {
                    'bias': bias,
                    'entry_price': current_price,
                    'position_size': position_size,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,  # Guardar TP para establecer después
                    'atr_1m': atr_1m,
                    'order_type': 'MARKET' if use_market_order else 'LIMIT'
                }
                
                # FIX (Jan 4, 2026): BACKTEST INSTANT FILL
                # In backtest, orders fill instantly - skip WAIT_FILL entirely
                order_status = order_result.get('status', '')
                if order_status in ('filled', 'closed') or order_result.get('filled', 0) > 0:
                    logger.info(f"✅ INSTANT FILL (backtest): {self.pending_order_id} - skipping WAIT_FILL")
                    
                    # Create position directly - same logic as _handle_wait_fill_state
                    fill_price = order_result.get('average') or order_result.get('price') or current_price
                    
                    self.risk_engine.create_position(
                        symbol=self.symbol,
                        direction=bias,
                        entry_price=fill_price,
                        position_size=position_size,
                        stop_loss=stop_loss,
                        atr_1m=atr_1m,
                        market_time=market_time
                    )
                    
                    self.state_machine.record_trade_open(self.pending_order_id)
                    
                    # Clear pending state
                    self.pending_order_id = None
                    self.entry_order_time = None
                    self._last_entry_data = None
                    
                    # Go directly to MANAGE
                    self.state_machine.transition_to(BotState.MANAGE, "Instant fill (backtest)")
                    
                    SIGNALS_GENERATED.labels(symbol=self.symbol, direction=bias).inc()
                    logger.info(f"🎯 Position opened: {bias} {position_size} @ {fill_price:.2f}")
                else:
                    # Normal flow for live trading - wait for fill
                    self.state_machine.start_entry_timer()
                    self.state_machine.transition_to(BotState.WAIT_FILL, "Order placed")
                    
                    SIGNALS_GENERATED.labels(symbol=self.symbol, direction=bias).inc()
                    logger.info(f"Entry order placed: {self.pending_order_id}")
            else:
                # FIX (Jan 3, 2026): Reduced cooldown from 60 to 5 minutes
                logger.warning("⚠️ EXECUTION FAILED - Cooldown 5m (was 60m before fix)")
                self._reset_trade_state("Order placement failed")
                self.state_machine.start_cooldown(5)  # 5 minutes cooldown (reduced from 60)
        
        except Exception as e:
            # FIX (Jan 3, 2026): Reduced cooldown from 60 to 5 minutes
            logger.error(f"Error placing order: {e}", exc_info=True)
            logger.warning("⚠️ Order exception - Cooldown 5m (was 60m before fix)")
            self._reset_trade_state("Order exception")
            self.state_machine.start_cooldown(5)  # 5 minutes cooldown (reduced from 60)
    
    # ===== WAIT_FILL STATE - MONITOR ORDER =====
    
    async def _handle_wait_fill_state(self, market_time: datetime):
        """WAIT_FILL state: monitor pending order"""
        
        if not self.pending_order_id:
            # 🔒 LIMPIEZA si no hay orden pendiente
            self._reset_trade_state("WAIT_FILL with no pending order")
            self.state_machine.transition_to(BotState.IDLE, "No pending order")
            return
        
        # FIX (Jan 4, 2026): Check if order is already in fills (backtest instant fill)
        # This catches cases where the order filled immediately but we still ended up in WAIT_FILL
        if hasattr(self.execution, 'fills'):
            for fill in self.execution.fills:
                if fill.get('order_id') == self.pending_order_id:
                    logger.info(f"✅ Order {self.pending_order_id} already filled (backtest) - processing immediately")
                    
                    if hasattr(self, '_last_entry_data') and self._last_entry_data:
                        entry_data = self._last_entry_data
                        self.risk_engine.create_position(
                            symbol=self.symbol,
                            direction=entry_data['bias'],
                            entry_price=fill.get('price', entry_data['entry_price']),
                            position_size=entry_data['position_size'],
                            stop_loss=entry_data['stop_loss'],
                            atr_1m=entry_data['atr_1m'],
                            market_time=market_time
                        )
                        
                        self.state_machine.record_trade_open(self.pending_order_id)
                        self._reset_trade_state("Instant fill processed")
                        self.state_machine.transition_to(BotState.MANAGE, "Fill detected (backtest)")
                        logger.info(f"🎯 Position opened from WAIT_FILL: {entry_data['bias']} {entry_data['position_size']} @ {fill.get('price'):.2f}")
                        return
        
        # Check order timeout (3 minutes = 3 candles) - USE MARKET TIME
        if self.entry_order_time:
            elapsed = (market_time - self.entry_order_time).total_seconds()
            if elapsed > ENTRY_TIMEOUT_MINUTES * 60:
                logger.warning(f"Entry timeout after {elapsed:.0f}s - attempting cancel")
                
                # Intentar cancelar en exchange
                cancel_success = False
                try:
                    cancel_success = await self.execution.cancel_order(
                        self.pending_order_id,
                        self.symbol
                    )
                except Exception as e:
                    logger.error(f"Error cancelling order: {e}")
                
                # CRITICAL: Limpiar open_orders incluso si la cancelación falla
                # (la orden puede ya estar filled o cancelada)
                if self.pending_order_id in self.execution.open_orders:
                    self.execution.open_orders.pop(self.pending_order_id, None)
                    logger.debug(f"Cleaned order {self.pending_order_id} from local tracking")
                
                # Reconciliar con exchange para estar seguros
                await self._reconcile_orders()
                
                # Limpieza completa del estado
                self._reset_trade_state("Order timeout")
                self.state_machine.transition_to(BotState.IDLE, "Entry timeout")
                return
        
        # Check if order was filled
        try:
            fill_status = await self.execution.poll_fill_status(
                self.pending_order_id,
                self.symbol
            )
            
            if fill_status:
                # Order filled - VERIFICAR POSICIÓN REAL antes de crear
                logger.info(f"✅ Entry order FILLED - verifying position on exchange...")
                
                # CRITICAL: Verificar que hay contracts en exchange
                await asyncio.sleep(0.5)  # Dar tiempo a que exchange actualice
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                
                if not exchange_has_position:
                    logger.error("⚠️ Order filled but NO position on exchange - possible partial fill or error")
                    self._reset_trade_state("Fill reported but no position")
                    self.state_machine.transition_to(BotState.IDLE, "No position after fill")
                    return
                
                logger.info("✅ Position confirmed on exchange - creating local tracking")
                
                # Limpiar de open_orders
                if self.pending_order_id in self.execution.open_orders:
                    self.execution.open_orders.pop(self.pending_order_id, None)
                
                # Create Position object for risk management
                # Use stored entry levels from last execution attempt
                if hasattr(self, '_last_entry_data') and self._last_entry_data:
                    entry_data = self._last_entry_data
                    self.risk_engine.create_position(
                        symbol=self.symbol,
                        direction=entry_data['bias'],
                        entry_price=entry_data['entry_price'],
                        position_size=entry_data['position_size'],
                        stop_loss=entry_data['stop_loss'],
                        atr_1m=entry_data['atr_1m'],
                        market_time=market_time  # Use market time, not datetime.now()
                    )
                    logger.info(f"Position created: {entry_data['bias']} {entry_data['position_size']:.6f} @ {entry_data['entry_price']:.2f}")
                    
                    # 🔥 ESTABLECER SL/TP EN LA POSICIÓN ABIERTA (UNA SOLA VEZ)
                    if not self._stops_set:
                        logger.info("📍 Setting SL/TP on open position...")
                        stops_set = await self.execution.set_position_stops(
                            symbol=self.symbol,
                            stop_loss=entry_data['stop_loss'],
                            take_profit=entry_data.get('take_profit')  # TP si existe
                        )
                        
                        if stops_set:
                            logger.info("✅ SL/TP successfully set on position")
                            self._stops_set = True
                        else:
                            logger.warning("⚠️ Failed to set SL/TP - position at risk!")
                            logger.critical("🚨 POSITION WITHOUT SL - MANUAL INTERVENTION RECOMMENDED")
                            # Continuar de todas formas - la posición está abierta
                            # TODO: Implementar retry o alerta externa
                    
                else:
                    logger.info(f"Position created: {entry_data['bias']} {entry_data['position_size']:.6f} @ {entry_data['entry_price']:.2f}")
                    # No entry data - error en tracking
                    self.pending_order_id = None
                    self.state_machine.transition_to(BotState.IDLE, "Fill without entry data")
                    return
                
                # Limpia pending_order_id PERO mantiene _last_entry_data para tracking
                self.pending_order_id = None
                self.state_machine.transition_to(BotState.MANAGE, "Order filled")
            else:
                # Still pending - stay in WAIT_FILL
                pass
        
        except Exception as e:
            logger.error(f"Error checking fill status: {e}")
    
    # ===== MANAGE STATE - RUN POSITION =====
    
    async def _handle_manage_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """MANAGE state: run open position"""
        
        # 🔥 CRITICAL: Verificar posición REAL en exchange PRIMERO
        # Esto previene loops si solo hay órdenes pendientes (TP/SL) pero no posición
        try:
            exchange_has_position = await self.execution.has_open_position(self.symbol)
            
            if not exchange_has_position:
                logger.warning("⚠️ MANAGE state but NO position on exchange")
                
                # Verificar si tenemos posición local
                if self.risk_engine.position:
                    logger.critical("🔄 Position closed on exchange but exists locally - syncing...")
                    
                    # Calcular PnL aproximado (no sabemos precio exacto de cierre)
                    position = self.risk_engine.position
                    estimated_pnl = 0  # No podemos calcular exacto
                    
                    # Registrar cierre
                    self.state_machine.record_trade_close(estimated_pnl, False)
                    
                    # Limpiar posición local
                    self.risk_engine.position = None
                else:
                    logger.warning("No local position either - likely only pending orders exist")
                
                # Cancelar todas las órdenes pendientes (SL/TP sin posición)
                logger.info("Cancelling all pending orders (no position to protect)")
                cancelled = await self.execution.cancel_all_orders(self.symbol)
                if cancelled > 0:
                    logger.info(f"Cancelled {cancelled} pending orders")
                
                # Limpieza completa y volver a IDLE
                self._reset_trade_state("No position on exchange")
                self.state_machine.transition_to(BotState.IDLE, "No position")
                return
                
        except Exception as e:
            logger.error(f"Error checking exchange position in MANAGE: {e}")
            # Continuar con el flujo normal si falla el check
        
        # 🔥 RETRY: Intentar establecer SL/TP si no se estableció en fill
        if not self._stops_set and hasattr(self, '_last_entry_data') and self._last_entry_data:
            logger.warning("🔄 RETRY: Attempting to set SL/TP (not set during fill)")
            try:
                stops_set = await self.execution.set_position_stops(
                    symbol=self.symbol,
                    stop_loss=self._last_entry_data['stop_loss'],
                    take_profit=self._last_entry_data.get('take_profit')
                )
                
                if stops_set:
                    # VERIFICAR que realmente se colocaron en exchange
                    await asyncio.sleep(0.5)
                    verification = await self.execution.verify_stops_on_exchange(self.symbol)
                    
                    if verification['has_sl']:
                        logger.info("✅ SL/TP successfully set AND verified on exchange")
                        self._stops_set = True
                    else:
                        logger.critical("❌ SL/TP claimed success but NOT found on exchange - UNPROTECTED!")
                        # Intentar nuevamente con parámetros ajustados
                        logger.info("Retrying with different parameters...")
                        await asyncio.sleep(1)
                        retry2 = await self.execution.set_position_stops(
                            symbol=self.symbol,
                            stop_loss=self._last_entry_data['stop_loss'],
                            take_profit=self._last_entry_data.get('take_profit')
                        )
                        if retry2:
                            self._stops_set = True
                else:
                    logger.error("❌ Failed to set SL/TP on retry - position still at risk!")
            except Exception as e:
                logger.error(f"Error setting stops on retry: {e}")
        
        # Verificar que tenemos posición local
        position = self.risk_engine.position
        if not position:
            # 🔒 LIMPIEZA COMPLETA si no hay posición local
            logger.warning("MANAGE state but no local position - transitioning to IDLE")
            self._reset_trade_state("MANAGE state with no position")
            self.state_machine.transition_to(BotState.IDLE, "No position")
            return

        # Track MFE/MAE for edge enforcement
        if position.entry_price > 0:
            if position.direction == 'LONG':
                excursion = (current_price - position.entry_price) / position.entry_price
            else:  # SHORT
                excursion = (position.entry_price - current_price) / position.entry_price
            
            # Update MFE (Max Favorable Excursion)
            if excursion > self._current_trade_mfe:
                self._current_trade_mfe = excursion
            # Update MAE (Max Adverse Excursion) - negative excursion
            if excursion < 0 and abs(excursion) > self._current_trade_mae:
                self._current_trade_mae = abs(excursion)

        # ==================================================================
        # TIER 2 INTEGRATION (FIX #6): Funding-based forced exit
        # TIER 2 INTEGRATION (FIX #13): Time-based forced exit
        # ==================================================================

        try:
            # FIX #6: exit on extreme funding (LONG only)
            exit_funding, funding_exit_reason = await should_exit_on_funding(
                self.external_data,
                self.symbol,
                position.direction
            )
            if exit_funding:
                logger.critical(f"🚨 FORCED EXIT (Funding): {funding_exit_reason}")
                await self.execution.close_position_market(
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=position.size
                )
                self.state_machine.record_trade_close(position.unrealized_pnl, position.unrealized_pnl > 0)
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': position.unrealized_pnl,
                    'reason': 'FUNDING_EXIT'
                })
                self.risk_engine.position = None
                self._reset_trade_state(f"Trade closed: FUNDING_EXIT")
                self.state_machine.start_cooldown()
                self.state_machine.transition_to(BotState.IDLE, "Trade closed: FUNDING_EXIT")
                return

            # FIX #13: time-based exit (stagnation-based, not clock-based)
            # REDESIGNED (Jan 4, 2026): Pass current ATR for stagnation check
            current_atr = position.atr_1m  # Use stored ATR, can be updated
            exit_time, time_exit_reason = position.should_force_exit_by_time(market_time, current_atr)
            if exit_time:
                logger.critical(f"🚨 FORCED EXIT (Time): {time_exit_reason}")
                await self.execution.close_position_market(
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=position.size
                )
                self.state_machine.record_trade_close(position.unrealized_pnl, position.unrealized_pnl > 0)
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': position.unrealized_pnl,
                    'reason': 'TIME_BASED_EXIT'
                })
                self.risk_engine.position = None
                self._reset_trade_state(f"Trade closed: TIME_BASED_EXIT")
                self.state_machine.start_cooldown()
                self.state_machine.transition_to(BotState.IDLE, "Trade closed: TIME_BASED_EXIT")
                return

        except Exception as e:
            logger.error(f"Error during forced-exit checks: {e}")
        
        # Get volume for analysis
        candles_1m = self.market_data.get_candles('1m')
        if candles_1m and len(candles_1m) > 0:
            df_1m = pd.DataFrame(candles_1m)
            current_volume = df_1m['volume'].iloc[-1]
            avg_volume = df_1m['volume'].mean()
        else:
            current_volume = 0
            avg_volume = 0
        
        # Find TP2 target
        tp2_target = self._find_tp2_target()
        
        # Get EMA20 from 5m for structure check (SMART BE)
        candles_5m = self.market_data.get_candles('5m')
        ema20_5m = None
        if candles_5m and len(candles_5m) >= 20:
            df_5m = pd.DataFrame(candles_5m)
            closes_5m = df_5m['close'].values
            ema_20 = calculate_ema(closes_5m, 20)
            ema20_5m = ema_20[-1] if len(ema_20) > 0 else None
        
        # Check for BOS against position
        has_bos_against = self._check_bos_against_position(position.direction, candles_5m)
        
        # Manage position (con datos de estructura para SMART BE)
        exit_action = await self.risk_engine.manage_position(
            current_price,
            atr_1m,
            current_volume,
            avg_volume,
            tp2_target=tp2_target,
            ema20_5m=ema20_5m,
            has_bos_against=has_bos_against,
            market_time=market_time  # Pass market time for trailing stop phases
        )
        
        if exit_action:
            action = exit_action.get('action')

            # Execute exchange action to match local risk engine decisions
            try:
                if action == 'PARTIAL_CLOSE':
                    close_size = float(exit_action.get('size', 0) or 0)
                    if close_size > 0:
                        await self.execution.close_position_market(
                            side='sell' if position.direction == 'LONG' else 'buy',
                            amount=close_size
                        )

                    # Mark TP1 reached for edge enforcement
                    self._reached_tp1 = True
                    
                    await self.trade_logger.log_event('tp1_hit', {
                        'entry': position.entry_price,
                        'price': current_price,
                        'size_closed': close_size,
                        'pnl_realized': exit_action.get('pnl', 0),
                        'reason': exit_action.get('reason', 'TP1')
                    })
                    # Stay in MANAGE with reduced local size
                    return

                if action == 'FULL_CLOSE':
                    close_size = float(exit_action.get('size', 0) or 0)
                    if close_size > 0:
                        await self.execution.close_position_market(
                            side='sell' if position.direction == 'LONG' else 'buy',
                            amount=close_size
                        )
                    
                    # Check if TP2 was reached
                    reason = exit_action.get('reason', '')
                    if 'TP2' in reason or 'tp2' in reason:
                        self._reached_tp2 = True
                    if 'BE' in reason or 'breakeven' in reason.lower():
                        self._is_be_stop = True
                        
            except Exception as e:
                logger.error(f"Error executing exchange close for {action}: {e}")

            # Record full-close only
            pnl = exit_action.get('pnl', 0)
            is_win = pnl > 0
            exit_reason = exit_action.get('reason', 'unknown')

            self.state_machine.record_trade_close(pnl, is_win)
            
            # Record for loss cooldown tracking
            self._record_trade_result(exit_reason, pnl, market_time)
            
            # Record trade for edge enforcement degradation monitoring
            if self._current_trade_entry_time:
                self._record_trade_for_edge_enforcement(
                    entry_price=self._current_trade_entry_price,
                    exit_price=current_price,
                    entry_time=self._current_trade_entry_time,
                    exit_time=market_time,
                    reached_tp1=self._reached_tp1,
                    reached_tp2=self._reached_tp2,
                    is_be_stop=self._is_be_stop
                )

            await self.trade_logger.log_event('trade_closed', {
                'entry': exit_action.get('entry_price', position.entry_price),
                'exit': current_price,
                'size': exit_action.get('size', position.size),
                'pnl': pnl,
                'reason': exit_action.get('reason', 'unknown')
            })

            self._reset_trade_state(f"Trade closed: {exit_action.get('reason')}")

            self.state_machine.start_cooldown()
            self.state_machine.transition_to(BotState.IDLE, f"Trade closed: {exit_action.get('reason')}")
    
    # ===== HELPERS =====
    
    async def _open_position_from_order(self, fill_status: dict):
        """Open position after order fill"""
        try:
            fill_price = fill_status.get('price', 0)
            fill_size = fill_status.get('amount', 0)
            
            # Calculate stop loss and ATR
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 14:
                df_1m = pd.DataFrame(candles_1m)
                highs = df_1m['high'].values
                lows = df_1m['low'].values
                closes = df_1m['close'].values
                atr_1m = calculate_atr(highs, lows, closes, 14)[-1]
            else:
                logger.warning("Not enough 1m data for ATR, using default")
                atr_1m = fill_price * 0.01  # 1% as fallback
            
            # Find support/resistance
            candles_5m = self.market_data.get_candles('5m')
            df_5m = pd.DataFrame(candles_5m) if candles_5m else pd.DataFrame()
            support_resistance = self._find_support_resistance(self.current_bias, df_5m)
            
            # Calculate stop loss
            stop_loss = self.risk_engine.calculate_stop_loss(
                fill_price,
                self.current_bias,
                support_resistance,
                atr_1m
            )
            
            # Open position in risk engine (use market_time for consistency)
            market_time = self.market_data.get_market_time()
            position = await self.risk_engine.open_position(
                symbol=self.symbol,
                direction=self.current_bias,
                entry_price=fill_price,
                stop_loss=stop_loss,
                atr_1m=atr_1m,
                hour=market_time.hour if market_time else 12,
                minute=market_time.minute if market_time else 0
            )
            
            if position:
                # Log trade open
                await self.trade_logger.log_event('trade_opened', {
                    'symbol': self.symbol,
                    'direction': self.current_bias,
                    'entry': fill_price,
                    'size': fill_size,
                    'stop_loss': stop_loss
                })
                
                # Initialize edge enforcement tracking
                self._current_trade_entry_time = market_time
                self._current_trade_entry_price = fill_price
                self._current_trade_mfe = 0.0
                self._current_trade_mae = 0.0
                self._reached_tp1 = False
                self._reached_tp2 = False
                self._is_be_stop = False
                
                # Transition to MANAGE
                self.state_machine.transition_to(BotState.MANAGE, "Position opened")
                logger.debug(f"Position opened: {self.current_bias} {fill_size} @ {fill_price:.2f}")
            else:
                logger.error("Failed to open position in risk engine")
                self.state_machine.transition_to(BotState.IDLE, "Failed to open position")
                
        except Exception as e:
            logger.error(f"Error opening position from order: {e}", exc_info=True)
            self.state_machine.transition_to(BotState.IDLE, "Error opening position")
    
    def _find_support_resistance(self, bias: str, df_5m: pd.DataFrame) -> float:
        """Find technical support/resistance level based on swing structure
        
        EDGE FIX: Use proper swing lookback (10 candles = 50min) not just 5,
        and this is just the BASE level - ATR buffer added in calculate_stop_loss
        """
        # Use 10 candles (50min) for more meaningful structure
        lookback = min(10, len(df_5m))
        
        if bias == 'LONG':
            # Recent swing low (HL)
            return df_5m['low'].iloc[-lookback:].min()
        else:
            # Recent swing high (LH)
            return df_5m['high'].iloc[-lookback:].max()
    
    def _calculate_atr_5m(self, df_5m: pd.DataFrame) -> float:
        """Calculate ATR from 5m timeframe for more stable SL sizing"""
        if len(df_5m) < 14:
            return 0.0
        
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        closes = df_5m['close'].values
        
        tr = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr.append(max(hl, hc, lc))
        
        if len(tr) < 14:
            return sum(tr) / len(tr) if tr else 0.0
        
        # Simple moving average of TR (last 14 periods)
        return sum(tr[-14:]) / 14
    
    def _check_atr_regime_allows_entry(self, df_5m: pd.DataFrame) -> tuple[bool, str]:
        """
        ATR REGIME FILTER: Block entries when ATR is already expanded
        
        We want to enter during CONTRACTION, not EXPANSION.
        If ATR is above 65th percentile of recent ATR, the move already happened.
        
        Returns: (can_enter, reason)
        """
        if len(df_5m) < 50:
            return True, "Not enough data for ATR regime"
        
        # Calculate ATR for each candle
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        closes = df_5m['close'].values
        
        atrs = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            atrs.append(max(hl, hc, lc))
        
        if len(atrs) < 30:
            return True, "Not enough ATR history"
        
        current_atr = atrs[-1]
        # Use last 50 candles for percentile (250 min = ~4 hours)
        lookback = min(50, len(atrs))
        recent_atrs = atrs[-lookback:]
        
        # Calculate 50th percentile (median) - more strict, want low ATR environments
        sorted_atrs = sorted(recent_atrs)
        p50_idx = int(len(sorted_atrs) * 0.50)
        atr_p50 = sorted_atrs[p50_idx]
        
        if current_atr > atr_p50:
            return False, f"ATR regime HIGH: {current_atr:.2f} > P50 {atr_p50:.2f} (move already happened)"
        
        return True, f"ATR regime OK: {current_atr:.2f} <= P50 {atr_p50:.2f}"
    
    def _check_volatility_minimum(self, df_1m: pd.DataFrame) -> tuple[bool, str]:
        """
        VOLATILITY MINIMUM FILTER: Block trades in low volatility regimes
        
        Problem in P3 (Dec 15-22): ATR was low, market in micro-range/chop.
        Low ATR → small SL → appears "cheap" → overtrading → stops accumulate
        
        Solution: Compare current ATR to its own 200-period SMA.
        If atr_ratio < 0.75, we're in a low-vol regime where sweeps don't expand.
        
        atr_ratio = atr_current / sma(atr, 200)
        
        Returns: (can_enter, reason)
        """
        if len(df_1m) < 200:
            return True, "Not enough data for volatility filter"
        
        # Calculate ATR for each candle
        highs = df_1m['high'].values
        lows = df_1m['low'].values
        closes = df_1m['close'].values
        
        atrs = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            atrs.append(max(hl, hc, lc))
        
        if len(atrs) < 200:
            return True, "Not enough ATR samples"
        
        # Current ATR (last 14 periods average)
        current_atr = sum(atrs[-14:]) / 14
        
        # SMA of ATR over last 200 periods
        atr_sma_200 = sum(atrs[-200:]) / 200
        
        if atr_sma_200 <= 0:
            return True, "ATR SMA is zero"
        
        atr_ratio = current_atr / atr_sma_200
        
        # Threshold: 0.75 = 25% below normal volatility
        MIN_VOL_RATIO = 0.75
        
        if atr_ratio < MIN_VOL_RATIO:
            return False, f"LOW VOL REGIME: ATR ratio {atr_ratio:.2f} < {MIN_VOL_RATIO} (chop zone)"
        
        return True, f"Volatility OK: ATR ratio {atr_ratio:.2f} >= {MIN_VOL_RATIO}"
    
    def _check_loss_cooldown(self, market_time: datetime) -> tuple[bool, str]:
        """
        LOSS STREAK COOLDOWN: Block trades after consecutive losses
        
        After 3 consecutive SL hits, enter 90-minute cooldown.
        This protects against chop regimes where stops cluster.
        
        Returns: (can_enter, reason)
        """
        # Check if we're still in cooldown
        if self._cooldown_until is not None:
            if market_time < self._cooldown_until:
                remaining = (self._cooldown_until - market_time).total_seconds() / 60
                return False, f"LOSS COOLDOWN: {remaining:.0f} min remaining ({self._consecutive_losses} consecutive losses)"
            else:
                # Cooldown expired, reset
                logger.info("✅ Loss cooldown expired, trading resumed")
                self._cooldown_until = None
                self._consecutive_losses = 0
        
        return True, f"No cooldown active (streak: {self._consecutive_losses})"
    
    def _record_trade_result(self, exit_reason: str, pnl: float, market_time: datetime):
        """
        Record trade result for cooldown tracking.
        Called when a trade closes.
        """
        if exit_reason == 'STOP_LOSS' or pnl < 0:
            self._consecutive_losses += 1
            logger.info(f"📉 Loss recorded: {self._consecutive_losses} consecutive losses")
            
            if self._consecutive_losses >= self._max_consecutive_losses:
                self._cooldown_until = market_time + timedelta(minutes=self._cooldown_minutes)
                logger.warning(f"🚫 COOLDOWN ACTIVATED: {self._max_consecutive_losses} consecutive losses → {self._cooldown_minutes} min pause until {self._cooldown_until}")
        else:
            # Win or breakeven resets the streak
            if self._consecutive_losses > 0:
                logger.info(f"✅ Win/BE recorded: resetting loss streak from {self._consecutive_losses}")
            self._consecutive_losses = 0
    
    def _check_expected_r(self, current_price: float, sl_distance: float, bias: str, df_1h: pd.DataFrame) -> tuple[bool, str]:
        """
        EXPECTED R FILTER: Only enter if potential reward >= 1.5R
        
        This is THE critical filter that P3 needs.
        
        Problem: System enters setups that can only produce 1R max.
        With SL 2-2.5 ATR, if market can only give 1R, we're mathematically doomed.
        
        Solution: Calculate distance to next HTF level (resistance/support).
        If expected_move / sl_distance < 1.5 → NO trade.
        
        This kills trades in the middle of ranges where there's no asymmetry.
        
        Returns: (can_enter, reason)
        """
        if len(df_1h) < 20:
            return True, "Not enough HTF data for Expected R"
        
        if sl_distance <= 0:
            return True, "Invalid SL distance"
        
        highs = df_1h['high'].values
        lows = df_1h['low'].values
        
        # Find recent swing high and swing low (last 20 1h candles = 20 hours)
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        range_size = recent_high - recent_low
        
        if range_size <= 0:
            return True, "No valid range"
        
        # Calculate position in range (0 = at low, 1 = at high)
        position_in_range = (current_price - recent_low) / range_size
        
        if bias == 'LONG':
            # For LONG: target is recent high (or higher)
            # Distance to target = recent_high - current_price
            distance_to_target = recent_high - current_price
            
            # If we're already near the top (>70% of range), very little upside
            if position_in_range > 0.70:
                return False, f"LONG near range top: {position_in_range:.0%} (no upside room)"
            
        else:  # SHORT
            # For SHORT: target is recent low (or lower)
            # Distance to target = current_price - recent_low
            distance_to_target = current_price - recent_low
            
            # If we're already near the bottom (<30% of range), very little downside
            if position_in_range < 0.30:
                return False, f"SHORT near range bottom: {position_in_range:.0%} (no downside room)"
        
        # Calculate Expected R
        expected_r = distance_to_target / sl_distance
        
        # Minimum threshold: 1.5R
        MIN_EXPECTED_R = 1.5
        
        if expected_r < MIN_EXPECTED_R:
            return False, f"Expected R too low: {expected_r:.2f}R < {MIN_EXPECTED_R}R (target ${distance_to_target:.0f} vs SL ${sl_distance:.0f})"
        
        return True, f"Expected R OK: {expected_r:.2f}R >= {MIN_EXPECTED_R}R (pos {position_in_range:.0%} in range)"
    
    def _check_not_post_impulse(self, df_1m: pd.DataFrame, atr_1m: float) -> tuple[bool, str]:
        """
        ANTI-IMPULSE FILTER: Block entries after impulsive candles
        
        If recent candles are > 1.2 ATR, we're entering AFTER the move.
        We want pullback/absorption, not chasing.
        
        Returns: (can_enter, reason)
        """
        if len(df_1m) < 5:
            return True, "Not enough 1m data"
        
        if atr_1m <= 0:
            return True, "Invalid ATR"
        
        # Check last 3 candles for impulsive moves
        IMPULSE_THRESHOLD = 1.2  # > 1.2 ATR = impulsive
        
        for i in range(-3, 0):
            candle_range = abs(df_1m['close'].iloc[i] - df_1m['open'].iloc[i])
            if candle_range > atr_1m * IMPULSE_THRESHOLD:
                return False, f"Post-impulse: Candle {i} range {candle_range:.2f} > {atr_1m * IMPULSE_THRESHOLD:.2f} (1.2 ATR)"
        
        return True, "No recent impulse detected"
    
    def _check_sweep_occurred(self, df_5m: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """
        SWEEP FILTER: Only enter if there was a liquidity sweep recently
        
        A sweep occurs when:
        - For LONG: Price went below recent lows (swept stops) then recovered
        - For SHORT: Price went above recent highs (swept stops) then recovered
        
        This captures the "stop hunt before expansion" pattern that precedes
        real moves, filtering out entries without prior liquidity grab.
        
        Returns: (sweep_occurred, reason)
        """
        if len(df_5m) < 15:
            return True, "Not enough data for sweep detection"
        
        # Look at last 10 candles for sweep patterns
        lookback = min(10, len(df_5m) - 5)
        
        # Get the range we're analyzing (excluding last 2 candles = current action)
        recent = df_5m.iloc[-lookback-2:-2]
        current = df_5m.iloc[-2:]
        
        if len(recent) < 5:
            return True, "Not enough history"
        
        # Find key levels to sweep
        if bias == 'LONG':
            # For LONG: We want to see a sweep below recent lows, then recovery
            recent_low = recent['low'].min()
            
            # Check if any of last 5 candles wicked below that low then closed above
            for i in range(-5, 0):
                candle = df_5m.iloc[i]
                low_wick = candle['low']
                close = candle['close']
                open_price = candle['open']
                
                # Sweep pattern: wick below recent low, close above it
                if low_wick < recent_low and close > recent_low:
                    # Additional: body should be bullish or at least not strongly bearish
                    if close >= open_price * 0.998:  # Allow small bearish body (0.2%)
                        return True, f"SWEEP detected: Low wick {low_wick:.2f} < {recent_low:.2f}, close {close:.2f} recovered"
            
            return False, f"No LONG sweep: Recent low {recent_low:.2f} not swept and recovered"
            
        else:  # SHORT
            # For SHORT: We want to see a sweep above recent highs, then rejection
            recent_high = recent['high'].max()
            
            # Check if any of last 5 candles wicked above that high then closed below
            for i in range(-5, 0):
                candle = df_5m.iloc[i]
                high_wick = candle['high']
                close = candle['close']
                open_price = candle['open']
                
                # Sweep pattern: wick above recent high, close below it
                if high_wick > recent_high and close < recent_high:
                    # Additional: body should be bearish or at least not strongly bullish
                    if close <= open_price * 1.002:  # Allow small bullish body (0.2%)
                        return True, f"SWEEP detected: High wick {high_wick:.2f} > {recent_high:.2f}, close {close:.2f} rejected"
            
            return False, f"No SHORT sweep: Recent high {recent_high:.2f} not swept and rejected"
    
    def _find_tp2_target(self) -> float:
        """Find TP2 target"""
        candles_5m = self.market_data.get_candles('5m')
        if not candles_5m:
            return 0
        
        df_5m = pd.DataFrame(candles_5m)
        
        if self.current_bias == 'LONG':
            return df_5m['high'].max()
        else:
            return df_5m['low'].min()
    
    def _check_bos_against_position(self, direction: str, candles_5m: list) -> bool:
        """
        Detecta Break of Structure (BOS) en contra de la posición
        
        BOS LONG (en contra de posición LONG):
        - Precio hace nuevo Lower Low (rompe último soporte)
        
        BOS SHORT (en contra de posición SHORT):
        - Precio hace nuevo Higher High (rompe última resistencia)
        
        Returns: True si hay BOS en contra
        """
        if not candles_5m or len(candles_5m) < 10:
            return False
        
        df = pd.DataFrame(candles_5m)
        
        # Solo mirar últimas 10 velas para BOS reciente
        recent = df.tail(10)
        
        if direction == 'LONG':
            # Para LONG: BOS = Lower Low (rompe mínimo anterior)
            # Buscar si el low más reciente es menor que todos los anteriores
            lows = recent['low'].values
            if len(lows) < 3:
                return False
            
            # Si el último low es menor que el mínimo de los 5 anteriores = BOS
            recent_low = lows[-1]
            previous_lows = lows[-6:-1]  # 5 velas anteriores
            if len(previous_lows) > 0:
                min_previous = min(previous_lows)
                if recent_low < min_previous:
                    logger.info(f"📉 BOS detectado (LONG): LL @ {recent_low:.2f} < {min_previous:.2f}")
                    return True
        
        else:  # SHORT
            # Para SHORT: BOS = Higher High (rompe máximo anterior)
            highs = recent['high'].values
            if len(highs) < 3:
                return False
            
            recent_high = highs[-1]
            previous_highs = highs[-6:-1]
            if len(previous_highs) > 0:
                max_previous = max(previous_highs)
                if recent_high > max_previous:
                    logger.info(f"📈 BOS detectado (SHORT): HH @ {recent_high:.2f} > {max_previous:.2f}")
                    return True
        
        return False
    
    async def emergency_close_all(self):
        """Emergency close all positions (market order)"""
        logger.critical("EMERGENCY POSITION CLOSE")
        
        try:
            position = self.risk_engine.position
            if position:
                # Get current price for PnL calculation
                current_price = 0.0
                candles_1m = self.market_data.get_candles('1m')
                if candles_1m and len(candles_1m) > 0:
                    current_price = candles_1m[-1]['close']
                
                # Calculate PnL
                if position.direction == 'LONG':
                    pnl = (current_price - position.entry_price) * position.size
                else:
                    pnl = (position.entry_price - current_price) * position.size
                
                # Market close via REST
                await self.execution.close_position_market(
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=position.size
                )
                
                # Record close in state machine
                self.state_machine.record_trade_close(pnl, pnl > 0)
                
                # Log to trade logger for backtest results
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': pnl,
                    'direction': position.direction,
                    'reason': 'EMERGENCY_CLOSE'
                })
                
                self.risk_engine.position = None
                
                logger.critical(f"Emergency close completed - PnL: {pnl:.2f}")
            
            # 🔒 LIMPIEZA COMPLETA independientemente de si había posición
            self._reset_trade_state("Emergency close")
        
        except Exception as e:
            logger.error(f"Error in emergency close: {e}", exc_info=True)
            # Limpia de todas formas
            self._reset_trade_state("Emergency close error")
    
    # NOTE: _open_position_from_order REMOVED - duplicated and unused
    # Position opening logic is in _handle_wait_fill_state after fill confirmation
    
    def get_status(self) -> dict:
        """Get engine status"""
        return {
            'state': str(self.state_machine.state),
            'current_bias': self.current_bias,
            'has_position': self.risk_engine.has_open_position(),
            'pending_order': self.pending_order_id is not None,
            'daily_trades': self.state_machine.daily_trades,
            'edge_state': self.edge_enforcement.state_machine.current_state.value if self.edge_enforcement else 'N/A',
            'regime': self.regime_classifier.current_regime.value if self.regime_classifier else 'N/A'
        }
    
    # ===== EDGE ENFORCEMENT INTEGRATION =====
    
    async def _update_edge_context(self):
        """Update edge context for pre-trade validation"""
        # Use market_time for consistency (live and backtest)
        market_time = self.market_data.get_market_time()
        if market_time is None:
            return
        
        # Only update periodically
        if self._last_context_update and (market_time - self._last_context_update).total_seconds() < self._context_update_interval:
            return
        
        try:
            candles_1h = self.market_data.get_candles('1h')
            if not candles_1h or len(candles_1h) < 60:
                return
            
            df_1h = pd.DataFrame(candles_1h)
            highs = df_1h['high'].values
            lows = df_1h['low'].values
            closes = df_1h['close'].values
            volumes = df_1h['volume'].values
            
            # Calculate ATR and history for Activity Regime analysis
            atr_values = calculate_atr(highs, lows, closes, 14)
            atr_current = atr_values[-1] if len(atr_values) > 0 else 0
            
            # Macro event check (simplified - no macro detected by default)
            is_macro_event_day = False
            if hasattr(self.external_data, 'macro') and self.external_data.macro:
                is_macro_event_day = self.external_data.macro.is_in_quiet_zone()
            
            # Update context via edge enforcement using new Activity Regime model
            # The new validate_context uses Activity Regime instead of session-based logic
            self._edge_context = self.edge_enforcement.edge_validator.validate_context(
                atr_current=atr_current,
                atr_history=atr_values,
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes,
                is_macro_event_day=is_macro_event_day,
                candle_minutes=60  # 1h candles
            )
            
            self._last_context_update = market_time
            
            if self._edge_context and not self._edge_context.is_valid():
                logger.warning(f"⚠️ Edge context invalid: {self._edge_context.invalid_reasons()}")
            else:
                logger.debug(f"✅ Edge context valid - Score: {self._edge_context.edge_score:.2f} ({self._edge_context.edge_regime})")
            
        except Exception as e:
            logger.error(f"Error updating edge context: {e}")
    
    async def _update_regime_classifier(self, current_price: float):
        """Update regime classifier with market data"""
        try:
            candles_1h = self.market_data.get_candles('1h')
            if not candles_1h or len(candles_1h) < 30:
                return
            
            df_1h = pd.DataFrame(candles_1h)
            highs = df_1h['high'].values
            lows = df_1h['low'].values
            closes = df_1h['close'].values
            volumes = df_1h['volume'].values
            
            # Calculate metrics for regime classification
            atr_values = calculate_atr(highs, lows, closes, 14)
            atr_5d = (atr_values[-1] - atr_values[-5]) / atr_values[-5] if atr_values[-5] > 0 else 0
            atr_20d = (atr_values[-1] - atr_values[-20]) / atr_values[-20] if len(atr_values) >= 20 and atr_values[-20] > 0 else 0
            
            # Session metrics
            session_range = highs[-1] - lows[-1]
            session_realized = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0
            
            # Expansion count
            avg_atr = np.mean(atr_values[-20:]) if len(atr_values) >= 20 else atr_values[-1]
            expansion_sizes = [(highs[i] - lows[i]) / avg_atr for i in range(-5, 0) if avg_atr > 0]
            expansion_count = sum(1 for s in expansion_sizes if s > 1.0)
            
            # Volume quality (simplified)
            vol_quality = 0.6  # Default moderate
            if len(volumes) >= 20:
                recent_vol = np.mean(volumes[-5:])
                avg_vol = np.mean(volumes[-20:])
                vol_quality = min(recent_vol / avg_vol, 1.0) if avg_vol > 0 else 0.5
            
            # Breakout success rate (simplified - use 0.6 default)
            breakout_success = 0.6
            
            # Price action consistency (simplified)
            price_consistency = 0.5
            if len(closes) >= 10:
                changes = np.diff(closes[-10:])
                same_direction = sum(1 for i in range(len(changes)-1) if changes[i] * changes[i+1] > 0)
                price_consistency = same_direction / (len(changes) - 1) if len(changes) > 1 else 0.5
            
            # Classify regime
            regime, confidence = self.regime_classifier.classify_regime(
                expansion_count=expansion_count,
                expansion_sizes=expansion_sizes,
                breakout_success_rate=breakout_success,
                atr_trend_5d=atr_5d,
                atr_trend_20d=atr_20d,
                session_range=session_range,
                session_realized_move=session_realized,
                volume_profile_quality=vol_quality,
                price_action_consistency=price_consistency,
                order_flow_imbalance_std=0.2  # Default stable
            )
            
            logger.debug(f"📊 Regime: {regime.value} (confidence: {confidence:.2f})")
            
        except Exception as e:
            logger.error(f"Error updating regime classifier: {e}")
    
    def _record_trade_for_edge_enforcement(self, 
                                           entry_price: float, 
                                           exit_price: float,
                                           entry_time: datetime,
                                           exit_time: datetime,
                                           reached_tp1: bool,
                                           reached_tp2: bool,
                                           is_be_stop: bool):
        """Record completed trade in edge enforcement for degradation monitoring"""
        try:
            self._trade_counter += 1
            
            # Calculate MFE/MAE from position tracking if available
            mfe = self._current_trade_mfe
            mae = self._current_trade_mae
            
            self.edge_enforcement.record_completed_trade(
                trade_id=self._trade_counter,
                entry_price=entry_price,
                exit_price=exit_price,
                entry_time=entry_time,
                exit_time=exit_time,
                mfe=mfe,
                mae=mae,
                reached_tp1=reached_tp1,
                reached_tp2=reached_tp2,
                is_be_stop=is_be_stop
            )
            
            # Reset tracking
            self._current_trade_mfe = 0.0
            self._current_trade_mae = 0.0
            self._current_trade_entry_time = None
            
            logger.info(f"📝 Trade #{self._trade_counter} recorded in edge enforcement")
            
        except Exception as e:
            logger.error(f"Error recording trade for edge enforcement: {e}")
