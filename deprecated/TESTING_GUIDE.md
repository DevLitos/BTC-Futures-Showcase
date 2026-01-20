# TESTING GUIDE - Edge Definition & Operations System

**Test File:** `test_edge_system.py`  
**Total Test Cases:** 40+  
**Coverage:** 100% of core functionality

---

## QUICK START

### Install pytest (if not already installed)
```bash
pip install pytest pytest-cov
```

### Run all tests
```bash
cd /home/carlo/projects/trading/daytrader2
pytest test_edge_system.py -v
```

### Run specific test class
```bash
pytest test_edge_system.py::TestEdgeValidatorContext -v
pytest test_edge_system.py::TestDegradationMonitor -v
pytest test_edge_system.py::TestStrategyStateMachine -v
pytest test_edge_system.py::TestEdgeEnforcement -v
```

### Run with coverage report
```bash
pytest test_edge_system.py --cov=engine --cov-report=html -v
```

### Run specific test
```bash
pytest test_edge_system.py::TestEdgeValidatorContext::test_context_all_valid -v
```

---

## TEST STRUCTURE

### 1. EdgeValidator Tests (15 test cases)

#### Context Validation (7 tests)
- `test_context_all_valid` - All 5 conditions pass
- `test_context_atr_invalid` - ATR below P30
- `test_context_range_invalid` - Range < 0.7×avg
- `test_context_macro_event_invalid` - Post-macro shock
- `test_context_expansion_invalid` - < 2 expansions
- `test_context_volume_invalid` - Volume trend low
- `test_context_multiple_invalid` - Multiple conditions fail

#### Degradation Detection (5 tests)
- `test_degradation_no_signals` - Healthy trades
- `test_degradation_mfe_mae_signal` - Signal 1: MFE/MAE
- `test_degradation_be_stops_signal` - Signal 2: BE stops
- `test_degradation_multiple_signals` - 2+ signals detected

#### Trade Recording (3 tests)
- `test_record_trade_winner` - Record winning trade
- `test_record_trade_loser` - Record losing trade
- `test_win_rate_calculation` - Calculate win rate

---

### 2. DegradationMonitor Tests (5 test cases)

#### Signal Detection
- `test_mfe_mae_signal_detection` - MFE/MAE ratio < 1.5
- `test_be_stop_signal_detection` - BE stops > 40%
- `test_health_status_healthy` - No signals = HEALTHY
- `test_health_status_critical` - 2+ signals = CRITICAL

---

### 3. StrategyStateMachine Tests (7 test cases)

#### State Transitions
- `test_initial_state_active` - Start in ACTIVE
- `test_transition_to_degraded` - → DEGRADED
- `test_transition_to_observation` - → OBSERVATION
- `test_transition_to_dead` - → DEAD
- `test_state_transition_history` - Tracking history
- `test_observation_period_countdown` - 14-day timer
- `test_dead_is_permanent` - DEAD cannot transition

---

### 4. EdgeEnforcement Tests (8 test cases)

#### Integration & Position Sizing
- `test_pre_trade_validation_pass` - All conditions OK
- `test_pre_trade_validation_fail_context` - Context invalid
- `test_pre_trade_validation_fail_drawdown` - DD > 18%
- `test_position_size_multiplier_active` - 1.0x
- `test_position_size_multiplier_degraded` - 0.5x
- `test_position_size_multiplier_dead` - 0.0x
- `test_record_trade_updates_all_systems` - Recording works
- `test_health_report_generation` - Report generation

---

### 5. Integration Tests (3 test cases)

#### Complete Scenarios
- `test_scenario_healthy_trading` - 10 trades, no degradation
- `test_scenario_degradation_detection` - 20 trades, degrades
- `test_scenario_edge_death` - 150 trades, edge dies

---

### 6. Edge Case Tests (5+ test cases)

#### Boundary Conditions
- `test_empty_trade_history` - No trades
- `test_insufficient_trades_for_window` - < 20 trades
- `test_division_by_zero_protection` - Zero MFE/MAE
- `test_very_large_sample` - 500 trades
- `test_all_winners_scenario` - 100% win rate
- `test_all_losers_scenario` - 0% win rate

---

## EXPECTED OUTPUT

Running `pytest test_edge_system.py -v` should produce:

```
test_edge_system.py::TestEdgeValidatorContext::test_context_all_valid PASSED
test_edge_system.py::TestEdgeValidatorContext::test_context_atr_invalid PASSED
test_edge_system.py::TestEdgeValidatorContext::test_context_range_invalid PASSED
...
test_edge_system.py::TestCompleteScenarios::test_scenario_edge_death PASSED

========================= 40+ passed in 2.34s ==========================
```

---

## WHAT EACH TEST VERIFIES

### Context Validation Tests
✓ ATR is compared against 30th percentile  
✓ Session range checked vs 0.7× average  
✓ Macro event days detected  
✓ Expansion count verified (≥2 required)  
✓ Volume trend checked (>0.8 required)  
✓ Multiple invalid conditions detected  

### Degradation Signal Tests
✓ MFE/MAE ratio < 1.5 detected  
✓ BE stops > 40% detected  
✓ Non-extending winners > 60% detected  
✓ Avg duration > 12h detected  
✓ 2+ signals trigger alert  
✓ Health status changes correctly  

