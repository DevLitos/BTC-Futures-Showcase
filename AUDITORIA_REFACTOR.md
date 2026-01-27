# 🔴 AUDITORÍA Y PLAN DE REFACTORIZACIÓN

**Fecha**: 27 Enero 2026  
**Archivo principal**: `src/application/live/trading_app.py` (9527 → ~9415 líneas después de refactor)  
**Archivo secundario**: `src/domain/risk/risk_engine.py` (3204 líneas)

---

## 📊 RESUMEN EJECUTIVO

El sistema tiene **código duplicado significativo** y **God Classes** que dificultan mantenimiento y generan bugs. Este documento detalla qué cambiar, cómo hacerlo, y en qué orden.

### ✅ PUNTOS COMPLETADOS (Jan 27, 2026)

| Punto | Descripción | Estado |
|-------|-------------|--------|
| 1 | Limpiar imports no usados | ✅ COMPLETADO |
| 2 | Eliminar métodos disabled | ✅ COMPLETADO |
| 3 | IndicatorCache centralizado | ✅ COMPLETADO - `src/shared/indicator_cache.py` |
| 4 | `_close_position_with_full_flow()` | ✅ COMPLETADO - 6 patrones refactorizados |
| 5 | Extraer PositionManager (utilities) | ✅ COMPLETADO - `src/application/live/position_manager.py` |
| 6 | Extracción completa de `_handle_manage_state` | ⚠️ PARCIAL - Demasiado riesgoso hacer de golpe |

### 📊 REDUCCIÓN DE CÓDIGO

- **trading_app.py**: 9527 → 9411 líneas (−116 líneas, ~1.2%)
- **Código duplicado eliminado**: ~200 líneas de patrones close_position
- **Nuevos módulos creados**: 
  - `src/shared/indicator_cache.py` (~300 líneas)
  - `src/application/live/position_manager.py` (~250 líneas)

### ⚠️ PUNTO 5 - ESTADO ACTUAL

El punto 5 (extraer ~3000 líneas de gestión de posiciones) es MUY riesgoso:
- `_handle_manage_state` tiene 1100+ líneas con 12+ responsabilidades distintas
- Tiene dependencias complejas con: reconciler, risk_engine, execution, state_machine
- Mover todo de golpe podría romper el sistema live

**LO COMPLETADO:**
- Funciones utility extraídas y usadas: `calculate_current_r`, `check_bos_against_position`
- Método unificado `_close_position_with_full_flow` que elimina 200+ líneas duplicadas
- Módulo `position_manager.py` listo para ir añadiendo más lógica gradualmente

**LO PENDIENTE (Fase 2, requiere más testing):**
- Mover trailing stop logic completo
- Mover partial exit logic
- Mover exhaustion detection
- Crear clase `PositionManager` stateful

---

## 3. ✅ CACHEAR CÁLCULOS DE INDICADORES (COMPLETADO)

### Problema
Los indicadores (ATR, ADX, RSI, EMA) se calculan múltiples veces por tick en lugar de cachearse:

| Indicador | Ocurrencias | Líneas aproximadas |
|-----------|-------------|-------------------|
| ATR 5m | 7+ | 2350, 2625, 2660, 3612, 4420, 6565, 7567, 7610 |
| ADX 1h | 5+ | 2399, 2680, 5009, 7687, 8172 |
| ADX 5m | 3+ | 3610, 8262, 8293 |
| RSI | 2+ | 4211, 7712 |
| EMA 50/200 | 4+ | 2500, 8175, 8268, 8520 |

### Solución
Crear un **IndicatorCache** que:
1. Almacena el timestamp de la última vela cerrada por timeframe
2. Solo recalcula cuando hay nueva vela cerrada
3. Expone métodos tipo `get_atr_5m()`, `get_adx_1h()`, etc.

### Código a implementar

