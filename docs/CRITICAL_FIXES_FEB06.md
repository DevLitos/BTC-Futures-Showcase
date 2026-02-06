# 🚨 CRITICAL FIXES - Feb 06, 2026

## RESUMEN EJECUTIVO

**Resultado del Trading Run**: 0 trades en 10+ horas
**Capital**: $257.81 (sin cambio)
**Oportunidades perdidas**: Rally de +$6,000 y caída de -$2,000

---

## PARTE 1: FIXES YA IMPLEMENTADOS (FIX #55)

### FIX #55a: SHORT OBSTINACY con Strong Trend Override

**Archivo**: `src/application/live/trading_app.py`

**Problema**: El sistema detectaba `all_lagging=True` pero seguía bloqueando SHORTs porque `impulse_down=False` (sensible a micro-rebotes en 1m).

**Solución implementada**:
```python
# ANTES:
soft_mode_eligible = all_lagging and price_impulse_down

# DESPUÉS:
STRONG_TREND_ADX_OVERRIDE = 45.0
strong_htf_trend = adx_1h >= STRONG_TREND_ADX_OVERRIDE
soft_mode_eligible = all_lagging and (price_impulse_down or strong_htf_trend)
```

### FIX #55b: SOFT MODE sin Displacement Memory

**Archivo**: `src/application/live/trading_app.py`

**Problema**: SOFT MODE solo se activaba con `displacement_memory_active`. Pero en tendencias fuertes sin displacement reciente, el sistema quedaba bloqueado en loop infinito.

**Solución implementada**:
```python
# NUEVO BLOQUE después del check de displacement_memory:
VERY_STRONG_TREND_OVERRIDE = 50.0
if all_lagging and adx_1h >= VERY_STRONG_TREND_OVERRIDE:
    self._current_trade_tags['obstinacy_soft_mode_eligible'] = True
    self._current_trade_tags['obstinacy_strong_trend_override'] = True
```

### FIX #55c: Entry Orchestrator actualizado

**Archivo**: `src/application/live/entry_orchestrator.py`

**Cambio**:
```python
# ANTES:
if soft_mode_eligible and displacement_memory_active:

# DESPUÉS:
strong_trend_override = engine._current_trade_tags.get('obstinacy_strong_trend_override', False)
soft_mode_active = soft_mode_eligible and (displacement_memory_active or strong_trend_override)
if soft_mode_active:
```

---

## PARTE 2: CRÍTICA AL ANÁLISIS DE "MISSED WINNERS"

### ⚠️ LOS "MISSED" SON MENTIRA

El log muestra "MISSED LONG @ $65,500" pero eso NO es una oportunidad perdida real.

**El problema fundamental**:
- El sistema detecta LONGs **DESPUÉS** de que el precio ya subió
- Detecta SHORTs **DESPUÉS** de que el precio ya bajó
- Esto no es "missed opportunity", es **lookahead bias disfrazado**

### Análisis del Bloque 00:26 - 02:11

| Hora | Precio | "Señal" detectada | Realidad |
|------|--------|-------------------|----------|
| 23:28:43 | $63,636 | LONG detectado | El precio ya subió $1,000 desde $62,600 |
| 00:26:xx | ~$63,700 | LONG bloqueado | Mercado lateral, peor momento para LONG |
| 01:30:xx | ~$63,800 | LONG evaluado | Precio oscilando sin dirección |
| 02:11:xx | ~$64,000 | LONG "missed" | El rally ya ocurrió |

**Patrón observado**:
1. Precio sube $1,000 en 30 minutos
2. Sistema detecta "oportunidad LONG"
3. STRUCTURE GATE bloquea (correctamente - ya es tarde)
4. Log registra "MISSED LONG"
5. **Esto NO es un missed winner - es el sistema detectando DESPUÉS del hecho**

### El verdadero problema

```
El sistema no detecta INICIO de movimientos
Detecta CONFIRMACIÓN de movimientos ya ocurridos
```

**Evidencia del log**:
```
23:25:57 - WAITING EDGE | source=LOCATION_EXTREME_LOW | conf=0% | Need BOS/breakout
23:28:43 - EDGE ARMED: TREND_MOMENTUM LONG | Price: $63,636
```

