# Machine Learning Implementation Study
## DayTrader 2.1 - BTC Perpetual Trading Bot
**Date:** January 23, 2026  
**Last Updated:** January 24, 2026 - **SHADOW MODE IMPLEMENTED** ✅

---

## Executive Summary

This document provides a comprehensive analysis of ML integration opportunities for the DayTrader 2.1 system. 

### 🎉 STATUS UPDATE (January 24, 2026)

**ML SHADOW MODE is now LIVE!** The system has been upgraded with ML infrastructure:

- ✅ `src/ml/` module structure created (14 files)
- ✅ LightGBM-based Entry Score Refinement implemented
- ✅ SHADOW mode active: Logs predictions WITHOUT affecting decisions
- ✅ Integrated in `signal_engine.py` with safe fallback
- ✅ Feature extraction pipeline operational
- ✅ Phase-based deployment ready (SHADOW → HYBRID → PRIMARY)

**Next Milestone:** Collect 300 trades to advance to HYBRID mode.

---

## 1. Current State Analysis

### 1.1 ML Libraries in Use
| Library | Present? | Notes |
|---------|----------|-------|
| scikit-learn | ✅ Yes | Added in requirements.txt (>=1.4.0) |
| TensorFlow | ❌ No | Not needed for tabular data |
| PyTorch | ❌ No | Not needed for tabular data |
| XGBoost | ❌ No | Using LightGBM instead |
| **LightGBM** | ✅ Yes | **PRIMARY MODEL** (>=4.3.0) |
| CatBoost | ❌ No | Not needed |

**Conclusion:** System now has ML infrastructure in SHADOW mode. Rule-based system still makes all decisions.

### 1.2 Data Currently Collected (ML Training Potential)

The system has **excellent data logging infrastructure** in `trade_logs/`:

#### Trade Records (`TradeLogRecord` - trade_logger.py)
```
Per-trade captured data:
├── Identity: trade_id, symbol, direction, trade_type
├── Timing: entry_time, exit_time, duration_minutes
├── Prices: entry_price, exit_price, stop_loss, take_profit
├── Performance: pnl_usd, pnl_r, mfe_r, mae_r
├── Context: regime, volatility_regime, session, day_of_week
├── Scoring: entry_score, score_breakdown (structure, timing, momentum, context, synergy)
└── Tags: entry_tags[], exit_tags[], metadata{}
```

**Sample Trade Record (from trades_2026-01-23.jsonl):**
- Full breakdown captured: `structure=2, timing=0, momentum=2, context=2, synergy=0`
- Adjustments tracked: `micro_trend_adj=1.0, filter_pipeline_adj=-0.75`
- Rich metadata: `worst_case_rr`, `order_fill_latency_ms`, `funding_rate`, etc.

#### Event Logs
- `events_YYYY-MM-DD.jsonl` - Real-time event stream (trade opened, TP hit, etc.)
- Useful for sequence learning and state transitions

### 1.3 Features/Indicators Already Calculated

The codebase calculates ~50+ features that could directly serve as ML inputs:

#### Price-Based Indicators (`indicators.py`)
| Indicator | Timeframes | ML Potential |
|-----------|-----------|--------------|
| EMA | 20, 50, 100, 200 | High - trend direction |
| ADX | 1h, 5m | High - regime detection |
| ADX Slope | 5-bar regression | High - momentum of momentum |
| ATR | 1m, 5m, 1h | High - volatility state |
| RSI | 14-period | Medium - mean reversion |
| VWAP | Daily | High - institutional reference |
| Bollinger Bands | 20/2σ | High - squeeze detection |
| CVD | Cumulative | Medium - order flow proxy |

#### Structural Indicators
| Feature | Source | ML Potential |
|---------|--------|--------------|
| Break of Structure (BOS) | `indicators.py` | High - momentum confirmation |
| Higher Lows / Lower Highs | `indicators.py` | High - structure classification |
| Swing Pivots | `micro_trend_filter.py` | High - key levels |
| Extension Z-score | `indicators.py` | High - mean reversion signal |

#### Context Features
| Feature | Source | ML Potential |
|---------|--------|--------------|
| Percentile 60d/20d | `location_filter.py` | Very High - macro context |
| Headroom to High/Low (ATR) | `location_filter.py` | High - room to run |
| Session (ASIA/LONDON/NY) | `config.py` | High - time patterns |
| Funding Rate | `external_data.py` | High - sentiment proxy |
| Volume Expansion Ratio | `indicators.py` | High - breakout confirmation |
| Spread (bid-ask) | `spread_filter.py` | Medium - liquidity proxy |

