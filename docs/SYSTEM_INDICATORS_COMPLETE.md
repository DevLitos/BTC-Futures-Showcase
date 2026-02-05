# 🔬 BTC SCALPER - INDICADORES Y LÓGICA COMPLETA

> Documento técnico: todos los indicadores, detección de tendencia, edge y entrada

---

## 1. DETECCIÓN DE TENDENCIA

### 1.1 Macro Trend (EMA)

**Archivo**: `src/domain/market/filters/macro_trend_filter.py`

| Parámetro | Valor | Timeframe |
|-----------|-------|-----------|
| EMA Fast | 20 | 15m, 1h |
| EMA Slow | 50 | 15m, 1h |
| Slope Lookback 15m | 3 velas = 45 min |
| Slope Lookback 1h | 2 velas = 2h |

**Cálculo de Slope**:
```
slope = (EMA_actual - EMA_hace_N_velas) / EMA_hace_N_velas * 100
```

**Umbrales de Slope**:
| Nivel | Umbral | Acción |
|-------|--------|--------|
| EXTREME | > 0.50% | Block (solo TREND mode) |
| MODERATE | 0.20-0.50% | Penalty -1.0 |
| MILD | 0.10-0.20% | Penalty -0.5 |
| MINIMAL | < 0.10% | Permitido |

**Size Caps contra macro**:
- TREND mode: 60%
- RANGE mode: 40%
- BREAKOUT mode: 50%

**Bypasses**:
- `displacement_bypass` → 50% size
- `anticipation_bypass` (conf ≥70%) → 60% size

---

### 1.2 Micro Trend (Estructura 5m)

**Archivo**: `src/domain/market/filters/micro_trend_filter.py`

**Indicadores**:
| Indicador | Período | Uso |
|-----------|---------|-----|
| EMA20 5m | 20 | Fast EMA |
| EMA50 5m | 50 | Slow EMA |
| RSI | 14 | Momentum |
| Swing Pivots | 3L/2R | Estructura HH/HL/LH/LL |

**Pesos de Señales** (normalizados a 1.0):
| Señal | Peso | Descripción |
|-------|------|-------------|
| ema_cross | 3.0 | EMA20 vs EMA50 |
| ema_slope_alignment | 2.5 | Ambas EMAs misma dirección |
| swing_lows | 2.0 | LL vs HL |
| swing_highs | 1.5 | LH vs HH |
| price_vs_ema50 | 1.5 | Precio relativo a EMA50 |
| ltf_confirmation | 2.0 | 1m confirma dirección |
| rsi_slope | 1.5 | Dirección RSI |
| price_vs_ema20 | 1.0 | Precio relativo a EMA20 |
| rsi_zone | 1.0 | RSI en zona bull/bear |

**Zonas RSI**:
- Bull: RSI > 60
- Bear: RSI < 40
- Oversold: RSI < 30
- Overbought: RSI > 70

**Output**:
- Estado: `BULLISH` | `BEARISH` | `NEUTRAL`
- Strength: 0.0 a 1.0
- Histéresis: 4 barras para HARD BLOCK, 6 para clearear

---

### 1.3 ADX Regímenes

**Archivo**: `src/shared/utils/adx_thresholds.py`

| Régimen | ADX | Comportamiento |
|---------|-----|----------------|
| RANGE | < 18 | Solo mean-reversion |
| TRANSITION | 18-20 | Cautela, size reducido |
| TREND | ≥ 20 | Trend-following activo |
| STRONG_TREND | ≥ 25 | Full conviction |

---

## 2. EDGE STATE MACHINE

**Archivo**: `src/domain/market/structure/edge_state_manager.py`

### Estados:
```
NO_EDGE (○) → EDGE_ARMED (◐) → EDGE_PROVISIONAL (◑) → EDGE_ACTIVE (●) → EDGE_EXPIRED (◌)
```

### Configuración:
| Parámetro | Valor |
|-----------|-------|
| Armed TTL | 180s |
| Active TTL | 300s |
| Max trades por edge | 2 |
| Confidence mínima | 0.40 |

---

## 3. TIPOS DE EDGE

### 3.1 DISPLACEMENT

**Archivo**: `src/application/live/trading_app.py`

**Detección**:
```
displacement_ratio = abs(candle_range) / ATR_5m
THRESHOLD = 1.5x ATR
```

**Zonas de Entrada**:
| Zona | Barras | Acción | Size |
|------|--------|--------|------|
| ZONA 1 (FAST) | 0-1 | Entrada directa | 25-40% |
| ZONA 2 (PULLBACK) | 2-5 | Entrada en pullback | 40-60% |
| ZONA 3 | >5 | NO TRADE | - |

**Validación Pullback**:
- Mín: 15% del displacement
- Máx: 45% del displacement
- Continuación: 10% = EDGE_ACTIVE

