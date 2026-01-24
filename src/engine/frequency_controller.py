"""
Trade Frequency Controller - Regime-aware trade frequency management

PRINCIPLE (Jan 20, 2026):
Different regimes allow different trade frequencies:
- TREND: More trades (riding momentum, multiple entries)
- RANGE_ACTIVO: Moderate frequency (selective mean-reversion)
- RANGE_PASIVO: Limited trades (low opportunity)
- EXHAUSTION: Very limited (high risk of reversal)
- TRANSITIONAL: Minimal (wait for clarity)

This prevents:
1. Overtrading in low-quality regimes
2. Missing opportunities in high-quality regimes
3. Revenge trading after losses
4. Clustering trades in short windows
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Optional, Tuple
import logging

from src.engine.regime.trading_regime import TradingRegime, REGIME_CHARACTERISTICS

logger = logging.getLogger(__name__)


@dataclass
class FrequencyTradeEntry:
    """Record of a trade for frequency tracking (local to frequency controller)"""
    timestamp: datetime
    regime: TradingRegime
    direction: str
    is_winner: Optional[bool] = None


@dataclass
class FrequencyDecision:
    """Result from frequency controller."""
    allow_trade: bool
    reason: str
    
    # Stats
    trades_in_session: int
    trades_in_regime: int
    minutes_since_last_trade: float
    
    # Limits (from regime)
    max_trades_allowed: int
    min_minutes_required: int
    
    # Warnings
    approaching_limit: bool = False
    cooldown_active: bool = False
    
    def __str__(self) -> str:
        if self.allow_trade:
            warn = " ⚠️ approaching limit" if self.approaching_limit else ""
            return f"✅ Trade allowed ({self.trades_in_session}/{self.max_trades_allowed}){warn}"
        else:
            return f"🚫 Trade blocked: {self.reason}"


class TradeFrequencyController:
    """
    Controls trade frequency based on regime and recent activity.
    
    Rules:
    1. Respect regime-specific limits
    2. Enforce minimum spacing between trades
    3. Extra cooldown after losses
    4. Session-aware (reset daily)
    
    Usage:
        controller = TradeFrequencyController()
        
        decision = controller.can_trade(
            regime=TradingRegime.RANGE_PASIVO,
            direction='LONG'
        )
        
        if not decision.allow_trade:
            logger.info(f"Trade blocked: {decision.reason}")
            return
        
        # Execute trade...
        
        # Record the trade
        controller.record_trade(
            regime=TradingRegime.RANGE_PASIVO,
            direction='LONG'
        )
    """
    
    # Extra cooldown after loss (minutes)
    LOSS_COOLDOWN_MINUTES = 5
    
    # Session start hour (UTC)
    SESSION_START_HOUR = 0
    
    def __init__(self):
        self._trades: List[FrequencyTradeEntry] = []
        self._session_start: Optional[datetime] = None
        self._last_trade_time: Optional[datetime] = None
        self._consecutive_losses = 0
        self._loss_cooldown_until: Optional[datetime] = None
    
    def _check_session_reset(self) -> None:
        """Reset if new session."""
        now = datetime.now(UTC)
        session_start = now.replace(
            hour=self.SESSION_START_HOUR, 
            minute=0, 
            second=0, 
            microsecond=0
        )
        
        if self._session_start is None or session_start > self._session_start:
            self._trades = []
            self._session_start = session_start
            self._consecutive_losses = 0
            self._loss_cooldown_until = None
            logger.info("📅 New trading session started")
    
    def can_trade(self, 
                  regime: TradingRegime,
                  direction: str | None = None) -> FrequencyDecision:
        """
        Check if a new trade is allowed.
        
        Args:
            regime: Current trading regime
            direction: 'LONG' or 'SHORT' (optional, for direction-specific limits)
        
        Returns:
            FrequencyDecision with allow_trade and details
        """
        self._check_session_reset()
        now = datetime.now(UTC)
        
        # Get regime characteristics
        chars = REGIME_CHARACTERISTICS.get(regime, REGIME_CHARACTERISTICS[TradingRegime.UNKNOWN])
        
        # Count session trades
        session_trades = len(self._trades)
        
        # Count trades in current regime
        regime_trades = len([t for t in self._trades if t.regime == regime])
        
        # Calculate time since last trade
        minutes_since_last = float('inf')
        if self._last_trade_time:
            delta = now - self._last_trade_time
            minutes_since_last = delta.total_seconds() / 60
        
        # =====================================================================
        # CHECK 1: Loss cooldown
        # =====================================================================
        if self._loss_cooldown_until and now < self._loss_cooldown_until:
            remaining = (self._loss_cooldown_until - now).total_seconds() / 60
            return FrequencyDecision(
                allow_trade=False,
                reason=f"Loss cooldown ({remaining:.1f}min remaining)",
                trades_in_session=session_trades,
                trades_in_regime=regime_trades,
                minutes_since_last_trade=minutes_since_last,
                max_trades_allowed=chars.max_trades_per_session,
                min_minutes_required=chars.min_minutes_between_trades,
                cooldown_active=True
            )
        
        # =====================================================================
        # CHECK 2: Maximum trades per session
        # =====================================================================
        if session_trades >= chars.max_trades_per_session:
            return FrequencyDecision(
                allow_trade=False,
                reason=f"Max trades ({chars.max_trades_per_session}) reached for {regime.value}",
                trades_in_session=session_trades,
                trades_in_regime=regime_trades,
                minutes_since_last_trade=minutes_since_last,
                max_trades_allowed=chars.max_trades_per_session,
                min_minutes_required=chars.min_minutes_between_trades
            )
        
        # =====================================================================
        # CHECK 3: Minimum spacing between trades
        # =====================================================================
        if minutes_since_last < chars.min_minutes_between_trades:
            wait_minutes = chars.min_minutes_between_trades - minutes_since_last
            return FrequencyDecision(
                allow_trade=False,
                reason=f"Minimum spacing: wait {wait_minutes:.1f}min",
                trades_in_session=session_trades,
                trades_in_regime=regime_trades,
                minutes_since_last_trade=minutes_since_last,
                max_trades_allowed=chars.max_trades_per_session,
                min_minutes_required=chars.min_minutes_between_trades
            )
        
        # =====================================================================
        # CHECK 4: Direction-specific limits (optional)
        # =====================================================================
        if direction:
            dir_trades = len([t for t in self._trades if t.direction == direction])
            # Limit any single direction to 60% of total allowed
            max_dir = int(chars.max_trades_per_session * 0.6) + 1
            if dir_trades >= max_dir:
                return FrequencyDecision(
                    allow_trade=False,
                    reason=f"Max {direction} trades ({max_dir}) reached",
                    trades_in_session=session_trades,
                    trades_in_regime=regime_trades,
                    minutes_since_last_trade=minutes_since_last,
                    max_trades_allowed=chars.max_trades_per_session,
                    min_minutes_required=chars.min_minutes_between_trades
                )
        
        # =====================================================================
        # ALLOWED - Check if approaching limit
        # =====================================================================
        approaching_limit = session_trades >= chars.max_trades_per_session - 1
        
        return FrequencyDecision(
            allow_trade=True,
            reason="Trade allowed",
            trades_in_session=session_trades,
            trades_in_regime=regime_trades,
            minutes_since_last_trade=minutes_since_last,
            max_trades_allowed=chars.max_trades_per_session,
            min_minutes_required=chars.min_minutes_between_trades,
            approaching_limit=approaching_limit
        )
    
    def record_trade(self,
                     regime: TradingRegime,
                     direction: str,
                     is_winner: bool | None = None) -> None:
        """
        Record a trade that was executed.
        
        Args:
            regime: Regime at time of trade
            direction: 'LONG' or 'SHORT'
            is_winner: True/False/None (None if still open)
        """
        now = datetime.now(UTC)
        
        self._trades.append(FrequencyTradeEntry(
            timestamp=now,
            regime=regime,
            direction=direction,
            is_winner=is_winner
        ))
        
        self._last_trade_time = now
        
        logger.info(f"📝 Trade recorded: {direction} in {regime.value} | "
                   f"Session total: {len(self._trades)}")
    
    def record_trade_result(self, is_winner: bool) -> None:
        """
        Update the last trade with its result.
        
        Args:
            is_winner: True if trade was profitable
        """
        if self._trades:
            self._trades[-1].is_winner = is_winner
            
            if is_winner:
                self._consecutive_losses = 0
                self._loss_cooldown_until = None
            else:
                self._consecutive_losses += 1
                # Apply escalating cooldown after losses
                cooldown_minutes = self.LOSS_COOLDOWN_MINUTES * self._consecutive_losses
                self._loss_cooldown_until = datetime.now(UTC) + timedelta(minutes=cooldown_minutes)
                logger.warning(f"⚠️ Loss #{self._consecutive_losses} - cooldown {cooldown_minutes}min")
    
    def get_session_stats(self) -> Dict:
        """Get statistics for current session."""
        self._check_session_reset()
        
        if not self._trades:
            return {
                'total_trades': 0,
                'wins': 0,
                'losses': 0,
                'by_regime': {},
                'by_direction': {}
            }
        
        completed = [t for t in self._trades if t.is_winner is not None]
        wins = [t for t in completed if t.is_winner]
        
        by_regime = {}
        for t in self._trades:
            regime_name = t.regime.value
            if regime_name not in by_regime:
                by_regime[regime_name] = 0
            by_regime[regime_name] += 1
        
        by_direction = {'LONG': 0, 'SHORT': 0}
        for t in self._trades:
            if t.direction in by_direction:
                by_direction[t.direction] += 1
        
        return {
            'total_trades': len(self._trades),
            'completed': len(completed),
            'wins': len(wins),
            'losses': len(completed) - len(wins),
            'win_rate': len(wins) / len(completed) * 100 if completed else 0,
            'consecutive_losses': self._consecutive_losses,
            'by_regime': by_regime,
            'by_direction': by_direction,
            'session_start': self._session_start.isoformat() if self._session_start else None
        }
    
    def force_cooldown(self, minutes: int, reason: str) -> None:
        """Force a cooldown period."""
        self._loss_cooldown_until = datetime.now(UTC) + timedelta(minutes=minutes)
        logger.warning(f"🚫 Forced cooldown: {minutes}min - {reason}")
    
    def clear_cooldown(self) -> None:
        """Clear any active cooldown."""
        self._loss_cooldown_until = None
        logger.info("✅ Cooldown cleared")


# =============================================================================
# SINGLETON
# =============================================================================

_frequency_controller: Optional[TradeFrequencyController] = None


def get_frequency_controller() -> TradeFrequencyController:
    """Get or create the frequency controller singleton."""
    global _frequency_controller
    if _frequency_controller is None:
        _frequency_controller = TradeFrequencyController()
    return _frequency_controller


def reset_frequency_controller():
    """Reset the singleton."""
    global _frequency_controller
    _frequency_controller = None


def can_trade_now(regime: TradingRegime, direction: str | None = None) -> FrequencyDecision:
    """Convenience function to check if trade is allowed."""
    return get_frequency_controller().can_trade(regime, direction)
