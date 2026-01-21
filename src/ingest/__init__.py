"""
Ingest Module - Data ingestion from various sources

Components:
- KrakenFuturesWebSocket: Real-time Kraken futures data
- OrderBookManager: Order book state management
- BaseIngestor: Abstract base class for ingestors
"""

from src.ingest.kraken_websocket import KrakenFuturesWebSocket
from src.ingest.order_book import OrderBookManager
from src.ingest.base_ingestor import BaseIngestor

__all__ = [
    'KrakenFuturesWebSocket',
    'OrderBookManager', 
    'BaseIngestor',
]
