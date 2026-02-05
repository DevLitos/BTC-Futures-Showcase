# Análisis de Duplicación de Código - Trading Domain

**Fecha:** 3 de Febrero, 2026  
**Alcance:** `/src/domain/trading/` (edge/, scoring/, strategy/, state/, execution_logic/, entities/, cost/)

---

## Resumen Ejecutivo

Se identificaron **7 categorías principales de duplicación** en el dominio de trading:

| Categoría | Severidad | Archivos Afectados | Esfuerzo Refactor |
|-----------|-----------|-------------------|-------------------|
| GateResult/FilterResult duplicados | 🔴 Alta | 4 archivos | Medio |
| Cálculo de indicadores repetido | 🟠 Media | 6+ archivos | Bajo |
| Patrón de validación de gates | 🔴 Alta | 3 archivos | Alto |
| Lógica de size multiplier | 🟡 Baja | 3 archivos | Bajo |
| Cálculo de expectancy | 🟠 Media | 2 archivos | Bajo |
| Estado/transiciones duplicadas | 🟡 Baja | 2 archivos | Medio |
| Detección de patrones de velas | 🟠 Media | 3 archivos | Medio |

---

## 1. 🔴 Clases `GateResult` Duplicadas (CRÍTICO)

### Problema
Existen **3 clases distintas llamadas `GateResult`** con estructuras similares pero incompatibles:

### Ubicación 1: `execution_logic/entry_gate.py` (líneas 55-66)
```python
@dataclass
class GateResult:
    """Result from a single gate evaluation."""
    name: str
    gate_type: GateType
    category: GateCategory
    passed: bool
    size_multiplier: float = 1.0      # 1.0 = full, 0.5 = half, 0.25 = probe
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
```

### Ubicación 2: `execution_logic/entry_quality.py` (líneas 39-54)
```python
@dataclass
class GateResult:
    """Result of Entry Quality Gate check"""
    decision: GateDecision
    reason: str
    
    # Modifiers when not blocked
    max_size_multiplier: float = 1.0
    require_limit: bool = False
    sl_multiplier: float = 1.0
    require_confirmations: int = 0
    
    # Additional context
    microtrend_blocked: bool = False
    chase_blocked: bool = False
    session_blocked: bool = False
```

### Ubicación 3: `strategy/pullback_short.py` (líneas 53-59)
```python
@dataclass
class GateResult:
    """Result of a gate check"""
    passed: bool
    gate: PullbackGate
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)
```

### Impacto
- Confusión semántica: mismo nombre, diferentes estructuras
- Imposible crear funciones genéricas de validación
- Dificulta testing y debugging

### Solución Recomendada
```python
# src/domain/trading/shared/gate_types.py
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from enum import Enum

class GateType(Enum):
    HARD = "HARD"
    SOFT = "SOFT"

@dataclass
class BaseGateResult:
    """Base result for all gate evaluations."""
    name: str
    passed: bool
    reason: str
    size_multiplier: float = 1.0
    details: Dict[str, Any] = field(default_factory=dict)

@dataclass 
class EntryGateResult(BaseGateResult):
    """Extended result for entry gates."""
    gate_type: GateType = GateType.SOFT
    category: str = ""
    require_limit: bool = False
    sl_multiplier: float = 1.0
```

---

## 2. 🔴 Clases `FilterResult` vs `GateResult` Conceptualmente Iguales

### Problema
`FilterResult` en `intelligent_filter.py` es conceptualmente idéntico a `GateResult`:

### `intelligent_filter.py` (líneas 46-56)
```python
@dataclass
class FilterResult:
    """Result from a single filter check"""
    name: str
    category: FilterCategory
    severity: FilterSeverity
    penalty: float  # 0.0 = no effect, 1.0 = full block
    reason: str
    context: Dict = field(default_factory=dict)
```

### Comparación con `entry_gate.py`
```python
@dataclass
class GateResult:
    name: str
    gate_type: GateType          # ≈ category
    category: GateCategory       # ≈ FilterCategory
    passed: bool                 # ≈ (penalty == 0)
    size_multiplier: float       # ≈ (1.0 - penalty)
    reason: str
    details: Dict[str, Any]      # ≈ context
```

