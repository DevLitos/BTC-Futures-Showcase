# 🔍 AUDITORÍA PROFUNDA: Filtros Duplicados y Lógica Redundante

**Fecha:** 4 de Febrero de 2026  
**Sistema:** BTC Perpetual Trading System  
**Autor:** Auditoría Automatizada  

---

## 📊 RESUMEN EJECUTIVO

| Categoría | Duplicaciones Encontradas | Severidad |
|-----------|---------------------------|-----------|
| Direction Checks | 5 sistemas paralelos | 🔴 CRÍTICA |
| Cooldown Systems | 6+ tipos diferentes | 🔴 CRÍTICA |
| Session/Hora Checks | 4 lugares distintos | 🟡 MEDIA |
| ADX/Volatility Checks | 7+ cálculos redundantes | 🟡 MEDIA |
| Score Systems | 4 sistemas paralelos | 🔴 CRÍTICA |
| Location/Extreme Checks | 3 implementaciones | 🟡 MEDIA |

---

## 1️⃣ DIRECTION CHECKS - 5 SISTEMAS PARALELOS

### DUPLICACIÓN #1.1: structural_bias vs active_direction vs forbidden_direction

**ARCHIVO 1:** `src/application/live/trading_app.py` (líneas 695-720)
```python
self._structural_bias: str | None = None  # HTF bias (doesn't block trades)
self._active_direction: str | None = None  # Current momentum direction
self._forbidden_direction: str | None = None  # Direction blocked by invalidation
```

**ARCHIVO 2:** `src/domain/trading/direction_orchestrator.py` (líneas 45-60)
```python
class SignalPriority(Enum):
    EXPANSION_MODE = 100      # Highest
    EDGE_BREAKOUT = 95
    DISPLACEMENT = 90
    STRUCTURAL_BIAS = 60      # HTF EMA-based
```

**QUÉ HACEN:**
- `structural_bias`: Deriva dirección de HTF (1h, daily) basado en EMA
- `active_direction`: Dirección actual basada en momentum/displacement
- `forbidden_direction`: Bloquea dirección por invalidación

**POR QUÉ ES REDUNDANTE:**
- El `DirectionOrchestrator` (archivo 2) ya consolida TODAS las señales de dirección
- Pero `trading_app.py` sigue manteniendo 3 variables separadas
- Ambos sistemas procesan la misma información pero en paralelo
- El orquestrador tiene prioridades claras (100→50) pero las variables legacy siguen existiendo

---

### DUPLICACIÓN #1.2: direction_lock vs expansion_direction

**ARCHIVO 1:** `src/application/live/trading_app.py` (líneas 758-765)
```python
self._direction_lock: str | None = None  # LONG/SHORT/None
self._direction_lock_reason: str | None = None  # "DISPLACEMENT"
self._direction_lock_expires_at: datetime | None = None
```

**ARCHIVO 2:** `src/application/live/trading_app.py` (líneas 721-727)
```python
self._expansion_mode: bool = False
self._expansion_direction: str | None = None
self._expansion_start_time: datetime | None = None
```

**QUÉ HACEN:**
- Ambos bloquean trades en dirección contraria después de un displacement fuerte
- `direction_lock` tiene timeout y motivo
- `expansion_mode` tiene dirección y tiempo de inicio

**POR QUÉ ES REDUNDANTE:**
- MISMO PROPÓSITO: bloquear dirección opuesta post-displacement
- Se actualizan juntos, se limpian juntos
- Solo necesitas UNO de los dos sistemas

---

### DUPLICACIÓN #1.3: DirectionOrchestrator vs checks manuales en GuardOrchestrator

**ARCHIVO 1:** `src/domain/trading/direction_orchestrator.py` (líneas 100-200)
- Orquesta señales con prioridades claras
- Detecta conflictos automáticamente
- Retorna `OrchestrationResult` unificado

**ARCHIVO 2:** `src/application/live/entry_orchestrator.py` (líneas 430-500)
```python
# 🚨 GUARD 13: COUNTER-MOMENTUM BLOCK
direction_lock = getattr(engine, '_direction_lock', None)
if direction_lock and direction_lock != ctx.bias:
    # BLOCK
    
forbidden_dir = getattr(engine, '_forbidden_direction', None)
if forbidden_dir == ctx.bias:
    # BLOCK
```

**POR QUÉ ES REDUNDANTE:**
- `DirectionOrchestrator` YA maneja forbidden_direction como veto
- `GuardOrchestrator` REPITE los mismos checks manualmente
- Si el orquestrador funcionara bien, los guards serían innecesarios

---

