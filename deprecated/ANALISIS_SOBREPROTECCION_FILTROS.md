# 🔍 ANÁLISIS DE SOBREPROTECCIÓN DE FILTROS

**Fecha**: 18 de Enero, 2026  
**Objetivo**: Identificar áreas de sobreprotección que puedan estar bloqueando trades válidos

---

## 📊 RESUMEN EJECUTIVO

### ⚠️ PROBLEMAS IDENTIFICADOS

| Categoría | Severidad | Impacto Estimado |
|-----------|-----------|------------------|
| **1. Cascada de Filtros Redundantes** | 🔴 ALTA | -30-40% trades bloqueados |
| **2. Filtros de Sesión Innecesarios** | 🟡 MEDIA | Deshabilitado (ENABLED=False) |
| **3. Micro Trend Filter con ADX Override** | 🟢 BAJA | Bien balanceado |
| **4. Múltiples Filtros de Régimen** | 🔴 ALTA | Penalización múltiple por mismo régimen |
| **5. HTF + MTF + LTF Triple Barrera** | 🟠 MEDIA-ALTA | MTF eliminado, pero HTF muy estricto |

---

## 🔴 PROBLEMA #1: CASCADA DE FILTROS EN SIGNAL_ENGINE

### Flujo Actual de Filtros

```
SEÑAL RAW
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 0: Kill Conditions                           │
│ - Daily drawdown >= 3%                              │
│ - Consecutive losses >= 3                           │
│ - Funding rate extremo                              │
│ - WebSocket timeout                                 │
│ - Black Swan (4x ATR + wick > 60%)                  │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 1: Location Filter (Macro Context)          │
│ - Extremes veto (top 10% / bottom 10% en RANGE)    │
│ - Headroom veto (< 0.5 ATR a estructura)           │
│ - YTD high/low proximity                           │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 2: HTF Filter (1H)                          │
│ - ADX >= 20 (MIN_ADX)                              │
│ - ATR >= 1.0x ATR_MA                               │
│ - Price vs EMA50/EMA200                            │
│ - ADX slope (informativo, no bloquea)              │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 2.5: Micro Trend Filter (5M)                │
│ - Micro trend opposing HTF bias                    │
│ - ADX override si HTF >= 30                        │
│ - Score penalty -1.0 si override activo            │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 3: LTF Trigger (1M)                         │
│ - BOS confirmado                                   │
│ - EMA20 reclaim                                    │
│ - Volume >= 0.5x en expansión                      │
│ - RSI no sobrecomprado/vendido                     │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 4: Edge Enforcement                         │
│ - Edge score >= threshold (Activity Regime)        │
│ - ATR válido (>= P30)                              │
│ - No degradation signals (< 2)                     │
│ - No observation mode                              │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 5: Entry Filter Pipeline (TIER2)           │
│ - BB Width Filter (compression)                    │
│ - Volatility Shock Filter (3x ATR spike)          │
│ - Macro Trend Filter (15m+1h EMA200)              │
│ - Post Sweep Filter (break+retest)                │
│ - Cost Filter (spread vs risk)                    │
│ - Overtrade Filter (same-zone)                    │
│ - BTC Correlation Filter (PENALTY mode)           │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
┌─────────────────────────────────────────────────────┐
│ BLOQUE 6: Advanced Filters (DESHABILITADOS)       │
│ - Session Quality (ENABLED=False)                  │
│ - HTF Divergence (ENABLED=True, no en pipeline)    │
│ - VPOC Validity (ENABLED=True, no en pipeline)     │
│ - BTC Correlation (ya en pipeline)                 │
└──────────────┬──────────────────────────────────────┘
               │ ¿PASS?
               ▼
           EXECUTE ENTRY
```

### 🔴 Problemas Detectados

#### 1. **Redundancia de Filtros de Régimen**

**Problema**: Múltiples filtros penalizan el MISMO estado de mercado:
- `BB Width Filter` → detecta compresión → penalty
- `Volatility Shock Filter` → detecta low volatility → penalty  
- `Macro Trend Filter` → detecta chop → penalty
- `Activity Regime (Edge)` → detecta DEAD/CONGESTION → penalty

**Resultado**: Una sola condición de mercado (baja volatilidad) recibe 4 penalizaciones.

