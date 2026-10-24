"""Exchange symbol normalization helpers."""

from __future__ import annotations


class SymbolMapper:
    @staticmethod
    def to_rest_symbol(exchange_name: str, symbol: str) -> str:
        if exchange_name == "krakenfutures" and symbol == "BTC/USD":
            return "BTC/USD:USD"
        return symbol

    @staticmethod
    def to_ws_symbol(exchange_name: str, symbol: str) -> str:
        if exchange_name == "krakenfutures":
            if symbol == "BTC/USD":
                return "PF_XBTUSD"
            return symbol.replace("/", "")
        return symbol
