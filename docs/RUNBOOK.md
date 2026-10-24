# Runbook — Connectors-only Runtime

## Arranque

```bash
make run
```

Verificar logs esperados:

- `CONNECTORS-ONLY MODE`
- `ws connected: krakenfutures`

## Parada

- `Ctrl+C` para shutdown graceful.
- El runtime cancela tareas, cierra WS/REST y deja gauges de salud en 0.

## Health y observabilidad

- Endpoint de métricas: `http://localhost:8000/metrics`
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

Este runbook aplica únicamente al runtime connectors-only. La lógica de trading se implementará encima de esta base.
