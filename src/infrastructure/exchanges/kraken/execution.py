"""Minimal Kraken REST connector (no trading logic)."""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt


class RESTExecutionEngine:
    def __init__(self, exchange_name: str, credentials: dict[str, Any] | None = None):
        self.exchange_name = exchange_name
        self.credentials = credentials or {}
        self._exchange: Any | None = None

    async def initialize(self) -> None:
        if self._exchange is not None:
            return
        exchange_cls = getattr(ccxt, self.exchange_name, None)
        if exchange_cls is None:
            raise ValueError(f"Unsupported exchange: {self.exchange_name}")
        self._exchange = exchange_cls(self.credentials)

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        await self.initialize()
        if self._exchange is None:
            raise RuntimeError("Exchange connector not initialized")
        return await self._exchange.fetch_ticker(symbol)

    async def close(self) -> None:
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
