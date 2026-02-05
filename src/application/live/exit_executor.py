"""
Exit Executor - Extracted from trading_app.py (Feb 04, 2026)

Handles the full position close flow with all the necessary steps:
- Reconciler notification
- Exchange size verification
- Order execution
- PnL recording
- Notifications
- State cleanup
"""
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from src.domain.risk.position_tracker import TrackedPosition

logger = logging.getLogger(__name__)

# Minimum BTC size for Kraken
KRAKEN_BTC_MIN_SIZE = 0.0001


class ExitExecutor:
    """
    Executes position closes with full flow.
    
    Encapsulates the 12-step close process:
    1. Reconciler notify (hard-lock)
    2. Get REAL size from exchange
    3. Validate minimum size
    4. Execute close order
    5. Calculate PnL
    6. Update state machine
    7. Record for kill-switch
    8. Log trade event
    9. Telegram notification
    10. Clear risk engine
    11. Reset trade state
    12. Start cooldown and transition
    """
    
    # Breakeven threshold - trades closer than this don't count
    BREAKEVEN_THRESHOLD_R = 0.15
    
    def __init__(
        self,
        execution,
        reconciler,
        state_machine,
        risk_engine,
        trade_logger,
        output_manager=None
    ):
        """
        Initialize exit executor.
        
        Args:
            execution: Order execution service
            reconciler: Exchange state reconciler
            state_machine: Trading state machine
            risk_engine: Risk management engine
            trade_logger: Trade event logger
            output_manager: Optional Telegram notifier
        """
        self.execution = execution
        self.reconciler = reconciler
        self.state_machine = state_machine
        self.risk_engine = risk_engine
        self.trade_logger = trade_logger
        self.output_manager = output_manager
        
        # Track session trades for breakeven refund
        self._session_trade_counts: Dict[str, int] = {}
    
    def set_session_trade_counts(self, counts: Dict[str, int]):
        """Set session trade counts for breakeven refund logic."""
        self._session_trade_counts = counts
    
    async def close_position(
        self,
        symbol: str,
        reason: str,
        current_price: float,
        market_time: datetime,
        position: 'TrackedPosition',
        cooldown_minutes: int = 3,
        extra_log_data: Optional[Dict[str, Any]] = None,
        record_trade_callback=None,
        reset_state_callback=None,
        create_task_func=None
    ) -> bool:
        """
        Execute full position close flow.
        
        Args:
            symbol: Trading symbol
            reason: Exit reason string
            current_price: Current market price
            market_time: Current market time
            position: Position to close
            cooldown_minutes: Post-trade cooldown
            extra_log_data: Additional data for logging
            record_trade_callback: Callback to record trade result
            reset_state_callback: Callback to reset trade state
            create_task_func: Function to create async tasks
        
        Returns:
            True if close successful, False otherwise
        """
        from src.domain.trading.state_machine import BotState
        
        try:
            # 1. Notify reconciler (hard-lock)
            self.reconciler.notify_exit_started(reason)
            
            # 2. Get REAL size from exchange to avoid orphans
            close_size = position.size
            try:
                exchange_pos = await self.execution.get_position(symbol)
                if exchange_pos:
                    exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                    if exchange_size > 0:
                        close_size = exchange_size
                        logger.debug(f"Using exchange size: {close_size}")
            except Exception as e:
                logger.debug(f"Could not get exchange position for {reason}: {e}")
            
            # 3. Validate minimum size
            if close_size < KRAKEN_BTC_MIN_SIZE:
                logger.warning(f"⚠️ {reason} skipped: size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                return False
            
            # 4. Execute close
            close_side = 'sell' if position.direction == 'LONG' else 'buy'
            await self.execution.close_position_market(
                symbol=symbol,
                side=close_side,
                amount=close_size,
                reason=reason
            )
            
            # 5. Calculate PnL
            pnl = position.unrealized_pnl
            is_win = pnl > 0
            pnl_r = position.calculate_r_realized(current_price)
            
            # 6. Update state machine
            self.state_machine.record_trade_close(pnl, is_win, exit_reason=reason)
            self.state_machine.update_daily_pnl(pnl)
            
            # 7. Record for kill-switch tracking
            if record_trade_callback:
                record_trade_callback(reason, pnl, market_time)
            
            # 7b. Record in activity tracker
            try:
                from src.domain.trading.activity_tracker import get_activity_tracker
                get_activity_tracker().record_trade_closed(
                    market_time=market_time,
                    exit_reason=reason,
                    pnl_r=pnl_r,
                    direction=position.direction
                )
            except Exception as e:
                logger.debug(f"Activity tracker error: {e}")
            
            # Breakeven refund logic
            self._handle_breakeven_refund(pnl_r, market_time)
            
            # 8. Log trade event
            base_log_data = {
                'entry': position.entry_price,
                'exit': current_price,
                'size': position.size,
                'pnl': pnl,
                'reason': reason,
                'r': pnl_r,
                'mfe_r': position.calculate_mfe_r(),
                'mae_r': position.calculate_mae_r(),
                'direction': position.direction,
                'regime': getattr(position, 'regime', 'UNKNOWN')
            }
            if extra_log_data:
                base_log_data.update(extra_log_data)
            await self.trade_logger.log_event('trade_closed', base_log_data)
            
            # 9. Telegram notification
            if self.output_manager and create_task_func:
                try:
                    fresh_capital = await self.execution.get_available_capital()
                    if fresh_capital > 0:
                        self.risk_engine.capital = fresh_capital
                except Exception:
                    pass
                
                create_task_func(self.output_manager.send_exit_notification({
                    'symbol': symbol,
                    'reason': reason,
                    'pnl': pnl,
                    'r_realized': pnl_r,
                    'exit_price': current_price,
                    'timestamp': market_time.isoformat(),
                    'account_value': self.risk_engine.capital
                }), f"{reason.lower()}_notification")
            
            # 10. Clear risk engine
            await self.risk_engine.clear_position(reason)
            
            # 11. Reset trade state
            if reset_state_callback:
                reset_state_callback(f"Trade closed: {reason}")
            
            # 12. Cooldown and transition
            self.state_machine.start_cooldown(cooldown_minutes)
            self.state_machine.transition_to(BotState.IDLE, f"Trade closed: {reason}")
            
            logger.info(f"✅ {reason} EXIT: PnL={pnl:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"❌ {reason} close failed: {e}")
            return False
    
    def _handle_breakeven_refund(self, pnl_r: float, market_time: datetime):
        """Refund session trade slot for breakeven trades."""
        if abs(pnl_r) >= self.BREAKEVEN_THRESHOLD_R:
            return
        
        try:
            from src.domain.trading.session_utils import get_session_name
            current_session = get_session_name(
                market_time.hour,
                market_time.minute,
                market_time.weekday()
            )
            if current_session in self._session_trade_counts:
                if self._session_trade_counts[current_session] > 0:
                    self._session_trade_counts[current_session] -= 1
                    logger.warning(
                        f"🔄 BREAKEVEN REFUND: Trade at {pnl_r:.2f}R | "
                        f"Session {current_session}: slot refunded"
                    )
        except Exception as e:
            logger.debug(f"Breakeven refund error: {e}")
