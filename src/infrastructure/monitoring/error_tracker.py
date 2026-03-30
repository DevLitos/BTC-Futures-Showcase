"""
Error Tracker - Advanced error detection, rate limiting, and kill switch
PRODUCTION SAFETY: 3 consecutive critical errors → stop bot + Telegram alert

Features:
- Consecutive error detection with configurable threshold
- Error rate limiting (max errors per time window)
- Error categorization and severity levels
- Automatic recovery detection
- Detailed error history with deduplication
- Health scoring for monitoring dashboards
"""

import logging
from datetime import datetime, timedelta, UTC
from typing import Optional, Callable, Awaitable, Dict, List
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import hashlib

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    DEBUG = "debug"          # Trace only, no counting
    WARNING = "warning"      # Log only, no counting
    ERROR = "error"          # Count toward consecutive
    CRITICAL = "critical"    # Immediate action, high priority


class ErrorCategory(Enum):
    """Error categories for better analysis"""
    NETWORK = "network"              # Connection, timeout errors
    EXCHANGE = "exchange"            # Exchange API errors
    DATA = "data"                    # Data validation, parsing errors
    EXECUTION = "execution"          # Order execution errors
    INTERNAL = "internal"            # Internal logic errors
    CONFIGURATION = "configuration"  # Config/settings errors
    UNKNOWN = "unknown"              # Uncategorized


@dataclass
class ErrorEvent:
    timestamp: datetime
    severity: ErrorSeverity
    source: str
    message: str
    details: Optional[str] = None
    category: ErrorCategory = ErrorCategory.UNKNOWN
    error_hash: Optional[str] = None  # For deduplication
    
    def __post_init__(self):
        # Generate hash for deduplication
        if self.error_hash is None:
            hash_input = f"{self.source}:{self.message[:100]}"
            self.error_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]


@dataclass
class ErrorTrackerConfig:
    """Configuration for error tracking"""
    max_consecutive_errors: int = 3           # Stop after this many consecutive
    error_window_seconds: int = 300           # 5 min window for rate limiting
    max_errors_in_window: int = 10            # Max errors before throttling
    reset_on_success: bool = True             # Reset counter on successful operation
    notify_on_stop: bool = True               # Send Telegram on stop
    notify_on_recovery: bool = True           # Send Telegram on recovery
    dedup_window_seconds: int = 60            # Deduplicate same errors within window
    health_decay_seconds: int = 900           # Health score decay period (15 min)


