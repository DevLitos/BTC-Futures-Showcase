"""Connectors-only runtime (all trading logic removed)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from src.infrastructure.exchanges.kraken.execution import RESTExecutionEngine
from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.monitoring.metrics import BUILD_INFO, start_metrics_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("connectors")


class Runtime:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.ws = KrakenFuturesWebSocket([os.getenv("WS_SYMBOL", "BTC/USD")])
        self.rest = RESTExecutionEngine(
            os.getenv("EXCHANGE", "krakenfutures"),
            {
                "apiKey": os.getenv("KRAKEN_API_KEY", ""),
                "secret": os.getenv("KRAKEN_PRIVATE_KEY", ""),
            },
        )

    def _signal(self, signum: int, _frame: object) -> None:
        logger.info("signal=%s stopping", signum)
        self._stop.set()

    async def run(self) -> None:
        BUILD_INFO.info({"mode": "connectors-only"})
        start_metrics_server(int(os.getenv("METRICS_PORT", "8000")))
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)
        ws_task = asyncio.create_task(self.ws.connect())
        await self._stop.wait()
        ws_task.cancel()
        await self.ws.disconnect()
        await self.rest.close()


async def main() -> None:
    logger.warning("CONNECTORS-ONLY MODE: trading logic/classes/guards removed")
    runtime = Runtime()
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
