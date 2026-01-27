# Clean Architecture - Estructura del Proyecto

## Visión General

Este proyecto ha sido reestructurado siguiendo los principios de **Clean Architecture** (también conocida como Hexagonal/Ports and Adapters). La migración se realizó de forma gradual usando **re-exports** para mantener compatibilidad hacia atrás.

## Estructura de Capas

```
src/
├── domain/           # 🎯 CAPA DE DOMINIO (reglas de negocio puras)
│   ├── trading/      # Entidades de trading (Side, Order, Position)
│   ├── risk/         # Lógica de gestión de riesgo
│   ├── market/       # Análisis de mercado y detección de régimen
│   └── events/       # Eventos de dominio
│
├── application/      # 📋 CAPA DE APLICACIÓN (casos de uso)
│   └── SignalEngine  # Orquestación principal del trading
│
├── infrastructure/   # 🔌 CAPA DE INFRAESTRUCTURA (adaptadores externos)
│   ├── exchanges/    # Conectores a exchanges (Kraken, Binance)
│   ├── ingest/       # Ingesta de datos (WebSocket, REST)
│   ├── storage/      # Persistencia (Parquet, PostgreSQL)
│   ├── messaging/    # Mensajería (Kafka)
│   └── monitoring/   # Observabilidad (Prometheus)
│
├── interfaces/       # 🖥️ CAPA DE INTERFACES (puntos de entrada)
│   ├── api/          # REST API (FastAPI)
│   ├── cli/          # Línea de comandos
│   └── output/       # Salida (Telegram, consola)
│
├── config/           # ⚙️ CONFIGURACIÓN
│   └── settings.py   # Validación con Pydantic
│
└── shared/           # 🔧 UTILIDADES COMPARTIDAS
    └── logging.py    # Configuración de logging
```

## Estado de Migración: ALIAS (Re-exports)

La migración actual usa **re-exports** para mantener compatibilidad:

| Nueva Ubicación | Re-exporta desde | Estado |
|----------------|------------------|--------|
| `src.domain` | `src.core.types`, `src.core.events` | ✅ Alias |
| `src.domain.risk` | `src.engine.risk` | ✅ Alias |
| `src.domain.market` | `src.engine.regime` | ✅ Alias |
| `src.infrastructure.exchanges` | `src.engine.execution` | ✅ Alias |
| `src.infrastructure.ingest` | `src.ingest` | ✅ Alias |
| `src.application` | `src.engine.signal_engine` | ✅ Alias |
| `src.config` | `src.core.settings` | ✅ Alias |

### ¿Por qué re-exports?

1. **Sin romper código existente**: Los imports de `src.engine` siguen funcionando
2. **Migración gradual**: Se puede mover lógica poco a poco
3. **Tests siguen pasando**: No hay cambios en la funcionalidad
4. **Rollback fácil**: Si algo falla, se revierten los alias

## Uso

### Imports Recomendados (Nueva Arquitectura)

```python
# Domain layer
from src.domain import Side, OrderType, TradeIntent, get_event_bus
from src.domain.risk import RiskEngine, Position
from src.domain.market import TrendIntelligence, RegimeDetector

# Infrastructure layer
from src.infrastructure import RESTExecutionEngine, KrakenFuturesWebSocket
from src.infrastructure.exchanges import ExchangeReconciler

# Application layer
from src.application import SignalEngine, SynchronizedMultiTimeframeBuffer

# Config
from src.config import get_settings, RuntimeMode
```

### Imports Legacy (Siguen Funcionando)

```python
# Estos imports siguen funcionando para compatibilidad
from src.core.types import Side, OrderType
from src.engine.signal_engine import SignalEngine
from src.engine.risk.risk_engine import RiskEngine
```

## Principios de Clean Architecture

### 1. Dependencia hacia adentro
```
interfaces → application → domain
           ↓
infrastructure
```

### 2. Domain no depende de nada externo
- Sin imports de ccxt, websockets, fastapi
- Solo Python puro + numpy para cálculos

### 3. Infrastructure implementa interfaces
- Fácil de mockear para tests
- Intercambiable (ej: Kraken ↔ Binance)

## Próximos Pasos (Migración Completa)

1. **Fase 1 (Actual)**: ✅ Estructura de carpetas + re-exports
2. **Fase 2**: Mover lógica real al dominio
3. **Fase 3**: Invertir dependencias (legacy re-exporta de nuevo)
4. **Fase 4**: Eliminar módulos legacy (`src/engine/` → `src/domain/`)

## Testing

```bash
# Verificar que los imports funcionan
uv run python -c "from src.domain import Side; print('✅ OK')"

# Ejecutar el sistema
uv run main.py
```
