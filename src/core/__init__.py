"""
Core module - Foundation types and runtime configuration.

This module provides the fundamental building blocks for the trading system:
- RuntimeMode: LIVE/PAPER/BACKTEST configuration with strict guardrails
- Types: Explicit dataclasses for all domain objects
- Events: Simple event bus for component coordination
- CostModel: Realistic transaction cost modeling
- Settings: Centralized configuration with pydantic-settings (NEW)
- LoggingConfig: Structured logging with secret redaction (NEW)
- KillSwitch: Hot trading pause mechanism (NEW)
- Health: Liveness/readiness endpoints (NEW)
- HealthBridge: Cross-process health sharing for FastAPI (NEW Jan 20, 2026)
"""

from .runtime_mode import RuntimeMode, RuntimeConfig, get_runtime_config, init_runtime
from .types import (
    MarketSnapshot, FeatureVector, TradeIntent, OrderIntent,
    ExecutionReport, PositionSnapshot, OrderStatus, Side, OrderType,
    FillType, TradeResult
)
from .events import EventBus, Event, EventType, get_event_bus
from .cost_model import (
    CostModel, CostEstimate, SlippageModel, FundingModel,
    get_cost_model, KRAKEN_FUTURES_FEES, BINANCE_FUTURES_FEES
)

# New production-grade modules
from .settings import (
    Settings, TradingMode, ExchangeType,
    get_settings, reset_settings,
    redact_secrets, mask_secret,
    generate_client_order_id, validate_startup
)
from .logging_config import (
    configure_logging, get_audit_logger, set_session_id,
    LogContext, AuditLogger
)
from .kill_switch import (
    KillSwitch, KillSwitchState, KillSwitchReason,
    get_kill_switch, init_kill_switch, check_kill_switch
)
from .health import (
    HealthStatus, HealthHandler,
    start_health_server, init_health_status, get_health_status
)
from .health_bridge import (
    HealthBridge, HealthBridgeWriter, BridgedHealthStatus,
    get_health_bridge
)

__all__ = [
    # Runtime
    'RuntimeMode', 'RuntimeConfig', 'get_runtime_config', 'init_runtime',
    # Types
    'MarketSnapshot', 'FeatureVector', 'TradeIntent', 'OrderIntent',
    'ExecutionReport', 'PositionSnapshot', 'OrderStatus', 'Side', 'OrderType',
    'FillType', 'TradeResult',
    # Events
    'EventBus', 'Event', 'EventType', 'get_event_bus',
    # Cost Model
    'CostModel', 'CostEstimate', 'SlippageModel', 'FundingModel',
    'get_cost_model', 'KRAKEN_FUTURES_FEES', 'BINANCE_FUTURES_FEES',
    # Settings (NEW)
    'Settings', 'TradingMode', 'ExchangeType',
    'get_settings', 'reset_settings',
    'redact_secrets', 'mask_secret',
    'generate_client_order_id', 'validate_startup',
    # Logging (NEW)
    'configure_logging', 'get_audit_logger', 'set_session_id',
    'LogContext', 'AuditLogger',
    # Kill Switch (NEW)
    'KillSwitch', 'KillSwitchState', 'KillSwitchReason',
    'get_kill_switch', 'init_kill_switch', 'check_kill_switch',
    # Health (NEW)
    'HealthStatus', 'HealthHandler',
    'start_health_server', 'init_health_status', 'get_health_status',
    # Health Bridge (NEW Jan 20, 2026)
    'HealthBridge', 'HealthBridgeWriter', 'BridgedHealthStatus',
    'get_health_bridge',
]
