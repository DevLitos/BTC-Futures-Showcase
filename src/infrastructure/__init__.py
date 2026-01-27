"""
Infrastructure Layer - External Adapters and I/O

Clean Architecture Layer: INFRASTRUCTURE
This layer contains adapters for:
- exchanges/: Exchange connectivity (Kraken, Binance)
- ingest/: Market data ingestion (WebSocket, REST)
- storage/: Data persistence (Parquet, PostgreSQL)
- messaging/: Message queues (Kafka)
- monitoring/: Metrics and observability (Prometheus)

Clean Architecture Rules:
- Implements interfaces defined in domain layer
- All external dependencies isolated here
- Easy to swap implementations (e.g., mock exchange for testing)

Migration Status: ALIAS (re-exports from src.engine and src.ingest)
Target: Gradually move adapters here
"""

# Re-export key infrastructure components for convenience
from src.infrastructure.exchanges.kraken.execution import RESTExecutionEngine
from src.infrastructure.ingest.websocket.kraken_websocket import KrakenFuturesWebSocket
from src.infrastructure.ingest.websocket.order_book import OrderBookManager

__all__ = [
    'RESTExecutionEngine',
    'KrakenFuturesWebSocket',
    'OrderBookManager',
]
