# State Machine Architecture - CORRECTED

## Overview
The signal engine now implements a PROPER three-level state machine that prevents busy loops and unnecessary re-evaluation.

## States

### 1. IDLE State
**Purpose**: Search for trade setups

**Entry Conditions**:
- Bot initialized and ready to trade
- Previous setup timed out / invalidated / executed

**Cooldown Protection**:
- After setup discard: 60-second cooldown before re-evaluating HTF/MTF
- Prevents rapid state churn if price flickers around thresholds

**Evaluation Chain**:
```
IDLE → HTF check → MTF check → (decision point)
       ↓           ↓           
       REJECT      REJECT
                   ↓
                   ✓ MTF passed
                   ├─ If "waiting for LTF": → WAIT_LTF
                   └─ If ready for LTF: → LTF check → EXECUTE
```

### 2. WAIT_LTF State
**Purpose**: Freeze HTF/MTF, only check LTF trigger

**Entry Trigger**:
- MTF returns: "Deep pullback - waiting for LTF trigger"
- This means: Strong impulse + validated pullback structure, now waiting for price action confirmation

**Duration**: 
- Max 300 seconds (5 minutes ≈ 5 candlesticks on 1m timeframe)
- Any time can exit early via invalidation

**Frozen Components**:
- HTF: NOT re-evaluated (no trend re-check)
- MTF: NOT re-evaluated (no structure re-check)
- **Only component evaluated**: LTF trigger

**LTF Trigger Conditions** (ANY ONE Fires Entry):
1. **BOS_LTF**: Price breaks recent 1m high/low
2. **EMA_RECLAIM**: Price reclaims 1m EMA9
3. **EMA_SLOPE**: 1m EMA9 slope turns positive (LONG) or negative (SHORT)

**Exit Conditions** (Invalidation):
1. **Timeout**: 300s elapsed → discard setup, enter cooldown
2. **EMA50 Break**: Price breaks 5m EMA50 → discard setup, enter cooldown
3. **Entry**: LTF trigger fires → execute trade

**Post-Exit Behavior**:
- Reset state: `setup_state = IDLE`, freeze flags = False
- Enter 60s cooldown: prevents re-evaluation of same failed setup

### 3. EXECUTING State (Future Enhancement)
**Purpose**: Trade active, risk management

**Current**: Not fully implemented

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                  MARKET DATA STREAM                          │
│  (1h, 5m, 1m candles + orderbook + trades)                  │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │  Check Setup State   │
        └──────┬──────┬────────┘
               │      │
    ┌──────────┘      └─────────────┐
    │                               │
    ▼                               ▼
┌─────────────────┐       ┌──────────────────────┐
│  IDLE State     │       │  WAIT_LTF State      │
│ (HTF+MTF+LTF)   │       │  (LTF only)          │
└────┬────────────┘       └──────┬───────────────┘
     │                           │
     │ HTF Rejected              │ Timeout/Invalid
     ├─→ Return                  ├─→ Cooldown (60s)
     │                           │    Then IDLE
     │ MTF Rejected              │
     ├─→ Return                  │ LTF Triggered
     │                           ├─→ EXECUTE ENTRY
     │ MTF "waiting for LTF"      │
     ├─→ Enter WAIT_LTF          │
     │   (freeze HTF/MTF)         │
     │                           │
     │ MTF Passed + Ready        │
     ├─→ LTF Check              │
        │                        │
        ├─ LTF Rejected: Return  │
        └─ LTF Passed: Execute   │
```

## Code Implementation Details

### State Variables (signal_engine.py)
```python
self.setup_state = None              # IDLE | WAIT_LTF | EXECUTING
self.setup_start_time = None          # datetime when entered WAIT_LTF
self.setup_frozen_htf = False         # Prevents HTF re-eval in WAIT_LTF
self.setup_frozen_mtf = False         # Prevents MTF re-eval in WAIT_LTF
self.last_setup_discard_time = None   # datetime of last setup discard
self.setup_discard_cooldown = 60      # seconds to wait after discard
```

### Main Loop (_main_loop method)
```python
while self.is_running:
    current_price = market_data.get_current_price()
    
    if self.state_machine.state == "IDLE":
        await self._handle_idle_state(current_price, atr_1m)
    elif self.state_machine.state == "WAIT_FILL":
        await self._handle_wait_fill_state()
    elif self.state_machine.state == "MANAGE":
        await self._handle_manage_state()
    
    await asyncio.sleep(1)  # 1-second tick
```

### _handle_idle_state Logic
```python
def _handle_idle_state(current_price, atr_1m):
    # 1. If already in WAIT_LTF: only check LTF (lines 235-295)
    if self.setup_state == "WAIT_LTF":
        [check timeout, invalidation, LTF trigger]
        return
    
    # 2. Cooldown after setup discard (lines 297-310)
    if self.last_setup_discard_time:
        if time_since < cooldown:
            return  # Skip evaluation
        else:
            clear_discard_time()
    
    # 3. Full evaluation: HTF → MTF → LTF (lines 312+)
    if not can_trade():
        return
    
    if not htf_filter.check():
        return  # HTF rejected
    
    if not mtf_filter.check():
        return  # MTF rejected
    
    if "waiting for LTF" in mtf_reason:
        self.setup_state = "WAIT_LTF"
        self.setup_start_time = now()
        return  # Enter WAIT_LTF
    
    if not ltf_filter.check():
        return  # LTF rejected
    
    await self._execute_entry()  # All passed
```

## Key Improvements

### Before (Broken)
- HTF/MTF re-evaluated every tick despite setup_state variables
- No cooldown after failed setup → rapid re-evaluation
- Orderbook imbalance caused hard rejections → many false negatives

### After (Fixed)
- WAIT_LTF state actually prevents HTF/MTF evaluation (check happens early)
- 60s cooldown after setup discard → no rapid loops
- Orderbook is informational only → no false rejections
- LTF trigger uses price structure (BOS, EMA reclaim, slope) → more reliable

## Testing Checklist

- [ ] Start bot in IDLE state
- [ ] Observe HTF evaluation
- [ ] Watch for "entering WAIT_LTF" message
- [ ] Confirm HTF/MTF NOT re-evaluated during WAIT_LTF
- [ ] Watch LTF trigger logs (BOS/EMA/Slope)
- [ ] Test timeout after 300s
- [ ] Test EMA50 invalidation
- [ ] Test cooldown behavior (60s after discard)
- [ ] Verify orderbook logged as informational (not blocking)

## Performance Notes

**Busy Loop Risk**: ELIMINATED
- WAIT_LTF state reduces evaluation from HTF+MTF+LTF to just LTF
- Cooldown prevents rapid state churn after failed setups
- HTF/MTF only evaluated once per setup attempt

**CPU Usage**: Lower in WAIT_LTF
- LTF check simpler than full 3-level evaluation
- Fewer indicator calculations
- Reduced allocation pressure

**Latency**: Similar or improved
- No longer spinning waiting for non-existent orderbook confirmation
- LTF triggers on first valid structure (BOS/EMA/slope)
- Entry execution happens sooner
