"""
FastAPI Application - Main entry point for the trading API

This module creates and configures the FastAPI application with:
- CORS middleware
- API Key authentication for protected endpoints
- Request logging middleware
- Lifespan management (startup/shutdown)
- Router registration
- Exception handlers

ARCHITECTURE:
    This API is designed to run as a SEPARATE PROCESS from the trading bot.
    Communication happens via:
    - health_status.json file (bot writes, API reads)
    - Kill switch file (API writes, bot reads)
    - Shared storage for trade logs

RUN:
    uvicorn src.api.app:app --host 0.0.0.0 --port 8080
    
    Or with hot reload for development:
    uvicorn src.api.app:app --reload --port 8080
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
import logging
import os

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.dependencies import (
    create_app_dependencies,
    shutdown_dependencies,
    get_settings,
)
from src.api.middleware import (
    RequestLoggingMiddleware,
    RateLimitMiddleware,
    verify_api_key,
    API_KEY_HEADER,
)
from src.core.health_bridge import get_health_bridge

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    
    Handles startup and shutdown tasks:
    - Initialize dependencies (settings, kill switch, etc.)
    - Initialize health bridge for reading bot status
    - Clean shutdown
    
    NOTE: This API does NOT start the trading bot.
    The bot runs as a separate process (main.py).
    """
    logger.info("🚀 Starting Daytrader API...")
    
    # Startup
    try:
        deps = create_app_dependencies()
        app.state.settings = deps['settings']
        app.state.event_bus = deps['event_bus']
        app.state.cost_model = deps['cost_model']
        app.state.kill_switch = deps['kill_switch']
        
        # Initialize health bridge for reading bot status
        app.state.health_bridge = get_health_bridge()
        
        # Log configuration
        settings = app.state.settings
        api_key_configured = bool(
            hasattr(settings, 'api_key') and settings.api_key
        )
        
        logger.info("=" * 60)
        logger.info("  DAYTRADER API STARTED")
        logger.info("=" * 60)
        logger.info(f"  Mode: {settings.trading_mode.value}")
        logger.info(f"  API Port: {getattr(settings, 'api_port', 8080)}")
        logger.info(f"  API Key Auth: {'✓ enabled' if api_key_configured else '⚠️ disabled'}")
        logger.info(f"  Health Bridge: {app.state.health_bridge.file_path}")
        logger.info(f"  Bot Alive: {app.state.health_bridge.is_bot_alive()}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Failed to start API: {e}")
        raise
    
    yield  # Application runs here
    
    # Shutdown
    logger.info("🛑 Shutting down trading API...")
    await shutdown_dependencies()
    logger.info("✅ API shutdown complete")


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.
    
    Features:
    - OpenAPI documentation at /docs and /redoc
    - CORS enabled (configure for production)
    - API key authentication for protected endpoints
    - Request logging with timing
    - Rate limiting (60 req/min per IP)
    
    Returns:
        Configured FastAPI instance
    """
    # Get version from env or default
    version = os.environ.get("APP_VERSION", "2.1.0")
    
    app = FastAPI(
        title="Daytrader API",
        description="""
REST API for BTC/USD perpetual futures trading system.

## Authentication

Protected endpoints require the `X-API-Key` header.
Configure via `API_KEY` environment variable.

## Endpoints

- **Health**: System status, liveness/readiness probes
- **Trading**: Position info, trade history, manual controls
- **Metrics**: Performance analytics, edge health, session stats

## Architecture

This API runs as a separate process from the trading bot.
Bot writes health status to a shared file, API reads it.
""",
        version=version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    
    # =================================================================
    # Middleware (order matters: last added = first executed)
    # =================================================================
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # TODO: Configure for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", API_KEY_HEADER],
        expose_headers=["X-Response-Time-Ms"],
    )
    
    # Rate limiting (60 requests per minute per IP)
    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    
    # Request logging
    app.add_middleware(RequestLoggingMiddleware)
    
    # =================================================================
    # Routers
    # =================================================================
    
    from src.api.routers import health, trading, metrics
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(trading.router, prefix="/api/v1", tags=["trading"])
    app.include_router(metrics.router, prefix="/api/v1", tags=["metrics"])
    
    # =================================================================
    # Exception Handlers
    # =================================================================
    
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "error": str(exc) if os.environ.get("DEBUG") else "See logs for details"
            }
        )
    
    return app


# Create default app instance
app = create_app()


# =============================================================================
# Root Endpoints
# =============================================================================

@app.get("/", tags=["root"])
async def root():
    """
    Root endpoint - API information and quick status.
    """
    # Try to get bot status from bridge
    try:
        bridge = get_health_bridge()
        bot_alive = bridge.is_bot_alive()
        status_age = bridge.get_status_age_seconds()
    except Exception:
        bot_alive = None
        status_age = None
    
    return {
        "name": "Daytrader API",
        "version": os.environ.get("APP_VERSION", "2.1.0"),
        "status": "running",
        "bot_alive": bot_alive,
        "status_age_seconds": round(status_age, 1) if status_age else None,
        "docs": "/docs",
        "health": "/api/v1/health",
    }


@app.get("/healthz", tags=["probes"])
async def liveness_probe():
    """
    Kubernetes liveness probe.
    
    Returns 200 if API process is alive.
    This does NOT check if the bot is running.
    """
    return {"live": True, "service": "api"}


@app.get("/readyz", tags=["probes"])
async def readiness_probe():
    """
    Kubernetes readiness probe.
    
    Returns 200 if API is ready to serve traffic.
    Checks if bot is alive via health bridge.
    """
    try:
        bridge = get_health_bridge()
        bot_alive = bridge.is_bot_alive(max_age_seconds=60)
        
        if not bot_alive:
            return JSONResponse(
                status_code=503,
                content={"ready": False, "reason": "Bot not responding"}
            )
        
        return {"ready": True, "bot_alive": True}
        
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "reason": str(e)}
        )
