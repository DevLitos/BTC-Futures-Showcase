# PULLBACK SHORT ENGINE - IMPLEMENTATION TRACKER

## Status: ✅ IMPLEMENTATION COMPLETE (v2 - Final Spec)

**Start Date:** January 14, 2026  
**Completion Date:** January 14, 2026  
**Last Update:** January 14, 2026 - Updated to final specification

---

## 📋 OVERVIEW

This document tracks the implementation of the "Pullback Short Engine" - a tactical short system that allows capturing corrections (e.g., 97k → 92k) within bullish cycles WITHOUT making the bot trend-agnostic.

### Core Principle
> **The bot remains "primarily LONG" when HTF says LONG. We introduce a SECOND type of short (SHORT_PULLBACK) that is tactical, NOT trend-reversal.**

---

## 🎯 IMPLEMENTATION CHECKLIST

### Phase 1: Configuration & Feature Flags ✅ COMPLETE
- [x] 1.1 Add `PullbackShortConfig` to `engine/config.py`
- [x] 1.2 Add feature flags to `core/settings.py`
- [x] 1.3 Add `TradeType` enum (LONG_MAIN, SHORT_STRUCTURAL, SHORT_PULLBACK)

### Phase 2: Indicators & Data ✅ COMPLETE
- [x] 2.1 Add `calculate_extension_z()` to `engine/indicators.py`
- [x] 2.2 Add `detect_swing_high()` to `engine/indicators.py`
- [x] 2.3 Add `detect_breakout_impulse()` to `engine/indicators.py`
- [x] 2.4 Add `calculate_activity_score()` to `engine/indicators.py`

### Phase 3: Pullback Short Engine Module ✅ COMPLETE
- [x] 3.1 Create `engine/pullback_short_engine.py`
- [x] 3.2 Implement extension detection (Gate 1)
- [x] 3.3 Implement failure/trigger detection (Gate 2)
- [x] 3.4 Implement breakout exclusion (Gate 3)
- [x] 3.5 Implement entry logic with `evaluate()` method

### Phase 4: Risk Management Integration ✅ COMPLETE
- [x] 4.1 Add pullback-specific sizing (`SIZE_FACTOR = 0.30`)
- [x] 4.2 Add pullback-specific SL/TP logic
- [x] 4.3 Add adaptive TP based on activity score

### Phase 5: Signal Engine Integration ✅ COMPLETE
- [x] 5.1 Add evaluation pipeline for SHORT_PULLBACK in `signal_engine.py`
- [x] 5.2 Add priority logic (LONG vs SHORT_PULLBACK when location blocks)
- [x] 5.3 Add trade_type tracking and tags
- [x] 5.4 Add `_evaluate_pullback_short()` method
- [x] 5.5 Add `_execute_pullback_short_entry()` method

### Phase 6: Guardrails & Safety ✅ COMPLETE
- [x] 6.1 Add daily pullback short limits (via `MAX_TRADES_PER_DAY`)
- [x] 6.2 Add consecutive loss protection (via `MAX_CONSECUTIVE_LOSSES`)
- [x] 6.3 Add integrity error disable (via `DISABLE_ON_INTEGRITY_ERROR`)

### Phase 7: Observability ✅ COMPLETE
- [x] 7.1 Add decision logging in engine
- [x] 7.2 Add Prometheus metrics in `monitoring/metrics.py`
- [x] 7.3 Integrate metrics calls in `signal_engine.py`

### Phase 8: Testing ✅ COMPLETE
- [x] 8.1 Create `tests/test_pullback_short_engine.py`
- [x] 8.2 All 26 tests passing

### Phase 9: Position Integration ✅ COMPLETE
- [x] 9.1 Add `trade_type` field to `Position` class
- [x] 9.2 Update `create_position()` to accept trade_type
- [x] 9.3 Pass trade_type in all create_position calls

---

## 📝 DETAILED SPECIFICATIONS

### 1. DESIGN PRINCIPLES (NON-NEGOTIABLE)

