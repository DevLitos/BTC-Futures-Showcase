# ADR-017: Deduplicación de Código - Feb 04, 2026

## Estado
**IMPLEMENTADO** ✅

## Contexto
El archivo `AUDITORIA_DUPLICACION_FEB04.md` identificó 6 categorías de código duplicado/redundante:
1. 5 sistemas paralelos de checks de dirección
2. 6+ tipos de cooldown diferentes
3. 4 ubicaciones de checks de session/hora
4. 7+ redundancias de cálculo ADX
5. 4 sistemas paralelos de scores
6. 3 implementaciones de location/extremos

## Decisión
Unificar la lógica duplicada en clases centralizadas con métodos helper.

## Cambios Implementados

### 1. ADXThresholds (trading_config.py)
```python
class ADXThresholds:
    RANGE = 18           # ADX < 18 = RANGE/CHOP
    TRANSITION = 20      # ADX 18-20 = zona de transición
    TREND = 20           # ADX > 20 = TREND
    STRONG_TREND = 25    # ADX > 25 = STRONG TREND
    
    @classmethod
    def is_ranging(cls, adx: float) -> bool
    @classmethod
    def is_trending(cls, adx: float) -> bool
    @classmethod  
    def is_strong_trend(cls, adx: float) -> bool
    @classmethod
    def get_regime(cls, adx: float) -> str
    @classmethod
    def get_size_mult(cls, adx: float) -> float
```

### 2. ScoreAdjustments (trading_config.py) - NUEVO
```python
class ScoreAdjustments:
    # Regime-based
    TREND_DAY_ADJUST = 0.0
    RANGE_DAY_ADJUST = +1.0
    TRANSITION_DAY_ADJUST = +0.5
    
    # Mode-based
    SOFT_MODE_PENALTY = -1.5
    DISCOVERY_MODE_PENALTY = -0.5
    
    # Micro-trend
    MICRO_TREND_ALIGNED_BONUS = +0.5
    MICRO_TREND_OPPOSE_PENALTY = -1.0
    
    # Momentum penalties
    BULLISH_CONTEXT_MILD = -0.15
    BULLISH_CONTEXT_MODERATE = -0.25
    BULLISH_CONTEXT_SEVERE = -0.5
    
    @classmethod
    def calculate_total_adjustment(...) -> float
```

### 3. SessionQuality Extensions (trading_config.py)
```python
class SessionQuality:
    QUALITY_SIZE_MULT = {'good': 1.0, 'moderate': 0.85, 'poor': 0.0}
    
    @classmethod
    def get_quality_size_mult(cls, quality: str) -> float
    @classmethod
    def is_quality_blocked(cls, quality: str) -> bool
```

### 4. DirectionStateManager Extensions (direction_state_manager.py)
```python
def validate_direction(self, proposed_direction: str) -> Tuple[bool, str, str]
def is_direction_blocked(self, proposed_direction: str) -> bool
def get_active_direction(self) -> Optional[str]
def sync_from_app(self, app: 'SignalEngine')  # NEW
def sync_to_app(self, app: 'SignalEngine')    # NEW
```

### 5. CooldownManager (cooldown_manager.py) - NUEVO ARCHIVO
```python
class CooldownType(Enum):
    KILL_SWITCH, LOSS_STREAK, DEAD_HOURS, REJECTION,
    SHORT_OBSTINACY, EDGE_CONTEXT, COUNTERTREND

class CooldownManager:
    def check_can_trade(self) -> CooldownCheckResult
    def activate_loss_streak_cooldown(...)
    def activate_rejection_cooldown(...)
    def activate_short_obstinacy_cooldown(...)
    def sync_from_app(self, app)
    def sync_to_app(self) -> Dict
```

### 6. IndicatorCache Extensions (indicator_cache.py)
```python
def get_adx_5m(self) -> Optional[float]  # NEW
def get_adx_values(self, timeframe: str) -> Optional[np.ndarray]  # NEW
def get_all_adx(self) -> Dict[str, Optional[float]]  # NEW
get_indicator_cache() -> IndicatorCache  # Singleton
```

## Archivos Modificados (20+)

| Archivo | Cambios |
|---------|---------|
| `trading_config.py` | +ADXThresholds, +ScoreAdjustments, +SessionQuality extensions |
| `cooldown_manager.py` | **NUEVO** - 495 líneas |
| `direction_state_manager.py` | +sync_from_app, +sync_to_app, +validate_direction |
| `indicator_cache.py` | +ADX 5m caching, +singleton, fixed imports |
| `entry_orchestrator.py` | Guards refactored to use centralized classes |
| `entry_execution_logic.py` | ADX check uses ADXThresholds |
| `trading_app.py` | CooldownManager integration, deprecated direction vars |
| `simplified_sizing.py` | Uses SessionQuality, ADXThresholds, deprecated LossStreakCooldown |
| `market_analysis.py` | Uses SessionQuality.is_quality_blocked |
| `entry_validators.py` | Uses SessionQuality.is_quality_blocked |
| `entry_quality.py` | Uses SessionQuality.is_quality_blocked |
| `filter_checks.py` | Uses ADXThresholds.TREND |
| `risk_engine.py` | Uses ADXThresholds.TREND |
| `adaptive_entry.py` | Uses ADXThresholds.TREND |
| `entry_scorer.py` | Uses ADXThresholds methods |
| `trend_intelligence.py` | Uses ADXThresholds methods |
| `edge_state_manager.py` | Uses SessionQuality methods |

## Beneficios
1. **Consistencia**: Un único lugar define thresholds de ADX, session quality, cooldowns, score adjustments
2. **Mantenibilidad**: Cambiar un threshold afecta todo el sistema automáticamente
3. **Reducción de bugs**: No más "18 vs 20" inconsistencies
4. **Testing**: Las clases centralizadas son más fáciles de testear
5. **Documentación**: El código es self-documenting con los classmethods

## Backward Compatibility
- Variables legacy marcadas como DEPRECATED con comentarios
- `sync_from_app()`/`sync_to_app()` bridges para migración gradual
- `LossStreakCooldown` mantiene API pero delega a CooldownManager

## Tests
```bash
# All modified files compile successfully
python -m py_compile src/config/trading_config.py  # ✅
python -m py_compile src/application/live/cooldown_manager.py  # ✅
python -m py_compile src/application/live/indicator_cache.py  # ✅
# ... all 20+ files OK

# Runtime tests pass
>>> ScoreAdjustments.SOFT_MODE_PENALTY
-1.5
>>> ScoreAdjustments.calculate_total_adjustment(regime='RANGE_DAY', is_soft_mode=True)
-0.5
>>> cm = get_cooldown_manager()
>>> cm.activate_rejection_cooldown(now, 'test', 60)
>>> cm.check_can_trade(now).can_trade
False
```

## Próximos Pasos (Opcional)
1. Añadir tests unitarios para nuevas clases
2. Migrar completamente código legacy después de validar en producción
3. Refactorizar entry_orchestrator guards para usar validate_direction directamente
4. Unificar cálculos de ADX para usar IndicatorCache en todos los lugares
