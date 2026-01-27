# 📊 REPORTE COMPLETO DEL SISTEMA DE TRADING BTC
## FideiRatioCapital - Versión Enero 27, 2026

---

# 1. ARQUITECTURA GENERAL

## 1.1 Diagrama ASCII del Flujo de Datos

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           KRAKEN FUTURES EXCHANGE                               │
│                           (WebSocket + REST API)                                │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         DATA INGESTION LAYER                                    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐                 │
│  │ KrakenWebSocket │  │ OrderBookMgr    │  │ ExternalData    │                 │
│  │ (Trades/Candles)│  │ (Depth/Spread)  │  │ (Funding/Macro) │                 │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘                 │
└───────────┼────────────────────┼────────────────────┼────────────────────────────┘
            │                    │                    │
            ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    SYNCHRONIZED MULTI-TIMEFRAME BUFFER                          │
│           ┌──────────┐    ┌──────────┐    ┌──────────┐                         │
│           │   1m     │    │   5m     │    │   1h     │                         │
│           │ (200+)   │    │ (50+)    │    │ (200+)   │                         │
│           └────┬─────┘    └────┬─────┘    └────┬─────┘                         │
└────────────────┼───────────────┼───────────────┼────────────────────────────────┘
                 │               │               │
                 └───────────────┼───────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SIGNAL ENGINE (Core)                                  │
│                                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────────┐  │
│   │                    FILTER HIERARCHY (Top→Bottom)                        │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ KILL         │ → Black Swan, Max DD, Circuit Breakers               │  │
│   │  │ CONDITIONS   │                                                       │  │
│   │  └──────┬───────┘                                                       │  │
│   │         ▼                                                               │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ HTF FILTER   │ → 1H EMA50/200, ADX>18, Bias Direction               │  │
│   │  │ (1H)         │                                                       │  │
│   │  └──────┬───────┘                                                       │  │
│   │         ▼                                                               │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ LOCATION     │ → 60-day Range %, Headroom, Extreme Veto             │  │
│   │  │ FILTER       │                                                       │  │
│   │  └──────┬───────┘                                                       │  │
│   │         ▼                                                               │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ ENTRY GATE   │ → Session Quality, RR Floor, Timing Score            │  │
│   │  │ (HARD BLOCKS)│                                                       │  │
│   │  └──────┬───────┘                                                       │  │
│   │         ▼                                                               │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ SCORING      │ → 4 Blocks (Structure, Timing, Momentum, Context)    │  │
│   │  │ SYSTEM       │ → Score 0-9, Threshold ~4.0 (Discovery)              │  │
│   │  └──────┬───────┘                                                       │  │
│   │         ▼                                                               │  │
│   │  ┌──────────────┐                                                       │  │
│   │  │ CHASE/RR     │ → Chase Penalty, Worst-Case RR, Cost Validation      │  │
│   │  │ VALIDATION   │                                                       │  │
│   │  └──────────────┘                                                       │  │
│   └─────────────────────────────────────────────────────────────────────────┘  │
│                                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────────┐  │
│   │                        STATE MACHINE                                    │  │
│   │                                                                         │  │
│   │      ┌───────┐        ┌───────────┐       ┌────────┐                   │  │
│   │      │ IDLE  │───────▶│ WAIT_FILL │──────▶│ MANAGE │                   │  │
│   │      └───┬───┘        └─────┬─────┘       └────┬───┘                   │  │
│   │          │                  │                  │                        │  │
│   │          │    timeout/      │                  │ TP/SL/Exit             │  │
│   │          │    cancel        │                  │                        │  │
│   │          ◀──────────────────┴──────────────────┘                        │  │
│   │                                                                         │  │
│   │      ┌─────────────┐                                                    │  │
│   │      │ OBSERVATION │ ← (recovery mode after losses)                     │  │
│   │      └─────────────┘                                                    │  │
│   └─────────────────────────────────────────────────────────────────────────┘  │
│                                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────────┐  │
│   │                        RISK ENGINE                                      │  │
│   │  • Position Sizing (Risk %, Leverage, Multipliers)                      │  │
│   │  • Stop Loss Calculation (ATR-based, Support/Resistance)                │  │
│   │  • Take Profit (1.0R target, scaled partials)                           │  │
│   │  • Trailing Stop (Hybrid ATR + R-Stepped)                               │  │
│   │  • Time Invalidation (Intelligent exit after N candles)                 │  │
│   │  • WORST_TRADE_PREVENTION (Max -0.35R per trade)                        │  │
│   └─────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         EXECUTION ENGINE                                        │
│                  (REST API → Kraken Futures)                                    │
│  • LIMIT orders (POST_ONLY default)                                            │
│  • MARKET orders (on high momentum)                                            │
│  • Exchange SL/TP placement                                                     │
│  • Position reconciliation                                                      │
└─────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         OUTPUT & PERSISTENCE                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐           │
│  │ Telegram    │  │ TradeLogger │  │ Prometheus  │  │ Health      │           │
│  │ Notifier    │  │ (Parquet)   │  │ Metrics     │  │ Endpoints   │           │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘           │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 1.2 Componentes Principales

