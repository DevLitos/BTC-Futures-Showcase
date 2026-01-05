# Especificaciones Técnicas Finales: Sistema de Trading BTC Perpetuo

Este documento constituye la versión definitiva y consolidada de la estrategia de **Continuación de Tendencia + Pullback Institucional**. Integra las reglas originales, las optimizaciones aceptadas y los protocolos de resiliencia técnica necesarios para una operación profesional.

---

## 1. Arquitectura de Datos y Marcos Temporales
El sistema opera bajo una jerarquía fractal estricta. La desalineación de cualquier nivel anula el setup.

| Nivel | Timeframe | Función Principal | Indicadores Clave |
| :--- | :--- | :--- | :--- |
| **HTF** | 1 Hora (1H) | Sesgo y Fuerza | EMA 50/200, ADX (14), ATR (14) |
| **MTF** | 5 Minutos (5m) | Estructura y ZOI | VWAP (D/S), EMA 20/50, Fibonacci |
| **LTF** | 1 Minuto (1m) | Gatillo y Gestión | Volumen Relativo, CVD, Order Book |

---

## 2. Filtros de Pre-Operación (Bloque 0 y HTF)
Antes de procesar cualquier señal, el bot debe validar el entorno macro y su propia integridad.

### 2.1 Resiliencia Técnica y Kill Conditions
*   **Heartbeat:** Verificación cada 1-2 segundos. Si el WebSocket falla por > 10s, verificar posición vía REST y cerrar a mercado si existe una abierta.
*   **Filtro de Velocidad (Cisne Negro):** Si una vela de 1m ≥ 3 × ATR(1m), cancelar órdenes, no abrir nuevas y activar *cooldown* de 15-30 min.
*   **Límites de Pérdida:** Drawdown diario ≥ 3% o 3 pérdidas consecutivas apagan el bot.
*   **Entorno Externo:** No operar con Funding > ±0.08%/8h o noticias macro inminentes.

### 2.2 Filtro de Tendencia y Correlación (1H / 5m)
*   **Dirección:** EMA 50 > EMA 200 (Long) / EMA 50 < EMA 200 (Short).
*   **Fuerza:** ADX(14) > 20 **Y** Pendiente del ADX positiva en las últimas 3-5 velas de 1H.
*   **Filtro Macro (Correlación):**
    *   Para **Long**: NQ y ES (5m) no deben estar haciendo *Lower Lows*.
    *   Para **Short**: NQ y ES (5m) no deben estar rompiendo máximos.

---

## 3. Configuración del Setup y Zona de Interés (MTF - 5m)
Buscamos el "respiro" del precio tras un impulso institucional.

1.  **Estructura:** Ruptura de estructura (BOS) con cierre de vela. Mantener HL (Long) o LH (Short).
2.  **Retroceso:** Entre el 38.2% y 61.8% del impulso previo.
3.  **ZOI (Zona de Interés):** Precio entre EMA 20 y EMA 50 (5m), en confluencia con VWAP diario/semanal.
4.  **Anti-Overextension:** No entrar si el precio está a > 2 × ATR(5m) del VWAP diario.
5.  **Prioridad:** Se prioriza el **primer pullback válido** tras el inicio de la sesión dominante.

---

## 4. Protocolo de Entrada (LTF - 1m)
La entrada es un "disparo" de alta precisión basado en flujo de órdenes.

*   **Condiciones Simultáneas:**
    *   Cierre de vela 1m a favor de la tendencia.
    *   Volumen Relativo > 1.5.
    *   Order Book Imbalance ≥ 60/40.
    *   CVD alineado en 1m (confirmación, no excluyente).
*   **Ejecución:** Orden **Limit Post-Only**. Timeout de 3 velas; si no se llena, cancelar. **Prohibido perseguir el precio.**

---

## 5. Gestión de Riesgo y Salidas
La rentabilidad se protege mediante una gestión mecánica y adaptativa.

### 5.1 Parámetros de Riesgo por Sesión
| Ventana Temporal | Riesgo por Trade | Filtros |
| :--- | :--- | :--- |
| **Londres / NY** | 1.0% | Estándar |
| **Apertura NY (9:30-9:45 EST)** | 0.25% - 0.5% | Estrictos (No operar si hay spike) |
| **Asia / Fines de Semana** | 0.5% | Estrictos |

### 5.2 Salidas y Toma de Ganancias
*   **Stop Loss (SL):** Técnico (bajo HL / sobre LH) + Buffer (0.25 × ATR 1m). Inmóvil.
*   **Take Profit 1 (TP1):** Al alcanzar +1R, cerrar 50% y mover SL a BE + fees.
*   **Take Profit 2 (TP2):** Máximo/Mínimo previo o High/Low de sesión.
*   **Trailing Stop:** 1 × ATR (1m).
*   **Salida Anticipada (Climática):** Si Volumen ≥ 2.5-3× media en zona de extensión, cerrar posición inmediatamente.
*   **Filtro de Tiempo:** Si tras 30-45 min no se alcanza TP1, cerrar parcial o totalmente la posición.

---

