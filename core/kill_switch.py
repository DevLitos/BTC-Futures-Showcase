"""
Kill Switch - Hot Trading Pause Without Process Kill

This module provides a kill switch mechanism that can:
1. Stop accepting new trades
2. Continue managing existing positions (SL/TP remain active)
3. Be activated without restarting the process

ACTIVATION METHODS:
1. Environment variable: TRADING_KILL_SWITCH=true
2. File presence: /app/KILL_SWITCH (or configured path)
3. API call: POST /kill-switch/activate
4. Programmatic: kill_switch.activate("reason")

USAGE:
    from core.kill_switch import get_kill_switch
    
    ks = get_kill_switch()
    
    # Check before opening new position
    if ks.is_active:
        logger.warning(f"Kill switch active: {ks.reason}")
        return  # Don't open new position
    
    # Activate programmatically
    ks.activate("Too many errors")
    
    # Deactivate
    ks.deactivate()

BEHAVIOR WHEN ACTIVE:
- No new entries allowed
- Existing positions continue to be managed
- SL/TP orders remain on exchange
- Trailing stops continue to update
- Position closes (SL hit, TP hit, time exit) still work
- Logs and metrics continue

This is NOT a shutdown - it's a pause for new trading only.
"""

import os
import logging
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, Callable, List
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class KillSwitchReason(Enum):
    """Predefined kill switch reasons."""
    MANUAL = "manual_activation"
    ENV_VAR = "environment_variable"
    FILE_FLAG = "file_flag"
    ERROR_THRESHOLD = "error_threshold_exceeded"
    RECONCILE_ERROR = "reconciliation_error"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    API_REQUEST = "api_request"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"


@dataclass
class KillSwitchState:
    """Current state of the kill switch."""
    active: bool = False
    reason: str = ""
    activated_at: Optional[datetime] = None
    activated_by: Optional[str] = None  # component that activated
    deactivation_allowed: bool = True  # if False, requires restart


