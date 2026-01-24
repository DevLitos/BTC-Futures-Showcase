"""
API Middleware - Security and request processing

This module provides middleware for the FastAPI application:
- API Key authentication for protected endpoints
- Request logging
- Rate limiting (basic)
"""

import logging
import time
from typing import Optional, Set, Callable
from functools import wraps

from fastapi import Request, HTTPException, Depends
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

# =============================================================================
# API Key Authentication
# =============================================================================

# Header name for API key
API_KEY_HEADER = "X-API-Key"

# Endpoints that don't require authentication (health checks for k8s probes)
PUBLIC_ENDPOINTS: Set[str] = {
    "/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/health",
    "/api/v1/ready",
    "/healthz",
    "/readyz",
}

# Endpoints that require authentication (dangerous operations)
PROTECTED_ENDPOINTS: Set[str] = {
    "/api/v1/health/kill-switch",
    "/api/v1/trade",
    "/api/v1/position",
}

api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def get_api_key_from_settings() -> Optional[str]:
    """Get API key from settings."""
    try:
        from src.core.settings import get_settings
        settings = get_settings()
        if hasattr(settings, 'api_key') and settings.api_key:
            api_key = settings.api_key
            if hasattr(api_key, 'get_secret_value'):
                return api_key.get_secret_value()
            return str(api_key)
    except Exception:
        pass
    return None


async def verify_api_key(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header)
) -> Optional[str]:
    """
    Verify API key for protected endpoints.
    
    Public endpoints (health checks) don't require auth.
    Protected endpoints require valid X-API-Key header.
    
    Args:
        request: FastAPI request
        api_key: API key from header
        
    Returns:
        API key if valid, None for public endpoints
        
    Raises:
        HTTPException: If authentication fails for protected endpoint
    """
    path = request.url.path
    
    # Check if endpoint is public
    if path in PUBLIC_ENDPOINTS:
        return None
    
    # Check if endpoint is protected
    is_protected = any(path.startswith(ep) for ep in PROTECTED_ENDPOINTS)
    
    # Get expected API key from settings
    expected_key = get_api_key_from_settings()
    
    # If no API key is configured, allow all requests (development mode)
    if not expected_key:
        if is_protected:
            logger.warning(f"⚠️ Protected endpoint {path} accessed without API key (no key configured)")
        return api_key
    
    # Validate API key for protected endpoints
    if is_protected:
        if not api_key:
            logger.warning(f"🚫 Missing API key for protected endpoint: {path}")
            raise HTTPException(
                status_code=401,
                detail="Missing API key. Provide X-API-Key header.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        
        if api_key != expected_key:
            logger.warning(f"🚫 Invalid API key for protected endpoint: {path}")
            raise HTTPException(
                status_code=403,
                detail="Invalid API key",
            )
    
    return api_key


# =============================================================================
# Request Logging Middleware
# =============================================================================

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs all requests with timing.
    
    Logs:
    - Method and path
    - Response status code
    - Request duration
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        
        # Get client IP (handle proxies)
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
        
        # Process request
        response = await call_next(request)
        
        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000
        
        # Log (skip health checks to reduce noise)
        path = request.url.path
        if path not in {"/api/v1/health", "/healthz", "/readyz", "/"}:
            logger.info(
                f"{request.method} {path} → {response.status_code} ({duration_ms:.1f}ms) "
                f"[{client_ip}]"
            )
        
        # Add timing header
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        
        return response


# =============================================================================
# Simple Rate Limiting
# =============================================================================

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple in-memory rate limiting middleware.
    
    Limits requests per IP address to prevent abuse.
    NOTE: For production, use Redis-based rate limiting.
    """
    
    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self._requests_per_minute = requests_per_minute
        self._request_counts: dict = {}  # ip -> (count, window_start)
        self._window_seconds = 60
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get client IP
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
        
        # Skip rate limiting for health checks
        if request.url.path in {"/api/v1/health", "/healthz", "/readyz"}:
            return await call_next(request)
        
        # Check rate limit
        now = time.time()
        
        if client_ip in self._request_counts:
            count, window_start = self._request_counts[client_ip]
            
            # Reset window if expired
            if now - window_start > self._window_seconds:
                self._request_counts[client_ip] = (1, now)
            else:
                # Increment count
                count += 1
                self._request_counts[client_ip] = (count, window_start)
                
                # Check limit
                if count > self._requests_per_minute:
                    logger.warning(f"Rate limit exceeded for {client_ip}")
                    raise HTTPException(
                        status_code=429,
                        detail=f"Rate limit exceeded. Max {self._requests_per_minute} requests per minute.",
                        headers={"Retry-After": str(int(self._window_seconds - (now - window_start)))},
                    )
        else:
            self._request_counts[client_ip] = (1, now)
        
        return await call_next(request)


# =============================================================================
# Dependency for getting authenticated user/key
# =============================================================================

async def get_authenticated_key(
    api_key: Optional[str] = Depends(verify_api_key)
) -> Optional[str]:
    """
    Dependency that returns the authenticated API key.
    
    Use this in protected routes to ensure authentication.
    """
    return api_key


def require_api_key(func: Callable) -> Callable:
    """
    Decorator that requires API key authentication.
    
    Usage:
        @router.post("/dangerous")
        @require_api_key
        async def dangerous_endpoint():
            ...
    """
    @wraps(func)
    async def wrapper(*args, api_key: str = Depends(get_authenticated_key), **kwargs):
        if api_key is None:
            raise HTTPException(status_code=401, detail="API key required")
        return await func(*args, **kwargs)
    return wrapper


__all__ = [
    'API_KEY_HEADER',
    'PUBLIC_ENDPOINTS',
    'PROTECTED_ENDPOINTS',
    'api_key_header',
    'verify_api_key',
    'get_authenticated_key',
    'require_api_key',
    'RequestLoggingMiddleware',
    'RateLimitMiddleware',
]
