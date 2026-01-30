"""
Entry Executor Module - Extracted from SignalEngine (AUDIT REFACTOR Point 5)

Created: Jan 27, 2026

This module handles the execution of entries after signal validation.
Extracted from _execute_entry method (~2200 lines → ~600 lines here + cleaner main code)

Contains:
- Pre-execution guards (concurrency, edge enforcement)
- Position size calculation
- Order execution
- Post-execution state management
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.application.live.trading_app import SignalEngine

from src.shared.indicators import calculate_atr, calculate_adx
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE
from src.config.trading_config import RiskManagement, OperatingMode

logger = logging.getLogger(__name__)


# =========================================================================
# DATA CLASSES
# =========================================================================

@dataclass
class ExecutionGuardResult:
    """Result of pre-execution guard checks."""
    can_execute: bool = True
    block_reason: str = ""
    needs_state_transition: bool = False
    transition_state: str = ""
    transition_reason: str = ""


@dataclass
class PositionSizeResult:
    """Result of position size calculation."""
    size: float = 0.0
    size_usd: float = 0.0
    multipliers_applied: Dict[str, float] = field(default_factory=dict)
    final_multiplier: float = 1.0
    min_size_ok: bool = True


@dataclass 
class EntryExecutionResult:
    """Result of entry execution."""
    success: bool = False
    order_id: Optional[str] = None
    fill_price: float = 0.0
    fill_size: float = 0.0
    error: str = ""


# =========================================================================
# PRE-EXECUTION GUARDS
# =========================================================================

class ExecutionGuards:
    """
    Pre-execution safety checks.
    
    Validates:
    1. No pending orders
    2. No open positions
    3. State machine in correct state
    4. Edge enforcement allows trade
    """
    
    @staticmethod
    async def check_all_guards(
        engine: 'SignalEngine',
        current_price: float
    ) -> ExecutionGuardResult:
        """Run all pre-execution guards."""
        result = ExecutionGuardResult()
        
        # 1. Check internal pending order
        if engine.pending_order_id:
            result.can_execute = False
            result.block_reason = f"Pending order {engine.pending_order_id} exists"
            result.needs_state_transition = True
            result.transition_state = "WAIT_FILL"
            result.transition_reason = "Recovered pending order state"
            logger.critical(f"⛔ EXECUTION ABORTED: {result.block_reason}")
            return result
        
        # 2. Check risk engine position
        if engine.risk_engine.has_open_position():
            result.can_execute = False
            result.block_reason = "Risk Engine reports open position"
            result.needs_state_transition = True
            result.transition_state = "MANAGE"
            result.transition_reason = "Recovered open position state"
            logger.critical(f"⛔ EXECUTION ABORTED: {result.block_reason}")
            return result
        
        # 3. Check execution engine open orders (exclude protective)
        if hasattr(engine.execution, 'open_orders') and engine.execution.open_orders:
            non_protective_orders = {
                oid: order for oid, order in engine.execution.open_orders.items()
                if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
            }
            
            if non_protective_orders:
                logger.critical(f"⛔ EXECUTION ABORTED: {len(non_protective_orders)} open entry orders")
                logger.info("🔄 Attempting order reconciliation...")
                
                await engine._reconcile_orders()
                
                # Re-check after reconciliation
                non_protective_orders_after = {
                    oid: order for oid, order in engine.execution.open_orders.items()
                    if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                }
                
                if non_protective_orders_after:
                    result.can_execute = False
                    result.block_reason = f"Still {len(non_protective_orders_after)} entry orders after reconciliation"
                    logger.critical(f"⛔ {result.block_reason} - aborting")
                    return result
                else:
                    logger.info("✅ Orders cleaned - proceeding with entry")
        
        # 4. Check State Machine state
        from src.domain.trading.state.state_machine import BotState
        if engine.state_machine.state != BotState.IDLE:
            result.can_execute = False
            result.block_reason = f"State is {engine.state_machine.state.name}, not IDLE"
            logger.critical(f"⛔ EXECUTION ABORTED: {result.block_reason}")
            return result
        
        return result
    
    @staticmethod
    async def check_edge_enforcement(
        engine: 'SignalEngine',
        current_price: float
    ) -> Tuple[bool, float, str]:
        """
        Check edge enforcement and get size multiplier.
        
        Returns:
            (can_trade, size_multiplier, block_reason)
        """
        await engine._update_edge_context()
        
        if engine._edge_context is None:
            return True, 1.0, ""
        
        current_drawdown = engine.risk_engine.get_drawdown_pct() / 100.0 if hasattr(engine.risk_engine, 'get_drawdown_pct') else 0.0
        
        can_trade, blockers = engine.edge_enforcement.validate_pre_trade_simple(
            context=engine._edge_context,
            current_drawdown=current_drawdown
        )
        
        if not can_trade:
            block_reason = f"Edge enforcement: {', '.join(blockers)}"
            logger.warning(f"⛔ EDGE ENFORCEMENT BLOCKED TRADE: {blockers}")
            
            # Track missed opportunity
            atr = engine._last_momentum_atr if hasattr(engine, '_last_momentum_atr') else 100.0
            engine.missed_tracker.log_blocked_signal(
                direction=engine.current_bias or 'UNKNOWN',
                price=current_price,
                block_reason=block_reason,
                block_category='edge_enforcement',
                score=engine._entry_score,
                atr=atr
            )
            return False, 0.0, block_reason
        
        size_multiplier = engine.edge_enforcement.get_position_size_multiplier()
        if size_multiplier < 1.0:
            logger.info(f"📉 Edge enforcement reducing position size to {size_multiplier*100:.0f}%")
        
        return True, size_multiplier, ""


# =========================================================================
# POSITION SIZE CALCULATOR
# =========================================================================

class PositionSizeCalculator:
    """
    Calculates position size with all multipliers.
    
    Multipliers:
    - Edge enforcement
    - Volatility regime
    - Session limits
    - Spread penalty
    - Anticipation probe
    - Loss streak
    """
    
    @staticmethod
    def calculate(
        engine: 'SignalEngine',
        current_price: float,
        stop_loss: float,
        edge_multiplier: float = 1.0
    ) -> PositionSizeResult:
        """Calculate final position size with all adjustments."""
        result = PositionSizeResult()
        result.multipliers_applied = {}
        
        # Base size from risk engine
        base_size = engine.risk_engine.calculate_position_size(current_price, stop_loss)
        result.size = base_size
        
        # 1. Edge enforcement multiplier
        result.multipliers_applied['edge'] = edge_multiplier
        
        # 2. Volatility regime multiplier
        vol_mult = 1.0
        if hasattr(engine, '_current_volatility_regime') and engine._current_volatility_regime:
            vol_mult = engine._current_volatility_regime.position_size_mult
            result.multipliers_applied['volatility'] = vol_mult
        
        # 3. Spread penalty multiplier
        spread_mult = 1.0
        if hasattr(engine, '_current_trade_tags') and engine._current_trade_tags.get('spread_penalty', 0) > 0:
            # Reduce size if spread elevated
            spread_mult = 0.8
            result.multipliers_applied['spread'] = spread_mult
        
        # 4. Anticipation probe multiplier
        antic_mult = 1.0
        if engine._current_trade_tags.get('anticipation_entry', False):
            antic_mult = engine._current_trade_tags.get('anticipation_probe_size', 0.30)
            result.multipliers_applied['anticipation'] = antic_mult
        
        # 5. Loss streak multiplier
        loss_mult = 1.0
        if hasattr(engine, '_consecutive_losses') and engine._consecutive_losses >= 2:
            loss_mult = max(0.5, 1.0 - (engine._consecutive_losses * 0.15))
            result.multipliers_applied['loss_streak'] = loss_mult
        
        # Calculate final multiplier
        result.final_multiplier = (
            edge_multiplier * vol_mult * spread_mult * antic_mult * loss_mult
        )
        
        # Apply multiplier
        result.size = base_size * result.final_multiplier
        result.size = round_size_to_exchange(result.size)
        result.size_usd = result.size * current_price
        
        # Check minimum size
        result.min_size_ok = result.size >= KRAKEN_BTC_MIN_SIZE
        
        if not result.min_size_ok:
            logger.warning(f"⚠️ Position size {result.size:.6f} below minimum {KRAKEN_BTC_MIN_SIZE}")
        
        return result


# =========================================================================
# ENTRY LOCATION GUARDS
# =========================================================================

class LocationGuards:
    """
    Location-based entry guards.
    
    Prevents entries in bad locations:
    - Too close to anchor levels (YTD high/low)
    - Too close to range boundaries in RANGE regime
    """
    
    MIN_HEADROOM_ATR = 0.75
    
    @staticmethod
    def check_anchor_levels(
        engine: 'SignalEngine',
        bias: str,
        current_price: float,
        atr: float
    ) -> Tuple[bool, str]:
        """
        Check if entry is too close to anchor levels.
        
        Returns:
            (can_enter, block_reason)
        """
        if not hasattr(engine, 'important_levels') or engine.important_levels is None:
            return True, ""
        
        try:
            anchor_levels = engine.important_levels.get_all_levels()
            if not anchor_levels:
                return True, ""
            
            regime = "RANGE"
            if hasattr(engine, '_location_context') and engine._location_context:
                regime = engine._location_context.regime.upper()
            
            # Only apply in non-trending regimes
            if regime not in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']:
                return True, ""
            
            if bias == 'LONG':
                # Check distance to nearest resistance above
                nearest_resistance = None
                for level in anchor_levels:
                    if level.price > current_price:
                        if nearest_resistance is None or level.price < nearest_resistance:
                            nearest_resistance = level.price
                
                if nearest_resistance is not None:
                    headroom = (nearest_resistance - current_price) / atr if atr > 0 else 0
                    if headroom < LocationGuards.MIN_HEADROOM_ATR:
                        return False, f"LONG with only {headroom:.2f} ATR to resistance @ ${nearest_resistance:.0f}"
            
            elif bias == 'SHORT':
                # Check distance to nearest support below
                nearest_support = None
                for level in anchor_levels:
                    if level.price < current_price:
                        if nearest_support is None or level.price > nearest_support:
                            nearest_support = level.price
                
                if nearest_support is not None:
                    headroom = (current_price - nearest_support) / atr if atr > 0 else 0
                    if headroom < LocationGuards.MIN_HEADROOM_ATR:
                        return False, f"SHORT with only {headroom:.2f} ATR to support @ ${nearest_support:.0f}"
            
            return True, ""
            
        except Exception as e:
            logger.debug(f"Anchor level check skipped: {e}")
            return True, ""
    
    @staticmethod
    def check_range_location(
        engine: 'SignalEngine',
        bias: str,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        Check if entry location is valid for range regime.
        
        Returns:
            (can_enter, block_reason)
        """
        if not hasattr(engine, '_location_context') or engine._location_context is None:
            return True, ""
        
        location_ctx = engine._location_context
        regime = location_ctx.regime.upper() if hasattr(location_ctx, 'regime') else "RANGE"
        
        if regime not in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']:
            return True, ""
        
        if bias == 'LONG':
            headroom_atr = location_ctx.headroom_to_high_atr
            if headroom_atr < LocationGuards.MIN_HEADROOM_ATR:
                return False, f"LONG in {regime} with only {headroom_atr:.2f} ATR headroom (need ≥0.75)"
        
        elif bias == 'SHORT':
            headroom_atr = location_ctx.headroom_to_low_atr
            if headroom_atr < LocationGuards.MIN_HEADROOM_ATR:
                return False, f"SHORT in {regime} with only {headroom_atr:.2f} ATR headroom (need ≥0.75)"
        
        return True, ""


