# BREAKOUT EDGE & DUAL EDGE MODES - January 28, 2026

## Resumen Ejecutivo

Este documento detalla las mejoras implementadas al sistema de trading BTC para:
1. Integrar correctamente el edge de BREAKOUT siguiendo la filosofía institucional
2. **NUEVO**: Implementar DOS MODOS DE EDGE mutuamente exclusivos

---

## 🔥 DUAL EDGE MODES - ARQUITECTURA

### Filosofía

```
El sistema ahora opera en DOS MODOS MUTUAMENTE EXCLUSIVOS:

┌─────────────────────────────────────────────────────────────────────┐
│                    EDGE_INSTITUTIONAL                                │
│   Low Frequency | High Conviction | 1-5 trades/semana              │
│   Size: 1.0x - 1.5x | RR: 2.0R+                                    │
│                                                                     │
│   Tipos:                                                           │
│   - BREAKOUT: Compresión + ruptura confirmada                      │
│   - RANGE: Extremos 90/10 con sweep+rejection                      │
│   - TREND: BOS + pullback clásico                                  │
└─────────────────────────────────────────────────────────────────────┘

              ↓ SI NO INSTITUCIONAL ACTIVO ↓

┌─────────────────────────────────────────────────────────────────────┐
│                      EDGE_INTRADAY                                   │
│   High Frequency | Low Conviction | 5-20 trades/día                │
│   Size: 0.15x - 0.30x | RR: 0.3R - 0.8R                           │
│                                                                     │
│   Criterios:                                                       │
│   - Mercado ACTIVO (ATR expandiendo)                               │
│   - Volumen > media                                                │
│   - Dirección clara (HH/HL o LL/LH secuencia)                     │
│   - Session activa (no poor)                                       │
└─────────────────────────────────────────────────────────────────────┘

NO COMPITEN. NO SE SOLAPAN. NO SE CONTRADICEN.
```

### Configuración de Edge Modes

**INSTITUTIONAL** (`InstitutionalEdgeConfig`):
```python
SIZE_MULTIPLIER_BASE = 1.0        # Size estándar
SIZE_MULTIPLIER_HIGH_CONVICTION = 1.5  # +50% en setups perfectos
MIN_RR = 2.0                      # Mínimo 2R target
```

**INTRADAY** (`IntradayEdgeCriteria`):
```python
SIZE_MIN = 0.15                   # 15% del size normal
SIZE_MAX = 0.30                   # 30% máximo
TP_R_TARGET = 0.5                 # 0.5R target
SL_R_MAX = 0.4                    # SL rápido
MAX_TRADES_PER_HOUR = 5           # Límite frecuencia
```

### Integración en Sizing

En `simplified_sizing.py`, el nuevo multiplicador `edge_mult`:
- INSTITUTIONAL activo: `1.0x - 1.5x`
- INTRADAY activo: `0.15x - 0.30x`
- Ninguno activo: `1.0x` (default)

---

## 1. Error de Red Telegram

### Problema
El bot de Telegram generaba stacktraces de `httpx.ReadError` y `telegram.error.NetworkError` durante el polling de mensajes:
```
2026-01-27 14:29:03,568 - telegram.ext.Updater - E - Exception happened while polling for updates.
httpcore.ReadError
httpx.ReadError
telegram.error.NetworkError: httpx.ReadError
```

### Solución
Agregado un manejador de errores personalizado en `src/interfaces/output/output_manager.py`:

```python
async def _telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle Telegram API errors gracefully, especially network issues.
    """
    error = context.error
    
    # Network errors during polling - log at DEBUG level (transient, expected)
    if isinstance(error, NetworkError):
        if 'read' in str(error).lower() or 'httpx' in str(error).lower():
            logger.debug(f"Telegram polling network hiccup (will retry): {type(error).__name__}")
            return  # Silently retry
        logger.warning(f"Telegram network error (will retry): {error}")
        return
    
    # Timeouts - also expected during long polling
    if isinstance(error, TimedOut):
        logger.debug("Telegram polling timeout (normal during long polls)")
        return
    
    # Rate limiting
    if isinstance(error, RetryAfter):
        logger.warning(f"Telegram rate limited, retry after {error.retry_after}s")
        return
    
    # Other errors - log at error level
    logger.error(f"Telegram error: {error}", exc_info=context.error)
```

**Archivo modificado**: `src/interfaces/output/output_manager.py`

---

## 2. Arquitectura BREAKOUT Corregida

