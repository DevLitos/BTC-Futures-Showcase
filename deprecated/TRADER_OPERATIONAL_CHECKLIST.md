# TRADER OPERATIONAL CHECKLIST
## Edge Definition & Operations Policy - Daily Operations

**This is NOT optional. This is your operational procedure.**

---

## 🔴 BEFORE MARKET OPEN (EVERY DAY)

### Context Check
- [ ] Check ATR current vs P30 (60-day history)
  - Valid if: ATR > P30
  - Log result
  
- [ ] Check session range vs historical average
  - Valid if: Range > 0.7 × average
  - Log result

- [ ] Check for macro events
  - Invalid if: Post-event without follow-through
  - Log decision

- [ ] Check expansion count (consecutive sessions > 1R)
  - Valid if: ≥ 2 sessions
  - Log count

- [ ] Check volume trend (London → NY)
  - Valid if: Ratio > 0.8
  - Log ratio

### Current State Check
- [ ] Review `enforcement.get_current_state()`
  - If 🟢 ACTIVE → proceed normally
  - If 🟡 DEGRADED → 50% position only
  - If 🟠 OBSERVATION → 50% position + day counter
  - If 🔴 DEAD → NO TRADING, review post-mortem
  
- [ ] Check position size multiplier
  - Apply to all calculations
  - Do NOT override

- [ ] Check active degradation signals
  - 0 signals = healthy
  - 1 signal = monitor
  - 2+ signals = alert active

**Decision:** Can I trade today?
- [ ] All context checks passed?
- [ ] State allows trading?
- [ ] Position sizing valid?
- If all YES → proceed to trade execution
- If ANY NO → WAIT or REDUCE sizing

---

## 🟡 BEFORE EACH TRADE

### Pre-Trade Validation
- [ ] Run `validate_pre_trade()`
  - Pass context validity
  - Pass current drawdown
  - Get response: can_trade, blockers

- [ ] Check blocker list
  - If empty → proceed
  - If any blockers → DO NOT TRADE
  - Log blockers

### Setup Validation
- [ ] Verify entry signal is VALID
  - Setup quality check
  - Timing check
  - Not emocional

- [ ] Verify risk/reward
  - SL is defined
  - TP levels are defined
  - Risk/reward acceptable

- [ ] Calculate position size
  - Base size × `get_position_size_multiplier()`
  - Never hardcode size
  - Respect degradation reduction

**Go/No-Go Decision:**
- [ ] `validate_pre_trade()` returned True?
- [ ] Setup is high quality?
- [ ] Position size calculated correctly?
- If all YES → EXECUTE
- If ANY NO → SKIP trade, wait for next

---

## 🟢 TRADE MANAGEMENT (LIVE)

### Entry Management
- [ ] Order placed successfully?
- [ ] Entry price confirmed?
- [ ] SL price confirmed?
- [ ] TP1 price confirmed?
- [ ] TP2 price confirmed (if applicable)?

### Position Management
- [ ] Monitor MFE (Maximum Favorable)
- [ ] Monitor MAE (Maximum Adverse)
- [ ] Track duration
- [ ] Ready for SL exit if needed

### Exit Management
- [ ] TP1 hit? → Reduce position / take profit
- [ ] TP2 hit? → Close remaining
- [ ] SL hit? → Record loss, analyze
- [ ] Time exit hit? → Close position

**DO NOT:**
- ❌ Move SL lower to reduce loss
- ❌ Hope for reversal beyond SL
- ❌ Extend TP without reason
- ❌ Deviate from plan

---

## 📊 AFTER EACH TRADE (WITHIN 5 MIN)

### Trade Recording
- [ ] Call `record_completed_trade()`
  - Trade ID
  - Entry price
  - Exit price
  - Entry time
  - Exit time
  - MFE value
  - MAE value
  - Reached TP1? (yes/no)
  - Reached TP2? (yes/no)
  - BE stop? (yes/no)

- [ ] Check health report
  - `get_health_report()`
  - Win rate status?
  - Degradation signals?
  - State status?

- [ ] Watch for alerts
  - Any new degradation signals?
  - Any state transitions logged?
  - Any critical warnings?

**Analysis:**
- [ ] Was this trade OPERATIONALLY correct?
  - Context valid? ✓/✗
  - Setup valid? ✓/✗
  - Timing correct? ✓/✗
  - Management correct? ✓/✗
  - PnL outcome? (irrelevant to quality)

---

## ⏰ HOURLY CHECK (EVERY HOUR DURING SESSION)

### Degradation Monitor
- [ ] Call `log_hourly_status()`
- [ ] Review output:
  - Current state?
  - Position multiplier?
  - Trade count?
  - Degradation health?
  - Win rate / Expectancy?

### Alert Check
- [ ] Any 2+ signals active?
  - If YES → system entering DEGRADED mode
  - Reduce future position sizes
  - Log alert

### Session Context
- [ ] Context still valid?
  - Check ATR again
  - Check range again
  - Check volume again
  - If INVALID → reduce risk or pause

