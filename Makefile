.PHONY: help install run test lint format clean docker-build docker-run

help:
	@echo "Connectors-only Runtime - Comandos"
	@echo "  make install      Instalar dependencias"
	@echo "  make run          Ejecutar runtime connectors-only"
	@echo "  make test         Ejecutar tests"
	@echo "  make lint         Ruff check"
	@echo "  make format       Ruff format + fix"
	@echo "  make clean        Limpiar caches"
	@echo "  make docker-build Build imagen"
	@echo "  make docker-run   Ejecutar contenedor"

install:
	uv sync --frozen

run:
	uv run python main.py

test:
	uv run python -m pytest -v

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true


docker-build:
	docker build -t daytrader-connectors:latest .


docker-run:
	docker run --rm -it \
		--env-file .env.secrets \
		-p 8000:8000 \
		daytrader-connectors:latest
