"""
Design Patterns - Reusable pattern implementations

╔══════════════════════════════════════════════════════════════════════════════╗
║ 📦 CENTRALIZED PATTERNS                                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ This module provides reusable design pattern implementations:                ║
║ • @singleton - Thread-safe singleton decorator                              ║
║ • @retry_with_backoff - Exponential backoff for retries                     ║
║                                                                              ║
║ Usage:                                                                       ║
║   from src.shared.patterns import singleton                                  ║
║   @singleton                                                                 ║
║   class MyClass:                                                             ║
║       pass                                                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

Created: Feb 05, 2026
Reason: Singleton pattern was duplicated in 15+ files
"""

import functools
import threading
import asyncio
import logging
from typing import TypeVar, Type, Callable, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# SINGLETON PATTERN
# =============================================================================

def singleton(cls: Type[T]) -> Type[T]:
    """
    Thread-safe singleton decorator.
    
    Ensures only one instance of the class exists. The instance is created
    lazily on first access and cached for subsequent accesses.
    
    Usage:
        @singleton
        class MyService:
            def __init__(self, config: dict):
                self.config = config
        
        # First call creates instance
        service1 = MyService({'key': 'value'})
        
        # Subsequent calls return same instance (args ignored)
        service2 = MyService()  # Same as service1
        
        # Reset for testing
        MyService._reset_instance()
    
    Note:
        - Arguments to __init__ are only used on first instantiation
        - Use _reset_instance() for testing to clear the singleton
    """
    instances: dict = {}
    lock = threading.Lock()
    
    @functools.wraps(cls)
    def wrapper(*args, **kwargs):
        if cls not in instances:
            with lock:
                # Double-check locking
                if cls not in instances:
                    instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    
    # Add reset method for testing
    def reset_instance():
        with lock:
            if cls in instances:
                del instances[cls]
    
    # Add get method that doesn't create new instance
    def get_instance() -> Optional[T]:
        return instances.get(cls)
    
    # Attach methods to wrapper (type: ignore for dynamic attribute assignment)
    wrapper._reset_instance = reset_instance  # type: ignore[attr-defined]
    wrapper._get_instance = get_instance  # type: ignore[attr-defined]
    wrapper._original_class = cls  # type: ignore[attr-defined]
    
    return wrapper  # type: ignore[return-value]


class Singleton(type):
    """
    Metaclass-based singleton for when decorator syntax doesn't work.
    
    Usage:
        class MyClass(metaclass=Singleton):
            def __init__(self):
                pass
    """
    _instances: dict = {}
    _lock = threading.Lock()
    
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]
    
    @classmethod
    def reset(mcs, cls):
        """Reset a specific singleton instance."""
        with mcs._lock:
            if cls in mcs._instances:
                del mcs._instances[cls]


# =============================================================================
# RETRY PATTERN
# =============================================================================

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None
):
    """
    Decorator for retrying functions with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries
        exponential_base: Base for exponential backoff (delay = base_delay * base^attempt)
        exceptions: Tuple of exception types to catch and retry
        on_retry: Optional callback called before each retry with (exception, attempt)
    
    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        def fetch_data():
            return requests.get(url)
        
        @retry_with_backoff(max_retries=5, exceptions=(ConnectionError,))
        async def async_fetch():
            async with session.get(url) as resp:
                return await resp.json()
    """
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exception: Optional[Exception] = None
                for attempt in range(max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e
                        if attempt < max_retries:
                            delay = min(base_delay * (exponential_base ** attempt), max_delay)
                            if on_retry:
                                on_retry(e, attempt + 1)
                            logger.warning(
                                f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                                f"after {delay:.1f}s: {e}"
                            )
                            await asyncio.sleep(delay)
                        else:
                            raise
                if last_exception:
                    raise last_exception
                raise RuntimeError("Retry loop completed without success or exception")
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                import time
                last_exception: Optional[Exception] = None
                for attempt in range(max_retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e
                        if attempt < max_retries:
                            delay = min(base_delay * (exponential_base ** attempt), max_delay)
                            if on_retry:
                                on_retry(e, attempt + 1)
                            logger.warning(
                                f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                                f"after {delay:.1f}s: {e}"
                            )
                            time.sleep(delay)
                        else:
                            raise
                if last_exception:
                    raise last_exception
                raise RuntimeError("Retry loop completed without success or exception")
            return sync_wrapper
    return decorator


# =============================================================================
# TIMEOUT PATTERN
# =============================================================================

def with_timeout(timeout_seconds: float, default: Any = None):
    """
    Decorator for async functions with timeout.
    
    Args:
        timeout_seconds: Maximum time to wait
        default: Value to return on timeout (if None, raises TimeoutError)
    
    Usage:
        @with_timeout(30.0, default=None)
        async def fetch_data():
            return await slow_operation()
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.error(f"⏰ TIMEOUT after {timeout_seconds}s in {func.__name__}")
                if default is None:
                    raise
                return default
        return wrapper
    return decorator


# =============================================================================
# RATE LIMITER
# =============================================================================

class RateLimiter:
    """
    Simple rate limiter with sliding window.
    
    Usage:
        limiter = RateLimiter(max_calls=10, window_seconds=60)
        
        if limiter.acquire():
            # Proceed with API call
            pass
        else:
            # Rate limited, wait or skip
            pass
    """
    
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: list[datetime] = []
        self._lock = threading.Lock()
    
    def acquire(self) -> bool:
        """Try to acquire a rate limit slot. Returns True if allowed."""
        now = datetime.now()
        
        with self._lock:
            # Remove old calls outside window
            cutoff = now.timestamp() - self.window_seconds
            self.calls = [c for c in self.calls if c.timestamp() > cutoff]
            
            if len(self.calls) < self.max_calls:
                self.calls.append(now)
                return True
            return False
    
    def reset(self):
        """Reset the rate limiter."""
        with self._lock:
            self.calls.clear()
