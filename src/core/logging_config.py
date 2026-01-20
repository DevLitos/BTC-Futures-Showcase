"""
Production Logging Configuration - Structured, Secure, Auditable

This module provides production-grade logging with:
- Structured JSON output (optional, for production)
- Secret redaction in all log output
- Correlation IDs (run_id, session_id)
- Separate audit log for critical trading events
- Log rate limiting to prevent spam

USAGE:
    from src.core.logging_config import configure_logging, get_audit_logger
    
    # At startup (once):
    configure_logging(settings)
    
    # Normal logging (secrets auto-redacted):
    logger.info("Processing order", extra={"order_id": "123"})
    
    # Audit logging (separate file, always enabled):
    audit = get_audit_logger()
    audit.order_placed(symbol="BTC/USD", side="buy", amount=0.01)
"""

import os
import sys
import json
import logging
import logging.handlers
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, Dict, Any, Set
from dataclasses import dataclass, field
from functools import wraps
import threading
import time

from src.core.settings import Settings, redact_secrets, SECRET_PATTERNS


# =========================================================================
# Constants
# =========================================================================

LOG_FORMAT_TEXT = "%(asctime)s - %(name)-25s - %(levelname)-8s - [%(run_id)s] %(message)s"
LOG_FORMAT_JSON = None  # JSON formatter handles this


# =========================================================================
# Safe Stream Handler (ignores BrokenPipeError during shutdown)
# =========================================================================

class SafeStreamHandler(logging.StreamHandler):
    """
    A StreamHandler that gracefully handles BrokenPipeError.
    
    This occurs when piping output to commands like `head` or `grep` that
    close the pipe before the program finishes writing. Instead of crashing
    with ugly tracebacks, we silently ignore these errors.
    """
    
    def emit(self, record: logging.LogRecord) -> None:
        """Emit a record, ignoring BrokenPipeError."""
        try:
            super().emit(record)
        except BrokenPipeError:
            # Pipe closed (e.g., `python main.py | head`), ignore silently
            pass
        except OSError as e:
            # EPIPE or similar on some systems
            if e.errno == 32:  # EPIPE
                pass
            else:
                raise
    
    def flush(self) -> None:
        """Flush the stream, ignoring BrokenPipeError."""
        try:
            super().flush()
        except BrokenPipeError:
            pass
        except OSError as e:
            if e.errno == 32:  # EPIPE
                pass
            else:
                raise

AUDIT_LOG_NAME = "audit"
AUDIT_LOG_FILE = "audit.log"

# Rate limiting: max logs per source per interval
RATE_LIMIT_INTERVAL = 60  # seconds
RATE_LIMIT_MAX_LOGS = 10  # max logs per source per interval


# =========================================================================
# Context Variables (for correlation)
# =========================================================================

class LogContext:
    """Thread-local storage for log context (run_id, session_id, etc.)."""
    
    _local = threading.local()
    
    @classmethod
    def set(cls, **kwargs):
        """Set context variables."""
        if not hasattr(cls._local, "context"):
            cls._local.context = {}
        cls._local.context.update(kwargs)
    
    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """Get a context variable."""
        if not hasattr(cls._local, "context"):
            return default
        return cls._local.context.get(key, default)
    
    @classmethod
    def get_all(cls) -> dict:
        """Get all context variables."""
        if not hasattr(cls._local, "context"):
            return {}
        return cls._local.context.copy()
    
    @classmethod
    def clear(cls):
        """Clear all context variables."""
        cls._local.context = {}


# =========================================================================
# Secret Redacting Filter
# =========================================================================

class SecretRedactingFilter(logging.Filter):
    """
    Logging filter that redacts secrets from all log messages.
    
    This filter processes every log record and removes any sensitive data
    before it reaches the handlers (console, file).
    """
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Redact secrets from log record."""
        
        # Redact the message
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        
        # Redact args if they're strings
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(str(arg)) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        
        # Add context variables
        context = LogContext.get_all()
        record.run_id = context.get("run_id", "no-run-id")
        record.session_id = context.get("session_id", "-")
        
        return True


# =========================================================================
# Rate Limiting Filter
# =========================================================================

class RateLimitingFilter(logging.Filter):
    """
    Logging filter that rate-limits repetitive log messages.
    
    Prevents log spam from reconnection loops, heartbeat failures, etc.
    Groups messages by (logger name, message template) and limits to N per interval.
    """
    
    def __init__(self, interval: float = RATE_LIMIT_INTERVAL, max_logs: int = RATE_LIMIT_MAX_LOGS):
        super().__init__()
        self.interval = interval
        self.max_logs = max_logs
        self._counts: Dict[str, int] = {}
        self._windows: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def _get_key(self, record: logging.LogRecord) -> str:
        """Generate a key for grouping similar messages."""
        # Use logger name and first 50 chars of message template
        msg = str(record.msg)[:50] if record.msg else ""
        return f"{record.name}:{msg}"
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Apply rate limiting."""
        
        # Never rate-limit ERROR or CRITICAL
        if record.levelno >= logging.ERROR:
            return True
        
        key = self._get_key(record)
        now = time.time()
        
        with self._lock:
            window_start = self._windows.get(key, 0)
            
            # Reset window if expired
            if now - window_start > self.interval:
                self._windows[key] = now
                self._counts[key] = 0
                
                # Log suppression summary
                suppressed = self._suppressed.get(key, 0)
                if suppressed > 0:
                    record.msg = f"[+{suppressed} similar messages suppressed] {record.msg}"
                    self._suppressed[key] = 0
            
            self._counts[key] = self._counts.get(key, 0) + 1
            
            if self._counts[key] > self.max_logs:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False
        
        return True


