"""
Shared Module - Cross-cutting utilities.

Clean Architecture Layer: SHARED (cross-cutting)
- Logging configuration
- Time utilities
- Common decorators and helpers

Migration Status: ALIAS (re-exports from src.core)
"""

from src.bootstrap.logging import (
    configure_logging, get_audit_logger, set_session_id,
    LogContext, AuditLogger
)

__all__ = [
    'configure_logging', 'get_audit_logger', 'set_session_id',
    'LogContext', 'AuditLogger',
]
