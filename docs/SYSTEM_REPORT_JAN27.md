# 📊 REPORTE COMPLETO DEL SISTEMA DE TRADING BTC
## FideiRatioCapital - DayTrader v2.1
### Actualizado: 27 de Enero, 2026

---

# 1. ARQUITECTURA GENERAL

## 1.1 Diagrama de Flujo

```
┌──────────────────────────────────────────────────────────────────────┐
│                           ENTRY POINT                                │
│                            main.py                                   │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         BOOTSTRAP LAYER                              │
│  ┌───────────────┐  ┌────────────────┐  ┌────────────────────────┐  │
│  │ dependencies  │  │    logging     │  │       runtime          │  │
│  │    .py        │  │      .py       │  │         .py            │  │
│  └───────────────┘  └────────────────┘  └────────────────────────┘  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      APPLICATION LAYER                               │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                     trading_app.py                             │  │
│  │  SignalEngine (5917 lines) - "God Class"                       │  │
│  │  - State machine                                               │  │
│  │  - Signal detection                                            │  │
│  │  - Position management                                         │  │
│  │  - Order execution                                             │  │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌────────────────────────┐  ┌────────────────────────────────────┐  │
│  │  entry_orchestrator.py │  │      entry_validators.py          │  │
│  │  - Execute entry flow  │  │  - ChaseValidator                 │  │
│  │  - 11 phases           │  │  - GraduatedSizer                 │  │
│  │  - Guards & gates      │  │  - RRValidator                    │  │
│  │                        │  │  - CostValidator                  │  │
│  │                        │  │  - HardEntryBlockValidator        │  │
│  └────────────────────────┘  └────────────────────────────────────┘  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         DOMAIN LAYER                                 │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │                     risk/risk_engine.py                       │   │
│  │  - TrackedPosition                                            │   │
│  │  - Stop loss calculation                                      │   │
│  │  - Trailing stop management                                   │   │
│  │  - Exit hierarchy (WTP, TIME_INVALIDATION, etc)               │   │
│  │  - Capital management                                         │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌─────────────────────────┐  ┌────────────────────────────────┐    │
│  │  trading/activity_      │  │  trading/fee_calculator.py     │    │
│  │  tracker.py             │  │  - Fee calculations            │    │
│  │  - Last trade time      │  │  - PnL with fees               │    │
│  │  - Win/loss tracking    │  │  - Kraken fee rates            │    │
│  │  - Daily stats          │  │                                │    │
│  └─────────────────────────┘  └────────────────────────────────┘    │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     INFRASTRUCTURE LAYER                             │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │              exchanges/kraken/execution_client.py             │   │
│  │  - Order execution (market/limit)                             │   │
│  │  - Position fetching                                          │   │
│  │  - Balance queries                                            │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌─────────────────────────┐  ┌────────────────────────────────┐    │
│  │  ingest/data_provider   │  │  messaging/telegram_bot.py     │    │
│  │  - CCXT data fetching   │  │  - Notifications               │    │
│  │  - Candle aggregation   │  │  - Health alerts               │    │
│  └─────────────────────────┘  └────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

## 1.2 Patrón de Estados

```python
class BotState(Enum):
    IDLE = "IDLE"                    # Esperando señal
    WAIT_LTF = "WAIT_LTF"            # HTF OK, esperando LTF
    PENDING_ENTRY = "PENDING_ENTRY"  # Orden limit enviada
    IN_TRADE = "IN_TRADE"            # Posición activa
    COOLDOWN = "COOLDOWN"            # Enfriamiento post-trade
```

## 1.3 Ciclo Principal

```
IDLE → detectar HTF signal → WAIT_LTF → confirmar LTF → 
     → PENDING_ENTRY → fill → IN_TRADE → exit → COOLDOWN → IDLE