1. **HTF structural filter is NOT touched** as base of system
2. **Bot remains "primarily LONG"** when HTF says LONG
3. **Two explicit short types:**
   - `SHORT_STRUCTURAL`: Only when HTF is bearish (exists or equivalent)
   - `SHORT_PULLBACK`: Tactical short allowed even with HTF bias LONG (NEW)
4. **Risk and exits are DIFFERENT** between types
5. **No hedging**: Cannot be long AND short simultaneously on same symbol
6. **Feature flags + kill switch** for everything new

### 2. PULLBACK SHORT ENGINE DEFINITION

#### 2.1 What It Is
A signal subsystem that allows opening a tactical short when:
- HTF is LONG (bullish market)
- Price is excessively extended
- Failure confirmation appears (structure/price action)
- No real breakout conditions present

#### 2.2 What It Is NOT
- NOT a "short trend reversal"
- NOT indiscriminate mean-reversion
- Does NOT average losses
- Does NOT seek huge targets by default

### 3. REQUIRED INDICATORS

| Indicator | Timeframe | Status | Notes |
|-----------|-----------|--------|-------|
| EMA50 | 1H | ✅ EXISTS | Used for HTF bias |
| ATR | 1H | ⏳ ADD | For extension_z calculation |
| ATR | 5M | ✅ EXISTS | For stops and triggers |
| VWAP | 5M | ✅ EXISTS | For TP1 target |
| RSI | 5M | ✅ EXISTS | Complementary filter |
| extension_z | DERIVED | ⏳ ADD | `(price - EMA50_1h) / ATR_1h` |
| breakout_impulse_flag | 5M | ⏳ ADD | Body/volume/close analysis |
| swing_high_confirmed | 5M | ⏳ ADD | Local structure |

### 4. GATE SYSTEM

#### Gate 1: EXTENSION DETECTION (Required)

```python
extension_z = (current_price - EMA50_1h) / ATR_1h

# Thresholds by regime
if regime == TREND:
    EXTENSION_Z_MIN = 2.3  # Harder in trends
else:  # RANGE
    EXTENSION_Z_MIN = 1.8  # Easier in ranges
```

**Complementary filters (2 of 3):**
- RSI_5m >= 72
- price_distance_to_VWAP >= 1.5 * ATR_5m
- volatility_ratio >= 1.2

#### Gate 2: FAILURE/TRIGGER (Required)

**Swing High Definition:**
```python
# Swing high = local high superior to N candles left and right
SWING_LOOKBACK = 3  # In 5m candles

# Or use existing BOS engine for HH/LH detection
```

**Failure Signal (choose 1):**
- **Primary**: BOS_down=True in 1m/5m after swing high
- **Alternative**: Close below low of impulse candle

#### Gate 3: BREAKOUT EXCLUSION (Veto)

```python
# Breakout detection on 5m
body = abs(close - open)
range = high - low
body_ratio = body / range

impulse = body > 0.6 * ATR_5m
close_near_high = (high - close) < 0.2 * range
volume_expansion = volume > 1.5 * SMA(volume, 20)

breakout_impulse_flag = impulse and close_near_high and volume_expansion

if breakout_impulse_flag:
    # PROHIBIT SHORT_PULLBACK for N candles
    BREAKOUT_COOLDOWN_CANDLES = 4  # 5m candles
```

### 5. ENTRY RULES

#### Order Type
- High velocity → MARKET (with slippage limits)
- Normal → LIMIT (post-only) on retest

#### Entry Placement
```python
# After trigger fires
retest_level = EMA20_5m  # or VWAP_5m or previous breakdown level
ENTRY_TIMEOUT_CANDLES = 3  # 5m candles, cancel if not filled
```

### 6. RISK MANAGEMENT (SHORT_PULLBACK SPECIFIC)

#### 6.1 Sizing
```python
SIZE_FACTOR_PULLBACK = 0.30  # 30% of normal size
final_qty = base_qty * SIZE_FACTOR_PULLBACK
final_qty = min(final_qty, max_pullback_notional / price)
```

