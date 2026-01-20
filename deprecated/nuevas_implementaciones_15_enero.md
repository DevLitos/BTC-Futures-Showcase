

0) Convención común (para todos los filtros)
Interfaz estándar (recomendado)
Definí una estructura única de retorno para cualquier filtro (esto te evita lógica duplicada):

Python
@dataclass
class FilterDecision:
    allow: bool                 # True/False (hard block)
    score_adjust: float = 0.0   # +/-
    strength: float = 0.0       # 0..1
    regime: str | None = None   # "RANGE", "CHAOS", ...
    reasons: list[str] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)
    cooldown_seconds: int = 0   # si querés que un filtro pueda pedir pausa
Integración mínima en signal_engine.py:

En _handle_idle_state: si decision.allow == False => return (no abre).
En _calculate_entry_score: sumar score_adjust.
Guardar tags en _current_trade_tags.
Esto te permite implementar todo lo que sigue de forma limpia.

1) CHOP/RANGO: agregar BB Width check (Bollinger Band width)
Qué hace
Detecta rangos estrechos (compresión) donde las señales trend-following fallan por whipsaw, incluso si ADX no lo detecta a tiempo.

Cómo calcular BB width
En TF de ejecución (o 5m si tu señal es 1m/3m):

basis = SMA(close, 20)
dev = STD(close, 20)
upper = basis + 2*dev
lower = basis - 2*dev
bb_width = (upper - lower) / basis (normalizado)
Reglas recomendadas
Si bb_width < 0.008 (0.8%) => RANGE (penalizar score y/o exigir score mínimo más alto)
Si bb_width < 0.005 (0.5%) => NO TRADE (porque es compresión extrema)
Notas:

Los umbrales dependen del TF: en 5m para BTC esos valores suelen andar; calibralo con histograma.
Integración
Meter en tu RegimeDetector (si existe) o crear bb_width_filter.py.
Corre junto con ADX y combinás:
if chaos: NO TRADE
elif range_by_adx or range_by_bbwidth: RANGE
else: TREND
Tags útiles
bb_width
range_by_bbwidth: bool
2) VOLATILITY SHOCK: TrueRange > k*ATR por vela + pausa temporal
Tu shock actual es “range_expansion > 2.5x + volume_spike”. Bien, pero te falta capturar una vela aislada anómala (wick gigante) aunque no haya volumen spike claro.

Cómo implementarlo
Cálculos
TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
ATR = RMA(TR, 14) (o EMA)
shock_ratio = TR / ATR
Reglas
Si shock_ratio >= 2.5 => entrar en estado SHOCK
Si shock_ratio >= 3.5 => CHAOS inmediato
Pausa temporal (cooldown por shock)
En vez de solo “NO TRADE mientras dure la condición”, guardá un timestamp:

shock_until = now + X minutes
X recomendado:
ratio 2.5–3.5 => 10–15 min
ratio > 3.5 => 20–30 min
Implementación técnica:

Un VolatilityShockFilter mantiene estado interno:
self._shock_until: datetime | None
En cada evaluación:
si now < self._shock_until: allow=False + reasons=["shock_cooldown"]
si no, evalúa la vela actual y setea _shock_until si aplica.
Integración exacta
En _handle_idle_state antes de evaluar score/entradas:

VolatilityShockFilter.evaluate()
Si bloquea, cortás.
Tags
atr, tr, shock_ratio, shock_until
3) Trailing dinámico: pasar de “R steps” a ATR-adaptativo continuo (o híbrido)
Tu trailing por R está bien para consistencia, pero no reacciona al régimen de volatilidad.

Opción A (recomendada): híbrido
Mantenés tus escalones R (para asegurar BE y locks).
Pero el stop dinámico final se calcula como:
Para LONG:

trail_stop = max(current_locked_stop, highest(high, N) - k * ATR)
donde k depende del régimen:
TREND: k=2.5
RANGE: k=1.5
CHAOS: no operar
Y N también puede variar (ej 10 velas en 1m, 6 velas en 3m, etc).

Integración
En tu gestor de posición (donde hoy aplicás BE/+0.4R/etc):

calculás locked_stop por tus reglas R
calculás atr_trail_stop
new_stop = max(locked_stop, atr_trail_stop)
solo mover stop si mejora y respeta min distance y exchange rules.
Tags
trail_k, atr_trail_stop, locked_stop, final_stop
4) Macro trend breaker: agregar 15m + slope hard veto
Tu macro 1h EMA200 + EMA50>EMA200 está bien, pero te falta el “cambio de régimen temprano” que se ve antes en 15m.

Cómo implementarlo
Checks en 15m
price > EMA200(15m) para permitir longs
y/o EMA50(15m) > EMA200(15m) como confirmación
y slope veto duro:
slope(EMA200_15m, lookback=5) < 0 => bloquear longs
o slope(EMA50_15m) < 0 también
Cómo calcular slope
Simple y robusto:

slope = (ema_now - ema_n_periods_ago) / ema_n_periods_ago
usar n=5 velas 15m (1h15) o n=8 (2h)
Integración
Crear MacroTrendFilter que evalúe 1h y 15m y devuelva:

