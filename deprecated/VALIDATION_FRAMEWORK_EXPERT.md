# 🎯 VALIDATION FRAMEWORK - Perspectiva Operativa Cuant

**Incorpora feedback de revisión experta**  
**Fecha**: Enero 3, 2026  
**Status**: Plan operativo riguroso para pasar a live trading

---

## ✅ ALINEAMIENTO TOTAL EN DIAGNÓSTICO

Tu validación operativa confirma:

| Diagnóstico | Mi análisis | Tu validación | Impacto |
|-------------|-----------|---------------|---------|
| Entra tarde | ✓ Confirmado | ✓ Típico de over-filtering | -2% |
| R:R bajo | ✓ 0.6:1 → 0.8:1 | ✓ Erosión por costos real | -0.7% |
| Costos no modelados | ✓ -8% distorsión | ✓ 5-12% en intradía | CRÍTICO |
| Over-filtering | ✓ -80% oportunidades | ✓ Destruye edge | FATAL |
| Lookahead bias | ✓ Risk -2% a -5% | ✓ OBLIGATORIO auditar | MUST-DO |

**Conclusión**: Mi análisis conservador. Podrías estar 3-5% peor en realidad.

---

## 🔬 PLAN OPERATIVO: 4 FASES RIGUROSAS

### FASE 1: Validación Técnica (Obligatoria)

#### 1.1 Auditar Lookahead Bias
**Criticidad**: 🔴 MÁXIMA

```python
# Metodología: Bar-by-bar validation
# Requiere: Reproducción manual de 20-30 trades

def validate_no_lookahead():
    """
    Para cada candle i en histórico:
    1. Evaluar ALL filtros usando SOLO data[0:i]
    2. Registrar decisión (buy/sell/hold)
    3. Ejecutar orden en data[i]['open']
    4. Verificar que NO usó data[i]['close'] para decisión
    5. Repetir 20+ veces
    
    Si encuentra discrepancias: STOP, revisar filters.py
    """
    
    critical_points = [
        "BOS validation: ¿usa closes[-1] o current_price?",
        "VWAP: ¿usa historial completo o datos futuros?",
        "ATR: ¿se calcula con cierre real o con ticks actuales?",
        "Fibonacci: ¿se dibuja después del cierre o durante la vela?"
    ]
    
    # Resultado esperado: CERO discrepancias
    # Si hay alguna: -3% a -10% de impacto
```

**Checklist**:
- [ ] Revisar `filters.py` línea 280-350 (MTFStructureFilter)
- [ ] Revisar `signal_engine.py` línea 400-450 (order placement timing)
- [ ] Crear test de 30 trades manuales
- [ ] Documentar exactamente dónde se coloca orden (open, close, mid?)

---

#### 1.2 Reproducción Manual de Trades
**Objetivo**: Validar que el bot hace lo que crees

```python
# Tomar 20 trades reales del backtest
# Para CADA uno:

trade_20 = {
    'date': '2025-12-15 14:32',
    'entry_signal_time': '14:31:59',
    'entry_price': 45234.50,
    'setup': 'HTF LONG + BOS 5m + Vol spike 1m',
    
    # Verificación:
    'expected_entry': 45234,  # ±5 pips
    'actual_entry': 45238,    # ±5 pips?
    'discrepancy': 4,         # ¿Normal o lookahead?
    
    'expected_sl': 44950,     # From ATR
    'actual_sl': 44950,       # Match?
    
    'exit_reason': 'TP1 hit',
    'exit_price': 45876,      # ¿Es 1.5R × size?
}

# Si el 80%+ de trades coincide: ✓ PASS
# Si hay desviaciones sistemáticas: ❌ FAIL (lookahead probable)
```

---

### FASE 2: Simplificación Controlada

#### 2.1 Eliminar MTF Filter (Controlado)

**Antes**: HTF(1h) → MTF(5m) → LTF(1m) = 3 filtros

**Experimento A**: Mantener 3 filtros (baseline)
```python
# Corre backtest standard
# Registra:
# - Total trades: 45
# - Avg entry time: -25 min del cierre 5m
# - Profit factor: 0.73
```

**Experimento B**: Eliminar MTF Filter
```python
# Corre backtest sin MTF
# Registra:
# - Total trades: 65 (44% más)
# - Avg entry time: -12 min del cierre 5m (50% más rápido)
# - Profit factor: 0.81 (¿mejoró?)
```

**Criterio de aceptación**:
- Si trades aumentan pero PF decrece: MTF filter tenía valor
- Si trades aumentan Y PF mejora: MTF era redundante (ELIMINAR)
- Si trades aumentan mucho pero PF cae: trade-off a evaluar

