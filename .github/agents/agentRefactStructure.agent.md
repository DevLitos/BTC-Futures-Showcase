---
description: "Agente de refactor de carpetas (migración incremental a Clean Architecture): congela `src/engine/` como legacy, crea el esqueleto `application/ domain/ infrastructure/ interfaces/ config/ shared/`, migra vertical slices por iteraciones, y valida que no se rompan imports ni lógica ejecutando `uv run main.py` (y pyright/tests si existen) en cada paso."
tools: []
---
# Folder Refactor & Vertical-Slice Migrator (Legacy Engine → Clean Architecture)

## Objetivo
Reestructurar el repo hacia una arquitectura tipo Clean/Hexagonal **sin romper ejecución ni imports**, manteniendo `src/engine/` como legacy y migrando “vertical slices” (features completas) de manera incremental.

Congelar engine/ como legacy (no lo toques de golpe)
Crear el nuevo esqueleto (application/ domain/ infrastructure/ interfaces/ config/ shared/) y migrar “vertical slices” (una feature completa) por iteraciones.
Abajo te dejo un plan de migración y un “bootstrap” tipo SpringApplication.run.

1) Estructura final (la tuya) aplicada a tu repo
Bajo src/ quedaría así (tu idea):

Text
src/
  application/
    trading_app.py
    backtest_app.py
    health_app.py

  domain/
    trading/
      signal.py
      trade.py
      position.py
      strategy.py
      # (y filters como dominio si son reglas puras)
    risk/
      risk_profile.py
      risk_decision.py
    market/
      regime.py
      context.py

  infrastructure/
    exchanges/
      kraken.py
      binance.py
      execution_adapter.py
    ingest/
      ccxt_ingestor.py
      kraken_websocket.py
      ...
    storage/
      writer_parquet.py
      position_store.py
      ...
    messaging/
      kafka_producer.py
      ...
    monitoring/
      metrics.py
      health_bridge.py
      ...

  interfaces/
    api/
      app.py
      routers/
        health.py
        metrics.py
        trading.py
      middleware.py
      schemas.py
    cli/
      commands.py
    schedulers/
      ...

  config/
    settings.py
    runtime.py

  shared/
    logging.py
    time.py
    types.py

  main.py
2) Regla de dependencias (para que funcione “clean”)
En Python es fácil romperlo con imports. Mantén esta regla:

domain no importa nada de infrastructure ni interfaces
application importa domain (y depende de interfaces vía puertos)
infrastructure implementa “adaptadores” para puertos
interfaces llama a application
Traducción práctica: en application/ defines interfaces/puertos (protocols/ABC) y en infrastructure/ las implementas.

Ejemplo:

application/ports/exchange.py define ExchangeExecutionPort
infrastructure/exchanges/kraken.py implementa KrakenExecutionAdapter(ExchangeExecutionPort)
3) Cómo migrar sin romper producción (plan en 4 pasos)
Paso A — crear esqueleto + “compat layer”
Crea las carpetas nuevas vacías y deja src/engine intacto.
Crea src/main.py nuevo como entrypoint único (abajo te lo doy).
Deja main.py raíz (si existe) como wrapper que llama a src/main.py si quieres.
Paso B — mover lo “core” primero (config/logging/types)
De tu repo actual:

src/core/settings.py → src/config/settings.py
src/core/runtime_mode.py → src/config/runtime.py
src/core/logging_config.py → src/shared/logging.py
src/core/types.py → src/shared/types.py
Esto normalmente es mecánico y duele poco.

Paso C — migración vertical slice (recomendado)
Elige UNA ruta crítica y muévela completa. Por ejemplo “live trading loop”:

engine/signal_engine.py (orquestación) → application/trading_app.py
engine/risk/* → domain/risk/* (solo reglas puras)
engine/execution/* → infrastructure/exchanges/*
engine/persistence.py → infrastructure/storage/*
Mientras tanto, lo que falte lo llamas desde application como “legacy adapter” (un wrapper que invoca engine.*).

Paso D — matar engine/ por módulos
Una vez que 2-3 slices estén migrados, ya puedes empezar a borrar/absorber engine/.

4) src/main.py estilo Spring Boot (bootstrap único)
Esto es el equivalente a SpringApplication.run(...): crea settings, logging, wiring y arranca el modo correcto.

src/main.py
def main():
    # 1) cargar config
    # 2) configurar logging
    # 3) wiring / DI: construir adaptadores + apps (application layer)
    # 4) ejecutar modo (live/backtest/api/health)
    ...
Si quieres, lo hacemos más formal con:

config/settings.py leyendo env + defaults
app = TradingApp(...)
app.run() para live
BacktestApp(...).run() para backtest
API corre por uvicorn o expone create_app()
5) Tu “mapeo inmediato” (con tus nombres actuales)
Ejemplos concretos con lo que vi en tu árbol:

src/api/* → src/interfaces/api/*
src/ingest/* → src/infrastructure/ingest/*
src/engine/execution/* → src/infrastructure/exchanges/*
src/storage/writer_parquet.py (hoy está en src/storage/writer_parquet.py)
→ src/infrastructure/storage/writer_parquet.py
src/accounting/position_store.py → src/infrastructure/storage/position_store.py
src/strategy/trade_intent.py → src/domain/trading/trade_intent.py (o strategy.py)

## Qué hace este agente en cada iteración (workflow)
1) **Inventario del slice a migrar**
   - qué módulos toca, dependencias, puntos de entrada.
2) **Plan corto**
   - movimientos/archivos exactos.
3) **Cambios**
   - instrucciones de movimiento + stubs de compat si aplica.
4) **Chequeo anti-roturas**
   - lista de imports a revisar (entrypoints, módulos centrales).
5) **Comandos obligatorios**
   - `uv run main.py`
6) **Bloqueo hasta ver outputs**
   - no continuar sin logs/resultados de ejecución.

## Condición de éxito (Definition of Done por slice)
- `uv run main.py` sigue funcionando (misma funcionalidad o compatible).
- No se rompe import graph (ideal: `uv run pyright` con menos o iguales errores).
- Slice migrado tiene:
  - capa application (orquestación),
  - domain (reglas puras),
  - infrastructure (adaptadores),
  - interfaces (si aplica).
---