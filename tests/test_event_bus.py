import pytest

from src.shared.events.event_bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_dispatches_to_subscriber() -> None:
    bus = EventBus()
    received: list[str] = []

    async def handler(event) -> None:
        received.append(event.name)

    bus.subscribe("ws_connected", handler)
    await bus.publish("ws_connected", {"exchange": "krakenfutures"})

    assert received == ["ws_connected"]