### Problema Original
El sistema de ANTICIPATION detectaba `COMPRESSION_BREAKOUT` pero nunca se conectaba con el EdgeStateManager. El edge BREAKOUT existía en código pero nunca se activaba.

### Filosofía Institucional Aplicada

```
ANTICIPATION → ARMS un BREAKOUT EDGE (no activa)
     ↓
CONFIRMACIÓN DE PRECIO SEPARADA
     ↓
EDGE_ACTIVE (solo entonces puede operar)
```

### Implementación

#### 2.1 ANTICIPATION Solo ARMA el Edge

En `src/application/live/trading_app.py`, cuando ANTICIPATION detecta compresión:

```python
# Check for COMPRESSION_BREAKOUT anticipation signal
anticipation_signal = self._current_trade_tags.get('anticipation_signal')
if anticipation_signal == 'COMPRESSION_BREAKOUT':
    anticipation_direction = self._current_trade_tags.get('anticipation_direction', 'LONG')
    anticipation_bb_pct = self._current_trade_tags.get('anticipation_bb_percentile', 50.0)
    anticipation_vol_ratio = self._current_trade_tags.get('anticipation_volume_ratio', 1.0)
    
    # SOLO ARMA, NO ACTIVA
    breakout_armed = self._edge_manager.check_for_breakout_edge(
        current_price=current_price,
        bb_width_percentile=anticipation_bb_pct,
        volume_ratio=anticipation_vol_ratio,
        direction=anticipation_direction,
        atr=atr_1h,
        market_time=market_time
    )
```

#### 2.2 Confirmación Institucional Separada

El método `confirm_breakout()` en `edge_state_manager.py` ahora requiere criterios institucionales:

```python
def confirm_breakout(
    self,
    current_price: float,
    breakout_direction: str,
    atr: float,
    market_time: datetime,
    bb_upper: float = 0.0,
    bb_lower: float = 0.0,
    close_price: float = 0.0,
    expansion_ratio: float = 0.0
) -> bool:
    """
    Confirma ruptura del breakout con criterios INSTITUCIONALES.
    
    CRITERIOS:
    1. Close FUERA de Bollinger Bands (no solo mecha)
    2. Dirección coincide con anticipation
    3. Expansión de BB comenzando (ratio > 1.0)
    """
```

**Criterio 1: Close fuera de BB**
```python
close_outside_bb = False
if bb_upper > 0 and bb_lower > 0 and close_price > 0:
    if breakout_direction == 'LONG' and close_price > bb_upper:
        close_outside_bb = True
    elif breakout_direction == 'SHORT' and close_price < bb_lower:
        close_outside_bb = True
```

#### 2.3 Mutual Exclusion: BREAKOUT vs RANGE

En `check_for_range_edge()`:

```python
# MUTUAL EXCLUSION: Si BREAKOUT armado → RANGE bloqueado
if self._state == EdgeState.EDGE_ARMED and self._current_bos:
    if self._current_bos.edge_type == EdgeType.BREAKOUT:
        logger.debug(
            "[RANGE_BLOCKED] BREAKOUT edge armado - RANGE deshabilitado por mutual exclusion"
        )
        return False
```

**Regla**: BREAKOUT invalida RANGE, no compiten.

---

## 3. Invalidación Lógica Post-Breakout

### Problema
El sistema solo tenía SL monetario. Faltaba invalidación LÓGICA que protege edge expectancy, no solo capital.

### Diferencia Crítica
- **SL** → Protege capital
- **Invalidación lógica** → Protege edge expectancy

### Implementación

Nuevo método `check_breakout_invalidation()` en `edge_state_manager.py`:

```python
def check_breakout_invalidation(
    self,
    current_price: float,
    close_price: float,
    bb_upper: float,
    bb_lower: float,
    atr: float
) -> tuple[bool, str]:
    """
    Verifica si el breakout se ha invalidado LÓGICAMENTE.
    
    Invalidación ocurre si:
    1. Close de vuelta DENTRO de BB (fake breakout)
    2. Pérdida del nivel de breakout + buffer ATR
    """
```

### Criterios de Invalidación

**Invalidación 1: Close dentro de BB**
```python
if direction == 'LONG' and close_price < bb_upper:
    return True, f"INVALIDATION: Close {close_price:.2f} < BB_upper {bb_upper:.2f} (vuelta dentro)"
elif direction == 'SHORT' and close_price > bb_lower:
    return True, f"INVALIDATION: Close {close_price:.2f} > BB_lower {bb_lower:.2f} (vuelta dentro)"
```

