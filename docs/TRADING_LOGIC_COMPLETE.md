# 📊 DOCUMENTACIÓN COMPLETA DE LÓGICA DE TRADING

## Sistema BTC Perpetual Trading - Análisis Profundo

**Fecha de Documentación:** Febrero 2026  
**Versión:** Post-Refactor Masivo (Feb 17, 2026) — ~5,700 líneas eliminadas

---

## 🏗️ ARQUITECTURA GENERAL

### Capas del Sistema

```
┌────────────────────────────────────────────────────────────────┐
│                    APPLICATION LAYER                            │
│  ┌──────────────────────┐  ┌──────────────────────┐            │
│  │    SignalEngine      │  │  EntryOrchestrator   │            │
│  │   (trading_app.py)   │  │   (entry_orch.py)    │            │
│  └──────────────────────┘  └──────────────────────┘            │
├────────────────────────────────────────────────────────────────┤
│                     DOMAIN LAYER                                │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │
│  │   TRADING   │ │    RISK     │ │   MARKET    │               │
│  │ EdgeManager │ │ RiskEngine  │ │  Regime/    │               │
│  │ Scoring     │ │ Trailing    │ │  Filters    │               │
│  │ State       │ │ Position    │ │  Structure  │               │
│  └─────────────┘ └─────────────┘ └─────────────┘               │
├────────────────────────────────────────────────────────────────┤
│                   INFRASTRUCTURE LAYER                          │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │
│  │  Exchange   │ │  Monitoring │ │   Storage   │               │
│  │   Client    │ │   Health    │ │   Events    │               │
│  └─────────────┘ └─────────────┘ └─────────────┘               │
└────────────────────────────────────────────────────────────────┘
```

---

## 🔄 FLUJO DE DECISIÓN DE TRADING

### 1. Ciclo Principal de Evaluación

```
TICK RECIBIDO
     │
     ▼
┌─────────────────┐
│ StateMachine    │──── Estado != IDLE? ──▶ Gestionar Posición
│ Check State     │
└────────┬────────┘
         │ Estado = IDLE
         ▼
┌─────────────────┐
│ Edge Manager    │──── NO_EDGE? ──▶ Skip (no evaluar nada)
│ Check Edge      │
└────────┬────────┘
         │ EDGE_ACTIVE o EDGE_PROVISIONAL
         ▼
┌─────────────────┐
│ Guard Guards    │──── BLOCK? ──▶ Aplicar Cooldown
│ (15+ checks)    │
└────────┬────────┘
         │ PASS (con penalties acumuladas)
         ▼
┌─────────────────┐
│ Entry Scorer    │──── Score < 4.5? ──▶ No Trade
│ (4 bloques)     │
└────────┬────────┘
         │ Score >= 4.5 (graduated sizing)
         ▼
┌─────────────────┐
│ Position Sizing │──── Size = 0? ──▶ No Trade
│ (RiskEngine)    │
└────────┬────────┘
         │ Size > min
         ▼
┌─────────────────┐
│ Execute Entry   │──▶ STATE = WAIT_FILL
│ (Limit Order)   │
└─────────────────┘
```

---

## 🎯 SISTEMA DE EDGE

### EdgeState - Estados del Edge

| Estado | Descripción | Acción |
|--------|-------------|--------|
| `NO_EDGE` | Sin oportunidad detectada | No evaluar nada |
| `EDGE_ARMED` | BOS detectado, esperando pullback | Monitorear |
| `EDGE_PROVISIONAL` | Entrada barata para probar dirección | Size 30%, TP 0.4-0.8R |
| `EDGE_ACTIVE` | Ventana de entrada abierta | Ejecutar con size normal |
| `EDGE_EXPIRED` | Oportunidad consumida/expirada | Reset, esperar nuevo edge |

### EdgeType - Tipos de Edge

