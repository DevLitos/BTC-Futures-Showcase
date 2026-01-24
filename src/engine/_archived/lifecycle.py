"""
Lifecycle - Base interface for components with start/stop lifecycle.

All components that run background tasks should implement this interface
for consistent initialization, shutdown, and resource cleanup.

Usage:
    class MyComponent(Lifecycle):
        async def start(self):
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
        
        async def stop(self):
            self._running = False
            if self._task:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, List
from functools import wraps

logger = logging.getLogger(__name__)


class Lifecycle(ABC):
    """
    Abstract base class for components with async lifecycle.
    
    Provides:
    - Consistent start/stop interface
    - Task tracking and cancellation
    - Graceful shutdown with timeout
    """
    
    def __init__(self):
        self._running = False
        self._tasks: List[asyncio.Task] = []
    
    @property
    def is_running(self) -> bool:
        """Check if component is running."""
        return self._running
    
    @abstractmethod
    async def start(self) -> None:
        """
        Start the component.
        
        Implementations should:
        1. Set self._running = True
        2. Create background tasks with _create_task()
        3. Initialize any resources
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the component gracefully.
        
        Implementations should:
        1. Set self._running = False
        2. Cancel tasks via _cancel_tasks()
        3. Cleanup resources
        """
        pass
    
    def _create_task(self, coro, name: str | None = None) -> asyncio.Task:
        """
        Create and track a background task.
        
        Args:
            coro: Coroutine to run
            name: Optional task name for debugging
            
        Returns:
            The created task
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task
    
    async def _cancel_tasks(self, timeout: float = 5.0) -> None:
        """
        Cancel all tracked tasks with timeout.
        
        Args:
            timeout: Maximum seconds to wait for task cancellation
        """
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"Some tasks did not stop within {timeout}s")
        
        self._tasks = []


async def with_timeout(coro, timeout: float, name: str = "operation") -> any:
    """
    Execute a coroutine with timeout.
    
    Args:
        coro: Coroutine to execute
        timeout: Maximum seconds to wait
        name: Operation name for error messages
        
    Returns:
        Result of the coroutine
        
    Raises:
        asyncio.TimeoutError: If timeout exceeded
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"Timeout ({timeout}s) in {name}")
        raise


async def safe_call(coro, default=None, name: str = "operation", timeout: float = 30.0):
    """
    Execute a coroutine safely with timeout, returning default on failure.
    
    Args:
        coro: Coroutine to execute
        default: Value to return on failure
        name: Operation name for logging
        timeout: Maximum seconds to wait
        
    Returns:
        Result of coroutine or default on failure
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"Timeout ({timeout}s) in {name}")
        return default
    except asyncio.CancelledError:
        raise  # Don't catch cancellation
    except Exception as e:
        logger.error(f"Error in {name}: {e}")
        return default


class LifecycleManager:
    """
    Manages multiple Lifecycle components.
    
    Provides ordered startup and reverse-ordered shutdown.
    
    Usage:
        manager = LifecycleManager()
        manager.register(websocket, "WebSocket")
        manager.register(signal_engine, "SignalEngine")
        
        await manager.start_all()
        # ... run ...
        await manager.stop_all()
    """
    
    def __init__(self):
        self._components: List[tuple] = []  # (component, name)
    
    def register(self, component: Lifecycle, name: str) -> None:
        """Register a component for lifecycle management."""
        self._components.append((component, name))
    
    async def start_all(self) -> None:
        """Start all components in registration order."""
        for component, name in self._components:
            logger.info(f"Starting {name}...")
            try:
                await component.start()
                logger.info(f"✓ {name} started")
            except Exception as e:
                logger.error(f"✗ Failed to start {name}: {e}")
                # Stop already-started components
                await self.stop_all()
                raise
    
    async def stop_all(self, timeout: float = 30.0) -> None:
        """Stop all components in reverse order."""
        # Reverse order for proper dependency shutdown
        for component, name in reversed(self._components):
            if component.is_running:
                logger.info(f"Stopping {name}...")
                try:
                    await asyncio.wait_for(component.stop(), timeout=timeout)
                    logger.info(f"✓ {name} stopped")
                except asyncio.TimeoutError:
                    logger.warning(f"⚠ {name} stop timed out")
                except Exception as e:
                    logger.error(f"✗ Error stopping {name}: {e}")