**Expected outcome**: Más trades, mejor R:R, PF probablemente mejora.

---

#### 2.2 Market Orders en Momentum

**Lógica condicional**:
```python
price_velocity = abs(close[-1] - close[-2]) / atr_1m

if price_velocity > 2.0:
    # En momentum: ejecutar INMEDIATAMENTE
    use_market_order()
    tracking['momentum_fills'] += 1
else:
    # En consolidación: esperar limit
    use_post_only()
    tracking['limit_fills'] += 1

# Registrar:
# - Momentum fills: N
# - Limit fills: M
# - Fill rates: N/(N+M), M/(N+M)
# - Avg slippage (momentum): X pips
# - Avg slippage (limit): Y pips
```

**Expected impact**: +1% a +3% en fill rate de buenos setups

---

### FASE 3: Validación Estadística

#### 3.1 Tamaño de Muestra

**Crítica**: "¿Es el resultado significativo o ruido?"

```python
# Actual data
total_trades = 45  # TOO SMALL
win_rate = 55%
r_squared_var = 0.12  # Varianza alta

# Cálculo de significancia
confidence_95 = 1.96 * sqrt(p * (1-p) / n)
confidence_95 = 1.96 * sqrt(0.55 * 0.45 / 45)
confidence_95 = ±0.146  # ±14.6%

# Resultado: 55% ± 14.6% = [40%, 70%]
# ES RUIDO ESTADÍSTICO

# Necesito: 150-200 trades mínimo
# Esto requiere: 6-9 meses de backtest
```

**Plan**:
```python
# Extender backtest a 12 meses
# Registrar cada trade:
trades_df = {
    'date': [...],
    'entry_signal': [...],
    'entry_price': [...],
    'exit_price': [...],
    'pnl': [...],
    'r_multiple': [...],  # Ganancia en R
    'holding_time_min': [...],
    'filter_combination': [...]  # Qué filtros activaron
}

# Calcular estadísticas MENSUALES
monthly_stats = trades_df.groupby(month).agg({
    'pnl': ['sum', 'mean', 'std'],
    'r_multiple': ['mean', 'std'],
    'win_rate': lambda x: (x > 0).sum() / len(x)
})

# Si en 12 meses ves:
# Mes A: +3%, Mes B: -2%, Mes C: +1%, ...
# Y la varianza es consistente → Edge real
#
# Si tienes:
# Mes A: +15%, Mes B: -8%, Mes C: +20%, ...
# Y varianza es ENORME → Ruido
```

---

#### 3.2 Sesgo de Optimización

**Riesgo**: Cambios que mejoran backtest histórico pero fracasan forward

```python
# FLAGGED CHANGES:
# 1. "Cambié TP1 de 1.5 a 2.0 porque el backtest mejoró"
#    → Típico overfitting si solo ves 45 trades
#
# 2. "Moví TRAILING_STOP de 1.0x a 2.0x ATR tras auditoría"
#    → Mejor, pero ¿fue por data-mining o lógica real?

# CONTROL: Out-of-sample testing
# Divide datos en 2 períodos:
# - Training: Jan-Jun 2025 (parámetros se ajustan aquí)
# - Testing: Jul-Dec 2025 (NUNCA toques parámetros)
#
# Si Training PF = 0.85, Testing PF = 0.82 → GOOD
# Si Training PF = 0.85, Testing PF = 0.65 → OVERFIT

# Implementar:
split_date = datetime(2025, 7, 1)
backtest_train = data[data.date < split_date]
backtest_test = data[data.date >= split_date]

results_train = backtest(backtest_train)
results_test = backtest(backtest_test, use_params_from=results_train)

# Si results_test degrada > 15%: FAIL (overfit)
```

---

#### 3.3 Dependencia Excesiva de ATR

**Riesgo**: Frágil a cambios de volatilidad

```python
# PROBLEMA:
# - HTF Filter usa ATR(1h)
# - MTF Filter usa ATR(5m)
# - LTF Filter usa ATR(1m)
# - TRAILING STOP usa ATR(1m)
# - STOP LOSS usa ATR(1m)
#
# Si ATR() tiene bug o comportamiento cambia → TODO cae

# VALIDACIÓN:
volatility_regimes = {
    'low': atr_1m < 50,    # <0.1% BTC
    'normal': 50 <= atr_1m <= 150,
    'high': atr_1m > 150,   # >0.3% BTC
}

for regime in volatility_regimes:
    regime_trades = filter_trades_by_atr(regime)
    pf_by_regime = profit_factor(regime_trades)
    
    print(f"{regime}: PF = {pf_by_regime}, N = {len(regime_trades)}")

# Expected:
# low:    PF = 0.70 (difícil, muchos falsas)
# normal: PF = 0.85 (ok)
# high:   PF = 0.75 (stops ampios, trailing activa)

# Si ves:
# low:    PF = 0.50 (PROBLEMA)
# normal: PF = 0.85
# high:   PF = 1.20 (SOSPECHOSO - overfit a volatilidad)
# → Revisar parámetros por régimen
```