#### Regime Classifications (Rule-Based → ML Target)
| Regime Type | Current Logic | ML Opportunity |
|-------------|---------------|----------------|
| Entry Regime | `TREND_EXPANSION, TREND_EXHAUSTION, RANGE, CHAOS` | **Replace rules with ML classifier** |
| Management Regime | `TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD` | **Predict transitions** |
| Daily Regime | `TREND_DAY, RANGE_DAY, TRANSITION_DAY` | **Predict at session start** |
| Bias Strength | `BULL_STRONG, BEAR_STRONG, NEUTRAL` | **Probabilistic instead of binary** |

---

## 2. Recommended ML Use Cases (Priority Ranked)

### 2.1 🥇 PRIORITY 1: Regime Transition Prediction

**Current Problem:**
The `RegimeDetector` uses static thresholds:
```python
# Current rule-based logic (regime_detector.py)
if adx > 25 and adx_slope > 0 and range_expansion > 1.2:
    regime = TREND_EXPANSION
elif adx > 25 and adx_slope <= 0:
    regime = TREND_EXHAUSTION
elif adx < 18:
    regime = RANGE
```

**Why ML Would Help:**
- Predict regime transitions 1-3 candles BEFORE they happen
- Current system is REACTIVE (detects after transition), ML can be PREDICTIVE
- Regime changes are the #1 cause of losing trades (e.g., entering RANGE during TREND→RANGE transition)

**Target Variable:**
- `next_regime_in_3_candles` (classification: 4 classes)
- Or `regime_change_probability` (regression: 0-1)

**Features Available:**
- ADX value + slope (5-bar)
- ATR ratio (current/baseline)
- BB width + expansion rate
- VWAP distance persistence
- Volume expansion ratio
- Current regime duration (bars in current state)

**Expected Impact:** +15-25% reduction in "bad regime" entries

---

### 2.2 🥈 PRIORITY 2: Entry Score Refinement

**Current Problem:**
Entry scoring uses fixed weights:
```python
# Current: 4 blocks × 0-2 points (entry_scorer.py)
score = structure + timing + momentum + context + synergy
# structure=2, timing=2, momentum=2, context=2, synergy=1 → max 9
```

**Why ML Would Help:**
- Learn optimal weights from actual trade outcomes
- Discover non-linear interactions (e.g., `high_momentum + extreme_percentile = bad`)
- Current fixed weights may be suboptimal

**Approach:** Two options:

**Option A: Score Calibration (Simpler)**
- Train model to predict `P(win | score, features)`
- Use model probability as entry score instead of fixed formula
- Gradient boosting on existing trade data

**Option B: Direct Entry Classification (More Complex)**
- Input: All features at entry moment
- Output: P(profitable trade)
- Skip scoring entirely, use ML probability

**Target Variable:**
- `pnl_r > 0` (binary classification)
- Or `pnl_r` (regression for expected R)

**Features:**
- Current score components as separate features
- Raw indicators: ADX, ATR, RSI, BB_width, etc.
- Context: session, day_of_week, percentile_60d
- Recent history: last_trade_pnl, consecutive_losses

**Expected Impact:** +10-20% improvement in win rate

---

## 🆕 SECTION 2.5: IMPLEMENTED ARCHITECTURE (January 24, 2026)

### Current ML Pipeline Flow
```
Features crudas (precio, volumen, vol, OI, funding)
        ↓
Feature Engineering (extract_features_live)
        ↓
LightGBM (clasificación → probabilidad de win)
        ↓
SHAP (explicación - pendiente de activar)
        ↓
Trade score + filtros de riesgo
```

### 📊 Modelos Implementados

#### 1️⃣ Probabilidad de Trade Ganador (ACTIVO en SHADOW)
- **Modelo:** LGBMClassifier
- **Target:** 
  - `1` si TP alcanzado (win)
  - `0` si SL (loss)
- **Output:** `P(win) ∈ [0, 1]`

