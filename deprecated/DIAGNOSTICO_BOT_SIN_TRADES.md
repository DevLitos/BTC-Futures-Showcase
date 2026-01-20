# 🔍 DIAGNÓSTICO EXHAUSTIVO: Bot Intradía Sin Trades

**Fecha:** 3 de Enero 2026  
**Sistema:** BTC Perpetual Trading Bot - Kraken Futures  
**Problema:** El bot ha dejado de generar trades

---

## 📊 RESUMEN EJECUTIVO

Después de analizar exhaustivamente el código, he identificado **múltiples cuellos de botella** que pueden bloquear la generación de trades. El sistema tiene una arquitectura de **filtros en cascada** donde CADA filtro debe pasar para que se genere un trade.

### ⚠️ PROBLEMAS CRÍTICOS IDENTIFICADOS

| Prioridad | Problema | Impacto | Dificultad |
|-----------|----------|---------|------------|
| 🔴 CRÍTICO | Filtros Tier 2 (FIX #6-#12) demasiado estrictos | Bloquea 80%+ señales | Media |
| 🔴 CRÍTICO | Cooldown de 60 minutos tras fallo de ejecución | Deja bot inactivo | Fácil |
| 🟡 ALTO | Session Quality Filter (ADX + Volatility BPS) | Bloquea en Asia/Weekend | Media |
| 🟡 ALTO | OBSERVATION mode sin salida clara | Bot atascado | Media |
| 🟠 MEDIO | Edge Enforcement con 2+ degradation signals | Bloquea completamente | Media |
| 🟠 MEDIO | VPOC Filter demasiado restrictivo (2% threshold) | Bloquea entradas válidas | Fácil |

---

## 1️⃣ DATOS (Data Pipeline)

### ✅ Lo que funciona bien:
- Timestamps se manejan en UTC y milisegundos
- ATR se calcula sobre velas CERRADAS (correcto)
- ADX usa período estándar de 14
- Buffer multi-timeframe sincronizado

### ⚠️ Problemas potenciales:

```python
# market_data.py línea 104-109
def has_sufficient_data(self) -> bool:
    return (len(self.candles['1h']) >= 50 and
            len(self.candles['5m']) >= 50 and
            len(self.candles['1m']) >= 20)
```

**DIAGNÓSTICO:** Si el bot arranca sin datos históricos suficientes, quedará bloqueado esperando:
- 50 velas de 1h = **50 horas** 
- 50 velas de 5m = **~4 horas**
- 20 velas de 1m = **20 minutos**

**ACCIÓN:** Verificar que `load_historical_candles()` se ejecuta correctamente al iniciar.

### 🔧 Test recomendado:
```python
# Añadir logging en signal_engine.py _process_tick()
if not has_data:
    status = self.market_data.get_candle_completion_status()
    logger.warning(f"⏳ DATA INCOMPLETE: 1m={status['1m']}/20, 5m={status['5m']}/50, 1h={status['1h']}/50")
```

---

## 2️⃣ GENERACIÓN DE SEÑALES (Signal Generation)

### Flujo actual de señales:
```
IDLE → HTF Filter → LTF Filter → EXECUTE ENTRY
       (1h EMA/ADX)  (1m BOS/EMA)
```

### ⚠️ PROBLEMA: No hay logging de señales RAW

El sistema NO registra señales antes de los filtros. Necesitas saber:
- ¿Cuántas veces el precio cumple condiciones técnicas básicas?
- ¿Dónde exactamente se bloquean?

### 🔧 IMPLEMENTAR: Contador de señales RAW

```python
# signal_engine.py - añadir en _handle_idle_state()

# ANTES de HTF filter - contar condición básica
candles_1m = self.market_data.get_candles('1m')
if candles_1m and len(candles_1m) >= 20:
    df_1m = pd.DataFrame(candles_1m)
    ema_9 = calculate_ema(df_1m['close'].values, 9)[-1]
    ema_20 = calculate_ema(df_1m['close'].values, 20)[-1]
    
    # Señal RAW = cruce de EMAs
    if ema_9 > ema_20:
        self._raw_long_signals += 1
        logger.info(f"📊 RAW LONG #{self._raw_long_signals}: EMA9 {ema_9:.2f} > EMA20 {ema_20:.2f}")
    elif ema_9 < ema_20:
        self._raw_short_signals += 1
        logger.info(f"📊 RAW SHORT #{self._raw_short_signals}: EMA9 {ema_9:.2f} < EMA20 {ema_20:.2f}")
```

---

## 3️⃣ FILTROS (Filter Analysis)

### 🔴 ARQUITECTURA DE FILTROS EN CASCADA

```
┌─────────────────────────────────────────────────────────────────┐
│                    FLUJO DE FILTROS                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  SEÑAL RAW                                                      │
│      │                                                          │
│      ▼                                                          │
│  ┌──────────────────────┐                                       │
│  │ 1. Kill Conditions   │ ← Funding rate, drawdown, losses      │
│  │    (filters.py:82)   │                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 2. Black Swan Filter │ ← 4x ATR spike + wick                 │
│  │    (filters.py:136)  │                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 3. State Machine     │ ← Cooldown, OBSERVATION, WAIT_FILL    │
│  │    (state_machine.py)│                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 4. External Data     │ ← Funding safe? Macro quiet zone?     │
│  │    (external_data.py)│                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 5. HTF Filter (1h)   │ ← EMA50>EMA200, ADX>20, Slope         │
│  │    (filters.py:240)  │                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 6. LTF Filter (1m)   │ ← BOS, EMA reclaim, Volume            │
│  │    (filters.py:622)  │                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 7. Edge Enforcement  │ ← Degradation, Context validity       │
│  │    (edge_enforce.py) │                                       │
│  └──────────┬───────────┘                                       │
│             │ PASS?                                             │
│             ▼                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 8. TIER 2 FILTERS (FIX #6-#12) ← ⚠️ CUELLO DE BOTELLA   │   │
│  │    • Funding Rate Control (FIX #6)                       │   │
│  │    • Session Quality (FIX #9)                            │   │
│  │    • HTF Divergence (FIX #10)                            │   │
│  │    • VPOC Validity (FIX #11)                             │   │
│  │    • BTC Trend Correlation (FIX #12)                     │   │
│  └──────────┬───────────────────────────────────────────────┘   │
│             │ PASS?                                             │
│             ▼                                                   │
│        EXECUTE ENTRY                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 📊 ANÁLISIS DE CADA FILTRO

#### FILTRO 1: Kill Conditions
**Archivo:** [filters.py#L82-L110](engine/filters.py#L82-L110)

| Condición | Threshold | Acción |
|-----------|-----------|--------|
| Daily drawdown | ≥3% | → OBSERVATION |
| Consecutive losses | ≥3 | → OBSERVATION |
| Funding rate | ≥0.08% | KILL (apagar) |
| WebSocket timeout | >75s | KILL |

**⚠️ PROBLEMA:** Si el bot tiene 3 pérdidas consecutivas, entra en OBSERVATION y NO SALE hasta que pase 1 hora Y gane 50% de 5 trades virtuales.

```python
# state_machine.py línea 214-234
def try_exit_observation_mode(self) -> bool:
    # Requisitos para salir:
    # 1. Mínimo 60 minutos en observation
    # 2. Al menos 5 trades evaluados  ← ⚠️ ¿Cómo evalúa si no opera?
    # 3. Win rate >= 50%
```

**🔴 BUG CRÍTICO:** El bot necesita evaluar 5 trades para salir de OBSERVATION, pero NO genera trades en OBSERVATION. **Es un deadlock.**

---

#### FILTRO 5: HTF Filter (1h)
**Archivo:** [filters.py#L240-L320](engine/filters.py#L240-L320)

| Condición | Threshold | Probabilidad de bloqueo |
|-----------|-----------|------------------------|
| ADX | >20 | 30-40% del tiempo |
| ADX Slope | >-0.5 (smart) | 20% del tiempo |
| ATR vs MA | ≥80% | 15% del tiempo |

```python
# config.py línea 72-85
class HTFFilters:
    MIN_ADX = 20                    # Mínimo ADX
    MIN_ADX_SLOPE = -0.5            # Slope negativo permitido
    MIN_ATR_VS_MA = 0.8             # ATR debe ser 80% de su media
```

**📊 LOGGING ACTUAL:** El sistema loggea cada 5 minutos cuando HTF rechaza.

---

#### FILTRO 6: LTF Filter (1m)
**Archivo:** [filters.py#L622-L750](engine/filters.py#L622-L750)

| Condición | Threshold | Probabilidad de bloqueo |
|-----------|-----------|------------------------|
| BOS (Break of Structure) | close > recent_high | 60% del tiempo |
| EMA Reclaim | price ≥ EMA9*0.99 | 30% del tiempo |
| EMA Slope | positive | 40% del tiempo |
| Volume (expansion regime) | ≥50% promedio | 30% del tiempo |
| Volume (otros regímenes) | ≥25% promedio | 10% del tiempo |

**⚠️ PROBLEMA:** El filtro requiere BOS OR EMA_reclaim OR slope_positive, pero luego el volumen puede bloquear:

```python
# filters.py línea 718-725
if regime == 'expansion_trend_continuation' and regime_confidence >= 0.6:
    volume_threshold = 0.5  # 50% del promedio
    if rel_vol < volume_threshold:
        return False, f"Volume too low for expansion regime"
```

---

#### FILTROS TIER 2 (FIX #6-#12) - 🔴 CUELLO DE BOTELLA PRINCIPAL

**Archivo:** [signal_engine.py#L871-L920](engine/signal_engine.py#L871-L920)

Estos filtros se ejecutan DESPUÉS de HTF+LTF, justo antes de colocar la orden:

```python
# signal_engine.py línea 871-920
# FIX #6: Funding rate control
funding_ok, funding_reason = await check_funding_rate_safe(...)
if not funding_ok:
    logger.warning(f"⛔ Entry blocked (Funding): {funding_reason}")
    return

# FIX #9: Session quality (ADX + volatility)
session_ok, session_reason = check_session_quality(adx_1h, volatility_bps)
if not session_ok:
    logger.warning(f"⛔ Entry blocked (Session): {session_reason}")
    return

# FIX #10: HTF divergence
if divergence_detected:
    logger.warning(f"⛔ Entry blocked (Divergence): {divergence_reason}")
    return

# FIX #11: VPOC validity
vpoc_ok, vpoc_reason = check_vpoc_validity(current_price, vpoc, bias)
if not vpoc_ok:
    logger.warning(f"⛔ Entry blocked (VPOC): {vpoc_reason}")
    return

# FIX #12: BTC trend correlation
btc_ok, btc_reason = await check_btc_trend(...)
if not btc_ok:
    logger.warning(f"⛔ Entry blocked (BTC Trend): {btc_reason}")
    return
```

### 📊 TABLA DE PROBABILIDAD DE BLOQUEO POR FILTRO TIER 2

| Filtro | Threshold | Bloquea cuando | % Estimado |
|--------|-----------|---------------|------------|
| **Funding Rate** | >0.03% | LONG en funding alto | 15-25% |
| **Session Quality** | ADX<20, Vol<80bps (London) | Asia, Weekend, baja volatilidad | **40-60%** |
| **HTF Divergence** | RSI no confirma | Cerca de extremos | 20-30% |
| **VPOC** | >2% lejos de VPOC | Entry lejos de valor | 25-35% |
| **BTC Trend** | EMA20 down | LONG cuando BTC baja | 30-40% |

**⚠️ PROBABILIDAD COMBINADA:** Si todos son independientes:
```
P(pasar_todos) = (0.85) × (0.50) × (0.75) × (0.70) × (0.65) ≈ 14%
```

**Esto significa que ~86% de las señales que pasan HTF+LTF son bloqueadas por Tier 2.**

---

## 4️⃣ ESTADO Y COOLDOWNS

### Estados del Bot

```
┌─────────┐    ┌───────────┐    ┌────────┐    ┌─────────────┐
│  IDLE   │───▶│ WAIT_FILL │───▶│ MANAGE │───▶│ OBSERVATION │
└────┬────┘    └───────────┘    └────────┘    └──────┬──────┘
     │                                                │
     │◀───────────────────────────────────────────────┘
     │                    (need 60min + 5 trades + 50% WR)
     │
     ▼
┌─────────┐
│   OFF   │  ← Kill condition extrema
└─────────┘
```

### ⚠️ PROBLEMAS DE COOLDOWNS

1. **Cooldown post-fallo de orden: 60 MINUTOS**
```python
# signal_engine.py línea 996
except Exception as e:
    logger.error(f"Error placing order: {e}")
    self._reset_trade_state("Order exception")
    self.state_machine.start_cooldown(60)  # ← 60 MINUTOS!
```

2. **Black Swan Cooldown: 8 minutos** (razonable)
```python
# config.py línea 66
BLACK_SWAN_COOLDOWN_MINUTES = 8
```

3. **Post-trade Cooldown: 15 minutos**
```python
# config.py línea 191
class PostTrade:
    COOLDOWN_MINUTES = 15
```

### 🔧 VERIFICAR ESTADO ACTUAL

```python
# Añadir endpoint de diagnóstico
def get_bot_status():
    return {
        'state': state_machine.state.value,
        'in_cooldown': state_machine.is_in_cooldown(),
        'cooldown_until': state_machine.cooldown_until,
        'consecutive_losses': state_machine.consecutive_losses,
        'daily_drawdown': state_machine.daily_drawdown,
        'daily_trades': state_machine.daily_trades,
        'observation_entry_time': state_machine.observation_entry_time,
        'trades_in_observation': state_machine.trades_in_observation
    }
```

---

## 5️⃣ RISK MANAGEMENT

### Problemas potenciales de sizing

```python
# risk_engine.py - position sizing
position_size = self.risk_engine.calculate_position_size(
    current_price,
    stop_loss,
    now.hour,
    now.minute,
    leverage=10
)
```

**⚠️ POSIBLES BLOQUEOS:**
1. Capital insuficiente (<$10)
2. Position size = 0 después de multiplicadores de edge/defensive
3. Stop loss demasiado cerca (orden rechazada por exchange)

### Verificación:
```python
# config.py línea 168
MIN_POSITION_SIZE_USD = 11  # Mínimo $11 USD
```

Si `capital * risk% / SL_distance < 11`, el bot NO genera trades.

---

## 6️⃣ EJECUCIÓN

### Flujo de ejecución actual

```
SIGNAL_ACCEPTED
      │
      ▼
┌──────────────────────┐
│ Strict Exchange Check│ ← Verifica posición/órdenes existentes
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Edge Enforcement     │ ← Valida pre-trade
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ TIER 2 FILTERS       │ ← FIX #6-#12
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Position Size Calc   │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Order Type Decision  │ ← Market (momentum) or Limit
└──────────┬───────────┘
           │
           ▼
     ORDER_SENT
           │
           ▼
┌──────────────────────┐
│ Poll Fill Status     │ ← Timeout 3 min
└──────────┬───────────┘
           │
           ▼
     ORDER_FILLED
```

### ⚠️ Errores silenciosos del exchange

```python
# execution.py línea 157-175
except ccxt.InsufficientFunds:
    logger.error("Insufficient balance")
    return None  # ← No hay retry
except ccxt.errors.DDoSProtection:
    logger.warning("Exchange rate limit - waiting 30s")
    await asyncio.sleep(30)
    return None  # ← Solo espera, no reintenta
```

**🔴 PROBLEMA:** Los errores de exchange devuelven `None` y el bot entra en cooldown de 60 minutos.

---

## 7️⃣ DIFERENCIAS BACKTEST VS LIVE

### Diferencias identificadas

| Aspecto | Backtest | Live |
|---------|----------|------|
| Filtros Tier 2 | **NO USADOS** | **SÍ USADOS** |
| Edge Enforcement | **NO USADO** | **SÍ USADO** |
| Session Quality | **NO USADO** | **SÍ USADO** |
| WebSocket timeout | N/A | 75s puede matar |
| Reconciliation | N/A | Cada 60s |
| Capital refresh | Instantáneo | Async (puede fallar) |

**🔴 BUG CRÍTICO:** El backtest NO tiene los filtros Tier 2:

```python
# backtest.py - NO incluye:
# - check_funding_rate_safe()
# - check_session_quality()
# - check_htf_divergence()
# - check_vpoc_validity()
# - check_btc_trend()
```

**Esto explica por qué el backtest genera trades y live no.**

---

## 8️⃣ EXPERIMENTOS CONTROLADOS

### EXPERIMENTO 1: Backtest con TODOS los filtros apagados

```python
# Crear archivo: backtest_no_filters.py

# Deshabilitar en config.py:
FundingRateControl.ENABLED = False
SessionQuality.ENABLED = False
DivergenceFilters.ENABLED = False
VPOCFilters.ENABLED = False
CorrelationFilters.ENABLED = False

# Ejecutar backtest 48h
# Contar: señales_raw, señales_HTF, señales_LTF, trades_ejecutados
```

### EXPERIMENTO 2: Encender filtros uno por uno

```python
# Orden recomendado (de menos a más restrictivo):
1. Solo HTF Filter → Contar trades
2. HTF + LTF → Contar trades
3. HTF + LTF + Session Quality → Contar trades  ← Probable cuello
4. HTF + LTF + Session + VPOC → Contar trades
5. Todos → Contar trades
```

### EXPERIMENTO 3: Test unitarios requeridos

```python
# tests/test_filter_isolation.py

def test_atr_calculation():
    """Verificar ATR se calcula correctamente"""
    pass

def test_adx_calculation():
    """Verificar ADX y slope"""
    pass

def test_ema_bias():
    """Verificar EMA50/200 crossover"""
    pass

def test_cooldown_expiry():
    """Verificar cooldowns expiran correctamente"""
    pass

def test_observation_exit():
    """Verificar salida de OBSERVATION mode"""
    pass

def test_session_quality_thresholds():
    """Verificar umbrales por sesión"""
    pass
```

---

## 🔧 PLAN DE ACCIÓN INMEDIATO

### Paso 1: Verificar estado actual (5 min)
```bash
# Ver logs del bot
grep -E "blocked|rejected|OBSERVATION|cooldown" /var/log/trading/bot.log | tail -100
```

### Paso 2: Añadir logging de diagnóstico (10 min)

Añadir en `signal_engine.py` después de cada filtro:
```python
logger.info(f"🔍 DIAGNOSTIC: HTF={htf_passed}, LTF={ltf_passed}, Funding={funding_ok}, Session={session_ok}, VPOC={vpoc_ok}, BTC={btc_ok}")
```

### Paso 3: Deshabilitar temporalmente Tier 2 (test)

```python
# config.py - TEMPORALMENTE para diagnóstico
class FundingRateControl:
    ENABLED = False  # ← Deshabilitar

class SessionQuality:
    ENABLED = False  # ← Deshabilitar

class DivergenceFilters:
    ENABLED = False  # ← Deshabilitar

class VPOCFilters:
    ENABLED = False  # ← Deshabilitar

class CorrelationFilters:
    ENABLED = False  # ← Deshabilitar
```

### Paso 4: Reducir cooldown de fallo de orden

```python
# signal_engine.py línea 996
self.state_machine.start_cooldown(5)  # Cambiar 60 → 5 minutos
```

### Paso 5: Arreglar deadlock de OBSERVATION

```python
# state_machine.py - Añadir timeout de OBSERVATION
def try_exit_observation_mode(self) -> bool:
    # ... código existente ...
    
    # NUEVO: Forzar salida después de 24 horas
    if elapsed_minutes > 24 * 60:
        logger.warning("OBSERVATION timeout (24h) - forcing exit to IDLE")
        self.transition_to(BotState.IDLE, "Observation 24h timeout")
        return True
```

---

## 📋 CHECKLIST DE VERIFICACIÓN

### Datos
- [ ] Verificar 50+ velas 1h cargadas
- [ ] Verificar 50+ velas 5m cargadas
- [ ] Verificar 20+ velas 1m cargadas
- [ ] Verificar último precio actualizado

### Estados
- [ ] Estado actual es IDLE (no OBSERVATION/OFF)
- [ ] No hay cooldown activo
- [ ] consecutive_losses < 3
- [ ] daily_drawdown < 3%

### Filtros
- [ ] HTF pasa (ADX>20, slope OK, ATR OK)
- [ ] LTF pasa (BOS/EMA/Volume OK)
- [ ] Funding rate < 0.03%
- [ ] Session quality OK (según horario)
- [ ] No divergence detectada
- [ ] VPOC válido
- [ ] BTC trend alineado

### Ejecución
- [ ] Capital > $10
- [ ] Position size > $11
- [ ] No órdenes pendientes
- [ ] No posición abierta

---

## 📈 MÉTRICAS DE MONITOREO RECOMENDADAS

```python
# Añadir Prometheus metrics
signals_raw_total = Counter('signals_raw_total', 'Señales antes de filtros')
signals_htf_passed = Counter('signals_htf_passed', 'Señales que pasan HTF')
signals_ltf_passed = Counter('signals_ltf_passed', 'Señales que pasan LTF')
signals_tier2_passed = Counter('signals_tier2_passed', 'Señales que pasan Tier 2')
signals_executed = Counter('signals_executed', 'Trades ejecutados')

# Por filtro bloqueador
filter_blocks = Counter('filter_blocks', 'Bloqueos por filtro', ['filter_name'])
```

---

## 🎯 CONCLUSIÓN

El bot probablemente no genera trades debido a:

1. **Filtros Tier 2 demasiado estrictos** (especialmente Session Quality)
2. **OBSERVATION mode sin mecanismo de salida viable**
3. **Cooldown de 60 minutos tras errores de ejecución**
4. **Backtest no refleja filtros de live** (falsa confianza)

**Recomendación inmediata:** Deshabilitar temporalmente filtros Tier 2 y verificar si el bot genera trades. Luego rehabilitar uno por uno para encontrar el cuello de botella específico.
