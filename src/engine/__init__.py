"""
Engine Module - Core trading engine components

This module contains the main trading logic including:
- SignalEngine: Main orchestrator
- Filters: Entry/exit filtering
- Regime: Market regime classification  
- Edge: Edge enforcement and validation
- Risk: Position sizing and risk management
- State: State machines
- Execution: Order execution
- Scoring: Entry scoring
- FilterChecks: Pure filter functions (extracted)
- IndicatorHelpers: Indicator calculations (extracted)
"""

from src.engine.signal_engine import SignalEngine
from src.engine.config import OperatingMode, get_session_name

# Extracted helper modules for testing and reuse
from src.engine.filter_checks import (
    check_trend_filter,
    check_momentum_filter,
    check_volatility_expansion,
    check_anti_reversal,
    check_volume_delta,
    check_loss_cooldown,
    check_atr_regime,
    check_expected_rr,
    check_bos_against_position,
)
from src.engine.indicator_helpers import (
    CachedIndicators,
    ts_to_int,
    calculate_atr_1m,
    calculate_atr_5m,
    calculate_atr_ratio,
    calculate_adx_value,
    calculate_rsi_value,
    get_indicator_cache,
    reset_indicator_cache,
)

__all__ = [
    # Main engine
    'SignalEngine',
    'OperatingMode',
    'get_session_name',
    # Filter checks (pure functions)
    'check_trend_filter',
    'check_momentum_filter',
    'check_volatility_expansion',
    'check_anti_reversal',
    'check_volume_delta',
    'check_loss_cooldown',
    'check_atr_regime',
    'check_expected_rr',
    'check_bos_against_position',
    # Indicator helpers
    'CachedIndicators',
    'ts_to_int',
    'calculate_atr_1m',
    'calculate_atr_5m',
    'calculate_atr_ratio',
    'calculate_adx_value',
    'calculate_rsi_value',
    'get_indicator_cache',
    'reset_indicator_cache',
]
