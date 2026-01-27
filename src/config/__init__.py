"""
Configuration Module - Application configuration.

Clean Architecture Layer: CONFIG (cross-cutting)
- Pydantic settings validation
- Runtime mode configuration
- Environment variable handling

Migration Status: ALIAS (re-exports from src.core)
"""

from src.config.settings import (
    Settings, TradingMode, ExchangeType,
    get_settings, reset_settings,
    redact_secrets, mask_secret,
    generate_client_order_id, validate_startup
)
from src.bootstrap.runtime import RuntimeMode, RuntimeConfig, get_runtime_config, init_runtime

__all__ = [
    # Settings
    'Settings', 'TradingMode', 'ExchangeType',
    'get_settings', 'reset_settings',
    'redact_secrets', 'mask_secret',
    'generate_client_order_id', 'validate_startup',
    # Runtime
    'RuntimeMode', 'RuntimeConfig', 'get_runtime_config', 'init_runtime',
]