allow=False si veto
score_adjust positivo si súper alineado (ej +0.5/+1)
Tags
macro_ema200_1h, macro_ema200_15m, macro_slope_15m, macro_allowed
5) Liquidity sweep: post-sweep break+retest confirmation
Tu SweepFilter evita salidas por mecha (bien), pero te falta: “ok, hubo sweep; ahora solo entro si se confirma el reversal”.

Cómo implementarlo (2 fases)
A) Detectar sweep event y guardar “zona”
Cuando detectás sweep bullish (barrió lows y cerró arriba):

registrar sweep_low, sweep_high, sweep_time
marcar pending_reversal_confirmation=True por X velas
B) Exigir confirmación break+retest
Para LONG:

Break: cierre por arriba de sweep_high (o del LH anterior)
Retest: vuelve a tocar/penetrar levemente esa zona y cierra arriba
recién ahí allow_long=True
Timeout: si en 10–20 velas no confirma, cancelar.

Integración
Esto es un filtro de entrada (no solo de salida):

PostSweepConfirmationFilter corre antes de abrir posición.
Si hay pending_confirmation, bloquea hasta confirmación.
Tags
sweep_detected, sweep_high, sweep_low, sweep_confirmed
6) BTC Correlation Filter: habilitarlo y hacerlo “contextual” (crítico)
Hoy está ENABLED=False. Si tradeás BTC/USD directamente, “correlación con BTC” es redundante, pero si tradeás otros pares/alts o incluso BTC perp vs spot, el “context filter” sigue siendo útil.

Qué debería hacer realmente
Más que correlación estadística, lo que querés es:

“Si BTC está en CHAOS o bearish fuerte, no hago longs en X”
Implementación recomendada (simple y efectiva)
Inputs
Un feed de BTC en 15m/1h (o el mismo si el símbolo es BTC).
Regímenes: BTC_TREND_UP, BTC_TREND_DOWN, BTC_CHAOS
Reglas
Si BTC_CHAOS => NO TRADE (global)
Si BTC_TREND_DOWN => bloquear LONG en símbolos risk-on, o aplicar penalty fuerte (-2/-3)
Si BTC_TREND_UP => bonus (+0.5/+1)
“CorrelationFilters” como feature flag
Activarlo por config.
Hacerlo dependiente de símbolo:
si symbol startswith BTC: usarlo como “MarketContextFilter” igual, no como “correlation”.
Integración
market_context_filter.py (renombrar ayuda a no confundirse).
Se evalúa temprano en _handle_idle_state.
7) Cost/R: spread vs SL distance + comisiones en R
Esto es clave si tenés muchos PnL 0.00 / micro-trades.

A) Spread vs SL distance check
Para LONG:

risk = entry - stop
spread = ask - bid (o estimado)
regla: spread <= 0.1 * risk (ej)
o risk >= spread * 5 (mínimo 5x spread)
Si no se cumple => bloquear o penalizar score (-2).

B) Comisiones en cálculo de R esperado
Si tu “Expected R ≥ 1.5” no incluye fees, estás sobreestimando.

Implementación:

fee_rate (maker/taker según tu ejecución)
costo ida y vuelta aprox: 2 * fee_rate * entry_price
convertirlo a R: fee_R = fee_cost / risk_per_unit
entonces:
expected_R_net = expected_R_gross - fee_R
exigir expected_R_net >= 1.5
Integración
En tu módulo donde calculás expected R y tamaño:

ajustar expected R por fees
validar spread/risk
8) Overtrading: same-zone prevention + HH/HL reset logic
Este es el otro gran agujero (tu log muestra “entradas en racimo”).

A) Same-zone prevention
Idea: no permitir re-entrar si el precio está “demasiado cerca” de la última entrada fallida.

Definición de zona
zone_center = last_entry_price
zone_radius = 0.3 * ATR (o 0.25%)
si abs(current_price - zone_center) <= zone_radius => bloquear re-entry por X minutos o hasta que haya nuevo HH.
Guardar:

last_trade_entry_price
last_trade_exit_time
last_trade_result (win/loss)
reentry_block_until o blocked_zone activo
B) HH/HL reset logic
Después de una pérdida en LONG:

exigir que el mercado haga:
un nuevo HH respecto a un swing high anterior, o
un HL válido + break del LH antes de permitir otro LONG.
Implementación rápida (sin swings complejos):

“Reset” si close > highest(high, N) (ej N=20 en 1m/3m)
o si EMA20 slope vuelve positiva y price > EMA20 por M velas
Esto corta el “machine-gun” de entradas mientras cae.

