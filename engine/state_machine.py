"""
State Machine for BTC Perpetual Trading Bot
Manages bot states: IDLE, WAIT_FILL, MANAGE, OBSERVATION, OFF

NOTE: This is the OPERATIONAL state machine that tracks order/position lifecycle.
For STRATEGY-LEVEL state (ACTIVE/DEGRADED/DEAD), see strategy_state_machine.py.

These two state machines serve different purposes:
- BotState: Order/position lifecycle (IDLE → WAIT_FILL → MANAGE → IDLE)
- StrategyState: Trading permission and sizing (ACTIVE → DEGRADED → DEAD)
"""

from enum import Enum
from typing import Optional
from datetime import datetime, timedelta, UTC
import logging

# Use centralized time provider for consistent timestamps
from .time_provider import get_time_provider, TimeProvider
from .core_utils import ensure_utc

logger = logging.getLogger(__name__)


class BotState(Enum):
    """Trading bot states"""
    IDLE = "IDLE"                   # Waiting for setup
    WAIT_FILL = "WAIT_FILL"        # Order placed, waiting for fill
    MANAGE = "MANAGE"               # Position open, managing exit
    OBSERVATION = "OBSERVATION"     # Observing only, no trading
    OFF = "OFF"                     # Bot is off (kill conditions met)


