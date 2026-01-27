# ============================================================
# BTC Trading Bot - Dockerfile
# ============================================================
# Bot autónomo: solo conoce Mercado, Estrategia, Riesgo
# Expone:
#   - 8000: /metrics (Prometheus)
#   - 8001: /health, /healthz, /readyz (Health checks)
# ============================================================

# ---------------------
# BUILDER STAGE
# ---------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

# Build dependencies (TA-Lib requiere compilación)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Instalar TA-Lib C library
RUN wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && \
    rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# Install Python dependencies
COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------------------
# PRODUCTION STAGE
# ---------------------
FROM python:3.12-slim AS production

LABEL maintainer="Fidei Ratio Capital"
LABEL description="BTC Trading Bot"

ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown

ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC

# Non-root user
RUN groupadd -r -g 1000 trader && \
    useradd -r -u 1000 -g trader -d /app -s /sbin/nologin trader

# Runtime dependencies + TA-Lib C library (required for ta-lib Python package)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy TA-Lib compiled libraries from builder
COPY --from=builder /usr/lib/libta_lib* /usr/lib/
COPY --from=builder /usr/include/ta-lib/ /usr/include/ta-lib/

# Update library cache
RUN ldconfig

WORKDIR /app

# Install pre-built wheels
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Copy application
COPY --chown=trader:trader src/ ./src/
COPY --chown=trader:trader main.py ./

# Create directories
RUN mkdir -p /app/logs /app/storage && \
    chown -R trader:trader /app

# Health check - puerto 8001 para health server
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8001/healthz || exit 1

USER trader

# Puertos:
#   8000: Prometheus metrics (/metrics)
#   8001: Health endpoints (/health, /healthz, /readyz)
EXPOSE 8000 8001

ENTRYPOINT ["python", "-u"]
CMD ["main.py"]
