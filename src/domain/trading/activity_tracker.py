"""
Activity Tracker Module
=======================

SINGLE SOURCE OF TRUTH for all trade activity tracking.

PROBLEM (Jan 27, 2026):
- System had multiple places updating _last_trade_time
- Activity Guard was reading from engine._last_trade_time
- But trades were executing and NOT updating this value
- Result: "24h without trade" message when trade happened minutes ago
- This triggered ANTI-PARALYSIS incorrectly → accepting garbage trades

SOLUTION:
This module is the ONLY place where trade activity is recorded.
All components MUST use this module for:
1. Recording trade activity
2. Getting time since last trade
3. Activity Guard decisions

NO COMPONENT should have its own _last_trade_time anymore.
"""

from datetime import datetime, UTC, timedelta
from typing import Optional
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ActivityLevel(Enum):
    """Activity level based on time since last trade."""
    NORMAL = "NORMAL"           # < 2h since trade
    MODERATE = "MODERATE"       # 2-4h since trade  
    SIGNIFICANT = "SIGNIFICANT" # 4-6h since trade
    SEVERE = "SEVERE"           # > 6h since trade


@dataclass
class ActivityStatus:
    """Current activity status."""
    last_trade_time: Optional[datetime]
    hours_since_trade: float
    level: ActivityLevel
    message: str
    
    def __str__(self) -> str:
        return f"Activity: {self.level.value} ({self.hours_since_trade:.1f}h since last trade)"