### Impacto
- Dos sistemas de validación paralelos que hacen lo mismo
- `IntelligentFilter` y `EntryGateEvaluator` resuelven el mismo problema

### Solución Recomendada
Unificar en una sola abstracción de "evaluación de condiciones":

```python
# src/domain/trading/shared/validation_result.py
@dataclass
class ValidationResult:
    """Unified result for all validation checks."""
    name: str
    category: str
    passed: bool
    size_multiplier: float  # 0.0-1.0
    severity: str  # GREEN, YELLOW, ORANGE, RED
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def penalty(self) -> float:
        return 1.0 - self.size_multiplier
```

---

## 3. 🟠 Cálculo Repetido de Indicadores Técnicos

### Problema
Los indicadores `calculate_atr`, `calculate_rsi`, `calculate_ema`, `calculate_adx` se llaman repetidamente en múltiples módulos con los mismos datos.

### Ubicaciones Encontradas

| Archivo | Indicadores | Líneas |
|---------|-------------|--------|
| `filter_checks.py` | ATR, RSI, EMA, ADX | 19, 46-47, 90, 98, 102, 155-156, 337 |
| `pullback_short.py` | EMA, ATR, RSI | 272-273, 282 |
| `anticipation.py` | ADX | 157, 163 |
| `momentum_exhaustion.py` | RSI | 149 (import) |

### Ejemplo de Duplicación

**En `filter_checks.py`:**
```python
def check_momentum_filter(df: pd.DataFrame, bias: str):
    closes = df['close'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    
    rsi = calculate_rsi(closes, 14)
    adx_values, _ = calculate_adx(highs, lows, closes, 14)
    atr = calculate_atr(highs, lows, closes, 14)
    # ...
```

**En `pullback_short.py`:**
```python
def _check_extension_gate(self, df_5m, df_1h, current_price):
    closes_1h = df_1h['close'].to_numpy()
    highs_1h = df_1h['high'].to_numpy()
    lows_1h = df_1h['low'].to_numpy()
    
    ema50_1h = calculate_ema(closes_1h, 50)
    atr_1h = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
    # ...
```

### Solución Recomendada
Crear un `IndicatorCache` o `MarketSnapshot` que calcule una sola vez:

```python
# src/domain/trading/shared/indicator_cache.py
@dataclass
class IndicatorSnapshot:
    """Pre-calculated indicators for a single evaluation cycle."""
    timestamp: datetime
    
    # 1m timeframe
    rsi_1m: float
    atr_1m: float
    
    # 5m timeframe
    rsi_5m: float
    atr_5m: float
    adx_5m: float
    ema50_5m: float
    
    # 1h timeframe
    ema50_1h: float
    ema200_1h: float
    atr_1h: float
    adx_1h: float
    
    @classmethod
    def from_dataframes(cls, df_1m, df_5m, df_1h, timestamp):
        """Calculate all indicators once."""
        return cls(
            timestamp=timestamp,
            rsi_1m=calculate_rsi(df_1m['close'].to_numpy(), 14)[-1],
            atr_1m=calculate_atr(...)[-1],
            # ... etc
        )
```

---

## 4. 🟠 Patrón de Validación Pre-Trade Repetido

### Problema
Tres módulos implementan el mismo patrón de "validación pre-trade":

### `edge_enforcement.py` - `validate_pre_trade()` (líneas 130-196)
```python
def validate_pre_trade(self, highs, lows, closes, volumes, atr_current, ...):
    block_reasons = []
    
    # 1. Calculate Activity Regime and Edge Score
    metrics, edge_score = self.activity_analyzer.analyze(...)
    
    # 2. Get context validation from EdgeValidator
    context = self.edge_validator.validate_context(...)
    
    # 3. Get degradation status
    degradation = self.degradation_monitor.get_summary_report()
    
    # 4. Evaluate state transitions
    self.state_machine.evaluate_state_transitions(...)
    
    # 5. Check all operating conditions
    if edge_score.value < min_score:
        block_reasons.append(...)
    
    return can_trade, block_reasons, edge_score
```

