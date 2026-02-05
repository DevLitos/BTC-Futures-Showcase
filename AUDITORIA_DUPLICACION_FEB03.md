# 🔍 AUDITORÍA PROFUNDA DE DUPLICACIÓN DE CÓDIGO

**Fecha**: 3 Febrero 2026  
**Alcance**: Análisis completo del codebase  
**Versión anterior**: AUDITORIA_REFACTOR.md (27 Enero 2026)

---

## 📊 RESUMEN EJECUTIVO

Se identificaron **~650+ líneas de código duplicado** distribuidas en 5 categorías principales:

| Categoría | Severidad | Líneas Estimadas | Archivos Afectados |
|-----------|-----------|------------------|-------------------|
| Módulos shared (indicadores) | 🔴 Crítica | ~125 líneas | 4 archivos |
| Exchanges (patrones internos) | 🟠 Media | ~200 líneas | 3 archivos |
| Commands/Tools | 🔴 Alta | ~180 líneas | 11 archivos |
| Domain/Trading | 🟠 Media | ~100 líneas | 8+ archivos |
| Backtest/Live application | 🟡 Baja | ~50 líneas | 2 archivos |

---

## 🔴 1. DUPLICACIÓN CRÍTICA EN MÓDULOS SHARED

### 1.1 Dos Clases `CachedIndicators` Incompatibles

**Archivos afectados:**
- [indicator_helpers.py](src/shared/indicator_helpers.py)
- [indicator_cache.py](src/shared/indicator_cache.py)

**Problema:** Dos dataclasses con el mismo nombre pero estructuras diferentes:

```python
# indicator_helpers.py (líneas 27-56)
@dataclass
class CachedIndicators:
    timestamp_1m: int = 0
    timestamp_5m: int = 0
    timestamp_1h: int = 0
    df_1m: Optional[pd.DataFrame] = None
    df_5m: Optional[pd.DataFrame] = None
    atr_1m: Optional[float] = None
    atr_5m: Optional[float] = None
    # ... campos por timeframe
```

```python
# indicator_cache.py (líneas 27-53)
@dataclass
class CachedIndicators:
    last_closed_ts: int = 0
    atr: float = 0.0
    atr_values: Optional[np.ndarray] = None
    adx: float = 20.0
    adx_slope: float = 0.0
    # ... campos genéricos
```

**Impacto:** Confusión al importar, potenciales bugs si se usa la versión incorrecta.

**✅ ACCIÓN REQUERIDA:**
```python
# ELIMINAR la clase de indicator_helpers.py
# MANTENER SOLO la de indicator_cache.py que es más genérica
```

---

### 1.2 Funciones ATR Triplicadas

**Archivos afectados:** [indicator_helpers.py](src/shared/indicator_helpers.py)

**Código duplicado:**

```python
# Función 1: calculate_atr_1m (líneas 73-82)
def calculate_atr_1m(df_1m: pd.DataFrame, period: int = 14) -> float:
    if df_1m is None or len(df_1m) < period + 1:
        return 0.0
    atr = calculate_atr(df_1m['high'].to_numpy(), df_1m['low'].to_numpy(), 
                        df_1m['close'].to_numpy(), period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

# Función 2: calculate_atr_5m (líneas 84-93) - IDÉNTICA
def calculate_atr_5m(df_5m: pd.DataFrame, period: int = 14) -> float:
    if df_5m is None or len(df_5m) < period + 1:
        return 0.0
    atr = calculate_atr(df_5m['high'].to_numpy(), df_5m['low'].to_numpy(), 
                        df_5m['close'].to_numpy(), period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

# Función 3: calculate_atr_generic (líneas 95-104) - IDÉNTICA
def calculate_atr_generic(df: pd.DataFrame, period: int = 14) -> float:
    # Mismo código exacto
```

**✅ ACCIÓN REQUERIDA:**
```python
# Mantener SOLO calculate_atr_generic y crear aliases:
def calculate_atr_generic(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR on any timeframe DataFrame."""
    if df is None or len(df) < period + 1:
        return 0.0
    atr = calculate_atr(df['high'].to_numpy(), df['low'].to_numpy(), 
                        df['close'].to_numpy(), period)
    return float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

# Aliases para backward compatibility
calculate_atr_1m = calculate_atr_generic
calculate_atr_5m = calculate_atr_generic
```