El precio ya había subido de $62,600 a $63,636 (+$1,000) cuando el sistema "detectó" el LONG. **Eso no es detección, es reacción tardía.**

---

## PARTE 3: EL FALLO FUNDAMENTAL - LOCATION_EXTREME

### ❌ LA REGLA ACTUAL ES LETAL PARA TENDENCIAS

```python
# REGLA ACTUAL (MALA):
if percentile_60d <= 10:
    block_short = True  # "Ya está muy bajo, no shortear"
```

### Por qué esto es mean-reversion bias disfrazado

| Lo que crees | La realidad |
|--------------|-------------|
| "Si está en el 10% más bajo, va a rebotar" | Los crashes grandes OCURREN en extremos |
| "Es zona de soporte" | Crypto no tiene soporte, tiene liquidaciones |
| "Risk management" | Es bias de mean-reversion |

### Qué pasó en tu sesión

1. ✅ Precio entra en percentil 10% ($62,600)
2. ❌ Sistema: "ya está barato, no short"
3. ❌ Sistema espera rebote
4. ✅ Precio sube $4,000 (pero sistema bloqueó LONGs por STRUCTURE GATE)
5. ✅ Precio cae $2,000 (pero sistema bloqueó SHORTs por LOCATION_EXTREME)
6. 📊 Resultado: 0 trades, 0 profit

### La diferencia que no estás viendo

```
EXTREMO POR AGOTAMIENTO ≠ EXTREMO POR PÁNICO
```

#### A) EXTREMO POR AGOTAMIENTO → NO SHORT ✅
- Velas pequeñas
- Volumen cayendo
- Rango decreciente
- RSI divergente
- Sin displacement reciente
- **Probable rebote**

#### B) EXTREMO POR PÁNICO → SÍ SHORT ✅
- Velas grandes
- Volumen expandiendo
- Displacement múltiple
- Range widening
- ADX subiendo
- **Continuación probable**

**Tu sistema trata ambos igual = error fatal**

---

## PARTE 4: CAMBIOS REQUERIDOS

### CAMBIO 1: LOCATION = MODULADOR, NO BLOQUEO

**Archivo**: `src/domain/market/filters/location_filter.py`

```python
# ❌ ACTUAL (MALO):
if percentile <= 10:
    return False, "LOCATION_EXTREME_LOW", context  # HARD BLOCK

# ✅ NUEVO (BUENO):
if percentile <= 10:
    context.in_extreme_low = True
    context.size_modifier = 0.4  # Reduce size, no bloquea
    context.require_confirmation = True
    context.tighten_stop = True
    return True, "LOCATION_CAUTION_LOW", context  # SOFT PASS
```

### CAMBIO 2: DETECTOR DE PÁNICO (CRASH MODE)

**Archivo**: Nuevo `src/domain/market/crash_detector.py`

```python
class CrashDetector:
    """Detecta condiciones de crash para override de location filters."""
    
    PANIC_CONDITIONS = {
        'displacement_count_30m': 2,  # 2+ displacements en 30min
        'adx_5m_min': 28,
        'volume_ratio_min': 1.6,
    }
    
    def is_panic_mode(
        self,
        displacement_count_30m: int,
        adx_5m: float,
        volume_ratio: float
    ) -> bool:
        """
        Detecta modo pánico donde location filters deben ignorarse.
        
        En pánico:
        - Location filters OFF
        - Solo trend/displacement
        - Size cap 0.6
        - Trail agresivo
        """
        return (
            displacement_count_30m >= self.PANIC_CONDITIONS['displacement_count_30m'] and
            adx_5m >= self.PANIC_CONDITIONS['adx_5m_min'] and
            volume_ratio >= self.PANIC_CONDITIONS['volume_ratio_min']
        )
```

### CAMBIO 3: DETECTOR DE ABSORCIÓN (PARA BLOQUEAR SHORTS)