| Componente | Archivo | Responsabilidad |
|------------|---------|-----------------|
| **SignalEngine** | `trading_app.py` | Core del sistema, orquesta filtros, estados, y ejecución |
| **EntryOrchestrator** | `entry_orchestrator.py` | Flujo completo de entrada (guards, score, sizing, execution) |
| **EntryValidators** | `entry_validators.py` | Chase, RR, Cost, Momentum validation |
| **RiskEngine** | `risk_engine.py` | Position sizing, SL/TP, trailing stops |
| **ActivityTracker** | `activity_tracker.py` | Single source of truth para tiempo desde último trade |
| **StateMachine** | `state_machine.py` | Estados IDLE, WAIT_FILL, MANAGE, OBSERVATION |

## 1.3 Patrón de Estados

```
IDLE ────────────────────────────────────────────────────────────────────────────►
  │                                                                               │
  │ Señal válida                                                                  │
  │ Score >= threshold                                                            │
  │ Guards passed                                                                 │
  ▼                                                                               │
WAIT_FILL ──────────────────────────────────────────────────────────────────────►│
  │                                                                               │
  │ Order filled                         │ Timeout (3 min)                        │
  │ Position confirmed                   │ Order cancelled                        │
  ▼                                      ▼                                        │
MANAGE ◄────────────────────────────────────────────────────────────────────────►│
  │                                                                               │
  │ Position closed                                                               │
  │ (TP, SL, Trailing, TIME_INVALIDATION, WTP)                                   │
  │                                                                               │
  └───────────────────────────────────────────────────────────────────────────────┘

OBSERVATION: Estado de recuperación después de múltiples pérdidas.
             Monitorea mercado pero no opera hasta que métricas mejoran.
```

---

# 2. FLUJO DE ENTRADA (ENTRY)

## 2.1 Fases en Orden

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                           FLUJO DE ENTRADA                                     │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  PHASE 0: PRE-CONDITIONS                                                       │
│  ├─ Rejection cooldown check                                                   │
│  ├─ Candle gate (new candle closed?)                                          │
│  ├─ Pending state conflicts                                                    │
│  └─ Exchange sync (cada 30s)                                                   │
│                                                                                │
│  PHASE 1: HTF FILTER (1H) - Con caché                                         │
│  ├─ EMA50/200 alignment                                                        │
│  ├─ ADX classification (RANGE/TRANSITION/TREND)                               │
│  └─ Bias determination (LONG/SHORT)                                           │
│                                                                                │
│  PHASE 2: REGIME DETECTION                                                     │
│  ├─ Trading mode (RANGE/TRANSITION/TREND)                                     │
│  ├─ Daily regime (TREND_DAY/RANGE_DAY)                                        │
│  └─ Trend phase (BIRTH → ACCEL → MATURE → EXHAUST → DEATH)                   │
│                                                                                │
│  PHASE 3: LOCATION FILTER                                                      │
│  ├─ 60-day percentile check                                                    │
│  ├─ Extreme veto (top/bottom 10%)                                             │
│  └─ Headroom to structure                                                      │
│                                                                                │
│  PHASE 4: GUARDS (entry_orchestrator.py)                                       │
│  ├─ Concurrency guards                                                         │
│  ├─ Edge enforcement                                                           │
│  ├─ ADX coherence                                                              │
│  ├─ Funding rate                                                               │
│  ├─ Spread filter                                                              │
│  └─ Session limits                                                             │
│                                                                                │
│  PHASE 5: SCORING (4 blocks × 0-2 pts = max 9)                                │
│  ├─ Structure block                                                            │
│  ├─ Timing block                                                               │
│  ├─ Momentum block                                                             │
│  └─ Context block + synergy bonus                                             │
│                                                                                │
│  PHASE 5.5: HARD ENTRY BLOCKS (Jan 27, 2026) ⭐ NUEVO                          │
│  ├─ session_quality = 'poor' → BLOCK                                          │
│  ├─ worst_case_rr < 0.7R → BLOCK                                              │
│  ├─ ADX < 18 + RR < 0.85R → BLOCK                                             │
│  └─ score_timing = 0 + RR < 1.0R → BLOCK                                      │
│                                                                                │
│  PHASE 6: CHASE VALIDATION                                                     │
│  ├─ Calculate chase ATR                                                        │
│  ├─ Apply chase penalty to score                                              │
│  └─ Block if adjusted score < threshold                                       │
│                                                                                │
│  PHASE 7: SL/TP CALCULATION                                                    │
│  ├─ Support/Resistance detection                                               │
│  ├─ Stop loss (ATR-based + regime multiplier)                                 │
│  └─ Take profit (1.0R base, regime-adjusted)                                  │
│                                                                                │
│  PHASE 8: RR VALIDATION                                                        │
│  ├─ Ideal RR calculation                                                       │
│  ├─ Worst-case RR (with slippage model)                                       │
│  └─ RR-based sizing multiplier                                                │
│                                                                                │
│  PHASE 9: SIZING                                                               │
│  ├─ Base size from risk %                                                      │
│  ├─ Apply all multipliers (12+)                                               │
│  └─ Minimum size validation                                                    │
│                                                                                │
│  PHASE 10: COST VALIDATION                                                     │
│  └─ Costs < 80% of expected profit                                            │
│                                                                                │
│  PHASE 11: EXECUTION                                                           │
│  ├─ LIMIT (default, POST_ONLY)                                                │
│  └─ MARKET (if velocity > 2x ATR)                                             │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

