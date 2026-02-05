# DEAD MODE FIX - Feb 05, 2026 (REVISADO)

## Problema Identificado

El sistema tenía un **"estado absorbente"** (deadlock) donde el trading quedaba permanentemente bloqueado:

```
ADX = 30.9 (indica TREND)
TrendPhase = RANGE_BOUND → Override forzaba RANGE mode
pct_60d = 23% → No está en extremos (15%/85%)
HARD BLOCK activado → 0 trades
```

### Síntomas en Log
- 12 hours de trading session
- 0 trades completados
- Ciclos infinitos de BIAS FLIP / BIAS RESTORE
- DEFENSIVE MODE activándose cuando sistema ya bloqueado

## Solución Implementada (REVISADA Feb 05 16:00)

### ⚠️ CORRECCIÓN CRÍTICA: ATR 15m → ATR 1m

**Análisis de datos reales:**
- ATR 1m típico: **$50-70 USD**
- Thresholds históricos de BIAS FLIP: **$30-90 USD**
- Propuesta original de ATR 15m habría dado: **$200-400 USD** (5-8x más alto)

**Conclusión:** ATR 15m habría hecho el sistema CIEGO a cambios de dirección.
Se mantiene ATR 1m como base con multiplicadores adaptativos.

### 1. RegimeScore Continuo (Fix #25) ✅
**Archivo:** `trading_app.py` líneas ~3500-3610

Reemplazamos el **override binario** ADX vs TrendPhase por un **score continuo**:

```python
RegimeScore = w_adx * ADX_norm + w_phase * Phase_score + w_vol * Vol_norm

# Weights (REVISADOS)
W_ADX = 0.40       # ADX contribuye 40% (más objetivo)
W_PHASE = 0.35     # TrendPhase contribuye 35%
W_VOLATILITY = 0.25 # Volatilidad contribuye 25%

# ADX normalización REVISADA: 15-45 (no 15-35)
# BTC puede tener ADX > 60 en tendencias fuertes
```

**Umbrales:**
- Score < 0.35 → RANGE
- Score 0.35-0.50 → TRANSITION  
- Score > 0.50 → TREND

### 2. RANGE_MID_MODE (Fix #25) ✅
**Archivo:** `trading_app.py` líneas ~4030-4130

Eliminamos el HARD BLOCK y lo reemplazamos con un **EdgeScore condicional**:

```python
EdgeScore = proximity + velocity + volume + structure + regime

RANGE_MID_EDGE_THRESHOLD = 0.35

if edge_score >= RANGE_MID_EDGE_THRESHOLD:
    # Trade con size reducido (30-55%)
else:
    # Soft block con log detallado (no deadlock)
```

### 3. BIAS Invalidation con ATR 1m + Confirmación Estructural (Fix #26, #27) 🔧 CORREGIDO
**Archivo:** `trading_app.py` líneas ~2530-2650

```python
# ❌ INCORRECTO (propuesta original):
# threshold = atr_15m * 0.8 * adaptive_mult (~$200-400)

# ✅ CORRECTO (implementación final):
# threshold = atr_1m * 1.5 * adaptive_mult (~$75-150)

BASE_ATR_MULT = 1.5  # Proven approach from historical data

# Adaptive multipliers:
# - Normal: 1.0x → threshold ~$75-105
# - DIRECTION_LOCK: 1.5x → threshold ~$112-157
# - EXPANSION_MODE: 2.0x → threshold ~$150-210

# Bounds: min $50, max $250
# Structural confirmation: CHECK ADICIONAL (no reemplazo)
```

### 4. Coordinación Defensive ↔ Regime (Fix #28) ✅
**Archivo:** `filters.py` + `trading_app.py`

```python
# BlackSwanFilter.check() ahora acepta regime_blocked
if regime_blocked:
    # Suprimir log de DEFENSIVE MODE (redundante)
    return False, f"DEFENSIVE (suppressed - {regime_block_reason})"
```

### 5. EdgeScore Probabilístico para Extremos (Fix #29) ✅
**Archivo:** `trading_app.py` líneas ~4130-4230

```python
extreme_edge = (
    pct_extremity * 0.40 +       # Cercanía al extremo
    velocity_factor * 0.25 +     # Momentum (reversal-friendly)
    volume_factor * 0.15 +       # Volumen (capitulation signal)
    structure_factor * 0.10 +    # Alineación estructural
    divergence_factor * 0.10     # RSI divergence
)

# Size = 25% + (edge * 40%) → rango 25%-65%
```

## Nuevos Tags de Logging

Los siguientes tags se agregan a `_current_trade_tags` para debugging/ML:

| Tag | Descripción |
|-----|-------------|
| `regime_score` | Score continuo [0,1] del régimen |
| `regime_adx_component` | Componente ADX normalizado |
| `regime_phase_component` | Componente TrendPhase normalizado |
| `regime_vol_component` | Componente volatilidad normalizado |
| `range_mid_mode` | Boolean si está en RANGE_MID_MODE |
| `range_mid_edge_score` | EdgeScore en zona media |
| `range_mid_edge_components` | Dict con componentes del edge |
| `bias_invalidation_threshold` | Threshold actual en USD |
| `atr_15m` | ATR 15m usado para cálculos |
| `bias_flip_rejected` | True si flip rechazado por estructura |
| `extreme_edge_score` | EdgeScore para trades en extremos |
| `extreme_edge_components` | Dict con componentes del edge extremo |

## Comportamiento Esperado Post-Fix

### Antes (DEAD MODE):
```
12:14 RANGE_BOUND | pct=23% | ADX=30.9
      → HARD BLOCK (no extreme)
      → DEFENSIVE MODE activado (redundante)
      → BIAS FLIP / RESTORE cycling
      → 0 trades
```

### Después:
```
12:14 RegimeScore=0.42 → TRANSITION
      pct=23% | EdgeScore=0.28 < 0.35
      → Soft block (no trade pero no deadlock)
      → DEFENSIVE MODE suprimido
      → Sistema puede reaccionar a nuevo displacement
```

## Validación

Para verificar el fix:

1. **Buscar logs de RegimeScore:**
   ```
   grep "REGIME SCORE" trading_run_*.log
   ```

2. **Verificar RANGE_MID_MODE:**
   ```
   grep "RANGE MID MODE" trading_run_*.log
   ```

3. **Confirmar BIAS FLIP mejorado:**
   ```
   grep "BIAS FLIP REJECTED" trading_run_*.log
   ```

4. **Verificar supresión de DEFENSIVE:**
   ```
   grep "DEFENSIVE.*suppressed" trading_run_*.log
   ```

## Riesgos y Mitigaciones

| Riesgo | Mitigación |
|--------|------------|
| Over-trading en zona media | EdgeScore threshold 0.35 + size reducido 30-55% |
| BIAS FLIP muy sensible | Confirmación estructural requerida |
| RegimeScore mal calibrado | Logging detallado para ajuste iterativo |

## Próximos Pasos

1. **Backtesting:** Correr backtest con nuevos fixes
2. **Calibración:** Ajustar weights si es necesario
3. **Monitoreo:** Observar trades en zona media del rango
4. **ML:** Usar nuevos tags para entrenar modelos

---
*Implementado por: GitHub Copilot*
*Fecha: Feb 05, 2026*
*Version: v2.1.1*
