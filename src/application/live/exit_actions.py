"""
Exit Actions Handler - Extracted from SignalEngine (AUDIT REFACTOR)

Created: Jan 27, 2026

Handles exit action execution from risk_engine.manage_position():
- PARTIAL_CLOSE: Close portion of position at TP1
- ADD_POSITION: Scale into winner
- FULL_CLOSE: Full position exit

This module extracts ~400 lines of execution logic from _handle_manage_state.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE

logger = logging.getLogger(__name__)


async def handle_partial_close(
    engine: 'SignalEngine',
    exit_action: Dict[str, Any],
    position,
    current_price: float
) -> bool:
    """
    Execute PARTIAL_CLOSE action (TP1 hit).
    
    Steps:
    1. Validate minimum size
    2. Verify position exists on exchange (race condition prevention)
    3. Verify direction matches
    4. Execute close
    5. Sync local size with exchange
    6. Update SL order with new position size
    7. Log TP1 hit event
    
    Returns:
        True if should return from _handle_manage_state (i.e., action completed)
    """
    close_size = float(exit_action.get('size', 0) or 0)
    
    # Validate minimum size
    if close_size < KRAKEN_BTC_MIN_SIZE:
        logger.warning(f"⚠️ PARTIAL_CLOSE blocked: size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
        return True
    
    if close_size <= 0:
        return True
    
    # Acquire hard lock first
    engine.reconciler.notify_partial_close("SMART_PARTIAL_CLOSE_VERIFY")
    
    # Verify position exists on exchange
    exchange_pos = await engine.execution.get_position(engine.symbol)
    if not exchange_pos:
        logger.warning("⚠️ PARTIAL_CLOSE ABORTED: No position on exchange (was closed externally)")
        engine.reconciler.notify_exit_completed()
        engine._reset_trade_state("Position closed externally before partial")
        engine.state_machine.to_idle("Position closed externally")
        return True
    
    exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
    if exchange_size < KRAKEN_BTC_MIN_SIZE:
        logger.warning(f"⚠️ PARTIAL_CLOSE ABORTED: Exchange position too small ({exchange_size:.6f})")
        engine.reconciler.notify_exit_completed()
        engine._reset_trade_state("Position closed externally before partial")
        engine.state_machine.to_idle("Position closed externally")
        return True
    
    # Verify direction matches
    exchange_side = exchange_pos.get('side', '').lower()
    expected_side = position.direction.lower()
    if exchange_side and exchange_side != expected_side:
        logger.critical(f"⚠️ PARTIAL_CLOSE ABORTED: Direction mismatch! Exchange={exchange_side}, Expected={expected_side}")
        engine.reconciler.notify_exit_completed()
        engine._reset_trade_state("Direction mismatch detected")
        engine.state_machine.to_idle("Direction mismatch")
        return True
    
    # Execute close
    await engine.execution.close_position_market(
        symbol=engine.symbol,
        side='sell' if position.direction == 'LONG' else 'buy',
        amount=close_size,
        reason="PARTIAL"
    )
    
    # Sync local size with exchange
    await asyncio.sleep(0.5)
    try:
        exchange_pos = await engine.execution.get_position(engine.symbol)
        if exchange_pos:
            exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
            if exchange_size > 0 and abs(exchange_size - position.size) > 0.00001:
                logger.warning(f"🔄 Size sync after TP1: local={position.size:.6f} → exchange={exchange_size:.6f}")
                position.size = exchange_size
                
                # Update SL order after partial close
                await _update_sl_after_size_change(engine, position, exchange_size)
        else:
            logger.critical("⚠️ No position on exchange after partial close - was fully closed")
            position.size = 0
    except Exception as sync_e:
        logger.error(f"Error syncing size after partial: {sync_e}")
    
    # Mark TP1 reached
    engine._reached_tp1 = True
    
    # Log event
    await engine.trade_logger.log_event('tp1_hit', {
        'entry': position.entry_price,
        'price': current_price,
        'size_closed': close_size,
        'pnl_realized': exit_action.get('pnl', 0),
        'reason': exit_action.get('reason', 'TP1')
    })
    
    return True  # Stay in MANAGE with reduced size


async def handle_add_position(
    engine: 'SignalEngine',
    exit_action: Dict[str, Any],
    position,
    current_price: float
) -> bool:
    """
    Execute ADD_POSITION action (scale into winner).
    
    Steps:
    1. Verify position exists on exchange
    2. Verify direction matches
    3. Sync local size
    4. Execute add order
    5. Update average entry price
    6. Cancel & replace SL/TP with new size
    7. Log event
    
    Returns:
        True if should return from _handle_manage_state
    """
    add_size = float(exit_action.get('add_size', 0))
    if add_size <= 0:
        return True
    
    # Verify position exists on exchange
    exchange_pos = await engine.execution.get_position(engine.symbol)
    if not exchange_pos:
        logger.warning("⚠️ ADD_POSITION blocked: No position confirmed on exchange")
        return True
    
    exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
    if exchange_size < 0.00001:
        logger.warning("⚠️ ADD_POSITION blocked: Exchange position size is zero")
        return True
    
    # Verify direction matches
    exchange_side = exchange_pos.get('side', '').lower()
    expected_side = position.direction.lower()
    if exchange_side and exchange_side != expected_side:
        logger.critical(f"⚠️ ADD_POSITION ABORTED: Direction mismatch! Exchange={exchange_side}, Expected={expected_side}")
        engine._reset_trade_state("Direction mismatch detected in ADD_POSITION")
        engine.state_machine.to_idle("Direction mismatch")
        return True
    
    # Sync local size if different
    if abs(exchange_size - position.size) > 0.00001:
        logger.warning(f"🔄 Size sync before ADD: local={position.size:.6f} → exchange={exchange_size:.6f}")
        position.size = exchange_size
    
    # Round add_size to exchange precision
    add_size = round_size_to_exchange(add_size)
    if add_size <= 0:
        logger.warning("⚠️ ADD_POSITION blocked: add_size rounds to zero")
        return True
    
    # Place additional order
    side = 'buy' if position.direction == 'LONG' else 'sell'
    logger.info(f"💪 ADDING TO WINNER: {add_size:.6f} {side}")
    
    try:
        add_order = await engine.execution.place_market_order(
            symbol=engine.symbol,
            side=side,
            amount=add_size
        )
        
        if add_order:
            await asyncio.sleep(0.5)
            
            # Re-fetch position to get actual new size
            new_exchange_pos = await engine.execution.get_position(engine.symbol)
            if new_exchange_pos:
                new_exchange_size = abs(float(new_exchange_pos.get('contracts', 0) or 0))
                actual_add = new_exchange_size - exchange_size
                
                if actual_add > 0:
                    # Update average entry price
                    old_entry = position.entry_price
                    old_size = exchange_size
                    new_entry_price = (old_entry * old_size + current_price * actual_add) / (old_size + actual_add)
                    
                    logger.info(
                        f"📊 ENTRY PRICE UPDATE: old={old_entry:.2f} × {old_size:.6f} + "
                        f"new={current_price:.2f} × {actual_add:.6f} = avg={new_entry_price:.2f}"
                    )
                    
                    position.entry_price = new_entry_price
                    position.size = new_exchange_size
                    
                    # Cancel & replace SL/TP with new size
                    await _replace_sl_tp_after_add(engine, position, new_exchange_size, new_entry_price)
                    
                    logger.info(f"✅ Position scaled: added {actual_add:.6f}, new size = {position.size:.6f}")
                else:
                    logger.warning(f"⚠️ Scale-in order may not have filled: expected +{add_size:.6f}, got +{actual_add:.6f}")
                    position.size = new_exchange_size
            else:
                position.size = exchange_size + add_size
                logger.warning(f"⚠️ Could not verify scale-in - assuming filled: {position.size:.6f}")
            
            await engine.trade_logger.log_event('add_to_winner', {
                'entry': position.entry_price,
                'price': current_price,
                'add_size': add_size,
                'new_total_size': position.size,
                'reason': exit_action.get('reason', 'ADD_POSITION')
            })
    except Exception as e:
        logger.error(f"Failed to add to winner: {e}")
    
    return True  # Stay in MANAGE


async def handle_full_close(
    engine: 'SignalEngine',
    exit_action: Dict[str, Any],
    position,
    current_price: float,
    market_time
) -> bool:
    """
    Execute FULL_CLOSE action.
    
    Steps:
    1. Notify reconciler (hard lock)
    2. Get real size from exchange
    3. Verify direction
    4. Execute close
    5. Verify position closed (emergency close if not)
    
    Returns:
        True always (full close completes the action handling)
    """
    from src.domain.trading.state.state_machine import BotState
    
    exit_reason = exit_action.get('reason', 'FULL_CLOSE')
    engine.reconciler.notify_exit_started(exit_reason)
    
    # Get real size from exchange
    close_size = position.size
    try:
        exchange_pos = await engine.execution.get_position(engine.symbol)
        if exchange_pos:
            exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
            if exchange_size > 0:
                # Verify direction matches
                exchange_side = exchange_pos.get('side', '').lower()
                expected_side = position.direction.lower()
                if exchange_side and exchange_side != expected_side:
                    logger.critical(f"⚠️ FULL_CLOSE ABORTED: Direction mismatch! Exchange={exchange_side}, Expected={expected_side}")
                    engine.reconciler.notify_exit_completed()
                    engine._reset_trade_state("Direction mismatch detected in FULL_CLOSE")
                    engine.state_machine.to_idle("Direction mismatch")
                    return True
                
                if abs(exchange_size - position.size) > 0.00001:
                    logger.warning(f"🔄 Size correction for FULL_CLOSE: local={position.size:.6f} → exchange={exchange_size:.6f}")
                close_size = exchange_size
            else:
                logger.warning("⚠️ No position on exchange for FULL_CLOSE - already closed externally")
                engine.reconciler.notify_exit_completed()
                engine._reset_trade_state("Position closed externally during FULL_CLOSE")
                engine.state_machine.to_idle("Position closed externally")
                return True
        else:
            logger.warning("⚠️ Could not get exchange position - using local size")
            close_size = float(exit_action.get('size', 0) or position.size)
    except Exception as e:
        logger.error(f"Error getting exchange size: {e} - using local size")
        close_size = float(exit_action.get('size', 0) or position.size)
    
    if close_size > 0:
        # Get exact size before close
        try:
            pre_close_pos = await engine.execution.get_position(engine.symbol)
            if pre_close_pos:
                exact_size = abs(float(pre_close_pos.get('contracts', 0) or 0))
                if exact_size > 0 and abs(exact_size - close_size) > 0.00001:
                    logger.info(f"🔄 Size adjustment before close: requested={close_size:.6f}, exchange={exact_size:.6f}")
                    close_size = exact_size
        except Exception as pre_e:
            logger.warning(f"Could not verify size before close: {pre_e}")
        
        await engine.execution.close_position_market(
            symbol=engine.symbol,
            side='sell' if position.direction == 'LONG' else 'buy',
            amount=close_size,
            reason="FULL_CLOSE"
        )
    
    # Check TP2/BE flags
    reason = exit_action.get('reason', '')
    if 'TP2' in reason or 'tp2' in reason:
        engine._reached_tp2 = True
    if 'BE' in reason or 'breakeven' in reason.lower():
        engine._is_be_stop = True
    
    # Verify position closed
    await asyncio.sleep(0.5)
    try:
        still_has_position = await engine.execution.has_open_position(engine.symbol)
        if still_has_position:
            logger.critical("⚠️ FULL_CLOSE executed but position still exists! Emergency close...")
            exchange_pos = await engine.execution.get_position(engine.symbol)
            if exchange_pos:
                remaining = abs(float(exchange_pos.get('contracts', 0) or 0))
                min_size = 0.00001
                if remaining > min_size:
                    await engine.execution.close_position_market(
                        symbol=engine.symbol,
                        side='sell' if position.direction == 'LONG' else 'buy',
                        amount=remaining,
                        reason="RESIDUAL"
                    )
                    logger.info(f"✅ Emergency closed remaining {remaining:.6f}")
                else:
                    logger.info(f"✓ Remaining size {remaining:.8f} below minimum - position effectively closed")
    except Exception as verify_e:
        logger.error(f"Error verifying close: {verify_e}")
    
    return True


async def _update_sl_after_size_change(
    engine: 'SignalEngine',
    position,
    new_size: float
) -> None:
    """Update SL order on exchange after position size changed (partial close)."""
    try:
        current_sl = engine.risk_engine.get_current_stop_loss()
        if current_sl and new_size > 0:
            logger.info(f"🔄 Updating SL order after partial: size={new_size:.6f}, SL={current_sl:.2f}")
            await engine.execution.update_stop_loss_on_exchange(
                symbol=engine.symbol,
                new_sl_price=current_sl,
                last_sent_sl=current_sl - 1,  # Force update
                atr=engine._last_momentum_atr or 100,
                spread=engine._last_spread_bps or 1,
                initial_risk=abs(position.entry_price - current_sl),
                direction=position.direction
            )
            logger.info("✅ SL order updated for new position size")
    except Exception as e:
        logger.error(f"Error updating SL after partial: {e}")


async def _replace_sl_tp_after_add(
    engine: 'SignalEngine',
    position,
    new_size: float,
    new_entry_price: float
) -> None:
    """Cancel and replace SL/TP orders after ADD_POSITION."""
    try:
        current_sl = engine.risk_engine.get_current_stop_loss()
        current_tp = getattr(engine, '_last_entry_data', {}).get('take_profit') if hasattr(engine, '_last_entry_data') else None
        
        if current_sl:
            # Cancel all existing orders
            logger.info("🔄 ADD TO WINNER: Cancelling old SL/TP to update size...")
            cancelled = await engine.execution.cancel_all_orders(engine.symbol)
            logger.info(f"✅ Cancelled {cancelled} orders")
            
            await asyncio.sleep(0.3)
            
            # Recalculate TP if needed
            if current_tp is None:
                if position.direction == 'SHORT':
                    risk_distance = current_sl - new_entry_price
                    current_tp = new_entry_price - (risk_distance * 2.0)
                else:
                    risk_distance = new_entry_price - current_sl
                    current_tp = new_entry_price + (risk_distance * 2.0)
            
            # Place new SL/TP
            stops_placed = await engine.execution.set_position_stops(
                symbol=engine.symbol,
                stop_loss=current_sl,
                take_profit=current_tp
            )
            
            if stops_placed:
                logger.info(f"✅ SL/TP REPLACED for ADD TO WINNER: SL={current_sl:.2f}, TP={current_tp:.2f}, Size={new_size:.6f}")
                if engine.risk_engine.position:
                    engine.risk_engine.position.mark_exchange_sl_active()
            else:
                logger.critical("❌ FAILED TO REPLACE SL/TP AFTER ADD - POSITION AT RISK!")
                if engine.risk_engine.position:
                    engine.risk_engine.position.mark_exchange_sl_inactive("Failed to replace after ADD")
    except Exception as e:
        logger.error(f"⚠️ Failed to update SL after ADD: {e}")
