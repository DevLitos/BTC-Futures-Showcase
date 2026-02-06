# 🔥 ANÁLISIS EXHAUSTIVO DE CONFLICTOS - Sistema BTC Trading
**Fecha:** February 6, 2026  
**Analista:** Sistema de Auditoría Automatizada  
**Última actualización:** February 6, 2026 - FIX #59, #60 completados

---

## 📋 RESUMEN EJECUTIVO

Se han identificado **14 conflictos potenciales** en la interacción entre los diferentes subsistemas:

| Severidad | Cantidad | Resueltos | Pendientes |
|-----------|----------|-----------|------------|
| 🔴 CRITICAL | 3 | ✅ 3 | 0 |
| 🟠 HIGH | 5 | ✅ 3 | 2 |
| 🟡 MEDIUM | 4 | ✅ 2 | 2 |
| 🟢 LOW | 2 | 0 | 2 |

---

## ✅ FIXES COMPLETADOS (Feb 6, 2026)

### FIX #59 - Conflictos Críticos Resueltos
1. **Conflicto #1**: Direction staleness tracking → `is_direction_stale()` en EdgeStateManager
2. **Conflicto #2**: BREAKOUT vs COMPRESSION_BREAKOUT → Check de redundancia en idle_state_handlers
3. **Conflicto #7**: Triple override priority → Jerarquía explícita (100, 80, 60, 0)

### FIX #60 - Conflictos High/Medium Resueltos
4. **Conflicto #9**: OBSTINACY counter race condition → Reset en guards passed
5. **Conflicto #8**: Size cap order → Documentación en SizingOrchestrator
6. **Conflicto #11** (parcial): ADX thresholds → anticipation.py usa ADXThresholds centralizado

---

## 1. 🔥 EDGE TYPES vs ANTICIPATION CONFLICTS

