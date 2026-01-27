# DayTrader 2.1 - Makefile
# BTC Perpetual Futures Trading Bot
# ============================================================

.PHONY: help install install-dev run run-live run-paper run-backtest test lint format check build clean docker-build docker-run

# Default target
help:
	@echo "DayTrader 2.1 - Available Commands"
	@echo "============================================================"
	@echo ""
	@echo "Setup:"
	@echo "  make install        Install production dependencies"
	@echo "  make install-dev    Install development dependencies"
	@echo ""
	@echo "Running:"
	@echo "  make run            Run in PAPER mode (default)"
	@echo "  make run-live       Run in LIVE mode (⚠️ REAL MONEY)"
	@echo "  make run-paper      Run in PAPER mode"
	@echo "  make run-backtest   Run backtest"
	@echo ""
	@echo "Development:"
	@echo "  make test           Run all tests"
	@echo "  make test-unit      Run unit tests only"
	@echo "  make test-cov       Run tests with coverage"
	@echo "  make lint           Run linter (ruff)"
	@echo "  make format         Format code (ruff + black)"
	@echo "  make check          Run all checks (lint + type + test)"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build   Build Docker image"
	@echo "  make docker-run     Run in Docker"
	@echo "  make docker-up      Start with docker-compose"
	@echo "  make docker-down    Stop docker-compose"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean          Remove cache and build artifacts"
	@echo "  make kill-switch    Activate kill switch (stop new trades)"
	@echo "  make resume         Deactivate kill switch (resume trading)"
	@echo ""

# ============================================================
# Setup
# ============================================================

install:
	uv sync --frozen

install-dev:
	uv sync --frozen --all-extras
	uv run pre-commit install

# ============================================================
# Running
# ============================================================

run: run-paper

run-live:
	@echo "⚠️  LIVE MODE - REAL MONEY AT RISK!"
	@echo "Press Ctrl+C within 5 seconds to cancel..."
	@sleep 5
	TRADING_MODE=LIVE uv run python main.py

run-paper:
	@echo "📊 PAPER MODE - Real orders will be placed!"
	TRADING_MODE=PAPER uv run python main.py

run-backtest:
	@echo "📈 Running backtest..."
	TRADING_MODE=BACKTEST uv run python run_backtest_kraken.py

# ============================================================
# Development
# ============================================================

test:
	uv run pytest tests/ -v

test-unit:
	uv run pytest tests/ -v -m "not integration"

test-cov:
	uv run pytest tests/ --cov=. --cov-report=html --cov-report=term-missing

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy engine core --ignore-missing-imports

check: lint typecheck test
	@echo "✅ All checks passed!"

# ============================================================
# Docker
# ============================================================

docker-build:
	docker build -t daytrader:latest -t daytrader:$(shell git rev-parse --short HEAD 2>/dev/null || echo "dev") .

docker-run:
	docker run --rm -it \
		--env-file .env.secrets \
		-v $(PWD)/storage:/app/storage \
		-v $(PWD)/logs:/app/logs \
		-p 8000:8000 \
		-p 8001:8001 \
		daytrader:latest

docker-run-detached:
	docker run -d \
		--name btc-trader \
		--env-file .env.secrets \
		-v $(PWD)/storage:/app/storage \
		-v $(PWD)/logs:/app/logs \
		-p 8000:8000 \
		-p 8001:8001 \
		--restart unless-stopped \
		daytrader:latest
	@echo "Container started. Logs: docker logs -f btc-trader"

docker-stop:
	docker stop btc-trader 2>/dev/null || true
	docker rm btc-trader 2>/dev/null || true

docker-logs:
	docker logs -f btc-trader

# ============================================================
# Maintenance
# ============================================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov/ .coverage
	@echo "🧹 Cleaned up cache files"

kill-switch:
	@echo "🛑 Activating kill switch..."
	@touch KILL_SWITCH
	@echo "Kill switch activated. Bot will not open new positions."
	@echo "To resume: make resume"

resume:
	@echo "✅ Deactivating kill switch..."
	@rm -f KILL_SWITCH
	@echo "Kill switch deactivated. Trading will resume on next loop."

# Check environment
check-env:
	@echo "Checking environment..."
	@test -n "$(KRAKEN_FUTURES_API_KEY)" || echo "⚠️  KRAKEN_FUTURES_API_KEY not set"
	@test -n "$(KRAKEN_FUTURES_SECRET)" || echo "⚠️  KRAKEN_FUTURES_SECRET not set"
	@test -n "$(TELEGRAM_BOT_TOKEN)" || echo "⚠️  TELEGRAM_BOT_TOKEN not set"
	@test -n "$(TELEGRAM_CHAT_ID)" || echo "⚠️  TELEGRAM_CHAT_ID not set"
	@echo "Environment check complete"

# ============================================================
# Version
# ============================================================

version:
	@echo "DayTrader v$(shell grep 'version' pyproject.toml | head -1 | cut -d'"' -f2)"
	@echo "Git: $(shell git rev-parse --short HEAD 2>/dev/null || echo 'not a git repo')"
	@echo "Python: $(shell python --version)"
