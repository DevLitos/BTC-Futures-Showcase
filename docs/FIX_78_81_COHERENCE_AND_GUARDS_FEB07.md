# FIX #78-#82: Macro Bias Coherence & Guards Relaxation

**Fecha:** Feb 07, 2026
**Autor:** Trading System + User Feedback
**Estado:** IMPLEMENTADO

## Resumen

Implementación de mejoras críticas basadas en feedback del usuario:

1. **FIX #78**: Macro Bias Coherence Check - Bloquear consensus incoherente
2. **FIX #79**: Relajar SHORT TOO_LATE gate 
3. **FIX #80**: Permitir LONG en reversal zone
4. **FIX #81**: Stats tracking para Crash Invalidation
5. **FIX #82**: Weighted Bias Score (continuo, no binario)

---

## Problema Original

Del análisis de logs:

```
TREND_CONSENSUS LONG ... macro=BEAR_STRONG
```

El sistema armaba edges **INCOHERENTES**:
- **CONSENSUS LONG** cuando el macro bias era **BEAR_STRONG**
- Esto permitía trades contra la tendencia macro sin justificación válida

### User Quote:
> "Todavía puedes tener: BEAR_STRONG + LONG consensus. Eso sigue siendo INCOHERENTE."

---

## FIX #78: Macro Bias Coherence Check

### Ubicación
`src/domain/trading/edge_state_manager.py` - `arm_consensus_edge()`

### Cambio
```python
def arm_consensus_edge(
    self,
    direction: str,
    current_price: float,
    atr: float,
    market_time: datetime,
    reason: str = "CONSENSUS",
    macro_bias_strength: str | None = None,  # 🔥 NEW
    has_momentum_override: bool = False       # 🔥 NEW
) -> bool:
    
    # 🔥 FIX #78: MACRO BIAS COHERENCE CHECK
    if macro_bias_strength and not has_momentum_override:
        is_incoherent = (
            (macro_bias_strength == 'BEAR_STRONG' and direction == 'LONG') or
            (macro_bias_strength == 'BULL_STRONG' and direction == 'SHORT')
        )
        if is_incoherent:
            logger.warning(f"🚫 CONSENSUS BLOCKED: {direction} incoherent with macro={macro_bias_strength}")
            return False
```

### Lógica
- **BEAR_STRONG + LONG consensus** = BLOCKED (unless momentum override)
- **BULL_STRONG + SHORT consensus** = BLOCKED (unless momentum override)
- Momentum override (DISPLACEMENT, EXPANSION) puede ignorar esto porque el price action ha demostrado que el macro se está rompiendo

---

## FIX #79: Relajar SHORT TOO_LATE Gate

### Problema
El sistema bloqueaba SHORTs cuando el precio estaba >2% debajo de EMA50:
```
SHORT in BAD ZONE - price 67897 already -2.09% below EMA50 (too late to sell)
```

Pero en un **downtrend activo**, el precio puede caer 3-5% debajo de EMA50 y **seguir cayendo**.

### Ubicación
`src/domain/trading/scoring/entry_scorer.py` - `_score_structure()`

### Cambio
**ANTES:**
```python
elif price_distance_pct < -0.02:  # More than 2% below EMA50
    htf_aligned = False
    zone_quality = "TOO_LATE"
```

**DESPUÉS:**
```python
# ALSO GOOD: Negative slope overrides (downtrend in progress)
elif ema50_slope < -0.02:
    htf_aligned = True
    zone_quality = "TREND_DOWN"

# BAD ZONE only when REALLY extended AND momentum fading
elif price_distance_pct < -0.04 and ema50_slope >= -0.02:
    htf_aligned = False
    zone_quality = "TOO_LATE"
```

### Lógica
- **OLD**: >2% below = blocked
- **NEW**: >4% below AND slope flat/up = blocked
- Permite SHORTs en downtrend activo (slope negativo)

---

## FIX #80: Permitir LONG en Reversal Zone

### Problema
LONGs bloqueados cuando precio 1-3% debajo de EMA50 con slope negativo:
```
STRUCTURE GATE: LONG against HTF (zone=NEUTRAL)
```

Pero esto bloquea **bounces válidos** desde soporte.

### Ubicación
`src/domain/trading/scoring/entry_scorer.py` - `_score_structure()`

### Cambio
```python
# 🔥 FIX #80: Within 3% below EMA50 → allow with score penalty
elif price_distance_pct >= -0.03:  # Within 3% below EMA50
    htf_aligned = True
    htf_slope_override = True
    zone_quality = "REVERSAL_ZONE"
```