---

### FASE 4: Paper Trading + Forward Test

#### 4.1 Checklist Pre-Paper Trading

```python
Pre-flight checks = {
    'lookahead_audit': 'PASS',           # ✓
    'manual_reproduction': '28/30 PASS',  # 93% accuracy
    'statistical_significance': 'PASS',  # N >= 150 trades
    'out_of_sample_test': 'PASS',        # Train vs Test consistency
    'volatility_regimes': 'PASS',        # PF stable across regimes
    'market_orders_logic': 'IMPLEMENTED', # velocity check working
}

if all([v in ['PASS', 'IMPLEMENTED'] for v in Pre_flight_checks.values()]):
    print("✓ READY FOR PAPER TRADING")
else:
    print("❌ NOT READY - Fix issues first")
```

#### 4.2 Paper Trading Protocol

**Duración**: 2 semanas (mínimo 10 trades)

```python
paper_trading_log = {
    'date': [],
    'signal_time': [],
    'expected_entry': [],      # Desde backtest
    'actual_entry': [],        # Paper trading real
    'entry_slippage': [],      # Diferencia (pips)
    'expected_exit': [],
    'actual_exit': [],
    'expected_pnl': [],        # Desde backtest
    'actual_pnl': [],          # Paper trading
    'pnl_variance': [],        # % diferencia
}

# Después de 10+ trades:
variance = mean(abs(pnl_variance))
if variance < 5%:
    print(f"✓ Paper trading matches backtest ({variance}% error)")
    print("READY FOR LIVE TRADING")
elif variance < 15%:
    print(f"⚠️ Acceptable variance ({variance}%), pero monitor closely")
else:
    print(f"❌ Unacceptable variance ({variance}%), STOP and debug")
```

---

## 🚨 "HEALTHY SKEPTICISM" CHECKLIST

Los puntos que cuestionarías:

### ❓ 1. Expectancy con Pocos Trades

**Mi status**: 45 trades históricos

**Riesgo**: Resultado estadísticamente no significativo

**Acción**:
- [ ] Extender backtest a 12 meses (target: 150-200 trades)
- [ ] Calcular confidence intervals
- [ ] Si CI incluye 0 o negativo: EXPECTED VALUE NO PROBADO

**Umbral aceptación**:
```python
# Con 150 trades y 55% WR:
# CI_95% = ±4.1% (55% ± 4.1% = [50.9%, 59.1%])
# Si target es +5% PnL:
# Necesitas: E[PnL] = 0.05 * Capital * Trades_per_month
# Con CI: mínimo E[PnL] = 0.009 * Capital (muy ajustado)

# CONCLUSIÓN: Más de 200 trades recomendado
```

---

### ❓ 2. Sesgo de Optimización

**Mi status**: Cambié TP1 y TRAILING basado en backtest

**Riesgo**: Parámetros optimizados al pasado, fallan en futuro

**Acción**:
- [ ] Train-test split (70/30 o 60/40)
- [ ] Parámetros se ajustan SOLO en training
- [ ] Validar en test (sin ajustar)
- [ ] Si test performance cae >15%: OVERFIT probable

**Implementación**:
```python
# Periodo 1: Jan-Jun 2025 (TRAINING)
# Puedo ajustar: TP1_RISK_MULTIPLE, TRAILING_STOP, etc.
backtest_results_train = run_backtest(data_train)
# → PF = 0.85, WR = 56%

# Periodo 2: Jul-Dec 2025 (TESTING)
# NO ajusto parámetros, solo evalúo
backtest_results_test = run_backtest(data_test, use_params=params_train)
# → PF = 0.81, WR = 54%
# Diferencia: -4.7% PF, -2% WR → ACCEPTABLE

# vs OVERFIT scenario:
# → PF = 0.65, WR = 49%
# Diferencia: -23.5% PF, -7% WR → RED FLAG
```

---

### ❓ 3. Dependencia de ATR

**Mi status**: ATR usado en 5+ módulos

**Riesgo**: Comportamiento frágil en diferentes volatilidades

**Acción**:
- [ ] Segmentar backtest por volatilidad
- [ ] Validar PF en cada régimen
- [ ] Si hay grandes inconsistencias: REVISAR parámetros

