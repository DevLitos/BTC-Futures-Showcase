"""
Dependency Injection for FastAPI

This module provides dependency injection patterns compatible with FastAPI.
All singletons are accessible via get_* functions that can be used with Depends().

Usage with FastAPI:
    from fastapi import Depends
    from src.core.dependencies import get_signal_engine, get_settings
    
    @app.get("/status")
    async def get_status(
        engine: SignalEngine = Depends(get_signal_engine),
        settings: Settings = Depends(get_settings)
    ):
        return {"status": "ok", "mode": settings.TRADING_MODE}

Usage in tests:
    from src.core.dependencies import override_dependency, reset_overrides
    
    override_dependency('signal_engine', mock_engine)
    # ... run tests ...
    reset_overrides()
"""

from functools import lru_cache
from typing import Optional, Dict, Any, Callable, TypeVar
import logging

logger = logging.getLogger(__name__)

# Type variable for generic dependency
T = TypeVar('T')

# Store for dependency overrides (used in testing)
_dependency_overrides: Dict[str, Any] = {}


def override_dependency(name: str, instance: Any) -> None:
    """
    Override a dependency for testing.
    
    Args:
        name: The dependency name (e.g., 'signal_engine', 'settings')
        instance: The mock/test instance to use
    """
    _dependency_overrides[name] = instance
    logger.debug(f"Dependency override set: {name}")


def reset_overrides() -> None:
    """Reset all dependency overrides."""
    _dependency_overrides.clear()
    # Also clear lru_cache on all cached functions
    get_settings.cache_clear()
    get_event_bus.cache_clear()
    get_cost_model.cache_clear()
    get_kill_switch.cache_clear()
    get_health_status.cache_clear()
    get_health_bridge.cache_clear()
    get_signal_engine.cache_clear()
    get_risk_engine.cache_clear()
    get_filter_pipeline.cache_clear()
    get_entry_scorer.cache_clear()
    get_regime_detector.cache_clear()
    get_execution_engine.cache_clear()
    logger.info("All dependency overrides cleared")


def _get_or_override(name: str, factory: Callable[[], T]) -> T:
    """Get override if exists, otherwise call factory."""
    if name in _dependency_overrides:
        return _dependency_overrides[name]
    return factory()


# =============================================================================
# CORE DEPENDENCIES
# =============================================================================

@lru_cache(maxsize=1)
def get_settings():
    """
    Get application settings.
    
    Returns cached Settings instance (pydantic-settings based).
    """
    from src.core.settings import get_settings as _get_settings
    return _get_or_override('settings', _get_settings)


@lru_cache(maxsize=1)
def get_event_bus():
    """
    Get event bus for component coordination.
    
    Returns cached EventBus instance.
    """
    from src.core.events import get_event_bus as _get_event_bus
    return _get_or_override('event_bus', _get_event_bus)


@lru_cache(maxsize=1)
def get_cost_model():
    """
    Get cost model for fee/slippage calculations.
    
    Returns cached CostModel instance.
    """
    from src.core.cost_model import get_cost_model as _get_cost_model
    return _get_or_override('cost_model', _get_cost_model)


@lru_cache(maxsize=1)
def get_kill_switch():
    """
    Get kill switch for emergency stop.
    
    Returns cached KillSwitch instance.
    """
    from src.core.kill_switch import get_kill_switch as _get_kill_switch
    return _get_or_override('kill_switch', _get_kill_switch)


@lru_cache(maxsize=1)
def get_health_status():
    """
    Get health status tracker.
    
    Returns cached HealthStatus instance.
    """
    from src.core.health import get_health_status as _get_health_status
    return _get_or_override('health_status', _get_health_status)


@lru_cache(maxsize=1)
def get_health_bridge():
    """
    Get health bridge for API/bot communication.
    
    Returns cached HealthBridge instance.
    """
    from src.core.health_bridge import get_health_bridge as _get_health_bridge
    return _get_or_override('health_bridge', _get_health_bridge)