## 2.2 Filtros/Gates Detallados

### KILL CONDITIONS (Bloqueo Absoluto)
| Filtro | Umbral | Acción |
|--------|--------|--------|
| Max Daily DD | > 3% | Emergency close all, stop bot |
| WebSocket Timeout | > 75s | Emergency close |
| Black Swan | Move > 4x ATR + 60% wick | 8 min cooldown |
| Consecutive Losses | >= 6 | Daily kill switch |

### HTF FILTER (1H)
| Verificación | Umbral | Resultado |
|--------------|--------|-----------|
| EMA50 vs EMA200 | EMA50 > EMA200 | LONG bias |
| EMA50 vs EMA200 | EMA50 < EMA200 | SHORT bias |
| ADX | < 18 | RANGE mode (50% size, stricter threshold) |
| ADX | 18-20 | TRANSITION mode (70% size) |
| ADX | > 20 | TREND mode (full size) |
| ADX | > 25 | Strong trend (chase penalty reduced) |

### LOCATION FILTER
| Verificación | Umbral | Acción |
|--------------|--------|--------|
| Percentile 60d | >= 90% | LONG blocked (in RANGE) |
| Percentile 60d | <= 10% | SHORT blocked (in RANGE) |
| Headroom to high | < 0.5 ATR | Score penalty |
| Breakout override | ADX >= 30 + BOS | Allow entry despite location |

### HARD ENTRY BLOCKS (Phase 5.5) - ⭐ NO BYPASS
| Condición | Umbral | Resultado |
|-----------|--------|-----------|
| Session Quality | = 'poor' | **BLOCK** (sin excepciones) |
| Worst-case RR | < 0.7R | **BLOCK** (sin high-score bypass) |
| Range + Low RR | ADX < 18 + RR < 0.85R | **BLOCK** |
| Bad Timing | score_timing=0 + RR < 1.0R | **BLOCK** |

### SESSION LIMITS
| Verificación | Umbral | Bypass |
|--------------|--------|--------|
| Trades per session | Variable (3-5) | Score >= 7.0 override |
| Session quality | poor/moderate/good | Sizing multiplier, no hard block |

---

# 3. SISTEMA DE SCORING

## 3.1 Los 4 Bloques + Synergy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SCORING SYSTEM                                    │
│                         Max Score = 9 points                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  BLOCK 1: STRUCTURE (0-2 points)                                           │
│  ├─ HTF alignment (EMA50/200)                    +1                        │
│  ├─ 5m structure valid (HL/LH)                   +1                        │
│  └─ Structure = 0 → HARD BLOCK (only real gate)                           │
│                                                                             │
│  BLOCK 2: TIMING (0-2 points)                                              │
│  ├─ BOS at candle close                          +1                        │
│  ├─ Reclaim quality / EMA20 touch                +1                        │
│  └─ timing = 0 + RR < 1.0 → HARD BLOCK (Jan 27)                           │
│                                                                             │
│  BLOCK 3: MOMENTUM (0-2 points)                                            │
│  ├─ ADX direction favorable                      +1                        │
│  ├─ ADX slope positive (or neutral)              +1                        │
│  └─ Volume confirmation                          +0.5                      │
│                                                                             │
│  BLOCK 4: CONTEXT (0-2 points)                                             │
│  ├─ Session quality (good=+1)                    0-1                       │
│  ├─ Volatility regime (normal=+1)                0-1                       │
│  └─ Trend phase bonus (BIRTH/ACCEL)              +0.5                      │
│                                                                             │
│  SYNERGY BONUS (+1 max)                                                    │
│  └─ HTF clean + BOS confirmed + momentum aligned  +1                       │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  ADJUSTMENTS (applied after base score)                                    │
│  ├─ Spread penalty                               -0 to -2                  │
│  ├─ Delta volume bonus                           +0 to +1                  │
│  ├─ Short obstinacy penalty                      -0.15 to -0.5             │
│  ├─ Location penalty                             -0 to -1                  │
│  ├─ Anticipation bonus                           +0 to +1                  │
│  ├─ With-trend bonus                             +0.5                      │
│  ├─ Chase penalty                                -0 to -2                  │
│  └─ Funding alignment bonus                      ±0.5                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 3.2 Umbrales Dinámicos