```python
class AbsorptionDetector:
    """Detecta absorción real para bloquear shorts en extremos."""
    
    def has_absorption(
        self,
        df_5m: pd.DataFrame,
        vwap: float,
        current_price: float
    ) -> tuple[bool, str]:
        """
        Solo bloquear shorts si hay señales REALES de giro:
        - Bullish engulfing en 5m
        - VWAP reclaim
        - No new low en 20min
        - Volume climax + rejection
        
        Retorna (has_absorption, reason)
        """
        reasons = []
        
        # 1. Bullish engulfing
        if self._is_bullish_engulfing(df_5m):
            reasons.append("bull_engulf")
        
        # 2. VWAP reclaim
        if current_price > vwap and df_5m.iloc[-2]['close'] < vwap:
            reasons.append("vwap_reclaim")
        
        # 3. No new low en 20min (4 velas de 5m)
        if self._no_new_low(df_5m, bars=4):
            reasons.append("no_new_low")
        
        # 4. Volume climax + rejection
        if self._volume_climax_rejection(df_5m):
            reasons.append("vol_climax")
        
        # Requiere al menos 2 señales
        has_absorption = len(reasons) >= 2
        return has_absorption, ", ".join(reasons) if reasons else "none"
```

### CAMBIO 4: STOPS AJUSTADOS EN EXTREMO

```python
# En risk_engine.py o stop_calculator.py

def calculate_stop_in_extreme(
    self,
    direction: str,
    entry_price: float,
    atr: float,
    df_5m: pd.DataFrame,
    is_extreme_low: bool,
    is_extreme_high: bool
) -> float:
    """
    En zonas extremas, usa stops más tight.
    
    Para SHORTs en extreme_low:
        stop = min(normal_stop, last_5m_high + 0.2*ATR)
    
    Para LONGs en extreme_high:
        stop = max(normal_stop, last_5m_low - 0.2*ATR)
    """
    normal_stop = self._calculate_normal_stop(direction, entry_price, atr)
    
    if direction == 'SHORT' and is_extreme_low:
        last_5m_high = df_5m.iloc[-1]['high']
        tight_stop = last_5m_high + 0.2 * atr
        return min(normal_stop, tight_stop)
    
    if direction == 'LONG' and is_extreme_high:
        last_5m_low = df_5m.iloc[-1]['low']
        tight_stop = last_5m_low - 0.2 * atr
        return max(normal_stop, tight_stop)
    
    return normal_stop
```

### CAMBIO 5: REFACTOR COMPLETO DE LOCATION_EXTREME

**Archivo**: `src/domain/market/filters/location_filter.py`

```python
def check_location_extreme(
    self,
    percentile_60d: float,
    bias: str,
    crash_detector: CrashDetector,
    absorption_detector: AbsorptionDetector,
    context: LocationContext
) -> tuple[bool, str, LocationContext]:
    """
    Nueva lógica de location extreme:
    
    1. Si CRASH MODE → ignorar location, permitir todo
    2. Si ABSORPTION detectada → bloquear contra-tendencia
    3. Si EXTREME sin crash ni absorption → SOFT MODE (size reducido)
    """
    
    # CAPA 1: Check crash mode (override todo)
    if crash_detector.is_panic_mode():
        context.crash_mode = True
        context.location_override = True
        return True, "CRASH_MODE_OVERRIDE", context
    
    # CAPA 2: Check absorption (para bloquear)
    has_absorption, absorption_reason = absorption_detector.has_absorption()
    if has_absorption:
        # Solo bloquear si hay señales REALES de giro
        if bias == 'SHORT' and percentile_60d <= 10:
            context.blocked_by_absorption = True
            return False, f"ABSORPTION_BLOCK: {absorption_reason}", context
        if bias == 'LONG' and percentile_60d >= 90:
            context.blocked_by_absorption = True
            return False, f"ABSORPTION_BLOCK: {absorption_reason}", context
    
    # CAPA 3: Extreme sin crash ni absorption → SOFT MODE
    if percentile_60d <= 10 and bias == 'SHORT':
        context.in_extreme_low = True
        context.size_modifier = 0.4
        context.require_confirmation = True
        context.stop_multiplier = 0.7  # Tighter stop
        return True, "EXTREME_LOW_SOFT", context  # ⚠️ NO BLOQUEAR
    
    if percentile_60d >= 90 and bias == 'LONG':
        context.in_extreme_high = True
        context.size_modifier = 0.4
        context.require_confirmation = True
        context.stop_multiplier = 0.7
        return True, "EXTREME_HIGH_SOFT", context  # ⚠️ NO BLOQUEAR
    
    return True, "LOCATION_OK", context
```

