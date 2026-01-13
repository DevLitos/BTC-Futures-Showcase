# ============================================================
# DayTrader 2.1 - Production Dockerfile
# BTC Perpetual Futures Trading Bot
# ============================================================

FROM python:3.12-slim

# Metadata
LABEL maintainer="DayTrader Team"
LABEL version="2.1"
LABEL description="BTC Perpetual Futures Trading Bot"

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=UTC
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user for security
RUN groupadd -r trader && useradd -r -g trader trader

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY pyproject.toml ./
COPY requeriments.txt ./

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requeriments.txt

# Copy application code
COPY engine/ ./engine/
COPY ingest/ ./ingest/
COPY output/ ./output/
COPY monitoring/ ./monitoring/
COPY config/ ./config/
COPY main.py ./
COPY run_backtest_kraken.py ./
COPY prometheus.yml ./

# Create necessary directories
RUN mkdir -p /app/logs /app/storage /app/output && \
    chown -R trader:trader /app

# Health check endpoint script
RUN echo '#!/usr/bin/env python3\n\
import http.server\n\
import socketserver\n\
import threading\n\
import json\n\
from datetime import datetime\n\
\n\
class HealthHandler(http.server.BaseHTTPRequestHandler):\n\
    def do_GET(self):\n\
        if self.path == "/health":\n\
            self.send_response(200)\n\
            self.send_header("Content-type", "application/json")\n\
            self.end_headers()\n\
            response = {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}\n\
            self.wfile.write(json.dumps(response).encode())\n\
        else:\n\
            self.send_response(404)\n\
            self.end_headers()\n\
    def log_message(self, format, *args):\n\
        pass  # Suppress logging\n\
\n\
def start_health_server(port=8000):\n\
    with socketserver.TCPServer(("", port), HealthHandler) as httpd:\n\
        httpd.serve_forever()\n\
\n\
if __name__ == "__main__":\n\
    start_health_server()\n\
' > /app/health_server.py

# Switch to non-root user
USER trader

# Expose ports
# 8000: Prometheus metrics
# 8001: Health check endpoint
EXPOSE 8000 8001

# Default command
CMD ["python", "main.py"]