### `entry_gate.py` - `evaluate()` (líneas 170-250)
```python
def evaluate(self, spread, volatility_shock, data_age_seconds, ...):
    results: List[GateResult] = []
    
    # HARD GATES
    if spread > spread_extreme:
        results.append(GateResult(..., passed=False))
    
    if volatility_shock:
        results.append(GateResult(..., passed=False))
    
    # SOFT GATES
    score_result = self._evaluate_score_gate(score, ...)
    results.append(score_result)
    
    # Compute final decision
    hard_gates_passed = all(r.passed for r in results if r.gate_type == GateType.HARD)
    
    return EntryDecision(can_trade=hard_gates_passed, ...)
```

### `intelligent_filter.py` - `evaluate()` (líneas 140-300)
```python
def evaluate(self, spread_bps, feed_age_sec, ...):
    self._filter_results = []
    
    # LAYER 1: HARD BLOCKS
    if not exchange_connected:
        self._add_result(FilterResult(severity=FilterSeverity.RED, ...))
    
    if feed_age_sec > 60:
        self._add_result(FilterResult(severity=FilterSeverity.RED, ...))
    
    # LAYER 2: SOFT PENALTIES
    if spread_ratio > 3:
        self._add_result(FilterResult(severity=FilterSeverity.YELLOW, ...))
    
    # Compute final decision
    return self._compute_decision()
```

### Impacto
- 3 sistemas paralelos de validación con lógica similar
- Inconsistencia en nombres: "gates" vs "filters" vs "blockers"
- Difícil mantener sincronizadas las reglas

### Solución Recomendada
Crear una abstracción común `ValidationPipeline`:

```python
# src/domain/trading/shared/validation_pipeline.py
class ValidationPipeline:
    """Unified validation pipeline for pre-trade checks."""
    
    def __init__(self):
        self.validators: List[Validator] = []
    
    def add_validator(self, validator: Validator, is_hard: bool = False):
        self.validators.append((validator, is_hard))
    
    def evaluate(self, context: MarketContext) -> ValidationDecision:
        results = []
        for validator, is_hard in self.validators:
            result = validator.check(context)
            result.is_hard = is_hard
            results.append(result)
        
        return self._compute_decision(results)
```

---

## 5. 🟡 Lógica de Size Multiplier Dispersa

### Problema
El cálculo de `size_multiplier` aparece en múltiples lugares con lógica similar:

### `state_types.py` (líneas 96-115)
```python
STATE_SIZE_MULTIPLIERS = {
    StrategyState.ACTIVE: 1.0,
    StrategyState.DEGRADED: 0.5,
    StrategyState.OBSERVATION: 0.5,
    StrategyState.DEAD: 0.0,
}

def get_state_size_multiplier(state: StrategyState) -> float:
    return STATE_SIZE_MULTIPLIERS.get(state, 1.0)
```

### `edge_enforcement.py` (líneas 411-450)
```python
def get_combined_size_multiplier(self, edge_score: Optional[EdgeScore] = None) -> float:
    state_mult = get_state_size_multiplier(self.state_machine.current_state)
    
    if state_mult == 0.0:
        return 0.0
    
    if edge_score:
        edge_mult = edge_score.get_size_multiplier(self.thresholds['min_edge_score'])
    else:
        edge_mult = 1.0
    
    return min(state_mult, edge_mult)  # Conservative approach
```

### `entry_gate.py` (líneas 340-370)
```python
def _compute_final_size(self, results: List[GateResult]) -> float:
    # Start with 1.0
    size = 1.0
    
    # Apply soft gate multipliers
    for r in results:
        if r.passed and r.size_multiplier < 1.0:
            size = min(size, r.size_multiplier)
    
    return size
```

### Solución Recomendada
Consolidar en un único `SizingCalculator`:

