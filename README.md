# BTC Futures Research Runtime

Research-oriented BTC futures runtime with Kraken connectivity, market-data ingestion, ICT-style strategies, risk controls, metrics, journaling, and historical backtesting.

This repository is experimental software. It does not promise profitability and should not be treated as financial advice or as proof that a strategy will perform in live markets.

## Operating modes

The project has two explicit modes:

- `MODE=backtest`: downloads historical Kraken data and evaluates the configured strategies.
- `MODE=live`: connects to Kraken Futures and may submit real orders when trading is enabled and credentials are configured.

There is no paper-trading implementation in this repository. Do not set live credentials or enable live trading until you have reviewed the code, limits, exchange permissions, and operational monitoring.

## Quick start

Requirements: Python 3.12+, `uv` or a standard Python virtual environment.

```bash
uv sync
MODE=backtest uv run python main.py
```

Run the tests and linter:

```bash
uv run pytest
uv run ruff check .
```

For local live execution, configure secrets outside Git and invoke it explicitly:

```bash
MODE=live TRADING_ENABLED=true uv run python main.py
```

The live runtime reads `KRAKEN_API_KEY` and `KRAKEN_PRIVATE_KEY`. Review `.env.example` and `.env.secrets.example`; never commit a populated secrets file.

## Architecture

- `main.py`: logging and mode selection.
- `src/application/runtime`: lifecycle, health loops, and live orchestration.
- `src/application/backtest`: historical data and backtest engine.
- `src/domain`: candles, positions, signals, indicators, and strategies.
- `src/infrastructure`: exchange, WebSocket, and Prometheus integrations.
- `src/shared`: event bus and reliability primitives.

The current runtime still contains trading orchestration. The connectors-only documents describe the intended boundary and should not be read as evidence that order execution has been removed.

## Docker

Build and run the image:

```bash
docker build -t btc-research-runtime .
docker run --rm --env-file .env.secrets -p 8000:8000 btc-research-runtime
```

Do not bake credentials into the image. The container runs as a non-root user and exposes Prometheus metrics on port `8000`.

Grafana is not included yet. The runtime provides the Prometheus endpoint, but this repository does not ship a Prometheus server, Grafana service, datasource provisioning, or dashboards.

## Documentation

- [Quickstart](docs/QUICKSTART.md)
- [Architecture](docs/ARCHITECTURE_CONNECTORS_ONLY_2026-03-30.md)
- [Runbook](docs/RUNBOOK.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Observability roadmap

### Available now

- Prometheus-compatible metrics endpoint on port `8000`.
- WebSocket connectivity, message throughput, reconnects, REST reachability, and circuit-breaker gauges.
- Structured runtime logs and local trade journal files.
- Docker healthcheck against the metrics endpoint.

### Planned

- Prometheus service configuration and retention policy.
- Grafana deployment with a provisioned datasource.
- Versioned dashboards for WebSocket health, exchange reachability, order execution, risk, and PnL.
- Alert rules for disconnects, repeated REST failures, circuit-open state, and execution uncertainty.

Grafana is not included in the current repository. The runtime exposes the metrics needed by Prometheus, but there is currently no Grafana container, dashboard, datasource provisioning, or `docker-compose` stack.

## Project status

This is a technical research prototype. Before considering any live deployment, independently validate the strategy out of sample, execution assumptions, slippage, fees, drawdown, exchange failure handling, and recovery procedures.