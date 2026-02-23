# monitoring/metrics.py
"""Prometheus metrics for the trading system. Only ALIVE metrics included."""

from prometheus_client import Counter, Gauge, Histogram, Info

# =============================================================================
# PRODUCTION METRICS - Orders, Reconciliation, Latency
# =============================================================================

BUILD_INFO = Info('trading_build_info', 'Build information and version')

ORDERS_PLACED_TOTAL = Counter(
    'trading_orders_placed_total', 'Orders placed on exchange',
    ['symbol', 'side', 'order_type', 'status'])
ORDERS_FAILED_TOTAL = Counter(
    'trading_orders_failed_total', 'Failed order placements',
    ['symbol', 'reason'])
ORDERS_CANCELLED_TOTAL = Counter(
    'trading_orders_cancelled_total', 'Orders cancelled',
    ['symbol', 'reason'])

RECONCILE_CYCLES_TOTAL = Counter('trading_reconcile_cycles_total', 'Reconciliation cycles')
RECONCILE_ERRORS_TOTAL = Counter('trading_reconcile_errors_total', 'Reconciliation errors', ['error_type'])
RECONCILE_DRIFT_DETECTED = Counter('trading_reconcile_drift_detected_total', 'Position drift detected')

WS_RECONNECTS_TOTAL = Counter('trading_ws_reconnects_total', 'WebSocket reconnections', ['exchange', 'status'])
WS_MESSAGES_TOTAL = Counter('trading_ws_messages_total', 'WebSocket messages', ['exchange', 'feed'])

REST_REQUEST_LATENCY = Histogram(
    'trading_rest_request_latency_seconds', 'REST API latency',
    ['exchange', 'endpoint'], buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
WS_MESSAGE_LATENCY = Histogram(
    'trading_ws_message_latency_seconds', 'WebSocket processing latency',
    ['exchange', 'feed'], buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25])
STRATEGY_LOOP_LATENCY = Histogram(
    'trading_strategy_loop_latency_seconds', 'Strategy loop latency',
    ['phase'], buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5])

# Position gauges
OPEN_POSITION_QTY = Gauge('trading_open_position_qty', 'Open position quantity', ['symbol', 'side'])
OPEN_POSITION_ENTRY_PRICE = Gauge('trading_open_position_entry_price', 'Entry price', ['symbol'])
OPEN_POSITION_UNREALIZED_PNL = Gauge('trading_open_position_unrealized_pnl', 'Unrealized PnL', ['symbol'])

# Account gauges
ACCOUNT_EQUITY = Gauge('trading_account_equity', 'Account equity USD', ['exchange'])
ACCOUNT_FREE_MARGIN = Gauge('trading_account_free_margin', 'Free margin', ['exchange'])
ACCOUNT_MARGIN_RATIO = Gauge('trading_account_margin_ratio', 'Margin ratio', ['exchange'])

# Kill switch & health
KILL_SWITCH_ACTIVE = Gauge('trading_kill_switch_active', 'Kill switch active')
HEALTH_WS_CONNECTED = Gauge('trading_health_ws_connected', 'WS connected', ['exchange'])
HEALTH_EXCHANGE_REACHABLE = Gauge('trading_health_exchange_reachable', 'Exchange reachable', ['exchange'])
HEALTH_TIME_DRIFT_MS = Gauge('trading_health_time_drift_ms', 'Clock drift ms', ['exchange'])

# Rate limiting
RATE_LIMIT_REMAINING = Gauge('trading_rate_limit_remaining', 'Rate limit remaining', ['exchange', 'endpoint'])
RATE_LIMIT_RESET_SECONDS = Gauge('trading_rate_limit_reset_seconds', 'Rate limit reset', ['exchange'])

# =============================================================================
# EDGE HEALTH METRICS
# =============================================================================

EDGE_EXPECTANCY_CURRENT = Gauge('edge_expectancy_current', 'Current expectancy (R)', ['window'])
EDGE_EXPECTANCY_HISTORICAL = Gauge('edge_expectancy_historical', 'Historical expectancy (R)', ['symbol'])
EDGE_HEALTH_RATIO = Gauge('edge_health_ratio', 'Expectancy ratio current/historical', ['symbol'])

MARKET_REGIME_CURRENT = Gauge('market_regime_current', 'Market regime (1=TREND,2=MEAN_REV,3=CHOPPY)', ['symbol'])
MARKET_ATR_RATIO = Gauge('market_atr_ratio', 'ATR ratio vs baseline', ['symbol'])

RISK_STREAK_CURRENT = Gauge('risk_streak_current', 'Current win/loss streak', ['symbol'])
RISK_ESCALATION_ACTIVE = Gauge('risk_escalation_active', 'Risk escalation active', ['symbol'])
RISK_CURRENT_PCT = Gauge('risk_current_pct', 'Current risk pct', ['symbol'])

# =============================================================================
# SIGNAL & RISK ENGINE METRICS
# =============================================================================

SIGNALS_GENERATED = Counter('engine_signals_generated_total', 'Signals generated', ['symbol', 'direction'])
SIGNAL_PROCESSING_LATENCY = Histogram(
    'engine_signal_processing_latency_seconds', 'Signal processing latency', ['symbol'])

ORDERS_RECOMMENDED = Counter('risk_orders_recommended_total', 'Orders recommended', ['symbol', 'direction'])
ORDERS_REJECTED = Counter('risk_orders_rejected_total', 'Orders rejected', ['symbol', 'reason'])
INTERNAL_PNL = Gauge('risk_internal_pnl_current', 'Internal PnL', ['symbol'])
NOTIFICATIONS_SENT = Counter('output_notifications_sent_total', 'Notifications sent', ['channel'])