### Lógica
- Precio dentro de 3% debajo de EMA50 → permitir entry
- El scoring naturalmente dará menor score por HTF misalignment
- Solo bloquear cuando >3% debajo

---

## FIX #81: Stats para Crash Invalidation

### Problema (User Feedback)
> "Si <50 PANIC_DOWN reales → esto es ajuste fino peligroso (overfitting)"

### Ubicación
`src/domain/trading/edge_state_manager.py` - `check_crash_invalidation()`

### Cambio
```python
# Initialize stats tracker
if not hasattr(self, '_crash_invalidation_stats'):
    self._crash_invalidation_stats = {
        'panic_down_detected': 0,
        'meltup_detected': 0,
        'long_invalidated': 0,
        'short_invalidated': 0,
        'skipped_no_edge': 0,
        'skipped_low_confidence': 0,
    }

# Track all detections
if is_panic_down:
    self._crash_invalidation_stats['panic_down_detected'] += 1
    
# Log stats when invalidating
logger.warning(f"Stats: {self._crash_invalidation_stats}")
```

### Nuevo Método
```python
def get_crash_invalidation_stats(self) -> dict:
    """Get stats for crash invalidation to validate usefulness."""
    return self._crash_invalidation_stats.copy()
```

---

## FIX #82: Weighted Bias Score (Continuo, No Binario)

### Problema (User Feedback)
> "Sigues en Lógica Binaria... IF/ELSE. No: PESO CONTINUO."

### Ubicación
`src/config/trading_config.py` - `BiasStrengthConfig.calculate_weighted_bias_score()`

### Cambio
```python
@classmethod
def calculate_weighted_bias_score(
    cls,
    ema50_vs_ema200: str,      # 'BULL', 'BEAR', 'NEUTRAL'
    di_direction: str | None,  # 'LONG', 'SHORT', or None
    di_strength: float,        # abs(+DI - -DI)
    price_vs_ema50_pct: float, # % distance from EMA50
    ema50_slope: float,        # slope of EMA50
    trade_direction: str       # 'LONG' or 'SHORT'
) -> dict:
```

### Componentes (Weighted):
| Componente | Peso | Descripción |
|------------|------|-------------|
| EMA50/200 macro | 35% | LAGGING pero estable |
| +DI/-DI direction | 30% | LEADING momentum |
| Price vs EMA50 | 20% | Posición relativa actual |
| EMA50 slope | 15% | Momentum de corto plazo |

### Output:
```python
{
    'score': -1.0 to +1.0,      # Negative=SHORT, Positive=LONG
    'alignment': -1.0 to +1.0,  # How well trade aligns with bias
    'size_mult': 0.3 to 1.0,    # Suggested size based on alignment
    'breakdown': {...}           # Component scores for debugging
}
```

### Size Multiplier Logic:
- alignment >= 0.5: Full size (1.0)
- alignment 0 to 0.5: 0.7 to 1.0
- alignment -0.5 to 0: 0.4 to 0.7
- alignment < -0.5: 0.3 (minimal)

---

## Impacto Esperado

| Métrica | Antes | Después |
|---------|-------|---------|
| CONSENSUS incoherente blocked | 0% | 100% |
| SHORT TOO_LATE false positives | ~75 blocks | Reducido |
| LONG NEUTRAL false positives | ~171 blocks | Reducido |
| Crash Invalidation tracked | No | Yes (stats) |
| Bias scoring | Binary (3 states) | Continuous (-1 to +1) |

---

## Próximos Pasos

1. **Ejecutar sistema** y verificar que no hay errores
2. **Revisar logs** para ver cuántos CONSENSUS son bloqueados por FIX #78
3. **Monitorear** crash_invalidation_stats para validar utilidad
4. **Medir** trades permitidos por FIX #79-80 y su profitability
5. **Integrar** weighted_bias_score en decisiones de sizing

---

## Archivos Modificados

1. `src/domain/trading/edge_state_manager.py`
   - `arm_consensus_edge()` - FIX #78
   - `check_crash_invalidation()` - FIX #81
   
2. `src/application/live/trading_app.py`
   - Llamadas a `arm_consensus_edge()` con nuevos params
   
3. `src/domain/trading/scoring/entry_scorer.py`
   - `_score_structure()` - FIX #79, #80
   
4. `src/config/trading_config.py`
   - `BiasStrengthConfig.calculate_weighted_bias_score()` - FIX #82
