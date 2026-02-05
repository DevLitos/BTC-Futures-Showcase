# 📊 ANÁLISIS COMPLETO DE ARQUITECTURA
## Sistema de Trading BTC - FideiRatioCapital
### Fecha: 5 de Febrero, 2026

---

## 🎯 RESUMEN EJECUTIVO

| Categoría | Componentes Totales | Activos | Duplicados/Muertos |
|-----------|---------------------|---------|-------------------|
| **EDGES** | 15 clases/enums | 12 | 3 |
| **TRENDS** | 18 clases/enums | 10 | 8 |
| **FILTERS** | 22 clases | 18 | 4 |
| **REGIMES** | 25 clases/enums | 12 | **13 DUPLICADOS** |

**⚠️ PROBLEMA CRÍTICO: 13 definiciones de régimen paralelas**

---

## 1. 🎯 EDGES (Sistema de Ventajas)

### 1.1 Tipos de Edge (`EdgeType`)
**Archivo:** `src/domain/trading/edge_state_manager.py:102`

| Tipo | Descripción | ¿Activo? |
|------|-------------|----------|
| `TREND` | BOS + Pullback en mercado tendencial (ADX > 18) | ✅ |
| `RANGE` | Extremo 85/15 + Sweep + Rejection | ✅ |
| `BREAKOUT` | Compresión BB + Ruptura (de ANTICIPATION) | ✅ |
| `SCALP` | Micro-ineficiencias alta frecuencia | ✅ |
| `DISPLACEMENT` | Movimiento fuerte ≥1.5x ATR_5m | ✅ |

### 1.2 Estados del Edge (`EdgeState`)
**Archivo:** `src/domain/trading/edge_state_manager.py:111`

| Estado | Símbolo | Descripción |
|--------|---------|-------------|
| `NO_EDGE` | ○ | Sin oportunidad |
| `EDGE_ARMED` | ◐ | Setup detectado, esperando confirmación |
| `EDGE_PROVISIONAL` | ◑ | Señal válida pero incompleta (starter) |
| `EDGE_ACTIVE` | ● | Ventana de entrada abierta |
| `EDGE_EXPIRED` | ◌ | Oportunidad consumida |

### 1.3 Modos de Operación (`EdgeMode`)
**Archivo:** `src/domain/trading/edge_state_manager.py:86`

| Modo | Descripción | Frecuencia |
|------|-------------|------------|
| `INSTITUTIONAL` | Low freq, high conviction | 1-5/semana |
| `PROBING` | High freq, low conviction | 5-20/día |

### 1.4 Clases de Criterios de Edge

| Clase | Archivo | Línea | Propósito |
|-------|---------|-------|-----------|
| `RangeEdgeCriteria` | edge_state_manager.py | 285 | Extremos 85/15 |
| `BreakoutEdgeCriteria` | edge_state_manager.py | 322 | BB squeeze |
| `DisplacementEdgeCriteria` | edge_state_manager.py | 360 | Fast zones 0-3 |
| `IntradayEdgeCriteria` | edge_state_manager.py | 493 | R-buckets, ADX |
| `ProvisionalEdgeConfig` | edge_state_manager.py | 199 | Starter position |
| `InstitutionalEdgeConfig` | edge_state_manager.py | 628 | Full position |

### 1.5 Clases de Gestión de Edge

| Clase | Archivo | Líneas | ¿Activo? | Descripción |
|-------|---------|--------|----------|-------------|
| `EdgeStateManager` | edge_state_manager.py | 1263 | ✅ | Gestor central |
| `BOSEvent` | edge_state_manager.py | 673 | ✅ | Break of Structure |
| `EdgeTransition` | edge_state_manager.py | 1166 | ✅ | Reglas de transición |
| `EdgeStats` | edge_state_manager.py | 723 | ✅ | Estadísticas |
| `LiquidityEvent` | edge_state_manager.py | 469 | ✅ | Sweep/rejection |
| `EdgeEnforcement` | edge/edge_enforcement.py | 63 | ✅ | Enforcement rules |
| `EdgeValidator` | edge/edge_validator.py | 133 | ✅ | Validación |
| `EdgeRecoveryProtocol` | edge/edge_recovery_protocol.py | 77 | ⚠️ Parcial | Recovery |
| `EdgeHealthTracker` | advanced_analytics.py | 1067 | ✅ | Monitoring |
| `EdgeScore` | activity_regime.py | 57 | ✅ | Scoring |

---

