"""Ingest Module - Real-time data from Kraken Futures."""

from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.ingest.websocket.order_book import OrderBookManager

__all__ = [
    'KrakenFuturesWebSocket',
    'OrderBookManager',
]
