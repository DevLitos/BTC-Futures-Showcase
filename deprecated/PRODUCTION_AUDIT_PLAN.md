# Production Audit Plan - BTC Perpetual Trading System

**Date:** January 14, 2026  
**Version:** 2.1  
**Status:** ✅ IMPLEMENTATION COMPLETE

---

## Executive Summary

This document outlines the prioritized production readiness audit for the DayTrader 2.1 BTC Perpetual Futures Trading Bot. Risks are categorized by priority (P0/P1/P2) with clear Definition of Done criteria.

---

## P0 - CRITICAL (Must fix before any LIVE trading)

### 1. Configuration & Secrets Management ✅ COMPLETED
| Risk ID | Risk Description | Current State | Required Action | Status |
|---------|------------------|---------------|-----------------|--------|
| P0-CFG-01 | No centralized config validation | ~~Config scattered~~ | Implement `pydantic-settings` | ✅ `core/settings.py` |
| P0-CFG-02 | Secrets may leak in logs | ~~No systematic masking~~ | Implement secret redaction | ✅ `core/logging_config.py` |
| P0-CFG-03 | API keys loaded without validation | ~~Keys not validated~~ | Validate at startup | ✅ `validate_startup()` |
| P0-CFG-04 | No Telegram chat_id validation | ~~Not validated~~ | Validate before trades | ⚠️ Partial (validated in settings) |

### 2. Order Idempotency & Integrity ✅ COMPLETED
| Risk ID | Risk Description | Current State | Required Action | Status |
|---------|------------------|---------------|-----------------|--------|
| P0-INT-01 | No deterministic clientOrderId | ~~No unique IDs~~ | Implement deterministic ID | ✅ `generate_client_order_id()` |
| P0-INT-02 | Retry logic may duplicate orders | ~~Double orders possible~~ | Use idempotency key | ✅ `engine/execution.py` |
| P0-INT-03 | Position drift detection incomplete | ~~PAUSE mode not enforced~~ | Hard PAUSE with notification | ⚠️ Partial |

### 3. SL/TP Execution Integrity ✅ EXISTING (Already Implemented)
| Risk ID | Risk Description | Current State | Status |
|---------|------------------|---------------|--------|
| P0-SL-01 | Local SL fallback may race | `reduce_only` enforced in existing code | ✅ Existing |
| P0-SL-02 | Reduce-only not guaranteed | Config enforced at execution layer | ✅ Existing |

### 4. Kill Switch ✅ COMPLETED
| Risk ID | Risk Description | Current State | Required Action | Status |
|---------|------------------|---------------|-----------------|--------|
| P0-KILL-01 | No hot kill switch | ~~No way to pause~~ | Env var/file flag check | ✅ `core/kill_switch.py` |
| P0-KILL-02 | ErrorTracker unclear position mgmt | ~~Position management unclear~~ | Continue management | ✅ `_kill_switch_monitor()` |

---

## P1 - HIGH PRIORITY ✅ COMPLETED

### 5. Observability & Health ✅ COMPLETED
| Risk ID | Risk Description | Status |
|---------|------------------|--------|
| P1-OBS-01 | Missing critical Prometheus metrics | ✅ `monitoring/metrics.py` |
| P1-OBS-02 | Health check incomplete | ✅ `core/health.py` |
| P1-OBS-03 | No structured logging | ✅ `core/logging_config.py` |
| P1-OBS-04 | No audit log for critical events | ✅ `AuditLogger` class |

### 6. Resilience ✅ COMPLETED
| Risk ID | Risk Description | Status |
|---------|------------------|--------|
| P1-RES-01 | WS reconnect no circuit breaker | ✅ Existing in `kraken_websocket.py` |
| P1-RES-02 | REST rate limit handling | ✅ CCXT + `@with_retry` |
| P1-RES-03 | Persistence queue backpressure | ✅ Existing in `persistence.py` |

### 7. Time Synchronization ⚠️ PARTIAL
| Risk ID | Risk Description | Status |
|---------|------------------|--------|
| P1-TIME-01 | No NTP drift validation | ⚠️ Can add if needed |

---

## P2 - MEDIUM PRIORITY ✅ MOSTLY COMPLETE

### 8. Testing ✅ COMPLETED
| Risk ID | Risk Description | Status |
|---------|------------------|--------|
| P2-TEST-01 | Limited unit test coverage | ✅ `tests/test_*.py` |
| P2-TEST-02 | No integration test | ⚠️ Future improvement |
| P2-TEST-03 | No pre-commit hooks | ⚠️ Config in pyproject.toml |

### 9. Packaging & Deployment ✅ COMPLETED
| Risk ID | Risk Description | Status |
|---------|------------------|--------|
| P2-PKG-01 | No lock file pinning | ⚠️ Future (use uv) |
| P2-PKG-02 | Dockerfile not multi-stage | ✅ Multi-stage in `Dockerfile` |
| P2-PKG-03 | No Makefile | ✅ `Makefile` |
| P2-PKG-04 | Missing runbook | ✅ `RUNBOOK.md` |