## 2. 📈 TRENDS (Detección de Tendencia)

### 2.1 Clases de TrendIntelligence
**Archivo:** `src/domain/market/regime/trend_intelligence.py`

| Clase | Línea | ¿Activo? | Descripción |
|-------|-------|----------|-------------|
| `TrendIntelligence` | 103 | ✅ | Motor principal de ciclo |
| `TrendPhase` (enum) | 34 | ✅ | 8 fases del ciclo |
| `TrendDirection` (enum) | 46 | ✅ | BULLISH/BEARISH/NEUTRAL |
| `TrendState` | 67 | ✅ | Estado completo |
| `TrendTransition` | 54 | ✅ | Registro de transiciones |

### 2.2 Fases del Ciclo de Tendencia (`TrendPhase`)

| Fase | Descripción | Trading |
|------|-------------|---------|
| `ACCUMULATION` | Pre-trend, building pressure | WAIT |
| `TREND_BIRTH` | Breakout inicial | ENTER |
| `TREND_ACCELERATION` | Momentum creciente | ADD |
| `TREND_MATURE` | Tendencia establecida | HOLD |
| `TREND_EXHAUSTION` | Señales de agotamiento | REDUCE |
| `DISTRIBUTION` | Pre-reversal | EXIT |
| `TREND_DEATH` | Failure/reversal | FLIP? |
| `RANGE_BOUND` | Sin tendencia clara | RANGE TRADE |

### 2.3 ⚠️ DUPLICACIÓN: `TrendDirection` definido DOS VECES

| Ubicación | Valores |
|-----------|---------|
| `trend_intelligence.py:46` | BULLISH, BEARISH, NEUTRAL |
| `trading_regime.py:43` | BULLISH, BEARISH, NEUTRAL |

**→ MISMO ENUM EN DOS ARCHIVOS**

### 2.4 Detectores de Dirección

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `DirectionOrchestrator` | direction_orchestrator.py | ✅ | **FUENTE ÚNICA** |
| `SignalPriority` | direction_orchestrator.py | ✅ | Jerarquía de prioridad |
| `DirectionSignal` | direction_orchestrator.py | ✅ | Señal individual |
| `DirectionSource` | unified_intelligence.py | ⚠️ MUERTO | Duplicado de SignalPriority |
| `DirectionDecision` | unified_intelligence.py | ⚠️ MUERTO | Nunca se usa |

### 2.5 Jerarquía de Prioridad de Señales (`SignalPriority`)

| Señal | Prioridad | Descripción |
|-------|-----------|-------------|
| `EXPANSION_MODE` | 100 | Displacement con lock - BYPASSES VETO |
| `EDGE_BREAKOUT` | 95 | Edge armado - autoridad soberana |
| `DISPLACEMENT` | 90 | Movimiento grande |
| `ANTICIPATION` | 85 | Pre-breakout detection |
| `FAST_REVERSAL` | 80 | Single-candle reversal |
| `MICRO_MOMENTUM` | 70 | HH/HL scoring |
| `STRUCTURAL_BIAS` | 60 | HTF EMA bias |
| `FORBIDDEN_FALLBACK` | 50 | Cuando todo está bloqueado |

### 2.6 Otros Detectores de Trend

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `FastReversalDetector` | fast_reversal_detector.py | ✅ | Reversals rápidos |
| `MicroTrendFilter` | micro_trend_filter.py | ✅ | HH/HL micro |
| `MicroTrendState` | micro_trend_filter.py | ✅ | BULLISH/BEARISH/NEUTRAL |
| `MomentumExhaustionDetector` | momentum_exhaustion.py | ⚠️ Parcial | Exhaustion detection |
| `MomentumValidator` | entry_validators.py | ✅ | Entry validation |

---

## 3. 🛡️ FILTERS (Filtros)

### 3.1 Filtros Core (Bloque de Seguridad)

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `KillConditionsFilter` | filters.py:68 | ✅ | Drawdown, pérdidas, timeout |
| `BlackSwanFilter` | filters.py:158 | ✅ | Movimientos extremos |
| `HTFFilter` | filters.py:322 | ✅ | Bias de timeframe alto |
| `LTFTriggerFilter` | filters.py:808 | ✅ | Trigger de entrada |
| `MTFStructureFilter` | filters.py:554 | ⚠️ COMENTADO | Estructura MTF |