```

---

# 2. FLUJO DE ENTRADA (ENTRY)

## 2.1 Fases en Orden

```
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 0: Pre-conditions (_handle_idle_state)                       │
├─────────────────────────────────────────────────────────────────────┤
│ ✓ Rejection cooldown expired                                        │
│ ✓ Candle gate (enough data)                                         │
│ ✓ No pending state conflicts                                        │
│ ✓ Exchange sync OK                                                  │
│ ✓ HTF filter passed                                                 │
│ ✓ MTF filter passed                                                 │
│ ✓ LTF filter passed                                                 │
│ ✓ Delta volume calculated                                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 1: GUARDS (entry_orchestrator.py)                            │
├─────────────────────────────────────────────────────────────────────┤
│ 1. Concurrency: No pending orders                                   │
│ 2. Concurrency: No open position same symbol                        │
│ 3. Edge enforcement (max loss tracking)                             │
│ 4. Anchor levels check (valid S/R)                                  │
│ 5. Range location check (not at extreme)                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 2: SCORING                                                    │
├─────────────────────────────────────────────────────────────────────┤
│ Base Score = Structure + Timing + Momentum + Context + Synergy      │
│                                                                     │
│ ADJUSTMENTS:                                                        │
│ (-) Spread penalty: spread > 0.02% ATR                              │
│ (+) Delta volume bonus: +0.5 if aligned                             │
│ (-) Short obstinacy: -1 if counter-funding                          │
│ (-) Location penalty: -0.5 per ATR beyond 2 ATR                     │
│ (+) Anticipation bonus: +0.5 if near level                          │
│ (+) With-trend bonus: +0.5 if trend aligned                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 3: STRUCTURE GATE                                             │
├─────────────────────────────────────────────────────────────────────┤
│ IF score_structure == 0 → HARD BLOCK (no HTF alignment)             │
│                                                                     │
│ Dynamic threshold calculation:                                      │
│ - Base: 5.0                                                         │
│ - Range regime: 4.5                                                 │
│ - Discovery mode: 4.0                                               │
│                                                                     │
│ IF final_score < threshold → SKIP (soft block)                      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 4: QUALITY GATE                                               │
├─────────────────────────────────────────────────────────────────────┤
│ Entry Quality Gate checks:                                          │
│ - Session quality                                                   │
│ - Session limits                                                    │
│ - Obstinacy checks                                                  │
│ - Size cap calculation                                              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 5: CHASE VALIDATION                                           │
├─────────────────────────────────────────────────────────────────────┤
│ Chase ATR = (current_price - candle_low) / ATR                      │
│                                                                     │
│ IF chase_atr > 2.5 → HARD BLOCK                                     │
│                                                                     │
│ Penalty calculation (regime-dependent):                             │
│ - TREND: chase > 1.0 ATR → -1 pt per 0.5 ATR                        │
│ - RANGE: chase > 0.5 ATR → -1.5 pt per 0.5 ATR                      │
│                                                                     │
│ REDUCTIONS:                                                         │
│ - Momentum context positive: penalty * 0.5                          │
│ - Trend expansion active: penalty * 0.6                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 5.5: HARD ENTRY BLOCKS ⭐ (Jan 27, 2026)                      │
├─────────────────────────────────────────────────────────────────────┤
│ THESE ARE NON-NEGOTIABLE. NO BYPASS. NO EXCEPTIONS.                 │
│                                                                     │
│ 1. session_quality == 'poor'           → HARD BLOCK                 │
│ 2. worst_case_rr < 0.7R                → HARD BLOCK                 │
│ 3. ADX < 18 AND worst_case_rr < 0.85R  → HARD BLOCK (ranging)       │
│ 4. score_timing == 0 AND rr < 1.0R     → HARD BLOCK (bad timing)    │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 6: RR VALIDATION                                              │
├─────────────────────────────────────────────────────────────────────┤
│ Ideal RR = |take_profit - entry| / |entry - stop_loss|              │
│                                                                     │
│ Worst-case RR = Ideal RR - slippage_impact                          │
│ (Uses slippage model with P95 caps)                                 │
│                                                                     │
│ RR-based sizing:                                                    │
│ - RR < 0.70: Block (0.0x)                                           │
│ - RR 0.70-0.80: Probe (0.25x)                                       │
│ - RR >= 0.80: Full (1.0x)                                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 7: SIZING                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ (See Section 5 for complete formula)                                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 8: COST VALIDATION                                            │
├─────────────────────────────────────────────────────────────────────┤
│ cost_usd = (position_size * price * fee_rate) * 2                   │
│ expected_profit = size * risk * 1.2  # Assume 1.2R                  │
│                                                                     │
│ IF cost_usd / expected_profit > 0.35 → BLOCK (costs too high)       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 9: ORDER EXECUTION                                            │
├─────────────────────────────────────────────────────────────────────┤
│ IF momentum_velocity > threshold AND market_orders_enabled:         │
│     → Execute MARKET order                                          │
│ ELSE:                                                               │
│     → Execute LIMIT order (POST_ONLY)                               │
│                                                                     │
│ Transition to PENDING_ENTRY → await fill                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

