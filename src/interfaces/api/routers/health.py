"""
Health Router - System health and status endpoints

Provides:
- Liveness check (is the API service running?)
- Readiness check (is the bot ready to trade?)
- Detailed health status with component checks
- Kill switch control

ARCHITECTURE:
    This router reads health status from the bot via HealthBridge.
    The bot writes health_status.json, and the API reads it.
    This decouples the API and bot processes.
"""

from datetime import datetime, UTC
from typing import Dict, Any, Optional
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.bootstrap.dependencies import get_settings, get_kill_switch
from src.config.health_bridge import get_health_bridge, BridgedHealthStatus
from src.interfaces.api.middleware import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# SCHEMAS
# =============================================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str  # "healthy", "degraded", "unhealthy"
    timestamp: str
    uptime_seconds: Optional[float] = None
    version: str = "2.1.0"
    bot_alive: bool = False
    status_age_seconds: Optional[float] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "timestamp": "2026-01-20T22:00:00Z",
                "uptime_seconds": 3600.5,
                "version": "2.1.0",
                "bot_alive": True,
                "status_age_seconds": 2.5
            }
        }


class DetailedHealthResponse(BaseModel):
    """Detailed health check with component status."""
    status: str
    timestamp: str
    version: str = "2.1.0"
    bot_status: Optional[Dict[str, Any]] = None
    components: Dict[str, Dict[str, Any]]
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "timestamp": "2026-01-20T22:00:00Z",
                "version": "2.1.0",
                "bot_status": {
                    "live": True,
                    "ready": True,
                    "uptime_seconds": 3600
                },
                "components": {
                    "websocket": {"status": "connected", "latency_ms": 45},
                    "exchange": {"status": "ok"},
                    "kill_switch": {"status": "inactive"}
                }
            }
        }


class ReadinessResponse(BaseModel):
    """Readiness check response."""
    ready: bool
    reason: Optional[str] = None
    bot_ready: bool = False
    bot_reason: Optional[str] = None


class KillSwitchRequest(BaseModel):
    """Kill switch control request."""
    activate: bool = Field(..., description="True to activate, False to deactivate")
    reason: str = Field(default="API request", description="Reason for the change")