```python
# src/shared/indicator_cache.py

from dataclasses import dataclass, field
from typing import Dict, Optional
import pandas as pd
import numpy as np
from src.shared.indicators import calculate_atr, calculate_adx, calculate_ema, calculate_rsi

@dataclass
class CachedIndicators:
    """Cached indicator values for a timeframe"""
    last_closed_ts: int = 0
    atr: float = 0.0
    adx: float = 0.0
    adx_slope: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    rsi: float = 50.0
    rsi_slope: float = 0.0

class IndicatorCache:
    """
    Centralized indicator caching system.
    
    Only recalculates when a new candle CLOSES (not forms).
    Key: Use candles[-2] timestamp as cache key (last CLOSED candle).
    """
    
    def __init__(self):
        self._cache: Dict[str, CachedIndicators] = {
            '1m': CachedIndicators(),
            '5m': CachedIndicators(),
            '1h': CachedIndicators(),
        }
    
    def get_atr(self, timeframe: str, candles: list, period: int = 14) -> float:
        """Get ATR for timeframe, recalculating only if needed"""
        if not candles or len(candles) < period + 1:
            return 0.0
        
        # Check if cache is valid (using CLOSED candle timestamp)
        closed_ts = candles[-2].get('timestamp', 0) if len(candles) >= 2 else 0
        if closed_ts == self._cache[timeframe].last_closed_ts:
            return self._cache[timeframe].atr
        
        # Recalculate
        df = pd.DataFrame(candles)
        atr_values = calculate_atr(
            df['high'].to_numpy(),
            df['low'].to_numpy(),
            df['close'].to_numpy(),
            period
        )
        
        atr = float(atr_values[-1]) if len(atr_values) > 0 and not np.isnan(atr_values[-1]) else 0.0
        
        # Update cache
        self._cache[timeframe].atr = atr
        self._cache[timeframe].last_closed_ts = closed_ts
        
        return atr
    
    def get_adx(self, timeframe: str, candles: list, period: int = 14) -> tuple[float, float]:
        """Get ADX and slope for timeframe"""
        if not candles or len(candles) < period + 1:
            return 20.0, 0.0
        
        closed_ts = candles[-2].get('timestamp', 0) if len(candles) >= 2 else 0
        if closed_ts == self._cache[timeframe].last_closed_ts:
            return self._cache[timeframe].adx, self._cache[timeframe].adx_slope
        
        df = pd.DataFrame(candles)
        adx_values, _ = calculate_adx(
            df['high'].to_numpy(),
            df['low'].to_numpy(),
            df['close'].to_numpy(),
            period
        )
        
        adx = float(adx_values[-1]) if len(adx_values) > 0 and not np.isnan(adx_values[-1]) else 20.0
        adx_slope = float(adx_values[-1] - adx_values[-2]) if len(adx_values) > 1 else 0.0
        
        self._cache[timeframe].adx = adx
        self._cache[timeframe].adx_slope = adx_slope
        
        return adx, adx_slope
    
    def get_emas(self, timeframe: str, candles: list) -> tuple[float, float, float]:
        """Get EMA20, EMA50, EMA200 for timeframe"""
        if not candles or len(candles) < 200:
            closes = pd.DataFrame(candles)['close'] if candles else pd.Series([])
            ema20 = float(closes.ewm(span=20).mean().iloc[-1]) if len(closes) >= 20 else 0
            ema50 = float(closes.ewm(span=50).mean().iloc[-1]) if len(closes) >= 50 else 0
            ema200 = float(closes.ewm(span=200).mean().iloc[-1]) if len(closes) >= 200 else 0
            return ema20, ema50, ema200
        
        closed_ts = candles[-2].get('timestamp', 0) if len(candles) >= 2 else 0
        if closed_ts == self._cache[timeframe].last_closed_ts:
            return self._cache[timeframe].ema20, self._cache[timeframe].ema50, self._cache[timeframe].ema200
        
        df = pd.DataFrame(candles)
        closes = df['close']
        
        ema20 = float(calculate_ema(closes.to_numpy(), 20)[-1])
        ema50 = float(calculate_ema(closes.to_numpy(), 50)[-1])
        ema200 = float(calculate_ema(closes.to_numpy(), 200)[-1]) if len(closes) >= 200 else 0.0
        
        self._cache[timeframe].ema20 = ema20
        self._cache[timeframe].ema50 = ema50
        self._cache[timeframe].ema200 = ema200
        
        return ema20, ema50, ema200
    
    def get_rsi(self, timeframe: str, candles: list, period: int = 14) -> tuple[float, float]:
        """Get RSI and slope for timeframe"""
        if not candles or len(candles) < period + 5:
            return 50.0, 0.0
        
        closed_ts = candles[-2].get('timestamp', 0) if len(candles) >= 2 else 0
        if closed_ts == self._cache[timeframe].last_closed_ts:
            return self._cache[timeframe].rsi, self._cache[timeframe].rsi_slope
        
        df = pd.DataFrame(candles)
        rsi_values = calculate_rsi(df['close'].to_numpy(), period)
        
        rsi = float(rsi_values[-1]) if len(rsi_values) > 0 and not np.isnan(rsi_values[-1]) else 50.0
        rsi_slope = float(rsi_values[-1] - rsi_values[-5]) if len(rsi_values) >= 5 else 0.0
        
        self._cache[timeframe].rsi = rsi
        self._cache[timeframe].rsi_slope = rsi_slope
        
        return rsi, rsi_slope
    
    def invalidate(self, timeframe: str = None):
        """Invalidate cache (force recalculation)"""
        if timeframe:
            self._cache[timeframe] = CachedIndicators()
        else:
            for tf in self._cache:
                self._cache[tf] = CachedIndicators()

# Singleton
_indicator_cache: Optional[IndicatorCache] = None

def get_indicator_cache() -> IndicatorCache:
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = IndicatorCache()
    return _indicator_cache
```

