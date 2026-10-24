# DEAD CODE AUDIT — Final Report

**Date:** 2026-02-18  
**Scope:** `src/` directory — live Python files only (excludes docs, tests, trade_logs)  
**Auditor:** Automated grep + read verification  

---

## CRITICAL CORRECTION: TREND mode is NOT dead

**Before removing any TREND-specific code, note:**  
Trade logs from Feb 7–17 contain `trend_mode` tags in production trades.  
`regime_handlers.py` L128-132 sets TREND when `regime_score ≥ 0.50`.  
**TREND code paths are ALIVE.** Do not delete them.

---

## Summary Table

| # | Item | File | Lines | Dead Lines | Confidence | Safe to Remove |
|---|------|------|-------|------------|------------|----------------|
| 1 | **entry_gate.py** (entire file) | execution_logic/entry_gate.py | 750 | ~750 | HIGH | YES |
| 2 | **filter_checks.py** (entire file) | execution_logic/filter_checks.py | 625 | ~625 | HIGH | YES |
| 3 | **regime_context.py** (entire file) | regime/regime_context.py | 594 | ~594 | HIGH | YES |
| 4 | `self._intelligent_filter` instance | trading_app.py L502 | — | ~682 (file) | HIGH | YES (instance+file) |
| 5 | `self._entry_gate` instance | trading_app.py L496 | — | ~336 (file) | HIGH | YES (instance+file) |
| 6 | `self._momentum_validator` instance | trading_app.py L1059 | — | ~40 | HIGH | YES |
| 7 | `HardEntryBlockValidator` class | entry_validators.py L970-1139 | — | ~170 | HIGH | YES |
| 8 | `ProbeValidator` class | entry_validators.py L1141-1306 | — | ~166 | HIGH | YES |
| 9 | `ProbingModeChecker` class | position_manager.py L398-525 | — | ~128 | HIGH | YES |
| 10 | Probe-mode block in risk_engine | risk_engine.py L2735-2762 | — | ~28 | HIGH | YES |
| 11 | `_log_probe_max_result()` | trade_recording.py L297-348 | — | ~52 | HIGH | YES |
| 12 | `probing_mode` branch in sizer | entry_validators.py L565-574 | — | ~10 | HIGH | YES |
| 13 | `anticipation.get_state()` | anticipation.py L475+ | — | ~20 | MEDIUM | YES |
| 14 | Dead imports (6 items) | trading_app.py, entry_orchestrator.py | — | 6 | HIGH | YES |
|   | **TOTAL DEAD LINES** | | | **~3,607** | | |

---

## Area 1 — TREND-specific branches

**Finding: NOT DEAD**

TREND mode is reachable via `regime_handlers.py` L128-132 when `regime_score >= 0.50`.  
Confirmed by trade logs: `trend_mode`, `trend_direction` tags in production events.  
**No TREND code should be deleted.**

---

## Area 2 — `probing_mode` / `is_probe` references

`probing_mode` is **never SET** anywhere in `src/`. Verified:

```
grep 'tags\[.probing_mode'     → 0 matches
grep 'probing_mode.*=.*["\']'  → 0 matches (except reads)
```

Every `probing_mode` **read** is dead because the value is always `False`/empty.

### 2a. `risk_engine.py` L2735-2762 — Probe mode block
- **What:** 28-line block: `is_probe = bool(trade_tags.get('probing_mode', False))` → always False → entire `if is_probe:` block (SL-only/TP-only check, skip trailing/partials/time) never executes
- **Verified:** `probing_mode` never SET → `is_probe` always `False`
- **Confidence:** HIGH

### 2b. `entry_validators.py` L565-574 — Probing sizing branch
- **What:** `probing_mode = trade_tags.get('probing_mode', False)` → `if probing_mode:` branch classifies into PROBE_MIN/MED/MAX → never enters
- **Verified:** `probing_mode` never SET
- **Confidence:** HIGH