#### 6.2 Stop Loss
```python
# SL above confirmed swing high + buffer
buffer = max(0.15 * ATR_5m, MIN_TICKS)
SL = swing_high + buffer

# IMPORTANT: Tight stop, not "ATR wide stop" - this is tactical
```

#### 6.3 Take Profit

**TP1 (Obligatory - Quick Cash):**
```python
TP1_target = VWAP_5m  # or EMA50_5m

# If distance too small (fees):
TP1 = entry - k_low * ATR_5m  # k_low = 0.5-0.9
```

**TP2 (Optional Runner):**
```python
TP2 = entry - k_high * ATR_5m  # k_high = 1.5-3.0
# or TP2 = EMA50_1h / midrange 20D
```

**Adaptive k based on activity:**
```python
k_tp = k_low + activity_score * (k_high - k_low)
# Dead market → k_tp closer (more fill probability)
# High vol → k_tp farther (let it run)
```

#### 6.4 Time Stop
```python
TIME_STOP_CANDLES = 6  # 5m candles = 30 min
# If no minimum MFE after N candles → exit
```

### 7. REGIME INTEGRATION

| Regime | ext_z Required | TP Behavior | Notes |
|--------|---------------|-------------|-------|
| TREND | >= 2.3 | k_tp = 0.9-2.0 | Harder to enter, bigger targets |
| RANGE | >= 1.8 | k_tp = 0.5-1.5 | Easier to enter, quicker exits |

### 8. PIPELINE INTEGRATION

**Evaluation Order:**
1. If position open → MANAGE only
2. If orders open → don't open new
3. Evaluate main setup (LONG per pipeline)
4. Evaluate SHORT_PULLBACK only if:
   - No active long signal, OR
   - Long blocked by location/anchor/chase

**Priority Rule:**
> If HTF bias LONG, the LONG has priority UNLESS blocked by location/anchor. Then allow pullback short if extension + failure confirmed.

### 9. TRADE TAGGING

```python
trade_type: Literal["LONG_MAIN", "SHORT_STRUCTURAL", "SHORT_PULLBACK"]
entry_reason: str  # "extension_fail", "trend_break", etc.

# Critical for post-analysis
```

### 10. LOGGING FORMAT

```
[SHORT_PULLBACK] Gate1: ext_z=2.1 (min=1.8) ✓
[SHORT_PULLBACK] Gate2: swing_high=97250, trigger=BOS_DOWN ✓
[SHORT_PULLBACK] Gate3: breakout_impulse=False ✓
[SHORT_PULLBACK] SIGNAL VALID → Entry at 96800, SL=97350, TP1=96200
```

### 11. PROMETHEUS METRICS

```python
short_pullback_signals_total
short_pullback_entries_total
short_pullback_blocks_total{reason="breakout_cooldown|ext_z_low|..."}
short_pullback_pnl_total
short_pullback_mfe_r_histogram
short_pullback_mae_r_histogram
```

### 12. SAFETY GUARDRAILS

```python
MAX_SHORT_PULLBACK_TRADES_PER_DAY = 5
MAX_CONSECUTIVE_PULLBACK_LOSSES = 2
DISABLE_ON_NEWS = True  # If macro monitor detects high-impact event
DISABLE_ON_INTEGRITY_ERROR = True  # If position/order desync
```

---

## 📊 FINAL PARAMETERS (Updated to Spec v2)

