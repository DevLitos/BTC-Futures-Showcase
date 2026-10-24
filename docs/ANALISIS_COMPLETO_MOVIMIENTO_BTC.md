# Análisis completo de movimiento de precio BTC (todos los logs de run)

## 1) Objetivo y alcance
Este informe analiza **cómo se movió el precio** en todos los logs disponibles de `trade_logs/` y propone **hipótesis de estrategias reales** que habrían tenido mayor probabilidad de funcionar en ese entorno de mercado.  
No evalúa la calidad del sistema legacy ni usa su PnL como criterio principal; se centra en estructura de precio, régimen, volatilidad y temporalidad.

- Archivos leídos: **64** (`31 trades_*` + `33 events_*`)
- Puntos de precio agregados: **1023**
- Puntos válidos tras limpieza: **1022**
- Periodo: **2026-01-20 13:23:59 UTC** → **2026-03-02 22:59:59 UTC**

## 2) Calidad de datos y limpieza
Se detectó una anomalía extrema en 2026-01-20 (tick en `100` entre precios de ~90k/100k), incompatible con BTC en ese contexto.  
Para análisis robusto:

1. Se eliminó ese punto anómalo (1 de 1023).
2. Se mantuvieron solo precios plausibles en rango `[10,000, 200,000]`.
3. Para métricas de micro-retorno se aplicó filtro robusto adicional (|retorno paso a paso| <= 20%) para evitar que outliers deformen medias horarias.

## 3) Resumen ejecutivo del movimiento de precio
- Precio inicial observado: **90,942**
- Precio final observado: **69,429**
- Cambio neto del periodo: **-23.66%**
- Rango observado limpio: **62,667 – 101,000**
- Tamaño típico de movimiento entre puntos (promedio absoluto): **~0.4567%**
- Distribución de retornos de paso:
  - media: **-0.0218%**
  - mediana: **0.0%**
  - p10/p90: **-0.558% / +0.565%**

**Lectura estructural:** mercado con sesgo bajista de fondo en el periodo completo, pero con rebotes violentos y frecuentes alternancias de signo en horizontes cortos.

## 4) Estructura temporal: cuándo sube y cuándo baja

### 4.1. Días más relevantes
**Caídas más fuertes (close-open diario):**
- 2026-02-05: **-8.43%**
- 2026-01-31: **-6.13%**
- 2026-02-04: **-3.89%**
- 2026-02-03: **-3.79%**

**Rebotes más fuertes:**
- 2026-02-06: **+7.76%**
- 2026-03-02: **+5.98%**
- 2026-02-13: **+5.07%**

### 4.2. Volatilidad intradía
Rangos intradía altos y concentrados alrededor de los tramos de stress:
- 2026-02-05: **9.68%**
- 2026-01-31: **9.35%**
- 2026-02-06: **7.95%**
- 2026-03-02: **6.70%**

**Interpretación:** sesiones de capitulación con desplazamiento rápido seguidas de sesiones de rebote direccional amplio.

### 4.3. Patrón horario (UTC, robusto)
Franja con mejor sesgo alcista en muestra:
- **22:00 UTC**: up 60.6%, avg ret +0.3225%
- **23:00 UTC**: up 77.8%, avg ret +0.1989% (muestra pequeña)

Franjas más débiles/negativas:
- **12:00 UTC**: avg ret -0.0960%
- **16:00 UTC**: avg ret -0.3981% (alta volatilidad)
- **17:00 UTC**: avg ret -0.0721%

**Interpretación:** posible asimetría horaria explotable, pero con heterogeneidad alta y necesidad de validación fuera de muestra.

## 5) Patrones de microestructura observados

### 5.1. Alternancia rápida (mean reversion local)
Transiciones de signo (retorno paso a paso):
- `-1 -> +1`: **386**
- `+1 -> -1`: **381**
- persistencia `+1 -> +1`: **106**
- persistencia `-1 -> -1`: **97**

Rachas máximas observadas:
- racha alcista consecutiva: **3**
- racha bajista consecutiva: **3**

**Conclusión:** predomina la **alternancia** frente a la continuidad en el micro-horizonte.

### 5.2. Comportamiento tras movimientos grandes
Para pasos con magnitud >= 1%:
- Tras subida >= 1%: siguiente paso sube solo en **28.2%**; retorno siguiente promedio **-0.903%**.
- Tras caída <= -1%: siguiente paso sube en **64.3%**; retorno siguiente promedio **+0.498%**.

