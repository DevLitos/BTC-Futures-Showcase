"""
Health Bridge - Shared health status between bot and API processes

This module provides a file-based mechanism for the bot process to export
its health status so that the API process can read it without shared memory.

Architecture:
- Bot (main.py) → writes health_status.json every N seconds
- API (FastAPI) → reads health_status.json to serve /health endpoints

This decouples the two processes while maintaining real-time health data.

USAGE (Bot side):
    from src.core.health_bridge import HealthBridge
    
    bridge = HealthBridge()
    
    # In main loop or periodic task:
    bridge.write_status(health_status.get_full_status())

USAGE (API side):
    from src.core.health_bridge import HealthBridge
    
    bridge = HealthBridge()
    status = bridge.read_status()
"""

import json
import logging
import threading
import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Default path for health status file
DEFAULT_HEALTH_FILE = Path("/app/storage/health_status.json")
# Fallback for local development
LOCAL_HEALTH_FILE = Path("storage/health_status.json")


@dataclass
class BridgedHealthStatus:
    """
    Serializable health status for cross-process communication.
    
    This is a simplified version of HealthStatus that can be
    serialized to JSON and read by the API process.
    """
    # Basic info
    live: bool = False
    live_reason: str = "Unknown"
    ready: bool = False
    ready_reason: str = "Unknown"
    
    # Runtime info
    version: str = "unknown"
    git_commit: str = "unknown"
    run_id: str = "unknown"
    uptime_seconds: float = 0.0
    
    # Component status
    ws_connected: bool = False
    exchange_reachable: bool = False
    time_drift_ms: int = 0
    kill_switch_active: bool = False
    has_integrity_error: bool = False
    
    # Position info
    has_open_position: bool = False
    position_symbol: str = ""
    position_side: str = ""
    position_size: float = 0.0
    position_entry_price: float = 0.0
    position_unrealized_pnl: float = 0.0
    
    # Timestamps
    started_at: str = ""
    last_ws_message: Optional[str] = None
    last_exchange_call: Optional[str] = None
    last_strategy_loop: Optional[str] = None
    last_updated: str = ""
    
    # Trading stats (daily)
    trades_today: int = 0
    pnl_today_usd: float = 0.0
    pnl_today_r: float = 0.0
    win_rate_today: float = 0.0
    
    # Current regime
    current_regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BridgedHealthStatus":
        """Create from dictionary (JSON deserialization)."""
        # Filter to only known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered_data)
    
    @classmethod
    def from_health_status(cls, health_status: Any) -> "BridgedHealthStatus":
        """
        Create from HealthStatus object (bot side).
        
        Args:
            health_status: HealthStatus instance from src.core.health
        """
        full_status = health_status.get_full_status()
        
        return cls(
            live=full_status.get("live", False),
            live_reason=full_status.get("live_reason", "Unknown"),
            ready=full_status.get("ready", False),
            ready_reason=full_status.get("ready_reason", "Unknown"),
            version=full_status.get("version", "unknown"),
            git_commit=full_status.get("git_commit", "unknown"),
            run_id=full_status.get("run_id", "unknown"),
            uptime_seconds=full_status.get("uptime_seconds", 0.0),
            ws_connected=full_status.get("components", {}).get("ws_connected", False),
            exchange_reachable=full_status.get("components", {}).get("exchange_reachable", False),
            time_drift_ms=full_status.get("components", {}).get("time_drift_ms", 0),
            kill_switch_active=full_status.get("components", {}).get("kill_switch_active", False),
            has_integrity_error=full_status.get("components", {}).get("has_integrity_error", False),
            has_open_position=full_status.get("position", {}).get("has_open", False) if full_status.get("position") else False,
            position_symbol=full_status.get("position", {}).get("symbol", "") if full_status.get("position") else "",
            position_side=full_status.get("position", {}).get("side", "") if full_status.get("position") else "",
            position_size=full_status.get("position", {}).get("size", 0.0) if full_status.get("position") else 0.0,
            started_at=full_status.get("timestamps", {}).get("started_at", ""),
            last_ws_message=full_status.get("timestamps", {}).get("last_ws_message"),
            last_exchange_call=full_status.get("timestamps", {}).get("last_exchange_call"),
            last_strategy_loop=full_status.get("timestamps", {}).get("last_strategy_loop"),
            last_updated=datetime.now(UTC).isoformat(),
        )


