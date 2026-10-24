from unittest.mock import AsyncMock

import pytest

from src.infrastructure.exchanges.kraken.execution import OrderRequest, RESTExecutionEngine


@pytest.mark.asyncio
async def test_place_order_normalizes_ws_symbol() -> None:
    exchange = AsyncMock()
    exchange.create_market_order.return_value = {
        "id": "order-1",
        "filled": 0.001,
        "average": 90000.0,
        "status": "closed",
    }

    engine = RESTExecutionEngine("krakenfutures")
    engine._exchange = exchange
    engine._initialized = True

    result = await engine.place_order(OrderRequest(symbol="PF_XBTUSD", side="buy", amount=0.001))

    assert result.success is True
    exchange.create_market_order.assert_awaited_once()
    assert exchange.create_market_order.call_args.args[:3] == ("BTC/USD:USD", "buy", 0.001)


@pytest.mark.asyncio
async def test_close_position_is_reduce_only() -> None:
    exchange = AsyncMock()
    exchange.create_market_order.return_value = {
        "id": "close-1",
        "filled": 0.001,
        "average": 89900.0,
        "status": "closed",
    }

    engine = RESTExecutionEngine("krakenfutures")
    engine._exchange = exchange
    engine._initialized = True

    result = await engine.close_position("BTC/USD:USD", "long", 0.001)

    assert result.success is True
    params = exchange.create_market_order.call_args.kwargs["params"]
    assert params["reduceOnly"] is True
