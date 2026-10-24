"""Kraken websocket connector with production-grade reconnection."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import dataclass
from typing import Any

import websockets

from src.infrastructure.exchanges.symbol_mapper import SymbolMapper
from src.shared.events.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class BackoffConfig:
    """Exponential backoff configuration."""
    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.1  # Random jitter factor


class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection."""
    
    def __init__(self, config: BackoffConfig | None = None) -> None:
        self._config = config or BackoffConfig()
        self._attempt = 0
        self._consecutive_failures = 0
    
    def next_delay(self) -> float:
        """Calculate next delay with exponential backoff and jitter."""
        delay = min(
            self._config.initial_delay * (self._config.multiplier ** self._attempt),
            self._config.max_delay
        )
        # Add jitter: ±jitter%
        jitter = delay * self._config.jitter * (2 * random.random() - 1)
        self._attempt += 1
        self._consecutive_failures += 1
        return max(0.1, delay + jitter)
    
    def reset(self) -> None:
        """Reset backoff after successful connection."""
        self._attempt = 0
        self._consecutive_failures = 0
    
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


class KrakenFuturesWebSocket:
    """WebSocket connector with exponential backoff reconnection.
    
    Production features:
    - Exponential backoff with jitter on reconnection
    - Automatic subscription management
    - Message counting and trade buffering
    - Event bus integration for decoupled architecture
    """
    
    WS_URL = "wss://futures.kraken.com/ws/v1"
    
    def __init__(
        self, 
        symbols: list[str] | None = None, 
        exchange_name: str = "krakenfutures", 
        event_bus: EventBus | None = None,
        backoff_config: BackoffConfig | None = None
    ):
        self.symbols = symbols or ["BTC/USD"]
        self.exchange_name = exchange_name
        self.event_bus = event_bus
        self.ws: Any | None = None
        self._shutdown = False
        self.trade_buffer: deque[dict[str, Any]] = deque(maxlen=1000)
        self.reconnect_count = 0
        self.message_count = 0
        self._backoff = ExponentialBackoff(backoff_config)
        self._last_message_time: float = 0

    @property
    def connected(self) -> bool:
        return self.ws is not None and not getattr(self.ws, "closed", False)

    async def connect(self) -> None:
        """Connect with exponential backoff on failures."""
        while not self._shutdown:
            try:
                async with websockets.connect(
                    self.WS_URL, 
                    ping_interval=20, 
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self.ws = ws
                    self._backoff.reset()  # Reset backoff on successful connection
                    
                    if self.event_bus is not None:
                        await self.event_bus.publish("ws_connected", {
                            "exchange": self.exchange_name,
                            "reconnect_count": self.reconnect_count
                        })
                    
                    # Subscribe to all symbols
                    for symbol in self.symbols:
                        product = SymbolMapper.to_ws_symbol(self.exchange_name, symbol)
                        await ws.send(json.dumps({
                            "event": "subscribe", 
                            "feed": "trade", 
                            "product_ids": [product]
                        }))
                    
                    # Message loop
                    while not self._shutdown:
                        payload = json.loads(await ws.recv())
                        self.message_count += 1
                        self._last_message_time = asyncio.get_event_loop().time()
                        
                        if self.event_bus is not None:
                            await self.event_bus.publish("ws_message", payload)
                        
                        feed = payload.get("feed", "unknown")
                        if feed == "trade":
                            self.trade_buffer.append(payload)
                            
            except asyncio.CancelledError:
                logger.info("WebSocket cancelled, shutting down")
                break
            except Exception as exc:
                self.reconnect_count += 1
                delay = self._backoff.next_delay()
                
                logger.warning(
                    "ws reconnecting in %.1fs (attempt %d): %s", 
                    delay, self._backoff.consecutive_failures, exc
                )
                
                if self.event_bus is not None:
                    await self.event_bus.publish("ws_reconnect", {
                        "exchange": self.exchange_name,
                        "error": str(exc),
                        "reconnect_count": self.reconnect_count,
                        "next_delay": delay,
                        "consecutive_failures": self._backoff.consecutive_failures
                    })
                
                await asyncio.sleep(delay)
            finally:
                self.ws = None
                if self.event_bus is not None and not self._shutdown:
                    await self.event_bus.publish("ws_disconnected", {
                        "exchange": self.exchange_name
                    })

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        self._shutdown = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
