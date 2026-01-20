# RUNBOOK - Production Operations Guide

**DayTrader 2.1 - BTC Perpetual Futures Trading Bot**  
**Last Updated:** January 14, 2026

---

## Table of Contents

1. [Quick Reference](#quick-reference)
2. [Startup Procedure](#startup-procedure)
3. [Shutdown Procedure](#shutdown-procedure)
4. [Pause Trading (Kill Switch)](#pause-trading-kill-switch)
5. [Resume Trading](#resume-trading)
6. [Key Rotation](#key-rotation)
7. [Crash Recovery](#crash-recovery)
8. [Common Issues](#common-issues)
9. [Monitoring & Alerts](#monitoring--alerts)
10. [Deployment Checklist](#deployment-checklist)

---

## Quick Reference

### Emergency Commands

```bash
# STOP NEW TRADES IMMEDIATELY (keeps managing positions)
make kill-switch
# OR
touch KILL_SWITCH
# OR
export TRADING_KILL_SWITCH=true

# RESUME TRADING
make resume
# OR
rm KILL_SWITCH

# FULL SHUTDOWN (graceful)
docker-compose down
# OR
Ctrl+C (wait for graceful shutdown)

# FORCE SHUTDOWN (use only if stuck)
docker kill daytrader-bot

# CHECK STATUS
curl http://localhost:8001/health | jq
```

### Health Endpoints

| Endpoint | Purpose | OK Status |
|----------|---------|-----------|
| `/healthz` | Liveness (is process alive?) | 200 |
| `/readyz` | Readiness (can it trade?) | 200 |
| `/health` | Full status | 200 |
| `/version` | Build info | 200 |

### Ports

| Port | Service |
|------|---------|
| 8000 | Prometheus metrics |
| 8001 | Health checks |

---

## Startup Procedure

### Prerequisites

1. **Environment file exists:** `~/.trad2_env` or `.env`
2. **Required variables set:**
   ```bash
   KRAKEN_FUTURES_API_KEY=xxx
   KRAKEN_FUTURES_SECRET=xxx
   TELEGRAM_BOT_TOKEN=xxx
   TELEGRAM_CHAT_ID=xxx
   TRADING_MODE=PAPER  # or LIVE
   ```

### Start in Docker (Recommended)

```bash
# Build image
make docker-build

# Start services
make docker-up

# Check logs
docker-compose logs -f trading-bot

# Verify health
curl http://localhost:8001/readyz
```

### Start Locally (Development)

```bash
# Install dependencies
make install

# Run in PAPER mode
make run-paper

# Run in LIVE mode (⚠️ REAL MONEY)
make run-live
```

### Verify Startup

1. Check logs for `✅ INITIALIZATION COMPLETE`
2. Check health: `curl http://localhost:8001/health | jq`
3. Check Prometheus: `curl http://localhost:8000/metrics | grep trading`
4. Check Telegram for startup notification

---

## Shutdown Procedure

### Graceful Shutdown

```bash
# Docker
docker-compose down

# Local
Ctrl+C (once)
```

The bot will:
1. Stop accepting new signals
2. Cancel pending orders (NOT SL/TP)
3. Keep SL/TP orders on exchange
4. Flush logs to disk
5. Close WebSocket connections
6. Exit cleanly

### Verify Shutdown

1. Check `✅ SHUTDOWN COMPLETE` in logs
2. Verify no orphan orders: Log into Kraken and check open orders
3. Position should remain protected by exchange SL/TP

### Force Shutdown (Emergency)

```bash
# If graceful shutdown hangs
docker kill daytrader-bot

# Or for local
Ctrl+C (twice quickly)
```

⚠️ **Warning:** Force shutdown may leave orphan orders. Always check exchange.

---

## Pause Trading (Kill Switch)

The kill switch stops **NEW** trades but continues managing existing positions.

### When to Use

- Market conditions are dangerous
- System showing errors but you want to keep position managed
- Temporary maintenance
- Manual intervention needed

### Activation Methods

**Method 1: File Flag (Recommended)**
```bash
touch KILL_SWITCH
# Or
make kill-switch
```

**Method 2: Environment Variable**
```bash
export TRADING_KILL_SWITCH=true
```

**Method 3: Docker**
```bash
docker exec daytrader-bot touch /app/KILL_SWITCH
```

### Verify Kill Switch Active

```bash
curl http://localhost:8001/health | jq '.components.kill_switch_active'
# Should return: true
```

### What Happens

- ✅ Existing position continues to be managed
- ✅ SL/TP orders remain on exchange
- ✅ Trailing stops continue updating
- ✅ Position will close if SL/TP hit
- ❌ No new entries will be opened
- ❌ No new signals acted upon

---

## Resume Trading

### Deactivation Methods

**Method 1: Remove File**
```bash
rm KILL_SWITCH
# Or
make resume
```

**Method 2: Unset Environment**
```bash
unset TRADING_KILL_SWITCH
```

**Method 3: Docker**
```bash
docker exec daytrader-bot rm /app/KILL_SWITCH
```

### Verify Resumed

```bash
curl http://localhost:8001/health | jq '.components.kill_switch_active'
# Should return: false
```

Trading will resume on the next strategy loop iteration (~1 second).

---

## Key Rotation

### Kraken API Keys

1. **Generate new keys on Kraken:**
   - Go to Kraken Futures → Settings → API
   - Create new key with required permissions
   - **Do NOT delete old key yet**

2. **Update environment:**
   ```bash
   # Edit ~/.trad2_env or .env
   KRAKEN_FUTURES_API_KEY=new_key
   KRAKEN_FUTURES_SECRET=new_secret
   ```

3. **Activate kill switch:**
   ```bash
   make kill-switch
   ```

4. **Restart bot:**
   ```bash
   docker-compose restart trading-bot
   ```

5. **Verify new keys work:**
   - Check logs for successful connection
   - Check health endpoint

6. **Delete old keys on Kraken**

### Telegram Bot Token

1. Create new bot with @BotFather
2. Update `TELEGRAM_BOT_TOKEN` in env
3. Restart bot
4. Send test message: check logs for delivery

---

## Crash Recovery

### Automatic Recovery

The bot automatically handles crash recovery on startup:

1. **Detects orphan positions** → Adopts them
2. **Detects orphan orders** → Cancels non-protective orders
3. **Keeps SL/TP orders** → Protective orders preserved
4. **Syncs state** → Position store synced from exchange

### Manual Recovery Checklist

If automatic recovery fails:

1. **Check exchange for open position:**
   ```bash
   # Log into Kraken Futures
   # Check Positions tab
   ```

2. **Check for orphan orders:**
   ```bash
   # Check Open Orders tab
   # Cancel any stale limit orders (NOT SL/TP!)
   ```

3. **Verify SL/TP exist:**
   - Every position MUST have SL order on exchange
   - If missing, set manually or close position

4. **Restart bot:**
   ```bash
   make docker-up
   ```

5. **Verify adoption:**
   - Check logs for "Position adopted"
   - Check health for position info

### Position Sync Issues

If local and exchange state diverge:

1. **Kill switch on:**
   ```bash
   make kill-switch
   ```

2. **Close position manually:**
   - Log into Kraken
   - Market close position
   - Cancel all orders

3. **Clear local state:**
   ```bash
   rm storage/positions/*
   ```

4. **Restart clean:**
   ```bash
   docker-compose restart trading-bot
   ```

---

## Common Issues

### Bot Not Opening Trades

**Check:**
1. Kill switch active? `curl localhost:8001/health | jq '.components.kill_switch_active'`
2. Score threshold too high? Check config
3. Daily loss limit hit? Check metrics
4. Session quality filter? Check logs

### WebSocket Disconnections

**Symptoms:** Frequent "WebSocket connection failed" in logs

**Causes:**
- Kraken maintenance
- Network issues
- Rate limiting

**Actions:**
1. Check Kraken status page
2. Bot will auto-reconnect with backoff
3. If persistent, check network

### Order Rejections

**Check logs for rejection reason:**
- `insufficient margin` → Reduce position size
- `invalid reduce_only` → SL/TP mismatch
- `rate limit` → Bot will retry

### Time Sync Issues

**Symptoms:** "Clock drift detected" warning

**Fix:**
```bash
# Check NTP
timedatectl status

# Force sync
sudo systemctl restart systemd-timesyncd
```

---

## Monitoring & Alerts

### Prometheus Queries

```promql
# Orders placed
sum(rate(trading_orders_placed_total[5m])) by (status)

# Failed orders
sum(rate(trading_orders_failed_total[5m])) by (reason)

# Position size
trading_open_position_qty

# Account equity
trading_account_equity

# WebSocket health
trading_health_ws_connected

# Kill switch status
trading_kill_switch_active
```

### Alert Rules (Grafana)

```yaml
- alert: BotNotReady
  expr: absent(trading_health_ws_connected{} == 1)
  for: 5m
  labels:
    severity: critical

- alert: HighOrderFailRate
  expr: rate(trading_orders_failed_total[5m]) > 0.1
  for: 10m
  labels:
    severity: warning

- alert: KillSwitchActive
  expr: trading_kill_switch_active == 1
  for: 1m
  labels:
    severity: warning
```

### Log Locations

| Log | Location | Purpose |
|-----|----------|---------|
| Main | `logs/trading_*.log` | All application logs |
| Audit | `logs/audit.log` | Critical trading events |
| Docker | `docker-compose logs` | Container output |

### Audit Log Events

The audit log contains structured JSON entries for:
- `ORDER_PLACED` - Every order sent
- `ORDER_CANCELLED` - Order cancellations
- `ORDER_FILLED` - Fill confirmations
- `POSITION_OPENED` - Entry confirmation
- `POSITION_CLOSED` - Exit with PnL
- `SL_SET` / `TP_SET` - Stop/target set
- `KILL_SWITCH` - Kill switch activation
- `RECONCILE_ERROR` - State discrepancy

---

## Deployment Checklist

### Pre-Deployment

- [ ] All tests passing: `make test`
- [ ] Lint clean: `make lint`
- [ ] Environment file configured
- [ ] API keys validated (test connection)
- [ ] Telegram bot tested
- [ ] Docker image built: `make docker-build`
- [ ] Storage directories exist with correct permissions

### First-Time Deployment

- [ ] Start in PAPER mode first
- [ ] Verify health endpoints
- [ ] Verify Prometheus metrics
- [ ] Verify Telegram notifications
- [ ] Open a test position manually on exchange
- [ ] Restart bot, verify position adoption
- [ ] Run for 24h in PAPER mode
- [ ] Review logs for errors

### Production Go-Live

- [ ] Backup current config
- [ ] Ensure kill switch file removed
- [ ] Set `TRADING_MODE=LIVE`
- [ ] Deploy new version
- [ ] Monitor first few trades closely
- [ ] Verify SL/TP set on exchange
- [ ] Set up alerting

### Post-Deployment Verification

- [ ] Health check returns 200: `curl localhost:8001/readyz`
- [ ] Metrics flowing: `curl localhost:8000/metrics | grep trading`
- [ ] Telegram working (check for startup message)
- [ ] WebSocket connected (check logs)
- [ ] No errors in logs
- [ ] Position tracking correct

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TRADING_MODE` | No | PAPER | LIVE/PAPER/BACKTEST |
| `EXCHANGE` | No | krakenfutures | Exchange to use |
| `KRAKEN_FUTURES_API_KEY` | Yes* | - | API key |
| `KRAKEN_FUTURES_SECRET` | Yes* | - | API secret |
| `TELEGRAM_BOT_TOKEN` | No** | - | Bot token |
| `TELEGRAM_CHAT_ID` | No** | - | Chat ID |
| `TRADING_KILL_SWITCH` | No | false | Kill switch |
| `METRICS_PORT` | No | 8000 | Prometheus port |
| `HEALTH_PORT` | No | 8001 | Health check port |
| `LOG_LEVEL` | No | INFO | Log verbosity |
| `LOG_FORMAT` | No | text | text/json |
| `MAX_POSITION_USD` | No | 10000 | Max position size |
| `MAX_DAILY_LOSS_PCT` | No | 3.0 | Daily loss limit |

*Required for LIVE/PAPER mode  
**Highly recommended for LIVE mode

---

## Support

For issues:
1. Check logs for errors
2. Review this runbook
3. Check Kraken status page
4. Review recent code changes
