"""
Spread Filter - Real-time spread monitoring and filtering

Features:
1. Rolling window of spread samples
2. Percentile-based thresholds (P95 more robust than mean)
3. Hard block on extreme spreads
4. Score penalty on elevated spreads
5. Size reduction in wide spread conditions
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from datetime import datetime, UTC
import statistics

logger = logging.getLogger(__name__)


@dataclass
class SpreadFilterConfig:
    """Configuration for spread filter."""
    
    # Rolling window settings
    WINDOW_SIZE: int = 50                    # Number of samples to track
    MIN_SAMPLES: int = 10                    # Minimum samples before filtering
    
    # Spread thresholds (relative to rolling stats)
    BLOCK_MULT_AVG: float = 2.0              # Block if spread > 2.0 * avg
    BLOCK_MULT_P95: float = 1.0              # Block if spread > 1.0 * P95
    PENALTY_MULT_AVG: float = 1.5            # Penalize if spread > 1.5 * avg
    
    # Score penalties
    PENALTY_POINTS: int = 1                  # Score penalty for elevated spread
    
    # Size reduction
    SIZE_REDUCTION_FACTOR: float = 0.7       # Reduce size to 70% in wide spread
    
    # Absolute thresholds (bps) as fallback
    ABSOLUTE_MAX_BPS: float = 30.0           # 0.30% - block any spread above this
    ABSOLUTE_ELEVATED_BPS: float = 15.0      # 0.15% - penalize above this
    
    # Session-specific adjustments
    ASIA_THRESHOLD_MULT: float = 1.3         # 30% more tolerant in ASIA
    
    # Logging
    LOG_ALL_CHECKS: bool = False             # Log every check (debug only)


@dataclass
class SpreadCheckResult:
    """Result of spread check."""
    allowed: bool
    reason: str
    spread_now_bps: float
    spread_avg_bps: float
    spread_p95_bps: float
    score_penalty: int = 0
    size_factor: float = 1.0


class SpreadFilter:
    """
    Real-time spread filter to prevent entries with excessive spread.
    
    Usage:
        filter = SpreadFilter()
        
        # Feed spread data (call on every price update)
        filter.update_spread(bid=96990, ask=97010)
        
        # Check before entry
        result = filter.check_entry_allowed(session='LONDON')
        if not result.allowed:
            logger.warning(f"Spread blocked: {result.reason}")
        else:
            score -= result.score_penalty
            size *= result.size_factor
    """
    
    def __init__(self, config: Optional[SpreadFilterConfig] = None):
        self.config = config or SpreadFilterConfig()
        self.spread_samples: deque = deque(maxlen=self.config.WINDOW_SIZE)
        self._last_update: Optional[datetime] = None
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        
        # Cache for percentile calculations (avoid recalc every check)
        self._cached_avg: float = 0.0
        self._cached_p95: float = 0.0
        self._cache_valid: bool = False
    
    def update_spread(
        self, 
        bid: float, 
        ask: float, 
        mid_price: Optional[float] = None
    ) -> float:
        """
        Update spread rolling window.
        
        Args:
            bid: Best bid price
            ask: Best ask price
            mid_price: Mid price (calculated if not provided)
        
        Returns:
            Current spread in bps
        """
        if bid <= 0 or ask <= 0 or ask <= bid:
            logger.debug(f"Invalid bid/ask: {bid}/{ask}")
            return 0.0
        
        if mid_price is None:
            mid_price = (bid + ask) / 2
        
        spread_abs = ask - bid
        spread_bps = (spread_abs / mid_price) * 10000  # Convert to bps
        
        self.spread_samples.append(spread_bps)
        self._last_bid = bid
        self._last_ask = ask
        self._last_update = datetime.now(UTC)
        self._cache_valid = False  # Invalidate cache
        
        if self.config.LOG_ALL_CHECKS:
            logger.debug(f"Spread updated: {spread_bps:.2f} bps")
        
        return spread_bps
    
    def _update_cache(self):
        """Update cached statistics."""
        if self._cache_valid:
            return
        
        if len(self.spread_samples) < self.config.MIN_SAMPLES:
            self._cached_avg = 0.0
            self._cached_p95 = 0.0
            return
        
        samples = list(self.spread_samples)
        self._cached_avg = statistics.mean(samples)
        
        # Calculate P95
        sorted_samples = sorted(samples)
        p95_idx = int(len(sorted_samples) * 0.95)
        self._cached_p95 = sorted_samples[min(p95_idx, len(sorted_samples) - 1)]
        
        self._cache_valid = True
    
    @property
    def current_spread_bps(self) -> float:
        """Get most recent spread in bps."""
        if not self.spread_samples:
            return 0.0
        return self.spread_samples[-1]
    
    @property
    def avg_spread_bps(self) -> float:
        """Get average spread in bps."""
        self._update_cache()
        return self._cached_avg
    
    @property
    def p95_spread_bps(self) -> float:
        """Get P95 spread in bps."""
        self._update_cache()
        return self._cached_p95
    
    def check_entry_allowed(
        self, 
        session: str = 'OTHER',
        current_spread_override: Optional[float] = None
    ) -> SpreadCheckResult:
        """
        Check if entry is allowed based on current spread.
        
        Args:
            session: Current trading session ('ASIA', 'LONDON', 'NY', etc.)
            current_spread_override: Override current spread (for testing)
        
        Returns:
            SpreadCheckResult with allowed flag and adjustments
        """
        spread_now = current_spread_override if current_spread_override is not None \
            else self.current_spread_bps
        
        # Need minimum samples to use relative thresholds
        if len(self.spread_samples) < self.config.MIN_SAMPLES:
            # Fall back to absolute thresholds
            if spread_now > self.config.ABSOLUTE_MAX_BPS:
                return SpreadCheckResult(
                    allowed=False,
                    reason=f"Spread {spread_now:.1f} bps > absolute max {self.config.ABSOLUTE_MAX_BPS:.1f} bps",
                    spread_now_bps=spread_now,
                    spread_avg_bps=0.0,
                    spread_p95_bps=0.0,
                    score_penalty=0,
                    size_factor=1.0
                )
            return SpreadCheckResult(
                allowed=True,
                reason="Insufficient samples, using absolute threshold",
                spread_now_bps=spread_now,
                spread_avg_bps=0.0,
                spread_p95_bps=0.0,
                score_penalty=0,
                size_factor=1.0
            )
        
        # Get stats
        self._update_cache()
        avg = self._cached_avg
        p95 = self._cached_p95
        
        # Session adjustment
        session_mult = 1.0
        if session.upper() == 'ASIA':
            session_mult = self.config.ASIA_THRESHOLD_MULT
        
        # Adjusted thresholds
        block_avg = avg * self.config.BLOCK_MULT_AVG * session_mult
        block_p95 = p95 * self.config.BLOCK_MULT_P95 * session_mult
        penalty_avg = avg * self.config.PENALTY_MULT_AVG * session_mult
        
        # Check absolute max (always applies)
        if spread_now > self.config.ABSOLUTE_MAX_BPS:
            return SpreadCheckResult(
                allowed=False,
                reason=f"Spread {spread_now:.1f} bps > absolute max {self.config.ABSOLUTE_MAX_BPS:.1f} bps",
                spread_now_bps=spread_now,
                spread_avg_bps=avg,
                spread_p95_bps=p95,
                score_penalty=0,
                size_factor=1.0
            )
        
        # Check relative blocks
        if spread_now > block_avg:
            return SpreadCheckResult(
                allowed=False,
                reason=f"Spread {spread_now:.1f} bps > {self.config.BLOCK_MULT_AVG}x avg ({block_avg:.1f} bps)",
                spread_now_bps=spread_now,
                spread_avg_bps=avg,
                spread_p95_bps=p95,
                score_penalty=0,
                size_factor=1.0
            )
        
        if spread_now > block_p95:
            return SpreadCheckResult(
                allowed=False,
                reason=f"Spread {spread_now:.1f} bps > P95 ({p95:.1f} bps)",
                spread_now_bps=spread_now,
                spread_avg_bps=avg,
                spread_p95_bps=p95,
                score_penalty=0,
                size_factor=1.0
            )
        
        # Check for penalty zone
        if spread_now > penalty_avg:
            return SpreadCheckResult(
                allowed=True,
                reason=f"Spread elevated: {spread_now:.1f} bps > {self.config.PENALTY_MULT_AVG}x avg",
                spread_now_bps=spread_now,
                spread_avg_bps=avg,
                spread_p95_bps=p95,
                score_penalty=self.config.PENALTY_POINTS,
                size_factor=self.config.SIZE_REDUCTION_FACTOR
            )
        
        # Check absolute elevated
        if spread_now > self.config.ABSOLUTE_ELEVATED_BPS:
            return SpreadCheckResult(
                allowed=True,
                reason=f"Spread elevated: {spread_now:.1f} bps",
                spread_now_bps=spread_now,
                spread_avg_bps=avg,
                spread_p95_bps=p95,
                score_penalty=self.config.PENALTY_POINTS,
                size_factor=self.config.SIZE_REDUCTION_FACTOR
            )
        
        # All good
        return SpreadCheckResult(
            allowed=True,
            reason="Spread normal",
            spread_now_bps=spread_now,
            spread_avg_bps=avg,
            spread_p95_bps=p95,
            score_penalty=0,
            size_factor=1.0
        )
    
    def get_average_spread(self) -> float:
        """Get average spread in bps."""
        self._update_cache()
        return self._cached_avg
    
    def get_p95_spread(self) -> float:
        """Get P95 spread in bps."""
        self._update_cache()
        return self._cached_p95
    
    def get_spread_stats(self) -> dict:
        """Get current spread statistics."""
        self._update_cache()
        return {
            'current_bps': self.current_spread_bps,
            'avg_bps': self._cached_avg,
            'p95_bps': self._cached_p95,
            'samples': len(self.spread_samples),
            'last_update': self._last_update.isoformat() if self._last_update else None
        }
    
    def reset(self):
        """Reset spread filter (e.g., on session change)."""
        self.spread_samples.clear()
        self._cache_valid = False
        self._last_update = None


# Singleton instance
_spread_filter: Optional[SpreadFilter] = None


def get_spread_filter() -> SpreadFilter:
    """Get singleton SpreadFilter instance."""
    global _spread_filter
    if _spread_filter is None:
        _spread_filter = SpreadFilter()
    return _spread_filter


def check_spread_for_entry(
    bid: float,
    ask: float,
    session: str = 'OTHER'
) -> SpreadCheckResult:
    """
    Convenience function to check spread before entry.
    
    Updates spread window and checks if entry is allowed.
    
    Usage:
        from src.engine.spread_filter import check_spread_for_entry
        
        result = check_spread_for_entry(bid=96990, ask=97010, session='LONDON')
        if not result.allowed:
            return None  # Block entry
        score -= result.score_penalty
        size *= result.size_factor
    """
    filter = get_spread_filter()
    filter.update_spread(bid, ask)
    return filter.check_entry_allowed(session)