### 2c. `position_manager.py` L398-525 — `ProbingModeChecker` class
- **What:** 128-line class with `check_probing_exit()` and `track_probe_max_sequence()`. Both methods return early at `if not trade_tags.get('probing_mode', False): return result`
- **Verified:** Still called from trading_app.py L7502/L7518 but executes zero meaningful logic
- **Confidence:** HIGH

### 2d. `trade_recording.py` L297-348 — `_log_probe_max_result()`
- **What:** 52-line method. Returns immediately at L300: `if probing_mode != 'PROBE_MAX': return`
- **Verified:** `probing_mode` never equals `'PROBE_MAX'`
- **Confidence:** HIGH

### 2e. `entry_validators.py` L1141-1306 — `ProbeValidator` class + factory
- **What:** Full class + `get_probe_validator()` factory. `validate_probe()` takes `is_probe` param but is never called from any src code
- **Verified:** `grep 'get_probe_validator'` → only 1 match (the definition)
- **Confidence:** HIGH

### 2f. `entry_gate.py` L80 — `is_probe` field on `EntryDecision`
- **What:** `is_probe: bool = False` set at L320 when `size_multiplier <= 0.3`, but the entire `EntryGateEvaluator` is dead (see Area 4)
- **Confidence:** HIGH (contained within dead file)

### 2g. `entry_quality.py` L300-327 — `get_sl_multiplier_for_regime()`
- **What:** Method with `is_probe` parameter. Defined but never called
- **Verified:** `grep 'get_sl_multiplier_for_regime'` → 1 match (definition only)
- **Confidence:** HIGH

---

## Area 3 — `anticipation.py` usage

### 3a. Module status: **ALIVE**
- `evaluate_all()` called from `idle_state_handlers.py` L443
- The file is actively used in production

### 3b. Dead method: `get_state()` (L475+)
- **What:** ~20-line method returning `AnticipationState` snapshot
- **Verified:** `grep 'anticipation.*get_state'` → 1 match (definition only). No external caller.
- **Confidence:** MEDIUM (could be useful for debugging/API but currently dead)

---

## Area 4 — Dead methods/classes inside live files

### 4a. **ENTIRE FILE: `entry_gate.py`** (750 lines) — HIGH
- **What:** `EntryGateEvaluator`, `EntryDecision`, `GateResult`, `GateType`, `GateCategory`, convenience functions
- **Verified:** 
  - `grep 'EntryGateEvaluator'` → only self-references + barrel `__init__.py` + docs
  - `grep 'get_entry_gate_evaluator|evaluate_entry_gates'` in `src/application/` → 0 matches
  - No application code imports or uses any symbol from this file
- **Note:** `__init__.py` barrel re-exports these symbols but nobody consumes them

### 4b. **ENTIRE FILE: `filter_checks.py`** (625 lines) — HIGH
- **What:** 14 filter functions (`check_trend_filter`, `check_momentum_filter`, `check_volatility_expansion`, `check_anti_reversal`, `check_volume_delta`, `check_loss_cooldown`, `check_atr_regime`, `check_expected_rr`, `check_bos_against_position` [deprecated], `check_atr_regime_allows_entry`, `check_volatility_minimum`, `check_not_post_impulse`, `check_sweep_occurred`, `get_filter_checks`)
- **Verified:** Every single function has only 1 match in code (its definition). No application code imports them.
  - `from...filter_checks import` → only barrel `__init__.py` and self-import
  - `check_bos_against_position` explicitly marked deprecated (L400) — replaced by `position_manager.check_bos_against_position`

### 4c. **`self._intelligent_filter` in `trading_app.py` L502** → makes `intelligent_filter.py` (682 lines) dead — HIGH
- **What:** `self._intelligent_filter = get_intelligent_filter()` constructed, never method-called
- **Verified:**
  - `grep '_intelligent_filter\.'` → 0 matches
  - `grep 'engine._intelligent_filter'` → 0 matches  
  - No application code calls any method on the instance or the singleton
- **Note:** Trade logs from Jan 26 contain `intelligent_filter_decision` tags, so it WAS used. Currently dead.

