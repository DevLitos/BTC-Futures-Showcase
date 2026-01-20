# Fix: RR Degradation from Fees/Slippage (Jan 20, 2026)

## Problem

Trades were being rejected with:
```
📊 RR Analysis: Ideal=1.20R → Worst-case=0.41R (slippage=0.79R)
🚫 WORST-CASE RR BELOW FLOOR: 0.41R < 0.50R
```

The slippage model was calculating a 66% degradation of RR due to fees.

## Root Cause Analysis

1. **Fee Structure**: Kraken Futures charges:
   - Maker: 2 bps (0.02%)
   - Taker: 5 bps (0.05%)
   - Round-trip: ~7 bps (0.07%)

2. **Trade Setup Too Tight**: 
   - Risk was only 0.17% of position ($157 on $91k BTC)
   - Fees alone = $68 per round-trip = 43% of risk
   - This mathematically destroys any reasonable RR

3. **Mathematical Reality**:
   - For a 1.2R ideal setup with 0.17% risk:
     - Entry fee: $18.26 (2 bps)
     - Exit fee: $45.64 (5 bps)
     - Total: $63.90 = 41% of risk
   - Worst-case RR = (reward - fees) / (risk + fees) = 0.41R

## Minimum Viable Risk Calculation

For fees to consume <30% of risk (leaving reasonable RR):
- 0.07% fees → need 0.23%+ risk minimum
- At $91k BTC with $78 ATR:
  - 2.0x ATR = 0.17% risk ❌ (too tight)
  - 3.0x ATR = 0.26% risk ✅ (viable)
  - 3.5x ATR = 0.30% risk ✅ (comfortable)

## Fixes Applied

### 1. Increased SL ATR Multipliers (regime_detector.py)

| Regime | Before | After | Reason |
|--------|--------|-------|--------|
| TREND_EXPANSION | 2.5x | 3.5x | Let trend run + fee survival |
| TREND_EXHAUSTION | 2.5x | 3.5x | Premium setups + fee survival |
| RANGE | 2.0x | 3.0x | Mean reversion + fee survival |

### 2. Changed Entry Order Type (signal_engine.py)

From:
```python
order_type=OrderType.MARKET  # Worst case assumption
```

To:
```python
order_type=OrderType.LIMIT  # Bot uses limit orders for entry
```

This saves 3 bps on entry slippage (1 bps + 2 bps maker vs 1 bps + 5 bps taker).

### 3. TP Adjustment Minimum RR (important_levels.py)

Added guard to prevent TP adjustment from reducing RR below viability:
```python
MIN_ADJUSTED_RR = 1.1  # Never adjust TP below 1.1R
```

Rationale: 1.1R ideal → ~0.6R worst-case (passes floor)

## Verification

With 3.0x ATR SL (RANGE regime):
```
Entry: $91,311
Risk: $236 (0.258%)
Ideal RR: 1.20R
Worst-case RR: 0.637R ✅
Degradation: 46.9% (acceptable)
```

## Trade-offs

1. **Wider Stops**: 
   - More room for price noise
   - Fewer stopped out trades
   - Larger dollar risk per trade (but RR preserved)

2. **Position Sizing Impact**:
   - With fixed % risk (e.g., 1%), wider stops = smaller position
   - This is actually correct behavior

3. **RANGE Regime**:
   - Was designed for quick scalps
   - Now needs 3x ATR which reduces trade count
   - But surviving trades are actually profitable

## Key Learning

**Fixed fees (in bps) scale with position value, not with risk amount.**

At $91k BTC:
- 1 bps = $9.13
- 7 bps round-trip = $64

This cost is FIXED regardless of risk size. To maintain positive expectancy:
- Risk must be large enough that fees are a minority (<30%) of risk
- At current BTC prices, minimum ~0.25% risk is required
- This translates to ~3x ATR at typical volatility

## Files Modified

1. `engine/regime_detector.py` - SL ATR multipliers
2. `engine/signal_engine.py` - Entry order type
3. `engine/important_levels.py` - TP adjustment minimum RR
4. `engine/slippage_model.py` - Realistic base slippage values
5. `tests/test_important_levels.py` - Updated test for new behavior