| Modo | Régimen | Sesión | Threshold Base |
|------|---------|--------|----------------|
| DISCOVERY | TREND | Buena | 4.0 |
| DISCOVERY | TREND | Moderada | 4.5 |
| DISCOVERY | RANGE | Cualquiera | 5.0 |
| PRODUCTION | TREND | Buena | 7.0 |
| PRODUCTION | RANGE | Cualquiera | 7.5 |

### Trend Phase Adjustments
| Fase | Ajuste |
|------|--------|
| TREND_BIRTH | -1.0 (más permisivo) |
| TREND_ACCELERATION | -0.5 |
| RANGE_BOUND | 0 |
| TREND_EXHAUSTION | +1.5 (más estricto) |
| DISTRIBUTION | +2.0 |

## 3.3 Score → Size Mapping

| Score Range | Size Multiplier | Entry Type |
|-------------|-----------------|------------|
| < 3.5 | 0.0 (blocked) | NO ENTRY |
| 3.5 - 4.5 | 0.25x | PROBE |
| 4.5 - 5.5 | 0.50x | REDUCED |
| > 5.5 | 1.0x | FULL |

---

# 4. GESTIÓN DE RIESGO

## 4.1 Cálculo de Stop Loss

```python
# Base SL calculation
sl_distance = ATR_5m * sl_atr_multiplier

# Multiplier by regime:
# - TREND: 3.0x ATR
# - RANGE: 2.0x ATR
# - BREAKOUT: 2.5x ATR

# Support/Resistance adjustment
if support_level exists and is closer:
    sl = support_level - buffer

# Final SL
if direction == 'LONG':
    stop_loss = entry_price - sl_distance
else:  # SHORT
    stop_loss = entry_price + sl_distance
```

## 4.2 Cálculo de Take Profit

```python
# Base TP = risk * TP_multiplier
risk = abs(entry_price - stop_loss)

# TP multiplier by daily regime:
# - TREND_DAY: 1.5R
# - RANGE_DAY: 0.8R (scalp)
# - Default: 1.0R

take_profit = entry_price ± (risk * tp_multiplier)
```

## 4.3 Trailing Stop (Hybrid System)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    HYBRID TRAILING STOP SYSTEM                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  COMPONENT 1: R-STEPPED LOCKS (Guaranteed Protection)                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  MFE >= 1.0R  → Lock -0.2R (RANGE) or no action (TREND)              │  │
│  │  MFE >= 1.2R  → Lock BE (RANGE) or +0.4R (TREND)                     │  │
│  │  MFE >= 1.5R  → Lock +0.9R                                           │  │
│  │  MFE >= 2.2R  → Lock +1.6R                                           │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  COMPONENT 2: ATR-ADAPTIVE TRAILING                                        │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  TREND regime: 2.5x ATR trailing (give room to run)                  │  │
│  │  RANGE regime: 2.0x ATR trailing (tighter, quicker exits)            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  FINAL STOP = max(r_stepped_lock, atr_trailing) for LONG                   │
│             = min(r_stepped_lock, atr_trailing) for SHORT                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 4.4 WORST_TRADE_PREVENTION

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    WORST_TRADE_PREVENTION (WTP)                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  CONCEPTO: "Si no invalida la tesis, no puede ser el peor trade del día"   │
│                                                                             │
│  FASE 1 (0-4 min): NO WTP - dejar que el setup se desarrolle               │
│                                                                             │
│  FASE 2 (4+ min): Evaluar continuamente:                                   │
│  ├─ MFE reached? → Si MFE >= 0.3R, trade está probándose, no WTP           │
│  ├─ Momentum alive? → ADX trending, ATR expanding                          │
│  ├─ Structure intact? → VWAP, swing levels                                 │
│  └─ Current R? → Si < -0.30R (TREND) o -0.60R (RANGE), EXIT                │
│                                                                             │
│  LÍMITES ABSOLUTOS:                                                        │
│  ├─ TIME_INVALIDATION_MAX_LOSS_R = -0.30R (TREND)                          │
│  ├─ TIME_INVALIDATION_MAX_LOSS_R = -0.60R (RANGE)                          │
│  └─ ABSOLUTE_MAX_LOSS_R = -0.35R (nunca perder más)                        │
│                                                                             │
│  EXCEPCIONES:                                                               │
│  └─ ADOPTED positions (recuperadas de exchange) → EXCLUIDAS de WTP         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 4.5 TIME_INVALIDATION