**Hiperparámetros conservadores (para datasets pequeños):**
```python
lgb_params = {
    'objective': 'binary',
    'metric': 'auc',
    'boosting_type': 'gbdt',
    'num_leaves': 8,           # Muy shallow para evitar overfitting
    'max_depth': 3,            # Profundidad limitada
    'learning_rate': 0.01,     # Aprendizaje lento
    'n_estimators': 100,       # Pocos árboles
    'min_child_samples': 20,   # Mínimo 20 samples por hoja
    'reg_alpha': 0.1,          # Regularización L1
    'reg_lambda': 0.1,         # Regularización L2
}
```

#### 2️⃣ Clasificador de Régimen (PENDIENTE)
- **Modelo:** LGBMClassifier
- **Target:** `TREND`, `RANGE`, `CHOPPY`, `ALTA_VOL`
- **Estado:** Estructura preparada, pendiente de implementar

### 🔧 Features que REALMENTE Usamos (BTC Intradía)

**No 200 features, sino las buenas:**

```python
ENTRY_FEATURES = [
    # Score components (calculados por sistema rule-based)
    'score_structure', 'score_timing', 'score_momentum', 
    'score_context', 'score_synergy',
    
    # Volatilidad
    'atr_ratio',           # ATR normalizado
    'bb_width',            # Ancho Bollinger
    'bb_squeeze',          # Squeeze detection
    
    # Tendencia
    'adx_1h', 'adx_5m',    # ADX multi-timeframe
    'effective_adx',       # ADX ponderado
    
    # Location/Context
    'percentile_60d',      # Ubicación en rango histórico
    'location_headroom_atr',
    'location_in_extreme',
    
    # Momentum
    'rsi_14',              # RSI estándar
    'cvd_bullish',         # Delta volumen
    'volume_ratio',        # Expansión/contracción
    
    # Session (one-hot)
    'session_good', 'session_poor',
    
    # Régimen (one-hot)
    'trading_mode_trend', 'trading_mode_range', 'trading_mode_transition',
    
    # External
    'funding_rate',
    
    # Historia
    'consecutive_losses',
]
```

**Total: 24 features** - LightGBM + pocas features > red neuronal gigante

### ⚠️ Principios de Validación

1. **Separación temporal estricta:** Walk-forward validation
2. **Sin shuffle:** Nunca mezclar datos de diferentes períodos
3. **OOS mínimo:** AUC > 0.55 fuera de muestra
4. **Consistencia:** 60%+ de folds positivos

### 📁 Estructura de Archivos ML
```
src/ml/
├── __init__.py                    # Exports principales
├── config.py                      # MLPhase enum, EntryMLConfig
├── feature_extractor.py           # ENTRY_FEATURES, extract_features_live()
├── models/
│   ├── __init__.py
│   ├── model_base.py              # BaseModel interface
│   └── entry_scorer_ml.py         # EntryScoreML (LightGBM wrapper)
├── inference/
│   ├── __init__.py
│   └── inference_engine.py        # MLInferenceEngine (main entry point)
├── training/
│   ├── __init__.py
│   ├── data_loader.py             # TradeDataLoader
│   └── train_entry_model.py       # train_entry_model(), check_training_readiness()
├── monitoring/
│   ├── __init__.py
│   └── drift_detector.py          # MLDriftDetector
└── serialized/
    └── .gitkeep                   # Modelos serializados (tras training)
```

### 🚦 Fases de Deployment

| Fase | Estado | Requisitos | Comportamiento |
|------|--------|------------|----------------|
| **DISABLED** | - | - | ML completamente off |
| **SHADOW** | ✅ ACTIVO | 0+ trades | Predice y loguea, NO afecta decisiones |
| **HYBRID** | Pendiente | 300+ trades, AUC>0.55 | Ajusta score ±1 punto |
| **PRIMARY** | Pendiente | 1000+ trades, validación | ML es scoring principal |

### 🔗 Punto de Integración en signal_engine.py

```python
# Línea ~4715 en signal_engine.py
# 🤖 ML SHADOW MODE - Log prediction without affecting decision
try:
    feature_snapshot = self._build_ml_feature_snapshot()
    ml_adjusted_score, ml_prob, ml_reason = self._ml_inference.get_entry_adjustment(
        rule_score=int(score),
        feature_snapshot=feature_snapshot
    )
    self._current_trade_tags['ml_win_prob'] = round(ml_prob, 4)
    self._current_trade_tags['ml_adjusted_score'] = ml_adjusted_score
    self._current_trade_tags['ml_reason'] = ml_reason
    self._current_trade_tags['ml_phase'] = self._ml_inference.phase.name
except Exception as e:
    logger.warning(f"⚠️ ML inference error (non-blocking): {e}")
```