Orden recomendado de implementación (para máximo impacto rápido)
MicroTrendFilter (ya lo vas a hacer) + arreglar excepción RSI<30 (condicionarla)
Same-zone + reset HH/HL (te reduce inmediatamente el overtrade)
TrueRange shock + shock cooldown
BB width (para rango real)
Spread vs risk + fees en R
Macro 15m slope veto
Post-sweep break+retest
Trailing híbrido ATR
Checklist de archivos/módulos (propuesta concreta)
filters/micro_trend_filter.py (tu nuevo)
filters/bb_width_filter.py
filters/volatility_shock_filter.py
filters/macro_trend_filter.py (1h + 15m + slope veto)
filters/post_sweep_confirmation_filter.py
filters/cost_filter.py (spread vs risk + fee-adjusted R)
filters/overtrade_filter.py (same-zone + reset logic)
signal_engine.py integración: pipeline + score adjustments + tags

---

## 🛑 TELEGRAM COMMANDS - /stop y /force_stop (Jan 15, 2026)

### Comandos disponibles

| Comando | Acción | Posiciones | Órdenes SL/TP | Bot sigue corriendo |
|---------|--------|------------|---------------|---------------------|
| `/stop` | Cierra posiciones + activa kill switch | ❌ CERRADAS | ❌ Canceladas | ✅ Sí (pero no opera) |
| `/force_stop` | Apaga la aplicación | ✅ INTACTAS | ✅ Activas en exchange | ❌ No |
| `/status` | Muestra estado del bot | - | - | - |

### Flujo de `/stop`

```
Usuario envía /stop
    ↓
1. emergency_close_all() - Cierra todas las posiciones
    ↓
2. kill_switch.activate() - Activa el kill switch
    ↓
Bot sigue corriendo pero NO abre nuevos trades
```

### Flujo de `/force_stop`

```
Usuario envía /force_stop
    ↓
1. shutdown(preserve_orders=True)
    ↓
2. Apaga WebSocket, REST, logging, etc.
    ↓
3. Las órdenes SL/TP permanecen en el exchange
    ↓
Aplicación termina
```

### Archivos modificados

1. **output/output_manager.py**
   - `set_shutdown_callback()` - Nuevo callback para shutdown
   - `_handle_stop_command()` - Ahora activa kill switch además de cerrar
   - `_handle_force_stop_command()` - Nuevo handler para shutdown
   - `_handle_status_command()` - Muestra estado del kill switch
   - `start_command_listener()` - Registra el nuevo comando

2. **main.py**
   - `_telegram_shutdown()` - Nuevo método para shutdown desde Telegram
   - `shutdown(preserve_orders=False)` - Nuevo parámetro para preservar órdenes
   - Registro de `set_shutdown_callback()` en `start()`

### Seguridad

- Solo responde a comandos del `TELEGRAM_CHAT_ID` configurado
- Comandos de otros chat_id son rechazados con log de warning- Comandos de otros chat_id son rechazados con log de warning

---

## FIX: EMA Distance Guard + Micro Trend Improvements (20:30 UTC)

### Problema identificado
El bot entró LONG a $95,788 cuando:
- EMA20 (5m) = $96,096 (precio 0.32% debajo)
- EMA50 (5m) = $96,308 (precio 0.54% debajo)
- RSI = 36.6 (zona de sobreventa)
- Estructura claramente bajista en 5m

El MicroTrendFilter no bloqueó porque:
1. `confirmed_strength` no alcanzó el threshold `STRENGTH_HARD_BLOCK = 0.65`
2. La histéresis (`HYSTERESIS_BARS_TO_BLOCK = 2`) no había confirmado la tendencia

### Cambios implementados

#### 1. EMA Distance Guard (signal_engine.py, línea ~1474)
**Nueva verificación antes del scoring:**
- HARD BLOCK si precio > 1% debajo de EMA20 (5m) para LONGs
- HARD BLOCK si precio > 1% arriba de EMA20 (5m) para SHORTs
- WARNING (solo log) si precio 0.5-1% en dirección incorrecta

```python
EMA_DISTANCE_BLOCK_PCT = 0.01   # 1% threshold
EMA_DISTANCE_WARNING_PCT = 0.005  # 0.5% warning
```

#### 2. Confluence Boost (micro_trend_filter.py)
**Nuevo multiplicador cuando múltiples señales fuertes coinciden:**
- 3 señales alineadas → 1.25x strength multiplier
- 4+ señales alineadas → 1.50x strength multiplier

Señales contadas para BEARISH:
- EMA20 < EMA50
- Swing LL detectado
- EMA20 slope < -1.5
- 1m bearish confirmation
- RSI < 40

#### 3. Thresholds reducidos (micro_trend_filter.py)
```python
STRENGTH_HARD_BLOCK = 0.55   # Antes: 0.65
STRENGTH_SOFT_BLOCK = 0.40   # Antes: 0.45
```

#### 4. Fix check_btc_trend (advanced_filters.py)
Arreglado el error `'ExternalDataManager' object has no attribute 'get_ohlcv'`:
- Ahora verifica si el método existe antes de llamarlo
- Retorna `True` (allow) si el método no está disponible

### Archivos modificados
1. `engine/signal_engine.py` - EMA Distance Guard
2. `engine/micro_trend_filter.py` - Confluence boost + thresholds reducidos + fix to_dict()
3. `engine/advanced_filters.py` - Fix check_btc_trend

### Tests
✅ 20/20 tests pasan
✅ SignalEngine imports OK