```python
# Feature Flags
ENABLE_SHORT_PULLBACK = False  # Master switch (default off)
SHORT_PULLBACK_LIVE_ALLOWED = False  # Start in paper

# Gate B: Extension
EXT_Z_MIN = 1.8  # Minimum for RANGE regime
EXT_Z_MIN_TREND = 2.3  # Stricter for TREND regime

# Gate C: Anti-Breakout (veto)
BREAKOUT_BODY_ATR_RATIO = 0.6
BREAKOUT_CLOSE_RATIO = 0.8  # close >= low + 0.8*(high-low)
BREAKOUT_COOLDOWN_5M = 4  # 20 min cooldown

# Swing High Detection
SWING_N = 3  # Candles left/right for swing detection
SWING_VALIDITY_5M = 12  # Swing expires after 12 candles (1 hour)

# Trigger: Close below confirm_level
# confirm_level = low of swing high candle

# Entry Mode
ENTRY_MODE = "RETEST"  # LIMIT order at VWAP/EMA20
ENTRY_TIMEOUT_5M = 2  # Cancel if not filled in 10 min
RETEST_LEVEL = "VWAP"

# Risk
SIZE_FACTOR = 0.30  # 30% of normal size
SL_BUFFER_ATR_5M = 0.15  # buffer = max(MIN_TICKS, 0.15*ATR)
SL_MIN_TICKS_BUFFER = 5

# TP1: Fixed at VWAP
TP1_TARGET = "VWAP"
TP1_CLOSE_PCT = 50  # Close 50% at TP1
TP1_MIN_PROFIT_FLOOR_ATR = 0.2

# TP2: Dynamic ATR
TP2_K_LOW = 1.2  # k when market dead
TP2_K_HIGH = 2.8  # k when market active
# k_tp2 = k_low + activity * (k_high - k_low)

# BE After TP1
BE_AFTER_TP1 = True
BE_OFFSET_ATR = 0.1  # BE = entry - 0.1*ATR

# Time Stop
TIME_STOP_5M = 6  # 30 min
TIME_STOP_MIN_MFE_R = 0.3  # Exit if MFE < 0.3R after time

# Safety
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_LOSSES = 2
```

---

## 🔧 IMPLEMENTATION LOG

### [2026-01-14] Phase 1: Configuration & Feature Flags ✅ COMPLETE

#### 1.1 TradeType Enum in config.py
**Status:** ✅ DONE

**Changes in `engine/config.py`:**
```python
class TradeType(str, Enum):
    LONG_MAIN = "LONG_MAIN"
    SHORT_STRUCTURAL = "SHORT_STRUCTURAL"
    SHORT_PULLBACK = "SHORT_PULLBACK"
```

#### 1.2 PullbackShortConfig in config.py
**Status:** ✅ DONE

**Changes in `engine/config.py`:**
- Added `PullbackShortConfig` class with 50+ parameters covering all gates, risk management, and safety

#### 1.3 Feature Flags in settings.py
**Status:** ✅ DONE

**Changes in `core/settings.py`:**
```python
enable_short_pullback: bool = Field(default=False, description="Master switch for pullback short engine")
short_pullback_live_allowed: bool = Field(default=False, description="Allow pullback shorts in live trading")
short_pullback_max_daily: int = Field(default=5, description="Maximum pullback short trades per day")
```

---

### [2026-01-14] Phase 2: Indicators & Data ✅ COMPLETE

#### 2.1 calculate_extension_z() in indicators.py
**Status:** ✅ DONE

**Function:** Calculates Z-score of price extension above EMA
```python
def calculate_extension_z(current_price: float, ema_values: np.ndarray, atr_values: np.ndarray) -> float
```

#### 2.2 detect_swing_high() in indicators.py
**Status:** ✅ DONE

**Function:** Detects local swing highs for stop placement
```python
def detect_swing_high(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, lookback: int = 3) -> tuple[bool, float]
```

#### 2.3 detect_swing_low() in indicators.py
**Status:** ✅ DONE

**Function:** Complementary swing low detection
```python
def detect_swing_low(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, lookback: int = 3) -> tuple[bool, float]
```

#### 2.4 detect_breakout_impulse() in indicators.py
**Status:** ✅ DONE

**Function:** Veto gate - detects strong breakout candles
```python
def detect_breakout_impulse(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, 
                            volumes: np.ndarray, atr: float, direction: str = "up") -> bool
```

#### 2.5 calculate_activity_score() in indicators.py
**Status:** ✅ DONE

