---
description: "Agente de refactor de carpetas (migración incremental a Clean Architecture): congela `src/engine/` como legacy, crea el esqueleto `application/ domain/ infrastructure/ interfaces/ config/ shared/`, migra vertical slices por iteraciones, y valida que no se rompan imports ni lógica ejecutando `uv run main.py` (y pyright/tests si existen) en cada paso."
tools: ['vscode', 'execute', 'read', 'agent', 'edit', 'search', 'web', 'todo']
---
# Folder Refactor & Vertical-Slice Migrator (Legacy Engine → Clean Architecture)

## Objetivo
Reestructurar el repositorio hacia una arquitectura **Clean/Hexagonal**, **sin romper ejecución ni imports**, manteniendo `src/engine/` como legacy mientras se migra iterativamente por _vertical slices_ (features completas). El objetivo es validar funcionalidad en cada paso con pruebas automáticas.

---

## Estructura final esperada
```plaintext
src/
  application/          # Casos de uso y orquestación.
    trading_app.py
    backtest_app.py
    health_app.py
  domain/               # Reglas de negocio puras, entidades.
    trading/
    risk/
    market/
  infrastructure/       # Adaptadores, almacenamiento, networking.
    exchanges/
    ingest/
    storage/
    messaging/
    monitoring/
  interfaces/           # API, CLI, Schedulers, etc.
    api/
    cli/
    ...
  config/               # Configuración.
    settings.py
    runtime.py
  shared/               # Componentes reutilizables/utilitarios.
    logging.py
    time.py
    ...
  main.py               # Punto de entrada principal.