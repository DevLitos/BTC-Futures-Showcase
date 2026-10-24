"""Trading runtime with ICT strategies integration."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from src.application.runtime.settings import RuntimeSettings, RunMode
from src.application.services.candle_aggregator import CandleAggregator, Tick
from src.application.services.position_manager import PositionManager, PositionManagerConfig, PositionEvent
from src.application.services.risk_manager import RiskManager, RiskLimits
from src.application.services.signal_orchestrator import SignalOrchestrator, OrchestratorConfig, SignalEvent
from src.application.services.trade_journal import TradeJournal
from src.domain.models.position import Position, PositionSide, PositionStatus
from src.domain.models.signal import Signal, SignalDirection, SignalType, SignalStrength
from src.domain.strategies import FVGTrendStrategy, IFVGStrategy, OrderBlockStrategy, Strategy
from src.infrastructure.exchanges.kraken.execution import RESTExecutionEngine, OrderRequest, OrderResult
from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.monitoring.metrics import (
    BUILD_INFO,
    HEALTH_CIRCUIT_FAILURES,
    HEALTH_CIRCUIT_OPEN,
    HEALTH_EXCHANGE_REACHABLE,
    HEALTH_WS_CONNECTED,
    WS_MESSAGES_TOTAL,
    WS_RECONNECTS_TOTAL,
    start_metrics_server,
)
from src.shared.events.event_bus import EventBus, RuntimeEvent
from src.shared.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = logging.getLogger("trading")


class Runtime:
    """Trading runtime with ICT strategies.
    
    PRODUCTION-READY: Just run `uv run main.py`
    
    Features:
    - WebSocket feed with exponential backoff reconnection
    - Candle aggregation from ticks (1m, 5m)
    - ICT strategy evaluation (FVG, IFVG, Order Block)
    - Position management with trailing stops
    - Risk manager with kill-switch
    - Frequent logging for monitoring
    """

    def __init__(self) -> None:
        self.settings = RuntimeSettings.from_env()
        self._stop = asyncio.Event()
        self.exchange_name = self.settings.exchange_name
        self.ws_symbol = self.settings.ws_symbol
        self.health_symbol = self.settings.health_symbol
        self.event_bus = EventBus()
        self.ws = KrakenFuturesWebSocket(
            [self.ws_symbol], 
            exchange_name=self.exchange_name, 
            event_bus=self.event_bus
        )
        self.rest = RESTExecutionEngine(
            self.exchange_name,
            {
                "apiKey": self.settings.kraken_api_key,
                "secret": self.settings.kraken_private_key,
            },
        )
        self.health_circuit = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=self.settings.health_cb_failure_threshold,
                recovery_timeout_seconds=self.settings.health_cb_recovery_seconds,
            )
        )
        self._tasks: list[asyncio.Task] = []
        self._last_ws_messages = 0
        self._last_ws_reconnects = 0
        self._last_exchange_health_error: str | None = None

        # Risk management and persistence
        self._journal = TradeJournal()
        self._risk_manager = RiskManager(RiskLimits(
            max_daily_loss_usd=100.0,
            max_daily_trades=10,
            max_position_size_btc=0.01,
            max_drawdown_pct=5.0,
            cooldown_after_loss_seconds=300
        ))

        # Trading components
        self._strategies = self._create_strategies()
        self._aggregator = CandleAggregator(
            symbol=self.ws_symbol,
            intervals=list(self.settings.candle_intervals),
            max_candles=200
        )
        self._orchestrator = SignalOrchestrator(
            strategies=self._strategies,
            buffer_size=200,
            config=OrchestratorConfig(
                max_signals_per_eval=1,
                min_signal_interval_seconds=300
            )
        )
        self._position_manager = PositionManager(
            config=PositionManagerConfig(
                max_positions=1,
                max_risk_per_position_pct=2.0,
                trailing_stop_activation_r=1.0,
                position_timeout_minutes=240
            )
        )
        
        # Wire up callbacks
        self._aggregator.on_candle(self.settings.primary_interval, self._on_candle_complete)
        self._orchestrator.on_signal(self._on_signal)
        self._position_manager.on_position(self._on_position_event)

        # Stats for logging
        self._tick_count = 0
        self._candle_count = 0
        self._signal_count = 0
        self._last_price = 0.0
        self._last_log_time = time.time()
        self._start_time = time.time()

        # Event bus subscriptions
        self.event_bus.subscribe("ws_connected", self._on_ws_connected)
        self.event_bus.subscribe("ws_disconnected", self._on_ws_disconnected)
        self.event_bus.subscribe("ws_reconnect", self._on_ws_reconnect)
        self.event_bus.subscribe("ws_message", self._on_ws_message)

    def _create_strategies(self) -> list[Strategy]:
        """Create enabled strategies based on settings."""
        strategies: list[Strategy] = []
        enabled = self.settings.enabled_strategies
        
        if "FVG_TREND" in enabled:
            strategies.append(FVGTrendStrategy())
            logger.info("Strategy enabled: FVG_TREND")
        if "IFVG" in enabled:
            strategies.append(IFVGStrategy())
            logger.info("Strategy enabled: IFVG")
        if "ORDER_BLOCK" in enabled:
            strategies.append(OrderBlockStrategy())
            logger.info("Strategy enabled: ORDER_BLOCK")
        
        return strategies

    async def _on_ws_connected(self, event: RuntimeEvent) -> None:
        logger.info("ws connected: %s", event.payload.get("exchange", self.exchange_name))

    async def _on_ws_disconnected(self, event: RuntimeEvent) -> None:
        logger.warning("ws disconnected: %s", event.payload.get("exchange", self.exchange_name))

    async def _on_ws_reconnect(self, event: RuntimeEvent) -> None:
        logger.warning(
            "ws reconnect: exchange=%s count=%s error=%s",
            event.payload.get("exchange", self.exchange_name),
            event.payload.get("reconnect_count", 0),
            event.payload.get("error", "unknown"),
        )

    async def _on_ws_message(self, event: RuntimeEvent) -> None:
        """Process WebSocket messages for trading."""
        try:
            data = event.payload
            if not isinstance(data, dict):
                return
            
            feed = data.get("feed")
            
            if feed == "trade":
                self._process_trade(data)
            elif feed == "trade_snapshot":
                trades = data.get("trades", [])
                for trade in trades[-5:]:  # Process recent trades
                    self._process_trade_data(trade)
        except Exception as e:
            logger.debug("Error processing ws message: %s", e)

    def _process_trade(self, data: dict) -> None:
        """Process a single trade message."""
        try:
            price = float(data.get("price", 0))
            qty = float(data.get("qty", 0))
            side = data.get("side", "")
            time_val = data.get("time")
            
            if price <= 0:
                return
            
            if isinstance(time_val, (int, float)):
                timestamp = datetime.fromtimestamp(time_val / 1000, tz=timezone.utc)
            else:
                timestamp = datetime.now(timezone.utc)
            
            tick = Tick(timestamp=timestamp, price=price, volume=qty, side=side)
            self._process_tick(tick)
        except Exception as e:
            logger.debug("Error processing trade: %s", e)

    def _process_trade_data(self, trade: dict) -> None:
        """Process trade from snapshot."""
        try:
            price = float(trade.get("price", 0))
            qty = float(trade.get("qty", 0))
            side = trade.get("side", "")
            time_val = trade.get("time")
            
            if price <= 0:
                return
            
            if isinstance(time_val, (int, float)):
                timestamp = datetime.fromtimestamp(time_val / 1000, tz=timezone.utc)
            else:
                timestamp = datetime.now(timezone.utc)
            
            tick = Tick(timestamp=timestamp, price=price, volume=qty, side=side)
            self._process_tick(tick)
        except Exception:
            pass

    def _process_tick(self, tick: Tick) -> None:
        """Process a tick through the system."""
        self._tick_count += 1
        self._last_price = tick.price
        
        # Log tick if enabled
        if self.settings.log_every_tick:
            logger.debug("[TICK] %.2f vol=%.4f %s", tick.price, tick.volume, tick.side)
        
        # Periodic status log (every N seconds)
        now = time.time()
        if now - self._last_log_time >= self.settings.log_interval_seconds:
            uptime = int(now - self._start_time)
            pos_count = len(self._position_manager.open_positions)
            candle_buffers = sum(len(b) for b in self._aggregator._candles.values())
            logger.info(
                "[STATUS] price=%.2f ticks=%d candles=%d signals=%d positions=%d buffer=%d uptime=%ds",
                self._last_price, self._tick_count, self._candle_count, 
                self._signal_count, pos_count, candle_buffers, uptime
            )
            self._last_log_time = now
        
        # Add to aggregator
        self._aggregator.add_tick(tick)
        
        # Update positions with current price
        if self.settings.trading_enabled:
            closed = self._position_manager.update_price(tick.price, tick.timestamp)
            for pos in closed:
                pnl = pos.realized_pnl or 0
                self._risk_manager.record_trade_result(pnl)
                self._journal.log_trade(pos, "closed", {"trigger": "price_update"})
                self._journal.save_open_positions(self._position_manager.open_positions)
                logger.info(
                    "[CLOSED] %s reason=%s pnl=$%.2f R=%.2fR",
                    pos.position_id, pos.exit_reason.value if pos.exit_reason else "unknown",
                    pnl, pos.realized_r or 0
                )

    def _on_candle_complete(self, candle) -> None:
        """Handle completed candle."""
        self._candle_count += 1
        
        # Log candle with more detail - convert to local time for display
        local_time = candle.timestamp.astimezone()  # Convert to local timezone
        direction = "▲" if candle.close > candle.open else "▼"
        change_pct = ((candle.close - candle.open) / candle.open) * 100
        logger.info(
            "[CANDLE] %s %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.4f (%.2f%%)",
            local_time.strftime("%H:%M"), direction,
            candle.open, candle.high, candle.low, candle.close, 
            candle.volume, change_pct
        )
        
        if self.settings.trading_enabled:
            self._orchestrator.add_candle(candle)

    def _on_signal(self, event: SignalEvent) -> None:
        """Handle signal from orchestrator."""
        self._signal_count += 1
        signal = event.signal
        
        logger.info(
            "[SIGNAL] %s from %s @ %.2f SL=%.2f TP=%.2f RR=%.2f strength=%s",
            signal.direction.value, signal.strategy_name,
            signal.entry_price, signal.stop_loss, signal.take_profit,
            signal.risk_reward_ratio, signal.strength.value
        )
        
        # Log signal to journal
        self._journal.log_signal(signal, "generated")
        
        if not self.settings.trading_enabled:
            logger.warning("[SIGNAL] Trading disabled, ignoring signal")
            self._journal.log_signal(signal, "ignored", "trading_disabled")
            return
        
        # Check risk manager
        can_trade, risk_reason = self._risk_manager.can_trade()
        if not can_trade:
            logger.warning("[SIGNAL] Risk blocked: %s", risk_reason)
            self._journal.log_signal(signal, "blocked", risk_reason)
            return
        
        # Check position manager
        can_open, reason = self._position_manager.can_open_position(signal)
        if not can_open:
            logger.info("[SIGNAL] Cannot open: %s", reason)
            self._journal.log_signal(signal, "rejected", reason)
            return
        
        # Validate position size
        size_valid, size_reason = self._risk_manager.validate_position_size(self.settings.position_size_btc)
        if not size_valid:
            logger.warning("[SIGNAL] Invalid size: %s", size_reason)
            self._journal.log_signal(signal, "rejected", size_reason)
            return
        
        # Execute order (always live - no paper mode)
        asyncio.create_task(self._execute_signal(signal))

    async def _execute_signal(self, signal: Signal) -> None:
        """Execute a signal via the execution engine."""
        try:
            side = "buy" if signal.direction == SignalDirection.LONG else "sell"
            
            request = OrderRequest(
                symbol=self.ws_symbol,
                side=side,
                amount=self.settings.position_size_btc,
                order_type="market",
                client_order_id=f"ict_{uuid4().hex[:12]}"
            )
            
            logger.info("[ORDER] Placing %s %s %.4f @ market (id=%s)",
                       request.side, request.symbol, request.amount, request.client_order_id)
            
            result = await self.rest.place_order(request)
            
            if result.success:
                logger.info("[ORDER] Executed: id=%s filled=%.4f @ %.2f",
                           result.order_id, result.filled_amount, result.filled_price)
                position = self._position_manager.open_from_signal(
                    signal,
                    size=self.settings.position_size_btc,
                    position_id=result.order_id or request.client_order_id
                )
                if position:
                    self._journal.log_trade(position, "opened", {
                        "order_id": result.order_id,
                        "client_order_id": result.client_order_id,
                        "filled_amount": result.filled_amount,
                        "filled_price": result.filled_price
                    })
                    self._journal.save_open_positions(self._position_manager.open_positions)
            else:
                logger.error("[ORDER] Failed: %s", result.error)
                self._journal.log_signal(signal, "execution_failed", result.error)
        except Exception as e:
            logger.error("[ORDER] Error: %s", e)
            self._journal.log_signal(signal, "execution_error", str(e))

    def _on_position_event(self, event: PositionEvent) -> None:
        """Handle position event."""
        pos = event.position
        
        if event.event_type == "opened":
            logger.info("[POSITION] OPENED %s %s @ %.2f SL=%.2f TP=%.2f", 
                       pos.position_id, pos.side.value, pos.entry_price, pos.stop_loss, pos.take_profit)
        elif event.event_type == "closed":
            pnl = event.details.get("pnl", 0)
            r_mult = event.details.get("r_multiple", 0)
            reason = event.details.get("reason", "unknown")
            logger.info("[POSITION] CLOSED %s %s pnl=$%.2f R=%.2fR", pos.position_id, reason, pnl, r_mult)
        elif event.event_type == "updated":
            update_type = event.details.get("update_type", "")
            if update_type == "trailing_stop":
                logger.info("[POSITION] Trailing SL: %s → %.2f", pos.position_id, pos.stop_loss)

    def _signal(self, signum: int, _frame: object) -> None:
        if self._stop.is_set():
            return
        logger.info("signal=%s stopping", signum)
        self._stop.set()

    async def _ws_health_loop(self) -> None:
        while not self._stop.is_set():
            HEALTH_WS_CONNECTED.labels(exchange=self.exchange_name).set(1 if self.ws.connected else 0)
            total_messages = self.ws.message_count
            new_messages = total_messages - self._last_ws_messages
            if new_messages > 0:
                WS_MESSAGES_TOTAL.labels(exchange=self.exchange_name, feed="trade").inc(new_messages)
            self._last_ws_messages = total_messages

            total_reconnects = self.ws.reconnect_count
            new_reconnects = total_reconnects - self._last_ws_reconnects
            if new_reconnects > 0:
                WS_RECONNECTS_TOTAL.labels(exchange=self.exchange_name, status="reconnect").inc(new_reconnects)
            self._last_ws_reconnects = total_reconnects
            await asyncio.sleep(5)

    async def _exchange_health_loop(self) -> None:
        base_interval = self.settings.health_interval_seconds
        max_interval = self.settings.health_max_interval_seconds
        current_interval = base_interval
        while not self._stop.is_set():
            HEALTH_CIRCUIT_OPEN.labels(exchange=self.exchange_name).set(1 if self.health_circuit.is_open else 0)
            HEALTH_CIRCUIT_FAILURES.labels(exchange=self.exchange_name).set(self.health_circuit.failures)
            if not self.health_circuit.allow_request():
                HEALTH_EXCHANGE_REACHABLE.labels(exchange=self.exchange_name).set(0)
                await asyncio.sleep(min(current_interval, max_interval))
                continue
            try:
                await self.rest.fetch_ticker(self.health_symbol)
                HEALTH_EXCHANGE_REACHABLE.labels(exchange=self.exchange_name).set(1)
                self.health_circuit.on_success()
                HEALTH_CIRCUIT_OPEN.labels(exchange=self.exchange_name).set(0)
                HEALTH_CIRCUIT_FAILURES.labels(exchange=self.exchange_name).set(0)
                current_interval = base_interval
                if self._last_exchange_health_error is not None:
                    logger.info("exchange health recovered: %s", self.health_symbol)
                    self._last_exchange_health_error = None
            except Exception as exc:
                HEALTH_EXCHANGE_REACHABLE.labels(exchange=self.exchange_name).set(0)
                self.health_circuit.on_failure()
                HEALTH_CIRCUIT_OPEN.labels(exchange=self.exchange_name).set(1 if self.health_circuit.is_open else 0)
                HEALTH_CIRCUIT_FAILURES.labels(exchange=self.exchange_name).set(self.health_circuit.failures)
                current_interval = min(max_interval, max(base_interval, current_interval * 2))
                error_msg = str(exc)
                if self._last_exchange_health_error != error_msg:
                    logger.warning("exchange health check failed (%s): %s", self.health_symbol, exc)
                    self._last_exchange_health_error = error_msg
            await asyncio.sleep(current_interval)

    async def _stats_loop(self) -> None:
        """Periodically log trading stats."""
        while not self._stop.is_set():
            await asyncio.sleep(300)  # Every 5 minutes
            stats = self._position_manager.get_stats()
            logger.info(
                "Stats: ticks=%d candles=%d signals=%d trades=%d win_rate=%.1f%% pnl=$%.2f",
                self._tick_count, self._candle_count, self._signal_count,
                stats["total_trades"], stats["win_rate"], stats["total_pnl"]
            )

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task is None:
            return
        if task.done():
            with suppress(Exception):
                task.result()
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _shutdown(self) -> None:
        # Flush candles
        self._aggregator.flush()
        
        # Save state for recovery
        self._journal.save_open_positions(self._position_manager.open_positions)
        self._journal.save_state({
            "tick_count": self._tick_count,
            "candle_count": self._candle_count,
            "signal_count": self._signal_count,
            "last_price": self._last_price,
            "risk_status": self._risk_manager.get_status(),
        })
        
        # Log final stats
        stats = self._position_manager.get_stats()
        risk_status = self._risk_manager.get_status()
        logger.info(
            "Final stats: ticks=%d candles=%d signals=%d trades=%d win_rate=%.1f%% pnl=$%.2f",
            self._tick_count, self._candle_count, self._signal_count,
            stats["total_trades"], stats["win_rate"], stats["total_pnl"]
        )
        logger.info("Risk status: daily_pnl=$%.2f daily_trades=%d killed=%s",
                   risk_status["daily_pnl"], risk_status["daily_trades"], risk_status["killed"])
        
        for task in self._tasks:
            await self._cancel_task(task)
        self._tasks.clear()
        await self.ws.disconnect()
        await self.rest.close()
        HEALTH_WS_CONNECTED.labels(exchange=self.exchange_name).set(0)
        HEALTH_EXCHANGE_REACHABLE.labels(exchange=self.exchange_name).set(0)
        HEALTH_CIRCUIT_OPEN.labels(exchange=self.exchange_name).set(0)
        HEALTH_CIRCUIT_FAILURES.labels(exchange=self.exchange_name).set(0)

    def _recover_state(self) -> None:
        """Recover state from previous run."""
        # Load saved state
        state = self._journal.load_state()
        if state:
            logger.info("Recovering state from %s", state.get("saved_at"))
            self._tick_count = state.get("tick_count", 0)
            self._candle_count = state.get("candle_count", 0)
            self._signal_count = state.get("signal_count", 0)
        
        # Load open positions from journal
        saved_positions = self._journal.load_open_positions()
        if saved_positions:
            logger.warning("Found %d saved positions - will sync with exchange", len(saved_positions))
            for pos_data in saved_positions:
                logger.warning("  - %s: %s %s @ %.2f (SL: %.2f, TP: %.2f)",
                             pos_data.get("position_id"),
                             pos_data.get("side"),
                             pos_data.get("symbol"),
                             pos_data.get("entry_price"),
                             pos_data.get("stop_loss"),
                             pos_data.get("take_profit"))

    async def _sync_positions_with_exchange(self) -> None:
        """Sync local positions with exchange on startup.
        
        Critical for production: ensures our state matches exchange reality.
        """
        if not self.settings.trading_enabled:
            logger.info("[SYNC] Skipped (trading disabled)")
            return
        
        try:
            logger.info("[SYNC] Fetching positions from exchange...")
            exchange_positions = await self.rest.fetch_positions(self.ws_symbol)
            
            if not exchange_positions:
                logger.info("[SYNC] No open positions on exchange")
                if self._position_manager.open_positions:
                    logger.warning("[SYNC] Local positions exist but exchange has none - clearing")
                    for pos in list(self._position_manager.open_positions):
                        self._position_manager.close_position(pos.position_id, pos.entry_price, "sync_cleared")
                    self._journal.save_open_positions([])
                return
            
            for ex_pos in exchange_positions:
                logger.info("[SYNC] Exchange: %s %s size=%.4f entry=%.2f pnl=$%.2f",
                           ex_pos.symbol, ex_pos.side, ex_pos.size, 
                           ex_pos.entry_price, ex_pos.unrealized_pnl)
                
                # Check if we have this position locally
                local_pos = None
                for pos in self._position_manager.open_positions:
                    if pos.side.value.lower() == ex_pos.side.lower():
                        local_pos = pos
                        break
                
                if local_pos is None:
                    logger.warning("[SYNC] Unknown exchange position - manual review needed")
                    self._journal.log_signal(
                        Signal(
                            timestamp=datetime.now(timezone.utc),
                            direction=SignalDirection.LONG if ex_pos.side == "long" else SignalDirection.SHORT,
                            signal_type=SignalType.MANUAL,
                            entry_price=ex_pos.entry_price,
                            stop_loss=ex_pos.entry_price * 0.98,
                            take_profit=ex_pos.entry_price * 1.04,
                            strength=SignalStrength.MODERATE,
                            strategy_name="EXCHANGE_SYNC"
                        ),
                        "sync_detected",
                        f"Size: {ex_pos.size}, PnL: ${ex_pos.unrealized_pnl:.2f}"
                    )
                else:
                    logger.info("[SYNC] Position confirmed: %s", local_pos.position_id)
                    
        except Exception as e:
            logger.error("[SYNC] Failed: %s", e)
            logger.warning("[SYNC] MANUAL VERIFICATION REQUIRED")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)

    async def run(self) -> None:
        """Run the trading system."""
        mode = self.settings.mode.value
        trading_status = "ON" if self.settings.trading_enabled else "OFF"
        BUILD_INFO.info({"mode": mode, "exchange": self.exchange_name})
        
        logger.info("=" * 70)
        logger.info("  ICT TRADING SYSTEM - BTC FUTURES")
        logger.info("=" * 70)
        logger.info("Exchange: %s | Symbol: %s", self.exchange_name, self.ws_symbol)
        logger.info("Mode: %s | Trading: %s", mode.upper(), trading_status)
        logger.info("Strategies: %s", ", ".join(s.name for s in self._strategies))
        logger.info("Position: %.4f BTC | Intervals: %s (primary=%ds)", 
                   self.settings.position_size_btc, 
                   self.settings.candle_intervals, 
                   self.settings.primary_interval)
        logger.info("Risk: max_loss=$%.2f max_trades=%d cooldown=%ds",
                   self._risk_manager._limits.max_daily_loss_usd,
                   self._risk_manager._limits.max_daily_trades,
                   self._risk_manager._limits.cooldown_after_loss_seconds)
        logger.info("Logging: status every %ds, tick_logs=%s",
                   self.settings.log_interval_seconds,
                   self.settings.log_every_tick)
        logger.info("=" * 70)
        
        # Recover state
        self._recover_state()
        
        # Sync with exchange (only in live mode)
        if self.settings.mode == RunMode.LIVE:
            await self._sync_positions_with_exchange()
        
        try:
            start_metrics_server(self.settings.metrics_port)
            logger.info("[METRICS] Server started on port %d", self.settings.metrics_port)
        except OSError as exc:
            logger.warning("[METRICS] Unavailable on port %s: %s", self.settings.metrics_port, exc)
        
        self._install_signal_handlers()
        logger.info("[SYSTEM] Signal handlers installed, starting WebSocket...")

        ws_task = asyncio.create_task(self.ws.connect(), name="ws-connect")
        ws_health_task = asyncio.create_task(self._ws_health_loop(), name="ws-health")
        exchange_health_task = asyncio.create_task(self._exchange_health_loop(), name="exchange-health")
        stats_task = asyncio.create_task(self._stats_loop(), name="stats")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop-wait")
        self._tasks = [ws_task, ws_health_task, exchange_health_task, stats_task]
        all_tasks = {ws_task, ws_health_task, exchange_health_task, stats_task, stop_task}

        try:
            while not self._stop.is_set():
                done, _ = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    break
                for task in done:
                    if task is stop_task:
                        continue
                    task_name = task.get_name()
                    try:
                        task.result()
                        logger.error("critical task ended unexpectedly: %s", task_name)
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:
                        logger.exception("critical task crashed: %s | error=%s", task_name, exc)
                    self._stop.set()
                    break
        finally:
            await self._cancel_task(stop_task)
            await self._shutdown()