```python
# Check after N candles (default 8 min in RANGE, 6 in TREND)
if position.candle_count >= TIME_INVALIDATION_CANDLES:
    
    # Calculate current R
    current_r = (current_price - entry) / initial_risk
    
    # Market type affects max loss
    if adx < 20:  # RANGING
        max_loss_r = -0.60  # More room in range
    else:
        max_loss_r = -0.30  # Tighter in trend
    
    # CRITICAL: Don't let time invalidation create worse loss
    if current_r <= max_loss_r:
        return CLOSE  # Exit to limit loss
    
    # If MFE >= threshold, trade is working
    if mfe_r >= 0.3:
        return None  # Let it run
    
    # If market is alive (ADX, ATR), give more time
    if market_alive:
        return HOLD
    
    # Structure invalidation check
    if structural_invalid:
        return CLOSE
    
    # Default: Hold in range, close in dead trend
    if is_ranging:
        return HOLD  # Let stop do its job
    else:
        return CLOSE
```

---

# 5. SIZING (TAMAÑO DE POSICIÓN)

## 5.1 Fórmula Base

```python
# Risk-based sizing
base_size = (capital * risk_pct) / stop_loss_distance

# Where risk_pct comes from DynamicRiskConfig.RISK_TIERS:
# < 2k€: 1.0% risk
# 2k-5k€: 0.75% risk
# > 5k€: 0.5% risk
```

## 5.2 TODOS los Multiplicadores

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    POSITION SIZE MULTIPLIERS                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1.  Edge Enforcement Mult        × 0.5 - 1.0 (basado en edge degradation) │
│  2.  Score-based Mult             × 0.0/0.25/0.5/1.0 (graduated sizing)    │
│  3.  Regime Mult                  × 0.5 (RANGE) / 0.7 (TRANSITION) / 1.0   │
│  4.  Volatility Mult              × 0.8 (HIGH) / 0.6 (SPIKE) / 1.0         │
│  5.  Session Quality Mult         × 0.3-1.0 (DEAD=0.3, OVERLAP=1.0)        │
│  6.  Loss Streak Mult             × 0.3-1.0 (after 3+ losses: 0.3)         │
│  7.  Spread Factor Mult           × 0.5-1.0 (wide spread = reduce)         │
│  8.  Countertrend Mult            × 0.5 (trading against strong bias)      │
│  9.  Day-of-Week Mult             × 0.6 (weekend) / 0.85 (Fri) / 1.0       │
│  10. Anticipation Probe Mult      × 0.30 (early probe entries)             │
│  11. RR-based Mult                × 0.25/0.5/1.0 (based on worst-case RR)  │
│  12. Gate Size Cap Mult           × 0.25-1.0 (from entry quality gate)     │
│  13. Location Size Cap Mult       × 0.5-1.0 (near extremes)                │
│  14. Daily Regime Mult            × 0.5 (RANGE_DAY) / 0.7 (TRANSITION)     │
│  15. Black Swan Defensive Mult    × 0.5-1.0 (after volatility events)      │
│  16. Funding Against Mult         × 0.75 (paying funding)                  │
│                                                                             │
│  FINAL SIZE = base_size × Π(all multipliers)                               │
│                                                                             │
│  MINIMUM: KRAKEN_BTC_MIN_SIZE = 0.0001 BTC                                 │
│  MAXIMUM: max_position_pct * capital (default 10%)                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 5.3 Ejemplo de Cálculo

```python
# Capital: $265
# Risk %: 1.0% → $2.65 risk
# Stop distance: $290 (entry 88000, SL 87710)
# Base size: 2.65 / 290 = 0.0091 BTC

# Multipliers applied:
# - Edge: 1.0
# - Score (5.5): 0.5
# - Regime (RANGE): 0.5
# - Session (ASIA): 0.75
# - Loss streak (0): 1.0
# - Spread: 1.0
# - RR (0.85): 0.5

# Final: 0.0091 × 1.0 × 0.5 × 0.5 × 0.75 × 1.0 × 1.0 × 0.5 = 0.00085 BTC
# Check minimum: 0.00085 > 0.0001 ✓
```

---

# 6. RÉGIMEN DE MERCADO

## 6.1 Clasificación ADX

| ADX Range | Clasificación | Comportamiento |
|-----------|---------------|----------------|
| < 18 | **RANGE** | Mean-reversion, extremes only, 50% size, 2x ATR SL |
| 18-20 | **TRANSITION** | Cautious, 70% size, +0.5 threshold |
| 20-25 | **WEAK_TREND** | Normal trend-following |
| > 25 | **STRONG_TREND** | Full trend-following, chase penalty reduced |
| > 30 | **VERY_STRONG** | Allow breakout override on location |
| > 40 | **EXTREME** | Parabolic move, reduce size |

