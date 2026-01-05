import asyncio
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest


def run_async(coro):
    return asyncio.run(coro)


def make_candles(
    n: int,
    start_ts_ms: int = 1_700_000_000_000,
    start_price: float = 50_000.0,
    step: float = 1.0,
    spread: float = 10.0,
    volume: float = 100.0,
) -> List[Dict[str, Any]]:
    candles: List[Dict[str, Any]] = []
    price = start_price
    for i in range(n):
        open_ = price
        close = price + step
        high = max(open_, close) + spread / 2
        low = min(open_, close) - spread / 2
        candles.append(
            {
                "timestamp": start_ts_ms + i * 60_000,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
        )
        price = close
    return candles


@dataclass
class FakeMarketData:
    candles: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    _market_time: datetime = field(default_factory=lambda: datetime.now(UTC))

    def get_candles(self, timeframe: str):
        return self.candles.get(timeframe)
    
    def get_market_time(self):
        return self._market_time


@dataclass
class FakeExecution:
    open_orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    available_capital: float = 1000.0
    exchange_has_position: bool = False
    position_contracts: float = 0.0
    placed_orders: List[Dict[str, Any]] = field(default_factory=list)
    closed_market: List[Dict[str, Any]] = field(default_factory=list)

    async def get_open_orders(self, symbol: str):
        return []

    async def get_position(self, symbol: str):
        if self.position_contracts != 0:
            return {"contracts": self.position_contracts}
        return None

    async def has_open_position(self, symbol: str):
        return self.exchange_has_position

    async def get_available_capital(self):
        return self.available_capital

    async def cancel_all_orders(self, symbol: str):
        return 0

    async def verify_stops_on_exchange(self, symbol: str):
        return {"has_sl": True, "has_tp": True}

    async def set_position_stops(self, symbol: str, stop_loss: float, take_profit: Optional[float] = None):
        return True

    async def place_market_order(self, **kwargs):
        self.placed_orders.append({"type": "market", **kwargs})
        return {"id": "order_market_1"}

    async def place_limit_order(self, **kwargs):
        self.placed_orders.append({"type": "limit", **kwargs})
        return {"id": "order_limit_1"}

    async def close_position_market(self, side: str, amount: float):
        self.closed_market.append({"side": side, "amount": amount})
        return {"status": "closed"}


@dataclass
class FakeExternalData:
    funding_rate: float = 0.0
    btc_ohlcv: Optional[pd.DataFrame] = None

    async def get_funding_rate(self, symbol: str):
        return self.funding_rate

    async def get_ohlcv(self, symbol: str, timeframe: str):
        return self.btc_ohlcv


@dataclass
class FakeTradeLogger:
    events: List[Dict[str, Any]] = field(default_factory=list)

    async def log_event(self, event_type: str, payload: Dict[str, Any]):
        self.events.append({"type": event_type, "payload": payload})


@pytest.fixture
def fake_market_data():
    md = FakeMarketData()
    md.candles["1m"] = make_candles(30, start_price=50_000, step=5)
    md.candles["5m"] = make_candles(30, start_price=50_000, step=10)
    md.candles["1h"] = make_candles(60, start_price=50_000, step=20)
    return md


@pytest.fixture
def fake_execution():
    return FakeExecution()


@pytest.fixture
def fake_external_data():
    # Default BTC candles for correlation check
    df = pd.DataFrame(make_candles(50, start_price=50_000, step=1))
    return FakeExternalData(funding_rate=0.0, btc_ohlcv=df)


@pytest.fixture
def fake_trade_logger():
    return FakeTradeLogger()


