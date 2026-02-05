# 📋 TAREAS PENDIENTES DE REFACTORIZACIÓN
## Consolidación de AUDITORIA_DUPLICACION_FEB03 + COMPLETE_ARCHITECTURE_ANALYSIS_FEB05
### Fecha: 5 de Febrero, 2026

---

## 🎯 RESUMEN EJECUTIVO

| Fuente | Items Identificados | Estado |
|--------|---------------------|--------|
| **AUDITORIA_DUPLICACION_FEB03** | 23 duplicaciones | ⏳ Pendiente |
| **COMPLETE_ARCHITECTURE_FEB05** | 30+ componentes muertos | ⏳ Pendiente |
| **Total estimado** | ~650 líneas duplicadas + 30 clases muertas | ⏳ |

---

## 🔴 PRIORIDAD 1: ELIMINAR CÓDIGO MUERTO (1-2 horas)

### 1.1 Archivo Completo a Eliminar
| Archivo | Razón | Acción |
|---------|-------|--------|
| `src/domain/trading/unified_intelligence.py` | **100% MUERTO** - Nunca se consulta | ❌ DELETE |

**Contenido muerto en este archivo:**
- `UnifiedTradingIntelligence` - nunca se instancia
- `DirectionSource` - duplica `SignalPriority`
- `DirectionDecision` - duplica `DirectionSignal`
- `RegimeMode` (versión duplicada) - usar `regime_context.py`

### 1.2 Código Muerto en Archivos Existentes

| Archivo | Componente | Acción |
|---------|------------|--------|
| `src/domain/market/filters/filters.py` | `MTFStructureFilter` (~100 líneas) | ❌ Eliminar (comentado) |
| `src/domain/market/regime/trading_regime.py:43` | `TrendDirection` enum | ❌ Eliminar (usar `trend_intelligence.py`) |

---

## 🔴 PRIORIDAD 2: DUPLICACIONES CRÍTICAS EN SHARED (2-3 horas)

### 2.1 Dos `CachedIndicators` Incompatibles

| Archivo | Mantener? |
|---------|-----------|
| `src/shared/indicator_cache.py` | ✅ MANTENER |
| `src/shared/indicator_helpers.py:27-56` | ❌ ELIMINAR clase |

**Acción:** Eliminar la definición de `CachedIndicators` en `indicator_helpers.py`

### 2.2 Tres Funciones ATR Idénticas

| Función | Archivo:Línea | Acción |
|---------|---------------|--------|
| `calculate_atr_1m` | indicator_helpers.py:73 | ❌ Reemplazar con alias |
| `calculate_atr_5m` | indicator_helpers.py:84 | ❌ Reemplazar con alias |
| `calculate_atr_generic` | indicator_helpers.py:95 | ✅ MANTENER |

**Solución:**
```python
# Mantener solo:
def calculate_atr_generic(df: pd.DataFrame, period: int = 14) -> float:
    ...

# Aliases para backward compatibility:
calculate_atr_1m = calculate_atr_generic
calculate_atr_5m = calculate_atr_generic
```

### 2.3 Dos `get_indicator_cache()` Diferentes

| Archivo | Retorna | Acción |
|---------|---------|--------|
| `indicator_cache.py` | `IndicatorCache` (clase completa) | ✅ MANTENER |
| `indicator_helpers.py` | `CachedIndicators` (dataclass) | ❌ ELIMINAR |

### 2.4 `ts_to_int()` Duplicada

| Archivo | Acción |
|---------|--------|
| `indicator_helpers.py` | ❌ ELIMINAR |
| `trading_app.py` | ❌ ELIMINAR |
| **NUEVO:** `src/shared/utils.py` | ✅ CREAR aquí |

---

## 🟠 PRIORIDAD 3: INFRAESTRUCTURA COMÚN (4-6 horas)

### 3.1 Path Setup Duplicado en 10+ Archivos

**Archivos afectados:**
- `cmd/backtest.py`
- `cmd/live.py`
- `cmd/replay.py`
- `tools/backtest_runner.py`
- `tools/cancel_all_orders.py`
- `tools/massive_backtest_mock.py`
- `tools/ml_feature_validation.py`
- `tools/probe_max_monte_carlo.py`
- `tools/probing_backtest_analysis.py`
- `tools/run_backtest_kraken.py`

**Solución:** Crear `src/bootstrap/path_setup.py`:
```python
import sys
from pathlib import Path

def ensure_project_root():
    root = Path(__file__).parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
```

