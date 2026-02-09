# ANÁLISIS LOG TRADING RUN 2026-02-07 / 2026-02-08

**Archivo:** `logs/trading_run_20260207_202504_d9dd5ad2.log`  
**Total líneas:** 16,251  
**Período:** 2026-02-07 21:25 → en curso  
**Analista:** Sistema automático + revisión manual

---

## RESUMEN EJECUTIVO

| Métrica | Valor |
|---------|-------|
| **Trades ejecutados** | 0 |
| **Intentos de entrada fallidos** | 28+ (bloques 1-6) |
| **Causa raíz** | BUG: `EntryRegime.TRANSITION` no existe |
| **Estado del fix** | ✅ APLICADO |
| **Balance** | $254.54 (sin cambios) |
| **Período analizado** | 21:25 - 10:39 UTC (~13 horas) |
| **Tiempo en cooldown** | ~2 horas acumuladas |
| **Market time stale warnings** | 300+ |
| **TREND_EXPANSION perdidos** | 8+ (ADX 51-53) |

---

## BLOQUE 1 (Líneas 1-1000)
**Período:** 21:25 - 00:56 UTC (≈3.5 horas)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 4 ocurrencias | 100% trades bloqueados |
| 2 | BIAS FLIP loop | 🟡 Alto | ~8 veces | Oscilación sin entrada |
| 3 | Penalizaciones excesivas | 🟡 Alto | Constante | Score siempre < 2.0 |
| 4 | Volume divergence -30% | 🟡 Medio | En cada tick | Sizing destruido |
| 5 | BB WIDTH extreme compression | 🟠 Bajo | Constante | Penalty adicional |
| 6 | Spam "ANTICIPATION preserved" | 🟠 Bajo | 17+ veces consecutivas | Ruido en logs |

### Secuencia de Fallas

```
21:33 - EDGE_ACTIVE SHORT armado
21:34 - Invalidado por precio > level+buffer
      ...oscilación en rango...
23:44 - Nuevo EDGE_ACTIVE SHORT
23:46 - INTENTO #1 → AttributeError: EntryRegime.TRANSITION → Cooldown 5min
00:06 - INTENTO #2 → mismo error → Cooldown 5min
00:21 - INTENTO #3 → mismo error → Cooldown 5min
00:48 - Detecta LONG → INTENTO #4 → mismo error → Cooldown 5min
```

### Métricas

- Trades: 0
- Balance: $254.54
- Ticks evaluados: 9,600
- Precio: $69,000 - $69,700 (rango ~$700)

---

## BLOQUE 2 (Líneas 1001-2000)
**Período:** 01:01 - 05:05 UTC (≈4 horas)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 5 ocurrencias más | 100% trades bloqueados |
| 2 | DISPLACEMENT detectado pero no ejecutado | 🔴 CRÍTICO | 4 veces | Pérdida de oportunidades |
| 3 | BIAS FLIP loop continuo | 🟡 Alto | ~12 veces | Nunca estabiliza |
| 4 | BTC correlation cooldown | 🟡 Medio | Constante | Penaliza -1.8 |
| 5 | Spam "ANTICIPATION preserved" | 🟠 Bajo | 60+ líneas | Ruido masivo |

### Secuencia de Fallas

```
01:36 - DISPLACEMENT $453 detectado → SETUP COMPLETE → AttributeError → Cooldown 5min
01:42 - INTENTO #2 tras cooldown → mismo error → Cooldown 5min
03:22 - DISPLACEMENT LONG detectado → Setup OK → mismo error → Cooldown 5min  
03:55 - DISPLACEMENT $500 detectado → Setup OK → mismo error → Cooldown 5min
04:57 - Nuevo SHORT edge → Setup OK → mismo error → Cooldown 5min
```

### Métricas

- Trades: 0
- Balance: $254.54
- Ticks evaluados: 9,600 → 17,880
- Precio: $68,935 - $69,653 (rango ~$718)
- Cooldowns acumulados: 5 × 5min = 25min en pausa

---

## BLOQUE 3 (Líneas 2001-3000)
**Período:** 05:23 - 06:31 UTC (≈1 hora)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 4 ocurrencias más | 100% trades bloqueados |
| 2 | Market time stale (>120s) | 🟡 Alto | ~150+ warnings | Datos desincronizados |
| 3 | COMPRESSION_BREAKOUT no puede ejecutar | 🔴 CRÍTICO | ~15 veces | Pierde oportunidades |
| 4 | BIAS FLIP loop continuo | 🟡 Alto | 3 veces en 1 hora | Inestabilidad |
| 5 | BB Width penalty en compression | 🟠 Medio | Constante -0.5 | Contradictorio |

### Secuencia de Fallas

