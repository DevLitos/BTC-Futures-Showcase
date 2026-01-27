"""
Bootstrap - Composition Root

This module is the composition root for the application.
It handles:
- Dependency injection setup
- Runtime configuration
- Logging configuration
- Component wiring

Import all bootstrap functions from here:
    from src.bootstrap import get_settings, configure_logging, init_runtime
"""

from src.bootstrap.dependencies import (
    get_settings,
    get_event_bus,
    get_cost_model,
    get_kill_switch,
    get_health_status,
    get_health_bridge,
    get_signal_engine,
    get_risk_engine,
    get_filter_pipeline,
    get_entry_scorer,
    get_regime_detector,
    get_execution_engine,
    override_dependency,
    reset_overrides,
)
from src.bootstrap.runtime import (
    RuntimeMode,
    RuntimeConfig,
    init_runtime,
    get_runtime_config,
)
from src.bootstrap.logging import (
    configure_logging,
    get_audit_logger,
    set_session_id,
)

__all__ = [
    # Dependencies
    "get_settings",
    "get_event_bus",
    "get_cost_model",
    "get_kill_switch",
    "get_health_status",
    "get_health_bridge",
    "get_signal_engine",
    "get_risk_engine",
    "get_filter_pipeline",
    "get_entry_scorer",
    "get_regime_detector",
    "get_execution_engine",
    "override_dependency",
    "reset_overrides",
    # Runtime
    "RuntimeMode",
    "RuntimeConfig",
    "init_runtime",
    "get_runtime_config",
    # Logging
    "configure_logging",
    "get_audit_logger",
    "set_session_id",
]
