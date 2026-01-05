"""
Time Provider - Centralized timestamp management.

This module ensures consistent timestamp handling across the system.
All components should use this for time operations to avoid timezone issues.

RULES:
1. Always use UTC - never local time
2. In backtest: use market_time from data
3. In live: use exchange server time when available, else datetime.now(UTC)
4. Never use datetime.now() without UTC
"""

from datetime import datetime, timezone, timedelta, UTC
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TimeProvider:
    """
    Centralized time provider for consistent timestamps.
    
    Modes:
    - LIVE: Returns current UTC time (optionally synced with exchange)
    - BACKTEST: Returns the provided market_time (from historical data)
    
    Usage:
        # Initialize with mode
        time_provider = TimeProvider(mode='live')
        
        # In backtest, set market time from data
        time_provider.set_market_time(candle_timestamp)
        
        # Get current time (returns market_time in backtest, UTC now in live)
        current = time_provider.now()
    """
    
    MODE_LIVE = "live"
    MODE_BACKTEST = "backtest"
    
    def __init__(self, mode: str = MODE_LIVE):
        """
        Initialize time provider.
        
        Args:
            mode: 'live' or 'backtest'
        """
        self.mode = mode
        self._market_time: Optional[datetime] = None
        self._exchange_offset: timedelta = timedelta(0)
        
    def set_mode(self, mode: str) -> None:
        """Change operating mode."""
        self.mode = mode
        if mode == self.MODE_LIVE:
            self._market_time = None
            
    def set_market_time(self, market_time: datetime) -> None:
        """
        Set market time (for backtest mode).
        
        In backtest, this should be called with each new candle's timestamp.
        
        Args:
            market_time: The timestamp from the historical data
        """
        # Ensure timezone awareness
        if market_time.tzinfo is None:
            market_time = market_time.replace(tzinfo=UTC)
        self._market_time = market_time
        
    def sync_with_exchange(self, exchange_server_time: datetime) -> None:
        """
        Sync with exchange server time.
        
        Calculates offset between local and exchange time for accurate
        order timing and duration calculations.
        
        Args:
            exchange_server_time: Timestamp from exchange server
        """
        local_time = datetime.now(UTC)
        if exchange_server_time.tzinfo is None:
            exchange_server_time = exchange_server_time.replace(tzinfo=UTC)
        self._exchange_offset = exchange_server_time - local_time
        
        offset_ms = self._exchange_offset.total_seconds() * 1000
        if abs(offset_ms) > 1000:  # More than 1 second drift
            logger.warning(f"Exchange time offset: {offset_ms:.0f}ms")
            
    def now(self) -> datetime:
        """
        Get current time.
        
        - In backtest mode: returns the set market_time
        - In live mode: returns current UTC time (with exchange offset if synced)
        
        Returns:
            Current datetime in UTC
        """
        if self.mode == self.MODE_BACKTEST and self._market_time is not None:
            return self._market_time
        
        # Live mode: current UTC with optional exchange offset
        return datetime.now(UTC) + self._exchange_offset
    
    def elapsed_since(self, start_time: datetime) -> timedelta:
        """
        Calculate elapsed time since start_time.
        
        Uses consistent time source based on mode.
        
        Args:
            start_time: The starting timestamp
            
        Returns:
            Elapsed timedelta
        """
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        return self.now() - start_time
    
    def is_expired(self, start_time: datetime, max_duration: timedelta) -> bool:
        """
        Check if duration since start_time has exceeded max_duration.
        
        Args:
            start_time: When the timer started
            max_duration: Maximum allowed duration
            
        Returns:
            True if expired
        """
        return self.elapsed_since(start_time) > max_duration
    
    @staticmethod
    def ensure_utc(dt: datetime) -> datetime:
        """
        Ensure datetime has UTC timezone.
        
        Args:
            dt: Datetime to check/convert
            
        Returns:
            Datetime with UTC timezone
        """
        if dt is None:
            return datetime.now(UTC)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    
    @staticmethod
    def now_utc() -> datetime:
        """
        Get current UTC time (static utility).
        
        Use this for simple cases where mode doesn't matter.
        For backtest-aware timing, use instance.now() instead.
        """
        return datetime.now(UTC)


# Global singleton instance (for backwards compatibility)
# Prefer explicit instantiation for new code
_time_provider: Optional[TimeProvider] = None


def get_time_provider(mode: str = TimeProvider.MODE_LIVE, instance: Optional[TimeProvider] = None) -> TimeProvider:
    """
    Get or create TimeProvider instance.
    
    Args:
        mode: 'live' or 'backtest'
        instance: Optional explicit instance to use (DI pattern)
        
    Returns:
        TimeProvider instance
    """
    global _time_provider
    
    # DI: si se pasa instancia explícita, usarla
    if instance is not None:
        return instance
    
    # Singleton fallback
    if _time_provider is None:
        _time_provider = TimeProvider(mode=mode)
    elif _time_provider.mode != mode:
        _time_provider.set_mode(mode)
    return _time_provider


def reset_time_provider():
    """Reset singleton (for testing)"""
    global _time_provider
    _time_provider = None


def set_backtest_time(market_time: datetime) -> None:
    """
    Convenience function to set backtest market time.
    
    Args:
        market_time: Current candle timestamp
    """
    provider = get_time_provider(TimeProvider.MODE_BACKTEST)
    provider.set_market_time(market_time)


def current_time() -> datetime:
    """
    Get current time from global provider.
    
    Returns:
        Current datetime in UTC
    """
    if _time_provider is None:
        return datetime.now(UTC)
    return _time_provider.now()