---

### 3.2 BREAKOUT

**Archivo**: `src/domain/market/structure/breakout_edge.py`

**Activación**:
- BB Width < percentil 10 (squeeze extremo)
- ADX > 18 (hay direccionalidad)
- Score breakout ≥ 2

**Confirmación** (cualquiera):
1. Rotura de nivel estructural
2. Cierre fuera de Bollinger Bands

**Targets**: 1R, 2R (con runners)

---

### 3.3 RANGE

**Archivo**: `src/domain/market/structure/range_edge.py`

**Activación**:
- Percentil ≥ 85% → SHORT edge
- Percentil ≤ 15% → LONG edge

**Requisito SWEEP + REJECTION**:
- Sweep: Penetración 0.1-0.4 ATR del swing
- Rejection: Cierre lado original en ≤3 velas
- Size: 30%

**Modo Probing** (cerca de VWAP/VPOC):
- Size: 25%
- TP: 0.3R
- SL extendido: 1.2x
- Max trades: 3

---

### 3.4 TREND (Consensus)

**Dos caminos**:
| Path | Requisito | Size |
|------|-----------|------|
| BOS_PULLBACK | BOS + pullback | 60-80% |
| CONSENSUS_DIRECT | Solo alineación | 40-60% |

---

### 3.5 INTRADAY

**Archivo**: `src/domain/market/structure/intraday_edge.py`

**Requisitos Base**:
- ADX 5m ≥ 25
- SL ≤ 0.5R

**R-Bucket**:
| R | Clasificación |
|---|---------------|
| < 0.30 | LOW |
| 0.30-0.50 | MEDIUM |
| ≥ 0.50 | HIGH |

**Time Invalidation**:
- R < 0.3: 2 min
- R ≥ 0.3: 5 min

**Sizing**:
- Base: 20%
- Con liquidity boost: 40%
- Max: 50%

---

### 3.6 PROVISIONAL

**Archivo**: `src/domain/market/structure/provisional_edge.py`

**Propósito**: Starter position - entrada barata para testear dirección

**Criterios (relajados)**:
- Score ≥ 0.6 (no ≥1)
- R ≥ 0.25 (no 0.30)

**Sizing**: 30% (35% si ADX > 35)

**Kill Switch**:
- PnL < -0.3% → sale
- Drawdown > 0.5% → sale

---

## 4. DIRECTION ORCHESTRATOR

**Archivo**: `src/application/live/direction_orchestrator.py`

### Prioridad de Señales:
| Prioridad | Fuente | Bypass Veto |
|-----------|--------|-------------|
| 100 | EXPANSION_MODE | ✅ |
| 95 | EDGE_BREAKOUT | ✅ |
| 90 | DISPLACEMENT | ❌ |
| 85 | ANTICIPATION (≥70% conf) | ✅ |
| 80 | FAST_REVERSAL | ❌ |
| 70 | MICRO_MOMENTUM | ❌ |
| 60 | STRUCTURAL_BIAS | ❌ |
| 50 | FORBIDDEN_FALLBACK | ❌ |

### Bias Inversion:
- Origen: `micro_trend_vs_edge` cuando micro trend contradice edge
- Bloqueado si: location extreme (< 5% o > 95% percentil)

---

## 5. PIPELINE DE FILTROS

**Archivo**: `src/domain/market/filters/filter_pipeline.py`

### Orden de Evaluación:
1. **Volatility Shock** - TrueRange vs ATR
2. **BB Width** - Ancho de Bollinger
3. **Macro Trend** - EMA alignment
4. **Post-Sweep** - Break+retest después de sweep
5. **Cost** - Spread vs risk
6. **Overtrade** - Prevención misma zona

---

### 5.1 Volatility Shock

| TF | MODERATE | ELEVATED | SEVERE | EXTREME |
|----|----------|----------|--------|---------|
| 1m | 3.0x | 3.8x | 4.5x | 6.0x |
| 5m | 2.5x | 3.2x | 4.0x | 5.5x |
| 15m | 2.2x | 2.8x | 3.5x | 5.0x |

**Acciones**:
- MODERATE: Penalty -0.5
- ELEVATED: Penalty -1.0
- SEVERE: BLOCK + cooldown 3-5 min
- EXTREME: BLOCK + cooldown 6-10 min

---

### 5.2 BB Width

| Umbral | Valor | Acción |
|--------|-------|--------|
| MIN | 0.5% | BLOCK si < (squeeze extremo) |
| NARROW | 0.8% | Penalty -1.0 |
| HIGH_VOL | 1.2% | Penalty -0.5 |
| EXTREME | 2.5% | Caución |

**Bollinger**: 20 períodos, 2 std dev

---