---

### 1.3 Función `get_indicator_cache()` Duplicada

**Archivos afectados:**
- [indicator_helpers.py](src/shared/indicator_helpers.py)
- [indicator_cache.py](src/shared/indicator_cache.py)

**Problema:** Dos singletons diferentes con el mismo nombre de función:

```python
# indicator_helpers.py
_indicator_cache: Optional[CachedIndicators] = None

def get_indicator_cache() -> CachedIndicators:  # Retorna dataclass
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = CachedIndicators()
    return _indicator_cache

# indicator_cache.py
_indicator_cache: Optional[IndicatorCache] = None

def get_indicator_cache() -> IndicatorCache:  # Retorna clase completa
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = IndicatorCache()
    return _indicator_cache
```

**✅ ACCIÓN REQUERIDA:**
- Eliminar versión de `indicator_helpers.py`
- Usar solo `indicator_cache.py`

---

### 1.4 Función `ts_to_int()` Duplicada

**Archivos afectados:**
- [indicator_helpers.py](src/shared/indicator_helpers.py)
- [trading_app.py](src/application/live/trading_app.py)

**Código idéntico:**
```python
def ts_to_int(ts) -> int:
    """Convert any timestamp to int (epoch ms)."""
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0
```

**✅ ACCIÓN REQUERIDA:**
- Mover a `src/shared/utils.py`
- Importar desde ahí en ambos archivos

---

## 🔴 2. DUPLICACIÓN EN COMMANDS Y TOOLS

### 2.1 Inicialización de Path del Proyecto (10/11 archivos)

**Patrón duplicado:**
```python
import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent  # O variantes
sys.path.insert(0, str(ROOT))
```

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

**✅ ACCIÓN REQUERIDA:**
```python
# Crear src/bootstrap/path_setup.py
import sys
from pathlib import Path

def ensure_project_root():
    """Ensures project root is in sys.path. Call from any entrypoint."""
    root = Path(__file__).parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
```

---

### 2.2 Configuración de Logging Repetida

**Archivos afectados:** 4+ archivos

**Variantes encontradas:**
```python
# Versión 1
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s'
)

# Versión 2
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)

# Versión 3
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
```

**✅ ACCIÓN REQUERIDA:**
```python
# Crear src/shared/logging_config.py
import logging

def configure_logging(name: str, level: int = logging.INFO, 
                      include_name: bool = True) -> logging.Logger:
    fmt = ('%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s' 
           if include_name else '%(asctime)s - %(levelname)-8s - %(message)s')
    logging.basicConfig(level=level, format=fmt)
    return logging.getLogger(name)
```

---

### 2.3 Carga de Credenciales de Exchange Duplicada

**Archivo:** `tools/run_backtest_kraken.py`

**Código a centralizar:**
```python
def load_env_file():
    """Load environment variables from ~/.trad2_env"""
    env_file = Path.home() / '.trad2_env'
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.startswith('export '):
                        line = line[7:].strip()
                    if '=' in line:
                        key, value = line.split('=', 1)
                        value = value.strip().strip('"').strip("'")
                        os.environ[key.strip()] = value
```

**✅ ACCIÓN REQUERIDA:**
- Mover a `src/config/env_loader.py`
- Reutilizar en todos los entrypoints

---

### 2.4 Descarga de Datos Históricos Duplicada

**Archivos afectados:**
- `tools/backtest_runner.py` → `load_from_ccxt()`
- `tools/run_backtest_kraken.py` → `download_kraken_data()`

**Código casi idéntico:**
```python
# Patrón repetido en ambos:
exchange = ccxt.xxx({'enableRateLimit': True})
since = exchange.parse8601(start_date.isoformat())

while since < exchange.milliseconds():
    chunk = exchange.fetch_ohlcv(symbol, timeframe, since, limit)
    if not chunk:
        break
    ohlcv.extend(chunk)
    since = chunk[-1][0] + 1

df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
```

**✅ ACCIÓN REQUERIDA:**
```python
# Crear src/infrastructure/data/historical_loader.py
class HistoricalDataLoader:
    def __init__(self, exchange_id: str = 'krakenfutures'):
        self.exchange = getattr(ccxt, exchange_id)({'enableRateLimit': True})
    
    def fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
        # Lógica unificada
        ...
```

---

## 🟠 3. DUPLICACIÓN EN DOMAIN/TRADING

