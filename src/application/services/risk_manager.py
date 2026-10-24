"""Risk Manager: enforces trading limits and kill-switches."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Risk management limits."""

    max_daily_loss_usd: float = 100.0
    max_daily_trades: int = 10
    max_position_size_btc: float = 0.01
    max_drawdown_pct: float = 5.0
    min_balance_usd: float = 100.0
    cooldown_after_loss_seconds: int = 300  # 5 min cooldown after losing trade


class RiskManager:
    """Enforces risk limits and provides kill-switch functionality.

    Critical for production:
    - Prevents excessive losses
    - Enforces position sizing
    - Provides emergency shutdown capability
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self._limits = limits or RiskLimits()
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._last_trade_date: datetime | None = None
        self._last_loss_time: datetime | None = None
        self._killed = False
        self._kill_reason: str = ""
        self._peak_balance = 0.0
        self._current_balance = 0.0

    @property
    def is_killed(self) -> bool:
        """True if kill-switch is active."""
        return self._killed

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def kill(self, reason: str) -> None:
        """Activate kill-switch - stops all trading."""
        self._killed = True
        self._kill_reason = reason
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def reset_kill(self) -> None:
        """Reset kill-switch (manual only)."""
        self._killed = False
        self._kill_reason = ""
        logger.warning("Kill switch reset manually")

    def update_balance(self, balance: float) -> None:
        """Update current balance and check drawdown."""
        self._current_balance = balance

        if balance > self._peak_balance:
            self._peak_balance = balance

        if self._peak_balance > 0:
            drawdown_pct = ((self._peak_balance - balance) / self._peak_balance) * 100
            if drawdown_pct >= self._limits.max_drawdown_pct:
                self.kill(f"Max drawdown reached: {drawdown_pct:.2f}% >= {self._limits.max_drawdown_pct}%")

        if balance < self._limits.min_balance_usd:
            self.kill(f"Balance below minimum: ${balance:.2f} < ${self._limits.min_balance_usd}")

    def record_trade_result(self, pnl: float) -> None:
        """Record a trade result and update limits."""
        now = datetime.now(UTC)

        # Reset daily counters if new day
        if self._last_trade_date is None or self._last_trade_date.date() != now.date():
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._last_trade_date = now

        self._daily_pnl += pnl
        self._daily_trades += 1

        if pnl < 0:
            self._last_loss_time = now

        # Check daily loss limit
        if self._daily_pnl <= -self._limits.max_daily_loss_usd:
            self.kill(f"Daily loss limit reached: ${self._daily_pnl:.2f}")

        logger.info(
            "Trade recorded: PnL=$%.2f, Daily PnL=$%.2f, Daily trades=%d", pnl, self._daily_pnl, self._daily_trades
        )

    def restore_daily_state(self, daily_pnl: float, daily_trades: int) -> None:
        """Restore today's counters after a process restart."""
        self._daily_pnl = daily_pnl
        self._daily_trades = daily_trades
        self._last_trade_date = datetime.now(UTC)
        if self._daily_pnl <= -self._limits.max_daily_loss_usd:
            self.kill(f"Daily loss limit restored: ${self._daily_pnl:.2f}")

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed.

        Returns:
            Tuple of (can_trade, reason)
        """
        if self._killed:
            return False, f"Kill switch active: {self._kill_reason}"

        # Check daily trade limit
        if self._daily_trades >= self._limits.max_daily_trades:
            return False, f"Daily trade limit reached: {self._daily_trades} >= {self._limits.max_daily_trades}"

        # Check cooldown after loss
        if self._last_loss_time:
            elapsed = (datetime.now(UTC) - self._last_loss_time).total_seconds()
            if elapsed < self._limits.cooldown_after_loss_seconds:
                remaining = self._limits.cooldown_after_loss_seconds - elapsed
                return False, f"Cooldown after loss: {remaining:.0f}s remaining"

        return True, "OK"

    def validate_position_size(self, size_btc: float) -> tuple[bool, str]:
        """Validate a position size.

        Returns:
            Tuple of (valid, reason)
        """
        if size_btc <= 0:
            return False, "Position size must be positive"

        if size_btc > self._limits.max_position_size_btc:
            return False, f"Position size {size_btc} > max {self._limits.max_position_size_btc}"

        return True, "OK"

    def get_status(self) -> dict:
        """Get current risk status."""
        can_trade, reason = self.can_trade()
        return {
            "can_trade": can_trade,
            "reason": reason,
            "killed": self._killed,
            "kill_reason": self._kill_reason,
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "current_balance": self._current_balance,
            "peak_balance": self._peak_balance,
            "limits": {
                "max_daily_loss": self._limits.max_daily_loss_usd,
                "max_daily_trades": self._limits.max_daily_trades,
                "max_position_size": self._limits.max_position_size_btc,
                "max_drawdown_pct": self._limits.max_drawdown_pct,
            },
        }