# =============================================================================
# ENGINE DEPENDENCIES
# =============================================================================

@lru_cache(maxsize=1)
def get_signal_engine():
    """
    Get the main signal engine.
    
    Returns cached SignalEngine instance.
    Note: In production, this should be initialized with proper config.
    """
    if 'signal_engine' in _dependency_overrides:
        return _dependency_overrides['signal_engine']
    
    # Lazy import to avoid circular dependencies
    from src.engine.signal_engine import SignalEngine
    
    # Note: SignalEngine requires async initialization, so we return class
    # The actual instance should be created in the app startup
    logger.warning("get_signal_engine() called but engine requires async init")
    return SignalEngine


@lru_cache(maxsize=1)
def get_risk_engine():
    """
    Get risk engine for position sizing.
    
    Returns cached RiskEngine instance.
    """
    from src.engine.risk.risk_engine import RiskEngine
    return _get_or_override('risk_engine', RiskEngine)


@lru_cache(maxsize=1)
def get_filter_pipeline():
    """
    Get entry filter pipeline.
    
    Returns cached EntryFilterPipeline instance.
    """
    from src.engine.filters.filter_pipeline import get_entry_filter_pipeline
    return _get_or_override('filter_pipeline', get_entry_filter_pipeline)


@lru_cache(maxsize=1)
def get_entry_scorer():
    """
    Get entry scorer for trade scoring.
    
    Returns cached EntryScorer instance.
    """
    from src.engine.scoring.entry_scorer import get_entry_scorer as _get_entry_scorer
    return _get_or_override('entry_scorer', _get_entry_scorer)


@lru_cache(maxsize=1)
def get_regime_detector():
    """
    Get regime detector.
    
    Returns cached RegimeDetector instance.
    """
    from src.engine.regime.regime_detector import RegimeDetector
    return _get_or_override('regime_detector', RegimeDetector)


@lru_cache(maxsize=1)
def get_execution_engine():
    """
    Get execution engine for order placement.
    
    Returns cached RESTExecutionEngine class.
    Note: Requires async initialization.
    """
    if 'execution_engine' in _dependency_overrides:
        return _dependency_overrides['execution_engine']
    
    from src.engine.execution.execution import RESTExecutionEngine
    return RESTExecutionEngine


# =============================================================================
# HELPER FUNCTIONS FOR FASTAPI
# =============================================================================

def create_app_dependencies():
    """
    Create all dependencies needed for app startup.
    
    Call this during FastAPI app lifespan startup.
    
    Returns:
        Dict with initialized dependencies
    """
    return {
        'settings': get_settings(),
        'event_bus': get_event_bus(),
        'cost_model': get_cost_model(),
        'kill_switch': get_kill_switch(),
    }


async def shutdown_dependencies():
    """
    Clean up dependencies during shutdown.
    
    Call this during FastAPI app lifespan shutdown.
    """
    reset_overrides()
    logger.info("Dependencies shutdown complete")


# =============================================================================
# DEPENDENCY INJECTION DECORATORS
# =============================================================================

def inject(*dependency_names: str):
    """
    Decorator to inject dependencies into a function.
    
    Usage:
        @inject('settings', 'event_bus')
        def my_function(settings, event_bus, other_arg):
            ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            for name in dependency_names:
                if name not in kwargs:
                    getter = globals().get(f'get_{name}')
                    if getter:
                        kwargs[name] = getter()
            return func(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Override functions
    'override_dependency',
    'reset_overrides',
    # Core dependencies
    'get_settings',
    'get_event_bus', 
    'get_cost_model',
    'get_kill_switch',
    'get_health_status',
    'get_health_bridge',
    # Engine dependencies
    'get_signal_engine',
    'get_risk_engine',
    'get_filter_pipeline',
    'get_entry_scorer',
    'get_regime_detector',
    'get_execution_engine',
    # Helpers
    'create_app_dependencies',
    'shutdown_dependencies',
    'inject',
]
