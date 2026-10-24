"""In-process async event bus for connectors runtime."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class RuntimeEvent:
    name: str
    payload: dict[str, Any]
    ts: datetime


Handler = Callable[[RuntimeEvent], Awaitable[None]]
logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: Handler) -> None:
        self._handlers[event_name].append(handler)

    async def publish(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        event = RuntimeEvent(name=event_name, payload=payload or {}, ts=datetime.now(UTC))
        handlers = self._handlers.get(event_name, [])
        if not handlers:
            return
        results = await asyncio.gather(*(handler(event) for handler in handlers), return_exceptions=True)
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("event handler failed: event=%s handler_idx=%s error=%s", event_name, idx, result)