| Tipo | Prioridad | Descripción | Timeout |
|------|-----------|-------------|---------|
| `DISPLACEMENT` | 100 | Movimiento fuerte (≥1.5x ATR) | 20 min (FIX #230) |
| `BREAKOUT` | 80 | Ruptura de compresión BB | 10 min (FIX #230) |
| `RANGE` | 60 | Extremo de rango (≤15% o ≥85%) | 10 min (FIX #230) |
| `TREND_BOS` | 40 | Break of Structure con pullback | 15 min |
| `TREND_CONSENSUS` | 20 | Entrada directa en consenso | 15 min |

### Criterios de Activación por Tipo

#### DISPLACEMENT EDGE
```python
DisplacementEdgeCriteria:
    MIN_DISPLACEMENT_ATR_MULT = 1.2   # Mínimo para detectar
    STRONG_DISPLACEMENT_MULT = 1.5    # Para EXPANSION_MODE
    
    # ZONAS DE ENTRADA:
    ZONA 1 (bars 0-1): FAST PATH - Entry directo, 25-40% size
    ZONA 2 (bars 2-10): PULLBACK - Entry en retroceso 15-45%, 40-60% size
    ZONA 3 (bars >15): CHASE BLOCK - NO PERSEGUIR
    
    EDGE_TIMEOUT_MIN = 20             # 20 min (FIX #230 TTL radical)
    INVALIDATION_OPPOSITE_MULT = 0.6  # 60% en contra = invalida
```

#### RANGE EDGE
```python
RangeEdgeCriteria:
    EXTREME_HIGH_PCT = 85.0   # ≥85% = extremo alto (SHORT)
    EXTREME_LOW_PCT = 15.0    # ≤15% = extremo bajo (LONG)
    
    PROBING_ENABLED = True
    PROBING_SIZE_MULT = 0.25  # 25% size
    PROBING_MIN_SCORE = 4
    
    RANGE_SIZE_MAX = 0.30     # Max 30% size en rango
    RANGE_EDGE_TIMEOUT_MIN = 10  # FIX #230
```

#### BREAKOUT EDGE
```python
BreakoutEdgeCriteria:
    BB_WIDTH_PERCENTILE_MAX = 25      # BB squeeze
    VOLUME_DECLINE_RATIO = 0.70       # Volumen ≤70%
    BREAKOUT_CONFIRMATION_CANDLES = 2
    
    MIN_TARGET_R = 2.0   # Mínimo 2R
    IDEAL_TARGET_R = 3.0
    BREAKOUT_TIMEOUT_MIN = 10  # FIX #230
```

---

## 📊 SISTEMA DE SCORING

### Arquitectura de 4 Bloques + Synergy

```
SCORE TOTAL = STRUCTURE + TIMING + MOMENTUM + CONTEXT + SYNERGY
              (0-2)      (0-2)    (0-2)       (0-2)     (0-1)
                                                        
              MAX POSIBLE = 9 puntos
              THRESHOLD   = 4.5 puntos (FIX #232)
```

### Score-Based Sizing Tiers (FIX #232)

| Score | Sizing | Multiplicador |
|-------|--------|---------------|
| < 4.5 | **BLOCK** | 0.0× |
| 4.5 - 5.5 | Probe | 0.25× |
| 5.5 - 6.0 | Reduced | 0.50× |
| ≥ 6.0 | Full | 1.0× |

### Bloque 1: STRUCTURE (0-2) - EL ÚNICO GATE

| Puntos | Condición |
|--------|-----------|
| 0 | Contra HTF (precio vs EMA50_1h mal alineado) → PENALTY -3 |
| 1 | HTF alineado pero estructura 5m débil |
| 2 | HTF alineado + estructura 5m limpia |

**Bypasses de Structure Gate:**
- `DISPLACEMENT_EDGE` activo → bypass (mercado ya votó)
- `EXPANSION_MODE` activo → bypass (tendencia confirmada)
- `ADX ≥ 50` → bypass (tendencia muy clara)

### Bloque 2: TIMING (0-2)

| Puntos | Condición |
|--------|-----------|
| 0 | Sin BOS confirmado, timing pobre |
| 1 | BOS detectado pero no en close |
| 2 | BOS confirmado en close de vela |

**Coherencia Semántica:** Si Structure=0, Timing se capea a 1.

### Bloque 3: MOMENTUM (0-2)

| Puntos | Condición |
|--------|-----------|
| 0 | ADX < 20 o slope negativo |
| 1 | ADX 20-25 o slope plano |
| 2 | ADX > 25 + slope positivo + volumen |

### Bloque 4: CONTEXT (0-2)

| Puntos | Condición |
|--------|-----------|
| 0 | Session muerta o vol extrema |
| 1 | Session activa, vol normal |
| 2 | Session premium (London/NY overlap) |

### Synergy Bonus (+1)

Se otorga cuando: `HTF_CLEAN + BOS_CONFIRMED`

---

## ⚙️ SISTEMA DE PENALTIES

### Arquitectura de Penalties (FIX #81)

**Filosofía:** No bloquear → hacer caro equivocarse.

```python
class PenaltySource(Enum):
    HIGH_SPREAD = "HIGH_SPREAD"
    DELTA_CONTRADICT = "DELTA_CONTRADICT"
    SHORT_OBSTINACY = "SHORT_OBSTINACY"
    DISPLACEMENT_CHASE = "DISPLACEMENT_CHASE"
    WEAK_EDGE = "WEAK_EDGE"
    TOXIC_COMBO = "TOXIC_COMBO"
    ABSORPTION_CONTRARY = "ABSORPTION_CONTRARY"  # FIX #234
```

### Jerarquía de Penalties

| Categoría | Penalties | Regla |
|-----------|-----------|-------|
| **CONTEXT** | regime, volatility, micro_trend, correlation | Solo aplica el PEOR |
| **EXECUTION** | cost, spread, timing, overtrade | Suma todos |

**Cap Global (FIX #131):** Penalty máxima = -3.0 puntos

### Penalties por Guard

| Guard | Trigger | Size Mult | Score Adj |
|-------|---------|-----------|-----------|
| ADX Divergence | HTF trending + LTF dead | 0.50 | -1.0 |
| Delta SEVERE | Delta ±5x vs bias | 0.50 | -1.0 |
| Delta MODERATE | Delta ±3-5x vs bias | 0.70 | -0.5 |
| Short Obstinacy | 3+ bullish signals | 0.25 | -1.5 |
| Displacement Chase | >1.5x displacement | 0.40-0.80 | -0.5 a -2.0 |
| Spread High | Spread > threshold | configurable | configurable |
| Absorption + Delta MOD | FIX #234: delta 3-5x + absorption | 0.50 | -1.5 |
| Absorption Only | FIX #234: absorption sin delta severe | 0.60 | -1.0 |

---

## 🛡️ SISTEMA DE GUARDS

### HARD BLOCKS (únicas situaciones que bloquean)

| Guard | Condición | Razón |
|-------|-----------|-------|
| Kill Switch | 6+ losses diarios | Protección de capital |
| Catastrophic Event | Funding extremo, volatility >5x | Evento de mercado |
| TrendPhase Block | EXHAUSTION/DISTRIBUTION + same direction | FIX #220 - No entrar al final de tendencias |
| Config Error | TP < RR_HARD_FLOOR | Error de configuración |
| POST_ONLY Spam | 3+ rechazos consecutivos | Mercado muy rápido |
| Location RANGE Veto | En rango + lejos de extremos | Sin edge en rango medio |
| Absorption + Delta SEVERE | Delta ≥5x + Absorption contrarios | FIX #234 - Solo SEVERE = hard block |

### SOFT PENALTIES (no bloquean, penalizan)

Todo lo demás se convierte en penalty:
- ADX divergence
- Spread alto
- Delta contradict
- Late confirmation
- Micro trend oppose
- **Absorption (sin delta severe)** — FIX #234: -1.0 a -1.5 score, 0.50-0.60× size

---

## 📈 SISTEMA DE RÉGIMEN

### 3-Layer Regime Fusion Architecture

```
┌─────────────────────────────────────────────┐
│             CAPA 1: MACRO                    │
│  EMA50 vs EMA200 + +DI/-DI + Slope EMA50    │
│  → macro_score ∈ [-2.5, +2.5]               │
│  → macro_dir: -1 (BEAR), 0 (NEUTRAL), +1    │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│             CAPA 2: MICRO                    │
│  Structure + Displacement + Delta + Absorb   │
│  → direction: LONG/SHORT/NEUTRAL            │
│  → confidence: 0-1                          │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│             CAPA 3: FUSIÓN                   │
│  alignment = macro_dir × micro_dir          │
│                                             │
│  alignment > 0  → size=1.0, RR_min=1.0     │
│  alignment < 0  → size=0.3, RR_min=2.0     │
│  alignment == 0 → size=0.6, RR_min=1.5     │
│                                             │
│  ÚNICO BLOCK: macro_strength > 2.0 AND      │
│               alignment < 0 AND              │
│               no_displacement                │
│  = "Fade estúpido" → BLOCK                  │
└─────────────────────────────────────────────┘
```

### TrendPhase - Ciclo de Tendencia

```
ACCUMULATION → TREND_BIRTH → TREND_ACCELERATION → TREND_MATURE
                                                       │
                                                       ▼
RANGE_BOUND ← TREND_DEATH ← DISTRIBUTION ← TREND_EXHAUSTION
```

| Fase | Descripción | Trading |
|------|-------------|---------|
| `ACCUMULATION` | Pre-trend, building pressure | WAIT |
| `TREND_BIRTH` | Breakout inicial | AGGRESSIVE ENTRY |
| `TREND_ACCELERATION` | Momentum creciente | FULL SIZE |
| `TREND_MATURE` | Tendencia establecida | TRAILING |
| `TREND_EXHAUSTION` | Señales de agotamiento | REDUCE/EXIT |
| `DISTRIBUTION` | Pre-reversal | NO NEW ENTRIES |
| `TREND_DEATH` | Failure/reversal | REVERSAL ALLOWED |
| `RANGE_BOUND` | Sin tendencia | RANGE STRATEGY |

### DailyRegimeDetector - Régimen Diario

```python
class DailyRegimeDetector:
    # Clasificación al inicio del día
    RANGE_ATR_THRESHOLD = 1.0   # range < 1.0x ATR = RANGE_DAY
    TREND_ATR_THRESHOLD = 1.5   # range > 1.5x ATR = TREND_DAY
    ADX_RANGE_THRESHOLD = 18
    ADX_TREND_THRESHOLD = 20
    
    DAILY_REGIME_CONFIG = {
        'TREND_DAY': {
            'allow_runners': True,
            'sl_atr_mult': 3.0,    # Stops amplios
            'tp_r_target': 2.0,    # Targets altos
            'max_trades': 4,
            'size_mult': 1.0,
        },
        'RANGE_DAY': {
            'allow_runners': False, # Quick exits
            'sl_atr_mult': 2.0,    # Stops tight
            'tp_r_target': 1.0,    # Quick targets
            'max_trades': 3,
            'size_mult': 0.5,
        }
    }
```

---

## 💰 GESTIÓN DE RIESGO

### Position Class - Invariantes

```python
class Position:
    # Invariantes que SIEMPRE deben cumplirse
    system_side: str        # 'LONG' o 'SHORT'
    exchange_side: str      # Debe coincidir con system_side
    
    entry_price: float
    stop_loss: float        # SL actual
    take_profit: float      # TP actual
    
    mfe: float              # Maximum Favorable Excursion
    mae: float              # Maximum Adverse Excursion
    
    # Validación continua
    def validate_integrity(self):
        assert self.system_side == self.exchange_side
        if self.system_side == 'LONG':
            assert self.stop_loss < self.entry_price
        else:
            assert self.stop_loss > self.entry_price
```

### Hybrid ATR Trailing Stop

#### R-Lock Tiers por Régimen

**RANGE (conservador):**
```python
R_LOCK_TIERS_RANGE = [
    (0.2, -0.25, 0),  # +0.2R MFE → max loss -0.25R
    (0.3, -0.20, 1),  # +0.3R → max loss -0.20R
    (0.5, -0.10, 2),  # +0.5R → max loss -0.10R
    (0.8, 0.0, 3),    # BE at 0.8R
    (1.0, -0.10, 4),  # Lock -0.1R at 1.0R
    (1.2, 0.0, 5),    # Lock BE at 1.2R
    (1.5, 0.40, 6),   # Lock +0.4R at 1.5R
    (1.8, 0.70, 7),   # Lock +0.7R at 1.8R
    (2.2, 1.10, 8),   # Lock +1.1R at 2.2R
]
```

**TREND (más espacio):**
```python
R_LOCK_TIERS_TREND = [
    (0.5, -0.40, 0),  # Solo proteger full loss en +0.5R
    (0.8, -0.20, 1),  # +0.8R → max loss -0.20R
    (1.0, 0.20, 2),   # Lock +0.2R at 1.0R
    (1.2, 0.50, 3),   # Lock +0.5R at 1.2R
    (1.5, 0.90, 4),   # Lock +0.9R at 1.5R
    (1.8, 1.20, 5),   # Lock +1.2R at 1.8R
    (2.2, 1.60, 6),   # Lock +1.6R at 2.2R
    (3.0, 2.20, 7),   # Lock +2.2R at 3.0R (runners)
]
```

#### ATR K-Factors

```python
K_FACTORS = {
    TREND: 1.5,        # Pullbacks normales ~1 ATR
    RANGE: 1.3,        # Algo de espacio
    VOLATILITY: 2.0,   # Solo volatility merece espacio
    CONSERVATIVE: 1.2, # Proteger profit rápido
}

# BOS Quality Multiplier
BOS_QUALITY_K_MULT = {
    'STRONG': 1.15,    # +15% K = más espacio para runners
    'NORMAL': 1.0,
    'WEAK': 0.90,      # -10% K = proteger rápido
}
```

### Fórmula Final del Stop

```python
# Para LONG:
final_stop = max(r_locked_stop, atr_trail_stop)

# Para SHORT:
final_stop = min(r_locked_stop, atr_trail_stop)

# ATR Trail (solo activo después de MFE >= 0.2R):
atr_trail_stop = swing_high - (k * atr)  # LONG
atr_trail_stop = swing_low + (k * atr)   # SHORT
```

---

## 🎚️ FILTROS DE ENTRADA

### Filter Pipeline (7 filtros)

| Filtro | Función | Acción |
|--------|---------|--------|
| **BB Width** | Detecta compresión Bollinger | Score adj |
| **Volatility Shock** | TrueRange > k*ATR | Block/Penalty |
| **Macro Trend** | EMA200 en 15m + 1h | Score adj |
| **Post-Sweep** | Break+retest después de sweep | Score bonus |
| **Cost Filter** | Spread vs risk | Size mult |
| **Overtrade** | Misma zona prevention | Block |
| **BTC Correlation** | Macro context | Score adj |

### ADX Thresholds (Single Source of Truth)

```python
class ADXThresholds:
    RANGE = 18              # < 18 = RANGE/CHOP
    TRANSITION = 20         # 18-20 = cautela
    TREND = 20              # > 20 = TREND mode
    STRONG_TREND = 25       # > 25 = full conviction
    
    INTRADAY_EDGE_MIN = 25  # Mínimo para intraday edge
    DISPLACEMENT_EDGE = 20  # Para displacement edges
    RANGE_EDGE_MAX = 25     # RANGE edge cuando ADX < 25
```

### Location Filter

```python
class LocationFilters:
    EXTREME_LOW_PERCENTILE = 10   # ≤10% = extremo bajo
    EXTREME_HIGH_PERCENTILE = 90  # ≥90% = extremo alto
    
    # En RANGE sin extremo → BLOCK
    # En extremo → ALLOW con bonus
```

---

## 🔀 GESTIÓN DE DIRECCIÓN

### DirectionStateManager

```python
class DirectionState:
    structural_bias: str | None     # HTF (1h) bias
    active_direction: str | None    # Momentum actual
    forbidden_direction: str | None # Bloqueada por invalidación
    
    displacement_bypass: bool       # Override por displacement
    strong_displacement: bool       # Override BIAS FLIP
    
    expansion_mode: bool            # Mercado en expansión
    expansion_direction: str | None
    
    direction_lock: str | None      # Lock post-displacement
    direction_lock_expires_at: datetime | None
```

### BIAS FLIP Recovery

```python
# Cuando el bias se invalida:
1. Guardar bias anterior en _bias_before_invalidation
2. Guardar nivel de invalidación en _bias_invalidation_level
3. Marcar _forbidden_direction

# Para restaurar:
if precio reclaims _bias_invalidation_level:
    _structural_bias = _bias_before_invalidation
    _forbidden_direction = None
```

### Expansion Mode

```python
# Activación:
if displacement >= 1.5x ATR:
    _expansion_mode = True
    _expansion_direction = bias
    _expansion_timeout_minutes = 20 * volatility_ratio

# Durante EXPANSION:
- forbidden_direction SUSPENDIDO
- BIAS FLIP threshold 3x normal
- Solo invalida: displacement opuesto, estructura HTF, timeout
```

### Direction Lock

```python
# Post-displacement:
_direction_lock = bias
_direction_lock_timeout_minutes = 15 * volatility_ratio

# Durante LOCK:
❌ No bias flip
❌ No forbid por retrocesos pequeños
✅ Solo invalidación MAYOR (3x displacement inicial)
```

---

## 📊 STATE MACHINE

### BotState - Estados Operacionales

| Estado | Descripción | Transiciones Válidas |
|--------|-------------|---------------------|
| `IDLE` | Esperando setup | → WAIT_FILL, OBSERVATION, OFF |
| `WAIT_FILL` | Orden pendiente | → MANAGE (fill, **antes** de SL/TP - FIX #233), IDLE (cancel) |
| `HARD_LOCK` | Transición - nada puede actuar | → MANAGE, IDLE |
| `MANAGE` | Gestionando posición | → IDLE (close) |
| `OBSERVATION` | Penalización activa | → IDLE |
| `OFF` | Kill switch activado | Manual reset |

### Cooldown Management

```python
# Tiers de cooldown por pérdidas consecutivas:
3 losses → 15 min cooldown
4 losses → 60 min cooldown
5 losses → 180 min cooldown
6 losses → KILL SWITCH (daily)

# Anti-revenge trading:
2 consecutive losses → 0.50x sizing
3+ consecutive losses → 0.25x sizing
```

### Daily Drawdown Protection

```python
daily_drawdown_limit_pct = 0.03  # 3% max DD diario
if daily_drawdown_pct >= limit:
    is_daily_halt = True  # NO recover, NO retry
```

---

## 🧮 CONFIGURACIÓN DE ASIMETRÍA

### Estrategia: HIGH WINRATE + CONTROLLED LOSSES

```python
class AsymmetryConfig:
    # TARGETS
    BASE_TP_R = 1.0           # Target base
    RANGE_DAY_TP_R = 0.8      # Scalp en range
    TREND_DAY_TP_R = 1.5      # Runner en trend
    
    # STOPS
    MAX_STOP_R = 1.0          # Stop máximo = TP
    
    # TIME INVALIDATION
    TIME_INVALIDATION_CANDLES = 15      # 15 min
    TIME_INVALIDATION_EXIT_R = 0.0      # Target: BE
    TIME_INVALIDATION_MAX_LOSS_R = -0.35  # Max loss
    
    # LÍMITE ABSOLUTO
    ABSOLUTE_MAX_LOSS_R = -0.40  # NUNCA más de -0.40R
```

---

## 🔧 FIXES CRÍTICOS IMPLEMENTADOS

### FIX #76 - Structure como Penalty
**Antes:** Structure=0 → HARD BLOCK  
**Ahora:** Structure=0 → Penalty -3.0  
**Razón:** Paralysis cuando FIX #70 bloquea LONGs + Structure bloquea SHORTs

### FIX #81 - Sistema de Penalties
**Antes:** Guards individuales bloquean  
**Ahora:** Guards acumulan penalties en RiskPenaltyAccumulator  
**Razón:** "Death by a thousand cuts" prevenible

### FIX #131 - Edge Floor Protection
**Problema:** Edges válidos destruidos por penalties  
**Solución:** Score floor de 4.0 cuando hay edge activo válido

### FIX #147 - Delta + Absorption Combo
**Trigger:** Delta SEVERE + Absorption contrarios  
**Acción:** HARD BLOCK (no penalty)  
**Razón:** Mercado claramente contradice

### FIX #162 - Micro Trend en Trends
**Antes:** Penalizar micro contrario siempre  
**Ahora:** ADX ≥ 35 → micro contrario = pullback = BONUS no penalty  
**Razón:** Pullback es oportunidad de entrada

### FIX #170 - Timezone Compatibility
**Bug:** datetime timezone-aware vs naive mixing  
**Fix:** ensure_utc() en todas las comparaciones

### FIX #220 - TrendPhase Hard Block
**Trigger:** EXHAUSTION/DISTRIBUTION/DEATH + same direction as trend  
**Acción:** HARD BLOCK (no entrar al final de tendencias)  
**Excepción:** TREND_DEATH reversal = permitido con penalty

### FIX #232 - Score Block Floor Rebalance
**Antes:** SCORE_BLOCK_FLOOR=5.5, PROBE=6.0, REDUCED=6.5, FULL≥6.5  
**Ahora:** SCORE_BLOCK_FLOOR=4.5, PROBE=5.5, REDUCED=6.0, FULL≥6.0  
**Razón:** Con edge floor de 4.0, scores 4.0-5.4 SIEMPRE bloqueados → sistema nunca operaba. Combinado con probe sizing + profit minimum = trampa mortal.

### FIX #233 - WAIT_FILL Infinite Loop
**Bug:** `pending_order_id=None` y `transition_to(MANAGE)` ocurrían DESPUÉS del SL/TP placement (3 retries + backoff ≈ >15s). Tick timeout cancelaba → estado quedaba en WAIT_FILL → siguiente tick re-procesaba el fill → posición creada 8+ veces.  
**Fix:** Transición a MANAGE ANTES de SL/TP placement. SL/TP se ejecuta ya en estado MANAGE.

### FIX #234 - Absorption Soft Penalty
**Antes:** Absorption (cualquier conf ≥50%) → HARD BLOCK  
**Ahora:** Solo DELTA_SEVERE + Absorption → HARD BLOCK. El resto → penalty (-1.0 a -1.5 score, 0.50-0.60× size)  
**Razón:** Absorption con 50% confianza sin delta mataba setups válidos.

### FIX #235 - MIN_PROFIT_FLOOR Lowered
**Antes:** $0.50 mínimo  
**Ahora:** $0.25 mínimo  
**Razón:** Con $245 capital y probe sizing (0.25×), profit esperado ≈$0.30 → bloqueado por $0.50. Paradoja: el sistema forzaba probe sizing pero luego bloqueaba probes por profit insuficiente.

### FIX #236 - Stale Fallback Penalty Cap
**Antes:** >12min fallback → -8.0 penalty (score 0 → guaranteed block)  
**Ahora:** >12min fallback → -2.5 penalty (max). Curva 8-12min suavizada.  
**Razón:** -8.0 era desproporcionado. La primary path (distance-based) ya capea a -4.0.

### FIX #237 - Move Consumption Bypass for Displacement
**Antes:** Move consumed > 90% → HARD BLOCK (aplica a TODO, incluido displacement)  
**Ahora:** DISPLACEMENT edges exentos del move consumption check  
**Razón:** El threshold de displacement (2.8× ATR) siempre supera el expected_move (1.2× ATR). Toda entrada por displacement estaba "consumed > 200%" → bloqueada. El chase penalty ya cubre este riesgo.

### FIX #238 - Displacement Exempt from Distance-Stale
**Antes:** Distance-based stale: 2.8 ATR → -4.0 penalty. PLUS chase: -1.5. Total = -5.5  
**Ahora:** DISPLACEMENT edges usan SOLO time-based stale (cap -1.5). Chase penalty sigue.  
**Razón:** La distancia ES la señal del displacement. Penalizar distancia en un displacement es circular. Chase + stale medían lo mismo y se sumaban, matando toda entrada.

### FIX #239 - Post-Penalty Edge Floor
**Antes:** Edge floor (4.0) se aplicaba en scorer ANTES de chase+stale penalties. Score 4.0 - 3.0 penalties = 1.0 → BLOCKED.  
**Ahora:** Edge floor se aplica DESPUÉS de TODOS los penalties en el orchestrator. Score mínimo = 4.0 si hay edge activo válido.  
**Razón:** El floor era dead code — downstream penalties lo destruían.

### FIX #240 - Adaptive Candle Gate During Active Edge
**Antes:** Price move threshold = 0.5 ATR, max ticks = 30 (siempre)  
**Ahora:** Con edge activo: threshold = 0.2 ATR, max ticks = 10 → 3× más evaluaciones  
**Razón:** Con 30 ticks entre evaluaciones, el sistema solo evaluaba ~2 veces/minuto. En un displacement que mueve $500 en 60s, 2 evaluaciones son insuficientes.

### FIX #241 - Edge Origin Refresh on Continuation
**Antes:** Edge origin se fijaba al activarse y nunca cambiaba durante 15 min  
**Ahora:** Si se detecta continuation displacement en la misma dirección, el origin se actualiza al precio actual y el timer se resetea  
**Razón:** El edge se volvía "stale" contra un origin lejano, cuando el mercado seguía dando señales frescas.

---

## 📁 ESTRUCTURA DE ARCHIVOS CLAVE

```
src/
├── application/live/
│   ├── trading_app.py      # SignalEngine - orquestador principal (9283 líneas)
│   ├── entry_orchestrator.py # Guards + Scoring + Sizing (4015 líneas)
│   └── direction_state_manager.py
│
├── domain/trading/
│   ├── edge_state_manager.py  # Edge lifecycle (3212 líneas)
│   ├── scoring/
│   │   └── entry_scorer.py    # 4-block scoring (578 líneas)
│   ├── state/
│   │   └── state_machine.py   # BotState machine (562 líneas)
│   └── strategy/
│
├── domain/risk/
│   ├── risk_engine.py      # Position + SL/TP + sizing (3794 líneas)
│   └── trailing_stop.py    # Hybrid ATR trailing (508 líneas)
│
├── domain/market/
│   ├── regime/
│   │   ├── trend_intelligence.py  # TrendPhase detection
│   │   └── regime_fusion.py       # 3-Layer fusion
│   ├── filters/
│   │   └── filter_pipeline.py     # 7 filtros
│   └── absorption_detector.py
│
└── config/
    ├── trading_config.py    # TODAS las configuraciones (3419 líneas)
    └── unified_constants.py # Constantes centralizadas

# ARCHIVOS ELIMINADOS (Feb 17, 2026):
# - src/infrastructure/monitoring/advanced_analytics.py (1217 líneas - zero imports)
# - src/domain/market/structure/htf_overextension.py (955 líneas - dead code)
# - src/infrastructure/monitoring/opportunity_tracker.py (987 líneas - zero value)
```

---

## 🔑 CONSTANTES CRÍTICAS

```python
# Entry Threshold (FIX #232)
SCORE_BLOCK_FLOOR = 4.5   # Block below this
SCORE_PROBE_MAX = 5.5     # 4.5-5.5 = probe (0.25×)
SCORE_REDUCED_MAX = 6.0   # 5.5-6.0 = reduced (0.50×)
# Above 6.0 = full (1.0×)

# Profit Minimum (FIX #235)
MIN_PROFIT_FLOOR = 0.25   # $0.25 min (was $0.50)

# Edge Timeouts (FIX #230 - TTL Radical)
DISPLACEMENT_TIMEOUT = 20  # minutos (was 90)
BREAKOUT_TIMEOUT = 10      # was 30
RANGE_TIMEOUT = 10         # was 20
TREND_TIMEOUT = 15

# Risk Limits
MAX_DAILY_DRAWDOWN = 3%
KILL_SWITCH_LOSSES = 6
ABSOLUTE_MAX_LOSS_R = -0.40

# Stale Penalty (FIX #236)
STALE_FALLBACK_MAX = -2.5  # Was -8.0
STALE_DISTANCE_MAX = -4.0  # Primary path cap

# ADX Thresholds
ADX_RANGE = 18
ADX_TREND = 20
ADX_STRONG = 25

# Regime Fusion
ALIGNED_SIZE_MULT = 1.0
MISALIGNED_SIZE_MULT = 0.3
NEUTRAL_SIZE_MULT = 0.6
```

---

## 📋 CHECKLIST DE ENTRADA

Para que un trade se ejecute, debe pasar:

1. ✅ **Edge activo** (EDGE_ACTIVE o EDGE_PROVISIONAL)
2. ✅ **StateMachine = IDLE** (no en cooldown, no en posición)
3. ✅ **Guards no BLOCK** (solo penalties acumuladas)
4. ✅ **Score ≥ 4.5** (FIX #232: graduated sizing — 4.5=probe, 5.5=reduced, 6.0+=full)
5. ✅ **TrendPhase OK** (no EXHAUSTION/DISTRIBUTION/DEATH en misma dir)
6. ✅ **Location OK** (si RANGE, debe estar en extremo)
7. ✅ **Size > mínimo** (después de multiplicadores)
8. ✅ **Daily DD < 3%** (no halt activo)
9. ✅ **Profit ≥ $0.25** (FIX #235: mínimo viable)

---

*Documentación actualizada — Febrero 17, 2026 — Post Refactor Masivo (~5,700 líneas eliminadas)*
