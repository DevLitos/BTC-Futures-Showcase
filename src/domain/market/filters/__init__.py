"""
Filters Module - All trading filters for signal validation

Reorganized from engine/ (Jan 20, 2026)

Categories:
- Core filters: HTF, LTF, Kill conditions
- Advanced filters: Session quality, funding, divergence, VPOC
- Pipeline: Unified filter evaluation
- Specialized: Location, Micro trend, Spread, Cost, etc.
"""

# Core filters
from src.domain.market.filters.filters import (
    KillConditionsFilter,
    BlackSwanFilter,
    HTFFilter,
    LTFTriggerFilter,
)

# Filter interface and pipeline
from src.domain.market.filters.filter_interface import FilterDecision, BaseFilter
from src.domain.market.filters.filter_pipeline import EntryFilterPipeline, get_entry_filter_pipeline

# Advanced filters
from src.domain.market.filters.advanced_filters import (
    check_funding_rate_safe,
    should_exit_on_funding,
    check_session_quality,
    check_htf_divergence,
    calculate_vpoc,
    check_vpoc_validity,
    check_btc_trend,
)

# Specialized filters
from src.domain.market.filters.location_filter import LocationFilter, get_location_filter, LocationContext
from src.domain.market.filters.micro_trend_filter import MicroTrendFilter, get_micro_trend_filter, MicroTrendState
from src.domain.market.filters.spread_filter import SpreadFilter, get_spread_filter, check_spread_for_entry, SpreadCheckResult
from src.domain.market.filters.cost_filter import CostFilter, get_cost_filter
from src.domain.market.filters.overtrade_filter import OvertradeFilter, get_overtrade_filter
from src.domain.market.filters.bb_width_filter import BBWidthFilter, get_bb_width_filter
from src.domain.market.filters.volatility_shock_filter import VolatilityShockFilter, get_volatility_shock_filter
from src.domain.market.filters.macro_trend_filter import MacroTrendFilter, get_macro_trend_filter
from src.domain.market.filters.post_sweep_filter import PostSweepConfirmationFilter, get_post_sweep_filter
from src.domain.market.filters.btc_correlation_filter import BTCCorrelationFilter, get_btc_correlation_filter

__all__ = [
    # Core
    'KillConditionsFilter',
    'BlackSwanFilter', 
    'HTFFilter',
    'LTFTriggerFilter',
    # Interface
    'FilterDecision',
    'BaseFilter',
    'EntryFilterPipeline',
    'get_entry_filter_pipeline',
    # Advanced
    'check_funding_rate_safe',
    'should_exit_on_funding',
    'check_session_quality',
    'check_htf_divergence',
    'calculate_vpoc',
    'check_vpoc_validity',
    'check_btc_trend',
    # Specialized
    'LocationFilter',
    'get_location_filter',
    'LocationContext',
    'MicroTrendFilter',
    'get_micro_trend_filter',
    'MicroTrendState',
    'SpreadFilter',
    'get_spread_filter',
    'check_spread_for_entry',
    'SpreadCheckResult',
    'CostFilter',
    'get_cost_filter',
    'OvertradeFilter',
    'get_overtrade_filter',
    'BBWidthFilter',
    'get_bb_width_filter',
    'VolatilityShockFilter',
    'get_volatility_shock_filter',
    'MacroTrendFilter',
    'get_macro_trend_filter',
    'PostSweepConfirmationFilter',
    'get_post_sweep_filter',
    'BTCCorrelationFilter',
    'get_btc_correlation_filter',
]