### 3.2 Filtros Especializados

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `MacroTrendFilter` | macro_trend_filter.py | ✅ | Veto macro por rango 60d |
| `MicroTrendFilter` | micro_trend_filter.py | ✅ | Micro-estructura |
| `SpreadFilter` | spread_filter.py | ✅ | Bid-ask monitoring |
| `CostFilter` | cost_filter.py | ✅ | Viabilidad por costos |
| `LocationFilter` | location_filter.py | ✅ | Same-zone prevention |
| `VolatilityShockFilter` | volatility_shock_filter.py | ✅ | True range detection |
| `PostSweepConfirmationFilter` | post_sweep_filter.py | ✅ | Break+retest |
| `BTCCorrelationFilter` | btc_correlation_filter.py | ⚠️ BYPASS | Solo para altcoins |
| `OvertradeFilter` | overtrade_filter.py | ✅ | Prevenir overtrade |

### 3.3 Pipeline de Filtros

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `EntryFilterPipeline` | filter_pipeline.py | ✅ | Orquesta todos los filtros |
| `FilterDecision` | filter_interface.py | ✅ | Resultado unificado |
| `BaseFilter` | filter_interface.py | ✅ | Interfaz base |
| `FilterPipeline` | filter_interface.py | ✅ | Clase base pipeline |

### 3.4 Sistema de Filtro Inteligente

| Clase | Archivo | ¿Activo? | Descripción |
|-------|---------|----------|-------------|
| `IntelligentFilter` | intelligent_filter.py | ✅ | Decisión inteligente |
| `FilterSeverity` | intelligent_filter.py | ✅ | HARD/SOFT/INFO |
| `FilterCategory` | intelligent_filter.py | ✅ | RISK/TIMING/COST |
| `FilterResult` | intelligent_filter.py | ✅ | Resultado individual |
| `IntelligentFilterDecision` | intelligent_filter.py | ✅ | Decisión final |
| `FilterCheckResult` | entry_evaluation.py | ✅ | Check result |
| `FilterStats` | api/schemas.py | ✅ | Stats para API |

---

## 4. 📊 REGIMES (Clasificación de Mercado)

### ⚠️ PROBLEMA CRÍTICO: **13 DEFINICIONES DE RÉGIMEN PARALELAS**

### 4.1 Definiciones de `RegimeMode` (3 DUPLICADOS)

| Archivo | Valores |
|---------|---------|
| `regime_context.py:40` | TREND, RANGE, BREAKOUT, TREND_EXHAUSTION, NO_TRADE |
| `unified_intelligence.py:57` | RANGE_EXTREME, RANGE_MID, TRANSITION, TREND, EXPANSION |
| (implícito en varios) | TREND_DAY, RANGE_DAY, TRANSITION_DAY |

### 4.2 Definiciones de `RegimeContext` (2 DUPLICADOS)

| Archivo | Línea | Propósito |
|---------|-------|-----------|
| `regime_context.py:50` | 50 | Contexto de mercado con sizing/SL |
| `regime_rules.py:38` | 38 | Contexto para reglas de régimen |

### 4.3 Funciones `get_regime_classifier()` (3 DUPLICADOS)

| Archivo | Retorna |
|---------|---------|
| `regime_classifier.py:257` | `RegimeClassifier` |
| `market_regime.py:408` | `ManagementRegimeClassifier` |
| `trading_regime.py:558` | `TradingRegimeClassifier` |

### 4.4 Clasificadores de Régimen (5 DIFERENTES)

| Clase | Archivo | Propósito | ¿Activo? |
|-------|---------|-----------|----------|
| `RegimeClassifier` | regime_classifier.py | Entry regime | ✅ |
| `ManagementRegimeClassifier` | market_regime.py | Position management | ✅ |
| `TradingRegimeClassifier` | trading_regime.py | Trading decisions | ⚠️ Parcial |
| `EdgeRegimeClassifier` | edge_regime_classifier.py | Edge validity | ✅ |
| `RegimeContextManager` | regime_context.py | **SINGLE SOURCE** | ✅ |
| `ActivityRegimeAnalyzer` | activity_regime.py | Activity scoring | ⚠️ Parcial |

### 4.5 Enums de Régimen (7 DIFERENTES)