### Cambios en trading_app.py

Reemplazar cada llamada a `calculate_atr(...)` por:
```python
# ANTES
atr_5m = calculate_atr(df_5m['high'].to_numpy(), df_5m['low'].to_numpy(), df_5m['close'].to_numpy(), 14)[-1]

# DESPUÉS
atr_5m = self._indicator_cache.get_atr('5m', candles_5m, 14)
```

Añadir en `__init__`:
```python
from src.shared.indicator_cache import get_indicator_cache
self._indicator_cache = get_indicator_cache()
```

---

## 4. EXTRAER close_position_common()

### Problema
El patrón de cierre de posición está duplicado 6+ veces con variaciones mínimas:

```python
# Patrón repetido en líneas: 6530-6550, 6640-6660, 6730-6760, 6805-6835, 7340-7380
await self.execution.close_position_market(
    symbol=self.symbol,
    side='sell' if position.direction == 'LONG' else 'buy',
    amount=close_size,
    reason="REASON"
)
pnl = position.unrealized_pnl
self.state_machine.record_trade_close(pnl, pnl > 0, exit_reason='REASON')
self.state_machine.update_daily_pnl(pnl)
self._record_trade_result('REASON', pnl, market_time)
# ... luego notificación ...
await self.risk_engine.clear_position("...")
self._reset_trade_state("...")
self.state_machine.transition_to(BotState.IDLE, "...")
```

### Solución

Crear método común:

