"""Trade Journal: persists all trading activity for auditability."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.models.position import Position
from src.domain.models.signal import Signal

logger = logging.getLogger(__name__)


class TradeJournal:
    """Persists trading activity to disk for recovery and audit.
    
    Critical for production:
    - Recover open positions after restart
    - Audit trail for all decisions
    - Debug signal/execution issues
    """

    def __init__(self, storage_dir: str = "storage/journal") -> None:
        self._storage_path = Path(storage_dir)
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._positions_file = self._storage_path / "positions.json"
        self._signals_file = self._storage_path / "signals.jsonl"
        self._trades_file = self._storage_path / "trades.jsonl"
        self._state_file = self._storage_path / "state.json"

    def save_state(self, state: dict[str, Any]) -> None:
        """Save runtime state for recovery."""
        state["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(self._state_file, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def load_state(self) -> dict[str, Any] | None:
        """Load saved state if exists."""
        if not self._state_file.exists():
            return None
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to load state: %s", e)
            return None

    def save_open_positions(self, positions: list[Position]) -> None:
        """Persist open positions for recovery."""
        data = []
        for pos in positions:
            data.append({
                "position_id": pos.position_id,
                "symbol": pos.symbol,
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "size": pos.size,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "status": pos.status.value,
                "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                "strategy_name": pos.strategy_name,
                "signal_type": pos.signal_type,
                "metadata": pos.metadata,
                "trailing_stop_activated": pos.trailing_stop_activated,
            })
        
        with open(self._positions_file, "w") as f:
            json.dump({"positions": data, "updated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        
        logger.debug("Saved %d open positions", len(positions))

    def load_open_positions(self) -> list[dict[str, Any]]:
        """Load persisted positions for recovery."""
        if not self._positions_file.exists():
            return []
        try:
            with open(self._positions_file) as f:
                data = json.load(f)
                return data.get("positions", [])
        except Exception as e:
            logger.error("Failed to load positions: %s", e)
            return []

    def log_signal(self, signal: Signal, action: str, reason: str = "") -> None:
        """Log a signal event."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "signal",
            "action": action,
            "reason": reason,
            "direction": signal.direction.value,
            "signal_type": signal.signal_type.value,
            "strategy": signal.strategy_name,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "risk_reward": signal.risk_reward_ratio,
            "strength": signal.strength.value,
        }
        self._append_jsonl(self._signals_file, entry)

    def log_trade(self, position: Position, event: str, details: dict[str, Any] | None = None) -> None:
        """Log a trade event (open, close, update)."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side.value,
            "entry_price": position.entry_price,
            "size": position.size,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "exit_price": position.exit_price,
            "exit_reason": position.exit_reason.value if position.exit_reason else None,
            "realized_pnl": position.realized_pnl,
            "realized_r": position.realized_r,
            "strategy": position.strategy_name,
            "details": details or {},
        }
        self._append_jsonl(self._trades_file, entry)

    def _append_jsonl(self, path: Path, entry: dict[str, Any]) -> None:
        """Append entry to JSONL file."""
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write to %s: %s", path, e)

    def get_daily_pnl(self) -> float:
        """Calculate today's PnL from trade log."""
        if not self._trades_file.exists():
            return 0.0
        
        today = datetime.now(timezone.utc).date()
        daily_pnl = 0.0
        
        try:
            with open(self._trades_file) as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("event") == "closed":
                        ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                        if ts.date() == today:
                            daily_pnl += entry.get("realized_pnl") or 0
        except Exception as e:
            logger.error("Failed to calculate daily PnL: %s", e)
        
        return daily_pnl

    def get_daily_trades(self) -> int:
        """Count today's trades."""
        if not self._trades_file.exists():
            return 0
        
        today = datetime.now(timezone.utc).date()
        count = 0
        
        try:
            with open(self._trades_file) as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("event") == "opened":
                        ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                        if ts.date() == today:
                            count += 1
        except Exception:
            pass
        
        return count