| Enum | Archivo | Valores |
|------|---------|---------|
| `EntryRegime` | regime_detector.py | TREND_EXPANSION, TREND_EXHAUSTION, NORMAL, PROBING, RANGE, CHAOS |
| `ManagementRegime` | market_regime.py | TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD |
| `TradingRegime` | trading_regime.py | STRONG_TREND, WEAK_TREND, RANGE, CHOPPY |
| `EdgeRegime` | edge_regime_classifier.py | REGIME_A, REGIME_B, REGIME_C, REGIME_D |
| `RegimeMode` | regime_context.py | TREND, RANGE, BREAKOUT, TREND_EXHAUSTION, NO_TRADE |
| `MarketRegime` | common/types.py | TRENDING, RANGING, VOLATILE |
| `TrailingRegime` | trailing_stop.py | AGGRESSIVE, STANDARD, TIGHT |

### 4.6 Detectores de Régimen Adicionales

| Clase | Archivo | ¿Activo? |
|-------|---------|----------|
| `DailyRegimeDetector` | trading_config.py | ✅ |
| `RegimeDetector` | regime_detector.py | ✅ |
| `RegimeConfig` (3 definiciones) | Múltiples | ⚠️ DUPLICADO |
| `RegimeThresholds` | unified_constants.py | ✅ |
| `RegimeGuidance` | market_regime.py | ✅ |
| `RegimeCharacteristics` | trading_regime.py | ✅ |
| `RegimeAnalysis` | trading_regime.py | ✅ |
| `RegimeMetrics` | edge_regime_classifier.py | ✅ |
| `RegimeTransition` | edge_regime_classifier.py | ✅ |
| `RegimeScoreResult` | regime_handlers.py | ✅ |
| `RegimeMultiplier` | simplified_sizing.py | ✅ |

---

## 5. 🔍 OTRAS SEÑALES Y DETECTORES

### 5.1 Anticipation Engine

| Clase | Archivo | ¿Activo? |
|-------|---------|----------|
| `AnticipationEngine` | anticipation.py | ✅ |
| `AnticipationSignal` | anticipation.py | ✅ |
| `AnticipationState` | anticipation.py | ✅ |

**Señales de Anticipation:**
- `COMPRESSION_BREAKOUT` - BB squeeze
- `FAILED_TEST` - Fallback
- `TIME_EXPANSION` - Time-based
- `STRUCTURE_BUILD` - HLs/LHs
- `DIVERGENCE_UNWIND` - RSI div

### 5.2 Bias y Strength

| Clase | Archivo | ¿Activo? |
|-------|---------|----------|
| `BiasStrength` | trading_config.py | ✅ |
| `BiasStrengthConfig` | trading_config.py | ✅ |
| `TrendExpansionConfig` | trading_config.py | ✅ |

### 5.3 Otros Detectores

| Clase | Archivo | ¿Activo? |
|-------|---------|----------|
| `PullbackShortSignal` | pullback_short.py | ⚠️ Parcial |
| `ReversalSignal` | fast_reversal_detector.py | ✅ |
| `FailSilentDetector` | fail_silent_detector.py | ✅ |
| `MLDriftDetector` | drift_detector.py | ⚠️ ML |

---

## 6. 📋 MAPA DE DUPLICACIONES

### 6.1 Código 100% Duplicado/Muerto

| Componente | Archivo | Razón |
|------------|---------|-------|
| `UnifiedTradingIntelligence` | unified_intelligence.py | **NUNCA SE CONSULTA** |
| `DirectionSource` | unified_intelligence.py | Duplica `SignalPriority` |
| `DirectionDecision` | unified_intelligence.py | Duplica `DirectionSignal` |
| `RegimeMode` | unified_intelligence.py | Duplica `regime_context.py` |
| `MTFStructureFilter` | filters.py | Comentado/desactivado |

### 6.2 Duplicaciones Parciales (Diferentes Propósitos pero Confuso)

| Concepto | Definiciones | Archivos |
|----------|--------------|----------|
| `RegimeContext` | 2 | regime_context.py, regime_rules.py |
| `TrendDirection` | 2 | trend_intelligence.py, trading_regime.py |
| `RegimeConfig` | 3 | regime_detector.py, market_regime.py, regime_classifier.py |
| `get_regime_classifier()` | 3 | Retornan clases diferentes |

### 6.3 Clasificadores Paralelos (Mismo Concepto, Diferentes Implementaciones)