**Invalidación 2: Pérdida del nivel de breakout**
```python
breakout_level = bos.breakout_confirmation_price or bos.price
buffer = BreakoutEdgeCriteria.INVALIDATION_LOSS_OF_BREAKOUT_LEVEL_ATR * atr  # 0.3 ATR

if direction == 'LONG' and current_price < (breakout_level - buffer):
    return True, "INVALIDATION: Pérdida nivel breakout"
```

### Configuración

En `BreakoutEdgeCriteria`:
```python
INVALIDATION_CLOSE_INSIDE_BB = True
INVALIDATION_LOSS_OF_BREAKOUT_LEVEL_ATR = 0.3  # 0.3 ATR buffer
```

---

## 4. R Mínimo BREAKOUT (2.0R)

### Problema
BREAKOUT tiene winrate bajo. Necesita R más alto para pagar los fallos.

### Filosofía
| Edge Type | Winrate | Target R |
|-----------|---------|----------|
| RANGE     | Alto    | 1.0R     |
| TREND     | Medio   | 1.5R     |
| BREAKOUT  | Bajo    | **2.0R** |

### Implementación

En `src/config/simplified_sizing.py`:

```python
class SimplifiedExitConfig:
    TP_R_TREND = 1.5
    TP_R_RANGE = 1.0
    TP_R_BREAKOUT = 2.0        # BREAKOUT MUST target 2.0R minimum
    TP_R_BREAKOUT_IDEAL = 3.0  # Ideal target
    
    @classmethod
    def get_tp_r(cls, regime_str: str, adx: Optional[float] = None, edge_type: Optional[str] = None) -> float:
        # BREAKOUT edge overrides regime - ALWAYS 2.0R minimum
        if edge_type and edge_type.upper() == 'BREAKOUT':
            logger.info(f"📊 BREAKOUT edge detected - using minimum TP {cls.TP_R_BREAKOUT}R")
            return cls.TP_R_BREAKOUT
        
        # Standard logic for TREND/RANGE
        ...
```

### Integración

En `entry_orchestrator.py`:
```python
current_edge_type = engine._current_trade_tags.get('edge_type', None)

tp_mult = SimplifiedExitConfig.get_tp_r(
    regime_str=engine._current_regime.value if engine._current_regime else "NORMAL",
    adx=ctx.adx_1h if hasattr(ctx, 'adx_1h') else None,
    edge_type=current_edge_type  # NEW: Pass edge type
)
```

---

## 5. Cooldown por Contexto

### Problema
Pérdidas correlacionadas disfrazadas de trades distintos:
```
BREAKOUT falla (-1R)
     ↓
Cooldown corto
     ↓
TREND edge entra en MISMA zona
     ↓
Mismo contexto → mismo resultado → otro -1R
```

### Filosofía Institucional
- Si BREAKOUT falla → bloquear TREND (mismo contexto de ruptura)
- Si TREND falla → bloquear BREAKOUT (mismo contexto)
- RANGE nunca se bloquea (contexto independiente)

### Implementación

**Configuración**:
```python
CONTEXT_COOLDOWN_MINUTES = 15  # 15 min de cooldown
```

**Estructura de datos** en EdgeStateManager:
```python
self._context_cooldown: Dict[str, datetime] = {}  # edge_type -> cooldown_until
```

**Establecer cooldown al expirar** en `_expire_edge()`:
```python
if set_context_cooldown and self._current_bos.trades_lost > 0:
    edge_type = self._current_bos.edge_type
    cooldown_until = datetime.utcnow() + timedelta(minutes=self.CONTEXT_COOLDOWN_MINUTES)
    
    if edge_type == EdgeType.BREAKOUT:
        # BREAKOUT falla → bloquear TREND
        self._context_cooldown['TREND'] = cooldown_until
        logger.warning(f"⏰ COOLDOWN: TREND bloqueado {self.CONTEXT_COOLDOWN_MINUTES}min por fallo de BREAKOUT")
    
    elif edge_type == EdgeType.TREND:
        # TREND falla → bloquear BREAKOUT
        self._context_cooldown['BREAKOUT'] = cooldown_until
    
    # RANGE no establece cooldown para otros (contexto distinto)
```