### 3.1 Tres Clases `EdgeScore` Diferentes

**Archivos afectados:**
- `src/domain/trading/edge/edge_framework.py` → `EdgeScore` con `score`, `components`, `reasoning`
- `src/domain/trading/scoring/edge_score.py` → `EdgeScore` con `base_score`, `multipliers`, `final_score`
- `src/domain/market/structure/levels.py` → `EdgeScore` con `conviction`, `edge_quality`, `direction`

**Impacto:** Confusión sobre cuál usar, posibles bugs de tipo.

**✅ ACCIÓN REQUERIDA:**
- Consolidar en una sola clase en `src/domain/trading/scoring/edge_score.py`
- Usar type aliases para casos específicos si es necesario

---

### 3.2 Detección de Patrones de Velas Duplicada

**Archivos afectados:**
- `src/domain/market/structure/liquidity.py` → `_detect_sweep_candle_pattern()`
- `src/domain/market/structure/price_action.py` → Detección de engulfing, doji, pin bar

**✅ ACCIÓN REQUERIDA:**
- Crear `src/domain/market/structure/candle_patterns.py`
- Mover toda la lógica de detección de patrones allí

---

### 3.3 Lógica de Size Multiplier Dispersa

**Archivos afectados:**
- `src/domain/trading/state/trading_state.py` → `STATE_SIZE_MULTIPLIERS`
- `src/domain/trading/capital_weighting.py` → `adjust_size_for_conviction()`
- `src/domain/trading/execution_logic/entry_logic.py` → `_compute_final_size()`

**✅ ACCIÓN REQUERIDA:**
- Consolidar en una clase `SizingCalculator` única

---

## 🟠 4. DUPLICACIÓN EN EXCHANGES

### 4.1 Patrón de Timeout con Lock (Repetido ~15 veces)

**Archivo:** `src/infrastructure/exchanges/kraken/execution.py`

**Código repetido:**
```python
async with self._exchange_lock:
    try:
        result = await asyncio.wait_for(
            self.exchange.some_method(...),
            timeout=EXCHANGE_TIMEOUT_MEDIUM
        )
    except asyncio.TimeoutError:
        logger.error(f"⏰ TIMEOUT ...")
        return None
```

**Ubicaciones aproximadas:** líneas 330, 505, 580, 650, 700, 770, etc.

**✅ ACCIÓN REQUERIDA:**
```python
async def _execute_with_timeout(self, coro, timeout=EXCHANGE_TIMEOUT_MEDIUM, 
                                operation_name: str = "operation"):
    async with self._exchange_lock:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"⏰ TIMEOUT en {operation_name} ({timeout}s)")
            return None
```

---

### 4.2 Manejo de Errores REST Repetido (~8 veces)

**Archivo:** `src/infrastructure/exchanges/kraken/execution.py`

**Código repetido:**
```python
except ccxt.errors.OrderNotFound:
    logger.error("Order not found on exchange")
    return None
except ccxt.InsufficientFunds:
    logger.error("Insufficient balance")
    return None
except ccxt.errors.DDoSProtection:
    logger.warning("Exchange rate limit - waiting 30s")
    await asyncio.sleep(30)
    return None
```

**✅ ACCIÓN REQUERIDA:**
- Crear decorator `@handle_exchange_errors`

---

### 4.3 Métodos `set_position_stops` y `set_position_stops_optimized` Duplicados

**Archivo:** `src/infrastructure/exchanges/kraken/execution.py`

**Problema:** Dos métodos de ~170 y ~58 líneas que hacen esencialmente lo mismo.

**✅ ACCIÓN REQUERIDA:**
- Consolidar en un solo método con flag `optimized: bool = False`

---

## 🟡 5. DUPLICACIÓN BACKTEST/LIVE

### 5.1 Inicialización de Filtros Duplicada

**Archivos afectados:**
- `src/application/backtest/backtest_app.py`
- `src/application/live/trading_app.py`

**Código similar:**
```python
# En ambos archivos:
self.kill_filter = KillConditionsFilter(...)
self.black_swan_filter = BlackSwanFilter()
self.htf_filter = HTFFilter(backtest_mode=...)
self.ltf_filter = LTFTriggerFilter(backtest_mode=...)
```