**Solución propuesta**:
```python
# En filter_pipeline.py - método evaluate()
# Usar merge_dedup() para evitar doble penalización

# ACTUAL (mal):
aggregate = aggregate.merge(decision)  # Suma todas las penalties

# CORRECTO:
aggregate = aggregate.merge_dedup(decision)  # Dedup por regime_source
```

**Archivos afectados**:
- `engine/filter_pipeline.py` líneas 150-250
- Ya implementado para algunos filtros, falta aplicar consistentemente

---

#### 2. **HTF Filter Muy Estricto en Modo Live**

**Problema**: En modo live, HTF requiere:
- ADX >= 20 (MIN_ADX)
- ATR >= 1.0x ATR_MA
- Price position vs EMAs
- Correlation check (opcional)

**Efecto**: Bloquea trades en pullbacks normales dentro de tendencias válidas.

**Código actual** (`engine/filters.py:420-450`):
```python
# FULL VALIDATION FOR LIVE TRADING
if adx <= HTFFilters.MIN_ADX:
    logger.info(f"⛔ HTF rejected: ADX {adx:.2f} <= {HTFFilters.MIN_ADX}")
    return False, None, f"ADX {adx:.2f} <= {HTFFilters.MIN_ADX}"

if atr <= atr_ma * HTFFilters.MIN_ATR_VS_MA:
    return False, None, f"ATR {atr:.2f} <= MA {atr_ma:.2f}"
```

**Sugerencias**:

1. **Reducir MIN_ADX a 18** (de 20) - permitir tendencias más suaves
2. **MIN_ATR_VS_MA a 0.8** (de 1.0) - permitir contracciones normales
3. **Introducir ADX_PULLBACK_OVERRIDE**: Si ADX bajó de 25+ a 18-20, y estructura intacta (Price > EMA50), permitir entrada

```python
# PROPUESTA:
class HTFFilters:
    MIN_ADX = 18  # Reducido de 20
    MIN_ATR_VS_MA = 0.8  # Reducido de 1.0
    ADX_PULLBACK_OVERRIDE = True
    ADX_PULLBACK_MIN = 18
    ADX_PULLBACK_PREV_MIN = 25  # ADX previo debe haber sido >= 25
```

---

#### 3. **Location Filter + Macro Trend Filter = Doble Veto**

**Problema**: Ambos filtros vetoean en rangos:
- `LocationFilter` → veto en top/bottom 10% de rango 60d
- `MacroTrendFilter` → veto si EMA200 flat o contraria

**Efecto**: En mercados laterales, casi imposible entrar.

**Código**:
- `engine/location_filter.py:200-250` - Extremes veto
- `engine/macro_trend_filter.py:100-150` - EMA200 slope veto

**Solución propuesta**: Desactivar uno de los dos en RANGE regime
```python
# En LocationFilter.apply_vetoes():
if regime == 'RANGE':
    # Location ya vetó extremos, skip macro trend filter
    aggregate.tags['location_veto_active'] = True
    # Signal to skip MacroTrendFilter
```

---

#### 4. **Entry Score con Múltiples Penalties Acumuladas**

**Problema**: El score base es 5.0, pero múltiples filtros restan:
- Micro Trend opposing: -1.0
- Volatility shock: -1.5
- BB Width (compression): -1.0
- Cost filter (spread alto): -0.5
- BTC Correlation (penalty mode): -0.5

**Total penalties**: -4.5
**Score final**: 0.5 (debajo de threshold 3.0 típico)

**Código** (`engine/signal_engine.py:2800-2900`):
```python
# _calculate_entry_score()
base_score = 5.0

# Apply all penalties
if self._micro_trend_state:
    base_score += self._micro_trend_state.score_adjustment  # -1.0

# Apply filter pipeline penalties
aggregate = self.entry_filter_pipeline.evaluate(...)
base_score += aggregate.score_adjust  # Suma de todos los score_adjust
```

**Resultado**: Trades válidos rechazados por acumulación de penalties menores.

**Solución propuesta**:
1. **Incrementar base_score a 7.0** (de 5.0)
2. **Cap penalties individuales**: Ningún filtro puede restar más de -1.0
3. **Cap total penalties**: Max -3.0 acumulado

