# ============================================================
# DayTrader 2.1 - Production Multi-Stage Dockerfile
# BTC Perpetual Futures Trading Bot
# ============================================================

# ---------------------
# BUILDER STAGE
# ---------------------
FROM python:3.12-slim AS builder

# Build environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml requeriments.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requeriments.txt

# ---------------------
# PRODUCTION STAGE
# ---------------------
FROM python:3.12-slim AS production

# Metadata
LABEL maintainer="DayTrader Team"
LABEL version="2.1.0"
LABEL description="BTC Perpetual Futures Trading Bot - Production"

# Build args for versioning
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=UTC
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Security: Create non-root user
RUN groupadd -r trader && useradd -r -g trader trader

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Install pre-built wheels from builder
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Copy application code
COPY core/ ./core/
COPY engine/ ./engine/
COPY ingest/ ./ingest/
COPY output/ ./output/
COPY monitoring/ ./monitoring/
COPY config/ ./config/
COPY main.py ./
COPY run_backtest_kraken.py ./
COPY prometheus.yml ./

# Create necessary directories with proper permissions
RUN mkdir -p /app/logs /app/storage /app/output && \
    touch /app/KILL_SWITCH && rm /app/KILL_SWITCH && \
    chown -R trader:trader /app

# Health check (uses our new health module endpoints)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8001/healthz || exit 1

# Switch to non-root user
USER trader

# Expose ports
# 8000: Prometheus metrics
# 8001: Health check endpoint (liveness/readiness)
EXPOSE 8000 8001

# Default entrypoint - use exec form for proper signal handling
ENTRYPOINT ["python", "-u"]
CMD ["main.py"]