## 6. ENTRY GUARDS

**Archivo**: `src/application/live/entry_orchestrator.py`

### Secuencia:
1. Concurrency (pending orders, open positions)
2. Edge enforcement
3. **Location Guards** (anchor, range)
4. ADX coherence
5. POST_ONLY rejection tracking
6. TP/RR sanity
7. Funding rate
8. Spread
9. **SHORT Obstinacy** - 3+ señales bullish bloquea SHORT
10. **Displacement Extreme** - Block si > 1.5x displacement
11. **Location Extreme** - Block en percentil 10/90

---

### 6.1 Location Extreme

| Dirección | Percentil | Acción |
|-----------|-----------|--------|
| SHORT | ≤ 10% | BLOCK |
| LONG | ≥ 90% | BLOCK |

**Override Breakout** (permite tradear):
- `breakout_score ≥ 3`
- `volume_expansion = True`

---

### 6.2 ADX Coherence

**Block si**:
```
HTF_ADX > 20 AND LTF_ADX < 10 AND NOT displacement_bypass
```

---

### 6.3 SHORT Obstinacy

**Threshold**: 3 señales bullish consecutivas bloquean SHORT

**SOFT MODE** (permite con restricciones):
- Size cap: 50%
- Score penalty: -1.5
- Condición: todas las señales son lagging

---

## 7. POSITION SIZING

**Archivo**: `src/application/live/sizing_calculator.py`

### 5 Multiplicadores Core:
| Multiplicador | Valores |
|---------------|---------|
| base_risk_pct | 1% fijo |
| rr_mult | 0.0 (block), 0.5, 1.0 |
| regime_mult | 0.7 (range), 1.0 (trend) |
| loss_streak_mult | 0.1 - 1.0 |
| session_mult | 0.8, 1.0, o BLOCK |

### RR Bucket:
| RR | Multiplicador |
|----|---------------|
| < 0.70 | HARD BLOCK |
| 0.70-1.00 | 0.5 |
| ≥ 1.00 | 1.0 |

### Loss Streak:
| Losses | Mult | Cooldown |
|--------|------|----------|
| 0-1 | 1.0 | 0 min |
| 2 | 0.6 | 0 min |
| 3 | 0.4 | 3 min |
| 4 | 0.25 | 5 min |
| 5+ | 0.1 | 10 min |

### Por Tipo de Edge:
| Edge | Path | Size |
|------|------|------|
| DISPLACEMENT | - | 30-60% |
| BREAKOUT | - | 50-80% |
| RANGE | - | 50-80% |
| TREND | BOS_PULLBACK | 60-80% |
| TREND | CONSENSUS | 40-60% |
| INTRADAY | - | 20-50% |
| PROVISIONAL | - | 25-35% |

---

## 8. ENTRY SCORING

**Archivo**: `src/domain/scoring/score_calculator.py`

### 4 Bloques (0-2 pts cada uno):
| Bloque | Componentes |
|--------|-------------|
| STRUCTURE | HTF alignment + 5m structure |
| TIMING | BOS confirmation + clean close |
| MOMENTUM | ADX + slope + volume |
| CONTEXT | Session + volatility ratio |
| SYNERGY | +0-1 bonus |

**Total Máximo**: 9 pts  
**Threshold Default**: 5 pts

**Solo STRUCTURE=0 bloquea entrada** (única gate real)

---

## 9. BYPASSES CRÍTICOS

| Bypass | Trigger | Efecto |
|--------|---------|--------|
| `displacement_bypass` | DISPLACEMENT edge | Salta structure gate, macro veto |
| `anticipation_bypass` | ANTICIPATION ≥70% | Salta macro veto, 60% size |
| `expansion_bypass` | EXPANSION_MODE | Salta todo incluyendo location |
| Breakout Override | `breakout_score ≥ 3` | Permite location extreme |
| SOFT MODE | Displacement memory | SHORT obstinacy con penalties |

---

## 10. TIMEFRAMES USADOS

| TF | Uso Principal |
|----|---------------|
| 1m | Timing, displacement, sweep detection |
| 5m | Régimen (ADX), estructura EMA, edge detection |
| 15m | Macro trend EMA20/50 |
| 1h | Macro trend EMA20/50, HTF ADX |

---

## 11. UMBRALES RÁPIDOS

| Concepto | Valor |
|----------|-------|
| ADX Range | < 18 |
| ADX Trend | ≥ 20 |
| ADX Strong | ≥ 25 |
| Displacement | ≥ 1.5x ATR |
| Location Extreme | ≤ 10% o ≥ 90% |
| BB Squeeze | < percentil 10 |
| Entry Score | ≥ 5 |
| Max Trades/Edge | 2 |
| Min RR | 0.70 |
| Time Invalidation | 2-5 min |