# 3. SISTEMA DE SCORING

## 3.1 Bloques de Score

| Bloque | Puntos | Qué Evalúa |
|--------|--------|------------|
| **Structure** | 0-2 | HTF alignment (EMA, BOS, trend) |
| **Timing** | 0-2 | Entry timing (near level, EMA reclaim) |
| **Momentum** | 0-2 | ADX, RSI, volume alignment |
| **Context** | 0-2 | Volatility, session, macro conditions |
| **Synergy** | 0-2 | Multiple confirmations aligned |

**MÁXIMO TEÓRICO: 10 puntos**

## 3.2 Fórmula de Score

```python
base_score = structure + timing + momentum + context
synergy_bonus = 1 if (timing >= 1 and momentum >= 1 and context >= 1) else 0
raw_score = base_score + synergy_bonus

# Ajustes del filter pipeline
adjusted_score = raw_score 
    - spread_penalty           # 0-2
    + delta_volume_bonus       # 0-0.5
    - short_obstinacy_penalty  # 0-1
    - location_penalty         # 0-2.5
    + anticipation_bonus       # 0-0.5
    + with_trend_bonus         # 0-0.5
    - chase_penalty            # 0-3

final_score = adjusted_score + micro_trend_adj + filter_pipeline_adj
```

## 3.3 Umbrales

| Contexto | Threshold | Resultado |
|----------|-----------|-----------|
| Normal | 5.0 | Block < 5, Probe < 6, Full >= 6 |
| Range Regime | 4.5 | Más permisivo |
| Discovery Mode | 4.0 | Experimental |
| Structure = 0 | N/A | HARD BLOCK siempre |

## 3.4 Score-Based Sizing

```python
EntryGateConfig:
    SCORE_BLOCK_FLOOR = 3.5    # Below = block (0.0x)
    SCORE_PROBE_MAX = 4.5      # 3.5-4.5 = probe (0.25x)
    SCORE_REDUCED_MAX = 5.5    # 4.5-5.5 = reduced (0.5x)
    # Above 5.5 = full (1.0x)
```

---

# 4. GESTIÓN DE RIESGO

## 4.1 Stop Loss Calculation

```python
def calculate_stop_loss(price, bias, levels, atr, atr_ratio, regime_mult):
    # Base SL distance
    sl_atr_multiple = 1.0  # Default
    
    # Regime adjustment
    if regime == 'RANGE':
        sl_atr_multiple = 0.8  # Tighter in range
    elif regime == 'TREND':
        sl_atr_multiple = 1.2  # Wider in trend
    
    # Volatility adjustment
    if atr_ratio > 1.5:
        sl_atr_multiple *= 1.2  # Widen in high vol
    
    # Calculate
    sl_distance = atr * sl_atr_multiple * regime_mult
    
    if bias == 'LONG':
        stop_loss = price - sl_distance
        # Snap to nearest level below
        for level in levels:
            if level < price and level < stop_loss:
                stop_loss = level - (atr * 0.1)  # Buffer
                break
    else:  # SHORT
        stop_loss = price + sl_distance
        # Snap to nearest level above
        ...
    
    return stop_loss
```

## 4.2 Take Profit Calculation

