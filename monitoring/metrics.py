# monitoring/metrics.py

from prometheus_client import Counter, Gauge, Histogram

# --- Métricas de Ingesta de Datos ---
# Counter: Un valor que solo puede aumentar (ej. número total de mensajes).
MESSAGES_INGESTED = Counter(
    'data_ingest_messages_total',
    'Número total de mensajes recibidos del proveedor de datos',
    ['provider', 'symbol'] # Etiquetas para filtrar (ej. por proveedor o por símbolo)
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


