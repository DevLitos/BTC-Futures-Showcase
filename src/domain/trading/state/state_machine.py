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
from datetime import datetime, date, timedelta, UTC
import logging
import threading

# Use centralized time provider for consistent timestamps
from src.shared.time_provider import get_time_provider, TimeProvider
from src.shared.utils import ensure_utc

logger = logging.getLogger(__name__)


class BotState(Enum):
    """Trading bot states"""
    IDLE = "IDLE"                   # Waiting for setup
    WAIT_FILL = "WAIT_FILL"        # Order placed, waiting for fill (DEPRECATED - use HARD_LOCK)
    HARD_LOCK = "HARD_LOCK"        # Transition state - NO SUBSYSTEM CAN ACT
    MANAGE = "MANAGE"               # Position open, managing exit
    OBSERVATION = "OBSERVATION"     # Observing only, no trading
    OFF = "OFF"                     # Bot is off (kill conditions met)


class StateMachine:
    """
    Manages the trading bot state and transitions.
    
    Thread-safe: Uses a lock to protect state transitions from race conditions
    in concurrent async operations.
    """

    def __init__(self, backtest_mode: bool = False):
        self.state = BotState.IDLE
        self.previous_state = None
        self.backtest_mode = backtest_mode
        
        # Thread-safe lock for state transitions
        self._state_lock = threading.Lock()
        
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
        
        # =====================================================================
        # 🔥 DAILY DRAWDOWN PROTECTION - SURVIVAL, NOT EDGE
        # Track equity at start of day, update after each close
        # If DD >= threshold → HALT (no recover, no retry)
        # =====================================================================
        self.daily_equity_start: float = 0.0  # Set at start of day / first trade
        self.daily_pnl: float = 0.0  # Cumulative PnL today
        self.daily_drawdown_pct: float = 0.0  # Current DD as percentage
        self.daily_drawdown_limit_pct: float = 0.03  # 3% max daily DD (configurable)
        self.is_daily_halt: bool = False  # Once True, stays True until manual reset
        self.daily_halt_reason: str = ""  # Why we halted
        self._last_dd_check_date: Optional[date] = None  # Track date for reset
        
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
        """
        Transition to a new state (thread-safe).
        
        Uses a lock to prevent race conditions when multiple async
        operations try to change state simultaneously.
        """
        with self._state_lock:
            if new_state == self.state:
                return
                
            self.previous_state = self.state
            self.state = new_state
            
            log_msg = f"State transition: {self.previous_state.value} → {self.state.value}"
            if reason:
                log_msg += f" | Reason: {reason}"
            logger.info(log_msg)

    def to_idle(self, reason: str = "") -> None:
        """Convenience method to transition to IDLE state."""
        self.transition_to(BotState.IDLE, reason)

    def can_trade(self) -> tuple[bool, str]:
        """
        Check if bot can enter new trades
        Returns: (can_trade: bool, reason: str)
        """
        # Check if in correct state
        if self.state != BotState.IDLE:
            return False, f"Not in IDLE state (current: {self.state.value})"
        
        # Check cooldown
        if self.cooldown_until is not None and self.is_in_cooldown():
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

    def record_trade_close(self, pnl: float, is_win: bool, exit_reason: str = "UNKNOWN"):
        """
        Record when a trade is closed.
        
        CRITICAL FIX (Jan 12, 2026):
        Consecutive losses are ONLY counted for REAL LOSSES:
        - SL hit (stop-loss triggered)
        - Market adverse exit (manual close at loss)
        - R < 0 with confirmed position
        
        Administrative events DO NOT count as losses:
        - FUNDING_EXIT (funding rate issue)
        - TIME_EXIT (stagnation timeout)
        - CANCELLED (order never filled)
        - RECONCILE_CLOSE (exchange sync)
        
        This prevents the bot from penalizing itself for non-trade events.
        """        
        if self.current_trade_id and self.trade_open_time is not None:
            duration = (self._get_time() - self.trade_open_time).total_seconds() / 60
            logger.info(f"Trade closed: {self.current_trade_id} | Reason: {exit_reason} | Duration: {duration:.1f}min | PnL: ${pnl:.2f}")
        
        # Determine if this is a REAL trade loss (not administrative)
        # 🔥 FIX (Jan 20, 2026): Added EXCHANGE_STOP_LOSS and EXCHANGE_TAKE_PROFIT
        # These are exchange-executed orders that should count as real trades
        # 🔥 FIX #15 (Feb 02, 2026): TIME_INVALIDATION debe contar como LOSS
        #    TIME_INVALIDATION_MAX_LOSS es un trade que perdió dinero → LOSS
        REAL_LOSS_REASONS = {
            'SL_HIT', 'STOP_LOSS', 'MARKET_CLOSE', 'MANUAL_CLOSE',
            'EXCHANGE_STOP_LOSS',  # SL executed by exchange
            'TRAILING_STOP',       # Trailing stop hit
            'TIME_INVALIDATION',   # 🔥 FIX #15: Trade stagnated at loss
            'TIME_INVALIDATION_MAX_LOSS',  # 🔥 FIX #15: Trade timed out at loss
        }
        REAL_WIN_REASONS = {
            'EXCHANGE_TAKE_PROFIT', 'TP_HIT', 'TAKE_PROFIT', 'TP1', 'TP2'
        }
        is_real_trade = (
            exit_reason.upper() in REAL_LOSS_REASONS or 
            exit_reason.upper() in REAL_WIN_REASONS or
            exit_reason.upper().startswith('SL') or
            exit_reason.upper().startswith('TP') or
            exit_reason.upper().startswith('EXCHANGE_')
        )
        
        # Administrative exits that should NOT count as losses
        ADMIN_REASONS = {'FUNDING_EXIT', 'TIME_EXIT', 'TIME_BASED_EXIT', 'CANCELLED', 
                         'RECONCILE_CLOSE', 'TIMEOUT', 'POST_ONLY_REJECT', 'SYNC_ERROR'}
        is_admin_event = exit_reason.upper() in ADMIN_REASONS
        
        # Update statistics
        if is_win:
            self.consecutive_losses = 0
            self.total_wins += 1
            logger.debug(f"WIN recorded - consecutive losses reset to 0")
        elif is_real_trade and not is_admin_event:
            # 🔥 FIX (Jan 22, 2026): EXCHANGE_STOP_LOSS MUST count as loss
            # The old logic required `pnl < 0`, but when exchange executes SL
            # we often don't have accurate PnL at detection time.
            # If exit_reason is SL/STOP type, it's a loss regardless of reported pnl.
            is_sl_type = (
                exit_reason.upper() in REAL_LOSS_REASONS or
                'SL' in exit_reason.upper() or
                'STOP' in exit_reason.upper()
            )
            
            if is_sl_type:
                # SL hit = ALWAYS a loss (even if pnl is 0 or slightly positive due to timing)
                self.consecutive_losses += 1
                self.total_losses += 1
                logger.info(f"📉 REAL LOSS recorded ({exit_reason}) - SL type exit counts as loss - consecutive losses: {self.consecutive_losses}")
            elif pnl < 0:
                # Other real trade with negative pnl
                self.consecutive_losses += 1
                self.total_losses += 1
                logger.info(f"📉 REAL LOSS recorded ({exit_reason}) - negative PnL - consecutive losses: {self.consecutive_losses}")
            else:
                # Real trade but positive/zero pnl and not SL type
                logger.info(f"📊 Trade closed ({exit_reason}) with PnL ${pnl:.2f} - not counting as loss")
        else:
            # Administrative event - don't count as loss
            logger.info(f"⚪ Administrative close ({exit_reason}) - NOT counted as loss (consecutive: {self.consecutive_losses})")
            # Don't increment consecutive_losses
            # Don't reset either - maintain current streak
        
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
        Get position size multiplier based on operational state.
        
        ANTI-REVENGE TRADING (Jan 9, 2026):
        - 2 consecutive losses → reduce to 0.5x
        - 3+ consecutive losses → reduce to 0.25x
        
        OBSERVATION mode still applies its own reduction.
        
        Returns: multiplier (0.0 to 1.0)
        """
        multiplier = 1.0
        
        # ANTI-REVENGE: Reduce sizing after consecutive losses
        if self.consecutive_losses >= 3:
            multiplier = 0.25
            logger.info(f"📉 ANTI-REVENGE: {self.consecutive_losses} consecutive losses → 0.25x sizing")
        elif self.consecutive_losses >= 2:
            multiplier = 0.5
            logger.info(f"📉 ANTI-REVENGE: {self.consecutive_losses} consecutive losses → 0.5x sizing")
        
        # OBSERVATION mode overrides if more restrictive
        if self.state == BotState.OBSERVATION:
            if self.setups_skipped >= self.observation_setups_to_skip:
                # Done skipping, trade at reduced size
                obs_mult = self.observation_size_multiplier
                multiplier = min(multiplier, obs_mult)
            else:
                # Still skipping setups - shouldn't reach here
                return 0.0
        
        return multiplier
    
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

    # =========================================================================
    # 🔥 DAILY DRAWDOWN PROTECTION METHODS
    # =========================================================================
    
    def initialize_daily_equity(self, equity: float):
        """
        Set starting equity for the day. Call this at bot startup and after midnight reset.
        """
        current_date = self._get_time().date()
        
        # Check if we need to reset for new day
        if self._last_dd_check_date is not None and self._last_dd_check_date != current_date:
            logger.info(f"🌅 New trading day detected - resetting daily drawdown")
            self.daily_pnl = 0.0
            self.daily_drawdown_pct = 0.0
            self.is_daily_halt = False
            self.daily_halt_reason = ""
        
        self.daily_equity_start = equity
        self._last_dd_check_date = current_date
        logger.info(f"📊 Daily equity initialized: ${equity:.2f} | DD limit: {self.daily_drawdown_limit_pct*100:.1f}%")
    
    def update_daily_pnl(self, trade_pnl: float) -> bool:
        """
        Update daily PnL after each trade close.
        Returns True if we should HALT (DD exceeded).
        """
        if self.is_daily_halt:
            return True  # Already halted
        
        self.daily_pnl += trade_pnl
        
        # Calculate current drawdown percentage
        if self.daily_equity_start > 0:
            self.daily_drawdown_pct = min(0, self.daily_pnl) / self.daily_equity_start
        else:
            self.daily_drawdown_pct = 0.0
        
        # Check if we hit the limit
        if abs(self.daily_drawdown_pct) >= self.daily_drawdown_limit_pct:
            self.is_daily_halt = True
            self.daily_halt_reason = f"Daily DD {abs(self.daily_drawdown_pct)*100:.1f}% >= limit {self.daily_drawdown_limit_pct*100:.1f}%"
            logger.critical(f"🛑 DAILY HALT: {self.daily_halt_reason}")
            logger.critical(f"🛑 Daily PnL: ${self.daily_pnl:.2f} | Starting equity: ${self.daily_equity_start:.2f}")
            return True
        
        logger.info(f"📉 Daily PnL: ${self.daily_pnl:.2f} | DD: {abs(self.daily_drawdown_pct)*100:.2f}%")
        return False
    
    def check_daily_halt(self) -> tuple[bool, str]:
        """
        Check if daily halt is active.
        Returns: (is_halted, reason)
        """
        if self.is_daily_halt:
            return True, self.daily_halt_reason
        return False, ""
    
    def force_daily_reset(self, new_equity: float):
        """Manual reset - only call this at start of new day or manual intervention."""
        self.daily_pnl = 0.0
        self.daily_drawdown_pct = 0.0
        self.is_daily_halt = False
        self.daily_halt_reason = ""
        self.daily_equity_start = new_equity
        self._last_dd_check_date = self._get_time().date()
        logger.info(f"🔄 Daily drawdown manually reset. New equity: ${new_equity:.2f}")

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
            'total_losses': self.total_losses,
            # 🔥 Daily drawdown info
            'daily_pnl': round(self.daily_pnl, 2),
            'daily_drawdown_pct': round(abs(self.daily_drawdown_pct) * 100, 2),
            'is_daily_halt': self.is_daily_halt,
            'daily_halt_reason': self.daily_halt_reason
        }