class HealthBridge:
    """
    Bridge for sharing health status between bot and API processes.
    
    Uses a JSON file as the communication mechanism:
    - Bot writes periodically (every 5 seconds)
    - API reads on demand
    
    Thread-safe for both reading and writing.
    """
    
    def __init__(self, file_path: Optional[Path] = None):
        """
        Initialize health bridge.
        
        Args:
            file_path: Path to health status file. If None, uses default.
        """
        if file_path:
            self._file_path = Path(file_path)
        elif DEFAULT_HEALTH_FILE.parent.exists():
            self._file_path = DEFAULT_HEALTH_FILE
        else:
            self._file_path = LOCAL_HEALTH_FILE
        
        self._lock = threading.Lock()
        self._last_write_time: Optional[datetime] = None
        self._cached_status: Optional[BridgedHealthStatus] = None
        self._cache_ttl_seconds = 2.0  # Cache reads for 2 seconds
        self._last_read_time: Optional[datetime] = None
        
        # Ensure directory exists
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
    
    @property
    def file_path(self) -> Path:
        """Get the health status file path."""
        return self._file_path
    
    def write_status(
        self,
        health_status: Any,
        extra_data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Write health status to file (bot side).
        
        Args:
            health_status: HealthStatus instance or dict
            extra_data: Additional data to include (trades, regime, etc.)
            
        Returns:
            True if write succeeded
        """
        try:
            # Convert to bridged format
            if hasattr(health_status, 'get_full_status'):
                bridged = BridgedHealthStatus.from_health_status(health_status)
            elif isinstance(health_status, dict):
                bridged = BridgedHealthStatus.from_dict(health_status)
            else:
                logger.warning(f"Unknown health_status type: {type(health_status)}")
                return False
            
            # Add extra data
            if extra_data:
                for key, value in extra_data.items():
                    if hasattr(bridged, key):
                        setattr(bridged, key, value)
            
            # Write atomically (write to temp, then rename)
            data = bridged.to_dict()
            temp_path = self._file_path.with_suffix('.tmp')
            
            with self._lock:
                with open(temp_path, 'w') as f:
                    json.dump(data, f, indent=2)
                temp_path.rename(self._file_path)
                self._last_write_time = datetime.now(UTC)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to write health status: {e}")
            return False
    
    def read_status(self, use_cache: bool = True) -> Optional[BridgedHealthStatus]:
        """
        Read health status from file (API side).
        
        Args:
            use_cache: If True, use cached value if fresh enough
            
        Returns:
            BridgedHealthStatus if available, None otherwise
        """
        # Check cache
        if use_cache and self._cached_status and self._last_read_time:
            age = (datetime.now(UTC) - self._last_read_time).total_seconds()
            if age < self._cache_ttl_seconds:
                return self._cached_status
        
        try:
            if not self._file_path.exists():
                logger.debug(f"Health file not found: {self._file_path}")
                return None
            
            with self._lock:
                with open(self._file_path, 'r') as f:
                    data = json.load(f)
            
            status = BridgedHealthStatus.from_dict(data)
            
            # Update cache
            self._cached_status = status
            self._last_read_time = datetime.now(UTC)
            
            return status
            
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid health status JSON: {e}")
            return self._cached_status  # Return stale cache if available
        except Exception as e:
            logger.error(f"Failed to read health status: {e}")
            return self._cached_status
    
    def is_bot_alive(self, max_age_seconds: float = 30.0) -> bool:
        """
        Check if bot process is alive based on last update time.
        
        Args:
            max_age_seconds: Maximum age of health data to consider alive
            
        Returns:
            True if bot appears to be running
        """
        status = self.read_status()
        if not status or not status.last_updated:
            return False
        
        try:
            last_update = datetime.fromisoformat(status.last_updated.replace('Z', '+00:00'))
            age = (datetime.now(UTC) - last_update).total_seconds()
            return age < max_age_seconds
        except Exception:
            return False
    
    def get_status_age_seconds(self) -> Optional[float]:
        """Get age of current health status in seconds."""
        status = self.read_status()
        if not status or not status.last_updated:
            return None
        
        try:
            last_update = datetime.fromisoformat(status.last_updated.replace('Z', '+00:00'))
            return (datetime.now(UTC) - last_update).total_seconds()
        except Exception:
            return None


# =============================================================================
# Background Writer Task (for bot side)
# =============================================================================

class HealthBridgeWriter:
    """
    Background task that periodically writes health status to file.
    
    Usage in bot:
        writer = HealthBridgeWriter(health_status)
        writer.start()
        
        # Later, update with extra data:
        writer.update_extra_data({
            'trades_today': 5,
            'pnl_today_usd': 150.0,
        })
        
        # On shutdown:
        writer.stop()
    """
    
    def __init__(
        self,
        health_status: Any,
        interval_seconds: float = 5.0,
        bridge: Optional[HealthBridge] = None
    ):
        """
        Initialize writer.
        
        Args:
            health_status: HealthStatus instance to export
            interval_seconds: How often to write (default 5s)
            bridge: HealthBridge instance (creates one if not provided)
        """
        self._health_status = health_status
        self._interval = interval_seconds
        self._bridge = bridge or HealthBridge()
        self._extra_data: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def update_extra_data(self, data: Dict[str, Any]) -> None:
        """Update extra data to include in health exports."""
        with self._lock:
            self._extra_data.update(data)
    
    def start(self) -> None:
        """Start background writer (call from async context)."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._write_loop())
        logger.info(f"Health bridge writer started (interval={self._interval}s)")
    
    def stop(self) -> None:
        """Stop background writer."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Health bridge writer stopped")
    
    async def _write_loop(self) -> None:
        """Background loop that writes health status."""
        import asyncio
        
        while self._running:
            try:
                with self._lock:
                    extra = dict(self._extra_data)
                
                # 🔥 FIX (Jan 21, 2026): Run file I/O in executor to avoid blocking event loop
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,  # Use default executor (ThreadPoolExecutor)
                    self._bridge.write_status,
                    self._health_status,
                    extra
                )
                
            except Exception as e:
                logger.error(f"Health bridge write error: {e}")
            
            await asyncio.sleep(self._interval)


# =============================================================================
# Singleton Access
# =============================================================================

_bridge_instance: Optional[HealthBridge] = None
_bridge_lock = threading.Lock()


def get_health_bridge() -> HealthBridge:
    """Get or create singleton HealthBridge instance."""
    global _bridge_instance
    
    if _bridge_instance is None:
        with _bridge_lock:
            if _bridge_instance is None:
                _bridge_instance = HealthBridge()
    
    return _bridge_instance


__all__ = [
    'BridgedHealthStatus',
    'HealthBridge',
    'HealthBridgeWriter',
    'get_health_bridge',
]
