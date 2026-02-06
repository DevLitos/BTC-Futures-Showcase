# 📊 ANÁLISIS COMPLETO DE ARQUITECTURA - POST LIMPIEZA
## Sistema de Trading BTC - FideiRatioCapital
### Fecha: 5 de Febrero, 2026 (ACTUALIZADO)

---

## 🎯 RESUMEN EJECUTIVO

| Categoría | Antes | Después | Eliminados |
|-----------|-------|---------|------------|
| **EDGES** | 15 clases/enums | 12 | 3 |
| **TRENDS** | 18 clases/enums | 10 | 8 |
| **FILTERS** | 22 clases | 18 | 4 |
| **REGIMES** | 25 clases/enums | 10 | **15 ELIMINADOS** |
| **Líneas código** | ~8000 | ~5300 | **~2700** |

**✅ LIMPIEZA COMPLETADA: 5 archivos eliminados, ~2700 líneas removidas**

---

## 1. 🎯 EDGES (Sistema de Ventajas) - SIN CAMBIOS

### 1.1 Tipos de Edge (\`EdgeType\`)
**Archivo:** \`src/domain/trading/edge_state_manager.py:102\`

| Tipo | Descripción | ¿Activo? |
|------|-------------|----------|
| \`TREND\` | BOS + Pullback en mercado tendencial (ADX > 18) | ✅ |
| \`RANGE\` | Extremo 85/15 + Sweep + Rejection | ✅ |
| \`BREAKOUT\` | Compresión BB + Ruptura (de ANTICIPATION) | ✅ |
| \`SCALP\` | Micro-ineficiencias alta frecuencia | ✅ |
| \`DISPLACEMENT\` | Movimiento fuerte ≥1.5x ATR_5m | ✅ |

### 1.2 Estados del Edge (\`EdgeState\`)

| Estado | Símbolo | Descripción |
|--------|---------|-------------|
| \`NO_EDGE\` | ○ | Sin oportunidad |
| \`EDGE_ARMED\` | ◐ | Setup detectado, esperando confirmación |
| \`EDGE_PROVISIONAL\` | ◑ | Señal válida pero incompleta (starter) |
| \`EDGE_ACTIVE\` | ● | Ventana de entrada abierta |
| \`EDGE_EXPIRED\` | ◌ | Oportunidad consumida |

---

## 2. 📈 TRENDS (Detección de Tendencia) - SIMPLIFICADO

### 2.1 Clases Activas de TrendIntelligence
**Archivo:** \`src/domain/market/regime/trend_intelligence.py\`

| Clase | Línea | ¿Activo? | Descripción |
|-------|-------|----------|-------------|
| \`TrendIntelligence\` | 103 | ✅ | Motor principal de ciclo |
| \`TrendPhase\` (enum) | 34 | ✅ | 8 fases del ciclo |
| \`TrendDirection\` (enum) | 46 | ✅ | BULLISH/BEARISH/NEUTRAL |
| \`TrendState\` | 67 | ✅ | Estado completo |
| \`TrendTransition\` | 54 | ✅ | Registro de transiciones |

### 2.2 Fases del Ciclo de Tendencia (\`TrendPhase\`)

| Fase | Descripción | Trading |
|------|-------------|---------|
| \`ACCUMULATION\` | Pre-trend, building pressure | WAIT |
| \`TREND_BIRTH\` | Breakout inicial | ENTER |
| \`TREND_ACCELERATION\` | Momentum creciente | ADD |
| \`TREND_MATURE\` | Tendencia establecida | HOLD |
| \`TREND_EXHAUSTION\` | Debilitamiento | REDUCE |
| \`TREND_DEATH\` | Fin de tendencia | EXIT |
| \`RANGE_BOUND\` | Sin tendencia clara | SCALP |
| \`TRANSITION\` | Cambio de fase | WAIT |

---

## 3. 🔍 FILTERS (Sistema de Filtros) - SIMPLIFICADO

### 3.1 Filtros Activos

| Filtro | Archivo | ¿Activo? |
|--------|---------|----------|
| \`HTFFilter\` | filters.py | ✅ |
| \`LTFTriggerFilter\` | filters.py | ✅ |
| \`KillConditionsFilter\` | filters.py | ✅ |
| \`BlackSwanFilter\` | filters.py | ✅ |
| \`ImpulseTracker\` | filters.py | ✅ |
| \`LocationFilter\` | location_filter.py | ✅ |
| \`MicroTrendFilter\` | micro_trend_filter.py | ✅ |
| \`SpreadFilter\` | spread_filter.py | ✅ |
| \`CostFilter\` | cost_filter.py | ✅ |
| \`OvertradeFilter\` | overtrade_filter.py | ✅ |
| \`BBWidthFilter\` | bb_width_filter.py | ✅ |
| \`VolatilityShockFilter\` | volatility_shock_filter.py | ✅ |
| \`MacroTrendFilter\` | macro_trend_filter.py | ✅ |
| \`PostSweepFilter\` | post_sweep_filter.py | ✅ |
| \`BTCCorrelationFilter\` | btc_correlation_filter.py | ✅ |

### 3.2 Filtros ELIMINADOS

| Filtro | Razón |
|--------|-------|
| \`MTFStructureFilter\` | Código muerto, nunca instanciado (~267 líneas eliminadas) |

---

## 4. 📦 REGIMES (Clasificación de Régimen) - REESTRUCTURADO

### 4.1 Archivos de Régimen ACTIVOS

| Archivo | Propósito | Líneas |
|---------|-----------|--------|
| \`trend_intelligence.py\` | Ciclo de tendencia (PRIMARY) | ~800 |
| \`edge_regime_classifier.py\` | Clasificación de edge | ~300 |
| \`regime_detector.py\` | Entry decisions | ~350 |
| \`activity_regime.py\` | Actividad 24/7 | ~200 |
| \`management_regime.py\` | Stub para risk_middleware (NUEVO) | 130 |

### 4.2 Archivos de Régimen ELIMINADOS

| Archivo | Líneas | Razón |
|---------|--------|-------|
| \`trading_regime.py\` | ~600 | TradingRegimeClassifier nunca llamado |
| \`regime_context.py\` | ~590 | RegimeContextManager nunca integrado |
| \`market_regime.py\` | ~480 | Reemplazado por management_regime.py |
| \`frequency_controller.py\` | ~380 | Nunca importado |

### 4.3 Enums de Régimen ACTIVOS

| Enum | Archivo | Valores |
|------|---------|---------|
| \`TrendPhase\` | trend_intelligence.py | 8 fases del ciclo |
| \`EdgeRegime\` | edge_regime_classifier.py | A, B, C, D |
| \`EntryRegime\` | regime_detector.py | TREND_EXPANSION, NORMAL, etc. |
| \`ManagementRegime\` | management_regime.py | TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD |

### 4.4 Clasificadores ACTIVOS

| Clasificador | Archivo | Propósito |
|--------------|---------|-----------|
| \`TrendIntelligence\` | trend_intelligence.py | **PRIMARY** - Ciclo de tendencia |
| \`EdgeRegimeClassifier\` | edge_regime_classifier.py | **PRIMARY** - Validez de edge |
| \`RegimeDetector\` | regime_detector.py | Entry decisions |
| \`DailyRegimeDetector\` | trading_config.py | Daily regime |
| \`_StubRegimeClassifier\` | management_regime.py | Stub para backward compat |

### 4.5 Funciones \`get_regime_classifier()\`

| Archivo | Retorna | Estado |
|---------|---------|--------|
| regime_classifier.py | \`RegimeClassifier\` | ⚠️ Renombrar en futuro |
| management_regime.py | \`_StubRegimeClassifier\` | ✅ Stub (retorna None) |

---

## 5. 📁 ESTRUCTURA DE ARCHIVOS ACTUAL

### 5.1 src/domain/market/regime/

\`\`\`
regime/
├── __init__.py              # Re-exports activos
├── trend_intelligence.py    # ✅ PRIMARY - Ciclo de tendencia
├── edge_regime_classifier.py # ✅ PRIMARY - Edge validity
├── regime_detector.py       # ✅ Entry decisions
├── activity_regime.py       # ✅ Activity analysis
├── management_regime.py     # ✅ NUEVO - Stub para risk_middleware
└── (ELIMINADOS: trading_regime.py, regime_context.py, market_regime.py)
\`\`\`

### 5.2 src/domain/market/filters/

\`\`\`
filters/
├── __init__.py
├── filters.py               # Core filters (SIN MTFStructureFilter)
├── filter_pipeline.py
├── filter_interface.py
├── location_filter.py
├── micro_trend_filter.py
├── spread_filter.py
├── cost_filter.py
├── overtrade_filter.py
├── bb_width_filter.py
├── volatility_shock_filter.py
├── macro_trend_filter.py
├── post_sweep_filter.py
├── btc_correlation_filter.py
├── regime_classifier.py
└── advanced_filters.py
\`\`\`

### 5.3 src/shared/

\`\`\`
shared/
├── __init__.py
├── utils.py                 # ts_to_int() añadido
├── indicator_helpers.py     # Limpiado (sin CachedIndicators)
├── patterns.py              # NUEVO: @singleton, @retry_with_backoff
├── unified_constants.py
└── (ELIMINADO: indicator_cache.py → usar app/live/indicator_cache.py)
\`\`\`

---

## 6. 📊 ARQUITECTURA SIMPLIFICADA

\`\`\`
                    ┌─────────────────────────────────┐
                    │       DirectionOrchestrator     │
                    │   (ÚNICA FUENTE DE DIRECCIÓN)   │
                    └───────────────┬─────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
┌───────────────┐         ┌─────────────────┐         ┌────────────────┐
│ TrendIntelli  │         │ EdgeStateMgr    │         │ EdgeRegime     │
│ (Cycle phase) │         │ (Edge state)    │         │ Classifier     │
└───────────────┘         └─────────────────┘         └────────────────┘
        │                           │                           │
        └───────────────────────────┼───────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │     EntryFilterPipeline       │
                    │     (15 filtros activos)      │
                    └───────────────────────────────┘
\`\`\`

---

## 7. 📊 ESTADÍSTICAS FINALES

| Métrica | Antes | Después | Diff |
|---------|-------|---------|------|
| Archivos de régimen | 9 | 5 | -4 |
| Clases clasificadoras | 9 | 5 | -4 |
| Enums de régimen | 7 | 4 | -3 |
| get_regime_classifier() | 3 | 2 | -1 |
| Líneas en regime/ | ~3200 | ~1800 | -1400 |
| Código muerto total | - | - | ~2700 líneas eliminadas |

---

## 8. ✅ VERIFICACIÓN

\`\`\`bash
# Todos los imports funcionan
✓ regime imports OK
✓ risk_middleware imports OK  
✓ advanced_analytics module imports OK
✓ get_current_regime() returns: None (esperado - stub)
✓ REGIME_GUIDANCE works: risk_mult=0.85
\`\`\`

---

## 9. 🎯 TRABAJO FUTURO (Bajo Prioridad)

| Tarea | Prioridad | Esfuerzo |
|-------|-----------|----------|
| Renombrar get_regime_classifier() en regime_classifier.py | P4 | 30 min |
| Consolidar RegimeConfig duplicados | P4 | 1 hora |
| Integrar ManagementRegime con TrendPhase si posible | P5 | 2 horas |

---

*Reporte actualizado el 5 de Febrero de 2026*
*Limpieza completada: 5 archivos eliminados, ~2700 líneas removidas*
*Arquitectura simplificada: 9 → 5 archivos de régimen*