class StateMachine:
    """
    Manages the trading bot state and transitions
    """

    def __init__(self, backtest_mode: bool = False):
        self.state = BotState.IDLE
        self.previous_state = None
        self.backtest_mode = backtest_mode
        
        # Use centralized TimeProvider for consistent timestamps
        time_mode = TimeProvider.MODE_BACKTEST if backtest_mode else TimeProvider.MODE_LIVE
        self._time_provider = get_time_provider(time_mode)
        
        # Legacy: kept for backwards compatibility but prefer _time_provider
        self._market_time: Optional[datetime] = None
        
        # Cooldown management
        self.cooldown_until: Optional[datetime] = None
        # In backtest, use minimal cooldown (1 candle = 1 min)
        self.cooldown_duration_minutes = 1 if backtest_mode else 15
        
        # Trade tracking
        self.current_trade_id: Optional[str] = None
        self.trade_open_time: Optional[datetime] = None
        self.entry_timer_start: Optional[datetime] = None
        self.entry_timeout_candles = 3  # Max 3 candles to fill
        
        # Risk tracking
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_drawdown = 0.0  # Track daily drawdown in absolute terms
        
        # Performance tracking
        self.profit_factor = 0.0
        self.expectancy = 0.0
        self.total_wins = 0
        self.total_losses = 0
        
        # OBSERVATION MODE - REDESIGNED (Jan 4, 2026)
        # Old: Block ALL trades for hours (hibernation)
        # New: Block N setups, then resume with reduced size (penalty, not paralysis)
        self.observation_entry_time: Optional[datetime] = None
        self.observation_setups_to_skip = 0       # NEW: Skip N setups, not time
        self.observation_size_multiplier = 1.0    # NEW: Size penalty after observation
        self.setups_skipped = 0                   # NEW: Counter
        
        # Legacy settings (kept for compatibility but reduced)
        self.observation_min_duration_minutes = 15  # Was 60 - much shorter
        self.observation_recovery_wr_threshold = 0.4  # Was 0.5 - easier recovery
        self.trades_in_observation = 0
        self.wins_in_observation = 0
    
    def set_market_time(self, market_time: datetime):
        """Update market time (called each tick from market_data)"""
        market_time = ensure_utc(market_time)
        self._market_time = market_time
        # Sync with centralized TimeProvider
        self._time_provider.set_market_time(market_time)
    
    def _get_time(self) -> datetime:
        """Get current time using centralized TimeProvider."""
        return self._time_provider.now()

    def transition_to(self, new_state: BotState, reason: str = ""):
        """Transition to a new state"""
        if new_state == self.state:
            return
            
        self.previous_state = self.state
        self.state = new_state
        
        log_msg = f"State transition: {self.previous_state.value} → {self.state.value}"
        if reason:
            log_msg += f" | Reason: {reason}"
        logger.info(log_msg)

    def can_trade(self) -> tuple[bool, str]:
        """
        Check if bot can enter new trades
        Returns: (can_trade: bool, reason: str)
        """
        # Check if in correct state
        if self.state != BotState.IDLE:
            return False, f"Not in IDLE state (current: {self.state.value})"
        
        # Check cooldown
        if self.is_in_cooldown():
            remaining = (self.cooldown_until - self._get_time()).total_seconds()
            return False, f"In cooldown for {remaining:.0f} more seconds"
        
        return True, "Ready to trade"

    def start_cooldown(self, duration_minutes: Optional[int] = None):
        """Start post-trade cooldown"""
        duration = duration_minutes or self.cooldown_duration_minutes
        self.cooldown_until = self._get_time() + timedelta(minutes=duration)
        logger.info(f"Cooldown started for {duration} minutes until {self.cooldown_until.strftime('%H:%M:%S')}")

    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period"""
        if self.cooldown_until is None:
            return False
        return self._get_time() < self.cooldown_until

    def start_entry_timer(self):
        """Start timer for order fill timeout"""
        self.entry_timer_start = self._get_time()
        logger.debug("Entry timer started")

    def is_entry_timeout(self, candle_duration_seconds: int = 60) -> bool:
        """
        Check if entry timeout exceeded
        candle_duration_seconds: duration of one candle in seconds (60 for 1m)
        """
        if self.entry_timer_start is None:
            return False
            
        elapsed = (self._get_time() - self.entry_timer_start).total_seconds()
        timeout_seconds = self.entry_timeout_candles * candle_duration_seconds
        
        return elapsed >= timeout_seconds

    def record_trade_open(self, trade_id: str):
        """Record when a trade is opened"""
        self.current_trade_id = trade_id
        self.trade_open_time = self._get_time()
        self.daily_trades += 1
        logger.info(f"Trade opened: {trade_id} at {self.trade_open_time.strftime('%H:%M:%S')}")

    def record_trade_close(self, pnl: float, is_win: bool):
        """Record when a trade is closed"""
        if self.current_trade_id:
            duration = (self._get_time() - self.trade_open_time).total_seconds() / 60
            logger.info(f"Trade closed: {self.current_trade_id} | Duration: {duration:.1f}min | PnL: ${pnl:.2f}")
        
        # Update statistics (drawdown now handled by risk_engine)
        if is_win:
            self.consecutive_losses = 0
            self.total_wins += 1
        else:
            self.consecutive_losses += 1
            self.total_losses += 1
        
        # Reset trade tracking
        self.current_trade_id = None
        self.trade_open_time = None
        self.entry_timer_start = None

    def update_performance_metrics(self, profit_factor: float, expectancy: float):
        """Update performance metrics"""
        self.profit_factor = profit_factor
        self.expectancy = expectancy

    def should_enter_observation_mode(self) -> bool:
        """Check if bot should enter observation mode"""
        # Poor performance metrics
        if self.profit_factor > 0 and self.profit_factor < 1.5:
            logger.warning(f"Profit factor {self.profit_factor:.2f} below threshold 1.5")
            return True
            
        if self.expectancy <= 0:
            logger.warning(f"Expectancy {self.expectancy:.2f} is not positive")
            return True
        
        # NUEVA LÓGICA: Demasiadas pérdidas consecutivas (FIX #8)
        # REDESIGNED (Jan 4, 2026): 3 losses = penalty, not hibernation
        if self.consecutive_losses >= 3:
            logger.warning(f"Consecutive losses {self.consecutive_losses} >= 3 - activating penalty mode")
            return True
            
        return False
    
    def enter_observation_mode(self, reason: str = "Performance degradation"):
        """
        REDESIGNED (Jan 4, 2026): Penalty mode, not hibernation.
        
        Old behavior: Block ALL trades for hours
        New behavior: Skip 2 setups, then trade at 50% size
        
        This keeps the bot active and learning, just with reduced exposure.
        """
        # LATCH: If already in observation, don't re-enter
        if self.state == BotState.OBSERVATION:
            return
        
        self.transition_to(BotState.OBSERVATION, reason)
        self.observation_entry_time = self._get_time()
        
        # NEW: Setup-based penalty instead of time-based
        self.observation_setups_to_skip = 2       # Skip next 2 valid setups
        self.observation_size_multiplier = 0.5    # Trade at 50% size after
        self.setups_skipped = 0
        
        self.trades_in_observation = 0
        self.wins_in_observation = 0
        
        logger.warning(f"🟡 PENALTY MODE ACTIVATED: {reason}")
        logger.warning(f"   - Will skip next {self.observation_setups_to_skip} setups")
        logger.warning(f"   - Then trade at {self.observation_size_multiplier*100:.0f}% size")
        logger.warning(f"   - NOT blocking for hours (penalty, not paralysis)")
    
    def should_skip_setup(self) -> tuple[bool, str]:
        """
        NEW (Jan 4, 2026): Check if current setup should be skipped.
        
        Called when a valid setup is detected.
        Returns (should_skip, reason)
        """
        if self.state != BotState.OBSERVATION:
            return False, ""
        
        if self.setups_skipped < self.observation_setups_to_skip:
            self.setups_skipped += 1
            remaining = self.observation_setups_to_skip - self.setups_skipped
            return True, f"Penalty: skipping setup {self.setups_skipped}/{self.observation_setups_to_skip} ({remaining} left)"
        
        # All setups skipped - can trade but at reduced size
        return False, ""
    
    def get_size_multiplier(self) -> float:
        """
        NEW (Jan 4, 2026): Get position size multiplier.
        
        Returns 1.0 normally, or reduced multiplier if in penalty mode.
        """
        if self.state == BotState.OBSERVATION:
            if self.setups_skipped >= self.observation_setups_to_skip:
                # Done skipping, trade at reduced size
                return self.observation_size_multiplier
            # Still skipping setups - shouldn't reach here
            return 0.0
        return 1.0
    
    def try_exit_observation_mode(self) -> bool:
        """
        REDESIGNED (Jan 4, 2026): Exit based on setup recovery, not time.
        
        Exit conditions:
        1. Skipped required setups AND made at least 1 trade
        2. That trade was a win (proves edge is back)
        OR
        3. 2 hour timeout (safety valve, much shorter than before)
        """
        if self.state != BotState.OBSERVATION:
            return True
        
        if self.observation_entry_time is None:
            return False
        
        elapsed_minutes = (self._get_time() - self.observation_entry_time).total_seconds() / 60
        
        # Safety timeout - 2 hours max (was 24h!)
        OBSERVATION_MAX_MINUTES = 120
        if elapsed_minutes >= OBSERVATION_MAX_MINUTES:
            logger.warning(f"🟢 PENALTY TIMEOUT ({OBSERVATION_MAX_MINUTES}min) - Resuming normal trading")
            self._exit_observation()
            return True
        
        # Check if we've completed the penalty + made a winning trade
        if self.setups_skipped >= self.observation_setups_to_skip:
            if self.trades_in_observation >= 1 and self.wins_in_observation >= 1:
                logger.info(f"✅ PENALTY COMPLETE - Won trade after skipping setups")
                self._exit_observation()
                return True
            elif self.trades_in_observation >= 2:
                # Even 2 trades (any outcome) is enough to exit
                logger.info(f"✅ PENALTY COMPLETE - Completed 2 trades in penalty mode")
                self._exit_observation()
                return True
        
        return False
    
    def _exit_observation(self):
        """Helper to cleanly exit observation mode"""
        self.transition_to(BotState.IDLE, "Exiting penalty mode")
        self.observation_entry_time = None
        self.observation_setups_to_skip = 0
        self.setups_skipped = 0
        self.observation_size_multiplier = 1.0
        self.trades_in_observation = 0
        self.wins_in_observation = 0
        self.consecutive_losses = 0
    
    def record_trade_in_observation(self, is_win: bool):
        """Registra trade mientras está en OBSERVATION mode"""
        if self.state == BotState.OBSERVATION:
            self.trades_in_observation += 1
            if is_win:
                self.wins_in_observation += 1

    def reset_daily_metrics(self):
        """Reset daily tracking metrics (call at start of new day)"""
        logger.info("Resetting daily metrics")
        self.consecutive_losses = 0
        self.daily_trades = 0
        # If in observation, can return to IDLE
        if self.state == BotState.OBSERVATION:
            self.transition_to(BotState.IDLE, "Daily reset from observation")

    def get_status(self, drawdown_pct: float = 0.0) -> dict:
        """
        Get current state machine status.
        
        Args:
            drawdown_pct: Current drawdown percentage (injected from caller).
                         This avoids circular imports with risk_engine.
        
        Returns:
            dict with state machine status
        """
        return {
            'state': self.state.value,
            'previous_state': self.previous_state.value if self.previous_state else None,
            'in_cooldown': self.is_in_cooldown(),
            'cooldown_until': self.cooldown_until.isoformat() if self.cooldown_until else None,
            'current_trade_id': self.current_trade_id,
            'drawdown_pct': round(drawdown_pct, 2),
            'consecutive_losses': self.consecutive_losses,
            'daily_trades': self.daily_trades,
            'profit_factor': round(self.profit_factor, 2),
            'expectancy': round(self.expectancy, 2),
            'total_wins': self.total_wins,
            'total_losses': self.total_losses
        }