class ErrorTracker:
    """
    Advanced error tracking with consecutive detection, rate limiting, and kill switch.
    
    Features:
    - Consecutive error counting with configurable threshold
    - Error rate limiting to prevent log spam
    - Error deduplication within time windows
    - Health scoring (0-100) for monitoring
    - Automatic recovery detection and notification
    - Detailed error history with categorization
    
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
        self.total_successes: int = 0
        self.error_history: List[ErrorEvent] = []
        self.is_stopped: bool = False
        self.stop_reason: Optional[str] = None
        self.last_error_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self.start_time: datetime = datetime.now(UTC)
        
        # Rate limiting
        self._error_timestamps: List[datetime] = []
        self._recent_hashes: Dict[str, datetime] = {}  # hash -> last seen
        
        # Error categorization
        self._errors_by_category: Dict[ErrorCategory, int] = defaultdict(int)
        self._errors_by_source: Dict[str, int] = defaultdict(int)
        
        # Recovery tracking
        self._was_degraded: bool = False
        self._recovery_count: int = 0
        
        logger.info(f"ErrorTracker initialized: max_consecutive={self.config.max_consecutive_errors}, "
                   f"rate_limit={self.config.max_errors_in_window}/{self.config.error_window_seconds}s")
    
    def _classify_error(self, source: str, message: str) -> ErrorCategory:
        """Auto-classify error category based on source and message"""
        msg_lower = message.lower()
        src_lower = source.lower()
        
        # Network errors
        if any(kw in msg_lower for kw in ['timeout', 'connection', 'network', 'socket', 'dns']):
            return ErrorCategory.NETWORK
        
        # Exchange errors
        if any(kw in msg_lower for kw in ['exchange', 'api', 'rate limit', 'insufficient', 'margin']):
            return ErrorCategory.EXCHANGE
        if any(kw in src_lower for kw in ['exchange', 'ccxt', 'binance', 'kraken']):
            return ErrorCategory.EXCHANGE
        
        # Execution errors
        if any(kw in msg_lower for kw in ['order', 'position', 'fill', 'execution']):
            return ErrorCategory.EXECUTION
        if any(kw in src_lower for kw in ['execution', 'order', 'trade']):
            return ErrorCategory.EXECUTION
        
        # Data errors
        if any(kw in msg_lower for kw in ['parse', 'invalid', 'missing', 'null', 'nan']):
            return ErrorCategory.DATA
        
        # Configuration errors
        if any(kw in msg_lower for kw in ['config', 'setting', 'parameter']):
            return ErrorCategory.CONFIGURATION
        
        return ErrorCategory.UNKNOWN
    
    def _is_duplicate(self, error_hash: str) -> bool:
        """Check if this error is a duplicate within the dedup window"""
        now = datetime.now(UTC)
        if error_hash in self._recent_hashes:
            last_seen = self._recent_hashes[error_hash]
            if (now - last_seen).total_seconds() < self.config.dedup_window_seconds:
                return True
        self._recent_hashes[error_hash] = now
        # Clean old hashes
        cutoff = now - timedelta(seconds=self.config.dedup_window_seconds * 2)
        self._recent_hashes = {h: t for h, t in self._recent_hashes.items() if t > cutoff}
        return False
    
    def _is_rate_limited(self) -> bool:
        """Check if we should rate limit error recording"""
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.config.error_window_seconds)
        self._error_timestamps = [t for t in self._error_timestamps if t > cutoff]
        return len(self._error_timestamps) >= self.config.max_errors_in_window
    
    async def record_error(self, source: str, message: str, 
                          severity: ErrorSeverity = ErrorSeverity.ERROR,
                          details: Optional[str] = None,
                          category: Optional[ErrorCategory] = None) -> bool:
        """
        Record an error event.
        
        Args:
            source: Error source (component name)
            message: Error message
            severity: Error severity level
            details: Optional detailed error info
            category: Optional error category (auto-detected if not provided)
        
        Returns: True if bot should stop (threshold reached)
        """
        now = datetime.now(UTC)
        
        # Auto-classify if not provided
        if category is None:
            category = self._classify_error(source, message)
        
        event = ErrorEvent(
            timestamp=now,
            severity=severity,
            source=source,
            message=message,
            details=details,
            category=category
        )
        
        # Check for duplicate
        if event.error_hash is not None and self._is_duplicate(event.error_hash):
            logger.debug(f"Duplicate error suppressed: [{source}] {message[:50]}...")
            return False
        
        # Check rate limiting
        if self._is_rate_limited():
            if severity != ErrorSeverity.CRITICAL:
                logger.warning(f"Error rate limited: [{source}] {message[:50]}...")
                return False
        
        self._error_timestamps.append(now)
        self.error_history.append(event)
        self.last_error_time = now
        self.total_errors += 1
        self._errors_by_category[category] += 1
        self._errors_by_source[source] += 1
        
        # Keep history bounded
        if len(self.error_history) > 1000:
            self.error_history = self.error_history[-500:]
        
        # Only count ERROR and CRITICAL toward consecutive
        if severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL):
            self.consecutive_errors += 1
            self._was_degraded = True
            
            emoji = "🔴" if severity == ErrorSeverity.CRITICAL else "⚠️"
            logger.error(f"{emoji} ERROR #{self.consecutive_errors}/{self.config.max_consecutive_errors}: "
                        f"[{source}] {message} (cat={category.value})")
            
            # Check if we should stop
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                await self._trigger_stop(source, message)
                return True
        elif severity == ErrorSeverity.WARNING:
            logger.warning(f"⚠️ WARNING: [{source}] {message}")
        
        return False
    
    def record_success(self, operation: str = "general"):
        """Record a successful operation - resets consecutive error counter"""
        was_degraded = self._was_degraded
        
        if self.config.reset_on_success and self.consecutive_errors > 0:
            logger.info(f"✅ Success after {self.consecutive_errors} errors - resetting counter")
            self.consecutive_errors = 0
        
        self.last_success_time = datetime.now(UTC)
        self.total_successes += 1
        
        # Check for recovery
        if was_degraded and self.consecutive_errors == 0:
            self._was_degraded = False
            self._recovery_count += 1
            logger.info(f"🔄 RECOVERED from error state (recovery #{self._recovery_count})")
    
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
                    'health_score': self.get_health_score(),
                    'error_breakdown': dict(self._errors_by_category),
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
    
    def get_health_score(self) -> int:
        """
        Calculate health score (0-100) based on error history.
        
        100 = Perfect (no recent errors)
        0 = Critical (at or near kill threshold)
        """
        now = datetime.now(UTC)
        
        # Base score starts at 100
        score = 100.0
        
        # Deduct for consecutive errors (major impact)
        if self.consecutive_errors > 0:
            score -= self.consecutive_errors * 25
        
        # Deduct for recent errors in window
        cutoff = now - timedelta(seconds=self.config.error_window_seconds)
        recent_errors = sum(1 for e in self.error_history if e.timestamp > cutoff)
        score -= recent_errors * 5
        
        # Bonus for successful operations (capped)
        if self.total_successes > 0 and self.total_errors > 0:
            success_ratio = self.total_successes / (self.total_successes + self.total_errors)
            score += min(10, success_ratio * 15)
        
        # Time since last error bonus
        if self.last_error_time:
            seconds_since_error = (now - self.last_error_time).total_seconds()
            score += min(10, seconds_since_error / 60)  # +1 per minute, max +10
        
        return max(0, min(100, int(score)))
    
    def reset(self):
        """Reset error tracker (for manual restart)"""
        self.consecutive_errors = 0
        self.is_stopped = False
        self.stop_reason = None
        self._was_degraded = False
        logger.info("ErrorTracker reset")
    
    def get_status(self) -> dict:
        """Get current error tracker status with full details"""
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.config.error_window_seconds)
        recent_errors = [e for e in self.error_history if e.timestamp > cutoff]
        
        return {
            'consecutive_errors': self.consecutive_errors,
            'max_allowed': self.config.max_consecutive_errors,
            'total_errors': self.total_errors,
            'total_successes': self.total_successes,
            'is_stopped': self.is_stopped,
            'stop_reason': self.stop_reason,
            'health_score': self.get_health_score(),
            'uptime_seconds': (now - self.start_time).total_seconds(),
            'recovery_count': self._recovery_count,
            'errors_in_window': len(recent_errors),
            'error_breakdown': {k.value: v for k, v in self._errors_by_category.items() if v > 0},
            'top_error_sources': dict(sorted(self._errors_by_source.items(), key=lambda x: -x[1])[:5]),
            'last_error': self.last_error_time.isoformat() if self.last_error_time else None,
            'last_success': self.last_success_time.isoformat() if self.last_success_time else None
        }
    
    def get_recent_errors(self, limit: int = 10) -> List[dict]:
        """Get recent errors as dictionaries for logging/display"""
        return [
            {
                'timestamp': e.timestamp.isoformat(),
                'severity': e.severity.value,
                'source': e.source,
                'message': e.message[:200],  # Truncate long messages
                'category': e.category.value
            }
            for e in self.error_history[-limit:]
        ]
    
    def log_status(self):
        """Log current status for monitoring"""
        status = self.get_status()
        health = status['health_score']
        health_emoji = "🟢" if health >= 80 else "🟡" if health >= 50 else "🔴"
        
        logger.info(f"ErrorTracker Status: {health_emoji} Health={health}% | "
                   f"Consecutive={status['consecutive_errors']}/{status['max_allowed']} | "
                   f"Total={status['total_errors']} errors, {status['total_successes']} successes | "
                   f"Recoveries={status['recovery_count']}")


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


def record_error_sync(source: str, message: str, 
                     severity: ErrorSeverity = ErrorSeverity.ERROR,
                     category: Optional[ErrorCategory] = None) -> bool:
    """
    Synchronous convenience function to record an error.
    Returns True if bot should stop (only when run synchronously).
    
    Usage:
        from src.infrastructure.monitoring.error_tracker import record_error_sync, ErrorSeverity
        
        if record_error_sync("my_module", "Something went wrong"):
            return  # Bot should stop
    
    Note: When called from async context, this returns False immediately
    and schedules the error recording as a background task.
    """
    tracker = get_error_tracker()
    if tracker is None:
        logger.warning(f"ErrorTracker not initialized, logging error: [{source}] {message}")
        return False
    
    # Run async in sync context (for non-async code paths)
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        # Track task properly to prevent GC and enable exception handling
        # If we're in an async context, create task but return False immediately
        # The error will be recorded asynchronously
        task = asyncio.create_task(
            tracker.record_error(source, message, severity, category=category),
            name=f"error_record_{source}"
        )
        
        def _on_done(t):
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    # Can't use logger here as it might cause recursion
                    print(f"[ErrorTracker] Background task failed: {exc}")
        
        task.add_done_callback(_on_done)
        return False  # Can't know result synchronously, assume no stop needed
    except RuntimeError:
        # No event loop running, run sync
        return asyncio.run(tracker.record_error(source, message, severity, category=category))
