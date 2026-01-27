"""
API Package - FastAPI application for trading system

This package provides REST API endpoints for:
- Trading operations (signals, positions)
- System status and health
- Metrics and analytics
- Configuration management

The API runs as a SEPARATE PROCESS from the trading bot.
Communication happens via:
- health_status.json (bot writes, API reads)
- KILL_SWITCH file (API writes, bot reads)
- Shared storage for trade logs

Usage:
    # Run with uvicorn (recommended)
    uvicorn src.api.app:app --host 0.0.0.0 --port 8080
    
    # Or with hot reload for development
    uvicorn src.api.app:app --reload --port 8080
    
    # With Docker Compose (see docker-compose.yml api service)
    docker compose up api

Authentication:
    Protected endpoints require X-API-Key header.
    Configure via API_KEY environment variable.
"""

from src.interfaces.api.app import create_app, app

__all__ = ['create_app', 'app']
