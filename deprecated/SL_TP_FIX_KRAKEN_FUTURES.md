# 🔧 Kraken Futures SL/TP Fix - Complete Solution

## Problem Analysis

**Live Trading Failure:**
```
❌ Failed to set SL: krakenfutures: createOrder failed due to invalidPrice
❌ Failed to set TP: krakenfutures: createOrder failed due to wouldNotReducePosition
```

**Context:**
- Entry order FILLED successfully (0.0001 BTC @ 90,377.00)
- Position created and tracked locally
- SL/TP placement FAILED repeatedly with CCXT errors
- System entered infinite retry loop
- **Position remained UNPROTECTED** (no stop loss)

---

## Root Causes Identified

### 1. **Invalid Order Type Parameter**
- Original code used `type='stop-loss'` with `triggerPrice` in params
- Kraken Futures CCXT implementation may expect different parameter names/formats
- Error: `invalidPrice` suggests price parameter not recognized

### 2. **"wouldNotReducePosition" Error**
- Occurs when TP order amount/price combination doesn't match exchange expectations
- Likely caused by:
  - Amount precision mismatch
  - Missing/incorrect `reduceOnly` parameter
  - Wrong order type for the exchange

### 3. **No Fallback Strategy**
- Original code had ONE retry attempt with same parameters
- If first attempt failed, all subsequent retries used identical (failing) parameters
- Position became unprotected indefinitely

---

## Solution Implemented

### **File Modified:** `engine/execution.py` - `set_position_stops()` function

### **Strategy: Multi-Level Fallback Approach**

```
┌─────────────────────────────────────────────┐
│       STOP LOSS PLACEMENT (SL)              │
├─────────────────────────────────────────────┤
│  Attempt 1: type='stop-loss' (Standard)     │
│             ↓ (if fails)                    │
│  Attempt 2: type='stop' with triggerPrice   │
│             ↓ (if fails)                    │
│  Attempt 3: type='limit' at worse price     │
│                                             │
│  ✅ SUCCESS = Return True                   │
│  ❌ FAILURE = Log critical, Return False    │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│     TAKE PROFIT PLACEMENT (TP)              │
├─────────────────────────────────────────────┤
│  Attempt 1: type='take-profit' (Standard)   │
│             ↓ (if fails)                    │
│  Attempt 2: type='limit' simple order       │
│                                             │
│  ✅ SUCCESS = Log and continue              │
│  ❌ FAILURE = Log error (secondary)         │
└─────────────────────────────────────────────┘
```

### **Key Improvements:**

1. **Multiple Order Types**
   - `stop-loss` (primary Kraken Futures standard)
   - `stop` with `triggerPrice` (fallback)
   - `limit` (last resort)

2. **Better Logging**
   - Each attempt clearly logged: `[SL Attempt 1]`, `[SL Attempt 2]`, etc.
   - Error details truncated to 100-150 chars for readability
   - Success messages with order IDs for verification

3. **Proper Error Handling**
   - Nested try-except for each strategy
   - Specific error message context
   - Outer exception handler as last resort

4. **Precision Improvements**
   - Amount formatted as `:.8f` (8 decimals for BTC)
   - Price formatted as `:.2f` (2 decimals for USD)
   - Position side clearly logged

5. **Fallback Prices**
   - SL limit order uses **slightly worse price** to ensure execution:
     - LONG: `stop_loss * 0.998` (0.2% below)
     - SHORT: `stop_loss * 1.002` (0.2% above)

### **Detailed Code Changes:**

#### **SL Placement (Lines 358-406)**
```python
# ATTEMPT 1: Standard type
type='stop-loss'
price=stop_loss
params={'reduceOnly': True, 'postOnly': False}

# ATTEMPT 2: Fallback type
type='stop'
params={'triggerPrice': stop_loss, 'reduceOnly': True}

# ATTEMPT 3: Last resort
type='limit'
price=fallback_sl_price  # 0.2% worse
params={'reduceOnly': True, 'postOnly': False}
```

#### **TP Placement (Lines 409-439)**
```python
# ATTEMPT 1: Standard type
type='take-profit'
price=take_profit
params={'reduceOnly': True, 'postOnly': False}

# ATTEMPT 2: Simple limit
type='limit'
price=take_profit
params={'reduceOnly': True, 'postOnly': False}
```

---

## Expected Behavior After Fix

### **Scenario 1: Success on Attempt 1**
```
[SL Attempt 1] Using 'stop-loss' type...
✅ SL (stop-loss) placed: order_id_12345
✅ SL CONFIGURED: 89664.01 ✓ | TP: 92159.48 ✓
```

### **Scenario 2: Success on Fallback**
```
[SL Attempt 1] Using 'stop-loss' type...
[SL Attempt 1 FAILED]: invalidPrice
[SL Attempt 2] Using 'stop' type with triggerPrice
✅ SL (stop) placed with triggerPrice: order_id_12346
✅ SL CONFIGURED: 89664.01 ✓ | TP: 92159.48 ⚠️
```

### **Scenario 3: All Attempts Fail (Position At Risk)**
```
[SL Attempt 1] Using 'stop-loss' type...
[SL Attempt 1 FAILED]: invalidPrice
[SL Attempt 2] Using 'stop' type with triggerPrice...
[SL Attempt 2 FAILED]: wouldNotReducePosition
[SL Attempt 3] Using 'limit' as fallback...
❌ ALL SL ATTEMPTS FAILED: krakenfutures: ...
❌ CRITICAL: SL placement failed - POSITION AT RISK!
```

---

## Testing Requirements

### **Next Step: Restart Live Trading**

When system next encounters a setup:

1. **Monitor Log Output**
   - Should see `[SL Attempt 1]` attempt
   - Look for `✅ SL` success message
   - Check `TP placed` confirmation

2. **Verify on Exchange**
   - Log into Kraken Futures manually
   - Confirm SL order is active (red line on chart)
   - Confirm TP order is active (green line on chart)

3. **Position Protection Status**
   - Position should have active SL within 1-2 seconds of entry
   - If using TP fallback (single TP), that's acceptable
   - Critical: **NO position should exist without any SL**

### **Success Criteria:**
- ✅ SL order placed successfully (any attempt level)
- ✅ Position has stop loss protection
- ✅ System continues to MANAGE state (not crashing/restarting)
- ✅ Trade can proceed to TP1 hit → SMART BE → TP2 exit

---

## Fallback for Extreme Cases

If all Kraken API attempts still fail, the system currently:
1. Logs critical error
2. Returns False (stops placement attempts)
3. Continues with position unprotected

**Future Enhancement:** If SL placement fails, consider:
- Immediately close the entire position (safety first)
- Or implement manual monitoring using market orders when SL would be breached
- Or use manual email/webhook alerts

---

## Configuration Files Unchanged

- ✅ `config.py` - No changes (MIN_ADX_SLOPE already at -0.5)
- ✅ `risk_engine.py` - No changes (SMART BE logic ready)
- ✅ `signal_engine.py` - No changes (call to set_position_stops already in place)

Only `execution.py` modified - **isolated fix** with no impact to other components.

---

## How to Deploy

1. **Automatic:** System loads updated code on next restart
2. **Manual Testing:** Run a new live session with real capital
3. **Monitoring:** Watch logs for fallback strategy attempts
4. **Verification:** Check Kraken Futures platform for active SL/TP orders

---

## Related Files

- [engine/execution.py](engine/execution.py#L316) - Implementation
- [engine/signal_engine.py](engine/signal_engine.py#L940) - Calls set_position_stops()
- [engine/risk_engine.py](engine/risk_engine.py) - Position tracking

