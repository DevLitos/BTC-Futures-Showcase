"""
Error Tracker - Consecutive error detection and kill switch
PRODUCTION SAFETY: 3 consecutive critical errors → stop bot + Telegram alert
"""

import logging
from datetime import datetime, UTC
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    WARNING = "warning"      # Log only
    ERROR = "error"          # Count toward consecutive
    CRITICAL = "critical"    # Immediate action


@dataclass
class ErrorEvent:
    timestamp: datetime
    severity: ErrorSeverity
    source: str
    message: str
    details: Optional[str] = None


@dataclass
class ErrorTrackerConfig:
    """Configuration for error tracking"""
    max_consecutive_errors: int = 3           # Stop after this many
    error_window_seconds: int = 300           # 5 min window for counting
    reset_on_success: bool = True             # Reset counter on successful operation
    notify_on_stop: bool = True               # Send Telegram on stop


class ErrorTracker:
    """
    Track consecutive errors and trigger kill switch when threshold reached.
    
    Usage:
        tracker = ErrorTracker(config, telegram_callback)
        
        try:
            await risky_operation()
            tracker.record_success()
        except Exception as e:
            if await tracker.record_error("execution", str(e)):
                # Bot should stop
                return
    """
    
    def __init__(self, 
                 config: Optional[ErrorTrackerConfig] = None,
                 telegram_callback: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.config = config or ErrorTrackerConfig()
        self.telegram_callback = telegram_callback
        
        # State
        self.consecutive_errors: int = 0
        self.total_errors: int = 0
        self.error_history: list[ErrorEvent] = []
        self.is_stopped: bool = False
        self.stop_reason: Optional[str] = None
        self.last_error_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        
        logger.info(f"ErrorTracker initialized: max_consecutive={self.config.max_consecutive_errors}")
    
    async def record_error(self, source: str, message: str, 
                          severity: ErrorSeverity = ErrorSeverity.ERROR,
                          details: Optional[str] = None) -> bool:
        """
        Record an error event.
        
        Returns: True if bot should stop (threshold reached)
        """
        now = datetime.now(UTC)
        
        event = ErrorEvent(
            timestamp=now,
            severity=severity,
            source=source,
            message=message,
            details=details
        )
        self.error_history.append(event)
        self.last_error_time = now
        self.total_errors += 1
        
        # Only count ERROR and CRITICAL toward consecutive
        if severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL):
            self.consecutive_errors += 1
            
            logger.error(f"🔴 ERROR #{self.consecutive_errors}/{self.config.max_consecutive_errors}: "
                        f"[{source}] {message}")
            
            # Check if we should stop
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                await self._trigger_stop(source, message)
                return True
        else:
            logger.warning(f"⚠️ WARNING: [{source}] {message}")
        
        return False
    
    def record_success(self, operation: str = "general"):
        """Record a successful operation - resets consecutive error counter"""
        if self.config.reset_on_success and self.consecutive_errors > 0:
            logger.info(f"✅ Success after {self.consecutive_errors} errors - resetting counter")
            self.consecutive_errors = 0
        
        self.last_success_time = datetime.now(UTC)
    
    async def _trigger_stop(self, source: str, last_error: str):
        """Trigger bot stop and send Telegram notification"""
        self.is_stopped = True
        self.stop_reason = f"Consecutive errors ({self.consecutive_errors}) from {source}: {last_error}"
        
        logger.critical(f"🚨 KILL SWITCH TRIGGERED: {self.stop_reason}")
        
        # Send Telegram notification
        if self.config.notify_on_stop and self.telegram_callback:
            try:
                await self.telegram_callback({
                    'reason': 'CONSECUTIVE_ERRORS_KILL_SWITCH',
                    'details': self.stop_reason,
                    'action': 'BOT STOPPED - Manual restart required',
                    'timestamp': datetime.now(UTC).isoformat(),
                    'error_count': self.consecutive_errors,
                    'last_errors': [
                        f"[{e.source}] {e.message}" 
                        for e in self.error_history[-5:]
                    ]
                })
            except Exception as e:
                logger.error(f"Failed to send stop notification: {e}")
    
    def should_stop(self) -> bool:
        """Check if bot should stop"""
        return self.is_stopped
    
    def reset(self):
        """Reset error tracker (for manual restart)"""
        self.consecutive_errors = 0
        self.is_stopped = False
        self.stop_reason = None
        logger.info("ErrorTracker reset")
    
    def get_status(self) -> dict:
        """Get current error tracker status"""
        return {
            'consecutive_errors': self.consecutive_errors,
            'max_allowed': self.config.max_consecutive_errors,
            'total_errors': self.total_errors,
            'is_stopped': self.is_stopped,
            'stop_reason': self.stop_reason,
            'last_error': self.last_error_time.isoformat() if self.last_error_time else None,
            'last_success': self.last_success_time.isoformat() if self.last_success_time else None
        }


# Singleton instance for global access
_global_tracker: Optional[ErrorTracker] = None


def get_error_tracker() -> Optional[ErrorTracker]:
    """Get global error tracker instance"""
    return _global_tracker


def init_error_tracker(telegram_callback: Optional[Callable] = None,
                      config: Optional[ErrorTrackerConfig] = None) -> ErrorTracker:
    """Initialize global error tracker"""
    global _global_tracker
    _global_tracker = ErrorTracker(config, telegram_callback)
    return _global_tracker