**Verificar cooldown** en cada check de edge:
```python
def is_context_in_cooldown(self, edge_type: EdgeType, market_time: datetime) -> bool:
    cooldown_until = self._context_cooldown.get(edge_type.value)
    if cooldown_until and market_time < cooldown_until:
        remaining = (cooldown_until - market_time).total_seconds() / 60
        logger.debug(f"⏰ {edge_type.value} en cooldown por contexto | Quedan {remaining:.1f} min")
        return True
    return False
```

**Aplicado en**:
- `check_for_bos()` (TREND)
- `check_for_breakout_edge()` (BREAKOUT)

---

## 6. Datos Guardados para Auditoría

En `BOSEvent` se agregaron campos para tracking:

```python
@dataclass
class BOSEvent:
    # ... campos existentes ...
    
    # NEW: Datos para invalidación y tracking
    breakout_confirmation_price: Optional[float] = None  # Precio al confirmar
    breakout_bb_upper: Optional[float] = None            # BB upper al confirmar
    breakout_bb_lower: Optional[float] = None            # BB lower al confirmar
    context_cooldown_until: Optional[datetime] = None    # Cooldown por contexto
```

---

## Archivos Modificados

| Archivo | Cambios |
|---------|---------|
| `src/interfaces/output/output_manager.py` | Error handler de Telegram |
| `src/application/live/trading_app.py` | Integración BREAKOUT con ANTICIPATION |
| `src/domain/trading/edge_state_manager.py` | Confirmación institucional, invalidación, cooldown |
| `src/config/simplified_sizing.py` | TP mínimo 2.0R para BREAKOUT |
| `src/application/live/entry_orchestrator.py` | Pasar edge_type a get_tp_r() |
| `src/application/live/idle_state_handlers.py` | Extracción BB% y vol ratio de anticipation |

---

## Diagrama de Flujo BREAKOUT

```
┌─────────────────┐
│  ANTICIPATION   │
│ detecta squeeze │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ check_for_      │  ← BB% ≤ 10, Vol ≤ 0.5
│ breakout_edge() │
└────────┬────────┘
         │ EDGE_ARMED
         ▼
┌─────────────────┐
│ confirm_        │  ← Close fuera BB + Expansion
│ breakout()      │
└────────┬────────┘
         │ EDGE_ACTIVE
         ▼
┌─────────────────┐
│  PUEDE OPERAR   │  → TP = 2.0R mínimo
│  (RiskEngine)   │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────┐
│        POST-ENTRY               │
│  ┌─────────────────────────┐   │
│  │ check_breakout_         │   │
│  │ invalidation()          │   │
│  │  ├─ Close dentro BB?    │   │
│  │  └─ Perdió nivel?       │   │
│  └─────────────────────────┘   │
└─────────────────────────────────┘
         │
    Si falla
         ▼
┌─────────────────┐
│ _expire_edge()  │ → TREND cooldown 15min
└─────────────────┘
```

---

## Configuración Actual

```python
# BREAKOUT
BreakoutEdgeCriteria:
    BB_WIDTH_PERCENTILE_MAX = 10      # BB squeeze threshold
    VOLUME_DECLINE_RATIO = 0.50       # Volume decline threshold
    BREAKOUT_TIMEOUT_MIN = 15         # Timeout si no confirma
    MIN_TARGET_R = 2.0                # TP mínimo
    IDEAL_TARGET_R = 3.0              # TP ideal
    INVALIDATION_CLOSE_INSIDE_BB = True
    INVALIDATION_LOSS_OF_BREAKOUT_LEVEL_ATR = 0.3

# COOLDOWN
EdgeStateManager:
    CONTEXT_COOLDOWN_MINUTES = 15     # Cooldown entre edges relacionados
```

---

## Notas de Ejecución

**Para ejecutar**:
```bash
cd /home/trading/traderAgents/FideiRatioCapital/systems/btc
TRADING_MODE=LIVE uv run python main.py
```

**Logs a monitorear**:
- `🔥 BREAKOUT EDGE ARMED` - Se detectó compresión
- `🚀 BREAKOUT CONFIRMED` - Se confirmó ruptura
- `⏰ COOLDOWN:` - Se estableció cooldown por contexto
- `INVALIDATION:` - Se invalidó breakout lógicamente

---

## Riesgos Conocidos

1. **Subactividad estructural**: Con filtros tan estrictos, esperar 2-3 trades/semana
2. **Breakouts estadísticamente pobres**: Necesita medición real de winrate/R alcanzado
3. **Dependencia de datos BB**: Si BB no se calcula bien, confirmación puede fallar

---

*Documento generado: January 28, 2026*
*Autor: Sistema de Trading BTC v2.1.0*
