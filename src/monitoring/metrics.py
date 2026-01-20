# monitoring/metrics.py

from prometheus_client import Counter, Gauge, Histogram, Info

# --- Métricas de Ingesta de Datos ---
# Counter: Un valor que solo puede aumentar (ej. número total de mensajes).
MESSAGES_INGESTED = Counter(
    'data_ingest_messages_total',
    'Número total de mensajes recibidos del proveedor de datos',
    ['provider', 'symbol'] # Etiquetas para filtrar (ej. por proveedor o por símbolo)
)

# =============================================================================
# PRODUCTION METRICS (P0/P1) - Orders, Reconciliation, Latency
# =============================================================================

# Build/Version info
BUILD_INFO = Info(
    'trading_build_info',
    'Build information and version'
)

# Order metrics
ORDERS_PLACED_TOTAL = Counter(
    'trading_orders_placed_total',
    'Total number of orders placed on exchange',
    ['symbol', 'side', 'order_type', 'status']  # status: success, failed
)

ORDERS_FAILED_TOTAL = Counter(
    'trading_orders_failed_total',
    'Total number of failed order placements',
    ['symbol', 'reason']  # reason: timeout, rejected, insufficient_margin, etc.
)

ORDERS_CANCELLED_TOTAL = Counter(
    'trading_orders_cancelled_total',
    'Total number of orders cancelled',
    ['symbol', 'reason']  # reason: user, stale, reconcile, shutdown
)

# Reconciliation metrics
RECONCILE_CYCLES_TOTAL = Counter(
    'trading_reconcile_cycles_total',
    'Total number of reconciliation cycles completed'
)

RECONCILE_ERRORS_TOTAL = Counter(
    'trading_reconcile_errors_total',
    'Total number of reconciliation errors detected',
    ['error_type']  # position_drift, order_mismatch, unknown
)

RECONCILE_DRIFT_DETECTED = Counter(
    'trading_reconcile_drift_detected_total',
    'Number of times position drift was detected'
)

# WebSocket metrics
WS_RECONNECTS_TOTAL = Counter(
    'trading_ws_reconnects_total',
    'Total number of WebSocket reconnection attempts',
    ['exchange', 'status']  # status: success, failed
)

WS_MESSAGES_TOTAL = Counter(
    'trading_ws_messages_total',
    'Total WebSocket messages received',
    ['exchange', 'feed']  # feed: trade, book, ticker
)

# Latency histograms
REST_REQUEST_LATENCY = Histogram(
    'trading_rest_request_latency_seconds',
    'Latency of REST API requests to exchange',
    ['exchange', 'endpoint'],  # endpoint: place_order, cancel_order, get_position, etc.
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

WS_MESSAGE_LATENCY = Histogram(
    'trading_ws_message_latency_seconds',
    'Latency from WebSocket message to processing complete',
    ['exchange', 'feed'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25]
)

STRATEGY_LOOP_LATENCY = Histogram(
    'trading_strategy_loop_latency_seconds',
    'Latency of each strategy loop iteration',
    ['phase'],  # phase: data_fetch, signal_calc, risk_check, execution
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5]
)

# Position gauges
OPEN_POSITION_QTY = Gauge(
    'trading_open_position_qty',
    'Current open position quantity (negative for short)',
    ['symbol', 'side']
)

OPEN_POSITION_ENTRY_PRICE = Gauge(
    'trading_open_position_entry_price',
    'Entry price of current open position',
    ['symbol']
)

OPEN_POSITION_UNREALIZED_PNL = Gauge(
    'trading_open_position_unrealized_pnl',
    'Unrealized PnL of current open position in USD',
    ['symbol']
)

# Account gauges
ACCOUNT_EQUITY = Gauge(
    'trading_account_equity',
    'Current account equity in USD',
    ['exchange']
)

ACCOUNT_FREE_MARGIN = Gauge(
    'trading_account_free_margin',
    'Current free margin available for trading',
    ['exchange']
)

ACCOUNT_MARGIN_RATIO = Gauge(
    'trading_account_margin_ratio',
    'Current margin usage ratio (0-1)',
    ['exchange']
)

# Kill switch status
KILL_SWITCH_ACTIVE = Gauge(
    'trading_kill_switch_active',
    'Whether kill switch is currently active (1=active, 0=inactive)'
)

# Health status
HEALTH_WS_CONNECTED = Gauge(
    'trading_health_ws_connected',
    'WebSocket connection status (1=connected, 0=disconnected)',
    ['exchange']
)

HEALTH_EXCHANGE_REACHABLE = Gauge(
    'trading_health_exchange_reachable',
    'Exchange API reachability (1=reachable, 0=unreachable)',
    ['exchange']
)

