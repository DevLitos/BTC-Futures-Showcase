"""
Market Quality Gate - System-level risk layer that blocks trades

This is NOT signal quality - it's MARKET/SYSTEM quality:
- Spread anomalies (execution risk)
- Data staleness (decision risk)
- Volatility shock/chaos (systemic risk)
- Exchange issues (operational risk)

This layer sits BEFORE SignalEngine. If market quality fails,
no signal evaluation happens.

PRINCIPLE: This is "risk of system", not "risk of trade"

Created: January 20, 2026
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timedelta
from enum import Enum
import logging
import numpy as np

logger = logging.getLogger(__name__)


class MarketQualityStatus(str, Enum):
    """Market quality classification."""
    OPTIMAL = "OPTIMAL"           # Full trading allowed
    DEGRADED = "DEGRADED"         # Trading with caution (reduced size)
    POOR = "POOR"                 # Probe trades only
    BLOCKED = "BLOCKED"           # No new trades


@dataclass
class MarketQualityResult:
    """Result from market quality gate evaluation."""
    allow_trade: bool
    status: MarketQualityStatus
    cooldown_seconds: int = 0
    size_multiplier: float = 1.0   # 1.0 = full, 0.5 = reduced, 0.25 = probe
    reasons: list = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        status_emoji = {
            MarketQualityStatus.OPTIMAL: "✅",
            MarketQualityStatus.DEGRADED: "⚠️",
            MarketQualityStatus.POOR: "🔶",
            MarketQualityStatus.BLOCKED: "🚫"
        }
        emoji = status_emoji.get(self.status, "❓")
        reasons_str = "; ".join(self.reasons) if self.reasons else "OK"
        return f"{emoji} Market: {self.status.value} ({self.size_multiplier:.0%}) - {reasons_str}"


class MarketQualityGate:
    """
    Evaluates market/system quality for trading.
    
    This gate checks:
    1. Spread quality (execution risk)
    2. Data freshness (decision risk)
    3. Volatility shock (systemic risk)
    4. Exchange connectivity (operational risk)
    5. Time-based restrictions (weekend, holidays)
    
    Usage:
        gate = MarketQualityGate()
        result = gate.evaluate(
            spread_bps=5.0,
            spread_normal_bps=3.0,
            last_data_age_seconds=2.0,
            volatility_ratio=1.2,
            ...
        )
        
        if not result.allow_trade:
            logger.warning(f"Market quality blocked: {result}")
            return
        
        # Proceed with signal evaluation
        size *= result.size_multiplier
    """
    
    # =========================================================================
    # THRESHOLDS
    # =========================================================================
    
    # Spread thresholds (in bps relative to normal)
    SPREAD_OPTIMAL_MULT = 1.2      # <= 1.2x normal = optimal
    SPREAD_DEGRADED_MULT = 2.0     # <= 2.0x normal = degraded
    SPREAD_POOR_MULT = 3.0         # <= 3.0x normal = poor (probe only)
    SPREAD_BLOCK_MULT = 5.0        # > 5.0x normal = blocked
    
    # Data freshness thresholds (seconds)
    DATA_OPTIMAL_AGE = 5.0         # <= 5s = optimal
    DATA_DEGRADED_AGE = 15.0       # <= 15s = degraded
    DATA_POOR_AGE = 30.0           # <= 30s = poor
    DATA_BLOCK_AGE = 60.0          # > 60s = blocked (stale)
    
    # Volatility thresholds (ATR ratio: current/historical)
    VOL_OPTIMAL_MAX = 1.5          # <= 1.5x = optimal
    VOL_DEGRADED_MAX = 2.0         # <= 2.0x = degraded
    VOL_POOR_MAX = 3.0             # <= 3.0x = poor (chaos approaching)
    VOL_BLOCK_MAX = 4.0            # > 4.0x = blocked (black swan territory)
    
    # Cooldown periods (seconds)
    COOLDOWN_DEGRADED = 30
    COOLDOWN_POOR = 120
    COOLDOWN_BLOCKED = 300
    
    def __init__(self):
        self._last_evaluation: Optional[MarketQualityResult] = None
        self._blocked_until: Optional[datetime] = None
        self._consecutive_blocks = 0
    
    def evaluate(self,
                 # Spread metrics
                 spread_bps: float = 0,
                 spread_normal_bps: float = 3.0,
                 
                 # Data freshness
                 last_data_age_seconds: float = 0,
                 
                 # Volatility
                 volatility_ratio: float = 1.0,      # current_atr / historical_atr
                 true_range_ratio: float = 1.0,      # current_TR / ATR
                 
                 # Exchange status
                 exchange_connected: bool = True,
                 exchange_latency_ms: float = 0,
                 
                 # Time context
                 market_time: datetime = None,
                 is_weekend: bool = False,
                 
                 # Recent fills quality
                 recent_slippage_bps: float = 0,
                 fills_last_hour: int = 0,
                 
                 **kwargs) -> MarketQualityResult:
        """
        Evaluate market quality and return trading permission.
        
        Returns:
            MarketQualityResult with allow_trade, status, and size_multiplier
        """
        market_time = market_time or datetime.utcnow()
        reasons = []
        details = {}
        
        # Check if still in cooldown
        if self._blocked_until and market_time < self._blocked_until:
            remaining = (self._blocked_until - market_time).total_seconds()
            return MarketQualityResult(
                allow_trade=False,
                status=MarketQualityStatus.BLOCKED,
                cooldown_seconds=int(remaining),
                size_multiplier=0.0,
                reasons=[f"In cooldown ({remaining:.0f}s remaining)"],
                details={'blocked_until': self._blocked_until}
            )
        else:
            self._blocked_until = None
        
        # =====================================================================
        # 1. EXCHANGE CONNECTIVITY (highest priority)
        # =====================================================================
        if not exchange_connected:
            self._set_cooldown(market_time, self.COOLDOWN_BLOCKED)
            return MarketQualityResult(
                allow_trade=False,
                status=MarketQualityStatus.BLOCKED,
                cooldown_seconds=self.COOLDOWN_BLOCKED,
                size_multiplier=0.0,
                reasons=["Exchange disconnected"]
            )
        
        details['exchange_latency_ms'] = exchange_latency_ms
        if exchange_latency_ms > 500:
            reasons.append(f"High latency: {exchange_latency_ms:.0f}ms")
        
        # =====================================================================
        # 2. DATA FRESHNESS
        # =====================================================================
        details['data_age_seconds'] = last_data_age_seconds
        
        if last_data_age_seconds > self.DATA_BLOCK_AGE:
            self._set_cooldown(market_time, self.COOLDOWN_BLOCKED)
            return MarketQualityResult(
                allow_trade=False,
                status=MarketQualityStatus.BLOCKED,
                cooldown_seconds=self.COOLDOWN_BLOCKED,
                size_multiplier=0.0,
                reasons=[f"Data stale: {last_data_age_seconds:.0f}s"],
                details=details
            )
        elif last_data_age_seconds > self.DATA_POOR_AGE:
            reasons.append(f"Data aging: {last_data_age_seconds:.0f}s")
        
        # =====================================================================
        # 3. SPREAD QUALITY
        # =====================================================================
        spread_ratio = spread_bps / spread_normal_bps if spread_normal_bps > 0 else 1.0
        details['spread_bps'] = spread_bps
        details['spread_ratio'] = spread_ratio
        
        if spread_ratio > self.SPREAD_BLOCK_MULT:
            self._set_cooldown(market_time, self.COOLDOWN_POOR)
            return MarketQualityResult(
                allow_trade=False,
                status=MarketQualityStatus.BLOCKED,
                cooldown_seconds=self.COOLDOWN_POOR,
                size_multiplier=0.0,
                reasons=[f"Extreme spread: {spread_bps:.1f}bps ({spread_ratio:.1f}x normal)"],
                details=details
            )
        elif spread_ratio > self.SPREAD_POOR_MULT:
            reasons.append(f"Wide spread: {spread_ratio:.1f}x normal")
        elif spread_ratio > self.SPREAD_DEGRADED_MULT:
            reasons.append(f"Elevated spread: {spread_ratio:.1f}x normal")
        
        # =====================================================================
        # 4. VOLATILITY (Shock/Chaos detection)
        # =====================================================================
        details['volatility_ratio'] = volatility_ratio
        details['true_range_ratio'] = true_range_ratio
        
        # Use max of both ratios for chaos detection
        vol_signal = max(volatility_ratio, true_range_ratio)
        
        if vol_signal > self.VOL_BLOCK_MAX:
            self._set_cooldown(market_time, self.COOLDOWN_BLOCKED)
            return MarketQualityResult(
                allow_trade=False,
                status=MarketQualityStatus.BLOCKED,
                cooldown_seconds=self.COOLDOWN_BLOCKED,
                size_multiplier=0.0,
                reasons=[f"Volatility shock: {vol_signal:.1f}x normal (chaos)"],
                details=details
            )
        elif vol_signal > self.VOL_POOR_MAX:
            reasons.append(f"High volatility: {vol_signal:.1f}x")
        elif vol_signal > self.VOL_DEGRADED_MAX:
            reasons.append(f"Elevated volatility: {vol_signal:.1f}x")
        
        # =====================================================================
        # 5. WEEKEND/OFF-HOURS (if applicable)
        # =====================================================================
        if is_weekend:
            reasons.append("Weekend trading")
            details['is_weekend'] = True
        
        # =====================================================================
        # 6. RECENT EXECUTION QUALITY
        # =====================================================================
        details['recent_slippage_bps'] = recent_slippage_bps
        if recent_slippage_bps > 15:
            reasons.append(f"Recent slippage high: {recent_slippage_bps:.1f}bps")
        
        # =====================================================================
        # DETERMINE FINAL STATUS
        # =====================================================================
        status, size_mult, cooldown = self._calculate_status(
            spread_ratio, last_data_age_seconds, vol_signal, reasons
        )
        
        # Reset consecutive blocks if we're allowing
        if status != MarketQualityStatus.BLOCKED:
            self._consecutive_blocks = 0
        
        result = MarketQualityResult(
            allow_trade=(status != MarketQualityStatus.BLOCKED),
            status=status,
            cooldown_seconds=cooldown,
            size_multiplier=size_mult,
            reasons=reasons if reasons else ["Market quality optimal"],
            details=details
        )
        
        self._last_evaluation = result
        return result
    
    def _calculate_status(
        self,
        spread_ratio: float,
        data_age: float,
        vol_signal: float,
        reasons: list
    ) -> Tuple[MarketQualityStatus, float, int]:
        """Calculate final status based on all factors."""
        
        # Count degradation factors
        poor_factors = 0
        degraded_factors = 0
        
        # Spread
        if spread_ratio > self.SPREAD_POOR_MULT:
            poor_factors += 1
        elif spread_ratio > self.SPREAD_DEGRADED_MULT:
            degraded_factors += 1
        
        # Data age
        if data_age > self.DATA_POOR_AGE:
            poor_factors += 1
        elif data_age > self.DATA_DEGRADED_AGE:
            degraded_factors += 1
        
        # Volatility
        if vol_signal > self.VOL_POOR_MAX:
            poor_factors += 1
        elif vol_signal > self.VOL_DEGRADED_MAX:
            degraded_factors += 1
        
        # Determine status
        if poor_factors >= 2:
            # Multiple poor factors = blocked
            return MarketQualityStatus.BLOCKED, 0.0, self.COOLDOWN_BLOCKED
        elif poor_factors >= 1:
            # One poor factor = probe only
            return MarketQualityStatus.POOR, 0.25, self.COOLDOWN_POOR
        elif degraded_factors >= 2:
            # Multiple degraded = poor
            return MarketQualityStatus.POOR, 0.25, self.COOLDOWN_DEGRADED
        elif degraded_factors >= 1:
            # One degraded = reduced
            return MarketQualityStatus.DEGRADED, 0.5, self.COOLDOWN_DEGRADED
        else:
            # All good
            return MarketQualityStatus.OPTIMAL, 1.0, 0
    
    def _set_cooldown(self, market_time: datetime, seconds: int):
        """Set cooldown period."""
        self._blocked_until = market_time + timedelta(seconds=seconds)
        self._consecutive_blocks += 1
        logger.warning(f"🚫 Market quality cooldown: {seconds}s (consecutive: {self._consecutive_blocks})")
    
    def get_last_evaluation(self) -> Optional[MarketQualityResult]:
        """Get the last evaluation result."""
        return self._last_evaluation
    
    def force_clear_cooldown(self):
        """Force clear cooldown (for recovery scenarios)."""
        self._blocked_until = None
        self._consecutive_blocks = 0
        logger.info("Market quality cooldown cleared")


# =============================================================================
# SINGLETON
# =============================================================================

_market_quality_gate: Optional[MarketQualityGate] = None


def get_market_quality_gate() -> MarketQualityGate:
    """Get or create the market quality gate singleton."""
    global _market_quality_gate
    if _market_quality_gate is None:
        _market_quality_gate = MarketQualityGate()
    return _market_quality_gate


def reset_market_quality_gate():
    """Reset the singleton."""
    global _market_quality_gate
    _market_quality_gate = None


def evaluate_market_quality(**kwargs) -> MarketQualityResult:
    """Convenience function to evaluate market quality."""
    return get_market_quality_gate().evaluate(**kwargs)