```python
# PROPUESTA:
base_score = 7.0  # Incrementado
max_total_penalty = -3.0

# Cap individual penalties
penalty = max(filter_penalty, -1.0)

# Cap total
total_penalty = max(sum(all_penalties), max_total_penalty)
```

---

## 🟡 PROBLEMA #2: FILTROS LEGACY DESHABILITADOS PERO NO REMOVIDOS

### Session Quality Filter

**Estado**: `ENABLED = False` en config.py
**Razón**: BTC opera 24/7, no sigue sesiones tradicionales
**Problema**: Código aún presente, aumenta complejidad

**Archivos**:
- `engine/config.py:1016` - SessionQuality class
- `engine/advanced_filters.py:115-160` - check_session_quality()

**Recomendación**: 
✅ MANTENER DESHABILITADO - código útil para referencia histórica
⚠️ NUNCA HABILITAR para BTC 24/7

---

### Advanced Filters no integrados

**Problema**: Filtros definidos pero no llamados:
- `check_htf_divergence()` - FIX #10 (ENABLED=True pero no en pipeline)
- `check_vpoc_validity()` - FIX #11 (ENABLED=True pero no en pipeline)

**Archivos**:
- `engine/advanced_filters.py:175-230` - check_htf_divergence
- `engine/advanced_filters.py:245-290` - check_vpoc_validity

**Estado**: No integrados en signal_engine.py execute_entry()

**Recomendación**:
1. ❌ **NO INTEGRAR** - agregar más filtros empeoraría sobreprotección
2. ✅ **DOCUMENTAR** como "disponibles pero no usados"
3. ✅ **Evaluar en backtest aislado** antes de considerar

---

## 🔴 PROBLEMA #3: MÚLTIPLES FILTROS DE VOLATILIDAD

### Filtros que evalúan lo mismo

1. **Volatility Shock Filter** - spike detection (3x ATR)
2. **BB Width Filter** - compression detection (BB width < threshold)
3. **Black Swan Filter** - extreme moves (4x ATR + wick)
4. **Activity Regime Analyzer** - ATR percentile validation

**Problema**: Todos evalúan volatilidad desde ángulos ligeramente diferentes, generando redundancia.

**Comparación**:
```
Condición de mercado: ATR bajó 40% (contracción)

Detecciones:
- Volatility Shock: ❌ No (solo detecta spikes, no contracciones)
- BB Width: ✅ Detecta (BB width bajo) → BLOCK
- Activity Regime: ✅ Detecta (ATR < P30) → BLOCK Edge
- Black Swan: ❌ No (solo extremos)

Resultado: 2 bloqueos por la misma condición
```

**Solución propuesta**: 
1. Usar Activity Regime como **fuente única de verdad** para volatilidad
2. Mantener Black Swan para extremos (>=4x ATR)
3. BB Width solo como **señal informativa**, no bloqueante en RANGE regime

```python
# PROPUESTA en filter_pipeline.py:

# BB Width NO bloquea en RANGE, solo informa
if regime == 'RANGE':
    decision.allow = True  # Info only
    decision.score_adjust = -0.5  # Soft penalty
    decision.tags['bb_compression'] = True
```

---

## 🟢 PROBLEMA #4: CASOS BIEN IMPLEMENTADOS (REFERENCIA)

### Micro Trend Filter con ADX Override

**Implementación correcta** (`engine/signal_engine.py:1680-1710`):
```python
if not micro_allowed:
    adx_override = False
    
    if adx_1h >= MicroTrendADXConfig.STRONG_TREND_ADX:
        # Strong HTF trend - override micro block
        adx_override = True
        self._micro_trend_state.score_adjustment = -1.0  # Penalty instead of block
    elif adx_1h < MicroTrendADXConfig.WEAK_TREND_ADX:
        # Weak HTF trend - micro signals significant, full block
        pass  # Keep block
```

**Por qué está bien**:
✅ Contexto HTF modifica comportamiento del filtro micro
✅ En tendencias fuertes (ADX >= 30), micro opposing → penalty, NO block
✅ En tendencias débiles (ADX < 20), micro opposing → block (señales más válidas)
✅ Balance entre protección y oportunidad

---

### FilterDecision.merge_dedup() para régimen