```python
def calculate_take_profit(price, risk, bias, regime):
    # Base TP multiplier
    tp_r_target = 1.2  # Default 1.2R
    
    # Regime adjustment
    if regime == 'TREND':
        tp_r_target = 1.5  # Trail more in trend
    elif regime == 'RANGE':
        tp_r_target = 1.0  # Quick exits in range
    
    # Daily regime adjustment
    if daily_regime == 'RANGE_DAY':
        tp_r_target = min(tp_r_target, 1.0)
    
    tp_distance = risk * tp_r_target
    
    if bias == 'LONG':
        return price + tp_distance
    else:
        return price - tp_distance
```

## 4.3 Hybrid Trailing Stop

```python
class HybridTrailingStop:
    """
    Tres fases de trailing:
    1. SETUP (0-0.3R): No trailing, fixed SL
    2. PROTECTION (0.3-1.0R): Breakeven lock + loose trail
    3. MAXIMIZATION (>1.0R): Aggressive trailing
    """
    
    # SETUP phase
    SETUP_MFE_THRESHOLD = 0.3  # R
    
    # PROTECTION phase  
    PROTECTION_MFE_THRESHOLD = 0.5
    BREAKEVEN_LOCK = 0.3       # Lock at +0.05R after 0.3R MFE
    PROTECTION_TRAIL_ATR = 2.0  # 2 ATR from high
    
    # MAXIMIZATION phase
    MAX_MFE_THRESHOLD = 1.0
    MAX_TRAIL_ATR = 1.2        # 1.2 ATR from high (tight)
    
    # Volatility-adjusted
    def get_trail_distance(self, mfe, atr, vol_ratio):
        if mfe < SETUP_MFE_THRESHOLD:
            return None  # Fixed SL
        elif mfe < MAX_MFE_THRESHOLD:
            base = PROTECTION_TRAIL_ATR * atr
            return base * min(vol_ratio, 1.5)  # Vol cap
        else:
            base = MAX_TRAIL_ATR * atr
            return base * min(vol_ratio, 1.2)
```

## 4.4 WORST_TRADE_PREVENTION (WTP)

```python
ExitHierarchy:
    WORST_TRADE_PREVENTION_ENABLED = True
    
    # Time thresholds
    WTP_MIN_TIME_MINUTES = 5       # Don't trigger before 5 min
    WTP_MAX_TIME_MINUTES = 60      # Always trigger by 60 min
    
    # Performance thresholds  
    WTP_MIN_MFE_R = 0.25          # Must have seen at least 0.25R profit
    WTP_REVERSION_THRESHOLD = 0.5  # Trigger if reverted 50% of MFE

def check_worst_trade_prevention(position, current_price, market_time):
    """
    Detect mediocre trades that are likely to become losers.
    Exit early to save capital.
    """
    age_minutes = (market_time - position.entry_time).total_seconds() / 60
    
    if age_minutes < WTP_MIN_TIME_MINUTES:
        return None  # Too early
    
    mfe_r = position.calculate_mfe_r()
    current_r = position.calculate_r_realized(current_price)
    
    # Condition 1: Had some profit, now lost most of it
    if mfe_r >= WTP_MIN_MFE_R:
        reversion = (mfe_r - current_r) / mfe_r if mfe_r > 0 else 0
        if reversion >= WTP_REVERSION_THRESHOLD:
            return {'reason': 'WORST_TRADE_PREVENTION', ...}
    
    # Condition 2: Never went into meaningful profit
    if age_minutes >= WTP_MAX_TIME_MINUTES and mfe_r < WTP_MIN_MFE_R:
        return {'reason': 'WORST_TRADE_PREVENTION', ...}
    
    return None
```

## 4.5 TIME_INVALIDATION