### 🛠️ Utility Script: `tools/ml_status.py`

```bash
# Ver estado actual del sistema ML
python tools/ml_status.py
```
Output ejemplo:
```
🤖 ML Status Report
==================
Phase: SHADOW (log-only, not affecting decisions)
Trades logged: 11
Need for HYBRID: 289 more trades
Current win rate: 63.6%
```

---

### 2.3 🥉 PRIORITY 3: Exit Timing Optimization

**Current Problem:**
Multiple exit mechanisms with fixed thresholds:
- `TIME_INVALIDATION`: 5 candles max hold
- `VPOC_EXIT`: Exit at VPOC (value area)
- Trailing stops with fixed ATR multipliers

**Why ML Would Help:**
- Learn optimal hold time per regime/entry quality
- Predict when MFE will be reached
- Dynamic trailing stop distance based on conditions

**Target Variables:**
- `optimal_exit_candles`: When did MFE occur?
- `should_exit_now`: Binary (better exit opportunity coming or not)

**Features:**
- Time in trade (candles)
- Current unrealized PnL (R)
- MFE so far (R)
- MAE so far (R)
- Current regime vs entry regime
- Momentum indicators at current moment

**Expected Impact:** +5-15% improvement in avg R per trade

---

### 2.4 PRIORITY 4: Position Sizing Optimization

**Current Problem:**
Position sizing uses rule-based multipliers:
```python
# From config.py
COUNTERTREND_SIZE_MULT = 0.50
NEUTRAL_SIZE_MULT = 0.75
WITHTREND_SIZE_MULT = 1.0
```

**Why ML Would Help:**
- Kelly criterion approximation based on predicted edge
- Size up when conditions are favorable, size down when uncertain
- Currently sizes are static per regime

**Approach:**
- Predict expected Sharpe ratio for trade
- Use Kelly formula: `size = edge / variance`
- Or simpler: regress `optimal_size` on features

**Expected Impact:** +10-20% improvement in risk-adjusted returns

---

## 3. Recommended ML Models

### 3.1 Model Selection Matrix

| Use Case | Recommended Model | Why | Latency | Interpretability |
|----------|-------------------|-----|---------|------------------|
| Regime Prediction | **XGBoost** or LightGBM | Handles tabular data well, fast inference, captures non-linearities | <1ms | High (SHAP values) |
| Entry Scoring | **LightGBM** | Slightly faster than XGBoost, good with categorical features (session) | <1ms | High |
| Exit Timing | **LSTM/GRU** or XGBoost | Sequential nature of hold decision; XGBoost if sequence not needed | 1-5ms | Medium |
| Position Sizing | **Linear Regression** or XGBoost | Simpler model preferred for critical decisions | <1ms | Very High |

### 3.2 Why NOT Deep Learning (Initially)

| Factor | Gradient Boosting | Deep Learning |
|--------|-------------------|---------------|
| Sample Size Needed | 1,000-10,000 trades | 100,000+ trades |
| Training Time | Minutes | Hours |
| Interpretability | High (SHAP) | Low |
| Overfitting Risk | Lower | Higher |
| Inference Latency | <1ms | 5-50ms |
| Feature Engineering | Manual (already done) | Automatic |

**Recommendation:** Start with gradient boosting (XGBoost/LightGBM), upgrade to deep learning only if:
- You accumulate 50,000+ labeled trades
- Gradient boosting plateaus
- You need to capture complex temporal patterns

### 3.3 Specific Model Configurations

**Regime Prediction (XGBoost):**
```python
params = {
    'objective': 'multi:softprob',  # 4 regime classes
    'num_class': 4,
    'max_depth': 4,                  # Shallow to prevent overfitting
    'learning_rate': 0.05,
    'n_estimators': 200,
    'min_child_weight': 10,          # Conservative
    'subsample': 0.8,
    'colsample_bytree': 0.8,
}
```

**Entry Scoring (LightGBM):**
```python
params = {
    'objective': 'binary',           # Win/loss classification
    'metric': 'auc',
    'num_leaves': 15,                # Shallow
    'learning_rate': 0.03,
    'feature_fraction': 0.8,
    'bagging_fraction': 0.8,
    'bagging_freq': 5,
    'max_depth': 5,
}
```

---

## 4. Implementation Roadmap

### Phase 1: Data Collection Enhancement (Week 1-2)

