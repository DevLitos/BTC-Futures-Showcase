# 🔍 ANÁLISIS DE CÓDIGO MUERTO Y DUPLICACIONES
## Sistema de Trading BTC - Jan 27, 2026

---

## 📋 RESUMEN EJECUTIVO

| Categoría | Hallazgos | Severidad |
|-----------|-----------|-----------|
| **Imports no usados** | 5 | Media |
| **Constantes no usadas** | 1 | Baja |
| **Variables obsoletas** | 3 | Media |
| **Clases duplicadas** | 1 | Alta |
| **Código comentado** | 2 | Baja |
| **Funciones que siempre retornan True** | 1 | Media |
| **Caché duplicado** | 2 | Alta |

---

## 1️⃣ IMPORTS NO USADOS

### trading_app.py

#### 1.1 `get_time_provider`, `TimeProvider` (Línea 48)
```python
from src.shared.time_provider import get_time_provider, TimeProvider
```
- **Estado**: NUNCA USADO en el archivo
- **Razón**: Se usa `market_data.get_market_time()` en su lugar
- **Seguro eliminar**: ✅ SÍ

#### 1.2 `calculate_chase_atr` (Línea 43)
```python
from src.domain.market.regime import EdgeRegimeClassifier, RegimeDetector, EntryRegime, calculate_chase_atr
```
- **Estado**: Importado pero no llamado directamente
- **Razón**: Se usa `calculate_chase_atr_helper` de `market_analysis.py` en su lugar
- **Seguro eliminar**: ✅ SÍ (el alias `calculate_chase_atr_helper` en línea 202 es suficiente)

#### 1.3 `PositionSizer` clase (Línea 47)
```python
from src.shared.utils import PositionSizer, get_position_sizer
```
- **Estado**: Se usa `get_position_sizer()` pero `PositionSizer` clase nunca se usa directamente
- **Nota**: `self._position_sizer` se inicializa pero NUNCA se usa (ni un solo `.` call)
- **Seguro eliminar clase**: ✅ SÍ (pero mantener `get_position_sizer`)

---

## 2️⃣ CONSTANTES NO USADAS

### trading_app.py

#### 2.1 `POSITION_CHECK_INTERVAL` (Línea 246)
```python
POSITION_CHECK_INTERVAL = 5  # Check position every 5 seconds
```
- **Estado**: Definida pero NUNCA referenciada
- **Seguro eliminar**: ✅ SÍ

---

## 3️⃣ VARIABLES/ATRIBUTOS OBSOLETOS

### trading_app.py

#### 3.1 `_indicator_cache` dict (Líneas 552-569)
```python
self._indicator_cache = {
    'last_candle_ts_1m': 0,
    'last_candle_ts_5m': 0,
    ...
}
```
- **Estado**: Existe junto con `_indicator_cache_v2` (IndicatorCache)
- **Razón**: Migración incompleta - se mantiene "por compatibilidad" pero `_indicator_cache_v2` es el sistema nuevo
- **Problema**: DOS sistemas de caché activos simultáneamente → duplicación de cálculos
- **Seguro eliminar**: ⚠️ REVISAR - usado en `_get_cached_indicators()` que todavía se llama

#### 3.2 `_position_sizer` (Línea 447)
```python
self._position_sizer = get_position_sizer(...)
```
- **Estado**: Inicializado pero NUNCA usado (`grep` no encuentra `_position_sizer.`)
- **Razón**: El sizing ahora usa `GraduatedSizer` y validators externos
- **Seguro eliminar**: ✅ SÍ

#### 3.3 `missed_tracker` (Línea 677)
```python
self.missed_tracker = MissedOpportunityTracker()
```
- **Estado**: Solo se usa en 2 lugares: `update_pending()` y `log_blocked_signal()`
- **Problema**: `log_blocked_signal` se llama en `entry_orchestrator.py` pero los datos NUNCA se analizan/exportan
- **Seguro eliminar**: ⚠️ NO - funcionalidad útil para análisis futuro, pero considerar si aporta valor

---

## 4️⃣ CLASES/DATACLASSES DUPLICADAS

### 🔴 CRÍTICO: `SizingResult` definida 3 VECES

| Archivo | Línea | Atributos |
|---------|-------|-----------|
| entry_validators.py | 46 | score_size_multiplier, rr_size_multiplier, etc. |
| entry_execution_logic.py | 69 | position_size, combined_multiplier, etc. |
| entry_orchestrator.py | 95 | size, size_usd, multipliers, etc. |

**Problema**: Confusión sobre cuál usar, inconsistencia en tipos
**Solución**: Unificar en UN SOLO archivo (`src/domain/trading/types.py`)

---

## 5️⃣ FUNCIONES QUE SIEMPRE RETORNAN True

### trading_app.py