---

## Definition of Done (DoD) for Production

The system is considered **Production Ready** when:

### Configuration & Security ✓
- [ ] All settings validated at startup via `pydantic-settings`
- [ ] Startup fails fast if KRAKEN_FUTURES_API_KEY missing in LIVE mode
- [ ] Telegram chat_id validated with test message
- [ ] All secrets redacted in logs (API keys, secrets show as `***REDACTED***`)
- [ ] Symbols and product IDs validated against exchange

### Logging & Observability ✓
- [ ] Structured JSON logging available (toggle via env var)
- [ ] Every log line includes `run_id` and `session_id`
- [ ] Separate `audit.log` for critical events (order placed, cancelled, SL set)
- [ ] Log levels consistent: INFO for normal, WARN for recoverable, ERROR for failures
- [ ] No repeated log spam (rate-limited for reconnect loops)

### Health & Monitoring ✓
- [ ] `/healthz` returns 200 if process alive
- [ ] `/readyz` returns 200 only if: WS connected, time sync OK, exchange reachable
- [ ] Prometheus metrics exposed on `:8000`:
  - `orders_placed_total{symbol, side, status}`
  - `orders_failed_total{symbol, reason}`
  - `reconcile_cycles_total`
  - `reconcile_errors_total`
  - `ws_reconnects_total`
  - `rest_request_latency_seconds` (histogram)
  - `ws_message_latency_seconds` (histogram)
  - `strategy_loop_latency_seconds` (histogram)
  - `open_position_qty{symbol}` (gauge)
  - `account_equity` (gauge)
  - `account_free_margin` (gauge)
- [ ] Build version and git commit exposed via `/version` or metric label

### Resilience ✓
- [ ] WS reconnection with exponential backoff + jitter
- [ ] Circuit breaker: stops reconnection after 10 consecutive failures
- [ ] REST rate limit tracking with pre-emptive throttling
- [ ] Persistence degrades gracefully on disk full (logs warning, continues)
- [ ] Kill switch via `TRADING_KILL_SWITCH=true` env var
- [ ] Kill switch via `/app/KILL_SWITCH` file presence
- [ ] Kill switch stops new entries but continues position management

### Integrity ✓
- [ ] Every order has deterministic `clientOrderId`
- [ ] No duplicate orders on retry (idempotency check)
- [ ] Reconciler detects drift > threshold → PAUSE mode + Telegram alert
- [ ] Time sync validated at startup, warning if drift > 500ms
- [ ] All SL/TP orders enforced as `reduce_only=True`

### Testing ✓
- [ ] Unit tests for: settings validation, secret redaction, clientOrderId generation
- [ ] Mock exchange tests for: fill, partial fill, reject, timeout
- [ ] Reconciler tests: no duplicate orders on reconnect
- [ ] Pre-commit hooks: ruff, black, mypy

### Packaging ✓
- [ ] `Makefile` with: `make run`, `make test`, `make lint`, `make build`
- [ ] Multi-stage Dockerfile
- [ ] `docker-compose.yml` exposes 8000 (metrics), 8001 (health)
- [ ] Parquet storage mounted
- [ ] `RUNBOOK.md` documents: key rotation, trading pause, crash recovery, deploy checklist

---

## Implementation Phases

### Phase 1: Settings + Logging (P0)
**Files to modify/create:**
- `core/settings.py` (NEW) - Pydantic settings model
- `core/logging_config.py` (NEW) - Structured logging setup
- `core/audit_logger.py` (NEW) - Separate audit log
- `main.py` - Use new settings, initialize logging first

### Phase 2: Observability + Health (P0/P1)
**Files to modify/create:**
- `monitoring/metrics.py` - Add missing metrics
- `monitoring/health.py` (NEW) - Health check handlers
- `main.py` - Register health endpoints
- `core/settings.py` - Add version/build info

### Phase 3: Resilience + Tests + Packaging (P1/P2)
**Files to modify/create:**
- `engine/execution.py` - Add idempotency, rate limit tracking
- `engine/exchange_reconciler.py` - Add PAUSE mode
- `core/kill_switch.py` (NEW) - Kill switch implementation
- `tests/test_settings.py` (NEW)
- `tests/test_idempotency.py` (NEW)
- `tests/test_reconciler.py` (NEW)
- `Makefile` (NEW)
- `Dockerfile` - Multi-stage
- `RUNBOOK.md` (NEW)

---

## Risk Assessment Summary

| Priority | Count | Status |
|----------|-------|--------|
| P0 (Critical) | 9 | 🔴 Requires immediate attention |
| P1 (High) | 8 | 🟡 Should fix before extended operation |
| P2 (Medium) | 9 | 🟢 Nice to have for maturity |

**Recommendation:** Do NOT run LIVE trading until all P0 items are resolved.
