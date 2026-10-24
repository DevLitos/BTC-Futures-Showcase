"""Strategy Runtime: integrates strategies with the trading system.

This module bridges the domain strategies with the infrastructure layer,
connecting to the existing WebSocket feed and execution engine.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from src.application.services.candle_aggregator import CandleAggregator, Tick
from src.application.services.position_manager import PositionManager, PositionManagerConfig, PositionEvent
from src.application.services.signal_orchestrator import SignalOrchestrator, OrchestratorConfig, SignalEvent
from src.domain.models.candle import Candle
from src.domain.models.signal import Signal, SignalDirection
from src.domain.strategies import FVGTrendStrategy, IFVGStrategy, OrderBlockStrategy, Strategy


logger = logging.getLogger(__name__)


@dataclass
class StrategyRuntimeConfig:
    """Configuration for the strategy runtime."""
    symbol: str = "PF_XBTUSD"
    candle_intervals: list[int] = field(default_factory=lambda: [60, 300])  # 1m, 5m
    primary_interval: int = 300  # 5m for signal evaluation
    max_candles: int = 200
    enabled_strategies: list[str] = field(
        default_factory=lambda: ["FVG_TREND", "IFVG", "ORDER_BLOCK"]
    )
    paper_mode: bool = True
    position_size_btc: float = 0.001


class StrategyRuntime:
    """Runtime for executing trading strategies.
    
    Integrates:
    - CandleAggregator: tick → candles
    - SignalOrchestrator: candles → signals
    - PositionManager: signals → positions
    
    Connects to:
    - WebSocket feed (via EventBus)
    - Execution engine (for order placement)
    
    Example:
        runtime = StrategyRuntime(config, event_bus, executor)
        await runtime.start()
    """

    def __init__(
        self,
        config: StrategyRuntimeConfig,
        event_bus: Any,  # EventBus from shared.events
        executor: Any | None = None,  # Optional RESTExecutionEngine
    ) -> None:
        """Initialize strategy runtime.
        
        Args:
            config: Runtime configuration
            event_bus: Event bus for WebSocket events
            executor: Optional execution engine for live trading
        """
        self._config = config
        self._event_bus = event_bus
        self._executor = executor
        self._running = False
        
        # Initialize strategies
        self._strategies = self._create_strategies()
        
        # Initialize services
        self._aggregator = CandleAggregator(
            symbol=config.symbol,
            intervals=config.candle_intervals,
            max_candles=config.max_candles
        )
        
        self._orchestrator = SignalOrchestrator(
            strategies=self._strategies,
            buffer_size=config.max_candles,
            config=OrchestratorConfig(
                max_signals_per_eval=1,
                min_signal_interval_seconds=300  # 5 min between signals
            )
        )
        
        self._position_manager = PositionManager(
            config=PositionManagerConfig(
                max_positions=1,
                max_risk_per_position_pct=2.0,
                trailing_stop_activation_r=1.0
            )
        )
        
        # Wire up callbacks
        self._setup_callbacks()
        
        # Stats
        self._tick_count = 0
        self._candle_count = 0
        self._signal_count = 0

    def _create_strategies(self) -> list[Strategy]:
        """Create enabled strategies."""
        strategies: list[Strategy] = []
        enabled = self._config.enabled_strategies
        
        if "FVG_TREND" in enabled:
            strategies.append(FVGTrendStrategy())
        if "IFVG" in enabled:
            strategies.append(IFVGStrategy())
        if "ORDER_BLOCK" in enabled:
            strategies.append(OrderBlockStrategy())
        
        return strategies

    def _setup_callbacks(self) -> None:
        """Wire up service callbacks."""
        # When primary interval candle completes, evaluate strategies
        self._aggregator.on_candle(
            self._config.primary_interval,
            self._on_candle_complete
        )
        
        # When signal is generated, handle it
        self._orchestrator.on_signal(self._on_signal)
        
        # When position changes, log it
        self._position_manager.on_position(self._on_position_event)

    async def start(self) -> None:
        """Start the strategy runtime."""
        if self._running:
            return
        
        self._running = True
        logger.info(f"Strategy runtime starting - {len(self._strategies)} strategies enabled")
        logger.info(f"Paper mode: {self._config.paper_mode}")
        
        # Subscribe to WebSocket events
        self._event_bus.subscribe("ws_message", self._on_ws_message)
        
        logger.info("Strategy runtime started")

    async def stop(self) -> None:
        """Stop the strategy runtime."""
        if not self._running:
            return
        
        self._running = False
        
        # Unsubscribe from events
        self._event_bus.unsubscribe("ws_message", self._on_ws_message)
        
        # Flush any pending candles
        self._aggregator.flush()
        
        logger.info(f"Strategy runtime stopped - processed {self._tick_count} ticks, {self._candle_count} candles, {self._signal_count} signals")

    def _on_ws_message(self, event: Any) -> None:
        """Handle WebSocket message event."""
        try:
            data = event.data if hasattr(event, 'data') else event
            
            # Parse trade data
            if isinstance(data, dict):
                feed = data.get("feed")
                
                if feed == "trade":
                    self._process_trade(data)
                elif feed == "trade_snapshot":
                    trades = data.get("trades", [])
                    for trade in trades[-10:]:  # Process last 10 trades
                        self._process_trade_data(trade)
        except Exception as e:
            logger.error(f"Error processing WS message: {e}")

    def _process_trade(self, data: dict) -> None:
        """Process a single trade message."""
        try:
            price = float(data.get("price", 0))
            qty = float(data.get("qty", 0))
            side = data.get("side", "")
            time_val = data.get("time")
            
            if price <= 0:
                return
            
            # Parse timestamp
            if isinstance(time_val, (int, float)):
                timestamp = datetime.utcfromtimestamp(time_val / 1000)
            else:
                timestamp = datetime.utcnow()
            
            tick = Tick(
                timestamp=timestamp,
                price=price,
                volume=qty,
                side=side
            )
            
            self._process_tick(tick)
        except Exception as e:
            logger.debug(f"Error processing trade: {e}")

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
                timestamp = datetime.utcfromtimestamp(time_val / 1000)
            else:
                timestamp = datetime.utcnow()
            
            tick = Tick(
                timestamp=timestamp,
                price=price,
                volume=qty,
                side=side
            )
            
            self._process_tick(tick)
        except Exception:
            pass

    def _process_tick(self, tick: Tick) -> None:
        """Process a tick through the aggregator."""
        self._tick_count += 1
        
        # Add to aggregator (may complete candles)
        self._aggregator.add_tick(tick)
        
        # Update positions with current price
        closed = self._position_manager.update_price(tick.price, tick.timestamp)
        
        for pos in closed:
            logger.info(f"Position closed: {pos.position_id} - PnL: {pos.realized_pnl:.2f}")

    def _on_candle_complete(self, candle: Candle) -> None:
        """Handle completed candle."""
        self._candle_count += 1
        
        logger.debug(f"Candle complete: {candle.timestamp} O={candle.open:.2f} H={candle.high:.2f} L={candle.low:.2f} C={candle.close:.2f}")
        
        # Add to orchestrator for strategy evaluation
        signals = self._orchestrator.add_candle(candle)
        
        # Signals are handled via callback

    def _on_signal(self, event: SignalEvent) -> None:
        """Handle signal from orchestrator."""
        self._signal_count += 1
        signal = event.signal
        
        logger.info(
            f"SIGNAL: {signal.direction.value} from {signal.strategy_name} "
            f"@ {signal.entry_price:.2f} (SL: {signal.stop_loss:.2f}, TP: {signal.take_profit:.2f}) "
            f"RR: {signal.risk_reward_ratio:.2f}"
        )
        
        # Check if we can open a position
        can_open, reason = self._position_manager.can_open_position(signal)
        
        if not can_open:
            logger.info(f"Cannot open position: {reason}")
            return
        
        # In paper mode, just track the position
        if self._config.paper_mode:
            position = self._position_manager.open_from_signal(
                signal,
                size=self._config.position_size_btc
            )
            if position:
                logger.info(f"Paper position opened: {position.position_id}")
        else:
            # Live mode - execute via executor
            asyncio.create_task(self._execute_signal(signal))

    async def _execute_signal(self, signal: Signal) -> None:
        """Execute a signal via the execution engine."""
        if not self._executor:
            logger.warning("No executor available for live trading")
            return
        
        try:
            side = "buy" if signal.direction == SignalDirection.LONG else "sell"
            
            # Place market order
            result = await self._executor.place_order(
                symbol=self._config.symbol,
                side=side,
                size=self._config.position_size_btc,
                order_type="market"
            )
            
            if result.get("success"):
                logger.info(f"Order placed: {result}")
                
                # Track position
                self._position_manager.open_from_signal(
                    signal,
                    size=self._config.position_size_btc,
                    position_id=result.get("order_id")
                )
            else:
                logger.error(f"Order failed: {result}")
                
        except Exception as e:
            logger.error(f"Execution error: {e}")

    def _on_position_event(self, event: PositionEvent) -> None:
        """Handle position event."""
        pos = event.position
        
        if event.event_type == "opened":
            logger.info(f"Position OPENED: {pos.position_id} {pos.side.value} @ {pos.entry_price:.2f}")
        elif event.event_type == "closed":
            pnl = event.details.get("pnl", 0)
            r_mult = event.details.get("r_multiple", 0)
            reason = event.details.get("reason", "unknown")
            logger.info(f"Position CLOSED: {pos.position_id} - Reason: {reason}, PnL: ${pnl:.2f}, R: {r_mult:.2f}R")
        elif event.event_type == "updated":
            update_type = event.details.get("update_type", "")
            if update_type == "trailing_stop":
                logger.info(f"Trailing stop updated: {pos.position_id} - New SL: {pos.stop_loss:.2f}")

    def get_stats(self) -> dict:
        """Get runtime statistics."""
        return {
            "ticks_processed": self._tick_count,
            "candles_completed": self._candle_count,
            "signals_generated": self._signal_count,
            "positions": self._position_manager.get_stats(),
            "strategies": [s.name for s in self._strategies],
            "running": self._running
        }

    def get_candles(self, interval: int, count: int | None = None) -> list[Candle]:
        """Get candles for analysis."""
        return self._aggregator.get_candles(interval, count)