```
05:26 - TRANSITION LONG (DISPLACEMENT) conf=70% → Setup OK → AttributeError → Cooldown 5min
05:31 - En cooldown: 175 more seconds
05:54 - TREND_DISPLACEMENT LONG ADX=46.2 conf=70% → Setup 3/3 → AttributeError → Cooldown 5min
05:59 - TREND LONG ADX=39.2 → Setup 2/3 → AttributeError → Cooldown 5min
06:05 - EDGE_ACTIVE LONG conf=80% → Setup 1/3 → (no intenta, score bajo)
06:10 - Setup 2/3 → AttributeError → Cooldown 5min
06:16 - Setup 3/3 LTF=3/3 → AttributeError → Cooldown 5min
```

### Nuevo Problema: Market Time Stale

El log muestra **masivos warnings de tiempo stale**:
```
[WARN] Market time stale by 120s - using real time
[WARN] Market time stale by 157s - using real time
```
**Implicación:** Los datos de mercado se desincronizaron repetidamente por >2 minutos.

**Períodos afectados:**
- 05:26 - 05:26 (4 segundos de stale burst)
- 05:33 - 05:33 (15 segundos)
- 05:42 - 05:43 (10 segundos)
- 05:48 - 05:49 (prolongado ~37 segundos hasta 157s stale)
- 06:07 - 06:09 (prolongado ~2 minutos)
- 06:28 - 06:30 (varios)

**Posible causa:** WebSocket desconectado momentáneamente o rate limiting.

### COMPRESSION_BREAKOUT Detectado Pero No Ejecutado

Múltiples anticipaciones de breakout detectadas pero nunca ejecutadas:
```
🔮 ANTICIPATION: COMPRESSION_BREAKOUT SHORT @ $69218 | conf=59%
   → squeeze: BB width 0.47% (percentile 0)
   → coiling: ADX 18.1 (slope +0.45)
   → volume_decline: Volume ratio 0.12
```

**Problema:** ADX=0.0 reportado en phase=UNKNOWN, posible bug en detección.

### Métricas

- **Trades:** 0
- **Balance:** $254.54 (sin cambios)
- **Ticks evaluados:** 19,560 → 21,360
- **Precio:** $69,130 - $69,250 (rango muy estrecho ~$120)
- **Cooldowns acumulados:** 9 × 5min = 45 min en pausa
- **Intentos fallidos por AttributeError:** 4 más (total: 13)

### Observación Crítica

Este bloque muestra **ADX muy alto (35-46)** en los setups LONG:
```
TREND_DISPLACEMENT (ADX=46.2, conf=70%)
TREND_DISPLACEMENT (ADX=39.2)
EDGE_ACTIVE conf=80%
```

**Estos eran trades de ALTA CALIDAD que el bug bloqueó.**

---

## FIX APLICADO

### Bug: `EntryRegime.TRANSITION` no existe

**Archivo:** `src/domain/market/regime/regime_detector.py` línea 369

**Antes:**
```python
is_low_trend_regime = regime in [EntryRegime.TRANSITION, EntryRegime.RANGE]
```

**Después:**
```python
is_low_trend_regime = regime in [EntryRegime.NORMAL, EntryRegime.RANGE]
```

**Justificación:** El Enum `EntryRegime` solo contiene:
- `TREND_EXPANSION`
- `TREND_EXHAUSTION`
- `NORMAL` ← Este es el equivalente a "TRANSITION" (ADX 18-25)
- `PROBING`
- `RANGE`
- `CHAOS`

---

## PATRONES OBSERVADOS

### 1. Ciclo BIAS FLIP destructivo
```
SHORT invalidado (precio sube $100-200) 
  → FORBID SHORT 
  → LONG detection enabled
  → Precio baja 
  → SHORT restored
  → Precio sube
  → [REPEAT]
```

**Causa:** El buffer de invalidación (~$90) es demasiado pequeño para el ATR actual (~$55-60).

### 2. Penalizaciones acumulativas
```
BB Width: -1.0
BTC Correlation: -1.8
Micro trend: -0.2
Volume divergence: -30% size
Low volume: size_mult=0.30
```

**Resultado:** Incluso con DISPLACEMENT detectado, el score final es muy bajo.

### 3. Edge expira antes de poder entrar
```
EDGE_ACTIVE → intento de trade → AttributeError → Cooldown 5min → EDGE_EXPIRED
```

**El cooldown de 5min tras excepción permite que el edge expire.**

---

## RECOMENDACIONES

### Crítico (P0)
1. ✅ **FIX APLICADO**: Cambiar `TRANSITION` → `NORMAL`

### Urgente (P1)
2. 🔜 **Reducir cooldown por excepción**: De 5min a 30s (es un bug de código, no de mercado)

