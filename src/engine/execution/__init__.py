"""
Execution Module - Order execution and exchange interaction

Reorganized from engine/ (Jan 20, 2026)

Components:
- RESTExecutionEngine: Main execution engine for orders
- ExchangeReconciler: Reconciliation between local and exchange state
- SlippageModel: Slippage estimation and modeling
"""

from src.engine.execution.execution import (
    RESTExecutionEngine,
    ExecutionError,
    OrderPlacementError,
    StopLossSetupError,
    TakeProfitSetupError,
    OrderRejectedError,
    ExchangeTimeoutError,
    InsufficientMarginError,
    PositionMismatchError,
    round_size_to_exchange,
    with_retry,
    KRAKEN_BTC_MIN_SIZE,
)
from src.engine.execution.exchange_reconciler import (
    ExchangeReconciler,
    ReconcilerState,
    PublicState,
    HardLockEvent,
    ExchangeSnapshot,
)
from src.engine.execution.slippage_model import (
    SlippageModel,
    SlippageConfig,
    SlippageMode,
    OrderType,
    get_slippage_model,
    calculate_worst_case_rr,
    AdaptiveSlippageTracker,
    get_adaptive_slippage_tracker,
)

__all__ = [
    # Execution
    'RESTExecutionEngine',
    'ExecutionError',
    'OrderPlacementError',
    'StopLossSetupError',
    'TakeProfitSetupError',
    'OrderRejectedError',
    'ExchangeTimeoutError',
    'InsufficientMarginError',
    'PositionMismatchError',
    'round_size_to_exchange',
    'with_retry',
    'KRAKEN_BTC_MIN_SIZE',
    # Reconciler
    'ExchangeReconciler',
    'ReconcilerState',
    'PublicState',
    'HardLockEvent',
    'ExchangeSnapshot',
    # Slippage
    'SlippageModel',
    'SlippageConfig',
    'SlippageMode',
    'OrderType',
    'get_slippage_model',
    'calculate_worst_case_rr',
    'AdaptiveSlippageTracker',
    'get_adaptive_slippage_tracker',
]
