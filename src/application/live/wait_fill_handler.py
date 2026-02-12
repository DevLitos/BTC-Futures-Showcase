"""
Wait Fill Handler - Extracted from trading_app.py (Feb 04, 2026)

Handles the WAIT_FILL state: monitoring pending orders for fill confirmation,
handling timeouts, late fills, position verification, and SL/TP placement.

This extracts ~540 lines from trading_app.py.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, Callable, Awaitable, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from src.domain.trading.state.state_machine import StateMachine
    from src.domain.risk.risk_engine import RiskEngine
    from src.infrastructure.storage import PositionStore
    from src.infrastructure.monitoring.system_health import SystemHealthMonitor
    from src.infrastructure.storage.parquet.trade_logger import TradeLogger

from src.domain.trading.state.state_machine import BotState
from src.domain.trading.strategy.pullback_short import TradeType
from src.infrastructure.exchanges.kraken.execution import KRAKEN_BTC_MIN_SIZE
from src.config.trading_config import get_session_name

logger = logging.getLogger(__name__)

# Constants
ENTRY_TIMEOUT_MINUTES = 3  # 3 minutes = 3 candles
SETUP_TTL_MS = 20000  # 20 seconds max for valid setup


@dataclass
class FillHandlerContext:
    """All dependencies and state needed by the fill handler."""
    # Services
    symbol: str
    execution: Any
    risk_engine: 'RiskEngine'
    state_machine: 'StateMachine'
    position_store: 'PositionStore'
    health_monitor: Optional['SystemHealthMonitor']
    trade_logger: 'TradeLogger'
    output_manager: Optional[Any]
    exhaustion_detector: Any
    edge_manager: Any
    
    # Current state
    pending_order_id: Optional[str]
    entry_order_time: Optional[datetime]
    last_entry_data: Optional[Dict[str, Any]]
    current_trade_tags: Dict[str, Any]
    current_trade_regime: str
    entry_score: float
    is_backtest: bool
    session_trade_counts: Dict[str, int]
    
    # Callbacks
    reset_trade_state: Callable[[str], None]
    reconcile_orders: Callable[[], Awaitable[None]]
    record_trade_price: Callable[[float], None]
    build_ml_feature_snapshot: Callable[[], Dict[str, Any]]


@dataclass  
class FillHandlerResult:
    """Result of fill handler execution."""
    handled: bool = False
    new_state: Optional[BotState] = None
    pending_order_id: Optional[str] = None
    stops_set: bool = False
    current_trade_id: Optional[str] = None
    gap_history: list = field(default_factory=list)
    exhaustion_tightened: bool = False
    last_trade_time: Optional[datetime] = None


class WaitFillHandler:
    """
    Handles WAIT_FILL state: monitoring pending orders for fills.
    
    Responsibilities:
    - Check for order fills (poll_fill_status)
    - Handle backtest instant fills
    - Handle order timeouts with late fill detection
    - Handle rejected orders (POST_ONLY crossed book)
    - Verify position on exchange after fill
    - Create local position tracking
    - Place SL/TP on filled position
    - Send notifications
    """
    
    def __init__(self):
        """Initialize handler."""
        self._consecutive_post_only_rejects = 0
    
    async def handle(
        self,
        ctx: FillHandlerContext,
        market_time: datetime
    ) -> FillHandlerResult:
        """
        Handle WAIT_FILL state for one tick.
        
        Args:
            ctx: All dependencies and state
            market_time: Current market time
            
        Returns:
            FillHandlerResult with state changes to apply
        """
        result = FillHandlerResult()
        
        if not ctx.pending_order_id:
            ctx.reset_trade_state("WAIT_FILL with no pending order")
            ctx.state_machine.transition_to(BotState.IDLE, "No pending order")
            result.handled = True
            result.new_state = BotState.IDLE
            return result
        
        # =====================================================================
        # 🔥 FIX #181 (Feb 11, 2026): Fast-path if position already exists
        # This means fill was processed but timeout interrupted before transition
        # Just transition to MANAGE immediately
        # =====================================================================
        if ctx.risk_engine.position is not None:
            logger.warning(f"🔄 FIX #181: WAIT_FILL with existing position - fast transition to MANAGE")
            ctx.state_machine.transition_to(BotState.MANAGE, "FIX #181: Position exists")
            result.handled = True
            result.new_state = BotState.MANAGE
            result.pending_order_id = None
            return result
        
        # =====================================================================
        # BACKTEST-ONLY instant fill from fills list
        # =====================================================================
        if ctx.is_backtest and hasattr(ctx.execution, 'fills'):
            backtest_result = await self._handle_backtest_fill(ctx, market_time)
            if backtest_result.handled:
                return backtest_result
        
        # Check order timeout (3 minutes = 3 candles)
        if ctx.entry_order_time:
            timeout_result = await self._handle_timeout(ctx, market_time)
            if timeout_result.handled:
                return timeout_result
        
        # Check if order was filled
        try:
            fill_result = await self._check_fill_status(ctx, market_time)
            return fill_result
        except Exception as e:
            logger.error(f"Error checking fill status: {e}")
            return result
    
    async def _handle_backtest_fill(
        self,
        ctx: FillHandlerContext,
        market_time: datetime
    ) -> FillHandlerResult:
        """Handle instant fill in backtest mode."""
        result = FillHandlerResult()
        
        for fill in ctx.execution.fills:
            if fill.get('order_id') == ctx.pending_order_id:
                logger.info(f"✅ Order {ctx.pending_order_id} already filled (backtest) - processing immediately")
                
                if ctx.last_entry_data:
                    entry_data = ctx.last_entry_data
                    fill_size = fill.get('amount', entry_data['position_size'])
                    fill_price = fill.get('price', entry_data['entry_price'])
                    
                    # Update position_store
                    try:
                        fill_side = 'buy' if entry_data['bias'].upper() == 'LONG' else 'sell'
                        await ctx.position_store.record_fill(
                            order_id=ctx.pending_order_id or 'bt_order',
                            symbol=ctx.symbol,
                            side=fill_side,
                            size=fill_size,
                            price=fill_price,
                            fee=0.0,
                            fill_type='MAKER',
                            fill_id=f"{ctx.pending_order_id}_fill",
                        )
                    except Exception as ps_err:
                        logger.warning(f"⚠️ Could not update position_store (backtest WAIT_FILL): {ps_err}")
                    
                    ctx.risk_engine.create_position(
                        symbol=ctx.symbol,
                        direction=entry_data['bias'],
                        entry_price=fill_price,
                        position_size=fill_size,
                        stop_loss=entry_data['stop_loss'],
                        atr_1m=entry_data['atr_1m'],
                        market_time=market_time,
                        trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL),
                        regime_mode=ctx.current_trade_regime,
                        edge_type=ctx.current_trade_tags.get('edge_type'),
                        entry_score=ctx.current_trade_tags.get('entry_score'),
                        is_weekend=market_time.weekday() >= 5 if market_time else False
                    )
                    
                    ctx.exhaustion_detector.start_tracking(market_time)
                    result.exhaustion_tightened = False
                    result.gap_history = []
                    
                    ctx.record_trade_price(fill_price)
                    
                    if ctx.pending_order_id:
                        ctx.state_machine.record_trade_open(ctx.pending_order_id)
                    ctx.reset_trade_state("Instant fill processed")
                    ctx.state_machine.transition_to(BotState.MANAGE, "Fill detected (backtest)")
                    
                    logger.info(f"🎯 Position opened from WAIT_FILL: {entry_data['bias']} {fill_size} @ {fill_price:.2f}")
                    
                    result.handled = True
                    result.new_state = BotState.MANAGE
                    result.pending_order_id = None
                    return result
        
        return result
    
    async def _handle_timeout(
        self,
        ctx: FillHandlerContext,
        market_time: datetime
    ) -> FillHandlerResult:
        """Handle order timeout with late fill detection."""
        result = FillHandlerResult()
        
        if ctx.entry_order_time is None:
            logger.warning("No entry_order_time set - cannot check timeout")
            return result
        
        elapsed = (market_time - ctx.entry_order_time).total_seconds()
        
        # ================================================================
        # 🔥 FIX #172b (Feb 11, 2026): HARD ESCAPE after 10 minutes
        # If stuck in WAIT_FILL for > 10 min, force exit unconditionally
        # ================================================================
        if elapsed > 600:  # 10 minutes = absolute max WAIT_FILL time
            logger.critical(f"🚨 FIX #172b: HARD ESCAPE - stuck in WAIT_FILL for {elapsed:.0f}s (>10min)!")
            
            # Force cleanup
            try:
                await ctx.execution.cancel_order(ctx.pending_order_id, ctx.symbol)
            except Exception:
                pass  # Ignore - we're force escaping anyway
            
            ctx.reset_trade_state("FIX #172b: HARD ESCAPE from WAIT_FILL")
            ctx.state_machine.transition_to(BotState.IDLE, "FIX #172b: Hard escape timeout")
            
            result.handled = True
            result.new_state = BotState.IDLE
            result.pending_order_id = None
            return result
        
        if elapsed <= ENTRY_TIMEOUT_MINUTES * 60:
            return result  # Not timed out yet
        
        logger.warning(f"Entry timeout after {elapsed:.0f}s - attempting cancel")
        
        # Step 1: Check if order was ALREADY filled before we cancel
        pre_cancel_filled = False
        try:
            fill_status = await ctx.execution.poll_fill_status(
                ctx.pending_order_id, ctx.symbol
            )
            if fill_status.get('filled'):
                logger.warning("⚠️ Order appears FILLED during timeout check - processing fill instead")
                pre_cancel_filled = True
        except Exception as e:
            logger.debug(f"Pre-cancel fill check error: {e}")
        
        if pre_cancel_filled:
            return result  # Let normal fill flow handle it
        
        # Step 2: Cancel the order
        try:
            cancel_success = await ctx.execution.cancel_order(
                ctx.pending_order_id,
                ctx.symbol
            )
            if ctx.health_monitor:
                ctx.health_monitor.record_order_canceled()
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
        
        # Step 3: Wait briefly and check for LATE FILL (race condition)
        await asyncio.sleep(0.5)
        
        try:
            exchange_has_position = await ctx.execution.has_open_position(ctx.symbol)
            if exchange_has_position:
                logger.critical("🚨 LATE FILL DETECTED: Order filled after timeout!")
                logger.critical("🔄 Closing orphan position created by late fill...")
                
                exchange_pos = await ctx.execution.get_position(ctx.symbol)
                if exchange_pos:
                    size = abs(float(exchange_pos.get('contracts', 0) or 0))
                    side = exchange_pos.get('side', '').lower()
                    
                    if size >= KRAKEN_BTC_MIN_SIZE:
                        close_side = 'sell' if side == 'long' else 'buy'
                        await ctx.execution.close_position_market(
                            symbol=ctx.symbol,
                            side=close_side,
                            amount=size,
                            reason="LATE_FILL"
                        )
                        logger.info(f"✅ Late fill position closed: {size:.6f} {side}")
                        
                        if ctx.output_manager:
                            await ctx.output_manager.send_critical_alert(
                                f"🚨 LATE FILL: Closed orphan {side} {size:.6f} (order filled after timeout)"
                            )
                    elif size > 0:
                        logger.warning(f"⚠️ Late fill size {size:.6f} < min - considered closed")
        except Exception as e:
            logger.error(f"Error checking late fill: {e}")
        
        # Step 4: Clean up local state
        if ctx.pending_order_id in ctx.execution.open_orders:
            ctx.execution.open_orders.pop(ctx.pending_order_id, None)
            logger.debug(f"Cleaned order {ctx.pending_order_id} from local tracking")
        
        # Step 5: Reconcile to ensure clean state
        await ctx.reconcile_orders()
        
        # Step 6: Complete cleanup
        ctx.reset_trade_state("Order timeout (enhanced)")
        ctx.state_machine.transition_to(BotState.IDLE, "Entry timeout")
        
        result.handled = True
        result.new_state = BotState.IDLE
        result.pending_order_id = None
        return result
    
    async def _check_fill_status(
        self,
        ctx: FillHandlerContext,
        market_time: datetime
    ) -> FillHandlerResult:
        """Check if order was filled and handle accordingly."""
        result = FillHandlerResult()
        
        fill_status = await ctx.execution.poll_fill_status(
            ctx.pending_order_id,
            ctx.symbol
        )
        
        # Handle rejected order (POST_ONLY that crossed book)
        if fill_status.get('rejected'):
            return await self._handle_rejection(ctx)
        
        # Reset rejection counter on successful fill
        self._consecutive_post_only_rejects = 0
        
        if fill_status.get('filled'):
            return await self._process_fill(ctx, market_time, fill_status)
        
        # Still pending - stay in WAIT_FILL
        return result
    
    async def _handle_rejection(
        self,
        ctx: FillHandlerContext
    ) -> FillHandlerResult:
        """
        Handle rejected order (POST_ONLY crossed book).
        
        🔥 FIX #78 (Feb 06, 2026): POST-ONLY RETRY MECHANISM
        Instead of immediately giving up:
        1. Retry 3 times with price adjustment (+/- tick to win spread)
        2. After 3 failures, fallback to MARKET order at 0.5× size
        
        User requirement: "3 retries with price adjustment, then market at 0.5× size"
        """
        result = FillHandlerResult()
        
        logger.warning(f"🚫 Order {ctx.pending_order_id} was REJECTED by exchange (likely POST_ONLY crossed book)")
        
        if ctx.health_monitor:
            ctx.health_monitor.record_order_rejected()
        
        self._consecutive_post_only_rejects += 1
        logger.info(f"📊 FIX #78: POST_ONLY rejection count: {self._consecutive_post_only_rejects}/3")
        
        # =================================================================
        # FIX #78: RETRY MECHANISM
        # =================================================================
        MAX_RETRIES = 3
        PRICE_ADJUSTMENT_TICKS = 1.0  # $1 adjustment per retry
        FALLBACK_SIZE_MULTIPLIER = 0.5  # Market order at half size
        
        entry_data = ctx.last_entry_data
        
        if not entry_data:
            logger.warning("⚠️ FIX #78: No entry_data available, cannot retry")
            ctx.reset_trade_state("Order rejected - no entry data for retry")
            ctx.state_machine.transition_to(BotState.IDLE, "Order rejected")
            result.handled = True
            result.new_state = BotState.IDLE
            result.pending_order_id = None
            return result
        
        bias = entry_data.get('bias', 'LONG')
        original_price = entry_data.get('entry_price', 0)
        original_size = entry_data.get('position_size', 0)
        stop_loss = entry_data.get('stop_loss', 0)
        take_profit = entry_data.get('take_profit', 0)
        
        if self._consecutive_post_only_rejects < MAX_RETRIES:
            # ==========================================
            # RETRY WITH PRICE ADJUSTMENT
            # ==========================================
            # LONG: Lower price to be more aggressive (below current ask)
            # SHORT: Higher price to be more aggressive (above current bid)
            adjustment = PRICE_ADJUSTMENT_TICKS * self._consecutive_post_only_rejects
            
            if bias == 'LONG':
                adjusted_price = original_price - adjustment
            else:
                adjusted_price = original_price + adjustment
            
            logger.info(f"🔄 FIX #78: RETRY {self._consecutive_post_only_rejects}/{MAX_RETRIES} - "
                       f"{bias} @ {adjusted_price:.2f} (adjusted from {original_price:.2f})")
            
            try:
                order_result = await ctx.execution.place_limit_order(
                    symbol=ctx.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=original_size,
                    price=adjusted_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
                
                if order_result and order_result.get('id'):
                    logger.info(f"✅ FIX #78: RETRY ORDER PLACED: {order_result['id']}")
                    
                    # Update pending order - stay in WAIT_FILL
                    result.handled = True
                    result.pending_order_id = order_result['id']
                    # Note: state stays WAIT_FILL - just update the order ID
                    
                    # Update entry data with new price
                    if ctx.last_entry_data is not None:
                        ctx.last_entry_data['entry_price'] = adjusted_price
                        ctx.last_entry_data['tags']['post_only_retry'] = self._consecutive_post_only_rejects
                        ctx.last_entry_data['tags']['original_price'] = original_price
                    
                    return result
                else:
                    logger.warning(f"⚠️ FIX #78: Retry order returned None")
                    
            except Exception as retry_err:
                logger.error(f"❌ FIX #78: Retry order failed: {retry_err}")
        
        # ==========================================
        # MAX RETRIES REACHED → MARKET ORDER FALLBACK
        # ==========================================
        if self._consecutive_post_only_rejects >= MAX_RETRIES:
            reduced_size = original_size * FALLBACK_SIZE_MULTIPLIER
            
            logger.warning(f"🚨 FIX #78: MAX RETRIES ({MAX_RETRIES}) REACHED - "
                          f"FALLBACK to MARKET @ {reduced_size:.6f} (0.5× original)")
            
            try:
                order_result = await ctx.execution.place_aggressive_order(
                    symbol=ctx.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=reduced_size,
                    price=original_price,  # Market uses this as reference
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
                
                if order_result and order_result.get('id'):
                    logger.info(f"✅ FIX #78: MARKET FALLBACK PLACED: {order_result['id']} @ {reduced_size:.6f}")
                    
                    # Reset counter - we got an order placed
                    self._consecutive_post_only_rejects = 0
                    
                    # Update state
                    result.handled = True
                    result.pending_order_id = order_result['id']
                    
                    # Update entry data
                    if ctx.last_entry_data is not None:
                        ctx.last_entry_data['position_size'] = reduced_size
                        ctx.last_entry_data['order_type'] = 'MARKET'
                        ctx.last_entry_data['tags']['post_only_fallback'] = True
                        ctx.last_entry_data['tags']['fallback_size_reduction'] = FALLBACK_SIZE_MULTIPLIER
                    
                    return result
                else:
                    logger.error("❌ FIX #78: Market fallback also failed - giving up")
                    
            except Exception as fallback_err:
                logger.error(f"❌ FIX #78: Market fallback error: {fallback_err}")
        
        # =================================================================
        # ALL RETRIES FAILED - RESET TO IDLE
        # =================================================================
        logger.error(f"❌ FIX #78: All retry attempts exhausted - resetting to IDLE")
        self._consecutive_post_only_rejects = 0
        
        ctx.reset_trade_state("Order rejected after all retries")
        ctx.state_machine.transition_to(BotState.IDLE, "Order rejected - max retries")
        
        result.handled = True
        result.new_state = BotState.IDLE
        result.pending_order_id = None
        return result
    
    async def _process_fill(
        self,
        ctx: FillHandlerContext,
        market_time: datetime,
        fill_status: Dict[str, Any]
    ) -> FillHandlerResult:
        """Process a confirmed fill."""
        result = FillHandlerResult()
        
        # ================================================================
        # 🔥 FIX #181 (Feb 11, 2026): Guard against duplicate fill processing
        # If position already exists in risk_engine, we already processed this fill
        # This prevents loop where timeout cancels mid-processing and we restart
        # ================================================================
        if ctx.risk_engine.position is not None:
            logger.warning(f"🔄 FIX #181: Position already exists - fill already processed, skipping to MANAGE")
            ctx.state_machine.transition_to(BotState.MANAGE, "FIX #181: Fill already processed")
            result.handled = True
            result.new_state = BotState.MANAGE
            result.pending_order_id = None
            return result
        
        logger.info(f"✅ Entry order FILLED - verifying position on exchange...")
        
        # Capture order→fill latency
        fill_latency_ms = 0
        order_type = 'market'
        was_converted = False
        
        if ctx.last_entry_data:
            entry_ts = ctx.last_entry_data.get('tags', {}).get('entry_timestamp_ms', 0)
            order_type = ctx.last_entry_data.get('order_type', 'LIMIT').lower()
            original_order_type = ctx.last_entry_data.get('original_order_type', order_type)
            was_converted = (original_order_type.lower() == 'limit' and order_type == 'market')
            
            if entry_ts:
                fill_ts = int(market_time.timestamp() * 1000)
                fill_latency_ms = fill_ts - entry_ts
                ctx.last_entry_data['tags']['order_fill_latency_ms'] = fill_latency_ms
                if fill_latency_ms > 5000:
                    logger.info(f"🏷️ TAG: Order→fill latency: {fill_latency_ms}ms ({order_type}, converted={was_converted})")
        
        # Check for stale setup (TTL expired)
        if order_type == 'market' and not was_converted and fill_latency_ms > SETUP_TTL_MS:
            logger.warning(f"⚠️ SETUP TTL EXPIRED: {fill_latency_ms}ms > {SETUP_TTL_MS}ms - LTF context stale")
            logger.warning("📉 Closing stale entry - waiting for fresh setup")
            
            try:
                await ctx.execution.close_position(ctx.symbol)
                ctx.reset_trade_state("Setup TTL expired")
                ctx.state_machine.transition_to(BotState.IDLE, "Setup TTL expired")
                
                if ctx.health_monitor:
                    ctx.health_monitor.record_order_filled(
                        latency_ms=fill_latency_ms, order_type=order_type, was_converted=False
                    )
                
                result.handled = True
                result.new_state = BotState.IDLE
                return result
            except Exception as close_err:
                logger.error(f"❌ Could not close stale entry: {close_err}")
        
        # Record fill in health monitor
        if ctx.health_monitor:
            ctx.health_monitor.record_order_filled(
                latency_ms=fill_latency_ms,
                order_type=order_type,
                was_converted=was_converted
            )
        
        # Verify position exists on exchange
        import asyncio
        await asyncio.sleep(0.5)
        exchange_has_position = await ctx.execution.has_open_position(ctx.symbol)
        
        if not exchange_has_position:
            logger.error("⚠️ Order filled but NO position on exchange - possible partial fill or error")
            ctx.reset_trade_state("Fill reported but no position")
            ctx.state_machine.transition_to(BotState.IDLE, "No position after fill")
            result.handled = True
            result.new_state = BotState.IDLE
            return result
        
        logger.info("✅ Position confirmed on exchange - creating local tracking")
        
        # Get position data for validation
        position_data = None
        if ctx.last_entry_data:
            expected_side = ctx.last_entry_data.get('bias', '').lower()
            position_data = await ctx.execution.get_position(ctx.symbol)
            if position_data:
                exchange_side = position_data.get('side', '').lower()
                if exchange_side and expected_side and exchange_side != expected_side:
                    logger.critical(f"🚨 SIDE MISMATCH: System={expected_side.upper()} vs Exchange={exchange_side.upper()}")
                    logger.critical("⚠️ Correcting system side to match exchange reality")
                    ctx.last_entry_data['bias'] = exchange_side.upper()
                elif exchange_side:
                    logger.info(f"✅ Side validated: System={expected_side.upper()} == Exchange={exchange_side.upper()}")
        
        # Clean from open_orders
        if ctx.pending_order_id in ctx.execution.open_orders:
            ctx.execution.open_orders.pop(ctx.pending_order_id, None)
        
        # Create position tracking
        if ctx.last_entry_data:
            entry_data = ctx.last_entry_data
            
            # Get actual fill data from exchange
            actual_price = entry_data['entry_price']
            actual_size = entry_data['position_size']
            
            if position_data:
                exchange_price = float(position_data.get('entryPrice', 0) or 0)
                exchange_qty = abs(float(position_data.get('contracts', 0) or 0))
                
                if exchange_price > 0:
                    if abs(exchange_price - actual_price) > 0.01:
                        logger.info(f"🔄 Using actual fill price: {actual_price:.2f} → {exchange_price:.2f}")
                    actual_price = exchange_price
                
                if exchange_qty > 0:
                    if abs(exchange_qty - actual_size) > 0.00001:
                        logger.info(f"🔄 Using actual fill size: {actual_size:.6f} → {exchange_qty:.6f}")
                    actual_size = exchange_qty
            
            # Update position_store
            try:
                fill_side = 'buy' if entry_data['bias'].upper() == 'LONG' else 'sell'
                existing_pos = ctx.position_store.get_position(ctx.symbol or '')
                if existing_pos and abs(existing_pos.size - actual_size) < 0.00001:
                    logger.info(f"✅ position_store already updated (market order path): {fill_side.upper()} {actual_size:.6f}")
                else:
                    await ctx.position_store.record_fill(
                        order_id=ctx.pending_order_id or 'unknown',
                        symbol=ctx.symbol,
                        side=fill_side,
                        size=actual_size,
                        price=actual_price,
                        fee=0.0,
                        fill_type='MAKER',
                        fill_id=f"{ctx.pending_order_id}_fill",
                    )
                    logger.info(f"✅ position_store updated: {fill_side.upper()} {actual_size:.6f} @ {actual_price:.2f}")
            except Exception as ps_err:
                logger.warning(f"⚠️ Could not update position_store on fill: {ps_err}")
            
            # Create position in risk engine
            ctx.risk_engine.create_position(
                symbol=ctx.symbol,
                direction=entry_data['bias'],
                entry_price=actual_price,
                position_size=actual_size,
                stop_loss=entry_data['stop_loss'],
                atr_1m=entry_data['atr_1m'],
                market_time=market_time,
                trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL),
                regime_mode=ctx.current_trade_regime,
                edge_type=ctx.current_trade_tags.get('edge_type'),
                entry_score=ctx.current_trade_tags.get('entry_score'),
                is_weekend=market_time.weekday() >= 5 if market_time else False
            )
            
            # Start exhaustion tracking
            ctx.exhaustion_detector.start_tracking(market_time)
            result.exhaustion_tightened = False
            result.gap_history = []
            
            trade_type_str = entry_data.get('trade_type', TradeType.LONG_MAIN)
            trade_type_name = trade_type_str.value if hasattr(trade_type_str, 'value') else str(trade_type_str)
            logger.info(f"Position created: {entry_data['bias']} {actual_size:.6f} @ {actual_price:.2f} [{trade_type_name}]")
            
            # Update session trade count
            session_for_count = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
            ctx.session_trade_counts[session_for_count] = ctx.session_trade_counts.get(session_for_count, 0) + 1
            logger.debug(f"📊 Session {session_for_count}: trade count → {ctx.session_trade_counts[session_for_count]}")
            
            # Start trade logging
            trade_tags = entry_data.get('tags', {})
            trade_score_breakdown = entry_data.get('score_breakdown', {})
            try:
                trade_type_for_log = entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL)
                trade_type_name = trade_type_for_log.value if hasattr(trade_type_for_log, 'value') else str(trade_type_for_log)
                
                # 🔥 EDGE TYPE (Feb 05, 2026): Get edge_type from tags
                edge_type_for_log = trade_tags.get('edge_type', '') if isinstance(trade_tags, dict) else ''
                
                result.current_trade_id = ctx.trade_logger.start_trade(
                    symbol=ctx.symbol,
                    direction=entry_data['bias'],
                    entry_price=actual_price,
                    stop_loss=entry_data['stop_loss'],
                    take_profit=float(entry_data.get('take_profit', 0) or 0),
                    position_size=actual_size,
                    entry_score=entry_data.get('score', 0),
                    entry_tags=list(trade_tags.keys()) if isinstance(trade_tags, dict) else [],
                    regime=trade_tags.get('regime', 'UNKNOWN'),
                    volatility_regime=trade_tags.get('volatility_regime', 'NORMAL'),
                    session=session_for_count,
                    trade_type=trade_type_name,
                    score_breakdown=trade_score_breakdown,
                    metadata={'worst_case_rr': trade_tags.get('rr_worst_case')},
                    feature_snapshot=ctx.build_ml_feature_snapshot(),
                    entry_trading_mode=trade_tags.get('trading_mode', ''),
                    entry_daily_regime=trade_tags.get('daily_regime', ''),
                    edge_type=edge_type_for_log
                )
                
                # Record trade executed
                from src.domain.trading.activity_tracker import record_trade_executed
                record_trade_executed(market_time)
                result.last_trade_time = market_time
                
                # Record entry in edge manager
                ctx.edge_manager.record_entry(market_time)
                logger.debug(f"🎯 Edge entry recorded")
            except Exception as log_err:
                logger.error(f"TradeLogger.start_trade() failed: {log_err} - continuing without trade logging")
                result.current_trade_id = None
            
            # Record in health monitor
            if ctx.health_monitor:
                ctx.health_monitor.record_trade_entry(
                    side=entry_data['bias'],
                    size=entry_data['position_size'],
                    score=int(ctx.entry_score)
                )
            
            # Validate position integrity
            if position_data and ctx.risk_engine.position:
                exchange_side = position_data.get('side', '').lower()
                exchange_qty = abs(float(position_data.get('contracts', 0)))
                
                integrity_ok = ctx.risk_engine.position.validate_integrity(
                    exchange_side=exchange_side,
                    exchange_qty=exchange_qty,
                    qty_tolerance=0.02
                )
                
                if not integrity_ok:
                    pos = ctx.risk_engine.position
                    has_side_mismatch = False
                    if pos is not None:
                        has_side_mismatch = any('SIDE MISMATCH' in e for e in pos.integrity_errors)
                    
                    if has_side_mismatch:
                        logger.critical("🚨 SIDE MISMATCH - SL/TP placement BLOCKED")
                        if ctx.health_monitor:
                            ctx.health_monitor.record_integrity_error(
                                f"side_mismatch: system={entry_data['bias']} vs exchange={exchange_side}"
                            )
                        result.stops_set = True  # Prevent stop placement
                    else:
                        logger.warning("⚠️ Partial fill detected - size adjusted, proceeding with SL/TP")
                        if ctx.health_monitor:
                            ctx.health_monitor.record_integrity_error(
                                f"qty_mismatch: expected={pos.expected_qty:.6f} vs exchange={exchange_qty:.6f}",
                                is_critical=False
                            )
            
            # Set SL/TP on position
            if not result.stops_set:
                stops_set = await self._set_stops_with_retry(ctx, entry_data)
                result.stops_set = stops_set
                
                if not stops_set:
                    # Critical: force close unprotected position
                    logger.critical("🚨 CRITICAL: SL/TP placement FAILED after 3 retries - FORCE CLOSING position!")
                    if ctx.risk_engine.position:
                        ctx.risk_engine.position.mark_exchange_sl_inactive("All placement attempts failed")
                    if ctx.health_monitor:
                        ctx.health_monitor.record_integrity_error(
                            "SL_PLACEMENT_FAILED: Force closing unprotected position",
                            is_critical=True
                        )
                    # Note: emergency_close_all needs to be called by trading_app
                    result.handled = True
                    result.new_state = None  # Signal that emergency close is needed
                    return result
        else:
            logger.error("No entry data available - Fill without entry tracking")
            ctx.state_machine.transition_to(BotState.IDLE, "Fill without entry data")
            result.handled = True
            result.new_state = BotState.IDLE
            result.pending_order_id = None
            return result
        
        # Send entry notification
        output_mgr = ctx.output_manager
        if output_mgr and ctx.last_entry_data:
            try:
                entry_data = ctx.last_entry_data
                import asyncio
                logger.info(f"📲 Sending entry notification: {entry_data.get('bias')} @ {entry_data.get('entry_price')}")
                
                async def safe_notify():
                    try:
                        await output_mgr.send_entry_notification({
                            'symbol': ctx.symbol,
                            'direction': entry_data.get('bias', 'UNKNOWN'),
                            'entry_price': entry_data.get('entry_price'),
                            'stop_loss': entry_data.get('stop_loss'),
                            'take_profit': entry_data.get('take_profit'),
                            'size': entry_data.get('position_size'),
                            'score': ctx.entry_score,
                            'regime': ctx.current_trade_regime,
                            'risk_usd': entry_data.get('tags', {}).get('risk_usd'),
                            'timestamp': market_time.isoformat() if hasattr(market_time, 'isoformat') else str(market_time)
                        })
                    except Exception as e:
                        logger.warning(f"⚠️ Entry notification error: {e}")
                
                asyncio.create_task(safe_notify(), name="send_entry_notification")
            except Exception as notif_err:
                logger.warning(f"⚠️ Entry notification task creation error: {notif_err}")
        
        # Final state transition
        result.pending_order_id = None
        ctx.state_machine.transition_to(BotState.MANAGE, "Order filled")
        result.handled = True
        result.new_state = BotState.MANAGE
        return result
    
    async def _set_stops_with_retry(
        self,
        ctx: FillHandlerContext,
        entry_data: Dict[str, Any]
    ) -> bool:
        """Set SL/TP with exponential backoff retry."""
        logger.info("📍 Setting SL/TP on open position...")
        
        max_sl_retries = 3
        base_delay = 0.5
        
        for attempt in range(max_sl_retries):
            stops_set = await ctx.execution.set_position_stops(
                symbol=ctx.symbol,
                stop_loss=entry_data['stop_loss'],
                take_profit=entry_data.get('take_profit')
            )
            
            if stops_set:
                logger.info(f"✅ SL/TP successfully set on position (attempt {attempt + 1})")
                if ctx.risk_engine.position:
                    ctx.risk_engine.position.mark_reduce_only_validated()
                    ctx.risk_engine.position.mark_exchange_sl_active()
                return True
            else:
                logger.warning(f"⚠️ SL/TP placement attempt {attempt + 1}/{max_sl_retries} failed")
                if attempt < max_sl_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"⏳ Waiting {delay:.1f}s before retry...")
                    await asyncio.sleep(delay)
        
        return False


# Factory function
_wait_fill_handler: Optional[WaitFillHandler] = None

def get_wait_fill_handler() -> WaitFillHandler:
    """Get singleton WaitFillHandler instance."""
    global _wait_fill_handler
    if _wait_fill_handler is None:
        _wait_fill_handler = WaitFillHandler()
    return _wait_fill_handler