```python
def check_mediocre_trade_exit(position, current_price, market_time, adx):
    """
    Exit trades that haven't worked within time limit.
    
    ⭐ Jan 27 Fix: Different thresholds for TREND vs RANGE
    """
    age_minutes = (market_time - position.entry_time).total_seconds() / 60
    
    # RANGE markets (ADX < 18): More patience
    if adx is not None and adx < 18:
        TIME_LIMIT = 90  # minutes
        MAX_LOSS = -0.60  # R (more tolerance for chop)
    else:  # TREND
        TIME_LIMIT = 60  # minutes
        MAX_LOSS = -0.30  # R (strict in trend)
    
    current_r = position.calculate_r_realized(current_price)
    
    if age_minutes >= TIME_LIMIT:
        if current_r <= MAX_LOSS:
            return {'reason': 'TIME_INVALIDATION_MAX_LOSS', ...}
        elif current_r <= 0:
            return {'reason': 'TIME_INVALIDATION', ...}
    
    return None
```

---

# 5. SIZING (TAMAÑO DE POSICIÓN)

## 5.1 Fórmula Base

```python
base_size = (capital * risk_per_trade) / risk_in_points
# risk_per_trade default = 0.01 (1% of capital)
```

## 5.2 Multiplicadores (16 en total)

| # | Multiplicador | Rango | Condición |
|---|---------------|-------|-----------|
| 1 | `edge_mult` | 0.5-1.0 | Edge enforcement (max loss tracking) |
| 2 | `score_mult` | 0.0-1.0 | Score < 3.5=0, < 4.5=0.25, < 5.5=0.5, else 1.0 |
| 3 | `rr_mult` | 0.0-1.0 | RR < 0.7=0, < 0.8=0.25, else 1.0 |
| 4 | `ltf_mult` | 0.25-1.0 | LTF confirmations (0/3=0.25, 1/3=0.5, 2+/3=1.0) |
| 5 | `loss_streak_mult` | 0.3-1.0 | 0 losses=1.0, 1=0.7, 2=0.5, 3+=0.3 |
| 6 | `session_mult` | 0.3-1.0 | DEAD_HOURS=0.3, moderate=0.5, good=1.0 |
| 7 | `day_mult` | 0.6-1.0 | Mon/Fri=0.8, weekend=0.6, else=1.0 |
| 8 | `spread_mult` | 0.8-1.0 | High spread reduces size |
| 9 | `countertrend_mult` | 0.5-1.0 | Counter-trend = 0.5 |
| 10 | `anticipation_mult` | 0.3-1.0 | Anticipation entry = 0.3 |
| 11 | `regime_mult` | 0.5-1.0 | RANGE=0.7, TRANSITION=0.8, TREND=1.0 |
| 12 | `volatility_mult` | 0.5-1.0 | ATR_ratio > 1.5 = 0.5 |
| 13 | `gate_cap` | 0.5-1.0 | Quality gate size cap |
| 14 | `range_mult` | 0.5-1.0 | Range regime reduction |
| 15 | `daily_regime_mult` | 0.7-1.0 | Daily regime (RANGE_DAY=0.7) |
| 16 | `location_cap` | 0.5-1.0 | Near extreme = 0.5 |

## 5.3 Fórmula Final

```python
final_size = base_size 
    * edge_mult 
    * score_mult 
    * rr_mult 
    * ltf_mult
    * loss_streak_mult 
    * session_mult 
    * day_mult 
    * spread_mult
    * countertrend_mult 
    * anticipation_mult 
    * regime_mult
    * volatility_mult 
    * gate_cap 
    * range_mult 
    * daily_regime_mult
    * location_cap

# Ensure minimum
final_size = max(final_size, KRAKEN_BTC_MIN_SIZE)  # 0.0001
```

## 5.4 Ejemplo de Cálculo

```
Capital: $10,000
Risk per trade: 1%
Entry: $88,000
Stop Loss: $87,500
Risk: $500 per BTC → $50 risk per trade

base_size = 50 / 500 = 0.1 BTC = $8,800 notional

Multiplicadores aplicados:
- score_mult: 0.5 (score 5.2)
- rr_mult: 0.25 (RR 0.75)
- session_mult: 0.8 (NY)
- loss_streak: 0.7 (1 loss)
- day_mult: 1.0 (Wednesday)
- regime_mult: 0.7 (RANGE)

Combined: 0.5 * 0.25 * 0.8 * 0.7 * 1.0 * 0.7 = 0.049

final_size = 0.1 * 0.049 = 0.0049 BTC = $431 notional
```

