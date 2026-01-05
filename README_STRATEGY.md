# BTC Perpetual Trading System

## 📋 Overview

Professional algorithmic trading system for BTC perpetual futures implementing a **Trend Continuation + Institutional Pullback** strategy.

### Strategy Summary
- **Multi-timeframe analysis**: 1H (bias) → 5m (structure) → 1m (trigger)
- **Institutional approach**: Fibonacci retracements, order flow, VWAP zones
- **Risk management**: Dynamic position sizing, TP1/TP2, trailing stops
- **Resilience**: Kill conditions, black swan filters, emergency protocols

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     TRADING SYSTEM                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐      ┌──────────────┐                    │
│  │ Data Ingest  │─────▶│ Signal Engine│                    │
│  │ (CCXT/WS)    │      │ (Strategy)   │                    │
│  └──────────────┘      └──────┬───────┘                    │
│                               │                            │
│                               ▼                            │
│  ┌──────────────────────────────────────────┐              │
│  │         State Machine                    │              │
│  │  IDLE → WAIT_FILL → MANAGE → OBSERVATION │              │
│  └──────────────────┬───────────────────────┘              │
│                     │                                      │
│           ┌─────────┴─────────┐                            │
│           ▼                   ▼                            │
│  ┌─────────────────┐  ┌──────────────┐                    │
│  │  Risk Engine    │  │Order Manager │                    │
│  │  (Position Mgmt)│  │(Execution)   │                    │
│  └─────────────────┘  └──────────────┘                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 📦 Components

### 1. **Signal Engine** (`engine/signal_engine.py`)
Main orchestrator implementing the complete strategy logic.

**Responsibilities:**
- Multi-timeframe data management
- Filter orchestration (HTF → MTF → LTF)
- State machine coordination
- Entry/exit logic

### 2. **Filters** (`engine/filters.py`)

#### Kill Conditions Filter (BLOQUE 0)
- Daily drawdown ≥ 3%
- Consecutive losses ≥ 3
- WebSocket timeout > 10s
- Funding rate > ±0.08%
- Macro event detection

#### Black Swan Filter
- Price velocity ≥ 3× ATR(1m)
- 30-minute cooldown activation

#### HTF Filter (BLOQUE 1 - 1H)
- EMA 50/200 trend direction
- ADX(14) > 20 with positive slope
- ATR above moving average
- Optional correlation with NQ/ES

#### MTF Structure Filter (BLOQUE 2 - 5M)
- Break of Structure (BOS) confirmation
- Fibonacci retracement (38.2% - 61.8%)
- HL/LH structure validation
- Price between EMA 20/50
- VWAP confluence
- Anti-overextension (< 2× ATR from VWAP)

#### LTF Trigger Filter (BLOQUE 3 - 1M)
- Candle close in bias direction
- Relative volume > 1.5×
- Order book imbalance ≥ 60/40
- CVD alignment
- No spread spikes
- Price velocity < 3× ATR

### 3. **Risk Engine** (`engine/risk_engine.py`)

**Position Sizing:**
- Session-based risk: 1.0% (London/NY), 0.5% (Asia/NY Open)
- Technical stop loss with ATR buffer
- Minimum position validation

**Exit Management:**
- **TP1**: At +1R, close 50%, move SL to BE
- **TP2**: Session high/low or swing point
- **Trailing Stop**: 1× ATR(1m)
- **Climactic Exit**: Volume ≥ 2.5× average
- **Time Exit**: Max 45 min without TP1

**Performance Tracking:**
- Profit Factor, Expectancy, Win Rate
- Trade-by-trade PnL tracking

### 4. **Order Manager** (`engine/order_manager.py`)

**Order Execution:**
- Limit post-only orders (avoid taker fees)
- 3-candle timeout (auto-cancel)
- Status tracking (PENDING → PLACED → FILLED)
- Exchange integration ready (CCXT)

### 5. **State Machine** (`engine/state_machine.py`)

**States:**
- **IDLE**: Waiting for setup
- **WAIT_FILL**: Order placed, awaiting fill
- **MANAGE**: Position open, managing exits
- **OBSERVATION**: Poor performance, observe only
- **OFF**: Kill conditions met

**Features:**
- 15-minute post-trade cooldown
- Consecutive loss tracking
- Daily metrics reset

### 6. **Indicators** (`engine/indicators.py`)

Optimized with Numba JIT compilation:
- EMA (50, 200, 20)
- ADX with slope
- ATR
- VWAP (daily/weekly)
- Fibonacci retracements
- Relative Volume
- CVD (Cumulative Volume Delta)
- Order book imbalance

### 7. **Configuration** (`engine/config.py`)

Centralized parameters:
- Timeframe definitions
- Indicator periods
- Risk limits
- Kill condition thresholds
- Session definitions
- All strategy parameters

---

## 🔄 Trading Flow (Pseudocode Implementation)