HEALTH_TIME_DRIFT_MS = Gauge(
    'trading_health_time_drift_ms',
    'Clock drift between local and exchange time in milliseconds',
    ['exchange']
)

# Rate limiting
RATE_LIMIT_REMAINING = Gauge(
    'trading_rate_limit_remaining',
    'Remaining API calls before rate limit',
    ['exchange', 'endpoint']
)

RATE_LIMIT_RESET_SECONDS = Gauge(
    'trading_rate_limit_reset_seconds',
    'Seconds until rate limit resets',
    ['exchange']
)

# =============================================================================
# EDGE HEALTH METRICS (Grafana Panel)
# =============================================================================

# Expectancy tracking (for Edge Health Panel)
EDGE_EXPECTANCY_CURRENT = Gauge(
    'edge_expectancy_current',
    'Expectancy actual de los últimos N trades (en R)',
    ['window']  # 'last_20', 'last_50', 'historical'
)

EDGE_EXPECTANCY_HISTORICAL = Gauge(
    'edge_expectancy_historical',
    'Expectancy histórico del sistema (en R)',
    ['symbol']
)

EDGE_HEALTH_RATIO = Gauge(
    'edge_health_ratio',
    'Ratio de expectancy actual vs histórico (alerta si < 0.5)',
    ['symbol']
)

# Market Regime tracking
MARKET_REGIME_CURRENT = Gauge(
    'market_regime_current',
    'Régimen de mercado actual (1=TREND, 2=MEAN_REV, 3=CHOPPY)',
    ['symbol']
)

MARKET_ATR_RATIO = Gauge(
    'market_atr_ratio',
    'ATR actual vs baseline (>2.0 = pánico)',
    ['symbol']
)

# Risk Escalation tracking
RISK_STREAK_CURRENT = Gauge(
    'risk_streak_current',
    'Racha actual de trades (positivo=wins, negativo=losses)',
    ['symbol']
)

RISK_ESCALATION_ACTIVE = Gauge(
    'risk_escalation_active',
    '1 si escalación activa, 0 si no',
    ['symbol']
)

RISK_CURRENT_PCT = Gauge(
    'risk_current_pct',
    'Porcentaje de riesgo actual por trade',
    ['symbol']
)

# Shadow Trading comparison
SHADOW_PNL_REAL = Gauge(
    'shadow_pnl_real',
    'PnL real acumulado',
    ['symbol']
)

SHADOW_PNL_ESCALATED = Gauge(
    'shadow_pnl_escalated',
    'PnL que habría sido con riesgo escalado',
    ['symbol']
)

SHADOW_OUTPERFORM_STREAK = Gauge(
    'shadow_outperform_streak',
    'Trades consecutivos donde escalated > real',
    ['symbol']
)

# PPO (Profit Per Opportunity)
PPO_CURRENT = Gauge(
    'ppo_current',
    'Profit Per Opportunity actual',
    ['symbol']
)

PPO_EXECUTION_RATE = Gauge(
    'ppo_execution_rate',
    'Tasa de ejecución de señales (trades/señales)',
    ['symbol']
)

# SL Bin performance
SL_BIN_EXPECTANCY = Gauge(
    'sl_bin_expectancy',
    'Expectancy por SL bin',
    ['bin']  # 'MICRO', 'NORMAL', 'WIDE'
)

SL_BIN_TRADE_COUNT = Counter(
    'sl_bin_trade_count_total',
    'Número de trades por SL bin',
    ['bin']
)

# Setup performance
SETUP_EXPECTANCY = Gauge(
    'setup_expectancy',
    'Expectancy por tipo de setup',
    ['setup']
)

SETUP_GRADE = Gauge(
    'setup_grade',
    'Grado del setup (1=A, 2=B, 3=C)',
    ['setup']
)

# TP1 Optimization
TP1_CURRENT_R = Gauge(
    'tp1_current_r',
    'TP1 recomendado actual (en R)',
    ['symbol']
)

TP1_PF_IF_ONLY = Gauge(
    'tp1_pf_if_only',
    'Profit Factor teórico si cerráramos todo en TP1',
    ['symbol']
)

# --- Métricas del Motor de Señales ---
SIGNALS_GENERATED = Counter(
    'engine_signals_generated_total',
    'Número total de señales de trading preliminares generadas',
    ['symbol', 'direction']
)

# Histogram: Mide la distribución de un valor (ej. latencia).
# Rastrea cuánto tiempo pasa desde que se recibe un tick hasta que se genera una señal.
SIGNAL_PROCESSING_LATENCY = Histogram(
    'engine_signal_processing_latency_seconds',
    'Latencia en el procesamiento de señales (desde el tick hasta la señal)',
    ['symbol']
)