## 6. Post-Operación y Evaluación
*   **Bloqueo Post-Trade:** 15 minutos de inactividad obligatoria tras cualquier cierre.
*   **Métricas de Supervivencia:** El bot debe mantener un *Profit Factor* ≥ 1.5 y una *Expectancy* > 0. De lo contrario, pasa a modo observación.


PSEUDOCÓDIGO 1
SETUP + ENTRADA (DETECCIÓN Y EJECUCIÓN)
Variables clave
state = IDLE
daily_dd
consecutive_losses
cooldown_timer
last_trade_time

LOOP PRINCIPAL (cada tick / 1s)
WHILE bot_is_on:

    UPDATE market_data (ticks, orderbook, indicators)

    IF kill_conditions_met():
        state = OFF
        BREAK

BLOQUE 0 – KILL CONDITIONS
FUNCTION kill_conditions_met():
    IF daily_dd >= 3%: RETURN True
    IF consecutive_losses >= 3: RETURN True
    IF api_error OR order_desync: RETURN True
    IF abs(funding_rate) >= funding_threshold: RETURN True
    IF macro_event_near: RETURN True
    IF websocket_silent > 10s: emergency_exit()
    RETURN False

BLOQUE 1 – FILTRO HTF (1H)
FUNCTION htf_filter():
    IF EMA50_1H > EMA200_1H AND price > VWAP_daily:
        bias = LONG
    ELSE IF EMA50_1H < EMA200_1H AND price < VWAP_daily:
        bias = SHORT
    ELSE:
        RETURN False

    IF ADX_1H <= 20: RETURN False
    IF ADX_slope_1H <= 0: RETURN False
    IF ATR_1H <= ATR_1H_MA20: RETURN False
    IF spread_abnormal OR volume_collapsed: RETURN False

    RETURN True

BLOQUE 2 – ESTRUCTURA MTF (5m)
FUNCTION mtf_structure():
    IF NOT break_of_structure_confirmed(): RETURN False
    IF pullback_fibo NOT IN [38%, 61.8%]: RETURN False
    IF correction_candles > 3: RETURN False
    IF structure_invalidated (HL/LH broken): RETURN False
    IF price NOT BETWEEN EMA20_5m AND EMA50_5m: RETURN False
    IF NOT near_vwap_daily_or_weekly(): RETURN False

    RETURN True

BLOQUE 3 – DISPARADOR LTF (1m)
FUNCTION ltf_trigger():
    IF candle_close NOT in bias_direction: RETURN False
    IF relative_volume < 1.5: RETURN False
    IF orderbook_imbalance < 60/40: RETURN False
    IF CVD NOT aligned: RETURN False
    IF spread_spike_detected: RETURN False
    IF price_velocity >= 3 * ATR_1m: RETURN False

    RETURN True

BLOQUE 4 – EJECUCIÓN
IF state == IDLE:

    IF cooldown_timer_active(): CONTINUE

    IF htf_filter() AND mtf_structure() AND ltf_trigger():

        CALCULATE stop_level
        CALCULATE position_size (risk based)

        IF position_size < min_size: CONTINUE

        PLACE limit_post_only_order
        START entry_timer
        state = WAIT_FILL

BLOQUE 5 – TIMEOUT DE ENTRADA
IF state == WAIT_FILL:

    IF order_filled:
        state = MANAGE
        RECORD trade_open_time

    ELSE IF entry_timer > 3 candles:
        CANCEL order
        state = IDLE

PSEUDOCÓDIGO 2
GESTIÓN DE TRADE + SALIDAS
BLOQUE 6 – GESTIÓN ACTIVA
IF state == MANAGE:

    UPDATE unrealized_pnl
    UPDATE ATR_1m

STOP LOSS (SIEMPRE ACTIVO)
IF price_hits_stop:
    CLOSE full_position
    UPDATE stats (loss)
    START cooldown (15 min)
    state = IDLE

TP1 – GESTIÓN INICIAL
IF price_reaches_1R AND tp1_not_taken:

    CLOSE 50% position
    MOVE stop_to_BE_plus_fees
    MARK tp1_taken = True

TP2 – GESTIÓN ESTRUCTURAL
IF tp1_taken:

    UPDATE trailing_stop = max(
        current_trailing,
        price - ATR_1m (LONG)
        price + ATR_1m (SHORT)
    )

SALIDA POR VOLUMEN CLIMÁTICO
IF volume_spike >= 3 * volume_mean AND price_extended:

    CLOSE remaining_position
    UPDATE stats (win)
    START cooldown
    state = IDLE

SALIDA POR ESTRUCTURA / TIEMPO
IF price_hits_trailing_stop:
    CLOSE remaining_position
    UPDATE stats
    START cooldown
    state = IDLE

IF time_in_trade > max_trade_time AND tp1_not_taken:
    CLOSE position (partial or full)
    UPDATE stats
    START cooldown
    state = IDLE

BLOQUE 7 – POST TRADE
UPDATE daily_dd
UPDATE expectancy
UPDATE profit_factor

IF daily_metrics_invalid:
    state = OBSERVATION