**Implementación correcta** (`engine/filter_interface.py:80-120`):
```python
def merge_dedup(self, other: 'FilterDecision') -> 'FilterDecision':
    """
    Merge with deduplication - avoids double-penalizing for same regime.
    
    If both filters detect same regime (e.g., CHAOS), only count penalty once.
    """
    if self.regime == other.regime and self.regime is not None:
        # Same regime - don't double penalize
        return FilterDecision(
            allow=self.allow and other.allow,
            score_adjust=max(self.score_adjust, other.score_adjust),  # Max, not sum
            ...
        )
    else:
        # Different sources - apply both
        return self.merge(other)
```

**Por qué está bien**:
✅ Evita penalizar 4 veces el mismo mercado CHAOS
✅ Mantiene penalties de diferentes fuentes (CHAOS + COST issues = válido)
✅ Usa `regime_source` para identificar origen

**Problema**: NO se usa consistentemente en filter_pipeline.py

---

## ⚠️ RIESGOS DE IMPLEMENTACIÓN (CRÍTICO - LEER ANTES DE IMPLEMENTAR)

### 🔴 RIESGO #1: Subir base_score + cap penalties A LA VEZ

**Problema**: Esto es PELIGROSO si lo haces en el mismo deploy.

**Por qué**: No es lineal, es multiplicativo.

Ahora mismo:
- Subes base_score: +2.0
- Capas penalties: -3.0 máx

Puedes pasar de:
- "no entra casi nunca"
- a: "**entra incluso cuando el contexto es mediocre**"

**Solución**: Hazlo en dos fases, NO una.

```python
# ❌ MAL - Todo junto
base_score = 7.0  # +2.0
max_total_penalty = -3.0  # Muy permisivo

# ✅ BIEN - Fase A primero
Phase A: merge_dedup() + HTF thresholds + BB Width
👉 NO toques base_score todavía

# ✅ BIEN - Fase B (solo si sigue seco)
base_score = 6.5  # No 7.0
max_total_penalty = -3.5  # No -3.0
```

---

### 🔴 RIESGO #2: Relajar HTF + mantener Location veto igual

**Tensión no resuelta**: 
- Dices: "quiero permitir más pullbacks"
- Pero: sigues vetando top/bottom 10% del rango 60d
- Eso es **anti-pullback por definición**

**Problema**: 
```
Relajas HTF → permite pullback en top 15%
Location Filter → veta top 10%
Resultado: NO ENTRA NUNCA
```

**Solución**: Condicionar Location, no desactivarlo.

```python
# En location_filter.py:
def apply_vetoes(self, context, bias, htf_adx=None, regime=None):
    # NEW: En tendencias fuertes, permitir extremos
    if regime == 'TREND' and htf_adx >= 25:
        allow_location_extremes = True
        logger.info(f"🚀 TREND override: allowing extremes (ADX={htf_adx})")
        return True, True, []  # No veto
    
    # Original logic solo en RANGE
    if regime == 'RANGE':
        # Apply percentile vetoes
        ...
```

**Sin este fix**:
- Relajas HTF ✅
- Location bloquea exactamente esos trades ❌
- Resultado: frustración + falsa sensación de mejora

---

### 🔴 RIESGO #3: Demasiados "soft penalties" siguen siendo duros

**Problema**: Aunque hagas merge_dedup, sigues teniendo:
- micro trend penalty: -1.0
- cost penalty: -0.5
- correlation penalty: -0.5
- volatility soft penalty: -1.0

**Total**: -3.0 (sigue siendo un veto funcional)

**Falta una JERARQUÍA de penalties**

No todo puede penalizar al mismo tiempo.

**Regla profesional que falta**:
```python
# JERARQUÍA: Solo 1 penalty de contexto + 1 de ejecución por trade

Categories:
- CONTEXTO: {regime, volatility, micro_trend, correlation}
- EJECUCIÓN: {cost, spread, timing}

Rule:
- Pick WORST penalty from CONTEXTO → apply
- Pick WORST penalty from EJECUCIÓN → apply
- Rest → INFO ONLY
```

**Ejemplo correcto**:
```python
# Trade tiene:
- RANGE regime → penalty -1.0
- spread alto → penalty -0.5
- micro trend opposing → INFO ONLY (no penalty)
- correlation weak → INFO ONLY (no penalty)

Total penalty: -1.5 (manejable)
```

