# ARQUITECTURA DEL SISTEMA DE DIRECCIÓN Y SIZING

## Fecha: Feb 2, 2026

## Resumen

Este documento describe cómo todos los componentes del sistema se comunican entre sí de forma coherente.

---

## 1. JERARQUÍA DE SEÑALES DE DIRECCIÓN

El `DirectionOrchestrator` (`direction_orchestrator.py`) es el **único punto de decisión** para la dirección de trading.

### Prioridades (mayor a menor):

```
100 - EXPANSION_MODE      → Desplazamiento fuerte con lock de 30 min
 90 - DISPLACEMENT        → Movimiento grande de precio (>= threshold ATR)
 80 - FAST_REVERSAL       → Vela individual de reversión (engulfing/strong body)
 70 - MICRO_MOMENTUM      → Score HH/HL/LL/LH en últimas velas 1m
 60 - STRUCTURAL_BIAS     → Bias de HTF basado en EMAs
 50 - FORBIDDEN_FALLBACK  → Cuando todo está bloqueado, usa dirección opuesta
```

### Reglas:

1. **Señal de mayor prioridad siempre gana**
2. **FORBIDDEN_DIRECTION puede vetar cualquier señal** (excepto EXPANSION_MODE)
3. **FAST_REVERSAL override MICRO_MOMENTUM** - diseñado para reaccionar rápido a reversiones
4. **Conflictos se loguean** para debugging

---

## 2. FLUJO DE DETERMINACIÓN DE DIRECCIÓN

```
┌─────────────────────────────────────────────────────────────────┐
│                    _handle_idle_state()                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 1. Check EXPANSION_MODE (strong displacement + direction lock)  │
│    → Si activo, _active_direction = expansion_direction         │
│    → Salta resto de lógica                                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. Check DISPLACEMENT (movimiento > threshold ATR)              │
│    → Si detectado, _active_direction = displacement_direction   │
│    → Salta resto de lógica                                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. Calculate micro_momentum (HH/HL scoring)                     │
│    → Score HH/HL/LL/LH de últimas 7 velas 1m                    │
│    → Requiere 4+ puntos en una dirección                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. Check FAST_REVERSAL (FastReversalDetector)                   │
│    → Detecta engulfing, strong body, momentum shift             │
│    → Si detectado, override micro_momentum                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. DirectionOrchestrator.orchestrate()                          │
│    → Recibe todas las señales                                   │
│    → Aplica prioridades                                         │
│    → Aplica vetos (forbidden_direction)                         │
│    → Retorna dirección final                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 6. _active_direction = orch_result.final_direction              │
│    → Tags actualizados con source, priority, conflicts          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. SISTEMA DE SIZING

El `SimplifiedSizer` (`simplified_sizing.py`) aplica multiplicadores en este orden:

### Multiplicadores:

```
1. BASE_RISK_PCT        → Riesgo base de la sesión
2. RR_MULT              → Ajuste por R:R esperado
3. REGIME_MULT          → Ajuste por régimen (TREND/RANGE)
4. LOSS_STREAK_MULT     → Reducción tras pérdidas consecutivas
5. SESSION_MULT         → Ajuste por sesión (ASIA/LONDON/NY)
6. EDGE_MULT            → Ajuste por tipo de edge
7. TOXIC_HOUR_MULT      → Reducción en horas tóxicas
8. TOXIC_COMBO_MULT     → Reducción en combos session+direction tóxicos
9. MOMENTUM_MULT        → Ajuste por momentum actual
```

### Fórmula Final:

```python
final_size = base_risk * RR * REGIME * LOSS * SESSION * EDGE * TOXIC_HOUR * TOXIC_COMBO * MOMENTUM
```

---

## 4. COOLDOWNS Y FILTROS

### Loss Streak Cooldown (`LossStreakCooldown`):

```
3 pérdidas → 15 min cooldown
4 pérdidas → 30 min cooldown  
5+ pérdidas → 60 min cooldown
```

### Toxic Hours:

```
Horas UTC {0, 4, 21, 23} → Sizing × 0.5
```

### Toxic Combos:

```
(ASIA, SHORT)           → Sizing × 0.3
(LONDON_NY_OVERLAP, SHORT) → Sizing × 0.3
(WEEKEND, LONG)         → Sizing × 0.3
```

---

## 5. CONFIGURACIÓN CENTRALIZADA

Todos los parámetros están en `RiskManagement` class en `trading_config.py`:

```python
# Toggles para activar/desactivar
TOXIC_HOURS_ENABLED = True
TOXIC_COMBOS_ENABLED = True
LOSS_STREAK_COOLDOWN_ENABLED = True
FAST_REVERSAL_ENABLED = True
IMMEDIATE_FLIP_ENABLED = True

# Parámetros ajustables
TOXIC_HOURS_SET = {0, 4, 21, 23}
TOXIC_HOURS_MULT = 0.5
FAST_REVERSAL_MIN_BODY_ATR = 1.2
...
```

---

## 6. DIAGRAMA DE DEPENDENCIAS

```
                ┌─────────────────────┐
                │  trading_config.py  │
                │  (RiskManagement)   │
                └─────────┬───────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
         ▼                ▼                ▼
┌─────────────────┐ ┌───────────────┐ ┌─────────────────────┐
│  simplified_    │ │  fast_        │ │  direction_         │
│  sizing.py      │ │  reversal_    │ │  orchestrator.py    │
│                 │ │  detector.py  │ │                     │
│ - ToxicHour     │ │               │ │ - DirectionSignal   │
│ - ToxicCombo    │ │ - Engulfing   │ │ - Orchestrate()     │
│ - LossCooldown  │ │ - StrongBody  │ │ - Priority rules    │
│ - Momentum      │ │ - Override    │ │                     │
└────────┬────────┘ └───────┬───────┘ └──────────┬──────────┘
         │                  │                    │
         └──────────────────┼────────────────────┘
                            │
                            ▼
                ┌─────────────────────┐
                │   trading_app.py    │
                │   (SignalEngine)    │
                │                     │
                │ - _handle_idle_     │
                │ - orchestrator      │
                │ - sizing            │
                └─────────────────────┘
```

---

## 7. CÓMO RECALIBRAR

1. **Cambiar horas tóxicas**: Editar `RiskManagement.TOXIC_HOURS_SET`
2. **Desactivar feature**: Set `XXX_ENABLED = False`
3. **Ajustar sensibilidad reversión**: Cambiar `FAST_REVERSAL_MIN_BODY_ATR`
4. **Cambiar cooldowns**: Editar `LOSS_STREAK_X_COOLDOWN_MIN`

---

## 8. LOGGING Y DEBUGGING

Tags añadidos a cada trade para análisis:

```
orchestrated_direction   → Dirección final elegida
direction_source         → Quién ganó (MICRO, FAST_REVERSAL, etc)
direction_priority       → Nivel de prioridad del ganador
direction_conflict       → Si hubo conflicto entre señales
fast_reversal_detected   → Si se detectó reversión rápida
fast_reversal_type       → ENGULFING, STRONG_BODY, MOMENTUM_SHIFT
```

---

**Última actualización: Feb 2, 2026**
