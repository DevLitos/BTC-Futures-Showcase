# Contributing

## Before opening a change

1. Read the active documentation and inspect the runtime mode affected by the change.
2. Keep credentials, logs, market exports, and generated output out of commits.
3. Add or update focused tests for behavior changes.

## Local checks

```bash
uv sync
uv run pytest
uv run ruff check .
```

Do not run live trading as part of tests. Backtests should use explicit configuration and should not be described as proof of future returns.

## Pull requests

Describe the behavior changed, the validation performed, and any operational or financial risk. Keep unrelated refactors out of the same change.