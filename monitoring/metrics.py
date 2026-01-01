# monitoring/metrics.py

from prometheus_client import Counter, Gauge, Histogram

# --- Métricas de Ingesta de Datos ---
# Counter: Un valor que solo puede aumentar (ej. número total de mensajes).
MESSAGES_INGESTED = Counter(
    'data_ingest_messages_total',
    'Número total de mensajes recibidos del proveedor de datos',
    ['provider', 'symbol'] # Etiquetas para filtrar (ej. por proveedor o por símbolo)
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