**Current State:** Good logging, but missing some ML-critical data

**Additions Needed:**

1. **Feature Snapshot at Entry** (`trade_logger.py`)
```python
# Add to TradeLogRecord
feature_snapshot: Dict[str, float] = {
    'adx_1h': 23.5,
    'adx_slope_5bar': 0.3,
    'atr_ratio': 1.15,
    'bb_width': 450.0,
    'bb_width_expansion': 1.2,
    'percentile_60d': 72.3,
    'vwap_distance_atr': 0.8,
    'volume_ratio': 1.3,
    'rsi_14': 55.2,
    'funding_rate': 0.0003,
    'spread_pct': 0.012,
    'regime_duration_bars': 15,
    # ... all features
}
```

2. **Regime Labels at Entry and Exit**
```python
# Add regime tracking
entry_regime: str
exit_regime: str
regime_changed_during_trade: bool
```

3. **Exit Metadata Enhancement**
```python
# Add to exit logging
candles_to_mfe: int           # How many candles until MFE was hit
candles_to_mae: int           # How many candles until MAE was hit
exit_quality: str             # 'OPTIMAL', 'EARLY', 'LATE' (post-hoc analysis)
```

### Phase 2: Training Pipeline (Week 3-4)

**Architecture:**
```
trade_logs/
    ↓
[Feature Extractor]
    ↓
training_data/
    features_YYYYMMDD.parquet
    ↓
[Walk-Forward Split] (already have in validation/walk_forward.py!)
    ↓
[Model Training]
    ↓
models/
    regime_predictor_v1.pkl
    entry_scorer_v1.pkl
    ↓
[Validation Report]
    validation_report_v1.json
```

**Key Files to Create:**
- `src/ml/feature_extractor.py` - Transform logs to features
- `src/ml/training_pipeline.py` - Walk-forward training
- `src/ml/model_registry.py` - Version and load models
- `src/ml/inference.py` - Real-time prediction wrapper

### Phase 3: Integration (Week 5-6)

**Inference Architecture:**
```
[Market Data Tick]
    ↓
[Feature Calculator] (existing indicators.py)
    ↓
[Feature Vector]
    ↓
[ML Inference Engine]
    ├── regime_predictor.predict() → P(TREND), P(RANGE), ...
    ├── entry_scorer.predict() → P(win)
    └── exit_optimizer.predict() → recommended_action
    ↓
[Signal Engine Decision] (signal_engine.py)
```

**Latency Budget:**
| Component | Target | Notes |
|-----------|--------|-------|
| Feature extraction | <5ms | Use cached indicators |
| Model inference | <2ms | XGBoost/LightGBM native |
| Total ML overhead | <10ms | Well within 1m candle |

### Phase 4: Model Monitoring (Ongoing)

**Drift Detection:**
```python
class ModelMonitor:
    """Track model performance and detect drift."""
    
    def __init__(self, model_name: str, baseline_auc: float):
        self.predictions: List[float] = []
        self.actuals: List[int] = []
        self.baseline_auc = baseline_auc
        
    def log_prediction(self, pred: float, actual: int):
        self.predictions.append(pred)
        self.actuals.append(actual)
        
        # Check rolling AUC every 50 trades
        if len(self.predictions) % 50 == 0:
            rolling_auc = roc_auc_score(self.actuals[-200:], self.predictions[-200:])
            if rolling_auc < self.baseline_auc * 0.9:
                logger.warning(f"🚨 MODEL DRIFT: {model_name} AUC dropped to {rolling_auc:.3f}")
```

**Retraining Triggers:**
- Performance degradation >10% vs baseline
- Regime distribution shift
- Manual monthly retraining as baseline

---

## 5. Code Integration Points

### 5.1 Files Requiring Modification

| File | Modification | Status |
|------|-------------|--------|
| `requirements.txt` | Add `scikit-learn`, `lightgbm` | ✅ DONE |
| `src/engine/trade_logger.py` | Add `feature_snapshot` field | ✅ DONE |
| `src/engine/signal_engine.py` | Call ML inference before entry | ✅ DONE |
| `src/engine/regime/regime_detector.py` | Replace or augment with ML | 🔄 Future |
| `src/engine/scoring/entry_scorer.py` | Use ML probability | 🔄 Future (HYBRID mode) |
| `pyproject.toml` | Add ML deps to project config | ✅ DONE |

### 5.2 New Files Created ✅