---

# 6. RÉGIMEN DE MERCADO

## 6.1 Clasificación ADX

```python
HTFFilters:
    ADX_RANGE_MAX = 18        # ADX < 18 → RANGE
    ADX_WEAK_TREND_MAX = 25   # ADX 18-25 → WEAK_TREND
    # ADX >= 25 → TREND
```

## 6.2 Diferencias TREND vs RANGE

| Aspecto | TREND | RANGE |
|---------|-------|-------|
| SL Multiple | 1.2 ATR | 0.8 ATR |
| TP Target | 1.5R | 1.0R |
| Chase Penalty | -1 per 0.5 ATR | -1.5 per 0.5 ATR |
| Size Mult | 1.0 | 0.7 |
| TIME_INVALIDATION | -0.30R max | -0.60R max |
| RR Floor (HARD) | 0.7R | 0.85R |

## 6.3 Daily Regime Detection

```python
DailyRegimeConfig:
    ADX_RANGE_THRESHOLD = 18  # Below = RANGE_DAY
    
    RANGE_DAY_ADJUSTMENTS:
        tp_r_target: 1.0      # Reduced TP
        size_multiplier: 0.7  # Reduced size
        max_position_pct: 1.5 # Lower max position
```

---

# 7. SESIONES

## 7.1 Horarios UTC

| Sesión | Horario UTC | Calidad |
|--------|-------------|---------|
| **LONDON** | 07:00-12:00 | good |
| **LONDON_NY_OVERLAP** | 12:00-16:00 | good |
| **NY** | 16:00-21:00 | good |
| **ASIA** | 23:00-07:00 | moderate |
| **DEAD_HOURS** | 21:00-23:00 | poor |

## 7.2 Session Quality Impact

```python
OperatingMode:
    SESSION_SIZE_MULTIPLIERS = {
        'LONDON': 1.0,
        'LONDON_NY_OVERLAP': 1.0,
        'NY': 1.0,
        'ASIA': 0.7,
        'DEAD_HOURS': 0.30,  # ⚠️ But HARD BLOCK blocks 'poor' anyway
    }
    
    SESSION_QUALITY_MAP = {
        'LONDON': 'good',
        'LONDON_NY_OVERLAP': 'good', 
        'NY': 'good',
        'ASIA': 'moderate',
        'DEAD_HOURS': 'poor',  # ⭐ HARD BLOCKED
    }
```

## 7.3 Session Limits

```python
SessionLimits:
    MAX_TRADES_PER_SESSION = {
        'LONDON': 3,
        'LONDON_NY_OVERLAP': 3,
        'NY': 3,
        'ASIA': 2,
        'DEAD_HOURS': 1,
    }
    
    # Override with high score
    SESSION_LIMIT_OVERRIDE_SCORE = 7.0
    SESSION_LIMIT_OVERRIDE_SIZE = 0.5  # 50% size when overriding
```

---

# 8. SALIDAS

## 8.1 Tipos de Exit (en orden de prioridad)

| # | Exit Type | Condición | Prioridad |
|---|-----------|-----------|-----------|
| 1 | `STOP_LOSS` | Price hit SL | HIGHEST |
| 2 | `TAKE_PROFIT` | Price hit TP | HIGH |
| 3 | `TRAILING_STOP` | Price hit trailing | HIGH |
| 4 | `WORST_TRADE_PREVENTION` | Mediocre trade detection | MEDIUM |
| 5 | `TIME_INVALIDATION_MAX_LOSS` | Time limit + losing | MEDIUM |
| 6 | `TIME_INVALIDATION` | Time limit + breakeven | MEDIUM |
| 7 | `VOLATILITY_SQUEEZE_EXIT` | BB squeeze with profit | LOW |
| 8 | `VPOC_EXIT` | Price reached VPOC | LOW |
| 9 | `REGIME_CHANGE` | Market regime changed | LOW |
| 10 | `MANUAL` | User command | LOWEST |

## 8.2 Scaled Partials

