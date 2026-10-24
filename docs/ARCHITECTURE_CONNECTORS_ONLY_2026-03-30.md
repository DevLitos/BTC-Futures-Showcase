# Arquitectura actual (connectors-only)

## Objetivo
Runtime mínimo para conectividad y observabilidad, sin lógica de señales/estrategia/ejecución de trades.

## Capas

- **Bootstrap**: `main.py`
  - Configura logging y dispara el runtime.
- **Application Runtime**: `src/application/runtime/connectors_runtime.py`
  - Supervisa tareas asíncronas.
  - Gestiona señales de apagado (`SIGINT`, `SIGTERM`).
  - Publica métricas de salud WS/REST.
  - Incluye circuit-breaker y backoff adaptativo para probes REST.
  - Integra event bus interno para eventos de conectividad.
- **Infrastructure / Ingest**: `src/infrastructure/ingest/websocket/kraken_websocket.py`
  - Conexión WS con reconexión automática.
  - Buffer circular de trades.
  - Contadores de mensajes/reconexiones.
  - Publicación de eventos (`ws_connected`, `ws_disconnected`, `ws_reconnect`, `ws_message`).
- **Infrastructure / Exchanges**: `src/infrastructure/exchanges/kraken/execution.py`
  - Conector REST mínimo para probes de salud (`fetch_ticker`).
  - Normaliza símbolos por exchange vía `src/infrastructure/exchanges/symbol_mapper.py`.
- **Monitoring**: `src/infrastructure/monitoring/metrics.py`
  - Métricas Prometheus de build, mensajes WS, reconexiones, salud y estado del circuit-breaker.

## Flujos

1. `main.py` crea `Runtime`.
2. `Runtime.run()` inicia:
   - tarea de conexión WS,
   - loop de salud WS,
   - loop de salud REST,
  - publicación/consumo de eventos internos,
   - wait de parada.
3. Si falla WS o llega señal, se activa stop.
4. Apagado determinista:
   - cancelar tareas,
   - cerrar WS,
   - cerrar REST,
   - resetear gauges de salud.

## Decisiones de diseño

- **Idempotencia en stop**: evita doble procesamiento de señales.
- **Supervisión centralizada**: un único runtime coordina tareas y shutdown.
- **Observabilidad mínima viable**: métricas de liveness, throughput y salud del circuit-breaker sin acoplar lógica de trading.
- **Normalización explícita de símbolos**: evita errores por formatos diferentes entre WS y REST.

## Redundancia eliminada en esta iteración

- `Runtime` monolítico incrustado en `main.py` (movido a capa `application/runtime`).
- Gestión de lifecycle dispersa (unificada en métodos privados de `Runtime`).
- Residuos de ejecución (`__pycache__`) borrados del workspace.
- Árbol legacy no usado movido a `deprecated/connectors_legacy/src/`.

## Riesgos abiertos

- El símbolo de health-check REST depende del exchange (`HEALTH_SYMBOL`, default `BTC/USD:USD`).
- El event bus es en memoria (sin persistencia de eventos).

## Próxima expansión natural (sin romper esta base)

- Persistencia opcional de eventos de conectividad para auditoría.
- Múltiples consumidores sobre event bus (alerting, storage, analytics).
- Soporte multi-exchange ampliando `SymbolMapper`.