**Function:** For adaptive TP targeting based on market activity
```python
def calculate_activity_score(volumes: np.ndarray, closes: np.ndarray, 
                             base_periods: int = 20, recent_periods: int = 5) -> float
```

---

### [2026-01-14] Phase 3: Pullback Short Engine Module ✅ COMPLETE

#### 3.1 Created engine/pullback_short_engine.py
**Status:** ✅ DONE (~600 lines)

**Key Components:**
- `PullbackShortSignal` dataclass with entry/SL/TP/size_factor/trade_type
- `GateResult` dataclass for per-gate evaluation results
- `PullbackShortEngine` class with singleton pattern

**Main Methods:**
- `evaluate()`: Runs all 3 gates and returns signal if valid
- `_evaluate_extension_gate()`: Gate 1 - Extension detection
- `_evaluate_trigger_gate()`: Gate 2 - Failure/trigger confirmation
- `_evaluate_breakout_veto()`: Gate 3 - Breakout exclusion
- `_check_daily_limits()`: Safety guardrail
- `_is_in_breakout_cooldown()`: Cooldown after breakout detection
- `record_result()`: Tracks trade outcomes for consecutive loss protection

---

### [2026-01-14] Phase 4: Risk Management Integration ✅ COMPLETE

#### 4.1 Pullback-Specific Sizing
**Status:** ✅ DONE

**Implementation:**
- `SIZE_FACTOR = 0.30` (30% of normal size)
- Applied in `PullbackShortSignal.size_factor`

#### 4.2 Pullback-Specific SL/TP Logic
**Status:** ✅ DONE

**Implementation:**
- SL: `swing_high + BUFFER_SL_ATR_5M * ATR` (tight tactical stop)
- TP1: `entry - k_tp * ATR` where k_tp is adaptive based on activity score

#### 4.3 Adaptive TP Based on Activity Score
**Status:** ✅ DONE

**Implementation:**
- `k_tp = k_low + activity_score * (k_high - k_low)`
- Dead market → smaller k (quicker exits)
- Active market → larger k (let it run)

---

### [2026-01-14] Phase 5: Signal Engine Integration ✅ COMPLETE

#### 5.1 Added Imports and Instance
**Status:** ✅ DONE

**Changes in `engine/signal_engine.py`:**
```python
from engine.pullback_short_engine import PullbackShortEngine, PullbackShortSignal
from engine.config import TradeType, PullbackShortConfig
```

#### 5.2 Added Evaluation in Location Veto Section
**Status:** ✅ DONE

**Logic:**
When LONG is blocked by location/anchor/chase → evaluate pullback short as alternative

#### 5.3 Added _evaluate_pullback_short() Method
**Status:** ✅ DONE

**Function:** Gathers all required data and calls engine.evaluate()

#### 5.4 Added _execute_pullback_short_entry() Method
**Status:** ✅ DONE

**Function:** Executes the pullback short entry with proper tagging and tracking

#### 5.5 Added Trade Type Tracking
**Status:** ✅ DONE

**Implementation:**
- `self._current_trade_type: Optional[TradeType]` tracks active trade type
- Trade close section records pullback results if applicable

---

### [2026-01-14] Phase 6: Guardrails & Safety ✅ COMPLETE

#### 6.1 Daily Pullback Short Limits
**Status:** ✅ DONE

**Implementation:**
- `MAX_TRADES_PER_DAY = 5` in PullbackShortConfig
- Checked in `_check_daily_limits()` method

#### 6.2 Consecutive Loss Protection
**Status:** ✅ DONE

**Implementation:**
- `MAX_CONSECUTIVE_LOSSES = 2` in PullbackShortConfig
- Tracked via `record_result()` method and `_pullback_results_today` list

#### 6.3 Integrity Error Disable
**Status:** ✅ DONE

**Implementation:**
- `DISABLE_ON_INTEGRITY_ERROR = True` in PullbackShortConfig
- Checked before evaluation

---