---

## PARTE 5: ESTRUCTURA GATE - EL OTRO PROBLEMA

### El log muestra STRUCTURE GATE bloqueando TODO

```
[BLOCK] STRUCTURE GATE: LONG against HTF (Price vs EMA50_1h) | Price: 63636, EMA50: 71159, Distance: -10.57%
```

### Problema

- Precio está 10.5% debajo de EMA50_1h
- Sistema bloquea TODOS los LONGs
- Pero el precio está en un rally de +$6,000

### Fix requerido

```python
# En entry_scorer.py

def check_structure_gate(
    self,
    price: float,
    ema50_1h: float,
    bias: str,
    is_expansion_mode: bool,
    displacement_recent: bool,
    adx_1h: float
) -> tuple[bool, str]:
    """
    STRUCTURE GATE con overrides para condiciones especiales.
    """
    distance_pct = (price - ema50_1h) / ema50_1h * 100
    
    # Normal gate: -6% para LONGs, +6% para SHORTs
    NORMAL_THRESHOLD = 6.0
    
    # OVERRIDE 1: EXPANSION MODE ignora gate
    if is_expansion_mode:
        return True, "STRUCTURE_GATE_OVERRIDE_EXPANSION"
    
    # OVERRIDE 2: Displacement reciente + ADX fuerte
    if displacement_recent and adx_1h >= 40:
        return True, "STRUCTURE_GATE_OVERRIDE_DISPLACEMENT"
    
    # OVERRIDE 3: Extremo con momentum (no mean-revert)
    # Si está muy lejos de EMA pero ADX > 50, es tendencia fuerte
    if abs(distance_pct) > 8 and adx_1h >= 50:
        return True, "STRUCTURE_GATE_OVERRIDE_STRONG_TREND"
    
    # Gate normal
    if bias == 'LONG' and distance_pct < -NORMAL_THRESHOLD:
        return False, f"STRUCTURE_GATE: LONG too far below EMA ({distance_pct:.1f}%)"
    
    if bias == 'SHORT' and distance_pct > NORMAL_THRESHOLD:
        return False, f"STRUCTURE_GATE: SHORT too far above EMA ({distance_pct:.1f}%)"
    
    return True, "STRUCTURE_GATE_OK"
```

---

## PARTE 6: DETECCIÓN TARDÍA - EL PROBLEMA RAÍZ

### El sistema detecta DESPUÉS, no ANTES

**Flujo actual (MALO)**:
1. Precio en $62,600
2. Sistema: "WAITING EDGE | conf=0% | Need BOS/breakout"
3. Precio sube a $63,600 (+$1,000)
4. Sistema: "EDGE ARMED: LONG"
5. STRUCTURE GATE bloquea
6. Log: "MISSED LONG @ $63,600"

**Esto no es MISSED - es DETECCIÓN TARDÍA**

### Lo que debería pasar (BUENO)

1. Precio en $62,600
2. Sistema detecta: "EXTREME LOW + PANIC + DISPLACEMENT"
3. Sistema: "EDGE ARMED: SHORT (o LONG si hay señal de reversal)"
4. Entrada inmediata con size reducido
5. Si funciona: profit
6. Si no: stop tight, pérdida mínima

### Cambio requerido: ANTICIPACIÓN vs CONFIRMACIÓN

```python
# ACTUAL (confirmación tardía):
if bos_detected:  # Ya pasó el movimiento
    arm_edge()

# NUEVO (anticipación):
if displacement_detected and context_favorable:
    arm_edge()  # ANTES del BOS
    reduce_size()  # Riesgo controlado
```

---

## PARTE 7: RESUMEN DE TODOS LOS CAMBIOS

### Archivos a modificar