```python
async def _close_position_with_full_flow(
    self,
    position: Position,
    current_price: float,
    exit_reason: str,
    market_time: datetime,
    cooldown_minutes: int = 2,
    send_notification: bool = True,
    use_exchange_size: bool = True,
) -> tuple[bool, float]:
    """
    Unified position close flow with all necessary tracking.
    
    Args:
        position: Position to close
        current_price: Current market price
        exit_reason: Reason string (e.g., 'TRAILING_STOP', 'BOS_INVALIDATION')
        market_time: Current market time
        cooldown_minutes: Minutes to wait before next trade
        send_notification: Whether to send Telegram notification
        use_exchange_size: If True, query exchange for actual size
    
    Returns:
        (success, pnl_usd)
    """
    close_size = position.size
    
    # 1. Get actual size from exchange if requested
    if use_exchange_size:
        try:
            exchange_pos = await self.execution.get_position(self.symbol)
            if exchange_pos:
                close_size = abs(float(exchange_pos.get('contracts', 0) or 0))
        except Exception as e:
            logger.debug(f"Could not get exchange position: {e}")
    
    # 2. Execute close
    try:
        await self.execution.close_position_market(
            symbol=self.symbol,
            side='sell' if position.direction == 'LONG' else 'buy',
            amount=close_size,
            reason=exit_reason
        )
    except Exception as e:
        logger.error(f"Close position failed: {e}")
        return False, 0.0
    
    # 3. Calculate PnL
    if position.direction == 'LONG':
        pnl = (current_price - position.entry_price) * close_size
    else:
        pnl = (position.entry_price - current_price) * close_size
    
    # 4. Record trade result
    is_win = pnl > 0
    self.state_machine.record_trade_close(pnl, is_win, exit_reason=exit_reason)
    self.state_machine.update_daily_pnl(pnl)
    self._record_trade_result(exit_reason, pnl, market_time)
    
    # 5. Calculate R realized
    initial_risk = abs(position.entry_price - position.initial_stop_loss)
    r_realized = pnl / (initial_risk * close_size) if initial_risk > 0 and close_size > 0 else 0
    
    # 6. Log trade
    try:
        await self.trade_logger.log_event('trade_closed', {
            'entry': position.entry_price,
            'exit': current_price,
            'size': close_size,
            'pnl': pnl,
            'reason': exit_reason,
            'r': r_realized,
            'mfe_r': position.calculate_mfe_r(),
            'mae_r': position.calculate_mae_r(),
            'direction': position.direction,
            'regime': self._current_trade_regime
        })
    except Exception as e:
        logger.error(f"Trade logging failed: {e}")
    
    # 7. Send notification
    if send_notification and self.output_manager:
        try:
            _create_safe_task(self.output_manager.send_exit_notification({
                'symbol': self.symbol,
                'reason': exit_reason,
                'pnl': pnl,
                'r_realized': r_realized,
                'exit_price': current_price,
                'entry_price': position.entry_price,
                'direction': position.direction,
                'timestamp': market_time.isoformat(),
                'account_value': self.risk_engine.capital
            }), f"notification_{exit_reason}")
        except Exception as e:
            logger.error(f"Notification failed: {e}")
    
    # 8. Clear state
    await self.risk_engine.clear_position(f"Trade closed: {exit_reason}")
    self._reset_trade_state(f"Trade closed: {exit_reason}")
    
    # 9. Start cooldown
    self.state_machine.start_cooldown(cooldown_minutes)
    self.state_machine.transition_to(BotState.IDLE, f"Trade closed: {exit_reason}")
    
    logger.info(f"✅ Position closed: {exit_reason} | PnL: ${pnl:.2f} ({r_realized:.2f}R)")
    
    return True, pnl
```

### Reemplazos a hacer

En cada lugar donde hay cierre de posición (~6 lugares), reemplazar por:
```python
success, pnl = await self._close_position_with_full_flow(
    position=position,
    current_price=current_price,
    exit_reason='TRAILING_STOP',  # o el que corresponda
    market_time=market_time,
    cooldown_minutes=2
)
if success:
    return
```

---

## 5. EXTRAER MÓDULO DE GESTIÓN DE POSICIONES

### Problema
La clase SignalEngine tiene ~3000 líneas dedicadas a:
- Trailing stops
- Partial exits
- BOS invalidation
- Time-based exits
- Exhaustion detection

### Solución
Crear `src/application/live/position_manager.py`:

```python
# src/application/live/position_manager.py

class PositionManager:
    """
    Manages open position lifecycle: trailing, partials, exits.
    
    Extracted from SignalEngine to reduce god class complexity.
    """
    
    def __init__(
        self,
        risk_engine: RiskEngine,
        execution,
        indicator_cache: IndicatorCache,
        trade_logger: TradeLogger,
        output_manager=None
    ):
        self.risk_engine = risk_engine
        self.execution = execution
        self._indicator_cache = indicator_cache
        self.trade_logger = trade_logger
        self.output_manager = output_manager
        self._exhaustion_detector = get_exhaustion_detector()
    
    async def update(
        self,
        current_price: float,
        market_time: datetime,
        candles_5m: list
    ) -> Optional[str]:
        """
        Update position management. Called every tick when in MANAGE state.
        
        Returns:
            Exit reason if position should close, None otherwise.
        """
        position = self.risk_engine.position
        if not position:
            return None
        
        # 1. Update MFE/MAE tracking
        self._update_extremes(position, current_price)
        
        # 2. Check BOS invalidation
        exit_reason = self._check_bos_invalidation(position, candles_5m)
        if exit_reason:
            return exit_reason
        
        # 3. Check exhaustion exit
        exit_reason = self._check_exhaustion(position, current_price, candles_5m)
        if exit_reason:
            return exit_reason
        
        # 4. Update trailing stop
        new_sl = self._update_trailing(position, current_price, candles_5m)
        if new_sl and new_sl != position.stop_loss:
            await self._update_exchange_sl(new_sl)
        
        # 5. Check partial exit conditions
        partial_action = self._check_partials(position, current_price)
        if partial_action:
            await self._execute_partial(position, partial_action)
        
        # 6. Check time-based exits
        exit_reason = self._check_time_exits(position, market_time)
        if exit_reason:
            return exit_reason
        
        return None
    
    def _update_extremes(self, position, price):
        """Track MFE/MAE"""
        if position.direction == 'LONG':
            position.highest_price = max(position.highest_price, price)
            position.lowest_price = min(position.lowest_price, price)
        else:
            position.highest_price = max(position.highest_price, price)
            position.lowest_price = min(position.lowest_price, price)
    
    # ... resto de métodos extraídos de _handle_manage_state ...
```