# =========================================================================
# STOP LOSS CALCULATOR
# =========================================================================

class StopLossCalculator:
    """
    Calculates stop loss level with structure support.
    """
    
    @staticmethod
    def calculate(
        engine: 'SignalEngine',
        entry_price: float,
        bias: str,
        df_5m: pd.DataFrame,
        atr_1m: float
    ) -> Tuple[float, float]:
        """
        Calculate stop loss and take profit.
        
        Returns:
            (stop_loss, take_profit)
        """
        # Find support/resistance for SL calculation
        support_resistance = engine._find_support_resistance(bias, df_5m)
        
        # Calculate ATR ratio for dynamic sizing
        atr_ratio = None
        if len(df_5m) >= 14:
            atr_values = calculate_atr(
                df_5m['high'].to_numpy(), 
                df_5m['low'].to_numpy(), 
                df_5m['close'].to_numpy(), 
                14
            )
            if len(atr_values) >= 10:
                current_atr = atr_values[-1]
                avg_atr = sum(atr_values[-10:]) / 10
                if avg_atr > 0:
                    atr_ratio = current_atr / avg_atr
        
        # Calculate SL
        stop_loss = engine.risk_engine.calculate_stop_loss(
            entry_price,
            bias,
            support_resistance,
            atr_1m,
            atr_ratio=atr_ratio
        )
        
        # Calculate TP (based on risk multiple)
        risk = abs(entry_price - stop_loss)
        if bias == 'LONG':
            take_profit = entry_price + (risk * RiskManagement.TP1_RISK_MULTIPLE)
        else:
            take_profit = entry_price - (risk * RiskManagement.TP1_RISK_MULTIPLE)
        
        return stop_loss, take_profit