class KillSwitch:
    """
    Kill switch for emergency trading pause.
    
    Thread-safe singleton that manages trading pause state.
    Can be activated by multiple sources (env var, file, API, code).
    """
    
    _instance: Optional["KillSwitch"] = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, file_path: Optional[Path] = None):
        # Prevent re-initialization
        if hasattr(self, "_initialized") and self._initialized:
            return
        
        self._state = KillSwitchState()
        self._file_path = file_path or Path("/app/KILL_SWITCH")
        self._callbacks: List[Callable[[KillSwitchState], None]] = []
        self._check_lock = threading.Lock()
        self._initialized = True
        
        logger.info(f"Kill switch initialized, file_path={self._file_path}")
    
    @property
    def is_active(self) -> bool:
        """
        Check if kill switch is active.
        
        Checks all activation sources:
        1. Internal state (set by activate())
        2. Environment variable
        3. File flag
        
        Returns:
            True if kill switch is active
        """
        with self._check_lock:
            # Check internal state first (fastest)
            if self._state.active:
                return True
            
            # Check environment variable
            env_value = os.environ.get("TRADING_KILL_SWITCH", "").lower()
            if env_value in ("true", "1", "yes"):
                # Auto-activate from env var
                self._activate_internal(KillSwitchReason.ENV_VAR.value, "env_var")
                return True
            
            # Check file flag
            if self._file_path.exists():
                # Auto-activate from file
                self._activate_internal(KillSwitchReason.FILE_FLAG.value, "file_flag")
                return True
            
            return False
    
    @property
    def reason(self) -> str:
        """Get the reason for activation."""
        return self._state.reason
    
    @property
    def state(self) -> KillSwitchState:
        """Get full state."""
        return self._state
    
    def _activate_internal(self, reason: str, activated_by: str, allow_deactivation: bool = True):
        """Internal activation without logging (to avoid log spam on repeated checks)."""
        if not self._state.active:
            self._state.active = True
            self._state.reason = reason
            self._state.activated_at = datetime.now(UTC)
            self._state.activated_by = activated_by
            self._state.deactivation_allowed = allow_deactivation
            
            # Notify callbacks
            for callback in self._callbacks:
                try:
                    callback(self._state)
                except Exception as e:
                    logger.error(f"Kill switch callback error: {e}")
    
    def activate(
        self, 
        reason: str, 
        activated_by: str = "unknown",
        allow_deactivation: bool = True
    ) -> None:
        """
        Activate the kill switch.
        
        Args:
            reason: Human-readable reason for activation
            activated_by: Component/system that triggered activation
            allow_deactivation: If False, requires process restart to deactivate
        """
        with self._check_lock:
            if self._state.active:
                logger.warning(f"Kill switch already active: {self._state.reason}")
                return
            
            self._activate_internal(reason, activated_by, allow_deactivation)
            
            logger.critical(
                f"🛑 KILL SWITCH ACTIVATED: {reason} (by {activated_by})"
            )
            
            # Try to log to audit
            try:
                from core.logging_config import get_audit_logger
                get_audit_logger().kill_switch_activated(
                    reason=reason,
                    activated_by=activated_by,
                    allow_deactivation=allow_deactivation
                )
            except Exception:
                pass  # Audit logger may not be initialized
    
    def deactivate(self, deactivated_by: str = "unknown") -> bool:
        """
        Deactivate the kill switch.
        
        Args:
            deactivated_by: Component/system requesting deactivation
            
        Returns:
            True if deactivated, False if deactivation not allowed
        """
        with self._check_lock:
            if not self._state.active:
                logger.info("Kill switch already inactive")
                return True
            
            if not self._state.deactivation_allowed:
                logger.error(
                    f"Kill switch deactivation not allowed (reason: {self._state.reason}). "
                    "Restart required."
                )
                return False
            
            old_reason = self._state.reason
            self._state = KillSwitchState()  # Reset to default
            
            logger.warning(
                f"✅ KILL SWITCH DEACTIVATED by {deactivated_by} "
                f"(was: {old_reason})"
            )
            
            # Also remove file flag if it exists
            if self._file_path.exists():
                try:
                    self._file_path.unlink()
                    logger.info(f"Removed kill switch file: {self._file_path}")
                except Exception as e:
                    logger.error(f"Could not remove kill switch file: {e}")
            
            return True
    
    def register_callback(self, callback: Callable[[KillSwitchState], None]) -> None:
        """
        Register a callback to be called on kill switch activation.
        
        Callbacks are called with the current state when kill switch activates.
        Useful for sending notifications, updating metrics, etc.
        
        Args:
            callback: Function to call on activation
        """
        self._callbacks.append(callback)
    
    def create_file_flag(self) -> None:
        """Create the kill switch file flag (for external activation)."""
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text(
                f"Kill switch activated at {datetime.now(UTC).isoformat()}\n"
            )
            logger.info(f"Kill switch file created: {self._file_path}")
        except Exception as e:
            logger.error(f"Could not create kill switch file: {e}")
    
    def get_status(self) -> dict:
        """Get kill switch status as dictionary (for health endpoint)."""
        return {
            "active": self._state.active,
            "reason": self._state.reason,
            "activated_at": self._state.activated_at.isoformat() if self._state.activated_at else None,
            "activated_by": self._state.activated_by,
            "deactivation_allowed": self._state.deactivation_allowed,
        }


# =========================================================================
# Singleton Access
# =========================================================================

_kill_switch: Optional[KillSwitch] = None


def init_kill_switch(file_path: Optional[Path] = None) -> KillSwitch:
    """
    Initialize the global kill switch.
    
    Args:
        file_path: Optional path for kill switch file flag
        
    Returns:
        KillSwitch instance
    """
    global _kill_switch
    _kill_switch = KillSwitch(file_path)
    return _kill_switch


def get_kill_switch() -> KillSwitch:
    """
    Get the global kill switch instance.
    
    Initializes with defaults if not already initialized.
    
    Returns:
        KillSwitch instance
    """
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = KillSwitch()
    return _kill_switch


# =========================================================================
# Decorator for Kill Switch Check
# =========================================================================

def check_kill_switch(func):
    """
    Decorator that checks kill switch before executing function.
    
    If kill switch is active, returns None and logs a warning.
    Use this on functions that open new positions.
    
    Example:
        @check_kill_switch
        async def open_position(self, ...):
            # This won't run if kill switch is active
            ...
    """
    import functools
    import asyncio
    
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        ks = get_kill_switch()
        if ks.is_active:
            logger.warning(
                f"Kill switch active ({ks.reason}), skipping {func.__name__}"
            )
            return None
        return func(*args, **kwargs)
    
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        ks = get_kill_switch()
        if ks.is_active:
            logger.warning(
                f"Kill switch active ({ks.reason}), skipping {func.__name__}"
            )
            return None
        return await func(*args, **kwargs)
    
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