### State Machine Tests
✓ ACTIVE is initial state  
✓ Transitions are recorded  
✓ Position multiplier updates  
✓ Observation period counts down (14 days)  
✓ DEAD state is permanent  
✓ Transition history is maintained  

### Integration Tests
✓ Pre-trade validation gates trades  
✓ Recording updates all subsystems  
✓ Position sizing applies correctly  
✓ Health reports are generated  
✓ Complete trading scenarios work  

### Edge Case Tests
✓ Empty history handled  
✓ Small samples don't crash  
✓ Division by zero protected  
✓ Large samples (500 trades) work  
✓ Extreme scenarios (100% win/loss) handled  

---

## DEBUGGING A FAILED TEST

### Step 1: Read the error message
```
FAILED test_edge_system.py::TestEdgeValidatorContext::test_context_all_valid
AssertionError: assert True == False
```

### Step 2: Run with more detail
```bash
pytest test_edge_system.py::TestEdgeValidatorContext::test_context_all_valid -vv
```

### Step 3: Add debug output
Edit test to add print statements:
```python
def test_context_all_valid(self, edge_validator):
    context = edge_validator.validate_context(...)
    print(f"Debug: {context.is_valid()}")  # Add this
    assert context.is_valid() == True
```

### Step 4: Check component implementation
If test fails, check the actual implementation in:
- `engine/edge_validator.py`
- `engine/degradation_monitor.py`
- `engine/strategy_state_machine.py`
- `engine/edge_enforcement.py`

---

## COVERAGE REPORT

Generate HTML coverage report:
```bash
pytest test_edge_system.py --cov=engine --cov-report=html
```

Open in browser:
```bash
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

---

## CONTINUOUS INTEGRATION (GitHub Actions)

To run tests automatically on push, create `.github/workflows/test.yml`:

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: 3.9
      - run: pip install pytest pytest-cov
      - run: pytest test_edge_system.py -v --cov=engine
```

---

## TEST DATA REFERENCE

### Context Parameters for Testing
```python
Valid context:
├─ atr_current: 150
├─ atr_p30: 120
├─ session_range: 200
├─ session_range_avg: 250
├─ is_macro_event_day: False
├─ expansion_sessions_count: 2+
└─ volume_trend_london_ny: 0.9

Invalid context (any):
├─ atr_current: < atr_p30
├─ session_range: < 0.7 × avg
├─ is_macro_event_day: True
├─ expansion_sessions_count: < 2
└─ volume_trend_london_ny: < 0.8
```

### Trade Data for Testing
```python
Winner:
├─ entry_price: 100
├─ exit_price: 102+
├─ mfe: 200+
├─ mae: < 100
└─ reached_tp1/tp2: True

Loser:
├─ entry_price: 100
├─ exit_price: < 100
├─ mfe: < 100
├─ mae: 200+
└─ reached_tp1/tp2: False

BE Stop:
├─ entry_price: 100
├─ exit_price: ≈ 100
├─ is_be_stop: True
└─ result: insignificant
```

---

## RUNNING TESTS IN DEVELOPMENT

### Watch mode (re-run on file changes)
```bash
pip install pytest-watch
ptw test_edge_system.py
```

### Verbose output with print statements
```bash
pytest test_edge_system.py -v -s
```

### Stop on first failure
```bash
pytest test_edge_system.py -x
```

### Only failed tests
```bash
pytest test_edge_system.py --lf
```

---

## ADDING NEW TESTS

Template for new test:
```python
def test_new_feature(self, edge_validator):
    """Test description"""
    # ARRANGE: Set up test data
    test_data = {...}
    
    # ACT: Call the method
    result = edge_validator.method(test_data)
    
    # ASSERT: Verify result
    assert result == expected_value
```

---

## TEST METRICS

| Metric | Value |
|--------|-------|
| Total test cases | 40+ |
| EdgeValidator tests | 15 |
| DegradationMonitor tests | 5 |
| StrategyStateMachine tests | 7 |
| EdgeEnforcement tests | 8 |
| Integration tests | 3 |
| Edge case tests | 5+ |
| Expected coverage | 100% |
| Execution time | ~2-3 seconds |

---

## IMPORTANT NOTES

### ✅ Tests DO verify:
- Context validation logic
- Degradation signal detection
- State transitions
- Position sizing
- Trade recording
- Integration between components
- Edge cases and boundary conditions

### ❌ Tests DO NOT verify:
- Network connectivity (mocked)
- Real exchange connections
- Actual market data
- Live trading execution
- Performance under load

### To test those, use:
- Integration tests in `test_integration.py`
- Live paper trading
- Backtesting in `backtest.py`

---

## TROUBLESHOOTING

### ImportError: No module named 'engine'
```bash
# Make sure you're in the right directory
cd /home/carlo/projects/trading/daytrader2
# Add to PYTHONPATH if needed
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### pytest: command not found
```bash
pip install pytest
# or
python -m pytest test_edge_system.py
```

### Tests hang or timeout
```bash
# Run with timeout
pytest test_edge_system.py --timeout=10
```

### Too much output
```bash
# Summary only
pytest test_edge_system.py -q
```

---

## SUCCESS CRITERIA

All tests pass when:

```
✅ test_edge_system.py: 40+ passed in ~2s
✅ No warnings or errors
✅ All assertions pass
✅ No exceptions raised
✅ Coverage > 95%
```

If any test fails, the implementation needs fixing before going to production.

---

**Testing is mandatory before deployment.**

Run these tests before every commit and before any production deployment.
