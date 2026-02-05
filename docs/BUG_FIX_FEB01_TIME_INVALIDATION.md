# FIX: TIME_INVALIDATION_MAX_LOSS Excessive Exits (Feb 01, 2026)

## 🚨 Problema Detectado

**35% de los trades (14/40)** en el período Jan 30 - Feb 01 terminaron con exit_reason = `TIME_INVALIDATION_MAX_LOSS`, lo cual es excesivo e indica un problema sistémico.

### Datos del Análisis

| Métrica | Valor |
|---------|-------|
| Trades totales | 40 |
| TIME_INVALIDATION_MAX_LOSS | 14 (35%) |
| Score = 0 en estos trades | 100% |
| MFE = 0 (nunca ganó) | 40% |
| Counter-momentum entries | 30% |

### Trades Problemáticos Identificados

| Trade | Score | MFE | Dirección | Tags Clave |
|-------|-------|-----|-----------|------------|
| 00:52 SHORT | **0** | 0.17R | SHORT | `displacement_bypass`, `fast_path` |
| 03:38 SHORT | **0** | 0.48R ✓ | SHORT | `displacement_bypass`, `fast_path` |
| 06:05 SHORT | **0** | 0.00R | SHORT | `bias_restored`, `fast_path` |
| 08:50 SHORT | **0** | 0.00R | SHORT | `fast_path`, `delta_contradicts` |
| 09:34 LONG | **0** | 0.07R | **LONG** ❌ | `bias_invalidated`, `forbidden_direction` |
| 10:33 LONG | **0** | 0.00R | **LONG** ❌ | `bias_invalidated`, `forbidden_direction` |
| 12:24 LONG | **0** | 0.00R | **LONG** ❌ | `protected_bounce`, `fast_path` |
| 14:37 SHORT | **0** | 0.37R | SHORT | `expansion_timeout`, `fast_path` |
| 23:46 SHORT | **0** | 0.16R | SHORT | `bias_invalidated`, `displacement_override` |

## 🔍 Root Cause Identificado

### 1. FAST_PATH bypassa toda validación de calidad
- Trades entran con `score=0` vía `displacement_bypass`
- No hay quality gate - si hay displacement, entra ciegamente
- Resultado: Muchas entradas de baja calidad

### 2. Entradas CONTRA-MOMENTUM
- ~30% de los TIME_INVALIDATION fueron LONG durante strong displacement SHORT
- Tags presentes: `bias_invalidated`, `forbidden_direction`, `protected_bounce`
- Regla violada: "Never fight the tape"

### 3. Timing tardío
- Varios trades con `expansion_timeout` = movimiento ya terminó
- Entrada en el peor momento del ciclo

### 4. Delta contradice dirección
- Trades con `delta_contradicts` = Volume flow opuesto al trade
- "Price can lie, volume doesn't"

## ✅ Solución Implementada

### FIX 1: Counter-Momentum Blocker en FAST_PATH
**Archivo:** `entry_orchestrator.py`

Añadido nuevo CHECK 2.5 antes de ejecutar FAST_PATH:

```python
# Check 2.5a: Si hay direction_lock opuesto → BLOCK
direction_lock = getattr(engine, '_direction_lock', None)
if direction_lock and direction_lock != disp_direction:
    logger.warning(
        f"🚫 FAST PATH BLOCKED: COUNTER-MOMENTUM | "
        f"Attempting {disp_direction} but DIRECTION_LOCK={direction_lock} | "
        f"Never fight the tape"
    )
    return None

# Check 2.5b: Si forbidden_direction = nuestra dirección → BLOCK
forbidden_dir = getattr(engine, '_forbidden_direction', None)
if forbidden_dir == disp_direction:
    logger.warning(
        f"🚫 FAST PATH BLOCKED: FORBIDDEN DIRECTION | "
        f"Attempting {disp_direction} but FORBIDDEN={forbidden_dir}"
    )
    return None

# Check 2.5c: Si expansion_direction es opuesto y aún activo → BLOCK
expansion_mode = getattr(engine, '_expansion_mode', False)
expansion_dir = getattr(engine, '_expansion_direction', None)
if expansion_mode and expansion_dir and expansion_dir != disp_direction:
    # Solo bloquear si el expansion es reciente (< 15 min)
    exp_start = getattr(engine, '_expansion_start_time', None)
    if exp_start and (datetime.now() - exp_start).total_seconds() < 900:
        logger.warning(
            f"🚫 FAST PATH BLOCKED: COUNTER-EXPANSION | "
            f"Attempting {disp_direction} but EXPANSION_MODE={expansion_dir}"
        )
        return None
```