### [2026-01-14] Phase 7: Observability ✅ COMPLETE

#### 7.1 Decision Logging in Engine
**Status:** ✅ DONE

**Implementation:**
- Detailed logging at each gate: `[SHORT_PULLBACK] Gate1: ext_z=X.X (min=Y.Y) ✓/✗`
- Signal valid/invalid summary logs

#### 7.2 Prometheus Metrics in monitoring/metrics.py
**Status:** ✅ DONE

**Metrics Added:**
```python
# Counters
short_pullback_signals_total = Counter(...)
short_pullback_entries_total = Counter(...)
short_pullback_blocks_total = Counter(..., labelnames=['reason'])
short_pullback_exits_total = Counter(..., labelnames=['reason'])

# Gauges
short_pullback_active = Gauge(...)
short_pullback_daily_count = Gauge(...)
short_pullback_consecutive_losses = Gauge(...)

# Histograms
short_pullback_pnl_histogram = Histogram(...)
short_pullback_mfe_r_histogram = Histogram(...)
short_pullback_mae_r_histogram = Histogram(...)
short_pullback_extension_z_histogram = Histogram(...)

# Helper Functions
update_pullback_short_signal()
update_pullback_short_entry()
update_pullback_short_exit()
```

---

### [2026-01-14] Phase 8: Testing ✅ COMPLETE

#### 8.1 Created tests/test_pullback_short_engine.py
**Status:** ✅ DONE (~400 lines)

#### 8.2 Test Results
**Status:** ✅ ALL 26 TESTS PASSING

**Test Categories:**
- Indicator tests (extension_z, swing_high, swing_low, breakout_impulse, activity_score)
- Engine tests (initialization, gate evaluation, daily limits, cooldown, consecutive losses)
- Config tests (default values, ranges)
- Integration tests (import verification)

```bash
$ python3 -m pytest tests/test_pullback_short_engine.py -v --tb=short
========================= 26 passed in 0.20s =========================
```

---

## 📁 FILES MODIFIED/CREATED

| File | Action | Lines Changed |
|------|--------|---------------|
| `engine/config.py` | MODIFIED | +150 lines (TradeType enum, PullbackShortConfig) |
| `core/settings.py` | MODIFIED | +3 fields |
| `engine/indicators.py` | MODIFIED | +250 lines (5 new functions) |
| `engine/pullback_short_engine.py` | CREATED | ~600 lines |
| `engine/signal_engine.py` | MODIFIED | +220 lines (pullback integration + metrics) |
| `engine/risk_engine.py` | MODIFIED | +10 lines (trade_type in Position) |
| `monitoring/metrics.py` | MODIFIED | +100 lines |
| `tests/test_pullback_short_engine.py` | CREATED | ~400 lines |
| `PULLBACK_SHORT_ENGINE_IMPLEMENTATION.md` | CREATED | This file |

---

## ✅ VERIFICATION COMPLETE

```bash
# Tests
$ python3 -m pytest tests/test_pullback_short_engine.py -v
# Result: 26 passed

# Import verification
$ python3 -c "from engine.signal_engine import SignalEngine; print('OK')"
# Result: OK

# Position with trade_type
$ python3 -c "from engine.risk_engine import Position; from engine.config import TradeType; ..."
# Result: Position created with trade_type=TradeType.SHORT_PULLBACK

# Metrics functions
$ python3 -c "from monitoring.metrics import update_pullback_short_exit; ..."
# Result: All metrics functions work correctly
```

---

## 🚀 NEXT STEPS (Optional Enhancements)

1. **Run Full Backtest** - Enable pullback shorts and run backtest to measure edge
2. **Paper Trading Test** - Run in paper mode with `short_pullback_live_allowed=False`
3. **Tune Parameters** - Adjust thresholds based on backtest results
4. **Add News Filter** - Integrate with external_data for high-impact event filtering

---

*Implementation completed: January 14, 2026*
*Last update: January 14, 2026 - Added Position.trade_type and metrics integration*