**✅ ACCIÓN REQUERIDA:**
```python
# Crear src/application/common/filter_factory.py
def create_filters(state_machine, capital, risk_engine=None, backtest_mode=False):
    return FilterBundle(
        kill=KillConditionsFilter(state_machine, capital, risk_engine, backtest_mode),
        black_swan=BlackSwanFilter(),
        htf=HTFFilter(backtest_mode),
        ltf=LTFTriggerFilter(backtest_mode),
    )
```

---

## 🔧 6. PATRONES REUTILIZABLES A EXTRAER

### 6.1 Patrón Singleton (15+ instancias)

**Código repetido en múltiples archivos:**
```python
_instance: Optional[Type] = None

def get_instance() -> Type:
    global _instance
    if _instance is None:
        _instance = Type()
    return _instance

def reset_instance():
    global _instance
    _instance = None
```

**✅ ACCIÓN REQUERIDA:**
```python
# Crear src/shared/patterns.py
from typing import TypeVar, Type

T = TypeVar('T')

def singleton(cls: Type[T]) -> Type[T]:
    """Decorator to make a class a singleton."""
    instances = {}
    
    def get_instance(*args, **kwargs) -> T:
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    
    get_instance.reset = lambda: instances.pop(cls, None)
    get_instance._cls = cls
    return get_instance
```

---

### 6.2 Decorator de Retry con Backoff

**Archivo:** `src/infrastructure/exchanges/kraken/execution.py`

**Código reutilizable:**
```python
def with_retry(max_retries: int = 3, base_delay: float = 1.0, 
               max_delay: float = 30.0, retryable_exceptions: tuple = (...)):
    """Decorator for async functions with exponential backoff retry."""
    ...
```

**✅ ACCIÓN REQUERIDA:**
- Mover a `src/shared/decorators.py`
- Usar en cualquier módulo que necesite retry

---

## 📊 7. PLAN DE IMPLEMENTACIÓN

### Fase 1: Quick Wins (1 día)

| Prioridad | Tarea | Impacto |
|-----------|-------|---------|
| 1 | Eliminar `CachedIndicators` duplicado en `indicator_helpers.py` | Alto |
| 2 | Consolidar funciones ATR en una sola | Alto |
| 3 | Eliminar `get_indicator_cache()` duplicado | Alto |
| 4 | Mover `ts_to_int()` a `utils.py` | Medio |

### Fase 2: Infraestructura Común (2 días)

| Prioridad | Tarea | Impacto |
|-----------|-------|---------|
| 5 | Crear `src/bootstrap/path_setup.py` | Medio |
| 6 | Crear `src/shared/logging_config.py` | Medio |
| 7 | Crear `src/config/env_loader.py` | Medio |
| 8 | Crear `src/infrastructure/data/historical_loader.py` | Medio |

### Fase 3: Refactorización Domain (3 días)

| Prioridad | Tarea | Impacto |
|-----------|-------|---------|
| 9 | Consolidar clases `EdgeScore` | Alto |
| 10 | Crear `SizingCalculator` único | Medio |
| 11 | Extraer `candle_patterns.py` | Bajo |

### Fase 4: Refactorización Exchanges (2 días)

| Prioridad | Tarea | Impacto |
|-----------|-------|---------|
| 12 | Crear helper `_execute_with_timeout()` | Alto |
| 13 | Crear decorator `@handle_exchange_errors` | Alto |
| 14 | Consolidar métodos `set_position_stops` | Medio |

---

## 📈 8. MÉTRICAS ESPERADAS POST-REFACTOR

| Métrica | Antes | Después |
|---------|-------|---------|
| Líneas duplicadas | ~650+ | ~50 (residual) |
| Archivos con path setup duplicado | 10 | 0 |
| Clases `CachedIndicators` | 2 | 1 |
| Funciones ATR idénticas | 3 | 1 |
| Singletons con código repetido | 15+ | 0 (usar decorator) |

---

## 🚀 9. PRÓXIMOS PASOS INMEDIATOS

1. **Revisar este documento** con el equipo
2. **Priorizar Fase 1** - cambios de bajo riesgo y alto impacto
3. **Crear branch** `refactor/dedup-feb03`
4. **Tests**: Asegurar que tests existentes pasen después de cada cambio
5. **Deploy gradual**: Empezar con backtest, luego live

---

**Autor**: GitHub Copilot  
**Revisado**: Pendiente