```
src/ml/
├── __init__.py              ✅ Created
├── config.py                ✅ Created (MLPhase, EntryMLConfig)
├── feature_extractor.py     ✅ Created (24 features)
├── models/
│   ├── __init__.py          ✅ Created
│   ├── model_base.py        ✅ Created (BaseModel interface)
│   └── entry_scorer_ml.py   ✅ Created (LightGBM wrapper)
├── inference/
│   ├── __init__.py          ✅ Created
│   └── inference_engine.py  ✅ Created (MLInferenceEngine)
├── training/
│   ├── __init__.py          ✅ Created
│   ├── data_loader.py       ✅ Created (TradeDataLoader)
│   └── train_entry_model.py ✅ Created (training pipeline)
├── monitoring/
│   ├── __init__.py          ✅ Created
│   └── drift_detector.py    ✅ Created (MLDriftDetector)
└── serialized/
    └── .gitkeep             ✅ Created (for model files)

tools/
└── ml_status.py             ✅ Created (status utility)
```

### 5.3 Integration Example ✅ IMPLEMENTED

**Signal Engine Integration (`signal_engine.py`):**

```python
# ANTES (sin ML)
async def _evaluate_entry(self, df_1h, df_5m, df_1m, bias, price, market_time):
    score, breakdown, blocked = self._entry_scorer.calculate_entry_score(...)
    if score >= self._entry_threshold:
        return True
    return False

# AHORA (con ML SHADOW)
async def _evaluate_entry(self, df_1h, df_5m, df_1m, bias, price, market_time):
    # 1. Scoring rule-based (sigue decidiendo)
    score, breakdown, blocked = self._entry_scorer.calculate_entry_score(...)
    
    # 2. ML SHADOW - Predice y loguea, NO afecta decisión
    try:
        feature_snapshot = self._build_ml_feature_snapshot()
        ml_adjusted_score, ml_prob, ml_reason = self._ml_inference.get_entry_adjustment(
            rule_score=int(score),
            feature_snapshot=feature_snapshot
        )
        # Log para análisis posterior
        self._current_trade_tags['ml_win_prob'] = round(ml_prob, 4)
        self._current_trade_tags['ml_adjusted_score'] = ml_adjusted_score
        self._current_trade_tags['ml_reason'] = ml_reason
        self._current_trade_tags['ml_phase'] = self._ml_inference.phase.name
    except Exception as e:
        logger.warning(f"⚠️ ML inference error (non-blocking): {e}")
    
    # 3. Decisión sigue siendo rule-based (SHADOW mode)
    if score >= self._entry_threshold:
        return True
    return False

# FUTURO (HYBRID MODE - cuando tengamos 300+ trades)
# La decisión combinará rule-based + ML:
# - Si ML confidence > 0.65 y score >= threshold-1 → boost +1
# - Si ML confidence < 0.35 y score <= threshold+1 → penalty -1
```

### 5.4 Cómo Avanzar de Fase

```python
# En MLInferenceEngine
def check_phase_advancement(self) -> Optional[MLPhase]:
    """Auto-check if ready to advance phase."""
    n_trades = self._count_logged_trades()
    
    if self.phase == MLPhase.SHADOW:
        if n_trades >= 300:
            auc = self._validate_model_oos()
            if auc >= 0.55:
                return MLPhase.HYBRID  # Ready to advance!
    
    elif self.phase == MLPhase.HYBRID:
        if n_trades >= 1000:
            sharpe_degradation = self._check_sharpe_consistency()
            if sharpe_degradation < 0.25:
                return MLPhase.PRIMARY  # Ready for primary!
    
    return None  # Stay in current phase
```

---

## 6. Data Requirements & Timeline

### 6.1 Minimum Data for Each Model

| Model | Minimum Trades | Recommended | Current Status |
|-------|---------------|-------------|----------------|
| Entry Scoring | 300 | 1,000+ | **~11 trades** (SHADOW activo) |
| Regime Prediction | 500 | 2,000+ | Pendiente (tras Entry) |
| Exit Timing | 500 | 2,000+ | Pendiente |
| Position Sizing | 200 | 500+ | Pendiente |

### 6.2 Data Collection Timeline (Actualizado)