```python
# src/domain/trading/shared/sizing.py
class SizingCalculator:
    """Single source of truth for position sizing multipliers."""
    
    def calculate(
        self,
        strategy_state: StrategyState,
        edge_score: Optional[float] = None,
        gate_results: Optional[List[ValidationResult]] = None,
        regime: Optional[str] = None
    ) -> float:
        # 1. State-based (highest priority)
        state_mult = STATE_SIZE_MULTIPLIERS.get(strategy_state, 1.0)
        if state_mult == 0.0:
            return 0.0
        
        # 2. Edge-based
        edge_mult = self._edge_to_multiplier(edge_score)
        
        # 3. Gate-based (soft reductions)
        gate_mult = self._gates_to_multiplier(gate_results)
        
        # Conservative: take minimum
        return min(state_mult, edge_mult, gate_mult)
```

---

## 6. 🟠 Cálculo de Expectancy Duplicado

### Problema
El cálculo de expectancy aparece en 2+ lugares:

### `edge_validator.py` (líneas 350-385)
```python
def calculate_expectancy(self, window_size: int = 50) -> float:
    if len(self.trade_history) < window_size:
        return 0.0
    
    recent_trades = self.trade_history[-window_size:]
    
    def get_pnl(t: DegradationTradeEntry) -> float:
        return t.exit_price - t.entry_price
    
    wins = [t for t in recent_trades if get_pnl(t) > 0]
    losses = [t for t in recent_trades if get_pnl(t) <= 0]
    
    win_rate = len(wins) / len(recent_trades)
    avg_win = sum(get_pnl(t) for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(get_pnl(t) for t in losses) / len(losses)) if losses else 0.0
    
    return (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
```

### `shared/utils.py` (también existe)
```python
def calculate_expectancy(wins: List[float], losses: List[float]) -> float:
    # Similar implementation
    pass
```

### Solución Recomendada
Usar una única función canónica:

```python
# src/shared/metrics.py
def calculate_expectancy(
    trades: List[Trade],
    window_size: int = 50
) -> ExpectancyResult:
    """
    Canonical expectancy calculation.
    
    Returns:
        ExpectancyResult with value, win_rate, avg_win, avg_loss
    """
    if len(trades) < window_size:
        return ExpectancyResult(value=0.0, sufficient_data=False)
    
    recent = trades[-window_size:]
    wins = [t for t in recent if t.pnl > 0]
    losses = [t for t in recent if t.pnl <= 0]
    
    win_rate = len(wins) / len(recent)
    avg_win = mean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = abs(mean([t.pnl for t in losses])) if losses else 0.0
    
    value = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    
    return ExpectancyResult(
        value=value,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        sufficient_data=True
    )
```

---

## 7. 🟠 Detección de Patrones de Velas Repetida

### Problema
La detección de patrones (engulfing, doji, pin bar, etc.) aparece en múltiples lugares:

### `filter_checks.py` - `check_anti_reversal()` (líneas 170-200)
```python
def check_anti_reversal(df: pd.DataFrame, bias: str) -> Tuple[bool, str]:
    # Engulfing patterns
    bearish_engulf = (close2 < open2 and close2 < open1 and open2 > close1)
    bullish_engulf = (close2 > open2 and close2 > open1 and open2 < close1)
    
    # Rejection wicks
    upper_wick = high2 - max(open2, close2)
    long_reject = upper_wick > candle_range * 0.6
```

### `momentum_exhaustion.py` - `_detect_exhaustion_candles()` (líneas 190-240)
```python
def _detect_exhaustion_candles(self, df: pd.DataFrame, direction: str):
    body = abs(last['close'] - last['open'])
    range_size = last['high'] - last['low']
    body_ratio = body / range_size
    
    # 1. Doji (tiny body)
    if body_ratio < 0.1:
        return True, "Doji candle at extreme"
    
    # 2. Pin bar
    if direction == 'LONG':
        upper_wick = last['high'] - max(last['open'], last['close'])
        if upper_wick > body * 2.5:
            return True, "Shooting star"
    
    # 3. Engulfing
    if body > prev_body * 1.5:
        if direction == 'LONG' and last['close'] < last['open']:
            return True, "Bearish engulfing"
```