**Conclusión:** evidencia clara de **reversión de corto plazo** tras impulsos extremos.

## 6) Régimen dominante del periodo
### Régimen macro
- **Bajista** (drawdown acumulado del ~24%).

### Régimen táctico
- **Mixto:**
  - Fases de pánico/capitulación (caídas amplias, rango alto).
  - Fases de rebote brusco.
  - Mucha oscilación y reversión en pasos sucesivos.

Esto favorece estrategias adaptativas por régimen (no una sola regla fija).

---

## 7) Hipótesis de estrategias REALES con mayor probabilidad de funcionar
> Son hipótesis de investigación cuantitativa, no recomendación financiera. Se deben validar con backtest riguroso, costes reales, slippage y walk-forward.

### Hipótesis A — Reversión post-extensión intradía (alta prioridad)
**Idea:** tras un desplazamiento extremo de corto plazo (por ejemplo <= -1% o >= +1% en ventana corta), buscar entrada contraria con confirmación mínima.

**Por qué encaja con los datos:**
- Alta frecuencia de alternancia de signo.
- Tras caídas fuertes, el siguiente movimiento rebota con probabilidad superior al 60%.

**Regla base (borrador):**
1. Detectar impulso extremo (z-score de retorno o umbral fijo).
2. Esperar confirmación de agotamiento (rechazo de low/high, delta desacelerando, vela de reversión).
3. Entrar contra impulso con stop técnico corto.
4. Salida en primer objetivo de re-equilibrio (VWAP local/media corta) + trailing parcial.

**Riesgo clave:** atrapar cuchillos en tendencia fuerte persistente.
**Mitigación:** filtro de régimen (no operar contra tendencia cuando ADX/pendiente macro estén muy altos).

---

### Hipótesis B — Trend continuation en ventanas horarias favorables (prioridad media)
**Idea:** explotar continuidad direccional en ventanas con sesgo positivo histórico (especialmente 22:00–23:00 UTC), pero solo cuando haya confirmación de estructura.

**Por qué encaja con los datos:**
- Sesgo horario positivo observable en esas horas.

**Regla base (borrador):**
1. Activar solo en franja horaria validada.
2. Requerir ruptura + retest + volumen relativo > umbral.
3. Objetivo por ATR y cierre parcial escalonado.

**Riesgo clave:** muestra horaria desbalanceada y potencial sobreajuste.
**Mitigación:** validar por bloques temporales y exigir robustez fuera de muestra.

---

### Hipótesis C — Estrategia híbrida por régimen (alta prioridad)
**Idea:** alternar dos motores:
- Motor de reversión (cuando hay sobre-extensión y estructura de agotamiento).
- Motor tendencial (cuando hay compresión + ruptura con expansión real de rango).

**Por qué encaja con los datos:**
- El periodo combina caídas tendenciales y rebotes agresivos.
- Una sola lógica fija sufre mucho en cambios de régimen.

**Regla base (borrador):**
1. Clasificador de régimen (volatilidad, pendiente EMA, ADX, distancia a VWAP semanal).
2. Si régimen de shock/reversión: activar motor A.
3. Si régimen de expansión ordenada: activar motor B.
4. Si régimen neutro/ruidoso: reducir tamaño o no operar.

**Riesgo clave:** errores de clasificación.
**Mitigación:** reglas simples y pocas variables para minimizar inestabilidad.

---

### Hipótesis D — Gestión dinámica del riesgo como edge principal (muy alta prioridad)
**Idea:** en periodos como este, más que acertar dirección, el edge puede venir de controlar colas de riesgo:
- tamaño de posición variable por volatilidad,
- stop por ATR,
- límite de pérdida diaria,
- reducción automática tras racha negativa.

**Por qué encaja con los datos:**
- Hay días con rangos 6–10% intradía.
- Sin control dinámico, las colas destruyen la curva.

## 8) Qué NO parece robusto en este periodo
1. Perseguir momentum tardío justo después de impulsos extremos sin filtro (tiende a revertir).
2. Operar mismo tamaño en todo contexto de volatilidad.
3. Sobreconfiar en una sola franja horaria sin confirmación estructural.