---

## 6. SEPARAR SignalEngine EN SUB-CLASES

### Estructura propuesta

```
src/application/live/
├── trading_app.py           # SignalEngine core (~3000 líneas)
├── signal_evaluator.py      # Entry signal evaluation (~2000 líneas)  
├── position_manager.py      # Position management (~2000 líneas)
├── trade_executor.py        # Order execution (~1000 líneas)
└── helpers/
    ├── indicator_cache.py   # Indicator caching
    └── notification_helper.py # Telegram notifications
```

### SignalEngine refactorizado

```python
class SignalEngine:
    """
    Main trading orchestrator.
    
    Delegates specialized logic to:
    - SignalEvaluator: Entry signal detection
    - PositionManager: Open position management
    - TradeExecutor: Order execution
    """
    
    def __init__(self, ...):
        # Core dependencies
        self.market_data = market_data
        self.execution = execution
        
        # Specialized components
        self._indicator_cache = get_indicator_cache()
        self._signal_evaluator = SignalEvaluator(self._indicator_cache, ...)
        self._position_manager = PositionManager(self.risk_engine, ...)
        self._trade_executor = TradeExecutor(self.execution, ...)
    
    async def _handle_idle_state(self, current_price, atr_1m, market_time):
        """Look for entry signals"""
        signal = await self._signal_evaluator.evaluate(
            current_price=current_price,
            candles_1m=self.market_data.get_candles('1m'),
            candles_5m=self.market_data.get_candles('5m'),
            candles_1h=self.market_data.get_candles('1h'),
            market_time=market_time
        )
        
        if signal.should_enter:
            await self._trade_executor.execute_entry(signal)
    
    async def _handle_manage_state(self, current_price, atr_1m, market_time):
        """Manage open position"""
        exit_reason = await self._position_manager.update(
            current_price=current_price,
            market_time=market_time,
            candles_5m=self.market_data.get_candles('5m')
        )
        
        if exit_reason:
            await self._trade_executor.execute_exit(exit_reason)
```

---

## ✅ ORDEN DE IMPLEMENTACIÓN

1. **Punto 3** (Indicator Cache) - 2h trabajo, bajo riesgo
2. **Punto 4** (Close Position Common) - 1h trabajo, bajo riesgo
3. **Punto 5** (Position Manager) - 4h trabajo, medio riesgo
4. **Punto 6** (Full Refactor) - 1-2 días, alto riesgo (requiere testing extensivo)

---

## 🔴 RIESGOS Y MITIGACIÓN

| Riesgo | Probabilidad | Mitigación |
|--------|--------------|------------|
| Introducir bugs en lógica de trading | Media | Tests unitarios antes de cambios |
| Romper cache invalidation | Baja | Usar timestamps de candles cerrados |
| Desincronización de estado | Media | Mantener position_store como SSOT |
| Regresión de performance | Baja | Medir tick latency antes/después |

---

## 📝 NOTAS IMPORTANTES

- **NUNCA** cambiar lógica de trading mientras se refactoriza
- **SIEMPRE** mantener logs idénticos para debugging
- **CADA** cambio debe ser ejecutable y testeable independientemente
- **NO** hacer cambios masivos de golpe - iterar en pequeños pasos