# =============================================================================
# PULLBACK SHORT ENGINE METRICS
# =============================================================================

PULLBACK_SHORT_SIGNALS_TOTAL = Counter('pullback_short_signals_total', 'Pullback short signals', ['symbol'])
PULLBACK_SHORT_ENTRIES_TOTAL = Counter('pullback_short_entries_total', 'Pullback short entries', ['symbol'])
PULLBACK_SHORT_BLOCKS_TOTAL = Counter('pullback_short_blocks_total', 'Pullback short blocks', ['symbol', 'reason'])
PULLBACK_SHORT_PNL_TOTAL = Gauge('pullback_short_pnl_total', 'Pullback short cumulative PnL', ['symbol'])
PULLBACK_SHORT_WINS_TOTAL = Counter('pullback_short_wins_total', 'Pullback short wins', ['symbol'])
PULLBACK_SHORT_LOSSES_TOTAL = Counter('pullback_short_losses_total', 'Pullback short losses', ['symbol'])
PULLBACK_SHORT_MFE_R = Histogram(
    'pullback_short_mfe_r', 'Pullback short MFE (R)', ['symbol'],
    buckets=[0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0])
PULLBACK_SHORT_MAE_R = Histogram(
    'pullback_short_mae_r', 'Pullback short MAE (R)', ['symbol'],
    buckets=[0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5])
PULLBACK_SHORT_EXTENSION_Z = Histogram(
    'pullback_short_extension_z', 'Pullback short extension Z', ['symbol'],
    buckets=[1.5, 1.8, 2.0, 2.3, 2.5, 3.0, 4.0, 5.0])
PULLBACK_SHORT_DAILY_COUNT = Gauge('pullback_short_daily_count', 'Pullback shorts today', ['symbol'])
PULLBACK_SHORT_CONSECUTIVE_LOSSES = Gauge('pullback_short_consecutive_losses', 'Consecutive losses', ['symbol'])


# =============================================================================
# UPDATER FUNCTIONS (only those with external callers)
# =============================================================================

def start_metrics_server(port: int = 8000):
    """Start Prometheus HTTP metrics server."""
    from prometheus_client import start_http_server
    import logging
    try:
        start_http_server(port)
        logging.info(f"Prometheus metrics server started on port {port}")
    except Exception as e:
        logging.error(f"Failed to start metrics server: {e}")


def update_edge_health(symbol: str, expectancy_last_20: float, expectancy_historical: float):
    """Update Edge Health metrics."""
    EDGE_EXPECTANCY_CURRENT.labels(window='last_20').set(expectancy_last_20)
    EDGE_EXPECTANCY_HISTORICAL.labels(symbol=symbol).set(expectancy_historical)
    ratio = expectancy_last_20 / expectancy_historical if expectancy_historical > 0 else 1.0
    EDGE_HEALTH_RATIO.labels(symbol=symbol).set(ratio)


def update_market_regime(symbol: str, regime_value: int, atr_ratio: float):
    """Update market regime metrics."""
    MARKET_REGIME_CURRENT.labels(symbol=symbol).set(regime_value)
    MARKET_ATR_RATIO.labels(symbol=symbol).set(atr_ratio)


def update_risk_escalation(symbol: str, streak: int, is_active: bool, current_risk_pct: float):
    """Update risk escalation metrics."""
    RISK_STREAK_CURRENT.labels(symbol=symbol).set(streak)
    RISK_ESCALATION_ACTIVE.labels(symbol=symbol).set(1 if is_active else 0)
    RISK_CURRENT_PCT.labels(symbol=symbol).set(current_risk_pct)


def update_pullback_short_signal(symbol: str | None, blocked: bool = False, reason: str = ""):
    """Update pullback short signal metrics."""
    PULLBACK_SHORT_SIGNALS_TOTAL.labels(symbol=symbol or 'UNKNOWN').inc()
    if blocked and reason:
        PULLBACK_SHORT_BLOCKS_TOTAL.labels(symbol=symbol or 'UNKNOWN', reason=reason).inc()


def update_pullback_short_entry(symbol: str | None, extension_z: float):
    """Update pullback short entry metrics."""
    PULLBACK_SHORT_ENTRIES_TOTAL.labels(symbol=symbol or 'UNKNOWN').inc()
    PULLBACK_SHORT_EXTENSION_Z.labels(symbol=symbol or 'UNKNOWN').observe(extension_z)


def update_pullback_short_exit(symbol: str | None, pnl: float, is_win: bool, mfe_r: float, mae_r: float):
    """Update pullback short exit metrics."""
    _sym = symbol or 'UNKNOWN'
    (PULLBACK_SHORT_WINS_TOTAL if is_win else PULLBACK_SHORT_LOSSES_TOTAL).labels(symbol=_sym).inc()
    PULLBACK_SHORT_MFE_R.labels(symbol=_sym).observe(mfe_r)
    PULLBACK_SHORT_MAE_R.labels(symbol=_sym).observe(mae_r)


def update_pullback_short_status(symbol: str | None, daily_count: int, consecutive_losses: int, cumulative_pnl: float):
    """Update pullback short status gauges."""
    _sym = symbol or 'UNKNOWN'
    PULLBACK_SHORT_DAILY_COUNT.labels(symbol=_sym).set(daily_count)
    PULLBACK_SHORT_CONSECUTIVE_LOSSES.labels(symbol=_sym).set(consecutive_losses)
    PULLBACK_SHORT_PNL_TOTAL.labels(symbol=_sym).set(cumulative_pnl)