### Solución Recomendada
Crear un módulo centralizado de patrones:

```python
# src/shared/candle_patterns.py
@dataclass
class CandlePattern:
    name: str
    is_bullish: bool
    confidence: float
    description: str

def detect_patterns(df: pd.DataFrame, lookback: int = 3) -> List[CandlePattern]:
    """Detect all candle patterns in recent data."""
    patterns = []
    
    # Doji
    if is_doji(df.iloc[-1]):
        patterns.append(CandlePattern("DOJI", None, 0.7, "Indecision"))
    
    # Engulfing
    if engulf := detect_engulfing(df.iloc[-2:]):
        patterns.append(engulf)
    
    # Pin bars
    if pin := detect_pin_bar(df.iloc[-1]):
        patterns.append(pin)
    
    return patterns

def is_doji(candle: pd.Series, threshold: float = 0.1) -> bool:
    body = abs(candle['close'] - candle['open'])
    range_size = candle['high'] - candle['low']
    return (body / range_size) < threshold if range_size > 0 else False
```

---

## 8. Máquinas de Estado Paralelas

### Problema Menor
Existen dos máquinas de estado que gestionan conceptos relacionados pero separados:

| Clase | Archivo | Propósito |
|-------|---------|-----------|
| `StateMachine` | `state/state_machine.py` | Estado operacional (IDLE, MANAGE, etc.) |
| `StrategyStateMachine` | `strategy_state_machine.py` | Estado de estrategia (ACTIVE, DEGRADED, etc.) |

### Observación
Esto NO es necesariamente duplicación - son responsabilidades distintas:
- `StateMachine`: Ciclo de vida de órdenes/posiciones
- `StrategyStateMachine`: Permisos de trading basados en métricas

Sin embargo, ambas tienen métodos similares:
- `transition_to_*`
- `can_trade()`
- `record_trade_*()`

### Posible Mejora
Extraer patrón común de máquina de estados:

```python
# src/shared/state_machine_base.py
class StateMachineBase(ABC):
    """Base class for all state machines."""
    
    @abstractmethod
    def transition_to(self, new_state: Enum, reason: str): ...
    
    @abstractmethod
    def can_trade(self) -> Tuple[bool, str]: ...
    
    def record_transition(self, from_state, to_state, reason):
        """Common transition logging."""
        pass
```

---

## Plan de Refactorización Recomendado

### Fase 1: Quick Wins (1-2 días)
1. ✅ Unificar `GateResult` en un solo módulo
2. ✅ Crear `candle_patterns.py` centralizado
3. ✅ Consolidar `calculate_expectancy`

### Fase 2: Indicadores (2-3 días)
1. Crear `IndicatorSnapshot` o cache
2. Refactorizar llamadas a indicadores para usar cache

### Fase 3: Validación Unificada (3-5 días)
1. Diseñar `ValidationPipeline` abstracta
2. Migrar `EntryGateEvaluator` → `ValidationPipeline`
3. Migrar `IntelligentFilter` → `ValidationPipeline`
4. Deprecar código legacy

### Fase 4: Sizing (1-2 días)
1. Crear `SizingCalculator` único
2. Migrar todos los cálculos de `size_multiplier`

---

## Métricas de Impacto Esperadas

| Métrica | Antes | Después (Est.) |
|---------|-------|----------------|
| Líneas de código (LOC) | ~5,500 | ~4,200 (-24%) |
| Clases duplicadas | 7 | 0 |
| Cobertura de tests potencial | ~30% | ~60% |
| Tiempo de onboarding | Alto | Medio |

---

## Archivos a Crear (Nuevos)

```
src/domain/trading/shared/
├── __init__.py
├── gate_types.py         # Unified GateResult
├── validation_result.py  # Unified ValidationResult
├── validation_pipeline.py
├── sizing.py             # SizingCalculator
├── indicator_cache.py    # IndicatorSnapshot
└── candle_patterns.py    # Pattern detection
```

---

*Documento generado automáticamente por análisis de código estático.*