| Fase | Estado | Trades | Próximo Paso |
|------|--------|--------|--------------|
| **Actual** | SHADOW | ~11 | Acumular datos |
| +2 semanas | SHADOW | ~100 | Seguir acumulando |
| +6 semanas | SHADOW | ~300 | **Entrenar modelo** |
| +8 semanas | HYBRID | 300+ | Validar en producción |
| +3 meses | HYBRID | ~600 | Evaluar para PRIMARY |
| +6 meses | PRIMARY? | ~1200 | Full ML (si métricas ok) |

### 6.3 Comandos Útiles

```bash
# Ver estado actual del sistema ML
python tools/ml_status.py

# Verificar readiness para entrenamiento
python -c "from src.ml.training import check_training_readiness; print(check_training_readiness())"

# Entrenar modelo (cuando tengamos 300+ trades)
python -c "from src.ml.training import train_entry_model; train_entry_model()"
```

**Recommendation:** El sistema está listo para SHADOW mode. Seguir operando normalmente mientras se acumulan datos. Entrenar el primer modelo después de 300 trades con full feature snapshots.

---

## 7. Risk Assessment

### 7.1 ML Risks in Trading

| Risk | Mitigation | Estado |
|------|-----------|--------|
| Overfitting | Walk-forward validation | ✅ Configurado |
| Data leakage | Strict temporal splits, no shuffle | ✅ Implementado |
| Regime shift | Continuous monitoring + drift detection | ✅ DriftDetector listo |
| Model failure | Fallback to rule-based system | ✅ try/except en signal_engine |
| Latency spike | Pre-load models, cache features | ✅ Modelo cargado al inicio |

### 7.2 Recommended Safeguards (IMPLEMENTADOS ✅)

1. **Rule-Based Fallback Activo**
   - SHADOW mode: ML no afecta decisiones
   - Errores de ML son non-blocking (try/except)
   - Si ML falla, sistema continúa con reglas

2. **Rollout Gradual**
   - ✅ Fase 1 (ACTUAL): ML signals logged but not acted on (shadow mode)
   - 🔄 Fase 2: ML weighted 30% in decision (HYBRID mode)
   - 🔄 Fase 3: ML weighted 70% after validation
   - 🔄 Fase 4: Full ML with rule-based guardrails

3. **Circuit Breakers**
   - Drift detection si AUC cae >10%
   - Desactivación automática si performance degrada
   - Daily reconciliation de predicciones vs outcomes

---

## 8. Conclusion & Next Steps

### ✅ COMPLETADO (Enero 24, 2026)

| Item | Estado |
|------|--------|
| Análisis de oportunidades ML | ✅ |
| Estructura `src/ml/` | ✅ 14 archivos |
| LightGBM Entry Scorer | ✅ |
| Feature extractor (24 features) | ✅ |
| Inference engine con fases | ✅ |
| Integración en signal_engine | ✅ |
| SHADOW mode activo | ✅ |
| Drift detector | ✅ |
| Utility script `ml_status.py` | ✅ |

### Immediate Actions (This Week)
1. ✅ **COMPLETADO** - `feature_snapshot` añadido a `TradeLogRecord`
2. ✅ **COMPLETADO** - MFE/MAE candle timing en exit logs
3. ✅ **COMPLETADO** - Regime tracking (entry/exit regime, regime_changed)
4. ✅ **COMPLETADO** - ML dependencies en requirements.txt
5. ✅ **COMPLETADO** - Estructura `src/ml/` completa
6. ✅ **COMPLETADO** - SHADOW MODE activo

### Short-term (En Progreso)
1. ✅ ML dependencies añadidas (scikit-learn, lightgbm)
2. ✅ Módulo `src/ml/` creado con 14 archivos
3. ✅ Feature extraction pipeline operacional
4. ✅ Walk-forward infrastructure lista para ML
5. 🔄 **Acumulando datos** - Objetivo: 300 trades

### Medium-term (Mes 2-3)
1. Alcanzar 300 trades con feature snapshots
2. Entrenar primer modelo con `train_entry_model()`
3. Validar AUC > 0.55 fuera de muestra
4. Activar HYBRID mode si métricas pasan

### Long-term (Mes 4+)
1. Implementar clasificador de régimen
2. Modelo de exit timing
3. Full ML integration con monitoring
4. SHAP explanations para interpretabilidad

---

## Appendix A: Feature Engineering Checklist