3. 🔜 **Investigar Market Time Stale**: WebSocket desconecta frecuentemente >2 minutos
   - Archivo: `src/application/services/market_data.py`
   - Añadir reconexión automática si stale > 60s

### Mejoras (P2)
4. 🔜 **Ampliar buffer de invalidación**: Multiplicar ATR×2.5 en vez de ATR×1.5

5. 🔜 **Reducir spam de logs**: Agrupar "ANTICIPATION preserved" cada 1min

6. 🔜 **Revisar penalizaciones BB Width**: En COMPRESSION_BREAKOUT no penalizar

---

## BLOQUE 4 (Líneas 3001-4000)
**Período:** 06:45 - 07:30 UTC (≈45 min)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 1 ocurrencia más | 100% trades bloqueados |
| 2 | Market time stale (128-178s) | 🟡 Alto | Continuo | Datos desincronizados |
| 3 | IMMEDIATE_FLIP detectado | 🟡 Medio | 2 veces | Volatilidad no aprovechada |
| 4 | BTC correlation penalty | 🟠 Medio | Constante -1.8 | Score reducido |

### Secuencia de Fallas

```
06:46:07 - IMMEDIATE_FLIP: SHORT → LONG en 1 tick
         - Prev: LONG invalidated | New: SHORT @ $69257
07:14:37 - DISPLACEMENT LONG detectado
         - conf=70%, Score=0.46
         - Setup: 3/3 checklist
         → AttributeError: EntryRegime.TRANSITION → Cooldown 5min
07:19:37 - Cooldown finalizado
```

### Market Time Stale Continuo

El stale persiste con valores extremos:
```
[WARN] Market time stale by 128s - using real time
[WARN] Market time stale by 148s - using real time  
[WARN] Market time stale by 178s - using real time
```

**Implicación:** WebSocket desconectado por ~3 minutos continuos.

### Métricas

- **Trades:** 0
- **Balance:** $254.54
- **Precio:** $69,200 - $69,400
- **Intentos fallidos:** 1 más (total: 14)

---

## BLOQUE 5 (Líneas 4001-5000)
**Período:** 08:12 - 09:05 UTC (≈53 min)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 6 ocurrencias | 100% trades bloqueados |
| 2 | DISPLACEMENT detectado | 🟡 Alto | $358+ moves | No ejecutado |
| 3 | BEARISH sweep blocking | 🟠 Medio | 4+ veces | LONGs bloqueados |
| 4 | BB WIDTH compression extreme | 🟠 Bajo | 0.45-0.50% | Squeeze perdido |

### Secuencia de Fallas

```
08:18:03 - LONG Setup → AttributeError: EntryRegime.TRANSITION → Cooldown 5min
08:26:10 - LONG Setup → mismo error → Cooldown 5min
08:33:03 - LONG Setup → mismo error → Cooldown 5min
08:39:04 - 🎯 DISPLACEMENT DETECTED: Up $358 ≥ $325 threshold
         - Señal de ALTA CALIDAD ignorada
08:46:04 - LONG Setup → mismo error → Cooldown 5min
08:51:18 - LONG Setup → mismo error → Cooldown 5min
08:56:19 - LONG Setup → mismo error → Cooldown 5min
```

### BEARISH Sweep Pattern

El sistema detectó sweeps bajistas que bloquearon LONGs:
```
08:14:03 - BEARISH sweep detected → LONG blocked
08:16:04 - BEARISH sweep detected → LONG blocked
08:23:45 - BEARISH sweep detected → LONG blocked
08:26:02 - BEARISH sweep detected → LONG blocked
```

### Métricas

- **Trades:** 0
- **Balance:** $254.54
- **Precio:** $69,400 - $69,800
- **Cooldowns:** 6 × 5min = 30 min en pausa
- **Intentos fallidos:** 6 más (total: 20)

---

## BLOQUE 6 (Líneas 5001-6000)
**Período:** 09:49 - 10:39 UTC (≈50 min)

### Problemas Detectados

| # | Problema | Severidad | Frecuencia | Impacto |
|---|----------|-----------|------------|---------|
| 1 | **BUG `EntryRegime.TRANSITION`** | 🔴 CRÍTICO | 8 ocurrencias | 100% trades bloqueados |
| 2 | 🚀 **TREND_EXPANSION 4/4 perdido** | 🔴 CRÍTICO | 5+ veces | Mejores setups ignorados |
| 3 | FORBIDDEN SOFT penalties | 🟡 Alto | FIX #77, #80, #81 | Sizing 21-35% |
| 4 | Price Direction blocks | 🟡 Medio | LONG blocked DOWN 0.40-0.48% | Filtro excesivo |
| 5 | BIAS FLIP loop | 🟠 Medio | 2 invalidaciones | Inestabilidad |

