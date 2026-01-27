"""
Filter Pipeline Integration - Centralizes all entry filters for signal_engine

This module provides a single entry point for running all filters and
aggregating their decisions using the FilterDecision interface.

Filters included:
1. BB Width Filter - Bollinger Band compression detection
2. Volatility Shock Filter - TrueRange > k*ATR with cooldown
3. Macro Trend Filter - EMA200 on 15m + 1h with slope veto
4. Post-Sweep Confirmation - Break+retest after sweep
5. Cost Filter - Spread vs risk, fee-adjusted R
6. Overtrade Filter - Same-zone prevention, HH/HL reset

PHASE A FIX (Jan 18, 2026): 
- Use merge_dedup() consistently for regime-based filters to avoid double penalties
- Added regime_source to all decisions for proper deduplication
- Categorized filters into CONTEXT vs EXECUTION for hierarchy

Created: January 15, 2026
Updated: January 18, 2026 - Phase A sobreprotección fix
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
import logging

from src.domain.market.filters.filter_interface import FilterDecision, FilterPipeline, BaseFilter
from src.domain.market.filters.bb_width_filter import BBWidthFilter, get_bb_width_filter
from src.domain.market.filters.volatility_shock_filter import VolatilityShockFilter, get_volatility_shock_filter
from src.domain.market.filters.macro_trend_filter import MacroTrendFilter, get_macro_trend_filter
from src.domain.market.filters.post_sweep_filter import PostSweepConfirmationFilter, get_post_sweep_filter
from src.domain.market.filters.cost_filter import CostFilter, get_cost_filter
from src.domain.market.filters.overtrade_filter import OvertradeFilter, get_overtrade_filter
from src.domain.market.filters.btc_correlation_filter import BTCCorrelationFilter, get_btc_correlation_filter

logger = logging.getLogger(__name__)


class EntryFilterPipeline:
    """
    Manages all entry filters and provides a unified evaluation interface.
    
    Usage in signal_engine:
        # During initialization:
        self.filter_pipeline = EntryFilterPipeline()
        
        # During _handle_idle_state:
        decision = self.filter_pipeline.evaluate(
            df_5m=df_5m,
            df_15m=df_15m,
            df_1h=df_1h,
            current_price=current_price,
            atr=atr,
            bias=bias,
            ...
        )
        
        if not decision.allow:
            return  # Blocked
        
        score += decision.score_adjust
        self._current_trade_tags.update(decision.tags)
    """
    
    def __init__(self,
                 bb_width_enabled: bool = True,
                 volatility_shock_enabled: bool = True,
                 macro_trend_enabled: bool = True,
                 post_sweep_enabled: bool = True,
                 cost_filter_enabled: bool = True,
                 overtrade_enabled: bool = True,
                 btc_correlation_enabled: bool = True,
                 btc_correlation_mode: str = "PENALTY",
                 is_btc_symbol: bool = True):
        """
        Initialize the filter pipeline.
        
        Args:
            *_enabled: Enable/disable individual filters
            btc_correlation_mode: STRICT, PENALTY, or CONTEXT_ONLY
            is_btc_symbol: True if trading BTC itself
        """
        # Initialize filters
        self.bb_width_filter = get_bb_width_filter(enabled=bb_width_enabled)
        self.volatility_shock_filter = get_volatility_shock_filter(enabled=volatility_shock_enabled)
        self.macro_trend_filter = get_macro_trend_filter(enabled=macro_trend_enabled)
        self.post_sweep_filter = get_post_sweep_filter(enabled=post_sweep_enabled)
        self.cost_filter = get_cost_filter(enabled=cost_filter_enabled)
        self.overtrade_filter = get_overtrade_filter(enabled=overtrade_enabled)
        self.btc_correlation_filter = get_btc_correlation_filter(
            enabled=btc_correlation_enabled,
            mode=btc_correlation_mode,
            is_btc_symbol=is_btc_symbol
        )
        
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
                 regime_mode: str = 'TREND',  # 🔥 FIX (Jan 22): Added regime_mode parameter
                 
                 # Control
                 early_exit: bool = True) -> FilterDecision:
        """
        Run all filters and return aggregated decision.
        
        Args:
            df_*: OHLC DataFrames for various timeframes
            current_price: Current market price
            bias: 'LONG' or 'SHORT'
            entry_price: Planned entry (defaults to current_price)
            stop_loss: Planned stop loss
            target_price: Planned take profit
            spread: Current bid-ask spread
            atr: Current ATR
            market_time: Current timestamp
            early_exit: Stop on first block
        
        Returns:
            Aggregated FilterDecision
        """
        entry_price = entry_price or current_price
        market_time = market_time or datetime.utcnow()
        
        self._last_decisions = {}
        
        # Start with allow
        aggregate = FilterDecision(
            allow=True,
            reasons=[],
            tags={}
        )
        
        # ===== 1. VOLATILITY SHOCK (check first - can pause everything) =====
        if self.volatility_shock_filter.enabled:
            decision = self.volatility_shock_filter.evaluate(
                df=df_5m,
                market_time=market_time,
                timeframe='5m'  # 🔥 FIX (Jan 22): Pass timeframe for TF-specific thresholds
            )
            decision.regime_source = 'volatility_shock'
            self._last_decisions['volatility_shock'] = decision
            # Use merge_dedup to avoid double-penalizing for same regime
            aggregate = aggregate.merge_dedup(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'volatility_shock'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [volatility]: {decision.reasons[0] if decision.reasons else 'shock'}")
                return aggregate
        
        # ===== 2. BB WIDTH (compression detection) =====
        if self.bb_width_filter.enabled:
            decision = self.bb_width_filter.evaluate(
                df=df_5m
            )
            decision.regime_source = 'bb_width'
            self._last_decisions['bb_width'] = decision
            # Use merge_dedup - BB Width determines regime, avoid duplicate penalties
            aggregate = aggregate.merge_dedup(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'bb_width'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [bb_width]: {decision.reasons[0] if decision.reasons else 'compression'}")
                return aggregate
        
        # ===== 3. MACRO TREND (15m + 1h EMA200 + slope) =====
        # CONTEXT category - use merge_dedup to avoid double penalty with other context filters
        if self.macro_trend_filter.enabled and bias:
            # 🔥 FIX (Jan 22): Use regime_mode parameter directly
            # In RANGE mode, macro slope never hard-blocks (only penalizes)
            decision = self.macro_trend_filter.evaluate(
                df_15m=df_15m,
                df_1h=df_1h,
                current_price=current_price,
                bias=bias,
                regime_mode=regime_mode
            )
            decision.regime_source = 'macro_trend'  # Mark source for dedup
            self._last_decisions['macro_trend'] = decision
            # PHASE A FIX: Use merge_dedup for regime-based filter
            aggregate = aggregate.merge_dedup(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'macro_trend'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [macro]: {decision.reasons[0] if decision.reasons else 'veto'}")
                return aggregate
        
        # ===== 4. POST-SWEEP CONFIRMATION =====
        # CONTEXT category - structural filter
        if self.post_sweep_filter.enabled and bias:
            # Use df_1m if available, otherwise df_5m (can't use 'or' with DataFrames)
            df_short_tf = df_1m if df_1m is not None and not df_1m.empty else df_5m
            decision = self.post_sweep_filter.evaluate(
                df=df_short_tf,
                current_price=current_price,
                atr=atr,
                bias=bias
            )
            decision.regime_source = 'post_sweep'
            self._last_decisions['post_sweep'] = decision
            # PHASE A FIX: Use merge_dedup for consistency
            aggregate = aggregate.merge_dedup(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'post_sweep'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [sweep]: {decision.reasons[0] if decision.reasons else 'blocked'}")
                return aggregate
        
        # ===== 5. COST FILTER (if we have SL/TP) =====
        # EXECUTION category - different from CONTEXT, use regular merge
        if self.cost_filter.enabled and entry_price and stop_loss:
            decision = self.cost_filter.evaluate(
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                spread=spread,
                bias=bias
            )
            decision.regime_source = 'cost'  # EXECUTION category
            self._last_decisions['cost'] = decision
            # Cost is EXECUTION category - use regular merge (doesn't dedup with CONTEXT)
            aggregate = aggregate.merge(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'cost'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [cost]: {decision.reasons[0] if decision.reasons else 'rr'}")
                return aggregate
        
        # ===== 6. OVERTRADE FILTER (same-zone + reset) =====
        # EXECUTION category
        if self.overtrade_filter.enabled and bias:
            # Use df_1m if available, otherwise df_5m (can't use 'or' with DataFrames)
            df_short_tf = df_1m if df_1m is not None and not df_1m.empty else df_5m
            decision = self.overtrade_filter.evaluate(
                df=df_short_tf,
                current_price=current_price,
                atr=atr,
                bias=bias,
                market_time=market_time
            )
            decision.regime_source = 'overtrade'  # EXECUTION category
            self._last_decisions['overtrade'] = decision
            # Overtrade is EXECUTION - use regular merge
            aggregate = aggregate.merge(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'overtrade'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [overtrade]: {decision.reasons[0] if decision.reasons else 'limit'}")
                return aggregate
        
        # ===== 7. BTC CORRELATION FILTER (macro context) =====
        # CONTEXT category - use merge_dedup
        if self.btc_correlation_filter.enabled and bias:
            decision = self.btc_correlation_filter.evaluate(
                bias=bias,
                market_time=market_time
            )
            decision.regime_source = 'btc_correlation'  # CONTEXT category
            self._last_decisions['btc_correlation'] = decision
            # PHASE A FIX: Use merge_dedup for context filter
            aggregate = aggregate.merge_dedup(decision)
            
            if not decision.allow and early_exit:
                aggregate.tags['blocked_by'] = 'btc_correlation'
                self._last_aggregate = aggregate
                logger.warning(f"[BLOCK] FILTER [btc_corr]: {decision.reasons[0] if decision.reasons else 'blocked'}")
                return aggregate
        
        # ===== FINAL AGGREGATE =====
        self._last_aggregate = aggregate
        
        # Log summary - only at INFO if significant adjustment
        if aggregate.score_adjust != 0:
            adj_str = f"{aggregate.score_adjust:+.1f}"
            if abs(aggregate.score_adjust) >= 1.5:
                logger.info(f"[DATA] FILTER: adj={adj_str}, {aggregate.reasons[0] if aggregate.reasons else 'ok'}")
            else:
                logger.debug(f"FILTER: adj={adj_str}, {aggregate.reasons[:2]}")
        
        return aggregate
    
    def record_trade_result(self,
                           entry_price: float,
                           exit_price: float,
                           entry_time: datetime,
                           exit_time: datetime,
                           direction: str,
                           pnl: float):
        """
        Record a trade result for filters that track history.
        Called by signal_engine after trade closes.
        """
        # Overtrade filter needs trade history
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
        from src.domain.market.filters.bb_width_filter import reset_bb_width_filter
        from src.domain.market.filters.volatility_shock_filter import reset_volatility_shock_filter
        from src.domain.market.filters.macro_trend_filter import reset_macro_trend_filter
        from src.domain.market.filters.post_sweep_filter import reset_post_sweep_filter
        from src.domain.market.filters.cost_filter import reset_cost_filter
        from src.domain.market.filters.overtrade_filter import reset_overtrade_filter
        from src.domain.market.filters.btc_correlation_filter import reset_btc_correlation_filter
        from src.domain.market.structure.btc_context_feed import reset_btc_context_feed
        
        reset_bb_width_filter()
        reset_volatility_shock_filter()
        reset_macro_trend_filter()
        reset_post_sweep_filter()
        reset_cost_filter()
        reset_overtrade_filter()
        reset_btc_correlation_filter()
        reset_btc_context_feed()
        
        # Re-initialize
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
