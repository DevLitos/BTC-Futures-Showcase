"""
Trade Recording Module - Handles all trade result recording and cooldown logic.

EXTRACTED from SignalEngine._record_trade_result()
Contains:
- Loss streak tracking with weighted exit types
- Tiered cooldown system
- Kill switch logic
- Dead hours blocking
- Countertrend and short obstinacy tracking
- Expansion trade false positive detection
- Win recovery calculation

Reduces trading_app.py by ~200 lines.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Any

from src.config.trading_config import LossStreakConfig, ShortObstinacyConfig, OperatingMode

logger = logging.getLogger(__name__)


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class TradeRecordingState:
    """State for trade recording (passed from engine)"""
    consecutive_losses: float = 0.0
    daily_losses_count: int = 0
    loss_exit_types: dict = field(default_factory=dict)
    countertrend_losses_session: int = 0
    last_countertrend_loss_time: Optional[datetime] = None
    short_cooldown_until: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    kill_switch_active: bool = False
    dead_hours_losses: int = 0
    dead_hours_blocked: bool = False
    expansion_trades_count: int = 0
    false_expansion_count: int = 0
    consecutive_expansion_losses: int = 0


@dataclass 
class TradeRecordingResult:
    """Result of recording a trade"""
    new_consecutive_losses: float
    new_daily_losses_count: int
    new_countertrend_losses: int
    cooldown_until: Optional[datetime] = None
    kill_switch_activated: bool = False
    dead_hours_blocked: bool = False
    short_cooldown_until: Optional[datetime] = None
    expansion_cluster_cooldown: bool = False


# ============================================================================
# TRADE RECORDER
# ============================================================================

class TradeRecorder:
    """
    Records trade results and manages cooldowns.
    
    Features:
    - Weighted loss streak (exit type dependent)
    - Tiered cooldown system (3/4/5+ losses)
    - Kill switch at 6+ losses
    - Dead hours loss tracking
    - Countertrend loss tracking
    - Short obstinacy cooldown
    - False expansion detection
    - Gradual win recovery
    """
    
    def __init__(
        self,
        kill_switch_threshold: int = 6,
        pause_after_3: int = 15,
        pause_after_4: int = 60,
        pause_after_5: int = 180,
        dead_hours_start: int = 4,
        dead_hours_end: int = 7,
        dead_hours_max_losses: int = 2,
    ):
        self.kill_switch_threshold = kill_switch_threshold
        self.pause_after_3 = pause_after_3
        self.pause_after_4 = pause_after_4
        self.pause_after_5 = pause_after_5
        self.dead_hours_start = dead_hours_start
        self.dead_hours_end = dead_hours_end
        self.dead_hours_max_losses = dead_hours_max_losses
    
    def record_result(
        self,
        exit_reason: str,
        pnl: float,
        market_time: datetime,
        trade_tags: dict,
        state: TradeRecordingState,
        health_monitor=None,
    ) -> TradeRecordingResult:
        """
        Record a trade result and update cooldown state.
        
        Args:
            exit_reason: Why the trade closed (STOP_LOSS, TP1_FULL, etc.)
            pnl: Profit/loss amount
            market_time: Current market time
            trade_tags: Current trade tags dict
            state: Current recording state
            health_monitor: Optional health monitor for exit recording
            
        Returns: TradeRecordingResult with updated state
        """
        result = TradeRecordingResult(
            new_consecutive_losses=state.consecutive_losses,
            new_daily_losses_count=state.daily_losses_count,
            new_countertrend_losses=state.countertrend_losses_session,
        )
        
        # Record in health monitor
        if health_monitor:
            exit_type = 'sl' if exit_reason == 'STOP_LOSS' or pnl < 0 else 'tp'
            health_monitor.record_exit(exit_type=exit_type)
        
        # Extract trade context
        is_countertrend = trade_tags.get('is_countertrend', False)
        was_short = trade_tags.get('direction', '') == 'SHORT'
        bullish_signals = trade_tags.get('bullish_signals', 0)
        
        # Check for false expansion
        result = self._handle_expansion_tracking(
            result, state, trade_tags, pnl, market_time
        )
        
        if pnl < 0:
            result = self._handle_loss(
                result, state, exit_reason, pnl, market_time,
                is_countertrend, was_short, bullish_signals
            )
        else:
            result = self._handle_win(result, state, pnl, trade_tags)
        
        return result
    
    def _handle_expansion_tracking(
        self,
        result: TradeRecordingResult,
        state: TradeRecordingState,
        trade_tags: dict,
        pnl: float,
        market_time: datetime,
    ) -> TradeRecordingResult:
        """Handle false expansion detection."""
        was_expansion_trade = trade_tags.get('regime', '') == 'TREND_EXPANSION'
        trade_mfe_r = trade_tags.get('mfe_r', 0.0)
        
        if not was_expansion_trade:
            return result
        
        new_expansion_count = state.expansion_trades_count + 1
        new_false_count = state.false_expansion_count
        new_consecutive = state.consecutive_expansion_losses
        
        if pnl < 0 and trade_mfe_r < 0.3:
            new_false_count += 1
            new_consecutive += 1
            false_rate = (new_false_count / new_expansion_count * 100) if new_expansion_count > 0 else 0
            logger.warning(
                f"🔴 FALSE EXPANSION #{new_false_count}: "
                f"MFE={trade_mfe_r:.2f}R < 0.3R AND loss → "
                f"Rate: {false_rate:.1f}% ({new_false_count}/{new_expansion_count})"
            )
            
            # Cluster protection
            if new_consecutive >= 3:
                cooldown_mins = 120
                result.cooldown_until = market_time + timedelta(minutes=cooldown_mins)
                result.expansion_cluster_cooldown = True
                logger.critical(
                    f"🚨 EXPANSION CLUSTER PROTECTION: "
                    f"{new_consecutive} consecutive false expansions → {cooldown_mins}min cooldown"
                )
        elif pnl > 0:
            if new_consecutive > 0:
                logger.info(f"✅ Expansion win: resetting consecutive expansion losses from {new_consecutive}")
            new_consecutive = 0
        
        return result
    
    def _handle_loss(
        self,
        result: TradeRecordingResult,
        state: TradeRecordingState,
        exit_reason: str,
        pnl: float,
        market_time: datetime,
        is_countertrend: bool,
        was_short: bool,
        bullish_signals: int,
    ) -> TradeRecordingResult:
        """Handle loss recording."""
        # Get weight for exit type
        pnl_r = -1.0  # Approximate as 1R loss
        exit_weight = LossStreakConfig.get_loss_weight(exit_reason, pnl_r)
        
        old_streak = state.consecutive_losses
        new_streak = old_streak + exit_weight
        result.new_consecutive_losses = new_streak
        result.new_daily_losses_count = state.daily_losses_count + 1
        
        logger.info(f"📉 Loss recorded: {exit_reason} (weight={exit_weight:.2f}) → streak: {old_streak:.2f} → {new_streak:.2f}")
        
        # Dead hours tracking
        current_hour = market_time.hour
        if self.dead_hours_start <= current_hour < self.dead_hours_end:
            new_dead_losses = state.dead_hours_losses + 1
            logger.warning(f"🌙 DEAD HOURS LOSS #{new_dead_losses}: Loss during {current_hour}:00 UTC")
            
            if new_dead_losses >= self.dead_hours_max_losses:
                result.dead_hours_blocked = True
                logger.critical(f"🚫 DEAD HOURS BLOCK: {new_dead_losses} losses in dead zone")
        
        # Countertrend tracking
        if is_countertrend:
            result.new_countertrend_losses = state.countertrend_losses_session + 1
            logger.warning(f"⚠️ CT LOSS #{result.new_countertrend_losses}: Countertrend trade failed")
        
        # Short obstinacy cooldown
        if was_short and bullish_signals >= 2:
            cooldown_mins = ShortObstinacyConfig.COOLDOWN_CANDLES_5M * 5
            result.short_cooldown_until = market_time + timedelta(minutes=cooldown_mins)
            logger.warning(f"⚠️ SHORT OBSTINACY COOLDOWN: Lost SHORT with {bullish_signals} bullish signals → {cooldown_mins}min cooldown")
        
        # Tiered cooldown + kill switch
        effective_losses = int(new_streak)
        
        if effective_losses >= self.kill_switch_threshold:
            result.kill_switch_activated = True
            logger.critical(f"🔴 KILL SWITCH ACTIVATED: {effective_losses} consecutive losses → NO MORE TRADING TODAY")
        elif effective_losses >= 5:
            result.cooldown_until = market_time + timedelta(minutes=self.pause_after_5)
            logger.critical(f"🚨 TIER 3 COOLDOWN: {new_streak:.1f} losses → {self.pause_after_5} min pause")
        elif effective_losses >= 4:
            result.cooldown_until = market_time + timedelta(minutes=self.pause_after_4)
            logger.warning(f"🚫 TIER 2 COOLDOWN: {new_streak:.1f} losses → {self.pause_after_4} min pause")
        elif effective_losses >= 3:
            result.cooldown_until = market_time + timedelta(minutes=self.pause_after_3)
            logger.warning(f"⚠️ TIER 1 COOLDOWN: {new_streak:.1f} losses → {self.pause_after_3} min pause")
        
        return result
    
    def _handle_win(
        self,
        result: TradeRecordingResult,
        state: TradeRecordingState,
        pnl: float,
        trade_tags: dict,
    ) -> TradeRecordingResult:
        """Handle win/breakeven with gradual recovery."""
        if state.consecutive_losses <= 0:
            return result
        
        # Calculate PnL in R
        sl_amount = trade_tags.get('sl_amount', 1.0)
        pnl_r = pnl / sl_amount if sl_amount > 0 else 0.0
        
        # Determine recovery based on win quality
        if pnl_r >= 0.8:
            recovery = 2.0
        elif pnl_r >= 0.4:
            recovery = 1.5
        elif pnl_r >= 0.1:
            recovery = 1.0
        else:
            recovery = 0.5
        
        old_streak = state.consecutive_losses
        result.new_consecutive_losses = max(0, old_streak - recovery)
        logger.info(
            f"✅ Win recorded ({pnl_r:.2f}R): streak recovery -{recovery:.1f} "
            f"({old_streak:.1f} → {result.new_consecutive_losses:.1f})"
        )
        
        return result


# ============================================================================
# FACTORY
# ============================================================================

def get_trade_recorder(
    kill_switch_threshold: int = 6,
    pause_after_3: int = 15,
    pause_after_4: int = 60,
    pause_after_5: int = 180,
) -> TradeRecorder:
    """Factory for TradeRecorder."""
    dead_start, dead_end = getattr(OperatingMode, 'DEAD_HOURS_UTC', (4, 7))
    max_dead = getattr(OperatingMode, 'DEAD_HOURS_MAX_LOSSES', 2)
    
    return TradeRecorder(
        kill_switch_threshold=kill_switch_threshold,
        pause_after_3=pause_after_3,
        pause_after_4=pause_after_4,
        pause_after_5=pause_after_5,
        dead_hours_start=dead_start,
        dead_hours_end=dead_end,
        dead_hours_max_losses=max_dead,
    )