## 6.2 Diferencias TREND vs RANGE

| Aspecto | TREND (ADX > 20) | RANGE (ADX < 18) |
|---------|------------------|------------------|
| **Trades permitidos** | Any location | Extremes only (10%/90%) |
| **Size multiplier** | 1.0x | 0.5x |
| **Score threshold** | Base | Base + 1.0 |
| **SL ATR multiplier** | 3.0x | 2.0x |
| **TP R target** | 1.5R | 1.0R (scalp) |
| **Max duration** | 90 min | 30 min |
| **TIME_INVALIDATION** | 6 candles, -0.30R max | 8 candles, -0.60R max |
| **Chase penalty** | Reduced if ADX > 25 | Full penalty |
| **Trailing** | 2.5x ATR, patient | 2.0x ATR, tighter |
| **BE trigger** | MFE >= 1.2R | MFE >= 1.0R |

## 6.3 Daily Regime Detection

```python
# Ejecutar al inicio del día/sesión
def classify_daily_regime(daily_range, daily_atr, daily_adx):
    range_ratio = daily_range / daily_atr
    
    if range_ratio > 1.5 and daily_adx > 20:
        return 'TREND_DAY'
    elif range_ratio < 1.0 or daily_adx < 18:
        return 'RANGE_DAY'
    else:
        return 'TRANSITION_DAY'
```

| Daily Regime | allow_runners | sl_atr_mult | tp_r_target | max_trades | size_mult |
|--------------|---------------|-------------|-------------|------------|-----------|
| TREND_DAY | True | 3.0 | 2.0 | 4 | 1.0 |
| RANGE_DAY | False | 2.0 | 1.0 | 3 | 0.5 |
| TRANSITION_DAY | True | 2.5 | 1.5 | 3 | 0.7 |

---

# 7. SESIONES

## 7.1 Horarios (UTC)

| Sesión | Horario UTC | Calidad | Size Mult |
|--------|-------------|---------|-----------|
| **LONDON_NY_OVERLAP** | 13:30-16:00 | Mejor | 1.0 |
| **LONDON** | 08:00-16:00 | Buena | 1.0 |
| **NY** | 13:30-21:00 | Buena | 0.90 |
| **NY_OPEN_VOLATILE** | 14:30-14:45 | Volátil | 0.70 |
| **ASIA** | 23:00-08:00 | Baja | 0.75 |
| **DEAD_HOURS** | 21:00-23:00 | Muy baja | 0.30 |
| **WEEKEND** | Fri 21:00 - Sun 21:00 | Baja liq. | 0.50 |

## 7.2 Session Quality Classification

```python
def get_session_quality(session_name):
    good_sessions = {'LONDON_NY_OVERLAP', 'LONDON', 'NY'}
    moderate_sessions = {'ASIA', 'NY_OPEN_VOLATILE'}
    poor_sessions = {'DEAD_HOURS', 'WEEKEND'}
    
    if session_name in good_sessions:
        return 'good'
    elif session_name in moderate_sessions:
        return 'moderate'
    else:
        return 'poor'  # → HARD BLOCK (Jan 27)
```

## 7.3 Impacto en Trading

- **session_quality = 'poor'**: **HARD BLOCK** - No entry (Jan 27 fix)
- **session_quality = 'moderate'**: Size reduced, higher threshold
- **session_quality = 'good'**: Full trading permissions

### Session Trade Limits
| Session | Max Trades | Override Score |
|---------|------------|----------------|
| LONDON_NY_OVERLAP | 5 | >= 7.0 |
| LONDON | 5 | >= 7.0 |
| NY | 5 | >= 7.0 |
| ASIA | 3 | >= 7.0 |
| DEAD_HOURS | 2 | N/A (blocked) |

---

# 8. SALIDAS (EXITS)

## 8.1 Tipos de Exit

| Exit Type | Prioridad | Condición |
|-----------|-----------|-----------|
| **STOP_LOSS** | 1 (máxima) | Price hits SL |
| **TRAILING_STOP** | 2 | Price hits trailing SL |
| **EXCHANGE_TAKE_PROFIT** | 3 | Exchange TP filled |
| **TIME_PROFIT_LOCK** | 4 | 10+ candles en profit, lock 0.3R |
| **MOMENTUM_EXIT** | 5 | RSI dies (< 40 LONG, > 60 SHORT) |
| **WORST_TRADE_PREVENTION** | 6 | Thesis invalidated + loss |
| **VPOC_EXIT** | 7 | Near VPOC with >= 0.85R profit |
| **VOLATILITY_SQUEEZE** | 8 | BB squeeze with 0.3-0.5R profit |
| **TIME_INVALIDATION** | 9 | N candles, no progress, market dead |
| **PARTIAL_SCALE** | 10 | MFE milestones hit |