class ActivityTracker:
    """
    Central activity tracker for the trading system.
    
    USAGE:
        tracker = get_activity_tracker()
        
        # When trade is executed (entry filled)
        tracker.record_trade_executed(market_time)
        
        # When trade closes
        tracker.record_trade_closed(market_time, exit_reason, pnl_r)
        
        # Getting hours since last trade
        hours = tracker.get_hours_since_last_trade(current_time)
    """
    
    # NOTE: Loss-based blocking removed (Jan 27).
    # Position sizing already reduces after losses - no need for double punishment.
    # We still track WTP exits for observability but don't block on them.
    
    # =========================================================================
    # 🔥 FIX #10: HOT HAND SESSION LIMIT (Feb 02, 2026)
    # =========================================================================
    # Base limit = 3 trades per session (ALWAYS guaranteed)
    # After base is consumed:
    #   - If last trade was WIN: +1 trade allowed
    #   - If last trade was LOSS: session STOPPED
    # 
    # Example:
    #   Trade 1: LOSS → OK (within base)
    #   Trade 2: LOSS → OK (within base)
    #   Trade 3: WIN  → OK (base consumed, but WIN allows trade 4)
    #   Trade 4: WIN  → OK (bonus, WIN allows trade 5)
    #   Trade 5: LOSS → STOPPED (no more trades)
    # =========================================================================
    
    def __init__(self):
        self._last_trade_time: Optional[datetime] = None
        self._trade_count_today: int = 0
        self._last_trade_date: Optional[str] = None  # YYYY-MM-DD
        self._total_trades_session: int = 0
        
        # Loss tracking (for observability only, not blocking)
        self._consecutive_losses: int = 0
        self._losses_today: int = 0
        self._wins_today: int = 0
        self._worst_trade_preventions_today: int = 0
        self._last_loss_time: Optional[datetime] = None
        self._pnl_today_r: float = 0.0
        
        # 🔥 FIX #10: Hot Hand - track last trade result
        self._last_trade_was_win: bool = False
        self._current_session: Optional[str] = None  # Track session for reset
        
    def record_trade_executed(self, market_time: datetime) -> None:
        """
        Record that a trade was executed.
        
        CALL THIS when:
        - Entry order is FILLED (not just placed)
        - Position is confirmed on exchange
        
        DO NOT call when:
        - Order is placed but not filled
        - Signal is detected but not acted on
        - Position is adopted (existing position found)
        
        Args:
            market_time: The time the trade was executed
        """
        old_time = self._last_trade_time
        self._last_trade_time = market_time
        self._total_trades_session += 1
        
        # Track daily count
        date_str = market_time.strftime('%Y-%m-%d')
        if self._last_trade_date != date_str:
            # New day - reset daily counters
            self._trade_count_today = 1
            self._last_trade_date = date_str
            self._losses_today = 0
            self._wins_today = 0
            self._worst_trade_preventions_today = 0
            self._pnl_today_r = 0.0
        else:
            self._trade_count_today += 1
        
        if old_time:
            hours_since = (market_time - old_time).total_seconds() / 3600
            logger.info(
                f"📊 Activity Tracker: Trade recorded | "
                f"Time since previous: {hours_since:.1f}h | "
                f"Daily count: {self._trade_count_today} | "
                f"Session total: {self._total_trades_session}"
            )
        else:
            logger.info(
                f"📊 Activity Tracker: First trade recorded | "
                f"Daily count: {self._trade_count_today}"
            )
    
    def record_trade_closed(
        self, 
        market_time: datetime, 
        exit_reason: str, 
        pnl_r: float,
        direction: str = ""
    ) -> None:
        """
        Record trade closure with outcome tracking (observability only).
        
        Args:
            market_time: Time of trade close
            exit_reason: Why the trade closed (e.g., WORST_TRADE_PREVENTION)
            pnl_r: PnL in R-multiples (negative = loss)
            direction: LONG or SHORT
        """
        is_loss = pnl_r < 0
        is_wtp = 'WORST_TRADE_PREVENTION' in exit_reason
        
        self._pnl_today_r += pnl_r
        
        if is_loss:
            self._consecutive_losses += 1
            self._losses_today += 1
            self._last_loss_time = market_time
            
            # 🔥 FIX #10: Track last trade result
            self._last_trade_was_win = False
            
            if is_wtp:
                self._worst_trade_preventions_today += 1
                logger.warning(
                    f"⚠️ Activity Tracker: WORST_TRADE_PREVENTION loss #{self._worst_trade_preventions_today} | "
                    f"Consecutive losses: {self._consecutive_losses} | "
                    f"PnL today: {self._pnl_today_r:.2f}R"
                )
            else:
                logger.info(
                    f"📊 Activity Tracker: Loss #{self._losses_today} | "
                    f"Consecutive: {self._consecutive_losses} | Exit: {exit_reason}"
                )
        else:
            # Win resets consecutive losses
            if self._consecutive_losses > 0:
                logger.info(
                    f"✅ Activity Tracker: Win breaks {self._consecutive_losses}-loss streak | "
                    f"PnL: {pnl_r:.2f}R"
                )
            self._consecutive_losses = 0
            self._wins_today += 1
            
            # 🔥 FIX #10: Track last trade result - enables Hot Hand bonus
            self._last_trade_was_win = True
            logger.info(
                f"🔥 HOT HAND: Last trade was WIN | "
                f"Session limit +1 if base consumed"
            )
    
    def get_daily_stats(self) -> dict:
        """Get daily trading statistics (observability)."""
        return {
            'trades_today': self._trade_count_today,
            'wins_today': self._wins_today,
            'losses_today': self._losses_today,
            'consecutive_losses': self._consecutive_losses,
            'wtp_exits_today': self._worst_trade_preventions_today,
            'pnl_today_r': self._pnl_today_r,
            'last_trade_was_win': self._last_trade_was_win,
        }
    
    def last_trade_was_win(self) -> bool:
        """
        🔥 FIX #10: Check if last trade was a win.
        
        Used by Hot Hand system to determine if bonus trade is allowed
        after base limit is consumed.
        
        Returns:
            True if last closed trade was a win, False otherwise.
        """
        return self._last_trade_was_win
    
    def reset_session_state(self, session_name: str) -> None:
        """
        🔥 FIX #10: Reset session state when session changes.
        
        Called when trading session changes (e.g., ASIA → LONDON)
        to reset the Hot Hand state.
        """
        if self._current_session != session_name:
            old_session = self._current_session
            self._current_session = session_name
            self._last_trade_was_win = False  # Reset on session change
            if old_session:
                logger.info(
                    f"📊 Session change: {old_session} → {session_name} | "
                    f"Hot Hand state reset"
                )
    
    def get_hours_since_last_trade(self, current_time: datetime) -> float:
        """
        Get hours since last trade.
        
        Args:
            current_time: Current market time
            
        Returns:
            Hours since last trade, or hours since day start if no trade yet
        """
        if self._last_trade_time is None:
            # No trade yet - return hours since market day start (00:00 UTC)
            day_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            return (current_time - day_start).total_seconds() / 3600
        
        return (current_time - self._last_trade_time).total_seconds() / 3600
    
    def get_activity_status(self, current_time: datetime) -> ActivityStatus:
        """
        Get current activity status.
        
        Args:
            current_time: Current market time
            
        Returns:
            ActivityStatus with level, hours, and message
        """
        hours_since = self.get_hours_since_last_trade(current_time)
        
        if hours_since < 2:
            level = ActivityLevel.NORMAL
            message = "Normal activity"
        elif hours_since < 4:
            level = ActivityLevel.MODERATE
            message = "Moderate inactivity"
        elif hours_since < 6:
            level = ActivityLevel.SIGNIFICANT
            message = "Significant inactivity - check market conditions"
        else:
            level = ActivityLevel.SEVERE
            message = "Severe inactivity - verify system is working"
        
        return ActivityStatus(
            last_trade_time=self._last_trade_time,
            hours_since_trade=hours_since,
            level=level,
            message=message
        )
    
    def get_last_trade_time(self) -> Optional[datetime]:
        """Get the last trade time (read-only)."""
        return self._last_trade_time
    
    def get_last_loss_time(self) -> Optional[datetime]:
        """Get the time of last losing trade (for cooldown calculations)."""
        return self._last_loss_time
    
    def get_trade_count_today(self) -> int:
        """Get number of trades today."""
        return self._trade_count_today
    
    def get_session_trade_count(self) -> int:
        """Get total trades this session."""
        return self._total_trades_session
    
    def reset_daily_count(self) -> None:
        """Reset daily trade count (call at day boundary)."""
        self._trade_count_today = 0
        self._last_trade_date = None
        logger.info("📊 Activity Tracker: Daily count reset")
    
    def force_set_last_trade_time(self, time: datetime, reason: str) -> None:
        """
        Force set last trade time (for initialization/adoption).
        
        Args:
            time: Time to set
            reason: Why this is being forced
        """
        logger.warning(f"📊 Activity Tracker: Force setting last_trade_time | Reason: {reason}")
        self._last_trade_time = time


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================
_activity_tracker: Optional[ActivityTracker] = None


def get_activity_tracker() -> ActivityTracker:
    """Get the singleton activity tracker instance."""
    global _activity_tracker
    if _activity_tracker is None:
        _activity_tracker = ActivityTracker()
    return _activity_tracker


def record_trade_executed(market_time: datetime) -> None:
    """Convenience function to record a trade execution."""
    get_activity_tracker().record_trade_executed(market_time)


def get_last_loss_time() -> Optional[datetime]:
    """
    Get time of last losing trade (for cooldown calculations).
    
    🔥 NEW (Feb 2, 2026): Used by LossStreakCooldown to enforce
    wait periods after consecutive losses.
    """
    return get_activity_tracker().get_last_loss_time()


def get_hours_since_last_trade(current_time: datetime) -> float:
    """Convenience function to get hours since last trade."""
    return get_activity_tracker().get_hours_since_last_trade(current_time)


def get_activity_level(current_time: datetime) -> ActivityLevel:
    """Convenience function to get current activity level."""
    return get_activity_tracker().get_activity_status(current_time).level