### 3.2 Configuración de Logging Repetida

**Crear:** `src/shared/logging_config.py`
```python
import logging

def configure_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    fmt = '%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s'
    logging.basicConfig(level=level, format=fmt)
    return logging.getLogger(name)
```

### 3.3 Carga de Credenciales Duplicada

**Crear:** `src/config/env_loader.py` con función `load_env_file()`

### 3.4 Descarga de Datos Históricos Duplicada

**Crear:** `src/infrastructure/data/historical_loader.py` con clase `HistoricalDataLoader`

---

## 🟠 PRIORIDAD 4: CONSOLIDAR RÉGIMEN (4-8 horas)

### 4.1 ⚠️ CAOS: 13+ Definiciones de Régimen

**Problema crítico:** El concepto "régimen" está definido en 13+ lugares diferentes.

### Enums de Régimen a Consolidar

| Enum Actual | Archivo | Acción |
|-------------|---------|--------|
| `EntryRegime` | regime_detector.py | ⏳ Evaluar |
| `ManagementRegime` | market_regime.py | ⏳ Evaluar |
| `TradingRegime` | trading_regime.py | ⏳ Evaluar |
| `EdgeRegime` | edge_regime_classifier.py | ⏳ Evaluar |
| `RegimeMode` | regime_context.py | ✅ MANTENER (SINGLE SOURCE) |
| `RegimeMode` | unified_intelligence.py | ❌ ELIMINAR (con archivo) |
| `MarketRegime` | common/types.py | ⏳ Evaluar |
| `TrailingRegime` | trailing_stop.py | ✅ Diferente propósito |

### Clasificadores de Régimen (6 Diferentes!)

| Clasificador | Archivo | Propósito |
|--------------|---------|-----------|
| `RegimeClassifier` | regime_classifier.py | Entry decisions |
| `ManagementRegimeClassifier` | market_regime.py | Position management |
| `TradingRegimeClassifier` | trading_regime.py | Trading mode |
| `EdgeRegimeClassifier` | edge_regime_classifier.py | Edge validity |
| `RegimeContextManager` | regime_context.py | **SINGLE SOURCE** |
| `ActivityRegimeAnalyzer` | activity_regime.py | Activity scoring |

**Recomendación:** Consolidar a 3 clasificadores max:
1. `RegimeContextManager` - Contexto general (MANTENER)
2. `EdgeRegimeClassifier` - Validez de edges (MANTENER)
3. `ManagementRegimeClassifier` - Gestión de posición (MANTENER)

### 3x `get_regime_classifier()` Retornando Clases Diferentes

| Archivo | Retorna |
|---------|---------|
| regime_classifier.py:257 | `RegimeClassifier` |
| market_regime.py:408 | `ManagementRegimeClassifier` |
| trading_regime.py:558 | `TradingRegimeClassifier` |

**Recomendación:** Renombrar funciones para claridad.

---

## 🟡 PRIORIDAD 5: EXCHANGES (3-4 horas)

### 5.1 Patrón Timeout+Lock Repetido ~15 veces

**Archivo:** `src/infrastructure/exchanges/kraken/execution.py`

**Solución:** Crear helper:
```python
async def _execute_with_timeout(self, coro, timeout=30.0, operation: str = "op"):
    async with self._exchange_lock:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"⏰ TIMEOUT en {operation}")
            return None
```

### 5.2 Manejo de Errores REST Repetido ~8 veces

**Solución:** Crear decorator `@handle_exchange_errors`

### 5.3 `set_position_stops` vs `set_position_stops_optimized`

**Problema:** Dos métodos de ~170 y ~58 líneas haciendo lo mismo.

**Solución:** Consolidar en uno con flag `optimized: bool = False`

---

## 🟡 PRIORIDAD 6: DOMAIN TRADING (2-3 horas)

### 6.1 Tres Clases `EdgeScore` Diferentes

| Archivo | Campos |
|---------|--------|
| edge_framework.py | score, components, reasoning |
| scoring/edge_score.py | base_score, multipliers, final_score |
| structure/levels.py | conviction, edge_quality, direction |

**Solución:** Consolidar en `src/domain/trading/scoring/edge_score.py`

### 6.2 Lógica de Size Multiplier Dispersa

| Archivo | Componente |
|---------|------------|
| trading_state.py | `STATE_SIZE_MULTIPLIERS` |
| capital_weighting.py | `adjust_size_for_conviction()` |
| entry_logic.py | `_compute_final_size()` |

