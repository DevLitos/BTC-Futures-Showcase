"""
Monitoring Module - System metrics and monitoring

Components:
- Metrics: Trading metrics collection and reporting
"""

from src.infrastructure.monitoring.metrics import (
    start_metrics_server,
    update_edge_health,
    update_market_regime,
    update_risk_escalation,
    ORDERS_RECOMMENDED,
    ORDERS_REJECTED,
    ORDERS_PLACED_TOTAL,
    ORDERS_FAILED_TOTAL,
)

__all__ = [
    'start_metrics_server',
    'update_edge_health',
    'update_market_regime',
    'update_risk_escalation',
    'ORDERS_RECOMMENDED',
    'ORDERS_REJECTED',
    'ORDERS_PLACED_TOTAL',
    'ORDERS_FAILED_TOTAL',
]
