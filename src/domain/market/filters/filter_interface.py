"""
Filter Interface - Common interface for all trading filters

Provides a standardized FilterDecision dataclass that all filters return,
enabling consistent integration in signal_engine.py.

Created: January 15, 2026
"""

from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict
from datetime import datetime
from abc import ABC, abstractmethod


@dataclass
class FilterDecision:
    """
    Standard return structure for all trading filters.
    
    Usage:
        decision = filter.evaluate(...)
        if not decision.allow:
            return  # Hard block
        score += decision.score_adjust
        tags.update(decision.tags)
        
    Added size_factor for position size caps
    """
    allow: bool                                    # True = pass, False = hard block
    score_adjust: float = 0.0                      # Score adjustment (+/-)
    strength: float = 0.0                          # Signal strength 0..1
    regime: Optional[str] = None                   # "RANGE", "CHAOS", "TREND", etc.
    reasons: List[str] = field(default_factory=list)  # Human-readable reasons
    tags: Dict[str, Any] = field(default_factory=dict)  # Tags for trade analysis
    cooldown_seconds: int = 0                      # Request pause after decision
    regime_source: Optional[str] = None            # Which filter set the regime (for dedup)
    size_factor: float = 1.0                       # 🔥 NEW: Size multiplier (0.25-1.0)
    
    def __str__(self) -> str:
        status = "✅ ALLOW" if self.allow else "❌ BLOCK"
        adj = f" ({self.score_adjust:+.1f})" if self.score_adjust != 0 else ""
        reasons_str = ", ".join(self.reasons) if self.reasons else "No specific reason"
        return f"{status}{adj}: {reasons_str}"
    
    def merge(self, other: 'FilterDecision') -> 'FilterDecision':
        """
        Merge two decisions (conservative: block wins).
        Useful for combining multiple filter results.
        """
        return FilterDecision(
            allow=self.allow and other.allow,
            score_adjust=self.score_adjust + other.score_adjust,
            strength=max(self.strength, other.strength),
            regime=other.regime if other.regime else self.regime,
            reasons=self.reasons + other.reasons,
            tags={**self.tags, **other.tags},
            cooldown_seconds=max(self.cooldown_seconds, other.cooldown_seconds),
            regime_source=other.regime_source if other.regime_source else self.regime_source,
            size_factor=min(self.size_factor, other.size_factor)  # 🔥 Most restrictive wins
        )
    
    def merge_dedup(self, other: 'FilterDecision', 
                   dedup_regimes: List[str] | None = None) -> 'FilterDecision':
        """
        Merge decisions with regime-based deduplication.
        
        If both decisions are for the same regime type (e.g., RANGE),
        only apply the penalty ONCE to avoid triple-veto.
        
        Args:
            other: Decision to merge
            dedup_regimes: Regimes to deduplicate (default: ['RANGE', 'COMPRESSION', 'SHOCK'])
        
        Example:
            - BB Width says RANGE → penalty -2
            - ADX says RANGE → would normally add another -2
            - With dedup, second RANGE penalty is skipped
        """
        dedup_regimes = dedup_regimes or ['RANGE', 'COMPRESSION', 'SHOCK', 'NO_TRADE']
        
        # Check if we should skip the score adjustment (same regime already applied)
        skip_score = False
        if self.regime and other.regime:
            if self.regime in dedup_regimes and other.regime in dedup_regimes:
                # Both are similar low-activity regimes - skip duplicate penalty
                skip_score = True
        
        new_score_adjust = self.score_adjust
        if not skip_score:
            new_score_adjust += other.score_adjust
        
        return FilterDecision(
            allow=self.allow and other.allow,
            score_adjust=new_score_adjust,
            strength=max(self.strength, other.strength),
            regime=other.regime if other.regime else self.regime,
            reasons=self.reasons + other.reasons,
            tags={**self.tags, **other.tags},
            cooldown_seconds=max(self.cooldown_seconds, other.cooldown_seconds),
            regime_source=other.regime_source if other.regime_source else self.regime_source,
            size_factor=min(self.size_factor, other.size_factor)  # 🔥 Most restrictive wins
        )
    
    @classmethod
    def allow_default(cls) -> 'FilterDecision':
        """Return a default allow decision"""
        return cls(allow=True, reasons=["Default allow"])
    
    @classmethod
    def block(cls, reason: str, **tags) -> 'FilterDecision':
        """Convenience method for hard block"""
        return cls(allow=False, reasons=[reason], tags=tags)
    
    @classmethod
    def penalize(cls, amount: float, reason: str, **tags) -> 'FilterDecision':
        """Convenience method for score penalty (still allowed)"""
        return cls(allow=True, score_adjust=-abs(amount), reasons=[reason], tags=tags)
    
    @classmethod
    def bonus(cls, amount: float, reason: str, **tags) -> 'FilterDecision':
        """Convenience method for score bonus"""
        return cls(allow=True, score_adjust=abs(amount), reasons=[reason], tags=tags)


class BaseFilter(ABC):
    """
    Abstract base class for all trading filters.
    
    Implement evaluate() to create a new filter.
    """
    
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._last_decision: Optional[FilterDecision] = None
    
    @abstractmethod
    def evaluate(self, **kwargs) -> FilterDecision:
        """
        Evaluate the filter and return a decision.
        
        Override this method in subclasses.
        """
        pass
    
    def get_last_decision(self) -> Optional[FilterDecision]:
        """Get the last decision made by this filter"""
        return self._last_decision
    
    def __call__(self, **kwargs) -> FilterDecision:
        """Allow calling filter as function"""
        if not self.enabled:
            return FilterDecision.allow_default()
        self._last_decision = self.evaluate(**kwargs)
        return self._last_decision


# =============================================================================
# FILTER PIPELINE - Runs multiple filters and merges results
# =============================================================================

class FilterPipeline:
    """
    Runs a sequence of filters and merges their decisions.
    
    Usage:
        pipeline = FilterPipeline([filter1, filter2, filter3])
        decision = pipeline.run(df_5m=df, current_price=price, ...)
    """
    
    def __init__(self, filters: List[tuple[str, BaseFilter]] | None = None):
        self.filters: List[tuple[str, BaseFilter]] = filters or []
        self._last_decisions: Dict[str, FilterDecision] = {}
    
    def add_filter(self, filter_instance: BaseFilter, name: str | None = None):
        """Add a filter to the pipeline"""
        self.filters.append((name or filter_instance.__class__.__name__, filter_instance))
    
    def run(self, early_exit: bool = True, **kwargs) -> FilterDecision:
        """
        Run all filters and merge decisions.
        
        Args:
            early_exit: If True, stop on first block (faster)
            **kwargs: Passed to each filter's evaluate()
        
        Returns:
            Merged FilterDecision
        """
        final = FilterDecision.allow_default()
        self._last_decisions = {}
        
        for name, filt in self.filters:
            if not filt.enabled:
                continue
            
            decision = filt(**kwargs)
            self._last_decisions[name] = decision
            final = final.merge(decision)
            
            if early_exit and not decision.allow:
                # Add which filter blocked
                final.tags['blocked_by'] = name
                break
        
        return final
    
    def get_filter_decisions(self) -> Dict[str, FilterDecision]:
        """Get individual decisions from last run"""
        return self._last_decisions