# --- Métricas del Motor de Riesgo ---
ORDERS_RECOMMENDED = Counter(
    'risk_orders_recommended_total',
    'Número total de recomendaciones de órdenes finales generadas',
    ['symbol', 'direction']
)

ORDERS_REJECTED = Counter(
    'risk_orders_rejected_total',
    'Número total de señales descartadas por el motor de riesgo',
    ['symbol', 'reason'] # Razón del rechazo (ej. 'daily_loss_limit')
)

# Gauge: Un valor que puede subir y bajar (ej. PnL actual).
INTERNAL_PNL = Gauge(
    'risk_internal_pnl_current',
    'PnL actual de la posición interna',
    ['symbol']
)

# --- Métricas de Salida ---
NOTIFICATIONS_SENT = Counter(
    'output_notifications_sent_total',
    'Número total de notificaciones enviadas',
    ['channel'] # Canal (ej. 'telegram')
)

# Para que estas métricas sean accesibles, necesitas exponer un endpoint HTTP.
# Esto se hace fácilmente si tus servicios están construidos con FastAPI o Flask,
# o puedes usar la función `start_http_server` de la librería.

def start_metrics_server(port: int = 8000 ):
    """
    Inicia un servidor HTTP para exponer las métricas a Prometheus.
    """
    from prometheus_client import start_http_server
    import logging
    
    try:
        start_http_server(port )
        logging.info(f"Servidor de métricas de Prometheus iniciado en el puerto {port}")
    except Exception as e:
        logging.error(f"No se pudo iniciar el servidor de métricas: {e}")


# =============================================================================
# EDGE HEALTH UPDATERS
# =============================================================================

def update_edge_health(symbol: str, 
                       expectancy_last_20: float,
                       expectancy_historical: float):
    """
    Update Edge Health metrics for Grafana.
    
    Alerta: Si expectancy_last_20 < 0.5 * expectancy_historical → Modo Observación
    """
    EDGE_EXPECTANCY_CURRENT.labels(window='last_20').set(expectancy_last_20)
    EDGE_EXPECTANCY_HISTORICAL.labels(symbol=symbol).set(expectancy_historical)
    
    # Calculate health ratio
    if expectancy_historical > 0:
        ratio = expectancy_last_20 / expectancy_historical
    else:
        ratio = 1.0
    
    EDGE_HEALTH_RATIO.labels(symbol=symbol).set(ratio)


def update_market_regime(symbol: str,
                         regime_value: int,  # 1=TREND, 2=MEAN_REV, 3=CHOPPY
                         atr_ratio: float):
    """Update market regime metrics."""
    MARKET_REGIME_CURRENT.labels(symbol=symbol).set(regime_value)
    MARKET_ATR_RATIO.labels(symbol=symbol).set(atr_ratio)


def update_risk_escalation(symbol: str,
                           streak: int,
                           is_active: bool,
                           current_risk_pct: float):
    """Update risk escalation metrics."""
    RISK_STREAK_CURRENT.labels(symbol=symbol).set(streak)
    RISK_ESCALATION_ACTIVE.labels(symbol=symbol).set(1 if is_active else 0)
    RISK_CURRENT_PCT.labels(symbol=symbol).set(current_risk_pct)


def update_shadow_trading(symbol: str,
                          pnl_real: float,
                          pnl_escalated: float,
                          outperform_streak: int):
    """Update shadow trading comparison metrics."""
    SHADOW_PNL_REAL.labels(symbol=symbol).set(pnl_real)
    SHADOW_PNL_ESCALATED.labels(symbol=symbol).set(pnl_escalated)
    SHADOW_OUTPERFORM_STREAK.labels(symbol=symbol).set(outperform_streak)


def update_ppo(symbol: str, ppo: float, execution_rate: float):
    """Update PPO metrics."""
    PPO_CURRENT.labels(symbol=symbol).set(ppo)
    PPO_EXECUTION_RATE.labels(symbol=symbol).set(execution_rate)


def update_sl_bin(bin_name: str, expectancy: float, increment_count: bool = False):
    """Update SL bin metrics."""
    SL_BIN_EXPECTANCY.labels(bin=bin_name).set(expectancy)
    if increment_count:
        SL_BIN_TRADE_COUNT.labels(bin=bin_name).inc()


def update_setup_metrics(setup_name: str, expectancy: float, grade: int):
    """Update setup performance metrics (grade: 1=A, 2=B, 3=C)."""
    SETUP_EXPECTANCY.labels(setup=setup_name).set(expectancy)
    SETUP_GRADE.labels(setup=setup_name).set(grade)


def update_tp1_metrics(symbol: str, current_r: float, pf_if_only: float):
    """Update TP1 optimization metrics."""
    TP1_CURRENT_R.labels(symbol=symbol).set(current_r)
    TP1_PF_IF_ONLY.labels(symbol=symbol).set(pf_if_only)


