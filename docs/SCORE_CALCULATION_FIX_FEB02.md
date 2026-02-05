# FIX: Score Calculation for FAST_PATH Entries
**Fecha:** 2 de Febrero, 2026  
**Archivos modificados:**
- `src/application/live/entry_orchestrator.py`
- `src/domain/risk/risk_engine.py`

---

## 📋 Resumen Ejecutivo

Este fix resuelve la pregunta: **"¿Por qué a veces Score=0?"**

### Respuesta: Era por DISEÑO, pero con consecuencias negativas

El sistema tenía **DOS pipelines de entrada** separados:
1. **Pipeline Normal** → Calcula score completo (max 9 puntos)
2. **FAST_PATH** → Saltaba TODO el scoring para velocidad

Los trades FAST_PATH tenían `entry_score=0` en los logs, lo que:
- Impedía análisis de calidad de FAST_PATH
- Hacía que TIME_INVALIDATION aplicara reglas incorrectas

---

## 🔬 Diagnóstico Detallado

### ¿Qué es FAST_PATH?
FAST_PATH es un bypass de la validación normal durante **displacement events** (movimientos explosivos >$200-300).

La filosofía era:
```
"Prefiero entrar feo que perder el tren"
```

Durante un displacement fuerte, el sistema entraba INMEDIATAMENTE (0-1 velas) sin calcular score para no perder el momentum.

### El Problema
Aunque era "por diseño", tenía consecuencias no intencionadas:

| Problema | Impacto |
|----------|---------|
| Score=0 en logs | Imposible distinguir FAST_PATH buenos de malos |
| TIME_INVALIDATION confundido | Aplicaba reglas de "baja calidad" a todos los FAST_PATH |
| No hay métricas | Sin datos para optimizar criterios de FAST_PATH |

### Flujo Antes del Fix
```
orchestrate_entry()
    │
    ├─► _displacement_fast_path()  ─►  Retorna SIN score ─► score=0 en logs
    │        (si bars_since <= 1)
    │
    └─► Pipeline normal ─► calculate_entry_score() ─► score=5-9 en logs
```

---

## ✅ Solución Implementada

### Cambio 1: Score INFORMATIVO en FAST_PATH

Ahora FAST_PATH calcula el score pero **NO bloquea** la entrada:

```python
# En _displacement_fast_path(), líneas 1144-1210

# 🔥 FIX (Feb 02): SCORE INFORMATIVO PARA FAST_PATH
fast_path_score = 0.0
fast_path_breakdown = {}

try:
    entry_scorer = getattr(engine, '_entry_scorer', None)
    if entry_scorer and ctx.df_1h is not None and ctx.df_5m is not None and ctx.df_1m is not None:
        score, breakdown, structure_blocked = entry_scorer.calculate_entry_score(
            df_1h=ctx.df_1h,
            df_5m=ctx.df_5m,
            df_1m=ctx.df_1m,
            bias=disp_direction,
            current_price=current_price,
            market_time=market_time,
            displacement_edge_active=True,  # BYPASS structure gate
        )
        fast_path_score = score
        fast_path_breakdown = breakdown
        
        # Guardar en tags y engine
        engine._current_trade_tags['entry_score'] = fast_path_score
        engine._entry_score = fast_path_score
        # ...
```

**Beneficios:**
1. ✅ Score real guardado para análisis
2. ✅ FAST_PATH NO se bloquea (sigue entrando inmediatamente)
3. ✅ TIME_INVALIDATION puede usar score real
4. ✅ Post-trade analysis puede identificar patrones

### Cambio 2: TIME_INVALIDATION usa Score Real

Antes:
```python
# HACK: Asumía que score=0 significaba FAST_PATH de baja calidad
is_fast_path_trade = self.entry_score == 0
```

Después:
```python
# 🔥 FIX (Feb 02): Usar SCORE REAL con threshold significativo
# Score < 4: Very low quality → -0.25R (cut fast)
is_low_quality_entry = self.entry_score is not None and self.entry_score < 4
```

---

## 📊 Impacto Esperado

### Antes del Fix
| Métrica | Valor |
|---------|-------|
| Trades con score=0 | ~35% (todos los FAST_PATH) |
| TIME_INVALIDATION en score=0 | Siempre -0.25R |
| Distinguir FAST_PATH buenos/malos | Imposible |

### Después del Fix
| Métrica | Valor Esperado |
|---------|----------------|
| Trades con score=0 | <5% (solo si falla el scoring) |
| TIME_INVALIDATION | Basado en score REAL |
| Distinguir FAST_PATH | ✅ Por score (4-9) |

---

## 🧪 Cómo Verificar

1. **En logs de trading:**
   ```
   📊 FAST PATH INFORMATIVE SCORE: 6.5 | breakdown={...}
   🚀🚀🚀 DISPLACEMENT FAST PATH ACTIVATED 🚀🚀🚀 | INFO_SCORE=6.5
   ```

2. **En trade_logs (JSONL):**
   ```json
   {
     "entry_type": "FAST_PATH_DISPLACEMENT",
     "entry_score": 6.5,
     "score_breakdown": {"structure": 2, "timing": 1, ...}
   }
   ```

3. **En TIME_INVALIDATION:**
   ```
   📊 TIME checkpoint: LOW_QUALITY (score=3.5) → max_loss=-0.25R (stricter)
   ```
   vs
   ```
   📊 TIME checkpoint: DISPLACEMENT mode → max_loss=-0.50R (relaxed)
   ```

---

## 📝 Tabla de Score Thresholds

| Score Range | Clasificación | TIME_INVALIDATION Max Loss |
|-------------|---------------|---------------------------|
| < 4 | LOW_QUALITY | -0.25R (estricto) |
| 4-5 | BELOW_AVERAGE | -0.30R (normal) |
| 5-7 | GOOD | Depende del edge type |
| 7-9 | EXCELLENT | Depende del edge type |

**Si es DISPLACEMENT edge:**
- Score < 4 pero DISPLACEMENT → -0.50R (el displacement tiene prioridad)
- Score ≥ 4 con DISPLACEMENT → -0.50R

**Si es RANGING (ADX < 20):**
- Todos los scores → -0.60R (respeta el stop estructural)

---

## 🔗 Archivos Relacionados

- [BUG_FIX_FEB01_TIME_INVALIDATION.md](BUG_FIX_FEB01_TIME_INVALIDATION.md) - Fix anterior de TIME_INVALIDATION
- [BREAKOUT_EDGE_IMPLEMENTATION_JAN28.md](BREAKOUT_EDGE_IMPLEMENTATION_JAN28.md) - Implementación de displacement edge
- `src/domain/trading/scoring/entry_scorer.py` - Sistema de scoring (4 bloques)