## 2️⃣ COOLDOWN SYSTEMS - 6 TIPOS DIFERENTES

### Lista de Cooldowns Encontrados:

| Cooldown | Archivo | Línea | Propósito |
|----------|---------|-------|-----------|
| `_cooldown_until` | trading_app.py | 592 | Loss streak cooldown |
| `_rejection_cooldown_until` | trading_app.py | 609 | Filter rejection cooldown |
| `_short_cooldown_until` | trading_app.py | 882 | Short obstinacy cooldown |
| `cooldown_until` (BlackSwan) | filters.py | 195 | Black swan event cooldown |
| `context_cooldown_until` (Edge) | edge_state_manager.py | 557 | Edge context cooldown |
| `LossStreakCooldown` | simplified_sizing.py | 230 | Time-based loss cooldown |

### DUPLICACIÓN #2.1: Loss Cooldown en 2 lugares

**ARCHIVO 1:** `src/application/live/trading_app.py` (líneas 579-604)
```python
# LOSS STREAK COOLDOWN
self._consecutive_losses = 0
self._cooldown_until = None
self._pause_after_3 = 15  # minutes
self._pause_after_4 = 60
self._pause_after_5 = 180
```

**ARCHIVO 2:** `src/config/simplified_sizing.py` (líneas 230-275)
```python
class LossStreakCooldown:
    @classmethod
    def get_cooldown_minutes(cls, consecutive_losses: int) -> int:
        if consecutive_losses >= 5:
            return RiskManagement.LOSS_STREAK_5_COOLDOWN_MIN
        if consecutive_losses >= 4:
            return RiskManagement.LOSS_STREAK_4_COOLDOWN_MIN
        if consecutive_losses >= 3:
            return RiskManagement.LOSS_STREAK_3_COOLDOWN_MIN
```

**POR QUÉ ES REDUNDANTE:**
- EXACTAMENTE la misma lógica implementada en dos lugares
- `trading_app.py` tiene sus propias constantes `_pause_after_X`
- `simplified_sizing.py` lee de `RiskManagement.LOSS_STREAK_X_COOLDOWN_MIN`
- Pueden tener valores diferentes causando comportamiento inconsistente

---

### DUPLICACIÓN #2.2: Rejection Cooldown vs Setup Discard Cooldown

**ARCHIVO 1:** `src/application/live/trading_app.py` (líneas 606-612)
```python
self._rejection_cooldown_until: datetime | None = None
self._rejection_cooldown_candles = 2
```

**ARCHIVO 2:** `src/application/live/trading_app.py` (líneas 524-525)
```python
self.last_setup_discard_time: datetime | None = None
self.setup_discard_cooldown = 10  # segundos
```

**QUÉ HACEN:**
- `rejection_cooldown`: Espera N velas después de filter block
- `setup_discard_cooldown`: Espera N segundos después de descartar setup

**POR QUÉ ES REDUNDANTE:**
- Ambos evitan reevaluación rápida tras rechazo
- Usan unidades diferentes (candles vs segundos)
- Podrían unificarse en un solo sistema de cooldown post-rejection

---

## 3️⃣ SESSION/HORA CHECKS - 4 LUGARES DISTINTOS

### DUPLICACIÓN #3.1: Session Quality Multipliers

**ARCHIVO 1:** `src/config/simplified_sizing.py` (líneas 435-455)
```python
class SessionMultiplier:
    GOOD = 1.0      # LONDON, NY_OVERLAP, NY
    MODERATE = 0.8  # ASIA
    # POOR = HARD BLOCK
```

**ARCHIVO 2:** `src/config/trading_config.py` (líneas 2004-2033)
```python
class SessionQuality:
    SESSION_SIZE_MULT = {
        'LONDON': 1.0,
        'NY_OVERLAP': 1.0,
        'NY': 1.0,
        'ASIA': 0.85,
        # ...
    }
```

**ARCHIVO 3:** `src/application/live/entry_orchestrator.py` (líneas 1218-1220)
```python
session_quality = engine._current_trade_tags.get('session_quality', 'moderate')
session_mult = {'good': 1.0, 'moderate': 0.85, 'poor': 0.7}.get(session_quality, 0.85)
```

**ARCHIVO 4:** `src/domain/trading/edge_state_manager.py` (líneas 3417, 3502)
```python
if session_quality == 'poor':
    # block in INTRADAY mode
    
if session_quality == 'good':
    # boost in INTRADAY mode
```

**POR QUÉ ES REDUNDANTE:**
- 4 definiciones de multiplicadores de sesión con valores DIFERENTES:
  - simplified_sizing: `MODERATE = 0.8`
  - trading_config: `ASIA = 0.85`
  - entry_orchestrator: `'moderate': 0.85`
  - edge_state_manager: usa 'good'/'poor' para lógica binaria