| Archivo | Cambio | Prioridad |
|---------|--------|-----------|
| `location_filter.py` | SOFT MODE en extremos, no HARD BLOCK | 🔴 CRÍTICO |
| `entry_scorer.py` | STRUCTURE GATE overrides | 🔴 CRÍTICO |
| `trading_app.py` | Ya implementado FIX #55 | ✅ HECHO |
| `entry_orchestrator.py` | Ya implementado FIX #55 | ✅ HECHO |
| NUEVO: `crash_detector.py` | Detector de pánico | 🟡 ALTO |
| NUEVO: `absorption_detector.py` | Detector de absorción | 🟡 ALTO |
| `risk_engine.py` | Stops tight en extremos | 🟡 ALTO |

### Parámetros clave

```python
# Thresholds para CRASH MODE
CRASH_MODE_CONFIG = {
    'displacement_count_30m': 2,
    'adx_5m_min': 28,
    'volume_ratio_min': 1.6,
}

# Thresholds para ABSORPTION
ABSORPTION_CONFIG = {
    'signals_required': 2,  # Mínimo 2 de 4 señales
}

# Size modifiers en EXTREME
EXTREME_CONFIG = {
    'size_modifier': 0.4,  # 40% del size normal
    'stop_multiplier': 0.7,  # 70% del stop normal (más tight)
    'rr_required_add': 0.3,  # +0.3 RR mínimo adicional
}

# STRUCTURE GATE overrides
STRUCTURE_GATE_CONFIG = {
    'normal_threshold': 6.0,  # % de distancia
    'override_adx_threshold': 50.0,  # ADX para override
    'override_displacement_adx': 40.0,  # ADX con displacement
}
```

---

## PARTE 8: ORDEN DE IMPLEMENTACIÓN