**Solución:** Crear `SizingCalculator` único

### 6.3 Detección de Patrones de Velas Duplicada

| Archivo | Función |
|---------|---------|
| liquidity.py | `_detect_sweep_candle_pattern()` |
| price_action.py | Detección de engulfing, doji, pin bar |

**Solución:** Crear `src/domain/market/structure/candle_patterns.py`

---

## 🟢 PRIORIDAD 7: PATRONES REUTILIZABLES (1-2 horas)

### 7.1 Patrón Singleton Repetido 15+ veces

**Código repetido:**
```python
_instance = None
def get_instance(): ...
def reset_instance(): ...
```

**Solución:** Crear `src/shared/patterns.py` con decorator `@singleton`

### 7.2 Retry con Backoff

**Solución:** Mover a `src/shared/decorators.py` y reutilizar

---

## 📊 CHECKLIST DE IMPLEMENTACIÓN

### Fase 1: Eliminación (1 día) ⬜
- [ ] Eliminar `unified_intelligence.py` completo
- [ ] Eliminar `TrendDirection` duplicado en `trading_regime.py`
- [ ] Eliminar `MTFStructureFilter` comentado en `filters.py`
- [ ] Eliminar `CachedIndicators` duplicado en `indicator_helpers.py`
- [ ] Consolidar funciones ATR (3 → 1 + aliases)
- [ ] Eliminar `get_indicator_cache()` duplicado
- [ ] Mover `ts_to_int()` a `utils.py`

### Fase 2: Infraestructura (2 días) ⬜
- [ ] Crear `src/bootstrap/path_setup.py`
- [ ] Crear `src/shared/logging_config.py`
- [ ] Crear `src/config/env_loader.py`
- [ ] Crear `src/infrastructure/data/historical_loader.py`
- [ ] Actualizar 10+ archivos que usan path setup

### Fase 3: Régimen (3 días) ⬜
- [ ] Documentar cuál clasificador usar para qué
- [ ] Renombrar `get_regime_classifier()` en cada archivo
- [ ] Evaluar consolidación de enums
- [ ] Marcar archivos deprecated

### Fase 4: Exchanges (2 días) ⬜
- [ ] Crear `_execute_with_timeout()` helper
- [ ] Crear `@handle_exchange_errors` decorator
- [ ] Consolidar `set_position_stops` métodos

### Fase 5: Domain (2 días) ⬜
- [ ] Consolidar `EdgeScore` clases
- [ ] Crear `SizingCalculator`
- [ ] Extraer `candle_patterns.py`
- [ ] Crear `@singleton` decorator

---

## 📈 MÉTRICAS ESPERADAS POST-REFACTOR

| Métrica | Antes | Después |
|---------|-------|---------|
| Líneas duplicadas | ~650+ | ~50 |
| Clases muertas | ~30 | 0 |
| Archivos de régimen | 9 | 5-6 |
| Definiciones de régimen | 13+ | 5-6 |
| Funciones ATR | 3 | 1 |
| Singletons repetidos | 15+ | 0 (decorator) |

---

## ⏱️ TIEMPO ESTIMADO TOTAL

| Fase | Tiempo |
|------|--------|
| Fase 1: Eliminación | 1 día |
| Fase 2: Infraestructura | 2 días |
| Fase 3: Régimen | 3 días |
| Fase 4: Exchanges | 2 días |
| Fase 5: Domain | 2 días |
| **TOTAL** | **10 días** |

---

## 🚨 RIESGOS Y MITIGACIONES

| Riesgo | Mitigación |
|--------|------------|
| Romper imports existentes | Crear aliases temporales |
| Tests fallando | Ejecutar tests después de cada cambio |
| Funcionalidad perdida | Revisar usos con grep antes de eliminar |
| Merge conflicts | Trabajar en branch dedicado |

---

## 📝 NOTAS

1. **NO TOCAR** archivos que están funcionando bien (edge_state_manager.py, trading_app.py)
2. **PRIORIZAR** eliminación de código muerto sobre refactorización
3. **DOCUMENTAR** cada cambio en commits claros
4. **TESTS** obligatorios antes de merge

---

*Generado: 5 Febrero 2026*
*Fuentes: AUDITORIA_DUPLICACION_FEB03.md + COMPLETE_ARCHITECTURE_ANALYSIS_FEB05.md*