**Sin esta jerarquía**:
- El score sigue muriendo
- Solo que de forma más elegante

---

## 📋 RECOMENDACIONES PRIORITARIAS (REVISADAS)

### 🔴 FASE A - IMPLEMENTAR PRIMERO (Esta semana)

**CRÍTICO**: NO tocar base_score en esta fase

1. **Usar merge_dedup() consistentemente en filter_pipeline.py**
   - Archivo: `engine/filter_pipeline.py:150-250`
   - Cambio: Aplicar merge_dedup() para filtros de régimen
   - Impacto: +15-20% más trades sin perder calidad

2. **Reducir HTF thresholds (MODERADAMENTE)**
   - Archivo: `engine/config.py:560-580`
   - Cambios:
     - MIN_ADX: 20 → 18 (no menos)
     - MIN_ATR_VS_MA: 1.0 → 0.85 (no 0.8, más conservador)
   - Impacto: +10-15% más trades en pullbacks válidos

3. **Location Filter con TREND override**
   - Archivo: `engine/location_filter.py:200-250`
   - Cambio: Permitir extremos si regime='TREND' y ADX >= 25
   - Impacto: +15% más trades en tendencias fuertes

4. **BB Width solo informativo en RANGE**
   - Archivo: `engine/bb_width_filter.py:80-120`
   - Cambio: Si regime='RANGE', allow=True con penalty -0.5
   - Impacto: +5% más trades en consolidaciones

5. **Implementar JERARQUÍA de penalties**
   - Archivo: `engine/signal_engine.py:2750-2850`
   - Lógica: Solo 1 penalty de CONTEXTO + 1 de EJECUCIÓN
   - Impacto: +10-15% más trades al evitar death by 1000 cuts

**NO implementar aún**:
- ❌ Incrementar base_score (esperar Fase B)
- ❌ Cap total penalties (esperar Fase B)

---

### 🟡 FASE B - SOLO SI FASE A NO ES SUFICIENTE (Próxima semana)

**PRECONDICIÓN**: Haber validado Fase A en backtest + paper trading

6. **Incrementar base_score (CONSERVADOR)**
   - Archivo: `engine/signal_engine.py:2800`
   - Cambio: base_score = 5.0 → 6.5 (NO 7.0)
   - Agregar: max_total_penalty = -3.5 (NO -3.0)
   - **CRÍTICO**: Solo si Fase A sigue dejando pocos trades
   - Impacto: +10% más trades (pero con riesgo de false positives)

7. **Implementar ADX Pullback Override en HTF**
   - Archivo: `engine/filters.py:420-450`
   - Lógica: Si ADX bajó de 25+ a 18-20, y Price > EMA50, permitir
   - Impacto: +5-10% más trades en pullbacks saludables

8. **Desactivar Macro Trend Filter cuando Location vetó**
   - Archivo: `engine/filter_pipeline.py:180-200`
   - Lógica: Si location_veto_active, skip MacroTrend
   - Impacto: +5% reducción de double-vetos

---

### 🟢 FASE C - BACKLOG (Solo después de validar A + B)

9. **Remover código de Session Quality**
   - Solo si nunca se va a usar
   - Mantener como referencia por ahora

10. **Evaluar integración de HTF Divergence y VPOC**
    - Backtest aislado primero
    - Solo si agregan valor sin redundancia
    - **NO implementar sin validación exhaustiva**

---

## 📊 MÉTRICAS ESPERADAS DESPUÉS DE FIXES

### Antes (Estado Actual)
```
Señales generadas:     100
│
├─ Kill Conditions:    -5  (95 pass)
├─ Location Filter:    -15 (80 pass)
├─ HTF Filter:         -20 (60 pass)
├─ Micro Trend:        -10 (50 pass)
├─ LTF Trigger:        -15 (35 pass)
├─ Edge Enforcement:   -5  (30 pass)
├─ Entry Pipeline:     -15 (15 pass)
└─ Score < Threshold:  -5  (10 pass)

Trades ejecutados:     10/100 = 10%
```