### Secuencia de Fallas

```
09:49:34 - LONG Setup → AttributeError: EntryRegime.TRANSITION → Cooldown 5min
09:55:02 - 🚀 TREND_EXPANSION DETECTED: 4/4 criteria met
         - bb_expansion ✓, atr_expansion ✓, vwap_distance ✓, adx_rising ✓
         - ADX: 51.7 (muy alto)
         → AttributeError → Cooldown 5min
10:00:05 - TREND TRANSITION: RANGE_BOUND → ACCUMULATION
         - BIAS CHANGED: SHORT → LONG
         → AttributeError → Cooldown 5min
10:05:06 - 🚀 TREND_EXPANSION: ADX=52.3
         → AttributeError → Cooldown 5min
10:10:11 - 🚀 TREND_EXPANSION: ADX=53.1
         → AttributeError → Cooldown 5min
10:15:44 - BIAS FLIP: LONG invalidated
         → AttributeError → Cooldown 5min
10:24:05 - Setup → AttributeError → Cooldown 5min
10:29:10 - Setup → AttributeError → Cooldown 5min
10:34:34 - BIAS FLIP: LONG invalidated (2nd time)
```

### 🚀 TREND_EXPANSION Perdido (CRÍTICO)

Este bloque muestra los **mejores setups del día** ignorados por el bug:
```
┌──────────────────────────────────────────────────────────┐
│ 🚀 TREND_EXPANSION DETECTED: 4/4 criteria met            │
│ ├── bb_expansion: True                                   │
│ ├── atr_expansion: True                                  │
│ ├── vwap_distance: True                                  │
│ └── adx_rising: True (ADX: 51.7 → 53.1)                 │
│ 📊 Price: $69,346 → $70,472 (movimiento de $1,126)       │
│ 💰 Ganancia potencial: ~$25-30 USD con el capital actual │
└──────────────────────────────────────────────────────────┘
```

**Con 10x leverage sobre $254.54 = $2,545.40 exposición**  
**Movimiento 1.62% = ~$41 potencial perdido**

### FORBIDDEN SOFT Penalties

Nuevos fixes reducen sizing agresivamente:
```
FIX #77: Reduce size to 60%
FIX #80: KILL SOFT reduces to 35%  
FIX #81: Final size 21-30%
```

**Problema:** Incluso si el bug no existiera, el sizing sería muy pequeño.

### Price Direction Blocks

```
LONG blocked: Price DOWN 0.40% in last period
LONG blocked: Price DOWN 0.48% in last period
```

**Problema:** Filtro demasiado conservador para DISPLACEMENT.

### Métricas

- **Trades:** 0
- **Balance:** $254.54
- **Precio:** $69,346 - $70,472 (rango amplio $1,126)
- **ADX máximo:** 53.1 (trend muy fuerte)
- **Cooldowns:** 8 × 5min = 40 min en pausa
- **Intentos fallidos:** 8 más (total: 28)

---

## RESUMEN BLOQUES 1-6

| Bloque | Período | Errores TRANSITION | Precio | Observación |
|--------|---------|-------------------|--------|-------------|
| 1 | 21:25-00:56 | 4 | $69,000-$69,700 | Primeros errores |
| 2 | 01:01-05:05 | 5 | $68,935-$69,653 | DISPLACEMENT perdidos |
| 3 | 05:23-06:31 | 4 | $69,130-$69,250 | Market stale masivo |
| 4 | 06:45-07:30 | 1 | $69,200-$69,400 | Stale continuo |
| 5 | 08:12-09:05 | 6 | $69,400-$69,800 | BEARISH sweep blocks |
| 6 | 09:49-10:39 | 8 | $69,346-$70,472 | **TREND_EXPANSION perdido** |
| **TOTAL** | **13+ horas** | **28** | **$68,935-$70,472** | **0 trades ejecutados** |

### Pérdida Estimada por Bug

El movimiento de $68,935 a $70,472 representa **+2.23%**

Con posición de $2,545.40 (10x leverage):
- **Capturando 50% del movimiento:** +$28.40
- **Capturando 100% del movimiento:** +$56.80

**El bug costó potencialmente $28-57 USD en este período.**

7. 🔜 **Fix COMPRESSION_BREAKOUT ADX=0.0**: El log muestra phase=UNKNOWN con ADX=0 - posible bug

---

## PRÓXIMOS ANÁLISIS

- [x] Bloque 1: Líneas 1-1000 ✅
- [x] Bloque 2: Líneas 1001-2000 ✅
- [x] Bloque 3: Líneas 2001-3000 ✅
- [ ] Bloque 4: Líneas 3001-4000
- [ ] Bloque 5: Líneas 4001-5000