```python
PartialProfitConfig:
    # TP1 - First partial
    TP1_TRIGGER_R = 1.0      # Trigger at 1.0R profit
    TP1_CLOSE_PERCENT = 0.5  # Close 50% of position
    
    # After TP1: Move SL to breakeven
    BREAKEVEN_LOCK = True
    
    # TP2 - Trail the rest
    TP2_TRAIL_ATR = 1.5      # Trail with 1.5 ATR
```

---

# 9. CONFIGURACIÓN CLAVE

## 9.1 Risk Management

```python
RiskManagement:
    RISK_PER_TRADE = 0.01         # 1% of capital
    MAX_DAILY_RISK = 0.03         # 3% daily max loss
    MAX_POSITION_PCT = 2.0        # 2% of capital per position
    CAPITAL_BUFFER = 0.9          # Use 90% of available
```

## 9.2 Order Execution

```python
OrderExecution:
    USE_POST_ONLY = True           # Maker orders only
    USE_MARKET_ON_MOMENTUM = True  # Market for fast moves
    MOMENTUM_VELOCITY_MULT = 2.0   # Velocity threshold
    
    # Chase prevention
    ANTI_CHASING_ENABLED = True
    ANTI_CHASING_ATR_THRESHOLD = 2.5  # Max chase
    ANTI_CHASING_LOOKBACK = 5         # Candles
```

## 9.3 Fees (Kraken Futures)

```python
FeeCalculator:
    MAKER_FEE = 0.0002    # 0.02%
    TAKER_FEE = 0.0005    # 0.05%
    DEFAULT_FEE = 0.0005  # Use taker as worst-case
```

---

# 10. MEJORAS JAN 27, 2026

## 10.1 HARD BLOCKS Implementados

```python
class HardEntryBlockValidator:
    """
    NO EXCEPTIONS. NO BYPASS. NO HIGH SCORE OVERRIDE.
    """
    
    HARD_RR_FLOOR = 0.7
    BLOCKED_SESSION_QUALITIES = {'poor'}
    
    @classmethod
    def validate_entry(cls, session_quality, worst_case_rr, adx, score, score_timing):
        # BLOCK 1: Poor session
        if session_quality == 'poor':
            return HardBlockResult(blocked=True, reason='session_quality')
        
        # BLOCK 2: Low RR
        if worst_case_rr < 0.7:
            return HardBlockResult(blocked=True, reason='rr_floor')
        
        # BLOCK 3: Ranging market + low RR
        if adx < 18 and worst_case_rr < 0.85:
            return HardBlockResult(blocked=True, reason='range_rr')
        
        # BLOCK 4: Bad timing + marginal RR
        if score_timing == 0 and worst_case_rr < 1.0:
            return HardBlockResult(blocked=True, reason='timing_zero')
        
        return HardBlockResult(blocked=False)
```

## 10.2 🎯 EDGE STATE MANAGER (NUEVO)

```
┌─────────────────────────────────────────────────────────────────────┐
│ FILOSOFÍA: EL EDGE ES UN EVENTO DISCRETO, NO UN CONTEXTO CONTINUO  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  El sistema NO busca entradas continuamente.                        │
│  Solo opera cuando hay un EDGE ACTIVO.                              │
│                                                                     │
│  Estados:                                                           │
│  ┌──────────┐    BOS      ┌────────────┐  Pullback  ┌────────────┐ │
│  │ NO_EDGE  │ ─────────→  │EDGE_ARMED  │ ─────────→ │EDGE_ACTIVE │ │
│  └──────────┘             └────────────┘            └────────────┘ │
│       ↑                         │                         │        │
│       │                    timeout/                   2 trades/    │
│       │                   invalidate                  timeout      │
│       │                         ↓                         ↓        │
│       │                 ┌──────────────────────────────────┐       │
│       └─────────────────│        EDGE_EXPIRED              │       │
│                         └──────────────────────────────────┘       │
│                                                                     │
│  Reglas:                                                           │
│  - Máximo 2 trades por BOS event                                   │
│  - Edge expira después de 30 min sin pullback                      │
│  - Edge expira después de 15 min sin entrada (post-pullback)       │
│  - Si 2 trades fallan → edge "quemado"                             │
│                                                                     │
│  Beneficios:                                                        │
│  - Elimina overtrading en zonas sin asimetría                      │
│  - Limita revenge trading algorítmico                              │
│  - Mide EXPECTANCY POR EDGE (la métrica que importa)               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Configuración EdgeStateManager

```python
EdgeStateManager:
    MAX_TRADES_PER_EDGE = 2          # Máximo 2 intentos por BOS
    EDGE_ARMED_TIMEOUT_MIN = 30      # Timeout si no hay pullback
    EDGE_ACTIVE_TIMEOUT_MIN = 15     # Ventana de entrada tras pullback
    PULLBACK_MIN_ATR = 0.3           # Mínimo pullback en ATR
    PULLBACK_MAX_ATR = 1.5           # Máximo pullback en ATR