**Test framework**:
```python
atr_percentiles = {
    'p25': 45 pips,   # Bajo
    'p50': 100 pips,  # Medio
    'p75': 160 pips,  # Alto
    'p100': 250 pips, # Extremo
}

for atr_level in atr_percentiles:
    subset = [t for t in trades if atr_at_entry ≈ atr_level]
    pf = profit_factor(subset)
    print(f"ATR={atr_level}: PF={pf:.2f}, N={len(subset)}")

# Expected:
# ATR=45:   PF=0.65 (difícil operar bajo volatilidad)
# ATR=100:  PF=0.85 (óptimo)
# ATR=160:  PF=0.80 (aceptable)
# ATR=250:  PF=0.75 (stops grandes, spread > margin)

# RED FLAG:
# ATR=100:  PF=0.95 (¿overfit a esta condición?)
# ATR=160:  PF=0.50 (¿sistema no adapta a high vol?)
```

---

## 📋 GO/NO-GO CHECKLIST

**Antes de live trading, todos deben ser GREEN**:

```
FASE 1 - VALIDACIÓN TÉCNICA
├─ [ ] Lookahead bias audit completado → 0 discrepancias
├─ [ ] 30 trades reproducidos manualmente → 90%+ accuracy
├─ [ ] Exactitud de entry/exit registrada → ±5 pips
└─ [ ] Timing de órdenes validado → on time, no early

FASE 2 - SIMPLIFICACIÓN
├─ [ ] Experimento MTF elimination realizado
├─ [ ] Profit factor comparado (antes vs después)
├─ [ ] Market orders en momentum implementado
└─ [ ] Fill rate mejorado registrado

FASE 3 - ESTADÍSTICA
├─ [ ] Backtest extendido a 150+ trades
├─ [ ] Confidence intervals calculados
├─ [ ] Train-test split realizado (PF variance < 15%)
├─ [ ] Volatilidad regimes validados
└─ [ ] Dependencia de ATR auditada

FASE 4 - PAPER TRADING
├─ [ ] 2 semanas completadas
├─ [ ] 10+ trades en paper
├─ [ ] PnL variance < 5% vs backtest
├─ [ ] No se encontraron surprises
└─ [ ] Sistema es predecible y reproducible

FINAL GO/NO-GO
├─ [ ] 7/7 fases PASS
└─ [ ] Risk manager da luz verde
```

---

## ⏱️ TIMELINE REALISTA

```
ESTA SEMANA (6 horas dev):
├─ Auditor lookahead bias (3h)
├─ Reproducir 30 trades (1.5h)
├─ Implementar market orders (1.5h)
└─ Documentar exactitud

PRÓXIMA SEMANA (2 horas dev + data):
├─ Extender backtest a 12m (30m)
├─ Train-test split (30m)
├─ Volatility regime analysis (30m)
└─ Preparar paper trading

SEMANAS 2-3 (monitoring):
├─ Paper trading 10+ trades
├─ Validate variance
└─ Final GO/NO-GO decision

TOTAL: 8-10 horas dev + 2 semanas forward testing
```

---

## 🎯 CRITERIO FINAL: GO vs NO-GO

### ✅ GO TO LIVE si:
```
1. Lookahead: PASS (0 discrepancias)
2. Stats: 150+ trades, E[R] > 0, CI no cruza 0
3. Paper: 10+ trades, variance < 5%, sin sorpresas
4. Train-test: Degradación < 15%
5. Risk: Drawdown máximo < 25%, Sharpe > 0.5
```

### ❌ NO-GO si:
```
1. Lookahead: Discrepancias encontradas
2. Stats: E[R] negativo, CI incluye 0
3. Paper: Variance > 15%, sorpresas frecuentes
4. Train-test: Degradación > 20%
5. Volatility: Comportamiento inconsistente por régimen
```

---

## 💾 DOCUMENTACIÓN REQUERIDA ANTES DE LIVE

```
/live_trading_checklist/
├─ lookahead_audit.txt          ← Resultados de auditoría
├─ manual_reproduction_30trades.xlsx ← Validación manual
├─ train_test_split_results.json    ← Out-of-sample performance
├─ volatility_regime_analysis.csv   ← PF por ATR nivel
├─ paper_trading_log.csv            ← 2 semanas de resultados
├─ final_parameters.json            ← Parámetros finales (locked)
└─ risk_approval.txt                ← Risk manager sign-off
```

---

**Status**: Framework riguroso establecido
**Próximo paso**: Ejecutar FASE 1 (auditoría lookahead)
**Criterio de éxito**: Pasar todas las fases con PASS/GREEN
