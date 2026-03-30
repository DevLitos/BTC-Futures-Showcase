"""Kraken websocket connector (data transport only)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

import websockets

logger = logging.getLogger(__name__)


class KrakenFuturesWebSocket:
    def __init__(self, symbols: list[str] | None = None):
        self.symbols = symbols or ["BTC/USD"]
        self.ws: Any | None = None
        self._shutdown = False
        self.trade_buffer: deque[dict[str, Any]] = deque(maxlen=1000)

    async def connect(self) -> None:
        while not self._shutdown:
            try:
                async with websockets.connect("wss://futures.kraken.com/ws", ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    for symbol in self.symbols:
                        product = "PF_XBTUSD" if symbol == "BTC/USD" else symbol.replace("/", "")
                        await ws.send(json.dumps({"event": "subscribe", "feed": "trade", "product_ids": [product]}))
                    while not self._shutdown:
                        payload = json.loads(await ws.recv())
                        if payload.get("feed") == "trade":
                            self.trade_buffer.append(payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("ws reconnecting: %s", exc)
                await asyncio.sleep(2)

    async def disconnect(self) -> None:
        self._shutdown = True
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