```
MAIN LOOP (every 1 second):
│
├─ BLOQUE 0: Kill Conditions
│  ├─ Daily DD ≥ 3% → STOP
│  ├─ 3 consecutive losses → STOP
│  ├─ WebSocket timeout → EMERGENCY CLOSE
│  ├─ Funding rate > threshold → STOP
│  └─ Black Swan (3× ATR candle) → COOLDOWN
│
├─ STATE: IDLE
│  ├─ Check cooldown
│  ├─ BLOQUE 1: HTF Filter (1H)
│  │  ├─ EMA 50 vs 200 → Determine BIAS
│  │  ├─ ADX > 20 with positive slope
│  │  └─ Correlation check (NQ/ES)
│  │
│  ├─ BLOQUE 2: MTF Structure (5M)
│  │  ├─ BOS confirmed
│  │  ├─ Fibonacci 38.2%-61.8%
│  │  ├─ HL/LH structure intact
│  │  ├─ Price between EMA 20/50
│  │  └─ Near VWAP, not overextended
│  │
│  ├─ BLOQUE 3: LTF Trigger (1M)
│  │  ├─ Candle closes in direction
│  │  ├─ Volume > 1.5× average
│  │  ├─ Order book 60/40 imbalance
│  │  └─ No spread spike
│  │
│  └─ BLOQUE 4: Execute Entry
│     ├─ Calculate technical stop loss
│     ├─ Calculate position size
│     ├─ Place LIMIT POST-ONLY order
│     └─ Transition to WAIT_FILL
│
├─ STATE: WAIT_FILL
│  ├─ Monitor order status
│  ├─ Check 3-candle timeout
│  ├─ If FILLED → Open position, transition to MANAGE
│  └─ If TIMEOUT → Cancel order, return to IDLE
│
├─ STATE: MANAGE
│  ├─ Update unrealized PnL
│  ├─ Update trailing stop
│  │
│  ├─ Check Stop Loss → Close full position
│  ├─ Check TP1 (+1R) → Close 50%, SL to BE
│  ├─ Check Trailing Stop → Close remaining
│  ├─ Check TP2 → Close remaining
│  ├─ Check Climactic Volume → Close
│  └─ Check Time Exit → Close if > 45min
│
├─ STATE: OBSERVATION
│  └─ Monitor only (Profit Factor < 1.5 or Expectancy ≤ 0)
│
└─ BLOQUE 7: Post-Trade
   ├─ Record PnL
   ├─ Update statistics
   ├─ 15-minute cooldown
   └─ Check performance metrics
```

---

## 🚀 Setup & Installation

### Requirements
```bash
Python 3.9+
Kafka (for data pipeline)
PostgreSQL (optional, for trade storage)
```

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Configure
Edit [engine/config.py](engine/config.py) for:
- Capital amount
- Risk percentages
- Kill condition thresholds
- Strategy parameters

### Run
```bash
# Start Kafka (if using)
docker-compose up -d

# Run trading system
python main.py
```

---

## 📊 Monitoring

### Prometheus Metrics
Available at `http://localhost:8000`

Metrics:
- `trading_signals_generated_total`
- `orders_recommended_total`
- `orders_rejected_total`
- Position status
- Performance metrics

### Logs
Real-time logging includes:
- Signal generation
- Filter results
- Order execution
- Position management
- Performance updates

---

## ⚙️ Configuration Examples

### Conservative Settings (Lower Risk)
```python
# engine/config.py
RiskManagement.RISK_LONDON_NY_PCT = 0.5  # 0.5% per trade
KillConditions.MAX_DAILY_DRAWDOWN_PCT = 2.0  # 2% max DD
RiskManagement.TP1_CLOSE_PCT = 75  # Close 75% at TP1
```

### Aggressive Settings (Higher Risk)
```python
RiskManagement.RISK_LONDON_NY_PCT = 1.5  # 1.5% per trade
KillConditions.MAX_DAILY_DRAWDOWN_PCT = 5.0  # 5% max DD
RiskManagement.TP1_CLOSE_PCT = 30  # Close 30% at TP1
```

---

## 🔐 Safety Features

1. **Kill Conditions**: Automatic shutdown on critical conditions
2. **Black Swan Filter**: Detects and handles extreme volatility
3. **WebSocket Monitoring**: Emergency exit if connection lost
4. **Position Limits**: Prevents over-leveraging
5. **Cooldown Periods**: Prevents revenge trading
6. **Observation Mode**: Auto-disable on poor performance

---

## 📈 Performance Expectations

Based on specifications:
- **Target Profit Factor**: ≥ 1.5
- **Expectancy**: Positive ($)
- **Win Rate**: 40-60% (typical for trend strategies)
- **Max Daily DD**: 3%
- **Max Consecutive Losses**: 3

---

## 🧪 Testing

### Backtest Mode
```python
# Set in main.py
exchange_connector = None  # Simulation mode
```

### Paper Trading
```python
# Use testnet exchange
exchange_connector = ccxt.binance({
    'options': {'defaultType': 'future'},
    'urls': {'api': 'https://testnet.binancefuture.com'}
})
```

### Live Trading
```python
# Use production exchange with real credentials
exchange_connector = ccxt.binance({
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
    'options': {'defaultType': 'future'}
})
```

---

## 📝 Development Notes

### Adding New Indicators
Add to `engine/indicators.py` using Numba optimization:
```python
@jit(nopython=True)
def calculate_your_indicator(data: np.ndarray, period: int) -> np.ndarray:
    # Your implementation
    pass
```

### Modifying Filters
Edit thresholds in `engine/config.py` or logic in `engine/filters.py`

### Custom Exit Logic
Extend `engine/risk_engine.py` → `manage_position()` method

---

## ⚠️ Disclaimer

This is a professional trading system for educational and research purposes. 

**Trading carries substantial risk of loss. Only trade with capital you can afford to lose.**

- Not financial advice
- Past performance does not guarantee future results
- Test thoroughly before live deployment
- Start with small position sizes

---

## 📚 References

- Specification Document: `Especificaciones Técnicas Finales_ Sistema de Trading BTC Perpetual.md`
- Strategy: Trend Continuation + Institutional Pullback
- Timeframes: 1H / 5M / 1M fractal hierarchy

---

## 🤝 Contributing

To extend or modify:
1. Fork the repository
2. Create feature branch
3. Test thoroughly (backtest + paper trading)
4. Submit pull request with detailed description

---

## 📞 Support

For questions about implementation:
- Review specification document
- Check logs for detailed execution flow
- Monitor Prometheus metrics
- Review state machine transitions

---

**Built with precision. Trade with discipline. 🎯**