## 8.2 Prioridad de Exits

```python
EXIT_PRIORITY = [
    'STOP_LOSS',           # Always first
    'TRAILING_STOP',       # Locked profits
    'TIME_PROFIT_LOCK',    # Time-based locks
    'MOMENTUM_EXIT',       # Momentum death
    'VPOC_EXIT',           # Resistance proximity
    'VOLATILITY_SQUEEZE',  # Compression exit
    'PARTIAL_SCALE',       # Partial profit taking
]
```

## 8.3 Scaled Partial Exits

```python
SCALE_OUT_LEVELS = [
    (1.0, 0.50),   # Exit 50% at +1.0R (TP1)
    (1.5, 0.25),   # Exit 25% at +1.5R
    (2.5, 0.15),   # Exit 15% at +2.5R
    # Remaining 10% runs with trailing
]
```

---

# 9. CONFIGURACIÓN CLAVE

## 9.1 Constantes Importantes

```python
# Operating Mode
MODE = "DISCOVERY"  # vs "PRODUCTION"

# Score Thresholds
DISCOVERY_SCORE_THRESHOLD = 4
PRODUCTION_SCORE_THRESHOLD = 7

# RR Floors
RR_HARD_FLOOR = 0.7  # Jan 27: Unified hard floor
RR_HARD_FLOOR_RANGE = 0.85  # Higher in ranging markets

# Risk per Trade (by equity tier)
# < 2k: 1.0%
# 2k-5k: 0.75%
# > 5k: 0.5%

# Take Profit
TP1_RISK_MULTIPLE = 1.0  # Target 1R

# Stop Loss ATR Multipliers
SL_ATR_TREND = 3.0
SL_ATR_RANGE = 2.0

# Time Invalidation
TIME_INVALIDATION_CANDLES = 8  # Range (was 5)
TIME_INVALIDATION_MAX_LOSS_R = -0.30  # Trend
TIME_INVALIDATION_MAX_LOSS_RANGE_R = -0.60  # Range

# Kill Switch
KILL_SWITCH_LOSSES = 6
MAX_DAILY_DRAWDOWN_PCT = 3.0

# Minimum Position
KRAKEN_BTC_MIN_SIZE = 0.0001
```

## 9.2 Valores por Defecto

```python
# Default leverage
DEFAULT_LEVERAGE = 2

# Cooldowns
COOLDOWN_AFTER_TRADE = 5  # minutes (was 15)
COOLDOWN_SL_NORMAL = 12  # minutes
COOLDOWN_TIME_INVALIDATION = 3  # minutes

# Entry timeout
ENTRY_TIMEOUT_CANDLES = 3  # 3 minutes
FILL_TIMEOUT_SECONDS = 180  # 3 minutes

# Spread
MAX_SPREAD_SPIKE_MULTIPLIER = 2.0

# Funding
MAX_FUNDING_RATE_PCT = 0.12
FORCE_EXIT_FUNDING_PCT = 0.20
```

---

# 10. MEJORAS RECIENTES (Jan 27, 2026)

## 10.1 HARD BLOCKS Implementados

### Antes (problemas)
```python
# Session quality = 'poor' pero entraba "anyway"
if session_quality == 'poor':
    logger.warning("Poor session but entering anyway")  # ❌ MALO

# RR < 0.7 pero usaba "probe" bypass
if worst_case_rr < 0.7:
    logger.warning("Low RR but using probe")  # ❌ MALO
```

### Después (corregido)
```python
# entry_validators.py - HardEntryBlockValidator
class HardEntryBlockValidator:
    HARD_RR_FLOOR = 0.7
    BLOCKED_SESSION_QUALITIES = {'poor'}
    
    @classmethod
    def validate_entry(cls, session_quality, worst_case_rr, ...):
        # BLOCK 1: session_quality = 'poor' → NO BYPASS
        if session_quality in cls.BLOCKED_SESSION_QUALITIES:
            return HardBlockResult(
                is_blocked=True,
                block_reason="HARD BLOCK: session_quality='poor'"
            )
        
        # BLOCK 2: worst_case_rr < 0.7R → NO HIGH-SCORE BYPASS
        if worst_case_rr < cls.HARD_RR_FLOOR:
            return HardBlockResult(
                is_blocked=True,
                block_reason="HARD BLOCK: RR < 0.7R"
            )
```

## 10.2 Código Eliminado

1. **Anti-paralysis threshold reduction**: Eliminado completamente
   - Causa: Aceptaba trades basura por "inactividad"
   - Fix: 0 trades es mejor que trades forzados