### FIX 2: TIME_INVALIDATION más estricto para score=0
**Archivo:** `risk_engine.py`

Añadido tracking de `entry_score` en Position y lógica diferenciada:

```python
# 🔥 FIX (Feb 01, 2026): Check if this is a FAST_PATH entry (score=0)
# FAST_PATH trades are lower quality - no score validation was done
# They need TIGHTER TIME_INVALIDATION, not looser
is_fast_path_trade = self.entry_score is not None and self.entry_score == 0

# Set max loss based on market type, edge type, AND entry quality
if is_fast_path_trade and not is_displacement_trade:
    # 🔥 FAST_PATH sin DISPLACEMENT = entrada de baja calidad
    # Usar límite MÁS ESTRICTO para cortar rápido las malas entradas
    max_loss_r = -0.25  # Más estricto que normal (-0.30R)
    logger.info(
        f"📊 TIME checkpoint: FAST_PATH (score=0) mode → max_loss={max_loss_r}R (stricter) | "
        f"Low quality entry, cut fast if wrong"
    )
```

### FIX 3: Propagación de entry_score al Position
**Archivos:** `risk_engine.py`, `trading_app.py`, `entry_orchestrator.py`

- Añadido parámetro `entry_score` al constructor de `Position`
- Añadido a `create_position()` method
- Propagado desde `_current_trade_tags['entry_score']` en todas las ubicaciones

## 📊 Impacto Esperado

### Antes del Fix
| Situación | Comportamiento |
|-----------|----------------|
| LONG durante SHORT displacement | ✅ Entra (malo) |
| Entry con forbidden_direction | ✅ Entra (malo) |
| score=0 trade | -0.30R max loss |
| Counter-expansion trade | ✅ Entra (malo) |

### Después del Fix
| Situación | Comportamiento |
|-----------|----------------|
| LONG durante SHORT displacement | ❌ BLOQUEADO |
| Entry con forbidden_direction | ❌ BLOQUEADO |
| score=0 trade | **-0.25R max loss** (más estricto) |
| Counter-expansion trade | ❌ BLOQUEADO |

### Reducción Estimada de TIME_INVALIDATION
- **Antes:** ~35% de trades
- **Esperado después:** <15% de trades
- **Razón:** Las peores entradas ahora serán bloqueadas antes de ejecutar

## 🧪 Verificación

### Tests a ejecutar
1. Backtest en mismo período (Jan 30 - Feb 01) para verificar reducción
2. Verificar que trades legítimos no sean bloqueados
3. Confirmar que score=0 trades usan -0.25R limit

### Logs a verificar
- `🚫 FAST PATH BLOCKED: COUNTER-MOMENTUM`
- `🚫 FAST PATH BLOCKED: FORBIDDEN DIRECTION`
- `🚫 FAST PATH BLOCKED: COUNTER-EXPANSION`
- `📊 TIME checkpoint: FAST_PATH (score=0) mode → max_loss=-0.25R`

## 📁 Archivos Modificados

1. **`src/application/live/entry_orchestrator.py`**
   - Añadido CHECK 2.5: Counter-Momentum Blocker

2. **`src/domain/risk/risk_engine.py`**
   - Añadido `entry_score` al constructor de Position
   - Añadido a `create_position()` method
   - Lógica TIME_INVALIDATION diferenciada para score=0

3. **`src/application/live/trading_app.py`**
   - Propagación de `entry_score` a `create_position()`

---

**Autor:** Trading System  
**Fecha:** Feb 01, 2026  
**Prioridad:** ALTA (35% de trades afectados)
