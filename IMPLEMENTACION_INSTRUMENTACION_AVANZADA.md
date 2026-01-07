# Implementación de Instrumentación Avanzada V2.1

## Resumen de Cambios

Este documento resume todas las características implementadas según las especificaciones proporcionadas.

---

## 1. 📦 Instrumentación Avanzada: "Caja Negra" de los Trades

### Archivo: `engine/advanced_analytics.py`

#### SL Bins Classification
Cada trade se clasifica en el momento de entrada según su distancia de Stop Loss relativa al ATR:

| Bin | Rango | Descripción |
|-----|-------|-------------|
| **MICRO** | < 0.6 ATR | Alta precisión, alta probabilidad de ser barridos por wick |
| **NORMAL** | 0.6 - 1.2 ATR | Sweet spot donde la estructura suele aguantar |
| **WIDE** | > 1.2 ATR | Trades conservadores o cambios de tendencia mayor |

**Regla**: Si el Bin MICRO tiene Expectancy negativo tras 100 trades, dejar de operarlos.

#### TradeInstrumenter Class
Nueva clase que captura toda la información en el momento de entrada:
- Entry details (price, direction, size, SL, TPs)
- SL Classification (distance, ATR ratio, bin)
- Market Context (ATR, ADX, regime, ATR ratio)
- Session Context (hour, session name)
- Setup Classification (type, grade)
- Risk Parameters (base, adjusted, multipliers)
- Streak State
- Candle Analysis (body ratio, wick dominance)
- Volume Context

---

## 2. 🎯 Modelo de Regímenes de Mercado (Context Engine)

### Archivo: `engine/market_regime.py`

Tres estados definidos matemáticamente usando ADX + Análisis de Velas:

| Régimen | Condición Técnica | Lógica de Gestión |
|---------|-------------------|-------------------|
| **TREND_EXPANSION** | ADX > 25 + Velas con cuerpo grande | Agresivo: Runner 30%, Trailing alejado (3x ATR) |
| **MEAN_REVERSION** | ADX < 20 + Wicks largos en extremos | Conservador: TP1 70%, cerrar runner rápido |
| **CHOPPY_DEAD** | ADX < 15 + Volumen decreciente | Defensivo: Riesgo 0.3x o No-Trade |

#### Implementación sin "Lag"
- Usa Volatilidad Relativa: `ATR_ratio = ATR_current / ATR_baseline`
- Si `ATR_ratio > 2.0`: régimen de expansión/pánico → reduce apalancamiento inmediatamente

#### Clases Implementadas
- `MarketRegime` (Enum)
- `RegimeConfig` (configuración de umbrales)
- `RegimeGuidance` (guía de trading por régimen)
- `MarketRegimeClassifier` (clasificador principal)

---

## 3. 💰 Capital Weighting y Risk Escalation

### Archivo: `engine/risk_middleware.py`

#### Edge Multiplier por Setup
| Setup | Riesgo |
|-------|--------|
| **Grade A** (Ganador histórico) | x1.25 risk |
| **Grade B** (Neutral) | x1.0 risk |
| **Grade C** (Probabilístico) | x0.5 risk |

#### Risk Escalation (Racha)
- **Base Risk**: 0.8% (8€ para cuenta de 1k)
- **Escalation**: +0.1% por cada win después de 5 consecutivas
- **Max Escalated Risk**: 1.5%
- **Hard Cap**: 2.0% (NUNCA exceder)
- **Reset**: 2 pérdidas consecutivas → vuelve al riesgo base

#### Clases Implementadas
- `SetupGrade` (Enum A/B/C)
- `SetupWeightConfig`
- `RiskEscalationConfig`
- `StreakTracker`

---

## 4. 🎯 Gestión del TP1 y Optimización del Runner

### Archivo: `engine/risk_middleware.py`

#### TP1Optimizer
Calcula TP1 óptimo basado en datos de MFE:
- Si el 80% de trades llegan a 1.3R antes de retroceder → mover TP1 de 1.0R a 1.2R
- Calcula Profit Factor teórico si cerráramos todo en TP1

#### ThreePhaseTrailing (3-Phase Trailing Stop)
| Fase | Trigger | Acción |
|------|---------|--------|
| **Fase 1 (Protección)** | Al 1.1R | Mover SL a Break Even + comisiones |
| **Fase 2 (Profit)** | Al 2.0R | Asegurar 1.0R de beneficio real |
| **Fase 3 (Extension)** | Al 2.5R + TREND_EXPANSION | Trailing basado en último swing de 3 velas |

#### BECalculator (Break-Even Real)
- Calcula BE incluyendo round-trip fees
- `BE_price = Entry + (Fees_RoundTrip / Position_Size)`
- Nunca mover a BE antes de cubrir 2x comisiones

---

## 5. 📊 Métrica PPO (Profit per Opportunity)

### Archivo: `engine/risk_middleware.py`

