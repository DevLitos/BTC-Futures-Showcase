# 📋 GUÍA DE INTEGRACIÓN: ACTIVAR FIX #6-13
## Pasos finales para activar todas las optimizaciones en signal_engine.py

**Fecha**: Enero 3, 2026  
**Status**: ✅ CÓDIGO IMPLEMENTADO - FALTA INTEGRACIÓN EN signal_engine.py

---

## 🔧 PASOS DE INTEGRACIÓN

### PASO 1: Importar advanced_filters en signal_engine.py

En la sección de imports de `engine/signal_engine.py`, agregar:

```python
# Después de otros imports
from engine.advanced_filters import (
    check_funding_rate_safe,
    should_exit_on_funding,
    check_session_quality,
    check_htf_divergence,
    calculate_vpoc,
    check_vpoc_validity,
    check_btc_trend,
    validate_train_test_split,
    get_session_name_utc
)
```

---

### PASO 2: Agregar validaciones en execute_entry()

En el método `execute_entry()` de `SignalEngine`, antes de crear la orden:

```python
async def execute_entry(self, symbol: str, bias: str, entry_price: float, 
                       stop_loss: float, tp1_price: float):
    """Execute entry with ALL validations"""
    
    # ... existing code ...
    
    # FIX #6: Funding rate check
    funding_ok, funding_reason = await check_funding_rate_safe(
        self.external_data, symbol, bias
    )
    if not funding_ok:
        logger.warning(f"Entry blocked: {funding_reason}")
        return None
    
    # FIX #9: Session quality check
    adx_1h = calculate_adx(self.df_1h['high'], self.df_1h['low'], 
                           self.df_1h['close'])[-1]
    volatility_bps = (self.df_1h['high'].iloc[-1] - self.df_1h['low'].iloc[-1]) / \
                     self.df_1h['close'].iloc[-1] * 10000
    
    session_ok, session_reason = check_session_quality(adx_1h, volatility_bps)
    if not session_ok:
        logger.warning(f"Entry blocked: {session_reason}")
        return None
    
    # FIX #10: HTF divergence check
    div_detected, div_reason = check_htf_divergence(self.df_1h, bias)
    if div_detected:
        logger.warning(f"Entry blocked: {div_reason}")
        return None
    
    # FIX #11: VPOC validity check
    vpoc = calculate_vpoc(self.df_1h)
    vpoc_ok, vpoc_reason = check_vpoc_validity(entry_price, vpoc, bias)
    if not vpoc_ok:
        logger.warning(f"Entry blocked: {vpoc_reason}")
        return None
    
    # FIX #12: BTC correlation check
    btc_ok, btc_reason = await check_btc_trend(self.external_data, bias)
    if not btc_ok:
        logger.warning(f"Entry blocked: {btc_reason}")
        return None
    
    # If all validations pass, proceed with entry
    logger.info("✅ All validations passed - Proceeding with entry")
    
    # ... existing entry code ...
```

---

### PASO 3: Agregar validaciones en manage_position()

En el método de gestión de posición, agregar verificaciones de exit:

```python
async def manage_position(self, position, current_price):
    """Manage open position with FIX #13 time-based exit"""
    
    # ... existing code ...
    
    # FIX #13: Time-based exit check
    should_exit, reason = position.should_force_exit_by_time()
    if should_exit:
        logger.warning(f"⏰ TIME-BASED EXIT: {reason}")
        await self.exit_position(position, reason)
        return
    
    # FIX #6: Check if funding became critical
    funding_exit, funding_reason = await should_exit_on_funding(
        self.external_data, position.symbol, position.direction
    )
    if funding_exit:
        logger.critical(f"💥 FUNDING EXIT: {funding_reason}")
        await self.exit_position(position, funding_reason)
        return
    
    # ... rest of existing code ...
```

---

### PASO 4: Agregar FIX #8 en risk_engine.py calls

En `state_machine.py` o donde se chequea SL, actualizar a:

```python
# ANTES:
# if position.check_stop_hit(current_price):
#     await exit_position(...)

# DESPUÉS (con FIX #8):
if position.check_stop_hit(current_price, current_low, atr_1m):
    await exit_position(...)
```

---

### PASO 5: Backtest Split Validation (FIX #7)

En `backtest.py`, agregar antes de ejecutar:

```python
# Función de split
def train_test_split(df, split_pct=0.70):
    split_idx = int(len(df) * split_pct)
    return df.iloc[:split_idx], df.iloc[split_idx:]

# Uso:
train_df, test_df = train_test_split(df_prices)

train_results = run_backtest(train_df)
test_results = run_backtest(test_df)

is_valid, assessment = validate_train_test_split(train_results, test_results)
logger.info(assessment)

if not is_valid:
    logger.warning("⚠️  OVERFITTING DETECTED - Model may not generalize")
else:
    logger.info("✅ GOOD GENERALIZATION - Safe to trade")
```

---

## ⚙️ PARÁMETROS A AJUSTAR EN config.py

Si necesitas cambiar comportamiento:

```python
# FIX #6: Funding rate
FundingRateControl.ENABLED = True  # o False para deshabilitar
FundingRateControl.MAX_FUNDING_RATE_PCT = 0.03  # en %

# FIX #8: Sweep detection
SweepFilter.ENABLED = True
SweepFilter.MAX_SWEEP_DEPTH_ATR = 0.5  # qué profundidad es barrida

# FIX #9: Session quality
SessionQuality.ENABLED = True
SessionQuality.WEEKEND_TRADING = False

# FIX #13: Time-based exit
TimeBasedExits.ENABLED = True
TimeBasedExits.MAX_HOLD_MINUTES = 120  # 2 horas máximo
TimeBasedExits.NIGHT_EXIT_HOUR = 20  # 8pm UTC
```

---

## 🧪 TESTING CHECKLIST

Antes de live trading, verificar:

```
✅ FIX #1: MTF removido - entry 10-20% más rápido
✅ FIX #2: Market orders activado - fill rate > 90%
✅ FIX #3: Trailing 3-phase funcionando
✅ FIX #4: Órdenes reducidas a 3-4 por trade
✅ FIX #5: Backtest realista (no lookahead)
✅ FIX #6: Funding check bloqueando en ALT FUNDING
✅ FIX #7: Overfitting detector funcionando
✅ FIX #8: Sweep detection activado
✅ FIX #9: Session quality filtrando sesiones malas
✅ FIX #10: Divergencia detectada
✅ FIX #11: VPOC calculado correctamente
✅ FIX #12: BTC trend check funcionando
✅ FIX #13: Time-based exit ejecutándose

Métricas esperadas en backtest:
├─ Trades: +15-25%
├─ WR: +3-5%
├─ R: +0.3-0.6R
├─ PnL: +8-15%
└─ Drawdown: <15%
```

---

## 🐛 TROUBLESHOOTING

Si falta algo:

| Error | Solución |
|-------|----------|
| `ImportError: advanced_filters` | Asegurar que `engine/advanced_filters.py` existe |
| `TypeError: check_funding_rate_safe()` | Verificar que `external_data_manager` está inicializado |
| `AttributeError: position.should_force_exit_by_time` | Verificar que risk_engine.py fue actualizado |
| Backtest overfitting > 10% | Revisar parámetros, hay sobreajuste |
| Paper trading PnL < backtest 50% | Validar fill rate real vs expected |

---

## 📊 VALIDACIÓN POST-IMPLEMENTACIÓN

Una vez integrado, correr:

```bash
# Test imports
python -c "from engine.advanced_filters import *; print('✅ Imports OK')"

# Test backtest
python professional_backtest.py --validate-train-test

# Test live (paper)
python main.py --paper-trading --risk-0.05

# Comparar resultados
# Si PnL backtest vs paper dentro de ±5% → GO
# Si diferencia > 10% → DEBUG antes de live
```

---

## 🎯 CRITERIO GO/NO-GO FINAL

**GO SI:**
```
✅ Backtest muestra +8-15% (con todos los FIX)
✅ Test set (30%) mostración > 80% de train set PnL
✅ WR >= 47%
✅ R >= 1.2
✅ Paper trading mostración ±5% vs backtest
✅ Drawdown < 15%
✅ Sin crashes en ejecución
```

**NO-GO SI:**
```
❌ Overfitting > 10% (test muestra <70% de train)
❌ Lookahead bias detectado
❌ Paper trading < 50% del backtest
❌ Drawdown > 20%
❌ Crashes en ejecución
```

---

## 🚀 LISTO PARA LIVE

Una vez todo validado en paper:

```
1. Set risk a 0.5% en config.py
2. Set PAPER_TRADING = False
3. Monitor primeras 10 trades
4. Si todo OK, scale a 1.0% risk
5. Target: +8-15% mensual consistente
```

---

**Status**: Ready for integration and testing  
**Next**: Implementar en signal_engine.py y run backtest validation
