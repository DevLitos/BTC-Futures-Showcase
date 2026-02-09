"""
Cooldown Manager - Unified cooldown handling (Feb 04, 2026 - AUDIT FIX)

Consolidates ALL cooldown types into a single source of truth:
1. Loss streak cooldown (_cooldown_until)
2. Rejection cooldown (_rejection_cooldown_until)
3. Short obstinacy cooldown (_short_cooldown_until)
4. Edge context cooldown (from edge_state_manager)
5. Countertrend cooldown (from BiasStrengthConfig)

RATIONALE:
Before this refactor, cooldowns were scattered across:
- trading_app.py: 3 different _*_cooldown_until variables
- simplified_sizing.py: LossStreakCooldown class
- edge_state_manager.py: context_cooldown_until
- frequency_controller.py: _loss_cooldown_until

This caused:
- Inconsistent behavior (different code paths for same purpose)
- Multiple cooldowns applied simultaneously (double penalties)
- Hard to debug which cooldown is active

NOW: One class to rule them all.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, Tuple, List
import logging

logger = logging.getLogger(__name__)


class CooldownType(Enum):
    """Types of cooldown with their priorities (higher = more important)."""
    KILL_SWITCH = 100      # Full trading halt for the day
    LOSS_STREAK = 80       # After consecutive losses
    DEAD_HOURS = 70        # During toxic hours
    REJECTION = 50         # After filter rejection
    SHORT_OBSTINACY = 40   # After short bias obstinacy
    EDGE_CONTEXT = 30      # Edge-specific context cooldown
    COUNTERTREND = 20      # After countertrend loss
    POST_TRADE = 15        # FEB06: After any trade (adaptive by PnL)


@dataclass
class CooldownState:
    """State of a single cooldown."""
    type: CooldownType
    active: bool = False
    expires_at: Optional[datetime] = None
    reason: str = ""
    triggered_by: str = ""  # What caused this cooldown
    severity: int = 0       # 0-10 severity for logging
    
    def is_expired(self, current_time: datetime) -> bool:
        """Check if cooldown has expired."""
        if not self.active or self.expires_at is None:
            return True
        return current_time >= self.expires_at
    
    def remaining_seconds(self, current_time: datetime) -> int:
        """Get remaining seconds until expiry."""
        if not self.active or self.expires_at is None:
            return 0
        delta = (self.expires_at - current_time).total_seconds()
        return max(0, int(delta))


@dataclass
class CooldownCheckResult:
    """Result of checking if trading is allowed."""
    can_trade: bool = True
    blocking_cooldown: Optional[CooldownType] = None
    reason: str = ""
    remaining_seconds: int = 0
    all_active: List[CooldownType] = field(default_factory=list)


class CooldownManager:
    """
    Unified cooldown manager for all cooldown types.
    
    Usage:
        mgr = CooldownManager()
        
        # Check if we can trade
        result = mgr.check_can_trade(current_time)
        if not result.can_trade:
            logger.info(f"Blocked by {result.blocking_cooldown}: {result.reason}")
            return
        
        # Activate a cooldown
        mgr.activate_loss_streak_cooldown(consecutive_losses=3, current_time=now)
        
        # Get status for logging
        status = mgr.get_status_summary(current_time)
    """
    
    # Configuration defaults (can be overridden via set_config)
    DEFAULT_LOSS_STREAK_3_MIN = 15
    DEFAULT_LOSS_STREAK_4_MIN = 60
    DEFAULT_LOSS_STREAK_5_MIN = 180
    DEFAULT_REJECTION_CANDLES = 2
    DEFAULT_REJECTION_SECONDS = 120  # 2 candles × 60s
    DEFAULT_SHORT_OBSTINACY_MIN = 10
    DEFAULT_COUNTERTREND_CANDLES = 2
    
    # FEB06: Post-trade cooldown adaptativo por PnL
    # Lógica: Trade ganador = 30s, perdedor = 6min
    # Idea: "No bloquear → hacer caro equivocarte"
    DEFAULT_POST_TRADE_WIN_SECONDS = 30      # Ganador: cooldown mínimo
    DEFAULT_POST_TRADE_LOSS_SECONDS = 360    # Perdedor: 6 minutos
    DEFAULT_POST_TRADE_BE_SECONDS = 60       # Breakeven: 1 minuto
    
    def __init__(self):
        """Initialize with empty cooldown states."""
        self._cooldowns: Dict[CooldownType, CooldownState] = {
            ct: CooldownState(type=ct) for ct in CooldownType
        }
        self._config: Dict[str, Any] = {}
        self._tick_count = 0
        self._consecutive_losses = 0
        self._kill_switch_active = False
        
    # =========================================================================
    # CONFIGURATION
    # =========================================================================
    
    def set_config(self, **kwargs):
        """Set configuration values."""
        self._config.update(kwargs)
        
    def _get_config(self, key: str, default: Any) -> Any:
        """Get config value with default."""
        return self._config.get(key, default)
    
    # =========================================================================
    # CHECK IF TRADING ALLOWED
    # =========================================================================
    
    def check_can_trade(self, current_time: datetime) -> CooldownCheckResult:
        """
        Check if trading is allowed (not blocked by any cooldown).
        
        Returns result with blocking cooldown if any is active.
        """
        result = CooldownCheckResult()
        
        # Collect all active cooldowns (not expired)
        active_cooldowns = []
        for ct, state in self._cooldowns.items():
            if state.active and not state.is_expired(current_time):
                active_cooldowns.append((ct, state))
        
        result.all_active = [ct for ct, _ in active_cooldowns]
        
        if not active_cooldowns:
            return result  # No active cooldowns, can trade
        
        # Sort by priority (highest first)
        active_cooldowns.sort(key=lambda x: x[0].value, reverse=True)
        
        # Return highest priority cooldown as the blocker
        blocking_type, blocking_state = active_cooldowns[0]
        
        result.can_trade = False
        result.blocking_cooldown = blocking_type
        result.reason = blocking_state.reason
        result.remaining_seconds = blocking_state.remaining_seconds(current_time)
        
        return result
    
    def is_in_cooldown(self, current_time: datetime) -> bool:
        """Simple check if any cooldown is active."""
        return not self.check_can_trade(current_time).can_trade
    
    # =========================================================================
    # LOSS STREAK COOLDOWN
    # =========================================================================
    
    def activate_loss_streak_cooldown(
        self,
        consecutive_losses: int,
        current_time: datetime,
        last_trade_time: Optional[datetime] = None
    ):
        """
        Activate loss streak cooldown based on number of consecutive losses.
        
        Tiered cooldown:
        - 3 losses → 15 min
        - 4 losses → 60 min
        - 5 losses → 180 min
        - 6+ losses → KILL SWITCH
        """
        self._consecutive_losses = consecutive_losses
        
        if consecutive_losses >= 6:
            # KILL SWITCH
            self.activate_kill_switch("6+ consecutive losses")
            return
        
        cooldown_minutes = 0
        if consecutive_losses >= 5:
            cooldown_minutes = self._get_config(
                'loss_streak_5_min', self.DEFAULT_LOSS_STREAK_5_MIN
            )
        elif consecutive_losses >= 4:
            cooldown_minutes = self._get_config(
                'loss_streak_4_min', self.DEFAULT_LOSS_STREAK_4_MIN
            )
        elif consecutive_losses >= 3:
            cooldown_minutes = self._get_config(
                'loss_streak_3_min', self.DEFAULT_LOSS_STREAK_3_MIN
            )
        
        if cooldown_minutes > 0:
            expires = current_time + timedelta(minutes=cooldown_minutes)
            state = self._cooldowns[CooldownType.LOSS_STREAK]
            state.active = True
            state.expires_at = expires
            state.reason = f"{consecutive_losses} consecutive losses - wait {cooldown_minutes}min"
            state.triggered_by = f"loss_streak_{consecutive_losses}"
            state.severity = min(10, consecutive_losses * 2)
            
            logger.warning(
                f"⏳ LOSS STREAK COOLDOWN ACTIVATED: {consecutive_losses} losses | "
                f"Wait {cooldown_minutes}min | Expires: {expires.strftime('%H:%M:%S')}"
            )
    
    def clear_loss_streak(self):
        """Clear loss streak cooldown (e.g., after a win)."""
        self._consecutive_losses = 0
        self._cooldowns[CooldownType.LOSS_STREAK].active = False
        self._cooldowns[CooldownType.LOSS_STREAK].expires_at = None
    
    # =========================================================================
    # KILL SWITCH
    # =========================================================================
    
    def activate_kill_switch(self, reason: str):
        """Activate kill switch - no more trading today."""
        self._kill_switch_active = True
        state = self._cooldowns[CooldownType.KILL_SWITCH]
        state.active = True
        # Expires at midnight (or never in practice for the day)
        state.expires_at = datetime.now().replace(hour=23, minute=59, second=59)
        state.reason = f"KILL SWITCH: {reason}"
        state.triggered_by = "kill_switch"
        state.severity = 10
        
        logger.critical(
            f"🛑 KILL SWITCH ACTIVATED: {reason} | "
            f"NO MORE TRADING TODAY"
        )
    
    def is_kill_switch_active(self) -> bool:
        """Check if kill switch is active."""
        return self._kill_switch_active
    
    def reset_daily(self):
        """Reset all cooldowns (called at day start)."""
        self._kill_switch_active = False
        self._consecutive_losses = 0
        for state in self._cooldowns.values():
            state.active = False
            state.expires_at = None
        logger.info("🔄 COOLDOWNS RESET: New trading day")
    
    # =========================================================================
    # REJECTION COOLDOWN
    # =========================================================================
    
    def activate_rejection_cooldown(
        self,
        current_time: datetime,
        reason: str = "Filter rejection",
        cooldown_seconds: Optional[int] = None
    ):
        """
        Activate rejection cooldown after a setup is rejected.
        
        Default: 2 minutes (2 candles × 60s)
        """
        if cooldown_seconds is None:
            cooldown_seconds = self._get_config(
                'rejection_seconds', self.DEFAULT_REJECTION_SECONDS
            )
        
        # Ensure cooldown_seconds is int (type safety fix)
        cooldown_seconds = int(cooldown_seconds) if cooldown_seconds else self.DEFAULT_REJECTION_SECONDS
        
        expires = current_time + timedelta(seconds=cooldown_seconds)
        state = self._cooldowns[CooldownType.REJECTION]
        state.active = True
        state.expires_at = expires
        state.reason = f"Rejection cooldown: {reason}"
        state.triggered_by = reason
        state.severity = 3
        
        if self._tick_count % 30 == 0:  # Rate-limited logging
            logger.info(
                f"⏸️ REJECTION COOLDOWN: {reason} | "
                f"Wait {cooldown_seconds}s"
            )
    
    # =========================================================================
    # SHORT OBSTINACY COOLDOWN
    # =========================================================================
    
    def activate_short_obstinacy_cooldown(
        self,
        current_time: datetime,
        cooldown_minutes: int = 10
    ):
        """Activate cooldown after short obstinacy detection."""
        cooldown_minutes = self._get_config(
            'short_obstinacy_min', cooldown_minutes
        )
        
        expires = current_time + timedelta(minutes=cooldown_minutes)
        state = self._cooldowns[CooldownType.SHORT_OBSTINACY]
        state.active = True
        state.expires_at = expires
        state.reason = f"Short obstinacy - wait {cooldown_minutes}min"
        state.triggered_by = "short_obstinacy"
        state.severity = 4
        
        logger.warning(
            f"⏳ SHORT OBSTINACY COOLDOWN: Wait {cooldown_minutes}min | "
            f"Expires: {expires.strftime('%H:%M:%S')}"
        )
    
    def is_short_obstinacy_active(self, current_time: datetime) -> bool:
        """Check if short obstinacy cooldown is active."""
        state = self._cooldowns[CooldownType.SHORT_OBSTINACY]
        return state.active and not state.is_expired(current_time)
    
    # =========================================================================
    # DEAD HOURS COOLDOWN
    # =========================================================================
    
    def activate_dead_hours_cooldown(
        self,
        current_time: datetime,
        end_hour_utc: int = 7
    ):
        """Activate cooldown during dead hours (e.g., 4am-7am UTC)."""
        # Calculate when dead hours end
        end_time = current_time.replace(
            hour=end_hour_utc, minute=0, second=0, microsecond=0
        )
        if end_time <= current_time:
            end_time += timedelta(days=1)
        
        state = self._cooldowns[CooldownType.DEAD_HOURS]
        state.active = True
        state.expires_at = end_time
        state.reason = f"Dead hours trading - blocked until {end_hour_utc}:00 UTC"
        state.triggered_by = "dead_hours"
        state.severity = 5
        
        logger.warning(
            f"🌙 DEAD HOURS COOLDOWN: No trading until {end_hour_utc}:00 UTC"
        )
    
    # =========================================================================
    # EDGE CONTEXT COOLDOWN
    # =========================================================================
    
    def activate_edge_context_cooldown(
        self,
        current_time: datetime,
        cooldown_seconds: int = 300,
        edge_type: str = ""
    ):
        """Activate edge context cooldown."""
        expires = current_time + timedelta(seconds=cooldown_seconds)
        state = self._cooldowns[CooldownType.EDGE_CONTEXT]
        state.active = True
        state.expires_at = expires
        state.reason = f"Edge context cooldown ({edge_type})"
        state.triggered_by = f"edge_{edge_type}"
        state.severity = 3
    
    # =========================================================================
    # COUNTERTREND COOLDOWN
    # =========================================================================
    
    def activate_countertrend_cooldown(
        self,
        current_time: datetime,
        candles: int = 2
    ):
        """Activate cooldown after countertrend loss."""
        candles = self._get_config('countertrend_candles', candles)
        cooldown_seconds = candles * 60  # Assume 1m candles
        
        expires = current_time + timedelta(seconds=cooldown_seconds)
        state = self._cooldowns[CooldownType.COUNTERTREND]
        state.active = True
        state.expires_at = expires
        state.reason = f"Countertrend loss - wait {candles} candles"
        state.triggered_by = "countertrend_loss"
        state.severity = 3
    
    # =========================================================================
    # POST-TRADE COOLDOWN (FEB06) - Adaptive by PnL
    # =========================================================================
    
    def activate_post_trade_cooldown(
        self,
        current_time: datetime,
        pnl_r: float,
        exit_reason: str = ""
    ):
        """
        Activate adaptive cooldown after a trade closes.
        
        FEB06 DESIGN:
        - Trade ganador (pnl_r > 0.1): 30 segundos (puede seguir operando)
        - Trade breakeven (-0.15 < pnl_r < 0.1): 60 segundos
        - Trade perdedor (pnl_r < -0.15): 6 minutos (penalización)
        
        RATIONALE: "No bloquear → hacer caro equivocarte"
        El sistema no impide trades, pero sí impone un "tax" temporal
        a los trades perdedores para evitar tilt.
        """
        # Determinar cooldown según resultado
        if pnl_r >= 0.1:
            # Ganador: cooldown mínimo
            cooldown_seconds = self._get_config(
                'post_trade_win_seconds', self.DEFAULT_POST_TRADE_WIN_SECONDS
            )
            outcome = "WIN"
        elif pnl_r > -0.15:
            # Breakeven: cooldown intermedio
            cooldown_seconds = self._get_config(
                'post_trade_be_seconds', self.DEFAULT_POST_TRADE_BE_SECONDS
            )
            outcome = "BREAKEVEN"
        else:
            # Perdedor: cooldown largo
            cooldown_seconds = self._get_config(
                'post_trade_loss_seconds', self.DEFAULT_POST_TRADE_LOSS_SECONDS
            )
            outcome = "LOSS"
        
        expires = current_time + timedelta(seconds=cooldown_seconds)
        state = self._cooldowns[CooldownType.POST_TRADE]
        state.active = True
        state.expires_at = expires
        state.reason = f"Post-trade cooldown ({outcome}: {pnl_r:.2f}R) - wait {cooldown_seconds}s"
        state.triggered_by = f"post_trade_{outcome.lower()}"
        state.severity = 2 if outcome == "WIN" else (3 if outcome == "BREAKEVEN" else 5)
        
        logger.info(
            f"⏱️ POST-TRADE COOLDOWN ({outcome}): {pnl_r:.2f}R → "
            f"Wait {cooldown_seconds}s | Expires: {expires.strftime('%H:%M:%S')} | "
            f"Reason: {exit_reason}"
        )
    
    # =========================================================================
    # SYNC METHODS (Bridge with trading_app.py legacy variables)
    # =========================================================================
    
    def sync_from_app(
        self,
        cooldown_until: Optional[datetime],
        rejection_cooldown_until: Optional[datetime],
        short_cooldown_until: Optional[datetime],
        consecutive_losses: int = 0,
        kill_switch_active: bool = False,
        tick_count: int = 0
    ):
        """
        Sync state from trading_app's legacy variables.
        
        Call this to bridge between old and new cooldown systems.
        """
        self._tick_count = tick_count
        self._consecutive_losses = consecutive_losses
        self._kill_switch_active = kill_switch_active
        
        if kill_switch_active:
            self._cooldowns[CooldownType.KILL_SWITCH].active = True
            self._cooldowns[CooldownType.KILL_SWITCH].expires_at = (
                datetime.now().replace(hour=23, minute=59, second=59)
            )
        
        if cooldown_until:
            self._cooldowns[CooldownType.LOSS_STREAK].active = True
            self._cooldowns[CooldownType.LOSS_STREAK].expires_at = cooldown_until
        else:
            self._cooldowns[CooldownType.LOSS_STREAK].active = False
        
        if rejection_cooldown_until:
            self._cooldowns[CooldownType.REJECTION].active = True
            self._cooldowns[CooldownType.REJECTION].expires_at = rejection_cooldown_until
        else:
            self._cooldowns[CooldownType.REJECTION].active = False
        
        if short_cooldown_until:
            self._cooldowns[CooldownType.SHORT_OBSTINACY].active = True
            self._cooldowns[CooldownType.SHORT_OBSTINACY].expires_at = short_cooldown_until
        else:
            self._cooldowns[CooldownType.SHORT_OBSTINACY].active = False
    
    def sync_to_app(self) -> Dict[str, Any]:
        """
        Get values to sync back to trading_app's legacy variables.
        
        Returns dict with values for _cooldown_until, etc.
        """
        return {
            'cooldown_until': self._cooldowns[CooldownType.LOSS_STREAK].expires_at if self._cooldowns[CooldownType.LOSS_STREAK].active else None,
            'rejection_cooldown_until': self._cooldowns[CooldownType.REJECTION].expires_at if self._cooldowns[CooldownType.REJECTION].active else None,
            'short_cooldown_until': self._cooldowns[CooldownType.SHORT_OBSTINACY].expires_at if self._cooldowns[CooldownType.SHORT_OBSTINACY].active else None,
            'kill_switch_active': self._kill_switch_active,
            'consecutive_losses': self._consecutive_losses,
        }
    
    # =========================================================================
    # STATUS & LOGGING
    # =========================================================================
    
    def get_status_summary(self, current_time: datetime) -> str:
        """Get human-readable status summary."""
        active = []
        for ct, state in self._cooldowns.items():
            if state.active and not state.is_expired(current_time):
                remaining = state.remaining_seconds(current_time)
                active.append(f"{ct.name}({remaining}s)")
        
        if not active:
            return "No active cooldowns"
        return f"Active cooldowns: {', '.join(active)}"
    
    def to_trade_tags(self, current_time: datetime) -> Dict[str, Any]:
        """Get cooldown info as trade tags."""
        result = self.check_can_trade(current_time)
        tags = {
            'cooldown_active': not result.can_trade,
            'cooldown_type': result.blocking_cooldown.name if result.blocking_cooldown else None,
            'cooldown_remaining_s': result.remaining_seconds,
            'consecutive_losses': self._consecutive_losses,
            'kill_switch': self._kill_switch_active,
        }
        return tags


# Singleton instance
_cooldown_manager: Optional[CooldownManager] = None

def get_cooldown_manager() -> CooldownManager:
    """Get singleton instance of CooldownManager."""
    global _cooldown_manager
    if _cooldown_manager is None:
        _cooldown_manager = CooldownManager()
    return _cooldown_manager
