"""
BTC Correlation Filter - Check BTC trend alignment before entries

For non-BTC symbols: Checks if BTC trend aligns with trade direction
For BTC symbols: Uses as additional "macro context" check

Logic:
- LONG on altcoin + BTC BEARISH = BLOCK (swimming against the tide)
- SHORT on altcoin + BTC BULLISH = BLOCK (swimming against the tide)
- Any trade + BTC NEUTRAL = ALLOW (no strong signal)

Created: January 15, 2026
"""

import logging
from typing import Optional
from datetime import datetime

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision
from src.domain.market.structure.btc_context_feed import (
    BTCMarketContextFeed, BTCMarketContext,
    get_btc_context_feed, reset_btc_context_feed
)

logger = logging.getLogger(__name__)


class BTCCorrelationFilter(BaseFilter):
    """
    Filter entries based on BTC trend alignment.
    
    Use cases:
    1. Trading altcoins: Avoid LONG if BTC is bearish (correlation drag)
    2. Trading BTC itself: Use as macro context (avoid fighting 1h trend)
    
    Modes:
    - STRICT: Block trades against BTC trend
    - PENALTY: Apply score penalty but don't block
    - CONTEXT_ONLY: Just tag trades, no filtering
    """
    
    # Penalty for counter-trend trades
    COUNTER_TREND_PENALTY = -3.0
    
    # Bonus for aligned trades
    ALIGNED_BONUS = 1.0
    
    # Cooldown after regime change (seconds)
    REGIME_CHANGE_COOLDOWN = 300  # 5 min
    
    def __init__(self,
                 enabled: bool = True,
                 mode: str = "PENALTY",  # STRICT, PENALTY, CONTEXT_ONLY
                 is_btc_symbol: bool = True,  # True if trading BTC itself
                 btc_feed: BTCMarketContextFeed | None = None):
        """
        Initialize BTC correlation filter.
        
        Args:
            enabled: Enable/disable the filter
            mode: STRICT (block), PENALTY (score adjust), CONTEXT_ONLY (tag only)
            is_btc_symbol: True if trading BTC (use as macro context)
            btc_feed: Optional BTCMarketContextFeed instance
        """
        self.enabled = enabled
        self.mode = mode.upper()
        self.is_btc_symbol = is_btc_symbol
        self.btc_feed = btc_feed or get_btc_context_feed()
        
        # Track last trend for regime change detection
        self._last_trend: Optional[str] = None
        self._regime_change_time: Optional[datetime] = None
        
        if self.mode not in ["STRICT", "PENALTY", "CONTEXT_ONLY"]:
            logger.warning(f"Unknown mode {mode}, defaulting to PENALTY")
            self.mode = "PENALTY"
        
        logger.info(f"BTCCorrelationFilter initialized: enabled={enabled}, mode={self.mode}, is_btc={is_btc_symbol}")
    
    def evaluate(self,
                 bias: str | None = None,
                 market_time: datetime | None = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate BTC correlation for trade.
        
        Args:
            bias: Trade direction ('LONG' or 'SHORT')
            market_time: Current market time
            **kwargs: Additional context (ignored)
        
        Returns:
            FilterDecision with allow/score_adjust/tags
        """
        if not self.enabled:
            return FilterDecision(allow=True)
        
        if bias is None:
            return FilterDecision(allow=True, tags={'btc_filter': 'no_bias'})
        
        bias = bias.upper()
        
        # Get BTC context
        context = self.btc_feed.get_context()
        
        # Check if data is stale
        if context.is_stale:
            logger.debug("BTC context stale, allowing trade")
            return FilterDecision(
                allow=True,
                tags={
                    'btc_trend': 'STALE',
                    'btc_filter': 'stale_data'
                }
            )
        
        # Check if feed is ready
        if not self.btc_feed.is_ready():
            return FilterDecision(
                allow=True,
                tags={
                    'btc_trend': 'NOT_READY',
                    'btc_filter': 'feed_not_ready'
                }
            )
        
        # Get BTC trend
        btc_trend = context.trend_bias
        btc_strength = context.trend_strength
        
        # Detect regime change
        self._detect_regime_change(btc_trend, market_time)
        
        # Build context tags
        tags = {
            'btc_trend': btc_trend,
            'btc_strength': round(btc_strength, 2),
            'btc_ema_slope': round(context.ema_slope_1h * 100, 4),
            'btc_volatility': context.volatility_regime,
            'btc_filter_mode': self.mode,
        }
        
        # Check alignment
        is_aligned = self._check_alignment(bias, btc_trend)
        tags['btc_aligned'] = is_aligned
        
        # In cooldown after regime change?
        if self._in_regime_cooldown(market_time):
            logger.info(f"⏳ BTC CORRELATION: In regime change cooldown, allowing cautiously")
            return FilterDecision(
                allow=True,
                score_adjust=-1.0,  # Small penalty during uncertainty
                tags={**tags, 'btc_filter': 'regime_cooldown'}
            )
        
        # Handle based on mode and alignment
        if is_aligned:
            # Trend aligned - give bonus
            if btc_trend != "NEUTRAL":
                logger.debug(f"✅ BTC ALIGNED: {bias} aligns with BTC {btc_trend}")
                return FilterDecision(
                    allow=True,
                    score_adjust=self.ALIGNED_BONUS,
                    tags={**tags, 'btc_filter': 'aligned'}
                )
            else:
                return FilterDecision(allow=True, tags={**tags, 'btc_filter': 'neutral'})
        
        else:
            # Counter-trend!
            msg = f"⚠️ BTC COUNTER-TREND: {bias} vs BTC {btc_trend} (strength={btc_strength:.2f})"
            
            if self.mode == "STRICT":
                logger.warning(f"⛔ {msg} → BLOCKED")
                return FilterDecision(
                    allow=False,
                    reasons=[f"Counter BTC trend: {bias} vs {btc_trend}"],
                    tags={**tags, 'btc_filter': 'blocked'}
                )
            
            elif self.mode == "PENALTY":
                # Apply penalty proportional to trend strength
                penalty = self.COUNTER_TREND_PENALTY * btc_strength
                logger.info(f"{msg} → penalty {penalty:.1f}")
                return FilterDecision(
                    allow=True,
                    score_adjust=penalty,
                    reasons=[f"Counter BTC trend"],
                    tags={**tags, 'btc_filter': 'penalized'}
                )
            
            else:  # CONTEXT_ONLY
                logger.debug(f"{msg} (context only, no action)")
                return FilterDecision(allow=True, tags={**tags, 'btc_filter': 'context_only'})
    
    def _check_alignment(self, bias: str, btc_trend: str) -> bool:
        """Check if trade bias aligns with BTC trend."""
        if btc_trend == "NEUTRAL":
            return True
        
        if self.is_btc_symbol:
            # For BTC itself: only block if directly opposing
            if bias == "LONG" and btc_trend == "BEARISH":
                return False
            if bias == "SHORT" and btc_trend == "BULLISH":
                return False
        else:
            # For altcoins: stricter alignment
            if bias == "LONG" and btc_trend == "BEARISH":
                return False
            if bias == "SHORT" and btc_trend == "BULLISH":
                return False
        
        return True
    
    def _detect_regime_change(self, current_trend: str, market_time: datetime | None = None):
        """Detect BTC trend regime change."""
        if market_time is None:
            from datetime import UTC
            market_time = datetime.now(UTC)
        
        if self._last_trend is not None and self._last_trend != current_trend:
            logger.info(f"📊 BTC REGIME CHANGE: {self._last_trend} → {current_trend}")
            self._regime_change_time = market_time
        
        self._last_trend = current_trend
    
    def _in_regime_cooldown(self, market_time: datetime | None = None) -> bool:
        """Check if in cooldown after regime change."""
        if self._regime_change_time is None:
            return False
        
        if market_time is None:
            from datetime import UTC
            market_time = datetime.now(UTC)
        
        elapsed = (market_time - self._regime_change_time).total_seconds()
        return elapsed < self.REGIME_CHANGE_COOLDOWN
    
    def get_current_trend(self) -> str:
        """Get current BTC trend."""
        return self.btc_feed.get_context().trend_bias
    
    def get_context(self) -> BTCMarketContext:
        """Get full BTC context."""
        return self.btc_feed.get_context()


# Singleton instance
_btc_correlation_filter: Optional[BTCCorrelationFilter] = None


def get_btc_correlation_filter(enabled: bool = True,
                                mode: str = "PENALTY",
                                is_btc_symbol: bool = True) -> BTCCorrelationFilter:
    """Get or create singleton BTCCorrelationFilter."""
    global _btc_correlation_filter
    
    if _btc_correlation_filter is None:
        _btc_correlation_filter = BTCCorrelationFilter(
            enabled=enabled,
            mode=mode,
            is_btc_symbol=is_btc_symbol
        )
    
    return _btc_correlation_filter


def reset_btc_correlation_filter():
    """Reset singleton for testing."""
    global _btc_correlation_filter
    _btc_correlation_filter = None