# =========================================================================
# EXECUTION HELPER
# =========================================================================

async def execute_entry_order(
    engine: 'SignalEngine',
    bias: str,
    size: float,
    current_price: float,
    stop_loss: float,
    take_profit: float
) -> EntryExecutionResult:
    """
    Execute the entry order on exchange.
    
    Returns:
        EntryExecutionResult with order details
    """
    result = EntryExecutionResult()
    
    side = 'buy' if bias == 'LONG' else 'sell'
    
    try:
        # PASO 2 — MAKER ONLY REAL (SIN EXCEPCIONES)
        # Entry is always LIMIT + post-only. If it doesn't fill, higher-level logic cancels.
        order_response = await engine.execution.place_limit_order(
            symbol=engine.symbol,
            side=side,
            amount=size,
            price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        
        if order_response:
            result.success = True
            result.order_id = order_response.get('id')
            result.fill_price = order_response.get('price', current_price)
            result.fill_size = order_response.get('filled', size)
            
            logger.info(f"✅ Order placed: {result.order_id} | {side} {result.fill_size:.6f} @ {result.fill_price:.2f}")
        else:
            result.error = "Order response was None"
            logger.error(f"❌ Order failed: {result.error}")
            
    except Exception as e:
        result.error = str(e)
        logger.error(f"❌ Order execution error: {e}")
    
    return result