---

## 📅 EVERY 30 TRADES (CHECKPOINT)

### Sample Size Analysis
- [ ] ✅ You've reached N=30 trades
- [ ] ✅ Now conclusions are statistically valid

### Call Weekly Report
- [ ] `log_weekly_report()`
- [ ] Review:
  - Total trades: ___
  - State: ___________
  - Position multiplier: ___x
  - Degradation signals: ___ / 4
  - Win rate: ___%
  - Expectancy: ____R
  - Profit factor: ____

### Evaluate Degradation
- [ ] Healthy? (0 signals) → CONTINUE ACTIVE
- [ ] Warning? (1 signal) → MONITOR closely
- [ ] Critical? (2 signals) → System should auto-degrade to 50%
- [ ] Severe? (3+ signals) → System may archive if persists

**Key Rule:** If 2+ signals, system automatically enters DEGRADED.
You don't decide this. The system does.

---

## 🚨 IF DEGRADATION DETECTED (2+ SIGNALS)

### Automatic Actions (System does this)
- Position size reduces to 50%
- Alert logged
- Observation period starts (14 days)

### Your Actions (You do this)
- [ ] Acknowledge alert in log
- [ ] Do NOT override system decision
- [ ] Continue trading with 50% size
- [ ] Focus on execution quality
- [ ] Monitor MFE/MAE, BE stops, duration, extensions

### Recovery Condition
- [ ] Signals drop below 2
- [ ] System automatically returns to ACTIVE
- [ ] Position size goes back to 100%
- [ ] Log recovery

### If Signals Don't Clear (14+ days)
- [ ] System enters OBSERVATION state
- [ ] Still 50% position size
- [ ] Mandatory 14-day monitoring
- [ ] After 14 days: recovery or death decision

---

## 💀 IF STRATEGY DIES

### Conditions for Death
- 3 consecutive 50-trade windows with negative expectancy
- OR: Structural edge loss (expected win rate < 45%)
- OR: Persistent degradation beyond recovery

### What You Do When Dead
1. [ ] Stop trading immediately
2. [ ] Review the post-mortem
3. [ ] Document what went wrong
4. [ ] Archive the strategy file
5. [ ] Move to next edge/strategy
6. [ ] Do NOT try to resurrect

**This is not failure. This is professionalism.**

---

## 📝 DAILY LOG TEMPLATE

```
═════════════════════════════════════════════════════════════
DAILY OPERATIONS LOG - YYYY-MM-DD
═════════════════════════════════════════════════════════════

MARKET OPEN CHECKLIST:
├─ Context validity: ✓/✗
├─ ATR: ___ vs P30: ___ → VALID/INVALID
├─ Range: ___ vs Avg: ___ → VALID/INVALID
├─ Macro events: NONE/[description]
├─ Expansions: __ consecutive sessions
├─ Volume trend: ___ → VALID/INVALID
└─ Overall: CAN TRADE / CANNOT TRADE

CURRENT STATE:
├─ State: 🟢 ACTIVE / 🟡 DEGRADED / 🟠 OBSERVATION / 🔴 DEAD
├─ Position multiplier: ___x
├─ Signals active: ___ / 4
└─ Health: HEALTHY / WARNING / CRITICAL / SEVERE

TRADES EXECUTED:
├─ Trade 1: Entry ___ → Exit ___ (PnL: ____)
│  ├─ MFE: ___ MAE: ___ Ratio: ___
│  ├─ TP1: ✓/✗ TP2: ✓/✗ BE: ✓/✗
│  └─ Operational quality: ✓ / ✗
├─ Trade 2: ...
└─ Total today: __

ALERTS:
├─ Degradation signals: __ / 4
├─ State changes: NONE / [list]
├─ New observations: [notes]
└─ Required actions: [list]

END OF DAY:
├─ Total trades: __
├─ Win rate: ___%
├─ Drawdown: ___%
├─ Notes: [key observations]
└─ Tomorrow readiness: ✓ / ✗
═════════════════════════════════════════════════════════════
```

---

## ✅ WHAT YOU ARE RESPONSIBLE FOR

- ✅ Executing when system says YES
- ✅ Respecting position size multipliers
- ✅ Recording trades accurately
- ✅ Monitoring context validity
- ✅ Respecting SL/TP rules
- ✅ Observing metrics (not judging)
- ✅ Logging everything

---

## ❌ WHAT YOU ARE NOT RESPONSIBLE FOR

- ❌ Deciding when to degrade (system does)
- ❌ Deciding when to observation (system does)
- ❌ Deciding when to kill strategy (system does)
- ❌ Interpreting market (trust edge rules)
- ❌ Optimizing parameters (not your job)
- ❌ Overriding degradation (forbidden)
- ❌ Trading when dead (forbidden)

---

## 🎯 THE FINAL RULE

**If system says NO → you say NO**
**If system says YES → you say YES**
**You are the executor, not the decision maker.**

This checklist is your daily protocol.
Follow it exactly.
No exceptions.