# =========================================================================
# JSON Formatter
# =========================================================================

class JSONFormatter(logging.Formatter):
    """
    JSON log formatter for structured logging.
    
    Outputs each log record as a single JSON line, suitable for
    log aggregation systems (ELK, Loki, CloudWatch, etc.).
    """
    
    def format(self, record: logging.LogRecord) -> str:
        """Format record as JSON."""
        
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", "unknown"),
            "session_id": getattr(record, "session_id", "-"),
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, "__dict__"):
            for key, value in record.__dict__.items():
                if key not in (
                    "name", "msg", "args", "created", "filename", "funcName",
                    "levelname", "levelno", "lineno", "module", "msecs",
                    "pathname", "process", "processName", "relativeCreated",
                    "stack_info", "exc_info", "exc_text", "thread", "threadName",
                    "run_id", "session_id", "message"
                ):
                    try:
                        # Try to serialize, skip if not possible
                        json.dumps(value)
                        log_data[key] = value
                    except (TypeError, ValueError):
                        log_data[key] = str(value)
        
        return json.dumps(log_data)


# =========================================================================
# Audit Logger
# =========================================================================

@dataclass
class AuditEvent:
    """Structured audit event."""
    timestamp: datetime
    event_type: str
    run_id: str
    session_id: str
    data: Dict[str, Any]