- Cada archivo tiene su propia interpretación de session quality

---

### DUPLICACIÓN #3.2: Toxic Hours/Combos

**ARCHIVO 1:** `src/config/simplified_sizing.py` (líneas 280-310)
```python
class ToxicHourMultiplier:
    @classmethod
    def get_multiplier(cls, hour_utc: int) -> float:
        if hour_utc in RiskManagement.TOXIC_HOURS_SET:
            return RiskManagement.TOXIC_HOURS_MULT
```

**ARCHIVO 2:** `src/config/simplified_sizing.py` (líneas 315-385)
```python
class ToxicCombinationBlock:
    @classmethod
    def is_blocked(cls, session_name: str, direction: str) -> Tuple[bool, str]:
        if combo in RiskManagement.TOXIC_COMBOS_SET:
            # BLOCK or REDUCE
```

**POR QUÉ SE SUPERPONE:**
- `ToxicHourMultiplier` bloquea por HORA específica (ej: 0, 4, 21, 23 UTC)
- `ToxicCombinationBlock` bloquea por SESSION+DIRECTION (ej: ASIA+SHORT)
- PERO las horas 0-7 UTC SON la sesión ASIA
- Un trade en hora 4 UTC + SHORT sería evaluado por AMBOS sistemas
- Resultado: doble penalización o bloqueo inconsistente

---

## 4️⃣ ADX/VOLATILITY CHECKS - 7+ CÁLCULOS REDUNDANTES

### DUPLICACIÓN #4.1: ADX calculado múltiples veces

**ARCHIVO 1:** `src/application/live/trading_app.py` (línea 3560)
```python
adx_5m_vals, _ = calculate_adx(df_5m['high'], df_5m['low'], df_5m['close'], 14)
```

**ARCHIVO 2:** `src/application/live/entry_orchestrator.py` (líneas 190-200)
```python
adx_1h, _ = calculate_adx(ctx.df_1h['high']..., 14)
adx_5m, _ = calculate_adx(ctx.df_5m['high']..., 14)
```

**ARCHIVO 3:** `src/shared/indicator_helpers.py` (líneas 290-310)
```python
cache.adx_5m = calculate_adx_value(cache.df_5m)
cache.adx_1h = calculate_adx_value(cache.df_1h)
```

**POR QUÉ ES REDUNDANTE:**
- El mismo ADX se calcula 3+ veces por tick
- Existe `IndicatorCache` (archivo 3) PERO no se usa consistentemente
- `trading_app.py` y `entry_orchestrator.py` calculan independientemente

---

### DUPLICACIÓN #4.2: ADX Thresholds dispersos

| Ubicación | Threshold | Propósito |
|-----------|-----------|-----------|
| trading_config.py L97 | ADX < 18 | RANGE_DAY |
| trading_config.py L98 | ADX > 20 | TREND_DAY |
| edge_state_manager.py L452 | ADX_5M >= 25 | INTRADAY edge |
| entry_orchestrator.py L195 | HTF > 20, LTF < 10 | ADX divergence block |
| BiasStrengthConfig L236 | ADX > 25 | Strong trend |
| BiasStrengthConfig L237 | ADX < 18 | Weak/ranging |

**POR QUÉ ES REDUNDANTE:**
- 6 lugares diferentes definen qué es "ADX alto" o "ADX bajo"
- Valores inconsistentes: 18, 20, 25...
- Cada módulo tiene su propia interpretación

---

## 5️⃣ SCORE SYSTEMS - 4 SISTEMAS PARALELOS

### DUPLICACIÓN #5.1: Entry Score vs Regime Score vs Confidence

**ARCHIVO 1:** `src/domain/trading/scoring/entry_scorer.py` (líneas 1-50)
```python
# 4 blocks × 0-2 points = max 8. Threshold: 5
# STRUCTURE (0-2), TIMING (0-2), MOMENTUM (0-2), CONTEXT (0-2), SYNERGY (0-1)
# Total possible: 9 points
```

**ARCHIVO 2:** `src/config/simplified_sizing.py` (líneas 85-100)
```python
regime_mult: float = 1.0
# TREND = 1.0, RANGE = 0.7
```

**ARCHIVO 3:** `src/domain/trading/direction_orchestrator.py` (línea 64)
```python
confidence: float = 1.0  # 0.0 to 1.0
```