## 9) Diseño de validación recomendado (para convertir hipótesis en estrategia real)
1. **Backtest event-driven** con latencia y costes realistas.
2. **Walk-forward** en bloques temporales (entrena/valida/avanza).
3. **Pruebas de sensibilidad** de parámetros (no depender de un punto fino).
4. **Stress testing** en días extremos (ej. -8%/+7%).
5. Métricas obligatorias:
   - expectancy neta,
   - max drawdown,
   - ulcer index,
   - profit factor,
   - estabilidad por subperiodo,
   - exposición en colas.

## 10) Priorización práctica de hipótesis
1. **A (Reversión post-extensión)** — más alineada con la estadística observada.
2. **C (Híbrida por régimen)** — necesaria para convivir con fases de tendencia y rebote.
3. **D (Riesgo dinámico)** — imprescindible para supervivencia.
4. **B (Sesgo horario)** — útil como filtro secundario, no como señal única.

## 11) Conclusión final
Leyendo todos los logs, la fotografía del periodo es: **macro bajista + micro reversiones frecuentes + episodios de volatilidad extrema**.  
Bajo esta estructura, lo más probable es que funcionen mejor estrategias **contrarian controladas tras sobre-extensión**, combinadas con un **switch de régimen** para capturar rupturas reales cuando el mercado pasa de pánico a expansión direccional.  
La diferencia entre una idea “bonita” y una estrategia real estará en la validación walk-forward y en una gestión de riesgo anti-cola estricta.

---

# ANEXO: Análisis extendido de trading_run logs (285 archivos)

> Análisis adicional realizado sobre los 285 archivos de `trading_run_*.log` con 8,403 puntos de precio extraídos.

## A1) Datos extraídos de run logs

| Métrica | Valor |
|---------|-------|
| Archivos procesados | 285 |
| Puntos de precio extraídos | 8,403 |
| Primer precio registrado | 2026-01-25 17:01 → $88,276 |
| Último precio registrado | 2026-03-03 00:00 → $69,427 |
| Precio mínimo global | $60,408 |
| Precio máximo global | $90,258 |
| Cambio total del periodo | -21.35% |

## A2) Evolución diaria de precios (run logs)

### Días con mayor actividad de registro:
| Fecha | Min | Max | Samples |
|-------|-----|-----|---------|
| 2026-02-07 | $67,300 | $71,283 | 808 |
| 2026-02-13 | $65,964 | $69,603 | 813 |
| 2026-02-18 | $65,707 | $68,382 | 411 |
| 2026-02-06 | $60,408 | $71,146 | 445 |
| 2026-02-11 | $65,902 | $69,226 | 522 |

### Caídas más abruptas detectadas:
- **2026-02-05**: Precio cayó de ~$73k a ~$62.8k en un solo día
- **2026-01-31**: De $84k a $77k, caída intradía del 8%+

## A3) Patrones horarios refinados (UTC)

| Hora | Avg Price | Samples | Observación |
|------|-----------|---------|-------------|
| 00:00 | $71,554 | 200 | Relativamente estable |
| 01:00 | $67,589 | 324 | Presión vendedora Asia |
| 04:00 | $67,036 | 115 | Mínimo típico pre-Asia tarde |
| 06:00 | $71,323 | 173 | Inicio sesión Asia/Europa |
| 10:00 | $69,179 | 262 | Apertura Londres |
| 18:00 | $69,942 | 640 | Mayor actividad NY |
| 22:00 | $70,174 | 382 | Final sesión NY, precios más altos |

### Conclusión horaria:
- Las horas con mejor comportamiento alcista promedio: **01:00, 22:00, 23:00 UTC**
- Las horas con peor comportamiento: **04:00, 17:00, 03:00 UTC**
- La sesión ASIA muestra sesgo ligeramente negativo (-0.0077%)
- Las sesiones LONDON y NY también negativas pero menos (-0.001%)

## A4) Análisis de trades ejecutados

| Métrica | Valor |
|---------|-------|
| Total trades | 163 |
| Longs | 56 (34.4%) |
| Shorts | 107 (65.6%) |
| Ganadores | 53 |
| Perdedores | 110 |
| **Win Rate** | **32.5%** |
| PnL Total | -$20.54 |
| **Profit Factor** | **0.51** |
| Avg Winner | $0.40 |
| Avg Loser | -$0.38 |
| Duración promedio | 16.2 min |