```
                 ┌──────────────────────────────────────┐
                 │   ¿QUÉ RÉGIMEN ESTAMOS?              │
                 └──────────────────────────────────────┘
                              │
      ┌───────────────────────┼───────────────────────┐
      │                       │                       │
      ▼                       ▼                       ▼
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│ RegimeClass │       │ TradingReg  │       │ EdgeRegime  │
│ (entry)     │       │ Classifier  │       │ Classifier  │
└─────────────┘       └─────────────┘       └─────────────┘
      │                       │                       │
      ▼                       ▼                       ▼
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│ ManagementR │       │ RegimeCont  │       │ ActivityReg │
│ Classifier  │       │ Manager     │       │ Analyzer    │
└─────────────┘       └─────────────┘       └─────────────┘

→ 6 CLASIFICADORES PARA EL MISMO CONCEPTO
```

---

## 7. 🎯 RECOMENDACIONES

### 7.1 Eliminar Inmediatamente

| Archivo | Componente | Acción |
|---------|------------|--------|
| `unified_intelligence.py` | Todo | ❌ **ELIMINAR** |
| `filters.py` | `MTFStructureFilter` | ❌ Eliminar código muerto |
| `trading_regime.py:43` | `TrendDirection` | ❌ Usar el de `trend_intelligence.py` |

### 7.2 Consolidar en Fase 2

| Antes | Después |
|-------|---------|
| 3x `RegimeConfig` | 1x `RegimeConfig` en `unified_constants.py` |
| 3x `get_regime_classifier()` | 1x con enum para tipo |
| 2x `RegimeContext` | 1x en `regime_context.py` |
| 6x Clasificadores de régimen | 2-3 con propósitos claros |

### 7.3 Arquitectura Propuesta (Simplificada)

```
                    ┌─────────────────────────────────┐
                    │       DirectionOrchestrator     │
                    │   (ÚNICA FUENTE DE DIRECCIÓN)   │
                    └───────────────┬─────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
┌───────────────┐         ┌─────────────────┐         ┌────────────────┐
│ TrendIntelli  │         │ EdgeStateMgr    │         │ RegimeContext  │
│ (Cycle phase) │         │ (Edge state)    │         │ Manager        │
└───────────────┘         └─────────────────┘         └────────────────┘
        │                           │                           │
        └───────────────────────────┼───────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │     EntryFilterPipeline       │
                    │     (11 filtros activos)      │
                    └───────────────────────────────┘
```

---

## 8. 📁 INVENTARIO DE ARCHIVOS

### 8.1 Archivos de Edge (OK - Bien organizados)
- `src/domain/trading/edge_state_manager.py` - 3585 líneas
- `src/domain/trading/edge/edge_enforcement.py`
- `src/domain/trading/edge/edge_validator.py`
- `src/domain/trading/edge/edge_recovery_protocol.py`

### 8.2 Archivos de Trend (PARCIALMENTE DUPLICADO)
- `src/domain/market/regime/trend_intelligence.py` ✅
- `src/domain/market/regime/trading_regime.py` ⚠️ Duplica TrendDirection
- `src/domain/trading/direction_orchestrator.py` ✅

### 8.3 Archivos de Filters (OK - Bien organizados)
- `src/domain/market/filters/` - 12 archivos

### 8.4 Archivos de Regime (CAOS TOTAL)
- `src/domain/market/regime/regime_context.py` ✅ **USAR ESTE**
- `src/domain/market/regime/regime_detector.py`
- `src/domain/market/regime/market_regime.py`
- `src/domain/market/regime/trading_regime.py`
- `src/domain/market/regime/edge_regime_classifier.py`
- `src/domain/market/regime/activity_regime.py`
- `src/domain/market/filters/regime_classifier.py`
- `src/domain/trading/regime_rules.py`
- `src/domain/trading/unified_intelligence.py` ❌ **ELIMINAR**

---

## 9. 📊 ESTADÍSTICAS FINALES

| Métrica | Valor |
|---------|-------|
| Total clases Edge/Trend/Filter/Regime | 80+ |
| Clases activamente usadas | ~50 |
| Clases duplicadas/muertas | ~30 |
| Archivos de régimen | 9 |
| Definiciones de "régimen" | 13+ |
| Funciones `get_regime_classifier` | 3 |
| Tiempo estimado de limpieza | 4-8 horas |

---

## 10. 🚨 ACCIÓN INMEDIATA RECOMENDADA

1. **Eliminar `unified_intelligence.py`** (código 100% muerto)
2. **Eliminar `TrendDirection` duplicado** en `trading_regime.py`
3. **Marcar archivos deprecated** con comentarios claros
4. **Consolidar RegimeConfig** en `unified_constants.py`
5. **Documentar cuál clasificador usar para qué**

---

*Reporte generado el 5 de Febrero de 2026*
*Análisis completo de 80+ componentes en 50+ archivos*