class AuditLogger:
    """
    Dedicated audit logger for critical trading events.
    
    Audit events are:
    - Always logged (never rate-limited)
    - Written to a separate file
    - Structured for easy parsing
    - Include full context for reconstruction
    
    Events logged:
    - ORDER_PLACED: Every order placed on exchange
    - ORDER_CANCELLED: Every order cancellation
    - ORDER_FILLED: Order fill confirmations
    - POSITION_OPENED: Position entry
    - POSITION_CLOSED: Position exit
    - SL_SET: Stop loss set/modified
    - TP_SET: Take profit set/modified
    - KILL_SWITCH: Kill switch activated
    - RECONCILE_ERROR: State discrepancy detected
    - SYSTEM_START: System startup
    - SYSTEM_STOP: System shutdown
    """
    
    def __init__(self, log_path: Path, run_id: str):
        self.log_path = log_path
        self.run_id = run_id
        
        # Create dedicated logger
        self.logger = logging.getLogger(AUDIT_LOG_NAME)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # Don't propagate to root
        
        # File handler (JSON format, one event per line)
        log_file = log_path / AUDIT_LOG_FILE
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=10
        )
        handler.setFormatter(JSONFormatter())
        self.logger.addHandler(handler)
    
    def _log_event(self, event_type: str, **data):
        """Log an audit event."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "run_id": self.run_id,
            "session_id": LogContext.get("session_id", "-"),
            **data
        }
        self.logger.info(json.dumps(event))
    
    # Order events
    def order_placed(self, symbol: str, side: str, amount: float, price: Optional[float] = None,
                    order_type: str = "limit", client_order_id: Optional[str] = None, **extra):
        """Log order placement."""
        self._log_event(
            "ORDER_PLACED",
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            order_type=order_type,
            client_order_id=client_order_id,
            **extra
        )
    
    def order_cancelled(self, order_id: str, symbol: str, reason: str = "", **extra):
        """Log order cancellation."""
        self._log_event(
            "ORDER_CANCELLED",
            order_id=order_id,
            symbol=symbol,
            reason=reason,
            **extra
        )
    
    def order_filled(self, order_id: str, symbol: str, side: str, amount: float,
                    price: float, fill_type: str = "full", **extra):
        """Log order fill."""
        self._log_event(
            "ORDER_FILLED",
            order_id=order_id,
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            fill_type=fill_type,
            **extra
        )
    
    def order_failed(self, symbol: str, side: str, amount: float, error: str,
                    client_order_id: Optional[str] = None, **extra):
        """Log order failure."""
        self._log_event(
            "ORDER_FAILED",
            symbol=symbol,
            side=side,
            amount=amount,
            error=error,
            client_order_id=client_order_id,
            **extra
        )
    
    # Position events
    def position_opened(self, symbol: str, side: str, size: float, entry_price: float,
                       stop_loss: float, **extra):
        """Log position open."""
        self._log_event(
            "POSITION_OPENED",
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            stop_loss=stop_loss,
            **extra
        )
    
    def position_closed(self, symbol: str, side: str, size: float, entry_price: float,
                       exit_price: float, pnl: float, pnl_r: float, reason: str, **extra):
        """Log position close."""
        self._log_event(
            "POSITION_CLOSED",
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_r=pnl_r,
            reason=reason,
            **extra
        )
    
    # SL/TP events
    def sl_set(self, symbol: str, order_id: str, price: float, **extra):
        """Log stop loss set."""
        self._log_event(
            "SL_SET",
            symbol=symbol,
            order_id=order_id,
            price=price,
            **extra
        )
    
    def tp_set(self, symbol: str, order_id: str, price: float, **extra):
        """Log take profit set."""
        self._log_event(
            "TP_SET",
            symbol=symbol,
            order_id=order_id,
            price=price,
            **extra
        )
    
    # System events
    def kill_switch_activated(self, reason: str, **extra):
        """Log kill switch activation."""
        self._log_event(
            "KILL_SWITCH",
            reason=reason,
            **extra
        )
    
    def reconcile_error(self, expected: Any, actual: Any, action: str, **extra):
        """Log reconciliation error."""
        self._log_event(
            "RECONCILE_ERROR",
            expected=str(expected),
            actual=str(actual),
            action=action,
            **extra
        )
    
    def system_start(self, mode: str, exchange: str, **extra):
        """Log system startup."""
        self._log_event(
            "SYSTEM_START",
            mode=mode,
            exchange=exchange,
            **extra
        )
    
    def system_stop(self, reason: str, **extra):
        """Log system shutdown."""
        self._log_event(
            "SYSTEM_STOP",
            reason=reason,
            **extra
        )


# =========================================================================
# Global Configuration
# =========================================================================

_audit_logger: Optional[AuditLogger] = None


def configure_logging(settings: Settings) -> None:
    """
    Configure logging for the entire application.
    
    This should be called once at startup, before any other logging.
    
    Args:
        settings: Application settings
    """
    global _audit_logger
    
    # Set context for all logs
    LogContext.set(run_id=settings.run_id)
    
    # Create log directory
    settings.log_path.mkdir(parents=True, exist_ok=True)
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level))
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Add filters
    secret_filter = SecretRedactingFilter()
    rate_limit_filter = RateLimitingFilter()
    
    # Console handler (uses SafeStreamHandler to handle pipe errors gracefully)
    console_handler = SafeStreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.log_level))
    console_handler.addFilter(secret_filter)
    console_handler.addFilter(rate_limit_filter)
    
    if settings.log_format == "json":
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT_TEXT))
    
    root_logger.addHandler(console_handler)
    
    # File handler (always text for readability)
    main_log_file = settings.log_path / f"trading_{settings.run_id}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        main_log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)  # Capture all levels in file
    file_handler.addFilter(secret_filter)
    # No rate limiting on file (we want complete logs)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT_TEXT))
    root_logger.addHandler(file_handler)
    
    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    
    # Initialize audit logger
    _audit_logger = AuditLogger(settings.log_path, settings.run_id)
    
    # Log startup
    _audit_logger.system_start(
        mode=settings.trading_mode.value,
        exchange=settings.exchange.value,
        version=settings.app_version,
        git_commit=settings.git_commit
    )
    
    logging.getLogger(__name__).info(
        f"Logging configured: level={settings.log_level}, format={settings.log_format}, "
        f"path={settings.log_path}"
    )


def get_audit_logger() -> AuditLogger:
    """
    Get the global audit logger.
    
    Raises:
        RuntimeError: If logging hasn't been configured yet
        
    Returns:
        The audit logger instance
    """
    if _audit_logger is None:
        raise RuntimeError(
            "Audit logger not initialized. Call configure_logging() first."
        )
    return _audit_logger


def set_session_id(session_id: str) -> None:
    """Set the current session ID for log correlation."""
    LogContext.set(session_id=session_id)
