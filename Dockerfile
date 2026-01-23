# ============================================================
# DayTrader 2.1 - Production Multi-Stage Dockerfile
# BTC Perpetual Futures Trading Bot
# ============================================================
# 
# SEGURIDAD:
# - Usuario no-root (trader:trader, UID/GID 1000)
# - Sin privilegios adicionales
# - Imagen mínima (slim)
# - Dependencias pre-compiladas
#
# BUILD con versión:
#   docker build -t daytrader:$(git rev-parse --short HEAD) \
#     --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) .
#
# ROLLBACK:
#   docker tag daytrader:abc1234 daytrader:latest
#   docker compose up -d
#
# ============================================================

# ---------------------
# BUILDER STAGE
# ---------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build dependencies (TA-Lib requiere compilación)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Instalar TA-Lib desde source (requerido para ta-lib Python)
RUN wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && \
    rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# Install Python dependencies as wheels
COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------------------
# PRODUCTION STAGE
# ---------------------
FROM python:3.12-slim AS production

# Metadata & Version Info
LABEL maintainer="DayTrader Team"
LABEL version="2.1.0"
LABEL description="BTC Perpetual Futures Trading Bot"

ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG VERSION=2.1.0

ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}
ENV APP_VERSION=${VERSION}

# Runtime environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=UTC
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# =========================================================================
# SECURITY: Non-root user with fixed UID/GID
# =========================================================================
RUN groupadd -r -g 1000 trader && \
    useradd -r -u 1000 -g trader -d /app -s /sbin/nologin trader

# Runtime dependencies only (minimal attack surface)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && rm -rf /tmp/* /var/tmp/*

WORKDIR /app

# Install pre-built wheels
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && \
    rm -rf /wheels && \
    pip cache purge

# Copy application code (order matters for cache)
COPY --chown=trader:trader src/ ./src/
RUN mkdir -p /app/output && chown trader:trader /app/output
COPY --chown=trader:trader main.py ./

# Create directories with correct ownership
RUN mkdir -p /app/logs /app/storage /app/output && \
    chown -R trader:trader /app

# =========================================================================
# HEALTH CHECK - Operativo, no solo técnico
# =========================================================================
# /readyz verifica:
# - Proceso vivo
# - Conexión a exchange
# - Datos recibidos recientemente
# - Sin errores críticos
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8001/readyz || exit 1

# =========================================================================
# RUNTIME
# =========================================================================
USER trader

EXPOSE 8000 8001

# Exec form para manejo correcto de señales (SIGTERM)
ENTRYPOINT ["python", "-u"]
CMD ["main.py"]
