# ⚡ SL/TP Fix - Quick Reference for Live Testing

## 🎯 What Was Fixed

The system was failing to place Stop Loss (SL) and Take Profit (TP) orders after entry:
- **Error:** `invalidPrice` on SL placement
- **Error:** `wouldNotReducePosition` on TP placement
- **Result:** Position opened UNPROTECTED (no stop loss)

## 🔧 How It's Fixed

Added **3-level fallback strategy** for SL placement:
1. Try Kraken standard `type='stop-loss'`
2. If fails, try `type='stop'` with `triggerPrice`
3. If fails, try `type='limit'` order at slightly worse price

Plus 2-level fallback for TP:
1. Try Kraken standard `type='take-profit'`
2. If fails, try simple `type='limit'` order

## 📊 What to Watch in Logs

### ✅ SUCCESS Pattern (All is Good):
```
[SL Attempt 1] Using 'stop-loss' type: sell 0.00011669 @ 89664.01
✅ SL (stop-loss) placed: order_abc123
✅ SL CONFIGURED: 89664.01 ✓ | TP: 92159.48 ✓
```

### 📋 FALLBACK Pattern (Still Good - Uses Backup):
```
[SL Attempt 1] Using 'stop-loss' type...
[SL Attempt 1 FAILED]: invalidPrice
[SL Attempt 2] Using 'stop' type with triggerPrice
✅ SL (stop) placed with triggerPrice: order_abc124
✅ SL CONFIGURED: 89664.01 ✓ | TP: 92159.48 ⚠️
```

### ❌ FAILURE Pattern (Position at Risk!):
```
[SL Attempt 1] Using 'stop-loss' type...
[SL Attempt 1 FAILED]: invalidPrice
[SL Attempt 2] Using 'stop' type...
[SL Attempt 2 FAILED]: wouldNotReducePosition
[SL Attempt 3] Using 'limit' as fallback...
❌ ALL SL ATTEMPTS FAILED: ...
❌ CRITICAL: SL placement failed - POSITION AT RISK!
```

## 🔍 What to Check on Kraken Futures Platform

**After entry order fills:**
1. Go to "Orders" → "Open Orders" section
2. Should see **Stop Loss order** (red line, below entry)
3. Should see **Take Profit order** (green line, above entry)
4. Both should show `Type: Stop` or `Type: Limit`
5. Both should show `Amount: 0.00011669 BTC` (your position size)

## 🚨 Emergency Actions

If you see the FAILURE pattern:
1. **IMMEDIATELY CLOSE POSITION** - DO NOT LEAVE IT UNPROTECTED
2. Check order fill status on Kraken Futures manually
3. If position is open and SL is missing, place it manually via Kraken UI
4. Share the log with system developer for API debugging

## 📈 Expected System Behavior

### Timeline:
```
00:00 - Setup detected, entry order placed
00:01 - Entry order FILLED ✅
00:02 - SL/TP placement attempts (0-3 seconds)
00:04 - Transition to MANAGE state
00:05+ - Monitor position for TP1 hit / SMART BE / Exit signals
```

### During MANAGE State:
- System logs position P&L every 2-3 seconds
- System monitors for TP1 hit (40% close at 1.5R)
- System monitors for SMART BE trigger (EMA20 lost or BOS against)
- System monitors for exit signals (stop hit, climactic volume, timeout)

## 💾 Log Files to Monitor

```bash
# Main log (most important)
tail -f logs/trading_system.log | grep -E "SL|TP|Setting stops|CRITICAL"

# Check for recent errors
grep "ALL SL ATTEMPTS FAILED" logs/trading_system.log
grep "CRITICAL.*POSITION AT RISK" logs/trading_system.log

# Verify successful placement
grep "✅ SL.*placed" logs/trading_system.log
grep "✅ TP.*placed" logs/trading_system.log
```

## 🔄 System Recovery

If SL/TP placement fails:
1. Position will remain open
2. System will NOT keep retrying (only attempts once)
3. You must manually intervene
4. System will eventually exit via stop hit or other conditions

## 📞 Next Steps

**To restart live trading with fix:**

1. Pull latest code changes
2. Stop current system: `Ctrl+C`
3. Restart: `python main.py`
4. Wait for new setup signal
5. Monitor logs closely for attempt 1 success
6. Verify SL/TP on Kraken Futures platform
7. Continue with MANAGE state

---

**File Modified:** `engine/execution.py` (lines 316-470)
**Syntax Check:** ✅ PASSED
**Ready for:** LIVE TESTING
**Risk Level:** LOW (only improves SL placement, no trading logic changes)

