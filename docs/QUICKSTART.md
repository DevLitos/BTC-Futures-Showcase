# Quickstart — Connectors-only

## 1) Instalar dependencias

```bash
make install
```

## 2) Configurar entorno mínimo

Variables relevantes (opcionales si usas defaults):

- `EXCHANGE` (default: `krakenfutures`)
- `WS_SYMBOL` (default: `BTC/USD`)
- `HEALTH_SYMBOL` (default: `BTC/USD:USD`)
- `METRICS_PORT` (default: `8000`)
- `HEALTH_INTERVAL_SECONDS` (default: `15`)
- `HEALTH_MAX_INTERVAL_SECONDS` (default: `60`)
- `HEALTH_CB_FAILURE_THRESHOLD` (default: `3`)
- `HEALTH_CB_RECOVERY_SECONDS` (default: `30`)

## 3) Ejecutar runtime

```bash
make run
```

## 4) Verificar métricas

```bash
curl http://localhost:8000/metrics | head
```

Métricas clave:

- `connectors_health_ws_connected`
- `connectors_health_exchange_reachable`
- `connectors_ws_messages_total`
- `connectors_ws_reconnects_total`
- `connectors_health_circuit_open`
- `connectors_health_circuit_failures`

## 5) Tests

```bash
make test
```
