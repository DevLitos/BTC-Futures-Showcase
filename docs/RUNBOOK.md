# Runbook — BTC Runtime

## Arranque

El modo debe elegirse explícitamente. El modo `live` puede operar con dinero real; el modo `backtest` usa datos históricos. Este proyecto no implementa paper trading.

```bash
MODE=backtest make run
```

En backtest se esperan logs de descarga/procesamiento histórico. En live se espera:

- `ws connected: krakenfutures`

## Parada

- `Ctrl+C` para shutdown graceful.
- El runtime cancela tareas, cierra WS/REST y deja gauges de salud en 0.

## Health y observabilidad

- Endpoint de métricas: `http://localhost:8000/metrics`
- Grafana: no está implementado en este repositorio; las métricas deben conectarse manualmente a Prometheus/Grafana.
- Circuit breaker:
  - `connectors_health_circuit_open`
  - `connectors_health_circuit_failures`

## Diagnóstico rápido

### WS caído o reconectando

```bash
curl -s http://localhost:8000/metrics | grep connectors_ws_reconnects_total
```

### REST health en fallo continuo

```bash
curl -s http://localhost:8000/metrics | grep connectors_health_exchange_reachable
curl -s http://localhost:8000/metrics | grep connectors_health_circuit_open
```

## Limpieza de entorno

```bash
make clean
```

## Alcance

Este runbook cubre el runtime activo, incluidos conectores, estrategias, riesgo, gestión de posiciones y backtest. La documentación histórica de `deprecated/` no define el comportamiento actual.