#### 5.1 `_check_session_filter` (Línea 5271)
```python
def _check_session_filter(self, market_time: datetime) -> tuple[bool, str]:
    """SESSION FILTER: 24/7 trading for BTC"""
    return True, f"Session OK: 24/7 trading ({market_time.hour}:00 UTC)"
```
- **Estado**: SIEMPRE retorna True
- **Razón**: BTC opera 24/7, el filtro perdió sentido
- **Seguro eliminar**: ⚠️ REVISAR quién lo llama - si nadie, eliminar

---

## 6️⃣ COMENTARIOS DE CÓDIGO ELIMINADO

### trading_app.py

#### 6.1 Línea 5384
```python
# 🔥 REMOVED: _check_not_compressed - was disabled (always returned True)
```

#### 6.2 Línea 5390
```python
# 🔥 REMOVED: _check_not_duplicate_zone - was disabled (always returned True)
```

**Estado**: Estos comentarios indican que funciones fueron eliminadas pero los comentarios permanecen
**Seguro eliminar**: ✅ SÍ (los comentarios solos no aportan valor)

---

## 7️⃣ SISTEMAS DE CACHÉ DUPLICADOS

### 🔴 CRÍTICO: Dos sistemas de caché de indicadores

#### Sistema 1: `_indicator_cache` dict (legacy)
- Ubicación: trading_app.py línea 552
- Usado por: `_get_cached_indicators()`

#### Sistema 2: `_indicator_cache_v2` (IndicatorCache singleton)
- Ubicación: trading_app.py línea 549
- Implementación: `src/shared/indicator_cache.py`

**Problema**: 
- Ambos calculan los mismos indicadores (ATR, ADX, RSI, BB)
- `_get_cached_indicators()` usa el dict viejo
- Otros módulos usan el singleton nuevo
- CPU duplicado, estado inconsistente posible

**Solución**: Migrar TODO a `IndicatorCache` y eliminar el dict

---

## 8️⃣ DUPLICACIÓN DE CÁLCULOS ATR

### Archivos que calculan `calculate_atr()`:

| Archivo | Llamadas | Contexto |
|---------|----------|----------|
| trading_app.py | 14 | Múltiples timeframes |
| entry_validators.py | 1 | 1m ATR |
| idle_state_handlers.py | 2 | 5m y 1h ATR |
| entry_orchestrator.py | 1 | ADX coherence |
| indicator_cache.py | 1 | Centralizado |
| indicator_helpers.py | 3 | Helpers |
| market_analysis.py | 1 | Expected R |
| filter_checks.py | 4 | Volatility checks |

**Problema**: El mismo ATR se calcula múltiples veces por tick
**Solución**: TODO debe pasar por `IndicatorCache`

---

## 9️⃣ ARCHIVO BACKUP OBSOLETO

### trading_app.py.bak
- **Ubicación**: src/application/live/trading_app.py.bak
- **Tamaño**: 7,807 líneas
- **Estado**: Versión anterior del archivo
- **Seguro eliminar**: ✅ SÍ (está en git history)

---

## 📊 RECOMENDACIONES DE ACCIÓN

### 🟢 ELIMINAR INMEDIATAMENTE (Bajo riesgo)

1. **Import no usado**: `TimeProvider`, `get_time_provider`
2. **Constante no usada**: `POSITION_CHECK_INTERVAL`
3. **Archivo backup**: `trading_app.py.bak`
4. **Comentarios de código eliminado** (líneas 5384, 5390)

### 🟡 ELIMINAR CON CUIDADO (Medio riesgo)

1. **`_position_sizer`**: Verificar que nada lo use via reflection
2. **`calculate_chase_atr` import**: Mantener solo el alias
3. **`PositionSizer` clase**: Mantener solo `get_position_sizer`

### 🔴 REFACTORIZAR (Alto impacto)

1. ✅ **~~Unificar `SizingResult`~~**: COMPLETADO
   - `DefensiveSizingResult` en entry_validators.py
   - `ExecutionSizingResult` en entry_execution_logic.py
   - `OrchestrationSizingResult` en entry_orchestrator.py
2. ✅ **~~Eliminar `_indicator_cache` dict~~**: Ya eliminado, usa `IndicatorCache` singleton
3. **Centralizar cálculos ATR**: Todo por el cache

---

## 🎯 IMPACTO ESTIMADO

| Acción | Líneas eliminadas | Riesgo |
|--------|-------------------|--------|
| Imports no usados | ~5 | Bajo |
| Constantes | ~1 | Bajo |
| Variables obsoletas | ~30 | Medio |
| Caché duplicado | ~100 | Alto (requiere testing) |
| Clases duplicadas | ~60 | Alto (requiere unificación) |

**Total estimado**: ~200 líneas de código redundante

---

## ✅ VERIFICACIÓN PRE-ELIMINACIÓN

Antes de eliminar cualquier código:

```bash
# 1. Buscar usos en todo el proyecto
grep -r "nombre_funcion" src/ tests/

# 2. Ejecutar tests
uv run pytest tests/ -v

# 3. Verificar que la app arranca
uv run python -c "from src.application.live.trading_app import SignalEngine; print('OK')"
```

---

*Reporte generado: Jan 27, 2026*