```

### Métricas del Edge

```python
EdgeStats:
    expectancy_per_bos: float    # R promedio por evento BOS (LA MÉTRICA CLAVE)
    expectancy_per_trade: float  # R promedio por trade
    total_bos_events: int        # Cuántos BOS detectados
    total_trades: int            # Trades ejecutados
    win_rate: float              # Porcentaje de wins
```

## 10.3 Código Eliminado

| Código | Razón |
|--------|-------|
| Anti-paralysis (activity-based threshold reduction) | Aceptaba trades basura por inactividad |
| HIGH_SCORE_RR_BYPASS | HARD BLOCKS lo precede, nunca se ejecutaba |
| PROBE BLOCK in poor session | HARD BLOCKS bloquea antes |

## 10.3 Umbrales Unificados

| Antes | Después | Razón |
|-------|---------|-------|
| `RR_BLOCK_FLOOR = 0.30` | `0.70` | Alineado con HARD BLOCK |
| `RR_PROBE_MAX = 0.50` | `0.80` | Coherente |
| `ADX_RANGE_THRESHOLD = 20` | `18` | Usa `HTFFilters.ADX_RANGE_MAX` |

## 10.4 ActivityTracker

```python
class ActivityTracker:
    """
    SINGLE SOURCE OF TRUTH para actividad de trading.
    
    Antes: _last_trade_time disperso en múltiples lugares
    Ahora: Un singleton centralizado
    """
    
    # Tracking (solo observabilidad, no bloquea)
    _consecutive_losses: int
    _losses_today: int
    _wins_today: int
    _worst_trade_preventions_today: int
    _pnl_today_r: float
    
    def record_trade_closed(self, market_time, exit_reason, pnl_r):
        """Registra cierre de trade para estadísticas."""
        ...
```

## 10.5 Bugs Críticos Corregidos

| Bug | Fix |
|-----|-----|
| `validate_rr()` firma incorrecta | Corregidos parámetros |
| `rr_result.size_modifier` inexistente | `→ rr_size_multiplier` |

---

# ANEXO: QUICK REFERENCE

```
ENTRY REQUIREMENTS:
✓ Score >= 5.0 (4.5 in RANGE, 4.0 discovery)
✓ Structure score > 0
✓ worst_case_rr >= 0.7R (0.85R if RANGE)
✓ session_quality != 'poor'
✓ timing_score > 0 OR rr >= 1.0R
✓ chase < 2.5 ATR
✓ cost_ratio < 0.35

SIZING CAPS:
- Min size: 0.0001 BTC
- Max single position: 2% of capital
- Max daily risk: 3% of capital

EXIT TRIGGERS:
- Stop Loss hit
- Take Profit hit  
- Trailing stop triggered
- WORST_TRADE_PREVENTION (mediocre trade)
- TIME_INVALIDATION (60min TREND, 90min RANGE)
- Regime change

SESSIONS (UTC):
07-12: LONDON (good)
12-16: OVERLAP (good)
16-21: NY (good)
21-23: DEAD_HOURS (BLOCKED)
23-07: ASIA (moderate)
```

---

*Documento generado: 27 Enero 2026*
*Sistema: FideiRatioCapital DayTrader v2.1*
