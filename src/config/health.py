"""
Health Check Endpoints - Production-Grade Liveness and Readiness

This module provides Kubernetes-compatible health endpoints:
- /healthz: Liveness probe - is the process alive?
- /readyz: Readiness probe - is the system ready to trade?

LIVENESS (/healthz):
Returns 200 if the process is running and responsive.
Returns 503 only if the process should be killed and restarted.

READINESS (/readyz):
Returns 200 only when ALL conditions are met:
- WebSocket connected
- Time sync OK (drift < threshold)
- Exchange reachable
- No integrity errors
- Kill switch not active (optional - may still want to be "ready" for monitoring)

USAGE:
    from src.config.health import start_health_server, HealthStatus
    
    # At startup:
    health_status = HealthStatus()
    start_health_server(health_status, port=8001)
    
    # Update status from components:
    health_status.set_ws_connected(True)
    health_status.set_exchange_reachable(True)
    health_status.set_time_drift_ms(50)
"""

import json
import logging
import threading
import http.server
import socketserver
from datetime import datetime, UTC
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """
    Aggregated health status for the trading system.
    
    Updated by various components to reflect current system state.
    Thread-safe for concurrent updates and reads.
    """
    
    # Process info
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: str = "unknown"
    git_commit: str = "unknown"
    run_id: str = "unknown"
    
    # Component status
    _ws_connected: bool = False
    _exchange_reachable: bool = False
    _time_drift_ms: int = 0
    _has_integrity_error: bool = False
    _kill_switch_active: bool = False
    _last_ws_message_at: Optional[datetime] = None
    _last_exchange_call_at: Optional[datetime] = None
    _last_strategy_loop_at: Optional[datetime] = None
    
    # Position info
    _has_open_position: bool = False
    _position_symbol: str = ""
    _position_side: str = ""
    _position_size: float = 0.0
    
    # Thresholds
    max_time_drift_ms: int = 500
    max_ws_silence_seconds: int = 30
    max_loop_silence_seconds: int = 60
    
    # Lock for thread-safety (RLock allows re-entrant calls from same thread)
    # CRITICAL: Must be RLock because get_full_status() calls is_live()/is_ready()
    # which also acquire the lock. With Lock, this causes DEADLOCK.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    
    def set_ws_connected(self, connected: bool) -> None:
        with self._lock:
            self._ws_connected = connected
            if connected:
                self._last_ws_message_at = datetime.now(UTC)
    
    def set_exchange_reachable(self, reachable: bool) -> None:
        with self._lock:
            self._exchange_reachable = reachable
            if reachable:
                self._last_exchange_call_at = datetime.now(UTC)
    
    def set_time_drift_ms(self, drift: int) -> None:
        with self._lock:
            self._time_drift_ms = drift
    
    def set_integrity_error(self, has_error: bool) -> None:
        with self._lock:
            self._has_integrity_error = has_error
    
    def set_kill_switch(self, active: bool) -> None:
        with self._lock:
            self._kill_switch_active = active
    
    def set_healthy(self, healthy: bool, reason: str = "") -> None:
        """
        Set overall health status.
        
        Args:
            healthy: Whether the system is healthy
            reason: Optional reason for the status
        """
        with self._lock:
            # We use exchange_reachable as the primary health indicator
            # since that's what is_healthy() checks along with other conditions
            self._exchange_reachable = healthy
            if healthy:
                self._last_exchange_call_at = datetime.now(UTC)
            # Note: reason is logged by caller, not stored here

    def update_ws_heartbeat(self) -> None:
        """Call this on every WS message to update heartbeat."""
        with self._lock:
            self._last_ws_message_at = datetime.now(UTC)
    
    def update_loop_heartbeat(self) -> None:
        """Call this on every strategy loop iteration."""
        with self._lock:
            self._last_strategy_loop_at = datetime.now(UTC)
    
    def set_position(self, symbol: str, side: str, size: float) -> None:
        with self._lock:
            self._has_open_position = size > 0
            self._position_symbol = symbol
            self._position_side = side
            self._position_size = size
    
    def clear_position(self) -> None:
        with self._lock:
            self._has_open_position = False
            self._position_symbol = ""
            self._position_side = ""
            self._position_size = 0.0
    
    def is_live(self) -> tuple[bool, str]:
        """
        Check liveness - is the process alive and responsive?
        
        Returns:
            (is_live, reason) - True if live, False with reason if not
        """
        with self._lock:
            # Process is live if strategy loop is running
            if self._last_strategy_loop_at:
                seconds_since_loop = (datetime.now(UTC) - self._last_strategy_loop_at).total_seconds()
                if seconds_since_loop > self.max_loop_silence_seconds:
                    return False, f"Strategy loop silent for {seconds_since_loop:.0f}s"
            
            return True, "OK"
    
    def is_ready(self) -> tuple[bool, str]:
        """
        Check readiness - is the system ready to trade?
        
        Returns:
            (is_ready, reason) - True if ready, False with reason if not
        """
        with self._lock:
            # Must have WS connected
            if not self._ws_connected:
                return False, "WebSocket disconnected"
            
            # Check WS heartbeat
            if self._last_ws_message_at:
                seconds_since_ws = (datetime.now(UTC) - self._last_ws_message_at).total_seconds()
                if seconds_since_ws > self.max_ws_silence_seconds:
                    return False, f"No WS message for {seconds_since_ws:.0f}s"
            
            # Must have exchange reachable
            if not self._exchange_reachable:
                return False, "Exchange unreachable"
            
            # Check time sync
            if self._time_drift_ms > self.max_time_drift_ms:
                return False, f"Time drift {self._time_drift_ms}ms > {self.max_time_drift_ms}ms"
            
            # No integrity errors
            if self._has_integrity_error:
                return False, "Integrity error detected"
            
            return True, "OK"
    
    def get_full_status(self) -> dict:
        """Get complete status as dictionary."""
        with self._lock:
            is_live, live_reason = self.is_live()
            is_ready, ready_reason = self.is_ready()
            
            return {
                "live": is_live,
                "live_reason": live_reason,
                "ready": is_ready,
                "ready_reason": ready_reason,
                "uptime_seconds": (datetime.now(UTC) - self.started_at).total_seconds(),
                "version": self.version,
                "git_commit": self.git_commit,
                "run_id": self.run_id,
                "components": {
                    "ws_connected": self._ws_connected,
                    "exchange_reachable": self._exchange_reachable,
                    "time_drift_ms": self._time_drift_ms,
                    "kill_switch_active": self._kill_switch_active,
                    "has_integrity_error": self._has_integrity_error,
                },
                "position": {
                    "has_open": self._has_open_position,
                    "symbol": self._position_symbol,
                    "side": self._position_side,
                    "size": self._position_size,
                } if self._has_open_position else None,
                "timestamps": {
                    "started_at": self.started_at.isoformat(),
                    "last_ws_message": self._last_ws_message_at.isoformat() if self._last_ws_message_at else None,
                    "last_exchange_call": self._last_exchange_call_at.isoformat() if self._last_exchange_call_at else None,
                    "last_strategy_loop": self._last_strategy_loop_at.isoformat() if self._last_strategy_loop_at else None,
                }
            }


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for health check endpoints."""
    
    # Class-level reference to health status (set by start_health_server)
    health_status: Optional[HealthStatus] = None
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/healthz":
            self._handle_liveness()
        elif self.path == "/readyz":
            self._handle_readiness()
        elif self.path == "/health":
            self._handle_full_health()
        elif self.path == "/version":
            self._handle_version()
        else:
            self.send_response(404)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
    
    def _handle_liveness(self):
        """
        /healthz - Liveness probe
        
        Returns 200 if process is alive, 503 if it should be killed.
        """
        if self.health_status is None:
            self._respond(503, {"live": False, "reason": "Health status not initialized"})
            return
        
        is_live, reason = self.health_status.is_live()
        
        if is_live:
            self._respond(200, {"live": True, "reason": reason})
        else:
            self._respond(503, {"live": False, "reason": reason})
    
    def _handle_readiness(self):
        """
        /readyz - Readiness probe
        
        Returns 200 only when system is fully ready to trade.
        """
        if self.health_status is None:
            self._respond(503, {"ready": False, "reason": "Health status not initialized"})
            return
        
        is_ready, reason = self.health_status.is_ready()
        
        if is_ready:
            self._respond(200, {"ready": True, "reason": reason})
        else:
            self._respond(503, {"ready": False, "reason": reason})
    
    def _handle_full_health(self):
        """
        /health - Full health status (for monitoring dashboards)
        """
        if self.health_status is None:
            self._respond(503, {"error": "Health status not initialized"})
            return
        
        status = self.health_status.get_full_status()
        
        # Return 200 if live, 503 otherwise
        is_live, _ = self.health_status.is_live()
        self._respond(200 if is_live else 503, status)
    
    def _handle_version(self):
        """
        /version - Version and build info
        """
        if self.health_status is None:
            self._respond(200, {"version": "unknown"})
            return
        
        self._respond(200, {
            "version": self.health_status.version,
            "git_commit": self.health_status.git_commit,
            "run_id": self.health_status.run_id,
        })
    
    def _respond(self, status_code: int, data: dict):
        """Send JSON response."""
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def log_message(self, format, *args):
        """Suppress HTTP access logs."""
        pass


class ReusableTCPServer(socketserver.TCPServer):
    """TCP server that allows address reuse."""
    allow_reuse_address = True


def start_health_server(health_status: HealthStatus, port: int = 8001) -> threading.Thread:
    """
    Start health check HTTP server in background thread.
    
    Args:
        health_status: HealthStatus instance to report
        port: Port to listen on
        
    Returns:
        The background thread running the server
    """
    # Set the shared health status
    HealthHandler.health_status = health_status
    
    def run_server():
        try:
            with ReusableTCPServer(("", port), HealthHandler) as httpd:
                logger.info(f"Health server started on port {port}")
                logger.info(f"  /healthz - Liveness probe")
                logger.info(f"  /readyz  - Readiness probe")
                logger.info(f"  /health  - Full status")
                logger.info(f"  /version - Version info")
                httpd.serve_forever()
        except OSError as e:
            logger.error(f"Health server could not start on port {port}: {e}")
    
    thread = threading.Thread(target=run_server, daemon=True, name="health-server")
    thread.start()
    
    return thread


# =========================================================================
# Global Health Status
# =========================================================================

_health_status: Optional[HealthStatus] = None


def init_health_status(
    version: str = "unknown",
    git_commit: str = "unknown",
    run_id: str = "unknown"
) -> HealthStatus:
    """
    Initialize global health status.
    
    Args:
        version: Application version
        git_commit: Git commit hash
        run_id: Unique run identifier
        
    Returns:
        HealthStatus instance
    """
    global _health_status
    _health_status = HealthStatus(
        version=version,
        git_commit=git_commit,
        run_id=run_id
    )
    return _health_status


def get_health_status() -> Optional[HealthStatus]:
    """Get global health status if initialized."""
    return _health_status
