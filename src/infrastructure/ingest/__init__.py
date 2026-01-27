"""
Ingest Module - Data ingestion from various sources

Components:
- KrakenFuturesWebSocket: Real-time Kraken futures data
- OrderBookManager: Order book state management
- BaseIngestor: Abstract base class for ingestors
"""

from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.ingest.websocket.order_book import OrderBookManager
from src.infrastructure.ingest.historical.base_ingestor import BaseIngestor

__all__ = [
    'KrakenFuturesWebSocket',
    'OrderBookManager', 
    'BaseIngestor',
]
