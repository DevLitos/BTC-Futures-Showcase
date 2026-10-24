"""Prometheus metrics for connectors-only runtime."""

from prometheus_client import Counter, Gauge, Info

BUILD_INFO = Info("connectors_build_info", "Build information")
WS_RECONNECTS_TOTAL = Counter("connectors_ws_reconnects_total", "WebSocket reconnects", ["exchange", "status"])
WS_MESSAGES_TOTAL = Counter("connectors_ws_messages_total", "WebSocket messages", ["exchange", "feed"])
HEALTH_WS_CONNECTED = Gauge("connectors_health_ws_connected", "WebSocket connected", ["exchange"])
HEALTH_EXCHANGE_REACHABLE = Gauge("connectors_health_exchange_reachable", "Exchange reachable", ["exchange"])
HEALTH_CIRCUIT_OPEN = Gauge("connectors_health_circuit_open", "Health circuit breaker open", ["exchange"])
HEALTH_CIRCUIT_FAILURES = Gauge(
    "connectors_health_circuit_failures", "Health circuit breaker failure count", ["exchange"]
)


def start_metrics_server(port: int = 8000) -> None:
    from prometheus_client import start_http_server

    start_http_server(port)
