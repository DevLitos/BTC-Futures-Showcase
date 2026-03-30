"""Minimal order-book cache (no trading decisions)."""

from __future__ import annotations


class OrderBookManager:
    def __init__(self, symbol: str = "BTC/USD"):
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def update_bid(self, price: float, qty: float) -> None:
        if qty <= 0:
            self.bids.pop(price, None)
        else:
            self.bids[price] = qty

    def update_ask(self, price: float, qty: float) -> None:
        if qty <= 0:
            self.asks.pop(price, None)
        else:
            self.asks[price] = qty