2. **High-score RR bypass**: Eliminado
   - Causa: Score alto permitía RR negativo
   - Fix: RR floor es absoluto, no negociable

3. **Session quality override**: Eliminado
   - Causa: "Entering anyway" en poor sessions
   - Fix: Poor session = hard block

4. **Activity-based threshold relaxation**: Eliminado
   - Causa: Reducía threshold por "horas sin trade"
   - Fix: Activity tracker corregido, no necesita compensación

## 10.3 Umbrales Unificados

| Concepto | Antes | Después |
|----------|-------|---------|
| RR Floor (cualquier régimen) | Variable (0.4-0.9) | **0.7R absoluto** |
| RR Floor (RANGE) | Variable | **0.85R** |
| Session quality 'poor' | Warning | **HARD BLOCK** |
| score_timing=0 + low RR | Allowed | **HARD BLOCK** |
| TIME_INVALIDATION RANGE | -0.25R | **-0.60R** (más room) |
| TIME_INVALIDATION TREND | -0.25R | **-0.30R** |

## 10.4 Activity Tracker Corregido

```python
# ANTES: Múltiples lugares actualizaban _last_trade_time
# - trading_app.py
# - entry_orchestrator.py  
# - risk_engine.py
# Resultado: "24h sin trade" cuando había trades minutos antes

# DESPUÉS: Single Source of Truth
# activity_tracker.py es el ÚNICO lugar que registra actividad
from src.domain.trading.activity_tracker import record_trade_executed

# Al ejecutar trade:
record_trade_executed(market_time)  # ← ÚNICA forma de registrar
```

---

# APÉNDICE A: Checklist de Validación Pre-Trade

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PRE-TRADE VALIDATION CHECKLIST                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  □ Kill conditions not triggered                                           │
│  □ HTF filter passed (EMA alignment, ADX > threshold)                      │
│  □ Daily regime classified                                                  │
│  □ Trading mode determined (RANGE/TRANSITION/TREND)                        │
│  □ Location filter passed (or penalty applied)                             │
│  □ Session quality != 'poor'                                               │
│  □ Session trade limit not exceeded (or score >= 7 override)               │
│  □ Spread within acceptable range                                          │
│  □ Funding rate not extreme                                                │
│  □ Score >= dynamic threshold                                              │
│  □ Structure score > 0                                                      │
│  □ Worst-case RR >= 0.7R (or >= 0.85R in RANGE)                            │
│  □ score_timing > 0 OR RR >= 1.0R                                          │
│  □ Chase penalty doesn't kill score                                        │
│  □ Final size >= KRAKEN_BTC_MIN_SIZE                                       │
│  □ Costs < 80% of expected profit                                          │
│  □ No cooldown active (rejection, loss streak, CT)                         │
│                                                                             │
│  IF ALL CHECKS PASS → EXECUTE ENTRY                                        │
│  IF ANY HARD BLOCK → NO ENTRY (no exceptions)                              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

# APÉNDICE B: Exit Decision Tree

```
Position Management (every tick):
│
├─ Check STOP_LOSS hit?
│   └─ YES → EXIT (exchange SL or emergency close)
│
├─ Check TRAILING_STOP hit?
│   └─ YES → EXIT
│
├─ Check WORST_TRADE_PREVENTION
│   ├─ Is adopted position? → SKIP WTP
│   ├─ candle_count < 4? → SKIP WTP
│   ├─ current_R < max_loss_r? → EXIT (limit loss)
│   └─ thesis_intact = False? → EXIT
│
├─ Check TIME_INVALIDATION
│   ├─ candle_count >= threshold?
│   │   ├─ MFE >= 0.3R? → HOLD (working)
│   │   ├─ market_alive? → HOLD (give time)
│   │   ├─ is_ranging? → HOLD (let stop work)
│   │   └─ structure_invalid? → EXIT
│   └─ NO → Continue
│
├─ Check MOMENTUM_EXIT
│   ├─ MFE >= 0.5R AND RSI death? → EXIT
│   └─ NO → Continue
│
├─ Check VPOC_EXIT
│   ├─ MFE >= 0.85R AND near VPOC? → EXIT
│   └─ NO → Continue
│
├─ Check VOLATILITY_SQUEEZE
│   ├─ 0.3R <= MFE <= 0.5R AND BB squeeze? → EXIT
│   └─ NO → Continue
│
├─ Update TRAILING_STOP
│   └─ Calculate new stop based on MFE and regime
│
└─ Check PARTIAL exits
    └─ MFE milestones → PARTIAL CLOSE
```

---

**Documento generado: Enero 27, 2026**  
**Sistema: FideiRatioCapital BTC Perpetual Trading System**  
**Modo: DISCOVERY**  
**Exchange: Kraken Futures**