**ARCHIVO 4:** `src/domain/trading/edge_state_manager.py` (líneas 120-170)
```python
class ProvisionalEdgeConfig:
    MIN_CRITERIA_MET = 2  # Mínimo 2 de 4 criterios
    TOTAL_CRITERIA = 4
```

**QUÉ HACEN:**
- `EntryScorer`: Score 0-9 para calidad de entrada
- `regime_mult`: Multiplicador 0.7-1.0 por régimen
- `confidence`: Confianza 0-1 del orquestrador de dirección
- `ProvisionalEdgeConfig`: Sus propios criterios 2/4

**POR QUÉ ES REDUNDANTE:**
- CUATRO sistemas de scoring paralelos que miden "calidad de trade"
- Cada uno con su propia escala y thresholds
- No hay una fuente única de verdad para "¿este trade es bueno?"

---

### DUPLICACIÓN #5.2: Score Adjustments en múltiples lugares

**ARCHIVO 1:** `src/domain/trading/scoring/entry_scorer.py`
- `micro_trend_adj`
- `filter_pipeline_adj`
- `late_confirmation_penalty`

**ARCHIVO 2:** `src/application/live/entry_orchestrator.py` (líneas 300-330)
```python
# SOFT MODE: Apply size cap and score penalty
SOFT_MODE_SCORE_PENALTY = -1.5
```

**ARCHIVO 3:** `src/config/trading_config.py` (líneas 46-60)
```python
'score_adjust': 0,      # TREND_DAY
'score_adjust': +1,     # RANGE_DAY
'score_adjust': +0.5,   # TRANSITION_DAY
```

**POR QUÉ ES REDUNDANTE:**
- Penalties y adjustments definidos en 3+ lugares
- Cada módulo modifica el score final independientemente
- Difícil saber el score REAL después de todos los ajustes

---

## 6️⃣ LOCATION/EXTREME CHECKS - 3 IMPLEMENTACIONES

### DUPLICACIÓN #6.1: Extreme Percentile Checks

**ARCHIVO 1:** `src/domain/market/filters/location_filter.py` (líneas 25-30)
```python
EXTREME_HIGH_PERCENTILE: float = 90.0   # Top 10%
EXTREME_LOW_PERCENTILE: float = 10.0    # Bottom 10%
```

**ARCHIVO 2:** `src/application/live/trading_app.py` (líneas 3946-3958)
```python
BASE_EXTREME_HIGH_PCT = 85
BASE_EXTREME_LOW_PCT = 15
# Con ajustes adaptativos:
EXTREME_HIGH_PCT = BASE_EXTREME_HIGH_PCT + vol_extreme_adj  # 80-90
EXTREME_LOW_PCT = BASE_EXTREME_LOW_PCT - vol_extreme_adj  # 10-20
```

**ARCHIVO 3:** `src/application/live/entry_orchestrator.py` (líneas 400-420)
```python
pct_60d = getattr(location_ctx, 'percentile_60d', 50)
in_extreme_low = pct_60d <= 10
in_extreme_high = pct_60d >= 90
```

**POR QUÉ ES REDUNDANTE:**
- 3 definiciones de "extremo" con valores DIFERENTES:
  - location_filter: 90/10 fijo
  - trading_app: 85/15 con ajuste adaptativo (80-95 / 5-20)
  - entry_orchestrator: 90/10 hardcodeado
- Un precio en percentil 88% es:
  - ❌ No extremo en trading_app (< 85 adaptativo)
  - ✅ Extremo en entry_orchestrator (hardcoded 90 check falla)
  - Comportamiento INCONSISTENTE

---

### DUPLICACIÓN #6.2: Location Veto vs Guard Block

**ARCHIVO 1:** `src/domain/market/filters/location_filter.py`
- `veto_long: bool`, `veto_short: bool`
- Calcula headroom, percentiles, breakout status

**ARCHIVO 2:** `src/application/live/entry_orchestrator.py` (líneas 400-440)
```python
# 🚨 GUARD 12: LOCATION EXTREME BLOCK
if ctx.bias == 'SHORT' and in_extreme_low:
    result.passed = False
    
if ctx.bias == 'LONG' and in_extreme_high:
    result.passed = False
```

**POR QUÉ ES REDUNDANTE:**
- `LocationFilter` YA calcula `veto_long` y `veto_short`
- `GuardOrchestrator` REPITE la misma lógica con sus propios thresholds
- Si LocationFilter dijera "veto_short=True", el guard es redundante

---

## 7️⃣ BLOQUEOS QUE SE SUPERPONEN

### SUPERPOSICIÓN #7.1: TOXIC_COMBO vs SESSION filters vs HOUR filters