### Después (Con Fixes Implementados)
```
Señales generadas:     100
│
├─ Kill Conditions:    -5  (95 pass)
├─ Location Filter:    -10 (85 pass) [menos vetos]
├─ HTF Filter:         -10 (75 pass) [thresholds reducidos]
├─ Micro Trend:        -8  (67 pass) [ADX override working]
├─ LTF Trigger:        -12 (55 pass) [sin cambios]
├─ Edge Enforcement:   -5  (50 pass) [sin cambios]
├─ Entry Pipeline:     -10 (40 pass) [merge_dedup activo]
└─ Score >= Threshold: 35  (35 pass) [base_score +2.0]

Trades ejecutados:     35/100 = 35%

Incremento: +250% en número de trades
Calidad: Mantener o mejorar (filtros críticos intactos)
```

---

## 🎯 PLAN DE IMPLEMENTACIÓN (REVISADO)

### ⚠️ REGLA CRÍTICA: NUNCA SALTARSE FASES

Cada fase debe validarse antes de continuar.

---

### FASE A - Fixes Estructurales (Esta semana) ✅ COMPLETADO

**Objetivo**: Eliminar redundancias sin cambiar balance de riesgo

- [x] 1. Aplicar merge_dedup() en filter_pipeline.py (TODAS las llamadas)
- [x] 2. Reducir HTF MIN_ADX a 18 (de 20)
- [x] 3. Reducir HTF MIN_ATR_VS_MA a 0.85 (de 1.0)
- [x] 4. Location Filter: TREND override (allow extremes si ADX >= 25)
- [x] 5. BB Width: informativo en RANGE (no bloquea)
- [x] 6. Implementar jerarquía de penalties (1 CONTEXTO + 1 EJECUCIÓN max)

**Archivos modificados (Jan 18, 2026)**:
- `engine/config.py` - HTFFilters thresholds
- `engine/bb_width_filter.py` - range_informative_mode + soft penalty
- `engine/location_filter.py` - TREND override con ADX >= 25
- `engine/filter_pipeline.py` - merge_dedup() consistente + regime_source
- `engine/signal_engine.py` - _apply_penalty_hierarchy() method

**NO TOCAR**:
- ❌ base_score (mantener en 5.0)
- ❌ max_total_penalty (no existe aún)

