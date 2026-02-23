"""Infrastructure Layer - External Adapters and I/O."""

from src.infrastructure.exchanges.kraken.execution import RESTExecutionEngine
from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.ingest.websocket.order_book import OrderBookManager

__all__ = [
    'RESTExecutionEngine',
    'KrakenFuturesWebSocket',
    'OrderBookManager',
]