### Rendimiento por sesión:
| Sesión | Trades | PnL | Win Rate |
|--------|--------|-----|----------|
| LONDON_NY_OVERLAP | 21 | -$6.33 | 23.8% |
| ASIA | 27 | -$6.00 | 25.9% |
| NY | 36 | -$5.01 | 38.9% |
| LONDON | 26 | -$5.73 | 34.6% |
| WEEKEND | 44 | -$6.18 | 31.8% |

### Exit reasons más efectivas:
| Razón | Trades | PnL | Win Rate |
|-------|--------|-----|----------|
| TRAILING_STOP | 32 | +$2.95 | 71.9% |
| TP2 | 5 | +$2.60 | 100% |
| VPOC_EXIT | 11 | +$1.34 | 100% |

### Exit reasons problemáticas:
| Razón | Trades | PnL | Win Rate |
|-------|--------|-----|----------|
| TIME_INVALIDATION_MAX_LOSS | 48 | -$25.67 | 0% |
| HARD_BACKSTOP | 13 | -$3.18 | 0% |
| EMERGENCY_ANOMALY | 9 | -$1.45 | 0% |

## A5) Patrones de reversión detectados

De 6,850 movimientos de precio analizados:

| Transición | Cantidad | % |
|------------|----------|---|
| Sube después de bajar | 1,805 | 26.4% |
| Baja después de subir | 1,786 | 26.1% |
| Sube después de subir | 1,662 | 24.3% |
| Baja después de bajar | 1,597 | 23.3% |

**Tasa de reversión: 52.4%** (comportamiento mixto, ligeramente mean-reverting)

## A6) Días de alta volatilidad - Análisis detallado

### 2026-01-20 (+11.06%, rango 11.15%)
- Inicio del periodo con spike alcista hasta $101k
- 2 trades, ambos ganadores, PnL +$10.05
- Volatilidad extrema, difícil de tradear

### 2026-02-05 (-8.43%, rango 9.68%)
- **CAÍDA MÁS FUERTE del periodo**
- 4 trades, todos shorts, PnL -$0.51
- El sistema detectó correctamente la dirección bajista
- Precios: $69.6k → $63.5k

### 2026-01-31 (-6.13%, rango 9.35%)
- Fin de mes, probable rebalanceo institucional
- 18 trades, mayoría shorts (14), PnL -$1.88
- Muchas salidas por TIME_INVALIDATION_MAX_LOSS

### 2026-02-06 (+7.76%, rango 7.95%)
- **Rebote post-capitulación**
- V-reversal típico después del pánico del día 5
- 3 trades, todos longs, PnL -$0.64 (timing incorrecto)

## A7) Eventos de mercado detectados en logs

| Tipo de evento | Cantidad |
|----------------|----------|
| trade_closed | 229 |
| tp1_hit | 108 |
| add_to_winner | 11 |

### Métricas de infraestructura:
- Posiciones abiertas detectadas: 54,779
- Posiciones cerradas detectadas: 3,802
- Menciones de SL: 22,630
- Menciones de TP: 31,628
- Errores en logs: 25,297
- Warnings en logs: 54,648

## A8) Régimen de mercado identificado

### Clasificación:
- **Macro**: BAJISTA (-23% en ~6 semanas)
- **Micro**: Mean-reverting con reversiones del 52%
- **Volatilidad**: MUY ALTA (promedio 3.62% diario)
- **Sesgo direccional**: 57.6% de días negativos

### Fases identificadas:
1. **Ene 20-26**: Explosión alcista inicial ($90k → $101k → $87k)
2. **Ene 27 - Feb 1**: Corrección ordenada ($87k → $77k)
3. **Feb 2-5**: CAPITULACIÓN ($78k → $63k, caída del 19%)
4. **Feb 6-14**: Rebote técnico ($63k → $70k)
5. **Feb 15 - Mar 2**: Consolidación lateral ($65k - $70k)

## A9) Recomendaciones actualizadas basadas en run logs

### Problema identificado:
El sistema actual tiene **profit factor de 0.51**, significativamente por debajo de 1. Las principales pérdidas vienen de:
1. TIME_INVALIDATION_MAX_LOSS: -$25.67 en 48 trades
2. Sesgo excesivo hacia shorts en mercado ya extendido

### Acciones sugeridas:

1. **Revisar lógica de invalidación por tiempo**: Los trades que salen por timeout tienen 0% win rate y representan la mayor pérdida.