```
PRICE FEATURES
✅ ATR (ratio vs baseline)
✅ ADX (1h, 5m, effective)
✅ RSI (14-period)
✅ Bollinger Band width + squeeze
□ EMA crossovers - pendiente
□ VWAP distance - pendiente

STRUCTURE FEATURES
✅ Score components (structure, timing, momentum, context, synergy)
□ BOS detected - ya capturado en score
□ HL/LH structure - ya capturado en score

CONTEXT FEATURES
✅ Session (one-hot: good/poor)
✅ Percentile 60d
✅ Location headroom (ATR)
✅ Location in extreme
✅ Funding rate
✅ Consecutive losses
✅ Trading mode (trend/range/transition)
□ Day of week - pendiente
□ Hour of day - pendiente

VOLUME FEATURES
✅ Volume ratio
✅ CVD bullish signal

TARGET VARIABLES
✅ Binary: pnl_r > 0 (IMPLEMENTADO)
□ Regression: pnl_r - futuro
□ Classification: regime_in_N_candles - futuro
□ Regression: candles_to_mfe - futuro
```

---

## Appendix B: References

- **Walk-Forward Validation:** Already implemented in `src/validation/walk_forward.py`
- **Trade Logging:** `src/engine/trade_logger.py`
- **Indicators:** `src/engine/indicators.py`
- **Regime Detection:** `src/engine/regime/regime_detector.py`
- **Entry Scoring:** `src/engine/scoring/entry_scorer.py`
- **Location Filter:** `src/engine/filters/location_filter.py`
- **ML Module:** `src/ml/` (nuevo, 14 archivos)
- **ML Status Utility:** `tools/ml_status.py`

---

## Appendix C: Arquitectura ML Resumida (IMPLEMENTADA)

### Pipeline Actual (SHADOW MODE)

```
┌─────────────────────────────────────────────────────────────────┐
│                     DATOS DE ENTRADA                            │
├─────────────────────────────────────────────────────────────────┤
│  • Precio (OHLCV)                                               │
│  • Volumen                                                      │
│  • Volatilidad (ATR, BB)                                        │
│  • Funding rate                                                 │
│  • Open Interest (pendiente)                                    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              FEATURE ENGINEERING (24 features)                  │
├─────────────────────────────────────────────────────────────────┤
│  extract_features_live() en src/ml/feature_extractor.py         │
│  • Normalización ATR                                            │
│  • One-hot encoding (session, regime)                           │
│  • Defaults seguros para valores faltantes                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   LightGBM (Clasificación)                      │
├─────────────────────────────────────────────────────────────────┤
│  EntryScoreML en src/ml/models/entry_scorer_ml.py               │
│  • Objetivo: binary (win/loss)                                  │
│  • Output: P(win) ∈ [0, 1]                                      │
│  • Hiperparams conservadores para datasets pequeños             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    SHAP (Explicación)                           │
├─────────────────────────────────────────────────────────────────┤
│  (Pendiente de activar tras entrenamiento)                      │
│  • Feature importance                                           │
│  • Explicación por trade                                        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              DECISIÓN DE TRADING                                │
├─────────────────────────────────────────────────────────────────┤
│  SHADOW MODE (actual):                                          │
│  • Rule-based score → DECIDE                                    │
│  • ML score → LOGUEA (no afecta)                               │
│                                                                 │
│  HYBRID MODE (futuro, 300+ trades):                            │
│  • Rule-based score + ML adjustment ±1                          │
│  • Decisión combinada                                           │
│                                                                 │
│  PRIMARY MODE (futuro, 1000+ trades):                          │
│  • ML probability → DECIDE                                      │
│  • Rule-based → guardrail                                       │
└─────────────────────────────────────────────────────────────────┘
```

### 🎯 Modelos Concretos

| Modelo | Framework | Target | Estado |
|--------|-----------|--------|--------|
| **Entry Scorer** | LGBMClassifier | `1=TP`, `0=SL` | ✅ SHADOW |
| **Regime Classifier** | LGBMClassifier | `TREND/RANGE/CHOPPY/ALTA_VOL` | 🔄 Pendiente |
| **Exit Timer** | LGBMRegressor | `candles_to_optimal_exit` | 🔄 Pendiente |

### ⚠️ Reglas de Oro

1. **Separar por ventanas temporales** - Walk-forward validation
2. **Nada de shuffle** - Orden temporal preservado  
3. **LightGBM + pocas features > Red neuronal gigante** - Para datasets pequeños
4. **SHADOW antes de HYBRID** - Validar sin riesgo
5. **Fallback siempre activo** - Rule-based como safety net
