"""
Runtime Mode Configuration - CRITICAL GUARDRAILS

This module enforces strict separation between LIVE, PAPER, and BACKTEST modes.
It is designed to PREVENT the catastrophic bugs that arise from mixing modes:
- Using backtest equity in live
- Instant fills in live
- Skipping exchange reconciliation

USAGE:
    from src.bootstrap.runtime import init_runtime, get_runtime_config, RuntimeMode
    
    # At startup (once):
    init_runtime(RuntimeMode.LIVE)
    
    # Anywhere else:
    config = get_runtime_config()
    if config.mode == RuntimeMode.LIVE:
        equity = await exchange.get_balance()  # REQUIRED in live
    
GUARDRAILS:
    - In LIVE mode, instant_fill=True will raise AssertionError
    - In LIVE mode, equity_source must be "exchange"
    - In LIVE mode, certain log patterns are forbidden
    - RuntimeConfig is immutable after initialization
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Set, Callable
from datetime import datetime, UTC
import logging
import os

logger = logging.getLogger(__name__)


class RuntimeMode(Enum):
    """
    Trading system runtime modes with strict behavioral contracts.
    
    LIVE: Real money, real exchange, full reconciliation, no shortcuts
    
    PAPER: ⚠️ MISLEADING NAME - Currently executes REAL orders on exchange!
           Real data, real orders, but simulated equity tracking.
           Use this for testing with small real positions.
           TODO: Rename to LIVE_MICRO or implement true paper trading.
    
    BACKTEST: Historical data, simulated execution, can use shortcuts
    """
    LIVE = "LIVE"
    PAPER = "PAPER"  # WARNING: Still executes real orders! See docstring.
    BACKTEST = "BACKTEST"


@dataclass(frozen=True)
class RuntimeConfig:
    """
    Immutable runtime configuration that governs all system behavior.
    
    Once set, this CANNOT be changed. This prevents accidental mode switches
    that could cause catastrophic losses.
    
    Attributes:
        mode: The runtime mode (LIVE/PAPER/BACKTEST)
        instant_fill: Whether orders fill instantly (FORBIDDEN in LIVE)
        equity_source: Where equity comes from ("exchange" or "simulated")
        reconcile_with_exchange: Whether to reconcile state with exchange
        reduce_only_required: Whether SL/TP MUST be reduce-only
        log_pnl_enabled: Whether to log PnL (may be disabled in LIVE for security)
        max_position_usd: Maximum position size in USD (safety limit)
        forbidden_log_patterns: Patterns that MUST NOT appear in LIVE logs
    """
    mode: RuntimeMode
    
    # Execution behavior
    instant_fill: bool = False
    simulated_slippage_bps: float = 5.0  # Only used if instant_fill=True
    
    # Data sources
    equity_source: str = "exchange"  # "exchange" or "simulated"
    position_source: str = "exchange"  # "exchange" or "local"
    
    # Safety mechanisms
    reconcile_with_exchange: bool = True
    reconcile_interval_seconds: float = 5.0
    reduce_only_required: bool = True
    
    # Logging
    log_pnl_enabled: bool = True
    log_equity_enabled: bool = True
    
    # Safety limits
    max_position_usd: float = 10000.0
    max_daily_trades: int = 100
    max_daily_loss_pct: float = 3.0
    
    # Forbidden patterns (will raise in LIVE if detected)
    forbidden_log_patterns: tuple = field(default_factory=lambda: (
        "backtest",
        "simulated",
        "instant_fill",
        "mock",
        "$10000",  # Default backtest equity
        "equity=10000",
    ))
    
    def __post_init__(self):
        """Validate configuration integrity - called automatically."""
        self._validate()
    
    def _validate(self):
        """
        CRITICAL: Validate that configuration is safe for the mode.
        Raises AssertionError if configuration would be dangerous.
        """
        if self.mode == RuntimeMode.LIVE:
            # LIVE MODE GUARDRAILS - These are NON-NEGOTIABLE
            assert not self.instant_fill, \
                "🚨 FATAL: instant_fill=True in LIVE mode is FORBIDDEN"
            
            assert self.equity_source == "exchange", \
                f"🚨 FATAL: equity_source must be 'exchange' in LIVE, got '{self.equity_source}'"
            
            assert self.position_source == "exchange", \
                f"🚨 FATAL: position_source must be 'exchange' in LIVE, got '{self.position_source}'"
            
            assert self.reconcile_with_exchange, \
                "🚨 FATAL: reconcile_with_exchange must be True in LIVE"
            
            assert self.reduce_only_required, \
                "🚨 FATAL: reduce_only_required must be True in LIVE"
            
            logger.info("✅ LIVE mode guardrails validated")
        
        elif self.mode == RuntimeMode.PAPER:
            # PAPER mode should behave like LIVE but with simulated execution
            assert self.equity_source in ("exchange", "simulated"), \
                f"Invalid equity_source for PAPER: {self.equity_source}"
            
            assert self.reduce_only_required, \
                "reduce_only_required should be True in PAPER for realism"
            
            logger.info("✅ PAPER mode guardrails validated")
        
        elif self.mode == RuntimeMode.BACKTEST:
            # BACKTEST mode can use shortcuts
            assert self.instant_fill or not self.reconcile_with_exchange, \
                "BACKTEST should use instant_fill or skip reconciliation"
            
            logger.info("✅ BACKTEST mode guardrails validated")
    
    def assert_not_forbidden_log(self, message: str) -> None:
        """
        Check if a log message contains forbidden patterns.
        Raises AssertionError in LIVE mode if pattern found.
        """
        if self.mode != RuntimeMode.LIVE:
            return
        
        message_lower = message.lower()
        for pattern in self.forbidden_log_patterns:
            if pattern.lower() in message_lower:
                raise AssertionError(
                    f"🚨 FORBIDDEN LOG PATTERN in LIVE: '{pattern}' found in: {message[:100]}"
                )
    
    def get_slippage_bps(self, volatility_percentile: float = 50.0) -> float:
        """
        Get slippage in basis points based on mode and market conditions.
        
        In LIVE: Returns 0 (slippage is REAL, measured post-hoc)
        In PAPER/BACKTEST: Returns simulated slippage adjusted for volatility
        """
        if self.mode == RuntimeMode.LIVE:
            return 0.0  # Real slippage, don't estimate
        
        # Volatility-adjusted slippage for simulation
        base_slippage = self.simulated_slippage_bps
        volatility_multiplier = 1.0 + (volatility_percentile - 50) / 100  # 0.5x to 1.5x
        return base_slippage * max(0.5, min(2.0, volatility_multiplier))


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================

_runtime_config: Optional[RuntimeConfig] = None
_initialized: bool = False


def init_runtime(
    mode: RuntimeMode,
    *,
    max_position_usd: float = 10000.0,
    max_daily_loss_pct: float = 3.0,
    simulated_slippage_bps: float = 5.0,
    **overrides
) -> RuntimeConfig:
    """
    Initialize the runtime configuration. MUST be called once at startup.
    
    Args:
        mode: RuntimeMode.LIVE, RuntimeMode.PAPER, or RuntimeMode.BACKTEST
        max_position_usd: Maximum position size (safety limit)
        max_daily_loss_pct: Maximum daily loss before halt
        simulated_slippage_bps: Simulated slippage for PAPER/BACKTEST
        **overrides: Additional config overrides (be careful!)
    
    Returns:
        RuntimeConfig: The initialized (immutable) configuration
    
    Raises:
        RuntimeError: If already initialized
        AssertionError: If configuration is invalid for the mode
    """
    global _runtime_config, _initialized
    
    if _initialized:
        raise RuntimeError(
            "🚨 Runtime already initialized! Cannot reinitialize. "
            "This is a safety feature to prevent mode switches during execution."
        )
    
    # Build mode-specific defaults
    if mode == RuntimeMode.LIVE:
        config_dict = {
            'mode': mode,
            'instant_fill': False,
            'equity_source': 'exchange',
            'position_source': 'exchange',
            'reconcile_with_exchange': True,
            'reduce_only_required': True,
            'max_position_usd': max_position_usd,
            'max_daily_loss_pct': max_daily_loss_pct,
        }
    elif mode == RuntimeMode.PAPER:
        config_dict = {
            'mode': mode,
            'instant_fill': False,  # Simulate realistic fills
            'equity_source': 'simulated',
            'position_source': 'local',
            'reconcile_with_exchange': False,
            'reduce_only_required': True,
            'simulated_slippage_bps': simulated_slippage_bps,
            'max_position_usd': max_position_usd,
            'max_daily_loss_pct': max_daily_loss_pct,
        }
    else:  # BACKTEST
        config_dict = {
            'mode': mode,
            'instant_fill': True,
            'equity_source': 'simulated',
            'position_source': 'local',
            'reconcile_with_exchange': False,
            'reduce_only_required': False,
            'simulated_slippage_bps': simulated_slippage_bps,
            'max_position_usd': float('inf'),  # No limit in backtest
            'max_daily_loss_pct': float('inf'),
        }
    
    # Apply overrides (with warnings)
    for key, value in overrides.items():
        if key in config_dict:
            if mode == RuntimeMode.LIVE:
                logger.warning(f"⚠️ Overriding LIVE config: {key}={value}")
            config_dict[key] = value
    
    # Create immutable config
    _runtime_config = RuntimeConfig(**config_dict)
    _initialized = True
    
    # Log the configuration
    logger.info(f"{'='*60}")
    logger.info(f"🚀 RUNTIME INITIALIZED: {mode.name}")
    logger.info(f"{'='*60}")
    logger.info(f"  instant_fill: {_runtime_config.instant_fill}")
    logger.info(f"  equity_source: {_runtime_config.equity_source}")
    logger.info(f"  position_source: {_runtime_config.position_source}")
    logger.info(f"  reconcile_with_exchange: {_runtime_config.reconcile_with_exchange}")
    logger.info(f"  reduce_only_required: {_runtime_config.reduce_only_required}")
    logger.info(f"  max_position_usd: ${_runtime_config.max_position_usd:,.2f}")
    logger.info(f"  max_daily_loss_pct: {_runtime_config.max_daily_loss_pct}%")
    logger.info(f"{'='*60}")
    
    return _runtime_config


def get_runtime_config() -> RuntimeConfig:
    """
    Get the current runtime configuration.
    
    Returns:
        RuntimeConfig: The active configuration
    
    Raises:
        RuntimeError: If runtime not initialized
    """
    if not _initialized or _runtime_config is None:
        raise RuntimeError(
            "🚨 Runtime not initialized! Call init_runtime() at startup."
        )
    return _runtime_config


def is_live() -> bool:
    """Check if running in LIVE mode."""
    return get_runtime_config().mode == RuntimeMode.LIVE


def is_paper() -> bool:
    """Check if running in PAPER mode."""
    return get_runtime_config().mode == RuntimeMode.PAPER


def is_backtest() -> bool:
    """Check if running in BACKTEST mode."""
    return get_runtime_config().mode == RuntimeMode.BACKTEST


def reset_runtime_for_testing():
    """
    Reset runtime state. ONLY for unit tests.
    
    This function is marked clearly as test-only and will log a warning
    if called. In production, the runtime should NEVER be reset.
    """
    global _runtime_config, _initialized
    logger.warning("⚠️ Resetting runtime configuration (test mode only)")
    _runtime_config = None
    _initialized = False


# =============================================================================
# ENVIRONMENT DETECTION
# =============================================================================

def detect_mode_from_env() -> RuntimeMode:
    """
    Detect runtime mode from environment variables.
    
    Environment variable: TRADING_MODE
    Values: LIVE, PAPER, BACKTEST (case insensitive)
    Default: BACKTEST (safest default)
    """
    mode_str = os.getenv('TRADING_MODE', 'BACKTEST').upper()
    
    mode_map = {
        'LIVE': RuntimeMode.LIVE,
        'PAPER': RuntimeMode.PAPER,
        'BACKTEST': RuntimeMode.BACKTEST,
        # Aliases
        'PRODUCTION': RuntimeMode.LIVE,
        'PROD': RuntimeMode.LIVE,
        'SIMULATION': RuntimeMode.PAPER,
        'SIM': RuntimeMode.PAPER,
        'TEST': RuntimeMode.BACKTEST,
    }
    
    if mode_str not in mode_map:
        logger.warning(f"Unknown TRADING_MODE '{mode_str}', defaulting to BACKTEST")
        return RuntimeMode.BACKTEST
    
    return mode_map[mode_str]


# =============================================================================
# DECORATORS FOR MODE-SPECIFIC BEHAVIOR
# =============================================================================

def live_only(func: Callable) -> Callable:
    """
    Decorator that ensures a function only runs in LIVE mode.
    In other modes, returns None without executing.
    """
    def wrapper(*args, **kwargs):
        if get_runtime_config().mode == RuntimeMode.LIVE:
            return func(*args, **kwargs)
        logger.debug(f"Skipping {func.__name__} (not in LIVE mode)")
        return None
    return wrapper


def backtest_only(func: Callable) -> Callable:
    """
    Decorator that ensures a function only runs in BACKTEST mode.
    """
    def wrapper(*args, **kwargs):
        if get_runtime_config().mode == RuntimeMode.BACKTEST:
            return func(*args, **kwargs)
        logger.debug(f"Skipping {func.__name__} (not in BACKTEST mode)")
        return None
    return wrapper


def not_in_live(func: Callable) -> Callable:
    """
    Decorator that PREVENTS a function from running in LIVE mode.
    Raises AssertionError if called in LIVE.
    """
    def wrapper(*args, **kwargs):
        config = get_runtime_config()
        if config.mode == RuntimeMode.LIVE:
            raise AssertionError(
                f"🚨 FORBIDDEN: {func.__name__} called in LIVE mode!"
            )
        return func(*args, **kwargs)
    return wrapper