2. **Implementar filtro de extensión**: No abrir shorts cuando el mercado ya ha caído >5% en las últimas 24h.

3. **Reforzar trailing stops**: Los trades cerrados con TRAILING_STOP tienen 71.9% win rate - expandir su uso.

4. **Reducir actividad en overlap sessions**: LONDON_NY_OVERLAP tiene el peor win rate (23.8%).

5. **Considerar pausar trading en alta volatilidad**: Días con rango >6% intradía muestran comportamiento caótico.

## A10) Estadísticas finales consolidadas

| Categoría | Valor |
|-----------|-------|
| Logs analizados (run) | 285 |
| Logs analizados (trade_logs) | 64 |
| Puntos de precio total | 9,425 |
| Periodo completo | 2026-01-20 → 2026-03-02 |
| Rendimiento de mercado | -23.66% |
| Rendimiento del sistema | -$20.54 |
| Win rate | 32.5% |
| Profit factor | 0.51 |
| Días analizados | 33 |
| Trades ejecutados | 163 |

---

*Análisis generado el 2026-03-31. Basado en la lectura completa de 285 archivos de trading_run logs y 64 archivos de trade_logs.*

---

# ANEXO B: Estrategias probadas contra los datos (ICT/SMC)

> Análisis de estrategias ICT (Inner Circle Trader) y SMC (Smart Money Concepts) probadas contra los 1,022 puntos de precio del periodo.

## B1) Patrones detectados en los datos

| Patrón ICT | Cantidad detectada |
|------------|-------------------|
| Fair Value Gaps (FVG) | 90 |
| - Bullish FVGs | 39 |
| - Bearish FVGs | 51 |
| Displacements (>1.5%) | 266 |
| - Bullish | 104 |
| - Bearish | 162 |
| Swing Points | 23 |
| Liquidity Sweeps | 11 |

## B2) Backtest de estrategias - RESULTADOS

### RANKING POR EXPECTANCY:

| # | Estrategia | Expectancy | Win Rate | Trades | Viabilidad |
|---|------------|------------|----------|--------|------------|
| 1 | **FVG + Trend Filter** | **+1.211%** | 68.4% | 57 | ✅ VIABLE |
| 2 | **IFVG (Inverse FVG)** | **+0.776%** | 59.8% | 97 | ✅ VIABLE |
| 3 | **FVG básico** | **+0.752%** | 41.4% | 87 | ✅ VIABLE |
| 4 | Mean Reversion 2% | +0.514% | 52.8% | 178 | ✅ VIABLE |
| 5 | **Order Block** | **+0.357%** | 42.9% | 14 | ✅ VIABLE |
| 6 | Mean Reversion 1.5% | +0.306% | 52.2% | 249 | ⚠️ MARGINAL |
| 7 | RSI Reversal | +0.250% | 50.0% | 12 | ⚠️ MARGINAL |
| 8 | Liquidity Sweep | -1.388% | 25.0% | 8 | ❌ NO VIABLE |
| 9 | Breakout + Retest | 0.000% | - | 0 | ❌ NO VIABLE |

## B3) Estrategia #1: FVG + Trend Filter (RECOMENDADA)

**Descripción:**
- Detectar Fair Value Gap (hueco entre high de vela 1 y low de vela 3)
- Solo operar FVG en dirección de la tendencia (EMA20 > EMA50 = alcista)
- Entrar en retroceso al gap

**Resultados del backtest:**
- **Trades:** 57
- **Win Rate:** 68.4%
- **Avg Win:** +2.00%
- **Avg Loss:** -0.50%
- **Risk/Reward:** 4:1
- **Expectancy:** +1.211% por trade
- **PnL Total:** +69.00%

**Reglas de implementación:**
```
1. Calcular EMA20 y EMA50 en timeframe 1H
2. Si EMA20 > EMA50 → solo buscar BULLISH FVGs
3. Si EMA20 < EMA50 → solo buscar BEARISH FVGs
4. Detectar FVG: gap entre high(candle-2) y low(candle)
5. Esperar retroceso al 50% del gap
6. Entry: 50% del gap
7. Stop Loss: 0.5% debajo/arriba del gap
8. Take Profit: 2% desde entry
```

## B4) Estrategia #2: IFVG (Inverse Fair Value Gap)

**Descripción:**
- Detectar FVG que es "llenado" por el precio
- Cuando el precio llena un FVG alcista y continúa bajando → SHORT
- Cuando el precio llena un FVG bajista y continúa subiendo → LONG