def check_edge_health_alert(expectancy_last_20: float,
                            expectancy_historical: float,
                            threshold: float = 0.5) -> bool:
    """
    Check if Edge Health alert should trigger.
    
    Returns True if should enter Observation Mode (reduce risk).
    """
    if expectancy_historical <= 0:
        return False
    
    ratio = expectancy_last_20 / expectancy_historical
    return ratio < threshold


# =============================================================================
# PULLBACK SHORT ENGINE METRICS (Jan 14, 2026)
# =============================================================================

PULLBACK_SHORT_SIGNALS_TOTAL = Counter(
    'pullback_short_signals_total',
    'Total pullback short signals generated',
    ['symbol']
)

PULLBACK_SHORT_ENTRIES_TOTAL = Counter(
    'pullback_short_entries_total',
    'Total pullback short entries executed',
    ['symbol']
)

PULLBACK_SHORT_BLOCKS_TOTAL = Counter(
    'pullback_short_blocks_total',
    'Total pullback short signals blocked',
    ['symbol', 'reason']  # reason: ext_z_low, no_trigger, breakout_cooldown, daily_limit, etc.
)

PULLBACK_SHORT_PNL_TOTAL = Gauge(
    'pullback_short_pnl_total',
    'Cumulative PnL from pullback shorts in USD',
    ['symbol']
)

PULLBACK_SHORT_WINS_TOTAL = Counter(
    'pullback_short_wins_total',
    'Total winning pullback short trades',
    ['symbol']
)

PULLBACK_SHORT_LOSSES_TOTAL = Counter(
    'pullback_short_losses_total',
    'Total losing pullback short trades',
    ['symbol']
)

PULLBACK_SHORT_MFE_R = Histogram(
    'pullback_short_mfe_r',
    'Maximum Favorable Excursion for pullback shorts (in R)',
    ['symbol'],
    buckets=[0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]
)

PULLBACK_SHORT_MAE_R = Histogram(
    'pullback_short_mae_r',
    'Maximum Adverse Excursion for pullback shorts (in R)',
    ['symbol'],
    buckets=[0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5]
)

PULLBACK_SHORT_EXTENSION_Z = Histogram(
    'pullback_short_extension_z',
    'Extension Z-score at entry for pullback shorts',
    ['symbol'],
    buckets=[1.5, 1.8, 2.0, 2.3, 2.5, 3.0, 4.0, 5.0]
)

PULLBACK_SHORT_DAILY_COUNT = Gauge(
    'pullback_short_daily_count',
    'Number of pullback short trades today',
    ['symbol']
)

PULLBACK_SHORT_CONSECUTIVE_LOSSES = Gauge(
    'pullback_short_consecutive_losses',
    'Current consecutive pullback short losses',
    ['symbol']
)


def update_pullback_short_signal(symbol: str, blocked: bool = False, reason: str = ""):
    """Update pullback short signal metrics."""
    PULLBACK_SHORT_SIGNALS_TOTAL.labels(symbol=symbol).inc()
    if blocked and reason:
        PULLBACK_SHORT_BLOCKS_TOTAL.labels(symbol=symbol, reason=reason).inc()


def update_pullback_short_entry(symbol: str, extension_z: float):
    """Update pullback short entry metrics."""
    PULLBACK_SHORT_ENTRIES_TOTAL.labels(symbol=symbol).inc()
    PULLBACK_SHORT_EXTENSION_Z.labels(symbol=symbol).observe(extension_z)


def update_pullback_short_exit(symbol: str, pnl: float, is_win: bool, mfe_r: float, mae_r: float):
    """Update pullback short exit metrics."""
    if is_win:
        PULLBACK_SHORT_WINS_TOTAL.labels(symbol=symbol).inc()
    else:
        PULLBACK_SHORT_LOSSES_TOTAL.labels(symbol=symbol).inc()
    
    # Update cumulative PnL (get current and add)
    # Note: For proper cumulative, you'd track in the engine and set here
    PULLBACK_SHORT_MFE_R.labels(symbol=symbol).observe(mfe_r)
    PULLBACK_SHORT_MAE_R.labels(symbol=symbol).observe(mae_r)


def update_pullback_short_status(symbol: str, daily_count: int, consecutive_losses: int, cumulative_pnl: float):
    """Update pullback short status gauges."""
    PULLBACK_SHORT_DAILY_COUNT.labels(symbol=symbol).set(daily_count)
    PULLBACK_SHORT_CONSECUTIVE_LOSSES.labels(symbol=symbol).set(consecutive_losses)
    PULLBACK_SHORT_PNL_TOTAL.labels(symbol=symbol).set(cumulative_pnl)