**Fórmula**: `PPO = PnL_Total_Real / Número_de_Señales_Válidas`

**Interpretación**:
- PPO bajo + PnL positivo: Estás filtrando demasiado (dejando dinero)
- PPO alto: Optimizando cada oportunidad al máximo

#### Clase Implementada
- `PPOTracker` - rastrea señales ejecutadas vs filtradas

---

## 6. 👻 Shadow Trades (Trades en la Sombra)

### Archivo: `engine/risk_middleware.py`

Antes de activar Risk Escalation en real, se implementa en modo "Shadow":
- Registra `PnL_Real` (lo que el bot ganó con riesgo fijo)
- Registra `PnL_Escalated` (lo que habría ganado con riesgo dinámico)

**Regla de activación**: Solo activar la lógica real cuando `PnL_Escalated > PnL_Real` durante 30 trades consecutivos.

---

## 7. 🔧 RiskManagerMiddleware

### Archivo: `engine/risk_middleware.py`

Patrón "Middleware" de Gestión:
- NO toca el código que genera la señal de entrada
- Intercepta la orden justo antes de llegar al Execution Engine
- Transforma señales crudas en órdenes aumentadas

**Beneficio**: Si algo falla, desconectar middleware y el bot sigue funcionando (Fail-safe).

#### Clase Principal
`RiskManagerMiddleware` - combina todos los componentes:
- Market Regime
- Setup Weighting
- Risk Escalation
- Shadow Trading
- TP1 Optimization
- 3-Phase Trailing

---

## 8. 📈 Monitoreo en Tiempo Real (Grafana)

### Archivo: `monitoring/metrics.py`

Nuevas métricas de Prometheus para Edge Health:

| Métrica | Descripción |
|---------|-------------|
| `edge_expectancy_current` | Expectancy últimos N trades |
| `edge_expectancy_historical` | Expectancy histórico |
| `edge_health_ratio` | Ratio actual/histórico (alerta si < 0.5) |
| `market_regime_current` | Régimen actual |
| `market_atr_ratio` | ATR vs baseline |
| `risk_streak_current` | Racha actual |
| `risk_escalation_active` | Si escalación está activa |
| `shadow_pnl_real` / `shadow_pnl_escalated` | Comparación shadow |
| `ppo_current` | PPO actual |
| `sl_bin_expectancy` | Expectancy por SL bin |
| `setup_expectancy` | Expectancy por setup |
| `tp1_current_r` | TP1 recomendado |

#### EdgeHealthTracker
Nueva clase en `advanced_analytics.py`:
- Rastrea expectancy rolling vs histórico
- Alerta si rolling < 50% de histórico
- Activa automáticamente "Modo Observación" (riesgo mínimo)

---

## Archivos Creados/Modificados

### Nuevos Archivos
1. `engine/market_regime.py` - Motor de regímenes de mercado
2. `engine/risk_middleware.py` - Middleware de gestión de riesgo
3. `demo_advanced_instrumentation.py` - Demo de integración

### Archivos Modificados
1. `engine/advanced_analytics.py` - Añadido TradeInstrumenter, EdgeHealthTracker
2. `monitoring/metrics.py` - Añadidas métricas de Edge Health

---

## Uso Básico

```python
from engine.market_regime import classify_market_regime
from engine.risk_middleware import process_signal_through_middleware
from engine.advanced_analytics import get_trade_instrumenter, record_trade_for_edge_health

# 1. Clasificar régimen
regime, details = classify_market_regime(adx=27.0, current_atr=600.0)

# 2. Procesar señal
order = process_signal_through_middleware(
    direction="LONG",
    base_size=0.01,
    entry_price=100000.0,
    stop_loss=99200.0,
    setup_name="BOS_RETEST",
    regime=regime,
    capital=5000.0
)

# 3. Instrumentar entrada
instrumenter = get_trade_instrumenter()
instrumenter.instrument_entry(
    trade_id="TRADE_001",
    entry_price=100000.0,
    direction="LONG",
    position_size=order.adjusted_size,
    stop_loss=99200.0,
    atr=600.0,
    adx=27.0,
    setup_type="BOS_RETEST"
)

# 4. Al cerrar: instrumentar salida
record = instrumenter.instrument_exit(
    exit_price=100900.0,
    pnl=56.25,
    exit_reason="TP1_HIT",
    ...
)

# 5. Actualizar Edge Health
record_trade_for_edge_health(record['r_realized'])
```

---

## Ejecutar Demo

```bash
cd /home/carlo/projects/trading/daytrader2.1
python demo_advanced_instrumentation.py
```

---

## Próximos Pasos Recomendados

1. **Integrar con `main.py`**: Conectar el middleware al flujo principal
2. **Configurar Grafana**: Crear dashboards para las nuevas métricas
3. **Backtest**: Ejecutar backtest con la nueva instrumentación
4. **Paper Trading**: Probar en forward-test antes de activar escalación real