### CONFLICTO #1: ANTICIPATION vs EDGE_STATE Dirección Opuesta ✅ RESUELTO
**Severidad:** 🔴 CRITICAL → ✅ FIXED (FIX #59)

**Solución implementada:**
- Nuevo campo `_direction_stale_since` en EdgeStateManager
- Método `is_direction_stale(threshold_minutes=5)` detecta direcciones obsoletas
- idle_state_handlers.py ahora verifica staleness antes de invalidar anticipation

**Archivos modificados:**
- [edge_state_manager.py](src/domain/trading/edge_state_manager.py) - Líneas 185-220
- [idle_state_handlers.py](src/application/live/idle_state_handlers.py) - Líneas 473-490

---

### CONFLICTO #2: BREAKOUT Edge vs COMPRESSION_BREAKOUT Anticipation ✅ RESUELTO
**Severidad:** 🔴 CRITICAL → ✅ FIXED (FIX #59)
**Severidad:** 🟠 HIGH

**Archivos afectados:**
- [edge_state_manager.py#L95-L115](src/domain/trading/edge_state_manager.py#L95-L115) (EdgePriority)
- [anticipation.py#L760-L850](src/domain/trading/anticipation.py#L760-L850) (evaluate_all)

**Descripción:**
- `EdgeType.BREAKOUT` tiene prioridad 80 en EdgePriority
- `AnticipationSignal.COMPRESSION_BREAKOUT` NO está en el sistema de prioridades
- Ambos detectan el MISMO patrón (BB squeeze)
- No hay regla clara sobre quién "gana"

**Consecuencias:**
1. Pueden activarse simultáneamente
2. Anticipation puede sugerir LONG, edge BREAKOUT puede ser SHORT
3. Sistema no sabe cuál seguir

**Solución recomendada:**
```python
# En idle_state_handlers.py - check_anticipation_signal()
if anticipation.signal == AnticipationSignal.COMPRESSION_BREAKOUT:
    # Si ya hay BREAKOUT edge activo, anticipation es redundante
    if edge_mgr.state == EdgeState.EDGE_ACTIVE and edge_mgr.current_bos.edge_type == EdgeType.BREAKOUT:
        logger.debug("ANTICIPATION redundant: BREAKOUT edge already active")
        anticipation = AnticipationState()  # No duplicar
```

---

### CONFLICTO #3: STRUCTURE_BUILD vs TREND Edge
**Severidad:** 🟡 MEDIUM

**Archivos afectados:**
- [anticipation.py#L222-L270](src/domain/trading/anticipation.py#L222-L270) (_detect_structure_building)
- [edge_state_manager.py#L750-L800](src/domain/trading/edge_state_manager.py#L750-L800) (BOSEvent)

**Descripción:**
- `AnticipationSignal.STRUCTURE_BUILD` detecta HLs/LHs formándose
- `EdgeType.TREND` espera BOS + pullback
- Pueden señalar direcciones opuestas:
  - STRUCTURE_BUILD: "Veo HLs formándose → LONG próximo"
  - TREND edge: "Ya hubo BOS SHORT, espero pullback → SHORT"

**Consecuencias:**
- Confusión en qué dirección tomar
- Sizing puede ser inconsistente

**Solución recomendada:**
- TREND edge tiene prioridad (ya confirmó movimiento)
- STRUCTURE_BUILD solo válido si no hay edge activo

---

## 2. 🔥 EDGE STATES vs ACTIVE_DIRECTION CONFLICTS

### CONFLICTO #4: EDGE_ACTIVE=LONG pero ACTIVE_DIRECTION=SHORT
**Severidad:** 🔴 CRITICAL

**Archivos afectados:**
- [trading_app.py#L3345-L3365](src/application/live/trading_app.py#L3345-L3365)
- [direction_state_manager.py#L28-L50](src/application/live/direction_state_manager.py#L28-L50)

**Descripción:**
```python
# trading_app.py L3345
self._active_direction = orch_result.final_direction  # Puede ser SHORT

# Pero edge_mgr.current_bos.direction puede ser LONG
# No hay validación de coherencia!
```

**Consecuencias:**
1. `_execute_entry()` usa `ctx.bias` que viene de `_active_direction`
2. Pero guards usan `edge_mgr.current_bos.direction` para validación
3. Trade puede pasar todos los guards pero entrar en dirección equivocada

**Evidencia en logs:**
```
edge_direction=LONG, active_direction=SHORT, bias=SHORT
→ Entra SHORT pero edge esperaba LONG
```

**Solución recomendada:**
```python
# En entry_orchestrator.py - run_all_guards()
if edge_mgr and edge_mgr.current_bos:
    if ctx.bias != edge_mgr.current_bos.direction:
        result.passed = False
        result.reason = f"Direction mismatch: bias={ctx.bias} vs edge={edge_mgr.current_bos.direction}"
        return result
```

---

### CONFLICTO #5: EDGE_EXPIRED pero ACTIVE_DIRECTION persiste
**Severidad:** 🟠 HIGH

**Archivos afectados:**
- [edge_state_manager.py#L1400-L1450](src/domain/trading/edge_state_manager.py#L1400-L1450)
- [trading_app.py#L781](src/application/live/trading_app.py#L781)

**Descripción:**
- Cuando edge expira (`EdgeState.EDGE_EXPIRED`), el sistema hace `_transition_to(NO_EDGE)`
- PERO `_active_direction` NO se resetea automáticamente
- Queda "colgada" hasta que otro displacement la sobreescriba

**Consecuencias:**
1. Sistema sin edge pero con dirección "congelada"
2. Nuevos setups contradictorios son bloqueados innecesariamente
3. Crea "dead zones" de 10-30 minutos

**Solución recomendada:**
```python
# En edge_state_manager.py - cuando expira edge
def _expire_edge(self, reason: str):
    self._transition_to(EdgeState.EDGE_EXPIRED, reason)
    # NUEVO: Notificar que active_direction debe evaluarse
    # (No resetear directamente - pero marcar como "needs_refresh")
    self._direction_needs_refresh = True
```

---

### CONFLICTO #6: EdgeState.NO_EDGE permite entrada vía PROVISIONAL
**Severidad:** 🟡 MEDIUM

**Archivos afectados:**
- [edge_state_manager.py#L260-L320](src/domain/trading/edge_state_manager.py#L260-L320) (ProvisionalEdgeConfig)
- [entry_orchestrator.py#L180-L195](src/application/live/entry_orchestrator.py#L180-L195)

**Descripción:**
- `ProvisionalEdgeConfig` permite entradas con criterios relajados
- Pero el guard `check_edge_enforcement()` verifica `edge_mgr.is_edge_active()`
- Si no hay edge → trade bloqueado ANTES de evaluar PROVISIONAL

**Código conflictivo:**
```python
# entry_orchestrator.py L180
can_trade, edge_mult, block_reason = await ExecutionGuards.check_edge_enforcement(engine, ctx.current_price)
if not can_trade:
    result.passed = False  # BLOQUEA aunque PROVISIONAL podría aplicar
```

**Solución recomendada:**
- Separar PROVISIONAL de edge enforcement
- PROVISIONAL debe poder activarse sin edge ACTIVE

---

## 3. 🔥 OBSTINACY vs SOFT_MODE vs DISPLACEMENT_BYPASS CONFLICTS

### CONFLICTO #7: Triple Override - ¿Quién gana? ✅ RESUELTO
**Severidad:** 🔴 CRITICAL → ✅ FIXED (FIX #59)

**Solución implementada:**
Jerarquía de prioridades explícita en entry_orchestrator.py:
- **PRIORIDAD 100**: DISPLACEMENT_BYPASS → bypass completo, sin caps
- **PRIORIDAD 80**: SOFT_MODE (strong_trend) → permite con 50% cap
- **PRIORIDAD 60**: SOFT_MODE (displacement_memory) → permite con 50% cap
- **PRIORIDAD 0**: HARD_BLOCK → bloquea trade

Tags de trazabilidad: `obstinacy_bypassed_by`, `obstinacy_override_priority`

**Archivo modificado:** [entry_orchestrator.py](src/application/live/entry_orchestrator.py) - Líneas 310-395

---

### CONFLICTO #8: SOFT_MODE Size Cap vs Edge Size Multiplier ✅ RESUELTO
**Severidad:** 🟠 HIGH → ✅ FIXED (FIX #60.3)

**Solución implementada:**
Documentación agregada al docstring de `SizingOrchestrator` explicando:
1. Caps son MULTIPLICATIVOS, no absolutos
2. Orden de aplicación definido (base → edge → regime → ... → CAPS ÚLTIMOS)
3. obstinacy_soft_cap = 0.5 significa 50% del size calculado

**Archivo modificado:** [entry_orchestrator.py](src/application/live/entry_orchestrator.py) - Líneas 1150-1185

---

### CONFLICTO #9: OBSTINACY Invalidation Counter Race Condition ✅ RESUELTO
**Severidad:** 🟡 MEDIUM → ✅ FIXED (FIX #60.2)

**Solución implementada:**
Reset del contador `_obstinacy_block_count` al final de `run_entry_guards()` cuando
`result.passed = True`. Esto asegura que el contador solo se acumula cuando el
bloqueo es específicamente por SHORT_OBSTINACY.

```python
# Al final de run_entry_guards():
if result.passed:
    engine._obstinacy_block_count = 0
```

**Archivo modificado:** [entry_orchestrator.py](src/application/live/entry_orchestrator.py) - Líneas 930-937
# 2. El trade pasa pero es rechazado después
```

**Consecuencia:**
- Contador puede acumularse incorrectamente
- Edge válido puede invalidarse prematuramente

**Solución recomendada:**
```python
# Reset counter si guard pasa por cualquier razón
if result.passed:
    engine._obstinacy_block_count = 0
```

---

## 4. 🔥 REGIME vs ADX vs TREND_PHASE CONFLICTS

### CONFLICTO #10: Tres Clasificadores de "Estado del Mercado"
**Severidad:** 🟠 HIGH

**Archivos afectados:**
- [regime_classifier.py#L1-L100](src/domain/market/filters/regime_classifier.py#L1-L100)
- [regime_detector.py#L1-L150](src/domain/market/regime/regime_detector.py#L1-L150)
- [trading_app.py#L2381](src/application/live/trading_app.py#L2381)

**Descripción:**
El sistema tiene TRES clasificadores que se superponen:

| Clasificador | Valores | Usado para |
|--------------|---------|------------|
| `Regime` (regime_classifier) | TREND, RANGE, NO_TRADE | Sizing, allow_entries |
| `EntryRegime` (regime_detector) | TREND_EXPANSION, TREND_EXHAUSTION, NORMAL, RANGE, CHAOS, PROBING | Chasing, order_type, RR |
| `TrendPhase` | RANGE_BOUND, EARLY_TREND, TREND_ACCELERATION, TREND_EXHAUSTION, DISTRIBUTION, TREND_DEATH | Score adjustments |

**Problema:**
```python
# Caso: ADX = 22
# - regime_classifier: TREND (ADX >= 18)
# - regime_detector: NORMAL (18 <= ADX < 25)
# - TrendPhase: podría ser RANGE_BOUND si EMAs flat

# ¿Qué sizing aplicar? ¿Qué RR mínimo?
```

**Consecuencias:**
1. Código usa uno u otro según contexto
2. No hay "source of truth" único
3. Sizing puede ser inconsistente

**Solución recomendada:**
Unificar en `UnifiedRegimeConfig`:
```python
# Usar SOLO UnifiedRegimeConfig
from src.config.unified_constants import get_unified_regime

regime = get_unified_regime(adx, trend_phase, volatility)
# Retorna configuración única con sizing, RR, etc.
```

---

### CONFLICTO #11: ADX Thresholds Inconsistentes
**Severidad:** 🟠 HIGH

**Archivos afectados:**
- [trading_config.py](src/config/trading_config.py) (ADXThresholds)
- [regime_classifier.py#L60-L70](src/domain/market/filters/regime_classifier.py#L60-L70)
- [regime_detector.py#L165-L170](src/domain/market/regime/regime_detector.py#L165-L170)

**Descripción:**
Múltiples thresholds de ADX definidos en diferentes lugares:

```python
# trading_config.py
class ADXThresholds:
    TREND = 18
    RANGE_EDGE_MAX = 15
    STRONG_TREND = 25

# regime_classifier.py (LEGACY)
ADX_TREND_THRESHOLD = 18  # Duplicado

# regime_detector.py (LEGACY)
ADX_TREND_THRESHOLD = 25.0  # DIFERENTE!
ADX_WEAK_THRESHOLD = 18.0
```

**Consecuencias:**
- `regime_classifier` dice TREND si ADX=19
- `regime_detector` dice NO_TREND si ADX=19 (need 25)
- Comportamiento impredecible

**Solución recomendada:**
```python
# Usar SOLO ADXThresholds de trading_config.py
# Marcar otros como @deprecated y migrar
```

---

### CONFLICTO #12: TrendPhase vs trading_mode Desincronización
**Severidad:** 🟡 MEDIUM

**Archivos afectados:**
- [entry_orchestrator.py#L77](src/application/live/entry_orchestrator.py#L77)
- [entry_orchestrator.py#L1077](src/application/live/entry_orchestrator.py#L1077)

**Descripción:**
```python
# EntryContext default
trend_phase: TrendPhase = TrendPhase.RANGE_BOUND

# Pero después:
regime_str = 'TREND' if is_trend else 'RANGE'

# TrendPhase puede ser RANGE_BOUND pero regime_str='TREND' si ADX > 18
```

**Consecuencias:**
- Score adjustment usa TrendPhase (puede dar -1 por RANGE_BOUND)
- Sizing usa regime_str (puede aplicar 1.0x por TREND)
- Resultado contradictorio

**Solución recomendada:**
Sincronizar ambos:
```python
if regime_str == 'TREND' and trend_phase == TrendPhase.RANGE_BOUND:
    # Inconsistencia detectada - usar ADX como tiebreaker
    trend_phase = TrendPhase.EARLY_TREND if adx > 18 else TrendPhase.RANGE_BOUND
```

---

## 5. 🔥 OTROS CONFLICTOS DETECTADOS

### CONFLICTO #13: Location Extreme Cooldown vs Edge Re-arm
**Severidad:** 🟢 LOW

**Archivos afectados:**
- [edge_state_manager.py#L1420-L1430](src/domain/trading/edge_state_manager.py#L1420-L1430)

**Descripción:**
- Cuando edge expira por `location_extreme`, hay cooldown
- Pero edge puede re-armarse inmediatamente si hay nuevo BOS
- Cooldown no se respeta para re-arm, solo para trades

**Solución recomendada:**
Ya implementado parcialmente con `_location_extreme_cooldown_until`

---

### CONFLICTO #14: Anticipation Time Validity vs Edge Timeout
**Severidad:** 🟢 LOW

**Archivos afectados:**
- [anticipation.py#L460](src/domain/trading/anticipation.py#L460)
- [edge_state_manager.py#L330](src/domain/trading/edge_state_manager.py#L330)

**Descripción:**
```python
# Anticipation válido por 10 minutos
valid_until = market_time + timedelta(minutes=10)

# Pero BREAKOUT edge timeout es 30 minutos
BREAKOUT_TIMEOUT_MIN = 30

# Anticipation expira mientras edge sigue vivo
```

**Consecuencias menores:**
- Logging confuso ("anticipation expired" pero edge sigue)

**Solución recomendada:**
- Sincronizar timeouts o documentar que son independientes

---

## 📊 MATRIZ DE PRIORIDADES DE RESOLUCIÓN (Actualizada Feb 6)

| Conflicto | Estado | Prioridad |
|-----------|--------|-----------|
| #1 ANTICIPATION Invalidation | ✅ FIXED (FIX #59) | 1 🔴 |
| #2 BREAKOUT vs ANTICIPATION | ✅ FIXED (FIX #59) | 2 🔴 |
| #7 Triple Override | ✅ FIXED (FIX #59) | 3 🔴 |
| #8 Size Cap Order | ✅ FIXED (FIX #60) | 4 🟠 |
| #9 Counter Race | ✅ FIXED (FIX #60) | 5 🟡 |
| #11 ADX Thresholds | ⚠️ PARCIAL (FIX #60) | 6 🟠 |
| #10 Tres Clasificadores | 🔲 PENDIENTE | 7 🟠 |
| #5 EDGE_EXPIRED persiste | 🔲 PENDIENTE | 8 🟠 |
| #6 NO_EDGE permite PROVISIONAL | 🔲 PENDIENTE | 9 🟡 |
| #3 STRUCTURE_BUILD vs TREND | 🔲 PENDIENTE | 10 🟡 |
| #12 TrendPhase vs trading_mode | 🔲 PENDIENTE | 11 🟡 |
| #13 Location Cooldown | 🔲 PENDIENTE | 12 🟢 |
| #14 Time Validity | 🔲 PENDIENTE | 13 🟢 |

---

## ✅ ACCIONES COMPLETADAS (Feb 6, 2026)

### FIX #59:
1. ✅ **Conflicto #1**: Direction staleness tracking → `is_direction_stale()`
2. ✅ **Conflicto #2**: Check de redundancia BREAKOUT vs COMPRESSION_BREAKOUT
3. ✅ **Conflicto #7**: Jerarquía explícita de prioridades (100, 80, 60, 0)

### FIX #60:
4. ✅ **Conflicto #9**: Reset counter OBSTINACY cuando guards pasan
5. ✅ **Conflicto #8**: Documentación de orden de size caps
6. ⚠️ **Conflicto #11**: anticipation.py usa ADXThresholds centralizado (parcial)

---

## 🔲 ACCIONES PENDIENTES

### Próximo Sprint:
- **Conflicto #10**: Migrar a `UnifiedRegimeConfig`
- **Conflicto #11** (completo): Eliminar TODOS los thresholds duplicados
- **Conflicto #5**: Auto-reset `_active_direction` cuando edge expira

### Backlog:
- Conflictos #3, #6, #12, #13, #14

---

## 📝 NOTAS FINALES

Este análisis se basa en el código fuente actual (Feb 6, 2026). Los conflictos identificados pueden manifestarse solo en condiciones específicas de mercado. Se recomienda:

1. **Agregar logging específico** para cada conflicto identificado
2. **Crear tests unitarios** que simulen las condiciones de conflicto
3. **Monitorear trade logs** buscando patrones de los conflictos

**Archivos clave a monitorear:**
- `trade_logs/trades_*.jsonl` - Buscar `direction_conflict`, `obstinacy_*`, `edge_*`
- `logs/` - Buscar "INVALIDATED", "BLOCKED", "CONFLICT"
