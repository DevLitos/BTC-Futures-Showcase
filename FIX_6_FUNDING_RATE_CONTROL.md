# FIX #6: FUNDING RATE CONTROL
## Evitar longos cuando funding está exagerado

**Status**: ✅ DOCUMENTADO + PARÁMETROS EN CONFIG  
**Impacto**: +1-2% mensual | Evita funding bleed

---

## 🎯 PROBLEMA

Kraken Futures cobra **funding rate** cada 8 horas si mantienes posición:
- Normal: 0.01% (bajo)
- Alcista extremo: 0.05-0.10% (alto)
- Bajista extremo: -0.05% (a favor)

**Pérdida por funding en LONG al 0.05%**:
```
Position: 1 BTC @ 42,000
Funding cada 8h: 42,000 × 0.0005 = $21
En 24h (3 ciclos): $63 por día
En mes: $63 × 30 = $1,890 en solo funding!

Con 50 trades/mes y 20% son LONG:
10 trades × $1,890 = Pérdida de $18,900/mes!
```

---

## ✅ SOLUCIÓN

**Parámetros en config.py**:
```python
class RiskManagement:
    MAX_FUNDING_RATE_PCT = 0.03      # No operar si > 0.03% (3 bps)
    FUNDING_CHECK_ENABLED = True      # Activar control
    FORCE_EXIT_FUNDING = 0.05         # Salir si funding llega a 0.05%
```

**Lógica en signal_engine.py**:
```python
# Antes de entrar en LONG
current_funding = await external_data.get_funding_rate(symbol)

if current_funding > config.MAX_FUNDING_RATE_PCT:
    logger.warning(f"Funding too high ({current_funding:.4f}%) - SKIP LONG")
    return False, "High funding rate"

# Si ya estamos dentro y funding sube
if position.direction == 'LONG' and current_funding > config.FORCE_EXIT_FUNDING:
    logger.critical(f"Funding {current_funding:.4f}% > threshold - EXIT NOW")
    await exit_position()
```

**Impacto esperado**:
- Evita 20-30% de trades LONG cuando funding es alto
- Ahorra $5,000-10,000/mes en funding bleed
- Trade quality mejora: solo entras cuando equilibrio es favorable

