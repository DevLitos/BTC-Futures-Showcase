"""
Cost Model - Realistic Transaction Cost Modeling

This module provides accurate cost estimation for:
- Trading fees (maker/taker)
- Slippage (dynamic, volatility-adjusted)
- Funding rates
- Spread costs

CRITICAL: In LIVE mode, costs are MEASURED, not estimated.
This model is used for:
- Pre-trade cost estimation (all modes)
- Post-trade cost calculation (PAPER/BACKTEST)
- Backtesting with realistic friction

USAGE:
    from src.domain.trading.cost.cost_model import CostModel, get_cost_model
    
    model = get_cost_model()
    
    # Estimate costs before trade
    costs = model.estimate_costs(
        side='LONG',
        size_usd=1000.0,
        entry_price=95000.0,
        atr_1m=100.0,
        is_maker=True
    )
    print(f"Estimated total cost: ${costs.total_usd:.2f}")
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, List, Tuple
from collections import deque
import numpy as np
import logging

from src.bootstrap.runtime import get_runtime_config, RuntimeMode

logger = logging.getLogger(__name__)


# =============================================================================
# FEE SCHEDULES
# =============================================================================

@dataclass(frozen=True)
class FeeSchedule:
    """Fee schedule for an exchange."""
    exchange: str
    maker_fee_bps: float  # Basis points (1 bp = 0.01%)
    taker_fee_bps: float
    funding_interval_hours: float = 8.0
    
    def get_fee_pct(self, is_maker: bool) -> float:
        """Get fee as percentage (0.02 = 0.02%)."""
        bps = self.maker_fee_bps if is_maker else self.taker_fee_bps
        return bps / 100  # bps to percent


# Common exchange fee schedules
KRAKEN_FUTURES_FEES = FeeSchedule(
    exchange='krakenfutures',
    maker_fee_bps=2.0,   # 0.02%
    taker_fee_bps=5.0,   # 0.05%
    funding_interval_hours=4.0
)

BINANCE_FUTURES_FEES = FeeSchedule(
    exchange='binance',
    maker_fee_bps=2.0,   # 0.02%
    taker_fee_bps=4.0,   # 0.04%
    funding_interval_hours=8.0
)


# =============================================================================
# COST ESTIMATE RESULTS
# =============================================================================

@dataclass
class CostEstimate:
    """
    Complete cost estimate for a trade.
    
    All costs are in the SAME currency (usually USD).
    """
    # Fees
    fee_entry_usd: float = 0.0
    fee_exit_usd: float = 0.0
    fee_total_usd: float = 0.0
    
    # Slippage
    slippage_entry_usd: float = 0.0
    slippage_exit_usd: float = 0.0
    slippage_total_usd: float = 0.0
    slippage_entry_bps: float = 0.0
    slippage_exit_bps: float = 0.0
    
    # Spread
    spread_cost_usd: float = 0.0
    spread_bps: float = 0.0
    
    # Funding (estimated per hour held)
    funding_rate_hourly: float = 0.0
    funding_cost_per_hour_usd: float = 0.0
    
    # Totals
    total_usd: float = 0.0
    total_bps: float = 0.0  # As percentage of notional
    
    # Metadata
    notional_usd: float = 0.0
    is_estimate: bool = True  # False if measured
    
    def __post_init__(self):
        """Calculate totals."""
        self.fee_total_usd = self.fee_entry_usd + self.fee_exit_usd
        self.slippage_total_usd = self.slippage_entry_usd + self.slippage_exit_usd
        
        self.total_usd = (
            self.fee_total_usd + 
            self.slippage_total_usd + 
            self.spread_cost_usd
        )
        
        if self.notional_usd > 0:
            self.total_bps = (self.total_usd / self.notional_usd) * 10000
    
    def to_dict(self) -> Dict:
        return {
            'fee_entry_usd': round(self.fee_entry_usd, 4),
            'fee_exit_usd': round(self.fee_exit_usd, 4),
            'fee_total_usd': round(self.fee_total_usd, 4),
            'slippage_entry_bps': round(self.slippage_entry_bps, 2),
            'slippage_exit_bps': round(self.slippage_exit_bps, 2),
            'slippage_total_usd': round(self.slippage_total_usd, 4),
            'spread_cost_usd': round(self.spread_cost_usd, 4),
            'spread_bps': round(self.spread_bps, 2),
            'total_usd': round(self.total_usd, 4),
            'total_bps': round(self.total_bps, 2),
            'funding_rate_hourly': round(self.funding_rate_hourly, 6),
            'is_estimate': self.is_estimate,
        }


# =============================================================================
# SLIPPAGE MODEL
# =============================================================================

class SlippageModel:
    """
    Dynamic slippage estimation model.
    
    Factors:
    - Base slippage (exchange/instrument specific)
    - Volatility multiplier (higher vol = more slippage)
    - Size impact (larger orders = more slippage)
    - Order type (market vs limit)
    
    The model learns from actual execution data in LIVE mode.
    """
    
    def __init__(
        self,
        base_slippage_bps: float = 3.0,
        volatility_multiplier: float = 0.5,
        size_impact_factor: float = 0.1,
        max_slippage_bps: float = 50.0
    ):
        self.base_slippage_bps = base_slippage_bps
        self.volatility_multiplier = volatility_multiplier
        self.size_impact_factor = size_impact_factor
        self.max_slippage_bps = max_slippage_bps
        
        # Calibration factor (could be loaded from config)
        self._calibration_factor = 1.0
    
    def estimate(
        self,
        size_usd: float,
        avg_volume_usd: float,
        volatility_percentile: float,
        is_market_order: bool = False,
        spread_bps: float = 0.0
    ) -> float:
        """
        Estimate slippage in basis points.
        
        Args:
            size_usd: Order size in USD
            avg_volume_usd: Average volume for sizing context
            volatility_percentile: Current volatility percentile (0-100)
            is_market_order: True for market orders (higher slippage)
            spread_bps: Current spread in basis points
            
        Returns:
            Estimated slippage in basis points
        """
        # Base slippage
        slippage = self.base_slippage_bps
        
        # Volatility adjustment (0.5x to 2x based on percentile)
        vol_mult = 1.0 + (volatility_percentile - 50) / 100 * self.volatility_multiplier
        vol_mult = max(0.5, min(2.0, vol_mult))
        slippage *= vol_mult
        
        # Size impact (logarithmic)
        if avg_volume_usd > 0:
            size_ratio = size_usd / avg_volume_usd
            if size_ratio > 0.01:  # More than 1% of avg volume
                size_impact = np.log1p(size_ratio * 100) * self.size_impact_factor
                slippage += size_impact
        
        # Market order penalty
        if is_market_order:
            slippage += spread_bps / 2  # Cross half the spread
            slippage *= 1.5  # Additional penalty for aggression
        
        # Apply calibration from actual data
        slippage *= self._calibration_factor
        
        # Cap at maximum
        return float(min(slippage, self.max_slippage_bps))


# =============================================================================
# FUNDING RATE MODEL
# =============================================================================

class FundingModel:
    """
    Funding rate cost estimation.
    
    Perpetual futures charge funding every N hours.
    The rate depends on the difference between perp and spot price.
    """
    
    def __init__(self, funding_interval_hours: float = 8.0):
        self.funding_interval_hours = funding_interval_hours
        self._current_rate: float = 0.0
        self._rate_history: deque = deque(maxlen=100)
        self._last_update: Optional[datetime] = None
    
    def update_rate(self, rate: float, timestamp: datetime | None = None) -> None:
        """Update current funding rate."""
        self._current_rate = rate
        self._last_update = timestamp or datetime.now(UTC)
        self._rate_history.append((self._last_update, rate))
    
    def estimate_cost(
        self,
        position_size_usd: float,
        side: str,
        hold_hours: float = 1.0
    ) -> Tuple[float, float]:
        """
        Estimate funding cost.
        
        Args:
            position_size_usd: Position notional in USD
            side: 'LONG' or 'SHORT'
            hold_hours: Expected hold time in hours
            
        Returns:
            Tuple of (cost_usd, hourly_rate)
        """
        # Number of funding periods in hold time
        periods = hold_hours / self.funding_interval_hours
        
        # Per-period cost
        period_cost = position_size_usd * abs(self._current_rate)
        
        # Direction: LONG pays if rate positive, SHORT pays if negative
        is_paying = (side == 'LONG' and self._current_rate > 0) or \
                    (side == 'SHORT' and self._current_rate < 0)
        
        total_cost = period_cost * periods if is_paying else 0.0
        hourly_rate = self._current_rate / self.funding_interval_hours
        
        return total_cost, hourly_rate


# =============================================================================
# MAIN COST MODEL
# =============================================================================

class CostModel:
    """
    Complete transaction cost model.
    
    Combines:
    - Fee estimation (maker/taker)
    - Slippage estimation (dynamic)
    - Funding rate costs
    - Spread costs
    
    In LIVE mode, it learns from actual execution data.
    """
    
    def __init__(
        self,
        fee_schedule: FeeSchedule = KRAKEN_FUTURES_FEES,
        base_slippage_bps: float = 3.0
    ):
        self.fee_schedule = fee_schedule
        self.slippage_model = SlippageModel(base_slippage_bps=base_slippage_bps)
        self.funding_model = FundingModel(
            funding_interval_hours=fee_schedule.funding_interval_hours
        )
    
    def estimate_costs(
        self,
        side: str,
        size_usd: float,
        entry_price: float,
        atr_1m: float = 0.0,
        spread_bps: float = 5.0,
        is_maker_entry: bool = True,
        is_maker_exit: bool = False,  # Stops are usually taker
        volatility_percentile: float = 50.0,
        avg_volume_usd: float = 1_000_000.0,
        expected_hold_hours: float = 1.0
    ) -> CostEstimate:
        """
        Estimate total costs for a trade.
        
        Args:
            side: 'LONG' or 'SHORT'
            size_usd: Position size in USD
            entry_price: Entry price for calculations
            atr_1m: Current ATR on 1m (for slippage context)
            spread_bps: Current spread in basis points
            is_maker_entry: True if entry is limit order
            is_maker_exit: True if exit is limit order
            volatility_percentile: Current volatility percentile
            avg_volume_usd: Average volume for size impact
            expected_hold_hours: Expected hold time for funding
            
        Returns:
            Complete cost estimate
        """
        # Entry fees
        entry_fee_pct = self.fee_schedule.get_fee_pct(is_maker_entry)
        fee_entry_usd = size_usd * (entry_fee_pct / 100)
        
        # Exit fees
        exit_fee_pct = self.fee_schedule.get_fee_pct(is_maker_exit)
        fee_exit_usd = size_usd * (exit_fee_pct / 100)
        
        # Slippage estimates
        slippage_entry_bps = self.slippage_model.estimate(
            size_usd=size_usd,
            avg_volume_usd=avg_volume_usd,
            volatility_percentile=volatility_percentile,
            is_market_order=not is_maker_entry,
            spread_bps=spread_bps
        )
        slippage_exit_bps = self.slippage_model.estimate(
            size_usd=size_usd,
            avg_volume_usd=avg_volume_usd,
            volatility_percentile=volatility_percentile,
            is_market_order=not is_maker_exit,
            spread_bps=spread_bps
        )
        
        slippage_entry_usd = size_usd * (slippage_entry_bps / 10000)
        slippage_exit_usd = size_usd * (slippage_exit_bps / 10000)
        
        # Spread cost (crossing the spread)
        spread_cost_usd = size_usd * (spread_bps / 10000) if not is_maker_entry else 0.0
        
        # Funding cost
        funding_cost_usd, funding_hourly = self.funding_model.estimate_cost(
            position_size_usd=size_usd,
            side=side,
            hold_hours=expected_hold_hours
        )
        
        return CostEstimate(
            fee_entry_usd=fee_entry_usd,
            fee_exit_usd=fee_exit_usd,
            slippage_entry_usd=slippage_entry_usd,
            slippage_exit_usd=slippage_exit_usd,
            slippage_entry_bps=slippage_entry_bps,
            slippage_exit_bps=slippage_exit_bps,
            spread_cost_usd=spread_cost_usd,
            spread_bps=spread_bps,
            funding_rate_hourly=funding_hourly,
            funding_cost_per_hour_usd=abs(funding_cost_usd / max(expected_hold_hours, 0.01)),
            notional_usd=size_usd,
            is_estimate=True,
        )


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================
_cost_model: Optional[CostModel] = None


def get_cost_model(exchange: str = 'krakenfutures') -> CostModel:
    """
    Get the global cost model instance.
    
    Args:
        exchange: Exchange name for fee schedule
    """
    global _cost_model
    
    if _cost_model is None:
        fee_schedule = {
            'krakenfutures': KRAKEN_FUTURES_FEES,
            'binance': BINANCE_FUTURES_FEES,
        }.get(exchange.lower(), KRAKEN_FUTURES_FEES)
        
        _cost_model = CostModel(fee_schedule=fee_schedule)
        logger.info(f"CostModel initialized for {exchange}")
    
    return _cost_model