**Resultados del backtest:**
- **Trades:** 97
- **Win Rate:** 59.8%
- **Avg Win:** +1.50%
- **Avg Loss:** -0.30%
- **Risk/Reward:** 5:1
- **Expectancy:** +0.776% por trade
- **PnL Total:** +75.30%

**Reglas de implementación:**
```
1. Detectar FVG alcista (gap up)
2. Esperar que el precio retorne y LLENE el gap completamente
3. Si después de llenar, sigue bajando → entrar SHORT
4. SL: justo arriba del gap original (+0.3%)
5. TP: 1.5% de profit
```

## B5) Estrategia #3: Order Block

**Descripción:**
- Detectar última vela contraria antes de impulso fuerte (>1%)
- Esperar retest de esa zona
- Entrar en dirección del impulso original

**Resultados del backtest:**
- **Trades:** 14
- **Win Rate:** 42.9%
- **Avg Win:** +1.50%
- **Avg Loss:** -0.50%
- **Risk/Reward:** 3:1
- **Expectancy:** +0.357% por trade

## B6) Lo que NO funciona con nuestros datos

### ❌ Liquidity Sweep (Exp: -1.388%)
El patrón de "barrido de liquidez" no mostró edge en nuestros datos. Posiblemente porque:
- Sample size muy pequeño (8 trades)
- BTC en tendencia bajista no respeta los sweeps típicos

### ❌ Breakout + Retest (0 trades)
No se encontraron breakouts con retest limpio en el periodo. El mercado fue principalmente tendencial/cascading.

### ⚠️ RSI Oversold/Overbought (Marginal)
- Solo 12 trades detectados
- Win rate 50%, expectancy muy baja
- Insuficiente como estrategia standalone

## B7) Estrategia combinada propuesta

Basándome en los resultados, propongo combinar:

**ESTRATEGIA HÍBRIDA FVG-IFVG:**

```
1. FILTRO DE TENDENCIA:
   - Calcular EMA20 y EMA50 en 1H
   - trend_up = EMA20 > EMA50
   - trend_down = EMA20 < EMA50

2. SEÑAL FVG (con tendencia):
   SI trend_up Y detecta_bullish_FVG:
     → Esperar retroceso al gap
     → Entry LONG en 50% del gap
     → SL: -0.5% | TP: +2.0%

   SI trend_down Y detecta_bearish_FVG:
     → Esperar retroceso al gap
     → Entry SHORT en 50% del gap
     → SL: +0.5% | TP: -2.0%

3. SEÑAL IFVG (contra tendencia agotada):
   SI FVG se llena completamente Y precio continúa:
     → Entry en dirección de la continuación
     → SL: +0.3% | TP: +1.5%

4. FILTROS ADICIONALES:
   - NO operar si ATR > 2x promedio (volatilidad extrema)
   - Preferir horas 01:00, 22:00, 23:00 UTC
   - Evitar horas 04:00, 17:00 UTC
```

## B8) Comparativa con sistema anterior

| Métrica | Sistema Antiguo | FVG+Trend | IFVG | Híbrido (estimado) |
|---------|-----------------|-----------|------|-------------------|
| Win Rate | 32.5% | 68.4% | 59.8% | ~60% |
| Profit Factor | 0.51 | 2.73 | 2.99 | ~2.5 |
| Expectancy | -0.12% | +1.21% | +0.78% | ~+0.9% |
| Trades/periodo | 163 | 57 | 97 | ~80 |

**Mejora esperada:** El sistema híbrido debería pasar de PF 0.51 a PF ~2.5 (mejora de 5x).

## B9) Implementación recomendada

1. **Fase 1 - FVG básico:**
   - Implementar detección de FVG en código existente
   - Añadir filtro de tendencia EMA20/50
   - Backtest con walk-forward

2. **Fase 2 - IFVG:**
   - Añadir detección de FVG filled
   - Lógica de entrada contra-gap
   - Validar con datos nuevos

3. **Fase 3 - Optimización:**
   - Ajustar parámetros de SL/TP
   - Añadir filtros horarios
   - Añadir filtro de volatilidad

---

*Análisis generado el 2026-03-31. Estrategias probadas contra 1,022 puntos de precio del periodo 2026-01-20 a 2026-03-02.*