### 4d. **`self._entry_gate` in `trading_app.py` L496** → makes `entry_quality.py` (336 lines) dead — HIGH
- **What:** `self._entry_gate = get_entry_quality_gate()` constructed, never method-called
- **Verified:**
  - `grep '_entry_gate\.'` → 0 matches
  - `grep 'entry_quality.*\.check\('` → 0 matches
  - `EntryQualityGate.check()` never called externally
- **Latent bug:** L198 references `GateDecision.ALLOW_FULL` which doesn't exist in the `GateDecision` enum (would raise `AttributeError` if reached)

### 4e. **`self._momentum_validator` in `trading_app.py` L1059** — HIGH
- **What:** `self._momentum_validator = get_momentum_validator()` constructed, never method-called
- **Verified:**
  - `grep '_momentum_validator\.'` → 0 matches
  - Instance created but no method ever invoked

### 4f. **`HardEntryBlockValidator` class** in `entry_validators.py` L970-1139 (~170 lines) — HIGH
- **What:** Full class + `HardBlockResult` dataclass (L970) + factory `get_hard_entry_validator()` (L1299)
- **Verified:**
  - `grep 'HardEntryBlockValidator'` in src code → only definition + factory return
  - `grep 'get_hard_entry_validator'` → 1 match (definition only)
  - Never imported, never instantiated, never called

### 4g. **`get_minimum_rr_for_session()` in `entry_validators.py` L1117** — HIGH
- **What:** Class method on `HardEntryBlockValidator` (already dead class)
- **Verified:** `grep 'get_minimum_rr_for_session'` → 1 match (definition only)

---

## Area 5 — Stale TREND constants

**Finding: NOT DEAD**

TREND mode is reachable (see Area 1). All TREND-related constants in `unified_constants.py` are potentially used at runtime when `regime_score >= 0.50`.

---

## Dead Imports

| Import | File | Line |
|--------|------|------|
| `get_entry_quality_gate` | trading_app.py | 122 |
| `get_intelligent_filter` | trading_app.py | 128 |
| `FilterSeverity` | trading_app.py | 128 |
| `get_momentum_validator` | trading_app.py | 209 |
| `GateDecision` | entry_orchestrator.py | 74 |
| `check_bos_against_position` (duplicate) | trading_app.py | 191 (already imported as `_check_bos_unified` at L109) |

---

## Recommended Cleanup Order

**Phase 1 — Delete entire dead files (safest, highest ROI):**
1. Delete `entry_gate.py` (750 lines)
2. Delete `filter_checks.py` (625 lines)
3. Delete `regime_context.py` (594 lines)
4. Clean barrel `execution_logic/__init__.py` (remove dead re-exports)

**Phase 2 — Delete dead classes/methods inside live files:**
5. Remove `HardEntryBlockValidator` + `HardBlockResult` + `ProbeValidator` + `ProbeValidationResult` + factories from `entry_validators.py` (~340 lines)
6. Remove `ProbingModeChecker` from `position_manager.py` (~128 lines)
7. Remove `_log_probe_max_result()` from `trade_recording.py` (~52 lines)
8. Remove probe-mode block from `risk_engine.py` L2735-2762 (~28 lines)
9. Remove `probing_mode` branch from `entry_validators.py` L565-574 (~10 lines)

**Phase 3 — Dead instances and imports in `trading_app.py`:**
10. Remove `self._entry_gate`, `self._intelligent_filter`, `self._momentum_validator` assignments
11. Remove associated dead imports
12. Remove dead `GateDecision` import from `entry_orchestrator.py`

**Phase 4 — Consider removing entire files now without consumers:**
13. `entry_quality.py` (336 lines) — only consumer was `self._entry_gate` (dead)
14. `intelligent_filter.py` (682 lines) — only consumer was `self._intelligent_filter` (dead)

**Total removable:** ~3,600+ lines of dead code across 7 files (3 entire files + blocks in 4 others)
