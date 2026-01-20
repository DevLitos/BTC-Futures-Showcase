# CRITICAL FIX: Eliminación de SL Lógico Local

**Fecha**: 12 Enero 2026  
**Problema**: Sistema cerrando posiciones basándose en verificación LOCAL de precio, no en confirmación del exchange

## El Bug Raíz

```
2025-01-12 19:38:XX | POSITION CLOSED - STOP_LOSS @ 91396.00 | Duration: 0.0min
2025-01-12 19:38:XX | EMERGENCY MARKET CLOSE (attempt 1/3)
2025-01-12 19:38:XX | 'stopLossPrice': None  ← El exchange NUNCA tuvo el SL!
```

**Diagnóstico**: El bot operaba en modo "paper trading" sin saberlo:
1. Colocaba orden de SL en exchange
2. SL fallaba silenciosamente (`stopLossPrice: None`)
3. El bot seguía verificando precio localmente (`if price <= stop_loss`)
4. Cuando el precio tocaba el SL, cerraba con market order
5. Esto causaba doble-close y confusión de estado

## Los 3 Fixes Implementados

### Fix 1: Eliminar SL Lógico Local

**Archivos modificados**:
- `engine/risk_engine.py` - Clase `Position`

**Cambios**:
```python
# Nuevos campos en Position
self.exchange_sl_active = False        # ¿Exchange tiene el SL?
self.exchange_sl_order_id = None       # ID de la orden SL

# Nuevos métodos
def mark_exchange_sl_active(order_id):
    # Llamar cuando SL se confirma en exchange
    
def mark_exchange_sl_inactive(reason):
    # Llamar cuando SL falla o se cancela

# Modificación CRÍTICA en check_stop_hit()
def check_stop_hit(...) -> bool:
    if self.exchange_sl_active:
        # SOLO loggear, NUNCA cerrar localmente
        return False  # Exchange cerrará cuando SL se ejecute
    
    # Solo cerrar localmente si NO hay SL en exchange
    return price_hit_sl
```

**En signal_engine.py**:
- Después de `set_position_stops()` exitoso → `mark_exchange_sl_active()`
- Después de fallo de SL → `mark_exchange_sl_inactive()`

---

### Fix 2: Hard-Lock en Salida (EXIT_PENDING)

**Problema**: Durante el cierre, otros subsistemas podían interferir.

**Archivos modificados**:
- `engine/exchange_reconciler.py`
- `engine/signal_engine.py`

**Nuevo evento de hard-lock**:
```python
class HardLockEvent(Enum):
    EXIT_PENDING = "EXIT_PENDING"  # Nuevo

def notify_exit_started(reason):
    # Entra en HARD_LOCK hasta confirmar position_size=0
    self.enter_hard_lock(reason, HardLockEvent.EXIT_PENDING, 5000)
```

**Integrado en todos los puntos de cierre**:
```python
# Antes de cada close_position_market()
self.reconciler.notify_exit_started("STOP_LOSS")
await self.execution.close_position_market(...)
```

**Puntos protegidos**:
- FUNDING_EXIT
- TIME_EXIT  
- PARTIAL_CLOSE
- FULL_CLOSE
- EMERGENCY_CLOSE

---

### Fix 3: Degradación cuando Exchange Inestable

**Problema**: Bot tomaba decisiones con datos stale cuando REST/WS fallaban.

**Archivos modificados**:
- `engine/exchange_reconciler.py`
- `engine/signal_engine.py`

**Nuevos campos en ExchangeReconciler**:
```python
self._consecutive_failures = 0
self._last_successful_poll = None
self._degraded_mode = False
self.MAX_CONSECUTIVE_FAILURES = 3    # Entrar en degradado tras 3 fallos
self.STALE_DATA_THRESHOLD_SEC = 10   # Datos >10s = stale
```

**Nuevo método**:
```python
def is_exchange_available() -> bool:
    if self._degraded_mode:
        return False
    if data_is_stale:
        return False
    return True
```

**Integrado en signal_engine**:
```python
# Antes de manage_position()
if not self.reconciler.is_exchange_available():
    logger.warning("DEGRADED MODE: Skipping logical exit checks")
    return  # No tomar decisiones con datos stale
```

---

## Flujo Correcto Ahora

```
1. Entrada ejecutada
2. Colocar SL en exchange
3. SI SL confirmado:
   - mark_exchange_sl_active()
   - check_stop_hit() → siempre False
   - Esperar evento del exchange
4. SI SL falló:
   - mark_exchange_sl_inactive()
   - check_stop_hit() → True cuando precio toca SL
   - Emergency market close

Durante salida:
1. notify_exit_started() → HARD_LOCK
2. Ejecutar cierre
3. Esperar position_size=0 del exchange
4. Liberar HARD_LOCK
```

## Logs Esperados

**SL activo en exchange**:
```
✅ EXCHANGE SL ACTIVE: Trust exchange to execute SL (order: abc123...)
📊 SL price touched @ 94500.00 - WAITING for exchange to execute (SL active)
```

**SL no activo (fallback)**:
```
⚠️ EXCHANGE SL INACTIVE: Not found on exchange after placement - will use emergency close
🚨 SL HIT @ 94500.00 - NO exchange SL active, will use emergency close
```

**Degradación**:
```
🚨 DEGRADED MODE: 3 consecutive exchange failures - blocking all trading decisions
⚠️ DEGRADED MODE: Skipping logical exit checks - exchange unavailable
✅ Exchange connection restored - exiting degraded mode
```

## Verificación

```bash
# Test básico
uv run python -c "
from engine.risk_engine import Position
from datetime import datetime, UTC

pos = Position('BTC/USD:USD', 'LONG', 100.0, 0.001, 95.0, 1.0)
pos.mark_exchange_sl_active('test_123')

# Precio toca SL - NO debe cerrar localmente
result = pos.check_stop_hit(94.0, market_time=datetime.now(UTC))
assert result == False
print('✅ FIX #1 funcionando: exchange SL activo previene cierre local')
"
```
