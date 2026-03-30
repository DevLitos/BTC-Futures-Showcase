"""
Filter Pipeline — REFACTORED from 7 filters to 3.

DELETED:
- BB Width Filter — compression = opportunity, not threat. Blocked 239 times during a $1400 rally.
- Macro Trend Filter — redundant with direction_orchestrator + mode_handlers + edge_state_manager.
- Post-Sweep Filter — 74 false blocks per session in trending markets. ADX override was a patch.
- BTC Correlation Filter — we ARE BTC. This filter is for altcoins.

KEPT:
1. Volatility Shock Filter — TrueRange > k*ATR with cooldown (safety)
2. Cost Filter — Spread vs risk, fee-adjusted R (essential RR validation)
3. Overtrade Filter — Same-zone prevention, HH/HL reset (safety)
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
import logging

from src.domain.market.filters.filter_interface import FilterDecision, FilterPipeline, BaseFilter

_last_filter_log: dict[str, datetime] = {}
_filter_log_interval = timedelta(minutes=2)
from src.domain.market.filters.volatility_shock_filter import VolatilityShockFilter, get_volatility_shock_filter
from src.domain.market.filters.cost_filter import CostFilter, get_cost_filter
from src.domain.market.filters.overtrade_filter import OvertradeFilter, get_overtrade_filter

logger = logging.getLogger(__name__)


class EntryFilterPipeline:
    """
    Manages entry filters and provides a unified evaluation interface.
    3 filters: volatility_shock, cost, overtrade.
    """

    def __init__(self,
                 volatility_shock_enabled: bool = True,
                 cost_filter_enabled: bool = True,
                 overtrade_enabled: bool = True):
        """Initialize the filter pipeline: volatility_shock, cost, overtrade."""
        self.volatility_shock_filter = get_volatility_shock_filter(enabled=volatility_shock_enabled)
        self.cost_filter = get_cost_filter(enabled=cost_filter_enabled)
        self.overtrade_filter = get_overtrade_filter(enabled=overtrade_enabled)

        # Track last decisions for debugging
        self._last_decisions: Dict[str, FilterDecision] = {}
        self._last_aggregate: Optional[FilterDecision] = None

    def evaluate(self,
                 # Data inputs
                 df_1m: pd.DataFrame | None = None,
                 df_5m: pd.DataFrame | None = None,
                 df_15m: pd.DataFrame | None = None,
                 df_1h: pd.DataFrame | None = None,
                 current_price: float | None = None,

                 # Trade parameters
                 bias: str | None = None,
                 entry_price: float | None = None,
                 stop_loss: float | None = None,
                 target_price: float | None = None,
                 spread: float | None = None,
                 atr: float | None = None,

                 # Context
                 market_time: datetime | None = None,
                 regime_mode: str = 'TREND',
                 adx_1h: float = 20.0,

                 # Control
                 early_exit: bool = True) -> FilterDecision:
        """
        Run all filters and return aggregated decision.
        """
        entry_price = entry_price or current_price
        market_time = market_time or datetime.utcnow()

        self._last_decisions = {}

        aggregate = FilterDecision(
            allow=True,
            reasons=[],
            tags={}
        )

        # ===== 1. VOLATILITY SHOCK (safety — pauses everything) =====
        if self.volatility_shock_filter.enabled:
            decision = self.volatility_shock_filter.evaluate(
                df=df_5m,
                market_time=market_time,
                timeframe='5m'
            )
            decision.regime_source = 'volatility_shock'
            self._last_decisions['volatility_shock'] = decision
            aggregate = aggregate.merge_dedup(decision)

            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'volatility_shock'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [volatility]: {decision.reasons[0] if decision.reasons else 'shock'}")
                return aggregate

        # ===== 2. COST FILTER (RR validation) =====
        if self.cost_filter.enabled and entry_price and stop_loss:
            decision = self.cost_filter.evaluate(
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                spread=spread,
                bias=bias
            )
            decision.regime_source = 'cost'
            self._last_decisions['cost'] = decision
            aggregate = aggregate.merge(decision)

            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'cost'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [cost]: {decision.reasons[0] if decision.reasons else 'rr'}")
                return aggregate

        # ===== 3. OVERTRADE FILTER (same-zone prevention) =====
        if self.overtrade_filter.enabled and bias:
            df_short_tf = df_1m if df_1m is not None and not df_1m.empty else df_5m
            decision = self.overtrade_filter.evaluate(
                df=df_short_tf,
                current_price=current_price,
                atr=atr,
                bias=bias,
                market_time=market_time
            )
            decision.regime_source = 'overtrade'
            self._last_decisions['overtrade'] = decision
            aggregate = aggregate.merge(decision)

            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'overtrade'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [overtrade]: {decision.reasons[0] if decision.reasons else 'limit'}")
                return aggregate

        # ===== FINAL AGGREGATE =====
        self._last_aggregate = aggregate

        if aggregate.score_adjust != 0:
            adj_str = f"{aggregate.score_adjust:+.1f}"
            if not aggregate.allow:
                logger.info(f"[DATA] FILTER: adj={adj_str}, {aggregate.reasons[0] if aggregate.reasons else 'ok'}")
            else:
                logger.debug(f"[DATA] FILTER: adj={adj_str}, {aggregate.reasons[0] if aggregate.reasons else 'ok'}")

        return aggregate

    def record_trade_result(self,
                           entry_price: float,
                           exit_price: float,
                           entry_time: datetime,
                           exit_time: datetime,
                           direction: str,
                           pnl: float):
        """Record a trade result for filters that track history."""
        self.overtrade_filter.record_trade_from_params(
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            direction=direction,
            pnl=pnl
        )

    def get_filter_decisions(self) -> Dict[str, FilterDecision]:
        """Get individual filter decisions from last evaluation"""
        return self._last_decisions

    def get_last_decision(self) -> Optional[FilterDecision]:
        """Get last aggregated decision"""
        return self._last_aggregate

    def get_filter_tags(self) -> Dict[str, Any]:
        """Get all tags from last evaluation (for trade logging)"""
        if self._last_aggregate:
            return self._last_aggregate.tags
        return {}

    def reset_all(self):
        """Reset all filter states (for testing)"""
        from src.domain.market.filters.volatility_shock_filter import reset_volatility_shock_filter
        from src.domain.market.filters.cost_filter import reset_cost_filter
        from src.domain.market.filters.overtrade_filter import reset_overtrade_filter

        reset_volatility_shock_filter()
        reset_cost_filter()
        reset_overtrade_filter()

        self.__init__()


# Singleton
_entry_filter_pipeline: Optional[EntryFilterPipeline] = None


def get_entry_filter_pipeline(**kwargs) -> EntryFilterPipeline:
    """Get or create the entry filter pipeline singleton"""
    global _entry_filter_pipeline
    if _entry_filter_pipeline is None:
        _entry_filter_pipeline = EntryFilterPipeline(**kwargs)
    return _entry_filter_pipeline


def reset_entry_filter_pipeline():
    """Reset the singleton"""
    global _entry_filter_pipeline
    if _entry_filter_pipeline:
        _entry_filter_pipeline.reset_all()
    _entry_filter_pipeline = None