```
Hora 4 UTC (ASIA session):
├── ToxicHourMultiplier: hora 4 está en TOXIC_HOURS_SET → mult 0.5
├── ToxicCombinationBlock: (ASIA, SHORT) en TOXIC_COMBOS_SET → BLOCK o mult 0.5
├── SessionMultiplier: ASIA = 0.8 (o 0.85 según archivo)
└── IntradayEdgeCriteria: SESSION_POOR_SIZE_MULT = 0.7

Resultado: Un SHORT en hora 4 UTC puede recibir:
- 0.5 × 0.5 × 0.8 × 0.7 = 0.14x size (14% del normal!)
O estar bloqueado completamente si TOXIC_COMBOS_MODE='BLOCK'
```

---

### SUPERPOSICIÓN #7.2: CONSENSUS vs DIRECTION checks

**Lugares que verifican dirección:**
1. `DirectionOrchestrator.orchestrate()` - priority-based
2. `GuardOrchestrator.run_all_guards()` - direction_lock check
3. `GuardOrchestrator.run_all_guards()` - forbidden_direction check
4. `GuardOrchestrator.run_all_guards()` - expansion_mode opposite check
5. `GuardOrchestrator.run_all_guards()` - SHORT OBSTINACY check
6. `_check_bias_invalidation()` en trading_app.py

**POR QUÉ SE SUPERPONEN:**
- El mismo trade pasa por 6 checks de dirección
- Cada uno puede bloquear por razones ligeramente diferentes
- Genera 6 posibles puntos de rechazo para la misma cosa

---

### SUPERPOSICIÓN #7.3: COOLDOWN vs REJECTION_COOLDOWN vs LOSS_COOLDOWN

```
Después de un trade perdedor:
├── _cooldown_until: activado si consecutive_losses >= 3
├── _rejection_cooldown_until: activado por _set_rejection_cooldown()
├── LossStreakCooldown: calculado en simplified_sizing
├── context_cooldown_until: en edge_state_manager
└── COUNTERTREND_COOLDOWN_AFTER_LOSS: 2 candles en BiasStrengthConfig

Problema: Un trade puede estar en 5 cooldowns DIFERENTES simultáneamente
```

---

## 📋 RESUMEN DE ACCIONES RECOMENDADAS

### CRÍTICAS (Implementar primero):

1. **Unificar Direction System**
   - ELIMINAR: `structural_bias`, `active_direction`, `forbidden_direction` como variables separadas
   - USAR: Solo `DirectionOrchestrator` como fuente única

2. **Unificar Cooldown System**
   - CREAR: Una sola clase `CooldownManager` que maneje todos los tipos
   - ELIMINAR: 6 implementaciones dispersas

3. **Unificar Score System**
   - CREAR: Un solo pipeline de scoring que calcule score final
   - ELIMINAR: Adjustments dispersos en 3+ archivos

### MEDIAS (Segunda prioridad):

4. **Centralizar Session Quality**
   - DEFINIR: UN solo mapa de session → quality → multiplier
   - USAR: En todos los módulos

5. **Centralizar ADX Calculations**
   - USAR: `IndicatorCache` consistentemente
   - DEFINIR: UN solo lugar para ADX thresholds

6. **Unificar Location Extreme Checks**
   - USAR: Solo `LocationFilter.veto_long/veto_short`
   - ELIMINAR: Checks redundantes en GuardOrchestrator

---

## 📊 IMPACTO ESTIMADO

| Área | LOC Duplicado | Riesgo de Bug | Complejidad |
|------|---------------|---------------|-------------|
| Direction | ~500 líneas | ALTO | Muy Alta |
| Cooldowns | ~300 líneas | ALTO | Alta |
| Sessions | ~150 líneas | MEDIO | Media |
| ADX | ~200 líneas | MEDIO | Media |
| Scores | ~250 líneas | ALTO | Alta |
| Location | ~100 líneas | MEDIO | Media |

**TOTAL:** ~1,500 líneas de código redundante que aumentan superficie de bugs

---

## ✅ CONCLUSIÓN

El sistema tiene una arquitectura en capas que se ha vuelto redundante:
- Cada nueva feature añadió checks sin eliminar los anteriores
- Existen 5 sistemas de dirección, 6 de cooldown, 4 de scoring
- Los valores no son consistentes entre módulos

**Recomendación:** Refactorizar hacia arquitectura de "Single Source of Truth" para cada concepto:
- UNA fuente de dirección (DirectionOrchestrator)
- UNA fuente de cooldown (CooldownManager)
- UNA fuente de session quality (SessionConfig)
- UNA fuente de scoring (EntryScorer + pipeline limpio)