### Fase 1: INMEDIATO (hoy) - ✅ COMPLETADO
1. ✅ FIX #55 - SHORT OBSTINACY (IMPLEMENTADO)
2. ✅ LOCATION_EXTREME → SOFT MODE (FIX #56 - IMPLEMENTADO)
3. ✅ STRUCTURE GATE overrides (FIX #56 - IMPLEMENTADO)
4. ✅ Tight stops en location extreme (FIX #56 - IMPLEMENTADO)

### Fase 2: ✅ COMPLETADO (Feb 06)
5. ✅ Crash Detector (`src/domain/market/crash_detector.py`)
6. ✅ Absorption Detector (`src/domain/market/absorption_detector.py`)

### Fase 3: ✅ COMPLETADO (Feb 06)
7. ✅ Refactor de detección temprana vs tardía - ANALIZADO Y DOCUMENTADO
8. ✅ Métricas de "real missed" vs "fake missed" - IMPLEMENTADO
9. ✅ Integrar timing classification en opportunity_tracker

---

## FIX #56 IMPLEMENTADO (Feb 06, 2026)

### Cambios realizados:

#### 1. LOCATION_EXTREME → SOFT MODE (`entry_orchestrator.py`)
- Ya NO es HARD BLOCK
- Ahora activa SOFT MODE con tags:
  - `location_extreme_soft_mode = True`
  - `location_extreme_size_cap = 0.40` (40% max size)
  - `location_extreme_tight_stops = True`
  - `location_extreme_need_confirm = True`

#### 2. PANIC MODE Detection (`entry_orchestrator.py`)
```python
PANIC_ADX_5M_MIN = 28
PANIC_ADX_1H_MIN = 45
PANIC_VOL_RATIO_MIN = 1.3

is_panic_mode = (
    displacement_bypass and
    (adx_5m >= PANIC_ADX_5M_MIN or effective_adx >= PANIC_ADX_1H_MIN) and
    vol_ratio >= PANIC_VOL_RATIO_MIN
)
```
Si hay PANIC MODE → permite trading normal, no SOFT MODE.

#### 3. STRUCTURE GATE Overrides (`entry_scorer.py`)
Añadidos nuevos overrides:
- `expansion_mode_active` → bypasea structure gate
- `strong_trend_adx >= 55` → bypasea structure gate

#### 4. Size Cap Application (`entry_validators.py`)
El `location_extreme_size_cap` se aplica automáticamente en el sizing.

#### 5. Tight Stops (`entry_orchestrator.py`)
Cuando `location_extreme_tight_stops = True`:
- SL multiplier reducido 30% (ej: 2.0 → 1.4)
- Mínimo 1.0 ATR

---

## CONCLUSIÓN

### Los 3 problemas fundamentales (TODOS RESUELTOS FASE 1):

1. ✅ **LOCATION_EXTREME bloquea cuando debería modular** → FIX #56: SOFT MODE
2. ✅ **STRUCTURE GATE no tiene overrides** → FIX #56: Override con contexto
3. ✅ **Tight stops en extremos** → FIX #56: Implementado
4. 🟡 **Detección tardía se reporta como "MISSED"** → Pendiente: No es MISSED, es diseño

### Filosofía BE WATER aplicada:

```
❌ Bloquear en extremos = ROCA
✅ Modular en extremos = AGUA (IMPLEMENTADO)

❌ Gate binario = ROCA  
✅ Gate con contexto = AGUA (IMPLEMENTADO)

❌ Detectar después = REACCIÓN
✅ Aceptar que no todo es oportunidad perdida = SABIDURÍA
```

### Archivos modificados FIX #56:
- `src/application/live/entry_orchestrator.py` - LOCATION_EXTREME soft mode, tight stops
- `src/application/live/entry_validators.py` - Size cap application
- `src/domain/trading/scoring/entry_scorer.py` - Structure gate overrides
- `src/application/live/trading_app.py` - Updated calculate_entry_score calls
- `src/domain/market/crash_detector.py` - NEW: Panic/Crash detection
- `src/domain/market/absorption_detector.py` - NEW: Absorption pattern detection

```
✅ Anticipar con riesgo controlado = ACCIÓN
```

---

## NUEVOS MÓDULOS CREADOS

### Crash Detector (`src/domain/market/crash_detector.py`)

Detecta condiciones de PÁNICO vs AGOTAMIENTO:

```python
from src.domain.market.crash_detector import CrashDetector, CrashDetectorResult

detector = CrashDetector()
result: CrashDetectorResult = detector.detect(
    displacement_pct=-0.03,  # -3% displacement
    adx_5m=32.0,
    adx_1h=55.0,
    vol_ratio=1.5,
    direction='SHORT'
)

if result.is_panic_down:
    # Allow shorts even at extreme lows - market is crashing
    pass
```

### Absorption Detector (`src/domain/market/absorption_detector.py`)

Detecta absorción institucional antes de bloquear:

```python
from src.domain.market.absorption_detector import AbsorptionDetector

detector = AbsorptionDetector()
result = detector.detect(
    df_5m=df_5m,
    current_price=63000.0,
    vwap=62800.0,
    delta_ratio=1.8,  # 1.8x more buyers
    direction='SHORT'
)

if result.is_bullish_absorption:
    # Buyers absorbing at lows - maybe don't short
    pass
else:
    # No absorption - crash may continue, allow short
    pass
```

---

## APÉNDICE: Logs de evidencia

### MISSED que NO son MISSED

```log
23:28:43 | EDGE ARMED: LONG @ $63,636
         | BLOCK: STRUCTURE GATE | Distance: -10.57%
         | MISSED: LONG @ $63,636
         
         ❌ Esto NO es missed
         ✅ El precio ya subió $1,000 antes de la detección
         ✅ STRUCTURE GATE bloqueó correctamente (tarde para entrar)
```

### LOCATION_EXTREME bloqueando mal

```log
23:25:57 | WAITING EDGE | source=LOCATION_EXTREME_LOW | conf=0%
         
         ❌ Esto bloquea SHORTs en el piso
         ❌ Pero el piso puede seguir cayendo
         ✅ Debería: SOFT MODE con size reducido
```

### SHORT OBSTINACY loop infinito

```log
08:25:01 | EDGE ARMED: SHORT | ADX=57.2 | conf=60%
08:25:01 | 🚫 BLOCKED: SHORT OBSTINACY | 4 bullish signals
08:26:01 | EDGE INVALIDATED by OBSTINACY (3x blocks)
08:26:31 | EDGE ARMED: SHORT | (mismo ciclo)
         
         ❌ Loop infinito porque all_lagging=True pero no hay override
         ✅ FIX #55 resuelve esto con strong_trend_override
```

---

## FIX #57: TIMING CLASSIFICATION (Feb 06, 2026)

### PROBLEMA ANALIZADO

**Los "MISSED" son en realidad detección tardía, no oportunidades perdidas reales.**

El sistema actual tiene **6 puntos de latencia** en el flujo de detección:

#### 1. SWING DETECTION = CONFIRMACIÓN TARDÍA
```
Swings se detectan con lookback=5 en timeframe 1H
= 5+ HORAS de retraso antes de confirmar un swing
```

#### 2. BOS DETECTION = DESPUÉS DE LA RUPTURA
```python
if current_price > prev_swing_high:  # ⚠️ YA ROMPIÓ
    break_distance = (current_price - prev_swing_high) / atr
    if 0.1 < break_distance < 1.5:  # ⚠️ Ya se movió 0.1-1.5 ATR
        bos_detected = True
```
Con ATR = $500, esto significa detectar el BOS **$50-$750 tarde**.

#### 3. EDGE ARMED → EDGE ACTIVE = ESPERAR PULLBACK
El sistema ARMA el edge y luego espera pullback.
Si el mercado sigue sin pullback, el edge expira.
Resultado: "MISSED" cuando en realidad esperó algo que nunca pasó.

### SOLUCIÓN IMPLEMENTADA

**Clasificación de Timing en MissedOpportunityTracker:**

| Categoría | Move % | Descripción |
|-----------|--------|-------------|
| `EARLY` | < 0.3% | Detectamos ANTES del movimiento significativo |
| `ON_TIME` | < 0.8% | Detectamos al inicio del movimiento |
| `LATE` | < 1.5% | Detectamos tarde, pero podría valer |
| `VERY_LATE` | > 1.5% | Persiguiendo precio, NO es oportunidad |

**Clasificación de Missed:**

| Tipo | Descripción |
|------|-------------|
| `REAL_MISSED_EARLY` | Detectamos temprano, bloqueado - FILTRO MUY AGRESIVO |
| `REAL_MISSED_ONTIME` | Detectamos a tiempo, bloqueado - REVISAR FILTRO |
| `FAKE_MISSED_LATE` | Detectamos tarde - NO ES REAL OPORTUNIDAD |
| `FAKE_MISSED_CHASE` | Persiguiendo - CORRECTO BLOQUEAR |
| `DODGED_LOSER` | MAE > MFE - CORRECTO BLOQUEAR |

### ARCHIVOS MODIFICADOS

1. **`src/infrastructure/monitoring/opportunity_tracker.py`**
   - Añadido `SignalTimingCategory` enum
   - Añadido campos timing a `MissedOpportunity`
   - `log_blocked_signal()` ahora acepta timing params
   - `_resolve_opportunity()` clasifica REAL vs FAKE
   - `get_statistics()` incluye timing breakdown
   - `log_summary()` muestra análisis de timing

2. **`src/domain/trading/edge_state_manager.py`**
   - Añadido `move_at_detection_pct` y `setup_origin_price` a `BOSEvent`
   - `_arm_edge()` calcula move% al momento de armar
   - `calculate_signal_timing()` - método de utilidad
   - `get_timing_for_missed_tracker()` - formato para tracker

### MÉTRICAS NUEVAS EN LOG

```
📊 TIMING ANALYSIS (Real vs Fake Missed):
  🎯 REAL MISSED (detected early but blocked):
     Early: 2
     On-time: 3
     Total: 5 (15% of winners)
  ⏰ FAKE MISSED (detected late - not real opportunities):
     Late: 8
     Chase: 20
     Total: 28 (85% of winners)
```

**INTERPRETACIÓN:**
- Si `fake_missed_pct > 70%` → El sistema NO pierde oportunidades, solo detecta tarde
- Si `real_missed_pct > 30%` → Hay filtros demasiado agresivos que revisar

### PRÓXIMOS PASOS

Para reducir la detección tardía necesitamos:
1. Usar timeframes más rápidos para swing detection
2. Implementar ANTICIPATION más agresivo (compresión pre-breakout)
3. Aceptar que algunos "missed" son diseño, no bugs


---

## FIX #58: SEÑALES DE ANTICIPACIÓN (Feb 06, 2026)

### PROBLEMA

El sistema **siempre detecta las señales TARDE** porque:

1. **ANTICIPATION estaba DESHABILITADO** como trigger (`idle_state_handlers.py:540`)
2. **Swing lookback=5 en 1H** = ~5 horas de delay para detectar estructura
3. **PROVISIONAL requería ADX≥25** = muy restrictivo para entradas tempranas
4. **Solo entraba por BOS** = el movimiento ya avanzó 0.5-1.5%

### ANÁLISIS DE LATENCIA

| Fuente de Latencia | Delay Estimado |
|-------------------|----------------|
| Swing detection (lookback=5, 1H) | ~5 horas |
| BOS detection (0.1-1.5 ATR threshold) | 0.3-2.0% de move |
| Edge ARMED esperando pullback | Variable |
| PROVISIONAL requiere ADX≥25 | Pierde pre-trends |

### SOLUCIÓN IMPLEMENTADA

**Cambio 1: Habilitar ANTICIPATION como trigger**

Archivo: `src/application/live/idle_state_handlers.py`

```python
# ANTES: DESHABILITADO
# if anticipation.confidence >= 0.75 and anticipation.direction:
#     tags['anticipation_entry'] = True

# AHORA: HABILITADO con conf≥65% y estructura válida
ANTICIPATION_TRIGGER_CONFIDENCE = 0.65  # Bajado de 0.75
ANTICIPATION_PROBE_SIZE = 0.35          # Tamaño pequeño para probar

if (anticipation.confidence >= ANTICIPATION_TRIGGER_CONFIDENCE 
    and anticipation.direction
    and anticipation.structure_level > 0):  # Solo con estructura válida
    
    tags['anticipation_entry'] = True
    tags['anticipation_probe_size'] = ANTICIPATION_PROBE_SIZE
```

**Cambio 2: Reducir swing lookback de 5→3**

Archivo: `src/application/live/trading_app.py`

```python
# ANTES: 5 velas de 1H = ~5 horas de delay
swing_high_detected, _ = detect_swing_high(..., lookback=5)

# AHORA: 3 velas de 1H = ~3 horas (40% más rápido)
SWING_LOOKBACK_1H = 3
swing_high_detected, _ = detect_swing_high(..., lookback=SWING_LOOKBACK_1H)
```

**Cambio 3: PROVISIONAL más accesible**

Archivo: `src/domain/trading/edge_state_manager.py`

```python
# ANTES: Requería bias fuerte (ADX≥25)
MIN_ADX_5M = 25  # STRONG_TREND

# AHORA: Permite bias en desarrollo (ADX≥20)
MIN_ADX_5M = 20  # MODERATE_TREND
```

### FLUJO MEJORADO

```
ANTES:
  Precio comprime → [espera 5h swing] → BOS detectado → [ya movió 1.5%] → entrada TARDE

AHORA:
  Precio comprime → ANTICIPATION detecta → [conf≥65%] → entrada TEMPRANA (probe 0.35x)
                  ↓
  Si breakout confirma → edge upgrades → añade posición (0.65x más)
  Si falla → sale con pérdida pequeña (0.35x)
```

### RIESGOS Y MITIGACIONES

| Riesgo | Mitigación |
|--------|------------|
| ANTICIPATION falsos positivos | Requiere structure_level > 0 + conf≥65% |
| Swing lookback=3 menos robusto | Solo afecta 1H, no 5m (lookback=12) |
| PROVISIONAL más trades | ADX≥20 sigue requiriendo direccionalidad |
| Over-trading | probe_size 0.35x limita exposure |

### ARCHIVOS MODIFICADOS

1. `src/application/live/idle_state_handlers.py` - Habilitar ANTICIPATION trigger
2. `src/application/live/trading_app.py` - Reducir swing lookback 5→3
3. `src/domain/trading/edge_state_manager.py` - PROVISIONAL ADX 25→20