class KillSwitchResponse(BaseModel):
    """Kill switch control response."""
    success: bool
    active: bool
    message: str


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Basic health check (liveness probe for API).
    
    Returns 200 if the API service is running.
    Also includes bot status from health bridge.
    """
    bridge = get_health_bridge()
    bot_status = bridge.read_status()
    bot_alive = bridge.is_bot_alive()
    status_age = bridge.get_status_age_seconds()
    
    # Determine overall status
    if bot_alive and bot_status and bot_status.ready:
        status = "healthy"
    elif bot_alive:
        status = "degraded"
    else:
        status = "unhealthy"
    
    return HealthResponse(
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        uptime_seconds=bot_status.uptime_seconds if bot_status else None,
        version=bot_status.version if bot_status else "unknown",
        bot_alive=bot_alive,
        status_age_seconds=round(status_age, 1) if status_age else None,
    )


@router.get("/health/detailed", response_model=DetailedHealthResponse)
async def detailed_health_check(
    settings=Depends(get_settings),
    kill_switch=Depends(get_kill_switch),
):
    """
    Detailed health check with component status.
    
    Reads real-time status from bot via health bridge.
    Includes all component checks and position info.
    """
    bridge = get_health_bridge()
    bot_status = bridge.read_status()
    bot_alive = bridge.is_bot_alive()
    
    components = {}
    overall_status = "healthy"
    
    # Bot status
    if bot_status:
        bot_info = {
            "live": bot_status.live,
            "live_reason": bot_status.live_reason,
            "ready": bot_status.ready,
            "ready_reason": bot_status.ready_reason,
            "uptime_seconds": round(bot_status.uptime_seconds, 1),
            "run_id": bot_status.run_id,
        }
        
        # WebSocket status
        components["websocket"] = {
            "status": "connected" if bot_status.ws_connected else "disconnected",
            "last_message": bot_status.last_ws_message,
        }
        
        # Exchange status
        components["exchange"] = {
            "status": "reachable" if bot_status.exchange_reachable else "unreachable",
            "time_drift_ms": bot_status.time_drift_ms,
            "last_call": bot_status.last_exchange_call,
        }
        
        # Position status
        if bot_status.has_open_position:
            components["position"] = {
                "status": "open",
                "symbol": bot_status.position_symbol,
                "side": bot_status.position_side,
                "size": bot_status.position_size,
                "entry_price": bot_status.position_entry_price,
                "unrealized_pnl": bot_status.position_unrealized_pnl,
            }
        else:
            components["position"] = {"status": "flat"}
        
        # Regime
        components["regime"] = {
            "current": bot_status.current_regime,
            "confidence": bot_status.regime_confidence,
        }
        
        # Daily stats
        components["daily_stats"] = {
            "trades": bot_status.trades_today,
            "pnl_usd": bot_status.pnl_today_usd,
            "pnl_r": bot_status.pnl_today_r,
            "win_rate": bot_status.win_rate_today,
        }
        
        if not bot_status.ready:
            overall_status = "degraded"
    else:
        bot_info = None
        overall_status = "unhealthy"
        components["bot"] = {"status": "not_responding"}
    
    # Kill switch status (local check)
    if kill_switch:
        ks_state = kill_switch.get_state()
        components["kill_switch"] = {
            "status": "active" if ks_state.is_active else "inactive",
            "reason": ks_state.reason.value if ks_state.reason else None,
        }
        if ks_state.is_active:
            overall_status = "degraded"
    else:
        components["kill_switch"] = {"status": "not_initialized"}
    
    # API credentials status
    has_keys = bool(settings.effective_api_key and settings.effective_secret)
    components["credentials"] = {
        "status": "configured" if has_keys else "missing",
        "exchange": settings.exchange.value,
    }
    
    return DetailedHealthResponse(
        status=overall_status,
        timestamp=datetime.now(UTC).isoformat(),
        version=bot_status.version if bot_status else settings.app_version,
        bot_status=bot_info,
        components=components,
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(
    kill_switch=Depends(get_kill_switch),
):
    """
    Readiness check (readiness probe).
    
    Returns 200 if the system is ready to trade:
    - API is running
    - Bot is alive and ready
    - Kill switch is not active
    
    Returns 503 if not ready.
    """
    bridge = get_health_bridge()
    bot_status = bridge.read_status()
    bot_alive = bridge.is_bot_alive()
    
    # Check bot status
    if not bot_alive:
        raise HTTPException(
            status_code=503,
            detail=ReadinessResponse(
                ready=False,
                reason="Bot not responding",
                bot_ready=False,
                bot_reason="No recent health status"
            ).model_dump()
        )
    
    if bot_status and not bot_status.ready:
        raise HTTPException(
            status_code=503,
            detail=ReadinessResponse(
                ready=False,
                reason=f"Bot not ready: {bot_status.ready_reason}",
                bot_ready=False,
                bot_reason=bot_status.ready_reason
            ).model_dump()
        )
    
    # Check kill switch
    if kill_switch:
        ks_state = kill_switch.get_state()
        if ks_state.is_active:
            raise HTTPException(
                status_code=503,
                detail=ReadinessResponse(
                    ready=False,
                    reason=f"Kill switch active: {ks_state.reason.value}",
                    bot_ready=True,
                    bot_reason=None
                ).model_dump()
            )
    
    return ReadinessResponse(
        ready=True,
        bot_ready=True,
    )


@router.post("/health/kill-switch", response_model=KillSwitchResponse)
async def toggle_kill_switch(
    request: KillSwitchRequest,
    kill_switch=Depends(get_kill_switch),
    api_key: str = Depends(verify_api_key),
):
    """
    Activate or deactivate the kill switch.
    
    ⚠️ PROTECTED ENDPOINT - Requires X-API-Key header.
    
    When activated:
    - No new trades will be opened
    - Existing positions continue with SL/TP
    - Bot keeps running for monitoring
    
    Args:
        request: KillSwitchRequest with activate flag and reason
    """
    if not kill_switch:
        raise HTTPException(status_code=500, detail="Kill switch not initialized")
    
    if request.activate:
        # Import reason enum
        from src.config.kill_switch import KillSwitchReason
        kill_switch.activate(KillSwitchReason.MANUAL, request.reason)
        logger.warning(f"🛑 Kill switch ACTIVATED via API: {request.reason}")
        message = f"Kill switch activated: {request.reason}"
    else:
        kill_switch.deactivate()
        logger.info("✅ Kill switch DEACTIVATED via API")
        message = "Kill switch deactivated"
    
    return KillSwitchResponse(
        success=True,
        active=kill_switch.get_state().is_active,
        message=message,
    )


@router.get("/health/bot-status")
async def get_bot_status():
    """
    Get raw bot health status from bridge.
    
    Returns the complete BridgedHealthStatus as JSON.
    Useful for debugging and monitoring dashboards.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if not status:
        raise HTTPException(
            status_code=503,
            detail="Bot health status not available"
        )
    
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "status_age_seconds": bridge.get_status_age_seconds(),
        "bot_alive": bridge.is_bot_alive(),
        "data": status.to_dict(),
    }