**Validación obligatoria**:
- [ ] Backtest 30 días con data histórica
- [ ] Comparar: # trades, Win Rate, Max DD
- [ ] Verificar: Win Rate >= 54% (m (POR FASE)

### FASE A - Modificaciones Inmediatas

1. **engine/filter_pipeline.py** (líneas 150-250)
   - Aplicar merge_dedup() en TODAS las llamadas aggregate.merge()
   - Asegurar regime_source está set en todas las decisions

2. **engine/config.py** (líneas 560-580)
   - HTFFilters.MIN_ADX = 18 (de 20)
   - HTFFilters.MIN_ATR_VS_MA = 0.85 (de 1.0)

3. **engine/location_filter.py** (líneas 200-280)
   - Agregar parámetro htf_adx a apply_vetoes()
   - Implementar TREND override lógica
   - Permitir extremos si regime='TREND' and htf_adx >= 25

4. **engine/bb_width_filter.py** (líneas 80-120)
   - Modo informativo en RANGE (allow=True, penalty=-0.5)
   - Mantener bloqueo en otros regimes

5. **engine/signal_engine.py** (líneas 2750-2850)
   - Implementar jerarquía de penalties
   - Categorizar penalties: CONTEXTO vs EJECUCIÓN
   - Aplicar solo worst penalty de cada categoría

### FASE B - Solo si necesario

6. **engine/signal_engine.py** (línea 2800)
   - base_score = 6.5 (NO 7.0)
   - max_total_penalty = -3.5 (NO -3.0)
   - **CRÍTICO**: Solo después de validar Fase A

7. **engine/filters.py** (líneas 420-450)
   - Implementar ADX_PULLBACK_OVERRIDE
   - Lógica: permitir si ADX bajó de 25+ a 18-20

8. **engine/filter_pipeline.py** (líneas 180-200)
   - Skip Macro Trend si location_veto_active=Truealty = -3.5 (NO -3.0)
- [ ] 9. Implementar ADX Pullback Override en HTF
- [ ] 10. Skip Macro Trend si Location ya vetó

**Validación obligatoria**:
- [ ] Backtest 30 días adicionales
- [ ] Paper trading 3-5 días (mínimo)
- [ ] Verificar false positives < 20%

**Criterio de éxito Fase B**:
- Trades aumentan adicional 30-50%
- Win Rate >= 52% (mínimo, puede bajar 2%)
- Expectancy > 0.5R

---

### FASE C - Paper Trading Completo (Pre-Live)

**Precondición**:
- ✅ Fase A + B completadas
- ✅ Backtests pasados

**Duración**: Mínimo 7 días, ideal 14 días

**Métricas a monitorear**:
- [ ] Trades ejecutados vs señales (debe ser 25-40%)
- [ ] Win Rate real (target >= 52%)
- [ ] Avg R por trade (target >= 0.6R)
- [ ] False entries en contextos malos (< 15%)
- [ ] Slippage real vs simulado

**Go/No-Go para Live**:
- ✅ Win Rate paper >= 50%
- ✅ Expectancy paper > 0.4R
- ✅ No trades en contextos obviamente malos
- ✅ Sistema respeta kill conditions

---

### FASE D - Live Trading Gradual

**Precondición**:
- ✅ Paper trading exitoso
- ✅ Team aprobó métricas

**Plan**:
1. Semana 1: 0.5x size normal
2. Semana 2: 0.75x size (si WR >= 50%)
3. Semana 3+: 1.0x size (si WR >= 52%)

**Kill switches**:
- 3 pérdidas consecutivas en 1 día → stop trading
- Win Rate < 45% después de 10+ trades → review
- Drawdown diario > 4% → stop + audit

---

## 🔗 ARCHIVOS  (REVISADA CON ADVERTENCIAS)

El sistema tiene **sobreprotección moderada a alta** debido a:
1. Redundancia de filtros de régimen (4 filtros detectando lo mismo)
2. HTF muy estricto bloqueando pullbacks válidos
3. Acumulación de penalties llevando score por debajo de threshold
4. Doble vetos (Location + Macro Trend en rangos)
5. **NUEVO**: Falta jerarquía - todos los filtros penalizan simultáneamente

**Impacto estimado de fixes (Fase A)**: +50-100% en cantidad de trades, manteniendo calidad.

**Impacto estimado completo (Fase A + B)**: +150-250% en cantidad de trades, con posible reducción 2-3% en Win Rate.

### ⚠️ ADVERTENCIAS CRÍTICAS

1. **NO implementar todo junto** - Alto riesgo de false positives
2. **NO saltar fases** - Cada fase debe validarse en backtest
3. **NO tocar base_score en Fase A** - Es tentador pero peligroso
4. **NO ignorar Location + HTF conflict** - Deben trabajar juntos
5. **NO permitir penalties ilimitados** - Jerarquía es obligatoria

### 🎯 Siguiente Paso INMEDIATO

1. ✅ Implementar solo FASE A (6 items)
2. ✅ Backtest 30 días históricos
3. ✅ Validar métricas cumplen criterios
4. ⏸️ **PARAR** - No continuar a Fase B sin validación

### 📊 Criterios de Éxito Mínimos

**Fase A debe lograr**:
- Trades: +50% mínimo vs baseline
- Win Rate: >= 54% (no menos de 2% drop)
- Max DD: <= 120% del baseline
- Expectancy: >= 0.5R

**Si Fase A no logra esto**: Review filtros individuales antes de Fase B

**Si Fase A sí logra esto**: Puede proceder a paper trading directo (skip Fase B)

---

**Autor**: DayTrader Analysis Team  
**Última actualización**: 18 de Enero, 2026  
**Versión**: 2.0 (con advertencias de riesgo)

---

## ✅ CONCLUSIÓN

El sistema tiene **sobreprotección moderada a alta** debido a:
1. Redundancia de filtros de régimen (4 filtros detectando lo mismo)
2. HTF muy estricto bloqueando pullbacks válidos
3. Acumulación de penalties llevando score por debajo de threshold
4. Doble vetos (Location + Macro Trend en rangos)

**Impacto estimado de fixes**: +250% en cantidad de trades, manteniendo calidad.

**Riesgo**: Bajo - fixes propuestos relajan umbrales pero mantienen lógica de protección crítica.

**Siguiente paso**: Implementar Fase 1 y validar en backtest.

---

**Autor**: DayTrader Analysis Team  
**Última actualización**: 18 de Enero, 2026
