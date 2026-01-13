"""
Risk Engine for BTC Perpetual Trading System
Handles: Position sizing, TP1/TP2, Trailing stops, Exit management

NOTE: Position sizing is now centralized in core_utils.PositionSizer.
This module integrates with the centralized sizer for consistency.
"""

import logging
import asyncio
from datetime import datetime, UTC
from typing import Optional, Dict, Tuple
from monitoring.metrics import ORDERS_RECOMMENDED

from engine.config import (
    RiskManagement, OrderExecution,
    get_risk_for_session, SweepFilter, TimeBasedExits
)
from engine.core_utils import (
    ensure_utc, PositionSizer, get_position_sizer
)
from engine.state_types import PositionState

logger = logging.getLogger(__name__)


# REGIME CONSTANTS
REGIME_NORMAL = "NORMAL"    # Cortar basura, rascar wins, proteger DD
REGIME_RUNNER = "RUNNER"    # Dejar correr, solo time exit
RUNNER_PROMOTION_MFE = 1.5  # MFE >= 1.5R promociona a RUNNER (muy selectivo)
RUNNER_TIME_EXIT_CANDLES = 90  # N velas fijas para time exit (90min)


class Position:
    """Represents an open position with execution integrity invariants"""
    
    def __init__(self, symbol: str, direction: str, entry_price: float, 
                 size: float, stop_loss: float, atr_1m: float,
                 market_time: datetime = None):
        self.symbol = symbol
        self.direction = direction  # 'LONG' or 'SHORT'
        self.entry_price = entry_price
        self.size = size
        self.stop_loss = stop_loss
        self.initial_stop_loss = stop_loss
        
        # ===== EXECUTION INTEGRITY INVARIANTS (CAPA 1) =====
        self.system_side = direction.upper()  # What system intended: 'LONG' or 'SHORT'
        self.exchange_side: Optional[str] = None  # What exchange confirmed: 'LONG' or 'SHORT'
        self.expected_qty = size  # What we ordered
        self.exchange_qty: Optional[float] = None  # What exchange confirmed
        self.reduce_only_validated = False  # True once SL/TP confirmed as reduce-only
        self.integrity_validated = False  # True once all invariants pass
        self.integrity_errors: list = []  # List of integrity issues
        self.position_state = PositionState.PENDING  # Explicit position state tracking
        
        # ===== EXCHANGE SL TRACKING (CRITICAL FIX) =====
        # If True: SL order is on exchange → trust exchange to execute, DON'T close locally
        # If False: No SL on exchange → must use emergency market close when price hits SL
        self.exchange_sl_active = False  # Set True when set_position_stops() succeeds
        self.exchange_sl_order_id: Optional[str] = None  # Track the SL order ID
        
        # REGIME SYSTEM: Trade starts as NORMAL, promoted to RUNNER if MFE >= 0.8R
        self.regime = REGIME_NORMAL
        
        # Take profit management
        self.tp1_price = self._calculate_tp1(entry_price, stop_loss, direction)
        self.tp1_hit = False
        self.tp2_price: Optional[float] = None
        
        # Trailing stop
        self.trailing_stop = stop_loss
        self.atr_1m = atr_1m
        
        # Smart BE (Breakeven) management
        self.be_moved = False  # Track if we've moved to BE
        
        # BOS invalidation tracking
        self.sl_tightened = False  # Track if SL was tightened due to BOS
        
        # MFE/MAE tracking for stagnation exit
        self.highest_price = entry_price  # Track highest price reached (for LONG)
        self.lowest_price = entry_price   # Track lowest price reached (for SHORT)
        
        # Tracking - ALWAYS use market_time, never datetime.now()
        self.open_time = market_time if market_time else datetime.now(UTC)
        self.unrealized_pnl = 0.0
        
        # 2-PHASE STOP: Track stop phase
        self.stop_phase = 1  # 1 = Exploration (0.7R), 2 = Validated (0.15R)
        self.original_stop_distance = abs(entry_price - stop_loss)
        
        # DYNAMIC SIZING: Track if we've added to winner
        self.added_to_winner = False
        self.initial_size = size  # Track original size before adds
        self.touched_minus_02r = False  # Track if price went -0.2R against
        
        # Candle counter for mediocre trade detection
        self.candle_count = 0
        
        logger.info(f"Position opened: {direction} {size} {symbol} @ {entry_price:.2f} | SL: {stop_loss:.2f} | TP1: {self.tp1_price:.2f}")
    
    def calculate_r_realized(self, exit_price: float) -> float:
        """
        Calculate realized R multiple for this trade.
        R = PnL / Initial Risk
        """
        initial_risk_per_unit = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk_per_unit == 0:
            return 0.0
        
        if self.direction == 'LONG':
            price_move = exit_price - self.entry_price
        else:
            price_move = self.entry_price - exit_price
        
        return price_move / initial_risk_per_unit
    
    def calculate_mfe_r(self) -> float:
        """Calculate Maximum Favorable Excursion in R."""
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk == 0:
            return 0.0
        
        if self.direction == 'LONG':
            mfe = self.highest_price - self.entry_price
        else:
            mfe = self.entry_price - self.lowest_price
        
        return mfe / initial_risk
    
    def calculate_mae_r(self) -> float:
        """Calculate Maximum Adverse Excursion in R."""
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk == 0:
            return 0.0
        
        if self.direction == 'LONG':
            mae = self.entry_price - self.lowest_price
        else:
            mae = self.highest_price - self.entry_price
        
        return mae / initial_risk
    
    def _calculate_tp1(self, entry: float, stop: float, direction: str) -> float:
        """Calculate TP1 at +1R"""
        risk = abs(entry - stop)
        if direction == 'LONG':
            return entry + (risk * RiskManagement.TP1_RISK_MULTIPLE)
        else:
            return entry - (risk * RiskManagement.TP1_RISK_MULTIPLE)
    
    def validate_integrity(self, exchange_side: str, exchange_qty: float, 
                          qty_tolerance: float = 0.02) -> bool:
        """
        CAPA 1: Validate position integrity against exchange data.
        
        Args:
            exchange_side: Side reported by exchange ('long' or 'short')
            exchange_qty: Quantity reported by exchange
            qty_tolerance: Allowed difference (default 2%)
        
        Returns:
            True if all invariants pass, False if any fails.
        """
        self.integrity_errors = []
        self.exchange_side = exchange_side.upper() if exchange_side else None
        self.exchange_qty = exchange_qty
        
        # Check 1: Side consistency
        if self.exchange_side and self.system_side != self.exchange_side:
            error = f"SIDE MISMATCH: system={self.system_side} vs exchange={self.exchange_side}"
            self.integrity_errors.append(error)
            logger.error(f"🚨 INTEGRITY: {error}")
            self.position_state = PositionState.INCONSISTENT
        
        # Check 2: Quantity consistency (within tolerance)
        is_partial = False
        # 🔥 AUDIT FIX: Add check for expected_qty > 0 to prevent division by zero
        if exchange_qty and self.expected_qty and self.expected_qty > 0:
            qty_diff = abs(exchange_qty - self.expected_qty) / self.expected_qty
            if qty_diff > qty_tolerance:
                error = f"QTY MISMATCH: expected={self.expected_qty:.8f} vs exchange={exchange_qty:.8f} (diff={qty_diff*100:.2f}%)"
                self.integrity_errors.append(error)
                logger.error(f"🚨 INTEGRITY: {error}")
                # Adjust internal size to match exchange reality
                logger.warning(f"⚠️ Adjusting position size from {self.size} to {exchange_qty}")
                self.size = exchange_qty
                is_partial = True
        
        # Check 3: SL/TP would reduce position (validated later)
        # This is set after SL/TP are successfully placed
        
        self.integrity_validated = len(self.integrity_errors) == 0
        
        # Update position state based on validation result
        if self.integrity_validated:
            self.position_state = PositionState.OPEN
            logger.info(f"✅ INTEGRITY: All invariants passed | side={self.system_side} qty={self.size:.8f}")
        elif is_partial and self.position_state != PositionState.INCONSISTENT:
            self.position_state = PositionState.PARTIAL
            logger.warning(f"⚠️ INTEGRITY: Partial fill detected, adjusting tracking")
        else:
            logger.critical(f"❌ INTEGRITY: {len(self.integrity_errors)} errors - SL/TP placement blocked")
        
        return self.integrity_validated
    
    def mark_reduce_only_validated(self):
        """Mark that SL/TP have been confirmed as reduce-only orders."""
        self.reduce_only_validated = True
        logger.info("✅ INTEGRITY: SL/TP confirmed as reduce-only")
    
    def mark_exchange_sl_active(self, order_id: str = None):
        """
        Mark that SL is ACTIVE on the exchange.
        
        When True: Trust exchange to execute SL, DON'T close locally.
        Called after set_position_stops() succeeds.
        """
        self.exchange_sl_active = True
        self.exchange_sl_order_id = order_id
        logger.info(f"✅ EXCHANGE SL ACTIVE: Trust exchange to execute SL (order: {order_id[:8] if order_id else 'N/A'})")
    
    def mark_exchange_sl_inactive(self, reason: str = "SL order failed"):
        """
        Mark that SL is NOT on the exchange.
        
        When False: Must use emergency market close when price hits SL.
        """
        self.exchange_sl_active = False
        self.exchange_sl_order_id = None
        logger.warning(f"⚠️ EXCHANGE SL INACTIVE: {reason} - will use emergency close")
    
    def update_tp2(self, tp2_price: float):
        """Set TP2 target"""
        self.tp2_price = tp2_price
        logger.info(f"TP2 set at {tp2_price:.2f} for {self.symbol}")
    
    def update_unrealized_pnl(self, current_price: float):
        """Update unrealized PnL and track MFE/MAE (highest/lowest price)"""
        if self.direction == 'LONG':
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
            # Track MFE (highest) and MAE (lowest) for LONG
            if current_price > self.highest_price:
                self.highest_price = current_price
            if current_price < self.lowest_price:
                self.lowest_price = current_price
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size
            # Track MFE (lowest) and MAE (highest) for SHORT
            if current_price < self.lowest_price:
                self.lowest_price = current_price
            if current_price > self.highest_price:
                self.highest_price = current_price
    
    def update_trailing_stop(self, current_price: float, atr_1m: float, market_time: datetime):
        """
        STEPPED TRAILING - Wide steps, let winners breathe!
        
        REGIME-AWARE:
        - NORMAL: Aplica trailing stepped (cut losses, capture small wins)
        - RUNNER: NO trailing fino - solo SL ampliado ya fijado
        
        Steps for NORMAL regime (based on MFE reached, not current price):
        - MFE >= 0.8R → stop = entry (breakeven) - FASE 1: activation threshold
        - MFE >= 1.0R → stop = +0.4R  
        - MFE >= 1.5R → stop = +0.9R
        - MFE >= 2.2R → stop = +1.6R
        
        NO ATR dynamic trailing. Just discrete levels.
        """
        # RUNNER regime: NO trailing fino, dejar correr
        if self.regime == REGIME_RUNNER:
            return  # SL ya fue fijado en promoción a RUNNER
        if market_time is None:
            market_time = self.open_time
        
        market_time = ensure_utc(market_time)
        self.atr_1m = atr_1m
        
        # Calculate MFE in R
        risk = abs(self.entry_price - self.initial_stop_loss)
        if risk <= 0:
            return
        
        mfe_r = self.calculate_mfe_r()
        
        # STEPPED TRAILING LEVELS (based on MFE reached, not current price)
        # Once MFE hits a level, lock in that stop - don't give it back
        
        if mfe_r >= 2.2:
            # Lock in +1.6R (new level for big runners)
            if self.direction == 'LONG':
                new_stop = self.entry_price + (risk * 1.6)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.entry_price - (risk * 1.6)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 4: MFE {mfe_r:.2f}R >= 2.2R → Lock +1.6R")
        
        elif mfe_r >= 1.5:
            # Lock in +0.9R
            if self.direction == 'LONG':
                new_stop = self.entry_price + (risk * 0.9)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.entry_price - (risk * 0.9)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 3: MFE {mfe_r:.2f}R >= 1.5R → Lock +0.9R")
        
        elif mfe_r >= 1.0:
            # Lock in +0.4R
            if self.direction == 'LONG':
                new_stop = self.entry_price + (risk * 0.4)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.entry_price - (risk * 0.4)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 2: MFE {mfe_r:.2f}R >= 1.0R → Lock +0.4R")
        
        elif mfe_r >= 0.8:
            # FASE 1: Trailing only activates at 0.8R MFE (lock in breakeven)
            if self.direction == 'LONG':
                self.trailing_stop = max(self.trailing_stop, self.entry_price)
            else:
                self.trailing_stop = min(self.trailing_stop, self.entry_price)
            logger.debug(f"📈 TRAILING STEP 1: MFE {mfe_r:.2f}R >= 0.8R → Lock BE (FASE 1 threshold)")
    
    def increment_candle_count(self):
        """Increment candle count for mediocre trade detection"""
        self.candle_count += 1
    
    def check_regime_promotion(self) -> bool:
        """
        PASO 3 - PROMOCIÓN A RUNNER
        
        Si MFE >= 1.2R → regime = RUNNER
        Sin indicadores nuevos. Sin excepciones.
        
        Al promocionar: El SL del RUNNER es el MEJOR entre:
        - El trailing stop actual (ya movido por stepped trailing)
        - BE (breakeven)
        
        NO bajamos el SL si el trailing ya lo subió.
        
        Returns: True if just promoted to RUNNER
        """
        if self.regime == REGIME_RUNNER:
            return False  # Ya es RUNNER
        
        mfe_r = self.calculate_mfe_r()
        
        if mfe_r >= RUNNER_PROMOTION_MFE:
            self.regime = REGIME_RUNNER
            
            # RUNNER SL = el MEJOR entre trailing actual y BE
            # NO reducir el SL si trailing ya lo subió
            if self.direction == 'LONG':
                # Para LONG, mayor = mejor
                runner_stop = max(self.trailing_stop, self.entry_price)
                self.stop_loss = runner_stop
                self.trailing_stop = runner_stop
            else:
                # Para SHORT, menor = mejor
                runner_stop = min(self.trailing_stop, self.entry_price)
                self.stop_loss = runner_stop
                self.trailing_stop = runner_stop
            
            logger.info(f"🏃 REGIME PROMOTED TO RUNNER: MFE={mfe_r:.2f}R >= {RUNNER_PROMOTION_MFE}R | SL locked at: {self.stop_loss:.2f}")
            return True
        
        return False
    
    def check_runner_time_exit(self) -> bool:
        """
        PASO 6 - TIME_BASED_EXIT para RUNNERS
        
        Condición: regime == RUNNER AND candle_count >= N
        N = constante fija (45 velas = 45 min en 1m chart)
        MISMO N para longs, shorts, todos activos.
        
        Returns: True if should exit via time
        """
        if self.regime != REGIME_RUNNER:
            return False
        
        if self.candle_count >= RUNNER_TIME_EXIT_CANDLES:
            logger.info(f"⏱️ RUNNER TIME EXIT: {self.candle_count} candles >= {RUNNER_TIME_EXIT_CANDLES} | Regime=RUNNER")
            return True
        
        return False
    
    def check_mediocre_trade_exit(self, n_candles: int = 6, mfe_threshold: float = 0.3,
                                     adx: float = None, atr_ratio: float = None) -> bool:
        """
        KILL SWITCH: Exit stagnant trades early (TIME_INVALIDATION)
        
        HYBRID LOGIC (Jan 9, 2026):
        Close ONLY if ALL conditions are met:
        1. candles >= n_candles (6)
        2. MFE < threshold_R (0.3R) - trade hasn't proven direction
        3. ADX < 20 - market is NOT trending (optional, defaults to True if not provided)
        4. ATR ratio < 0.8 - volatility is compressed (optional, defaults to True if not provided)
        
        This avoids:
        - Killing slow but directional trades in trending markets
        - Staying stuck in dead ranges with no volatility
        
        Args:
            n_candles: Number of candles before checking (default 6)
            mfe_threshold: Minimum MFE in R required (default 0.3R)
            adx: Current ADX value (if None, condition is considered met)
            atr_ratio: Current ATR / Average ATR (if None, condition is considered met)
        
        Returns: True if trade should be killed
        """
        if self.candle_count < n_candles:
            return False
        
        mfe_r = self.calculate_mfe_r()
        
        # Condition 1: MFE too low (didn't prove direction)
        if mfe_r >= mfe_threshold:
            return False  # Trade is proving itself, don't kill
        
        # Condition 2: ADX check (skip if market is trending)
        ADX_THRESHOLD = 20
        if adx is not None and adx >= ADX_THRESHOLD:
            # Market is trending - give trade more time
            logger.debug(f"📊 TIME_INVALIDATION skipped: ADX={adx:.1f} >= {ADX_THRESHOLD} (trending market)")
            return False
        
        # Condition 3: Volatility check (skip if volatility is expanding)
        ATR_RATIO_THRESHOLD = 0.8  # Below 80% of average = compressed
        if atr_ratio is not None and atr_ratio >= ATR_RATIO_THRESHOLD:
            # Volatility is normal/expanding - trade might move soon
            logger.debug(f"📊 TIME_INVALIDATION skipped: ATR ratio={atr_ratio:.2f} >= {ATR_RATIO_THRESHOLD} (volatility OK)")
            return False
        
        # ALL conditions met: market is dead/lateral, kill the trade
        adx_str = f"ADX={adx:.1f}" if adx else "ADX=N/A"
        atr_str = f"ATR_ratio={atr_ratio:.2f}" if atr_ratio else "ATR=N/A"
        logger.warning(f"☠️ TIME_INVALIDATION: {self.candle_count} velas, MFE={mfe_r:.2f}R < {mfe_threshold}R | {adx_str} | {atr_str}")
        return True
    
    def check_add_to_winner(self, current_price: float) -> dict:
        """
        DYNAMIC SIZING: Add 0.5x position when trade proves itself
        
        Condition:
          - MFE >= 0.6R
          - Never touched -0.2R (clean move)
        
        This increases R on clear winners without increasing drawdown.
        
        Returns: dict with add_size if should add, None otherwise
        """
        if self.added_to_winner:
            return None  # Already added
        
        mfe_r = self.calculate_mfe_r()
        
        # Track if we've been -0.2R against
        risk = abs(self.entry_price - self.initial_stop_loss)
        if risk > 0:
            if self.direction == 'LONG':
                worst_drawdown = (self.entry_price - self.lowest_price) / risk
            else:
                worst_drawdown = (self.highest_price - self.entry_price) / risk
            
            if worst_drawdown >= 0.2:
                self.touched_minus_02r = True
        
        # Check conditions
        if mfe_r >= 0.6 and not self.touched_minus_02r:
            add_size = self.initial_size * 0.5
            self.added_to_winner = True
            logger.info(f"💪 ADD TO WINNER: MFE {mfe_r:.2f}R >= 0.6R, no -0.2R touch → Adding {add_size:.6f}")
            return {
                'action': 'ADD_POSITION',
                'add_size': add_size,
                'new_total_size': self.size + add_size,
                'reason': f'Winner add at MFE {mfe_r:.2f}R'
            }
        
        return None
    
    def move_stop_to_breakeven(self, reason: str = "Structure break"):
        """
        Move stop to breakeven + fees
        
        SMART IMPLEMENTATION (Opción B):
        Solo mueve SL a BE cuando:
        1. TP1 ya alcanzado (estamos en profit)
        2. Estructura rompe: EMA20 perdida o BOS en contra
        
        Esto reduce salidas prematuras y aumenta profit factor.
        """
        if self.be_moved:
            return  # Ya movido, no hacer nada
        
        fee_buffer = self.entry_price * 0.001  # 0.1% buffer for fees
        
        if self.direction == 'LONG':
            self.stop_loss = self.entry_price + fee_buffer
            self.trailing_stop = self.stop_loss
        else:
            self.stop_loss = self.entry_price - fee_buffer
            self.trailing_stop = self.stop_loss
        
        self.be_moved = True
        logger.info(f"✅ SMART BE: Stop moved to BE + fees: {self.stop_loss:.2f} | Reason: {reason}")
    
    def tighten_stop_loss(self, tighten_pct: float = 0.5):
        """
        Tighten stop loss when thesis is invalidated (BOS before TP1)
        
        Args:
            tighten_pct: Percentage to tighten (0.5 = move SL 50% closer to entry)
        
        Example:
            Entry: 100, SL: 95, Current: 102
            Distance: 5
            New SL: 100 - (5 * 0.5) = 97.5
        """
        if self.sl_tightened:
            return  # Ya ajustado, no hacer nada
        
        current_distance = abs(self.entry_price - self.stop_loss)
        new_distance = current_distance * tighten_pct
        
        if self.direction == 'LONG':
            new_sl = self.entry_price - new_distance
            self.stop_loss = max(self.stop_loss, new_sl)  # Solo mover hacia arriba
        else:
            new_sl = self.entry_price + new_distance
            self.stop_loss = min(self.stop_loss, new_sl)  # Solo mover hacia abajo
        
        self.trailing_stop = self.stop_loss
        self.sl_tightened = True
        
        logger.warning(f"⚠️ BOS INVALIDATION: SL tightened to {self.stop_loss:.2f} ({tighten_pct*100:.0f}% closer)")
    
    def check_stop_hit(self, current_price: float, current_low: float = None, 
                       atr_1m: float = None, market_time: datetime = None) -> bool:
        """
        Check if stop loss was hit and MANUAL CLOSE is needed.
        
        CRITICAL FIX (Jan 12, 2026):
        - If exchange_sl_active = True: Return False ALWAYS
          (trust exchange to execute SL, don't close locally)
        - If exchange_sl_active = False: Check price and return True if hit
          (must use emergency market close)
        
        Args:
            current_price: Current price
            current_low: Candle low (optional, for sweep detection)
            atr_1m: ATR (optional, for sweep detection)
            market_time: Current market time
        
        Returns: True if SL hit AND we need to close manually (no exchange SL)
        """
        # 🔥 CRITICAL: If exchange has the SL, DON'T trigger local close
        # The exchange will execute the SL and we'll detect position_size=0
        if self.exchange_sl_active:
            # Check if price hit SL for LOGGING only (not action)
            if self.direction == 'LONG':
                sl_touched = current_price <= self.stop_loss
            else:
                sl_touched = current_price >= self.stop_loss
            
            if sl_touched:
                logger.debug(f"📊 SL price touched @ {current_price:.2f} - WAITING for exchange to execute (SL active)")
            return False  # NEVER trigger local close when exchange has SL
        
        # No exchange SL - we must manage locally
        # Check if current price hits stop
        if self.direction == 'LONG':
            sl_hit = current_price <= self.stop_loss
        else:
            sl_hit = current_price >= self.stop_loss
        
        if not sl_hit:
            return False
        
        logger.warning(f"🚨 SL HIT @ {current_price:.2f} - NO exchange SL active, will use emergency close")
        
        # GRACE PERIOD: First 10 minutes, only exit on EXTREME moves
        if market_time is not None:
            duration_minutes = (market_time - self.open_time).total_seconds() / 60
            if duration_minutes < 10:
                # In grace period - check if it's an extreme move
                sl_distance = abs(self.entry_price - self.stop_loss)
                current_adverse_move = abs(current_price - self.entry_price)
                
                # Only exit if move is > 1.5x the SL distance (catastrophic)
                if current_adverse_move < sl_distance * 1.5:
                    logger.debug(f"⏸️ GRACE PERIOD: {duration_minutes:.1f}min, adverse move {current_adverse_move:.2f} < 1.5x SL {sl_distance*1.5:.2f} - holding")
                    return False
                else:
                    logger.warning(f"🚨 GRACE PERIOD OVERRIDE: Extreme move {current_adverse_move:.2f} > 1.5x SL - executing")
        
        # FIX #8: Validate it's not a sweep if parameters provided
        if current_low is not None and atr_1m is not None and SweepFilter.ENABLED:
            is_sweep = self._is_sweep_detected(current_low, atr_1m)
            if is_sweep:
                logger.warning(f"⚠️  SWEEP DETECTED: Low {current_low:.2f}, SL {self.stop_loss:.2f} - NOT executing")
                return False
        
        return True
    
    def _is_sweep_detected(self, current_low: float, atr_1m: float) -> bool:
        """
        FIX #8: Detect if SL hit is a liquidation sweep, not a real break
        
        Sweep: Wick below SL but small enough to indicate liquidation hunting
        Real break: Extended move below SL
        """
        if self.direction != 'LONG':
            return False
        
        wick_below_sl = self.stop_loss - current_low
        max_sweep_depth = atr_1m * SweepFilter.MAX_SWEEP_DEPTH_ATR
        
        if wick_below_sl < max_sweep_depth:
            logger.warning(f"   Wick depth {wick_below_sl:.2f} < {max_sweep_depth:.2f} (sweep threshold)")
            return True
        
        return False
    
    def check_trailing_stop_hit(self, current_price: float) -> bool:
        """
        Check if trailing stop hit.
        
        Works before AND after TP1 - the trailing_stop is updated by
        update_trailing_stop() based on MFE levels.
        
        If trailing_stop has been moved from initial stop, check it.
        FASE 1: Trailing only activates at MFE >= 0.8R
        """
        # Check if trailing has moved (means MFE reached at least 0.8R per FASE 1)
        initial_stop = self.initial_stop_loss
        trailing_moved = abs(self.trailing_stop - initial_stop) > 0.01  # Small tolerance
        
        if not trailing_moved:
            return False  # Trailing not active yet
        
        if self.direction == 'LONG':
            return current_price <= self.trailing_stop
        else:
            return current_price >= self.trailing_stop
    
    def check_tp1_hit(self, current_price: float) -> bool:
        """Check if TP1 reached"""
        if self.tp1_hit:
            return False
        
        if self.direction == 'LONG':
            return current_price >= self.tp1_price
        else:
            return current_price <= self.tp1_price
    
    def check_tp2_hit(self, current_price: float) -> bool:
        """Check if TP2 reached"""
        if not self.tp2_price or not self.tp1_hit:
            return False
        
        if self.direction == 'LONG':
            return current_price >= self.tp2_price
        else:
            return current_price <= self.tp2_price
    
    def check_structure_break(self, current_price: float, ema20_5m: float, 
                             has_bos_against: bool = False) -> tuple[bool, str]:
        """
        Check if market structure breaks (señal para mover SL a BE)
        
        Returns: (should_move_be, reason)
        
        Condiciones para mover a BE:
        1. TP1 ya alcanzado (estamos en profit)
        2. Y ALGUNA de:
           - Precio pierde EMA20 (5m)
           - BOS en contra detectado
        """
        if not self.tp1_hit:
            return False, "TP1 not hit yet"
        
        if self.be_moved:
            return False, "BE already moved"
        
        # Check EMA20 break
        if self.direction == 'LONG':
            ema20_broken = current_price < ema20_5m
        else:
            ema20_broken = current_price > ema20_5m
        
        # Determinar razón
        if has_bos_against:
            return True, "BOS contra tendencia"
        elif ema20_broken:
            return True, f"Precio perdió EMA20 ({ema20_5m:.2f})"
        
        return False, "Estructura intacta"
    
    def get_time_in_trade_minutes(self, market_time: datetime = None) -> float:
        """
        Get time in trade in minutes.
        
        Args:
            market_time: Current market time (REQUIRED - never use datetime.now())
        """
        current_time = market_time if market_time else self.open_time
        return (current_time - self.open_time).total_seconds() / 60
    
    def should_force_exit_by_time(self, market_time: datetime = None, current_atr: float = None) -> tuple[bool, str]:
        """
        REDESIGNED (Jan 4, 2026): Stagnation-based exit, NOT clock-based.
        
        Exit if trade is STAGNATING (no progress + vol dying), not by time alone.
        This prevents cutting winners just because clock hit X minutes.
        
        Exit conditions:
        1. STAGNATION: After 60min, less than 0.35R progress + ATR contracting
        2. ABSOLUTE MAX: 180min (safety net only)
        3. DISABLED: Overnight exit (BTC trades 24/7)
        
        Args:
            market_time: Current market time
            current_atr: Current ATR value (for contraction check)
        
        Returns: (should_exit: bool, reason: str)
        """
        if not TimeBasedExits.ENABLED:
            return False, ""
        
        if market_time is None:
            return False, ""
        
        hold_minutes = self.get_time_in_trade_minutes(market_time)
        
        # Check 1: STAGNATION EXIT (primary logic)
        if getattr(TimeBasedExits, 'STAGNATION_EXIT_ENABLED', False) and hold_minutes >= getattr(TimeBasedExits, 'STAGNATION_CHECK_MINUTES', 60):
            # Calculate current progress in R
            risk_amount = abs(self.entry_price - self.initial_stop_loss)
            if risk_amount > 0:
                if self.direction == 'LONG':
                    current_progress = (self.highest_price - self.entry_price) / risk_amount if hasattr(self, 'highest_price') else 0
                else:
                    current_progress = (self.entry_price - self.lowest_price) / risk_amount if hasattr(self, 'lowest_price') else 0
                
                min_progress = getattr(TimeBasedExits, 'MIN_PROGRESS_R', 0.35)
                
                # Check if progress is below minimum
                if current_progress < min_progress:
                    # Additional checks: ATR contraction
                    should_exit = True
                    reasons = []
                    
                    # Check ATR contraction (if current_atr provided)
                    if getattr(TimeBasedExits, 'REQUIRE_ATR_CONTRACTION', True) and current_atr is not None:
                        atr_ratio = current_atr / self.atr_1m if self.atr_1m > 0 else 1.0
                        contraction_threshold = getattr(TimeBasedExits, 'ATR_CONTRACTION_RATIO', 0.7)
                        if atr_ratio >= contraction_threshold:
                            should_exit = False  # ATR not contracting, don't exit
                        else:
                            reasons.append(f"ATR contracted to {atr_ratio:.0%}")
                    
                    if should_exit:
                        reasons.insert(0, f"Stagnation: {current_progress:.2f}R progress in {hold_minutes:.0f}min")
                        return True, " + ".join(reasons)
        
        # Check 2: ABSOLUTE MAX (safety net - much longer than before)
        if hold_minutes > TimeBasedExits.MAX_HOLD_MINUTES:
            return True, f"Safety max hold exceeded ({hold_minutes:.0f}min > {TimeBasedExits.MAX_HOLD_MINUTES}min)"
        
        # Check 3: OVERNIGHT - respects config but disabled by default for BTC
        if getattr(TimeBasedExits, 'AVOID_OVERNIGHT', False):
            current_hour = market_time.hour
            night_hour = getattr(TimeBasedExits, 'NIGHT_EXIT_HOUR', 22)
            if current_hour >= night_hour:
                return True, f"Night exit time ({current_hour}:00 >= {night_hour}:00 UTC)"
        
        return False, ""


class RiskEngine:
    """
    Manages position sizing, risk limits, and exit management
    """
    
    def __init__(self, capital: float = 0.0):
        self.capital = capital  # Will be updated from exchange balance
        
        # Equity tracking
        self.initial_equity = capital if capital > 0 else 10000.0  # Default to 10k if not set
        self.equity = self.initial_equity
        self.max_equity = self.initial_equity
        self.max_drawdown_pct = 0.0
        
        # Position tracking
        self.position: Optional[Position] = None
        
        # Performance tracking
        self.total_pnl = 0.0
        self.winning_trades = []
        self.losing_trades = []
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    def create_position(self, symbol: str, direction: str, entry_price: float,
                       position_size: float, stop_loss: float, atr_1m: float,
                       market_time: datetime = None) -> Position:
        """Create a new position object for tracking"""
        self.position = Position(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            size=position_size,
            stop_loss=stop_loss,
            atr_1m=atr_1m,
            market_time=market_time
        )
        return self.position
    
    async def update_capital_from_balance(self, balance_dict: Dict) -> bool:
        """Update capital from exchange balance"""
        try:
            if not balance_dict or not isinstance(balance_dict, dict):
                logger.error("Invalid balance_dict provided")
                return False
            
            # CCXT returns structure: {'free': {...}, 'used': {...}, 'total': {...}}
            totals = balance_dict.get('total', {})
            free_balances = balance_dict.get('free', {})
            used_balances = balance_dict.get('used', {})
            
            if not totals:
                logger.warning("No 'total' key in balance_dict")
                return False
            
            # Try to get USDT balance (most common for futures)
            for currency in ['USDT', 'USD', 'EUR']:
                if currency in totals and totals[currency] > 0:
                    total = float(totals[currency])
                    free = float(free_balances.get(currency, 0.0))
                    used = float(used_balances.get(currency, 0.0))
                    
                    self.capital = total
                    
                    # =====================================================================
                    # 🔒 FIX (Jan 13, 2026): SYNC EQUITY FROM EXCHANGE IN LIVE MODE
                    # Initial equity must be set from real exchange balance, not default 10k
                    # This ensures DD calculations and PnL tracking use real values
                    # =====================================================================
                    if self.initial_equity == 10000.0 or self.equity == 10000.0:
                        # First time update - sync all equity values to real balance
                        logger.info(f"💰 LIVE: Syncing equity from exchange (was default 10k)")
                        self.initial_equity = total
                        self.equity = total
                        self.max_equity = total
                    
                    logger.info(f"💰 Capital actualizado: ${self.capital:.2f} {currency} (Libre: ${free:.2f}, En uso: ${used:.2f})")
                    return True
            
            # Log available currencies if we didn't find USDT/USD/EUR
            available = [f"{k}:{v:.6f}" for k, v in totals.items() if v > 0]
            logger.warning(f"No USDT/USD/EUR balance found. Available: {available}")
            return False
            
        except Exception as e:
            logger.error(f"Error updating capital from balance: {e}")
            return False
    
    def calculate_position_size(self, entry_price: float, stop_loss: float, 
                                hour: int, minute: int, leverage: int = 10) -> float:
        """
        Calculate BASE position size (without multipliers).
        
        DEPRECATED: For full sizing with state/edge/defensive multipliers,
        use PositionSizer.calculate_position_size() from core_utils.
        
        This method is kept for backwards compatibility but delegates
        to the centralized PositionSizer for the base calculation.
        
        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            hour: Current hour (for session-based risk - legacy)
            minute: Current minute (for session-based risk - legacy)
            leverage: Leverage to use
        
        Returns:
            Base position size in base currency (without multipliers)
        """
        # Get risk for current session (legacy session-based risk)
        risk_decimal = get_risk_for_session(hour, minute)
        
        # Use centralized PositionSizer for base calculation
        sizer = get_position_sizer(
            risk_pct=risk_decimal,
            max_position_pct=0.10,
            leverage=leverage
        )
        
        base_size = sizer.calculate_base_size(
            capital=self.capital,
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_pct=risk_decimal,
            leverage=leverage
        )
        
        # Check minimum size
        min_size_units = OrderExecution.MIN_POSITION_SIZE_USD / entry_price
        if base_size < min_size_units:
            logger.warning(f"Position size {base_size:.6f} below minimum {min_size_units:.6f}")
            return 0.0
        
        logger.info(f"Position sizing (base): Capital ${self.capital:.2f} × Risk {risk_decimal*100:.2f}% = {base_size:.6f} BTC")
        
        return base_size
    
    def calculate_stop_loss(self, entry_price: float, direction: str, 
                           support_resistance: float, atr_1m: float,
                           atr_ratio: float = None,
                           regime_atr_mult: float = None) -> float:
        """
        Calculate technical stop loss with ATR buffer
        support_resistance: HL for LONG, LH for SHORT
        
        EDGE FIX: Use minimum 1.2 ATR distance to avoid noise stops
        The SL is the FURTHER of: structure level OR ATR minimum
        
        CAPA 2: SL is CAPPED at max_atr_multiple to avoid false RR
        - SL cannot exceed 4.0 ATR from entry (prevents fake high RR trades)
        
        REGIME-AWARE SL (Jan 12, 2026):
        - RANGE: tighter SL (1.5 ATR) - quick scalps to value
        - TREND: wider SL (2.5 ATR) - let winners run
        - Volatility adjustment still applies ON TOP of regime base
        
        DYNAMIC SL (Jan 9, 2026) - NOW RELATIVE TO REGIME:
        - If ATR ratio > 1.2 (high volatility): +0.5 ATR to avoid barridas
        - If ATR ratio < 0.8 (low volatility): -0.5 ATR to protect capital
        """
        buffer = RiskManagement.STOP_LOSS_ATR_BUFFER * atr_1m
        
        # 🔥 BASE ATR multiplier from regime (or default 2.5)
        base_atr_multiplier = regime_atr_mult if regime_atr_mult is not None else 2.5
        
        # Volatility adjustment ON TOP of regime base
        if atr_ratio is not None:
            if atr_ratio > 1.2:
                # High volatility - add room to avoid stops
                atr_multiplier = base_atr_multiplier + 0.5
                logger.debug(f"📈 High volatility (ATR ratio {atr_ratio:.2f}) → SL @ {atr_multiplier:.1f} ATR")
            elif atr_ratio < 0.8:
                # Low volatility - tighter stops ok
                atr_multiplier = max(1.0, base_atr_multiplier - 0.5)  # Never below 1.0
                logger.debug(f"📉 Low volatility (ATR ratio {atr_ratio:.2f}) → SL @ {atr_multiplier:.1f} ATR")
            else:
                atr_multiplier = base_atr_multiplier
        else:
            atr_multiplier = base_atr_multiplier
        
        # CAPA 2: Maximum SL distance cap (prevents fake high RR)
        max_atr_multiple = 4.0  # Never exceed 4 ATR from entry
        max_sl_distance = max_atr_multiple * atr_1m
        
        # Minimum SL distance = dynamic ATR (survive noise in BTC)
        min_sl_distance = atr_multiplier * atr_1m
        
        if direction == 'LONG':
            structure_stop = support_resistance - buffer
            # SL must be at least min_sl_distance below entry
            atr_stop = entry_price - min_sl_distance
            stop = min(structure_stop, atr_stop)  # Use the LOWER (further) one
            
            # CAPA 2: Apply max cap
            max_stop = entry_price - max_sl_distance
            if stop < max_stop:
                logger.warning(f"⚠️ SL capped: {stop:.2f} → {max_stop:.2f} (max {max_atr_multiple} ATR)")
                stop = max_stop
        else:  # SHORT
            structure_stop = support_resistance + buffer
            # SL must be at least min_sl_distance above entry  
            atr_stop = entry_price + min_sl_distance
            stop = max(structure_stop, atr_stop)  # Use the HIGHER (further) one
            
            # CAPA 2: Apply max cap
            max_stop = entry_price + max_sl_distance
            if stop > max_stop:
                logger.warning(f"⚠️ SL capped: {stop:.2f} → {max_stop:.2f} (max {max_atr_multiple} ATR)")
                stop = max_stop
        
        logger.info(f"Stop loss calculated: {stop:.2f} (Structure: {support_resistance:.2f}±{buffer:.2f}, ATR min: {min_sl_distance:.2f} [{atr_multiplier}x])")
        return stop
    
    async def open_position(self, symbol: str, direction: str, entry_price: float,
                           stop_loss: float, atr_1m: float, hour: int, minute: int) -> Optional[Position]:
        """Open a new position"""
        async with self._lock:
            if self.position:
                logger.warning("Cannot open position: position already exists")
                return None
            
            # Calculate position size
            size = self.calculate_position_size(entry_price, stop_loss, hour, minute)
            if size == 0:
                logger.warning("Position size is zero, cannot open position")
                return None
            
            # Create position
            self.position = Position(symbol, direction, entry_price, size, stop_loss, atr_1m)
            
            ORDERS_RECOMMENDED.labels(symbol=symbol, direction=direction).inc()
            
            return self.position
    
    async def manage_position(self, current_price: float, atr_1m: float, 
                             volume: float, avg_volume: float,
                             tp2_target: Optional[float] = None,
                             ema20_5m: Optional[float] = None,
                             has_bos_against: bool = False,
                             market_time: datetime = None,
                             adx: Optional[float] = None,
                             atr_ratio: Optional[float] = None) -> Optional[dict]:
        """
        Manage open position with REGIME-BASED exit logic
        
        RÉGIMEN NORMAL (default):
        - STOP_LOSS: exactamente el actual
        - TRAILING_STOP: stepped, solo para régimen NORMAL
        - TIME_EXIT: DESACTIVADO
        - Objetivo: Cortar basura, rascar pequeños wins, proteger DD
        
        RÉGIMEN RUNNER (promoción cuando MFE >= 0.8R):
        - STOP_LOSS: ampliado a BE o -0.3R
        - TRAILING: DESACTIVADO (dejar correr)
        - TIME_BASED_EXIT: único exit (N velas = 45)
        - Objetivo: AQUÍ ESTÁ EL DINERO - dejar correr
        
        Args:
            market_time: Current market time (REQUIRED)
        
        Returns: exit_action dict if position should be closed, None otherwise
        """
        if not self.position:
            return None
        
        async with self._lock:
            pos = self.position
            pos.update_unrealized_pnl(current_price)
            
            # ================================================================
            # PASO 3: Check promoción a RUNNER (antes de cualquier exit)
            # ================================================================
            pos.check_regime_promotion()
            
            # Update trailing stop (solo aplica en régimen NORMAL)
            pos.update_trailing_stop(current_price, atr_1m, market_time)
            
            # Set TP2 if provided and not yet set
            if tp2_target and not pos.tp2_price:
                pos.update_tp2(tp2_target)
            
            # ================================================================
            # PASO 4: RÉGIMEN NORMAL - Gestión conservadora
            # Aplica: STOP_LOSS + TRAILING_STOP
            # NO aplica: TIME_EXIT
            # ================================================================
            
            if pos.regime == REGIME_NORMAL:
                # 0. Check mediocre trade exit (kill stagnant trades early)
                # HYBRID LOGIC (Jan 9, 2026): ADX + ATR ratio to avoid killing slow trends
                if pos.check_mediocre_trade_exit(n_candles=6, adx=adx, atr_ratio=atr_ratio):
                    return await self._close_position(current_price, 'TIME_INVALIDATION')
                
                # 0.5. ADD TO WINNER: Scale into proven winners (MFE >= 0.6R, no -0.2R touch)
                add_action = pos.check_add_to_winner(current_price)
                if add_action:
                    # Return add action to signal_engine for execution
                    return add_action
                
                # 1. Check stop loss (with grace period for first 10 min)
                sl_check = pos.check_stop_hit(current_price, market_time=market_time)
                if sl_check:
                    return await self._close_position(current_price, 'STOP_LOSS')
                
                # 2. Check TP1 (partial close, promotes toward RUNNER)
                if pos.check_tp1_hit(current_price):
                    return await self._handle_tp1(current_price)
                
                # 2.5. Check TP2 (after TP1 hit, close remaining runner)
                if pos.check_tp2_hit(current_price):
                    logger.info(f"🎯🎯 TP2 HIT @ {current_price:.2f} - Closing remaining runner")
                    return await self._close_position(current_price, 'TP2')
                
                # 3. Check trailing stop (only for NORMAL regime)
                if pos.check_trailing_stop_hit(current_price):
                    return await self._close_position(current_price, 'TRAILING_STOP')
                
                # NO TIME_EXIT para régimen NORMAL - let trailing handle it
                return None
            
            # ================================================================
            # PASO 5: RÉGIMEN RUNNER - Dejar correr
            # Aplica: STOP_LOSS ampliado + TIME_BASED_EXIT
            # NO aplica: Trailing fino, cierres por debilidad
            # ================================================================
            
            if pos.regime == REGIME_RUNNER:
                # 1. Check stop loss (ampliado - ya se movió en promoción)
                sl_check = pos.check_stop_hit(current_price, market_time=market_time)
                if sl_check:
                    return await self._close_position(current_price, 'STOP_LOSS')
                
                # 1.5. Check TP2 (runner's profit target)
                if pos.check_tp2_hit(current_price):
                    logger.info(f"🎯🎯 TP2 HIT @ {current_price:.2f} - Runner hit target!")
                    return await self._close_position(current_price, 'TP2')
                
                # 2. TIME_BASED_EXIT: La única salida programada para RUNNERS
                # Condición: candle_count >= N (45 velas fijas)
                if pos.check_runner_time_exit():
                    mfe_r = pos.calculate_mfe_r()
                    logger.info(f"🏃 RUNNER TIME EXIT: {pos.candle_count} candles, MFE={mfe_r:.2f}R")
                    return await self._close_position(current_price, 'TIME_BASED_EXIT')
                
                # No más exits - dejar correr
                return None
            
            return None
    
    async def _handle_tp1(self, current_price: float) -> dict:
        """Handle TP1 hit - close 60% and move stop to BE immediately"""
        pos = self.position
        
        # Calculate partial close
        close_size = pos.size * (RiskManagement.TP1_CLOSE_PCT / 100.0)
        remaining_size = pos.size - close_size
        
        # Calculate PnL for closed portion
        if pos.direction == 'LONG':
            pnl = (current_price - pos.entry_price) * close_size
        else:
            pnl = (pos.entry_price - current_price) * close_size
        
        self.total_pnl += pnl
        self.winning_trades.append(pnl)
        
        # Update position
        pos.size = remaining_size
        pos.tp1_hit = True
        
        # MOVE TO BREAKEVEN IMMEDIATELY after TP1
        # This protects the runner from giving back gains
        old_sl = pos.stop_loss
        pos.stop_loss = pos.entry_price
        pos.trailing_stop = pos.entry_price
        pos.be_moved = True
        
        logger.info(f"🎯 TP1 HIT - Closed {close_size:.4f} @ {current_price:.2f} | PnL: ${pnl:.2f} | Remaining: {remaining_size:.4f}")
        logger.info(f"🔒 SL moved to BE: {old_sl:.2f} → {pos.entry_price:.2f}")
        
        return {
            'action': 'PARTIAL_CLOSE',
            'reason': 'TP1',
            'price': current_price,
            'size': close_size,
            'pnl': pnl,
            'remaining_size': remaining_size
        }
    
    async def _close_position(self, current_price: float, reason: str) -> dict:
        """Close entire position and update equity/drawdown"""
        pos = self.position
        
        # Calculate final PnL
        if pos.direction == 'LONG':
            pnl = (current_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - current_price) * pos.size
        
        self.total_pnl += pnl
        
        # Update equity
        self.equity += pnl
        if self.equity > self.max_equity:
            self.max_equity = self.equity
        # Drawdown as % from max equity
        drawdown = (self.max_equity - self.equity) / self.max_equity if self.max_equity > 0 else 0.0
        drawdown_pct = max(0.0, min(drawdown * 100, 100.0))
        self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown_pct)
        logger.info(f"Equity: ${self.equity:.2f} | Max equity: ${self.max_equity:.2f} | Drawdown: {drawdown_pct:.2f}%")
        
        if pnl > 0:
            self.winning_trades.append(pnl)
        else:
            self.losing_trades.append(pnl)
        
        duration = pos.get_time_in_trade_minutes()
        
        logger.info(f"POSITION CLOSED - {reason} @ {current_price:.2f} | PnL: ${pnl:.2f} | Duration: {duration:.1f}min")
        
        exit_action = {
            'action': 'FULL_CLOSE',
            'reason': reason,
            'price': current_price,
            'size': pos.size,
            'pnl': pnl,
            'duration_minutes': duration,
            'entry_price': pos.entry_price,
            'equity': self.equity,
            'drawdown_pct': drawdown_pct
        }
        
        self.position = None
        
        return exit_action
    
    def get_drawdown_pct(self) -> float:
        """Return current drawdown as percent from max equity"""
        if self.max_equity > 0:
            return max(0.0, min((self.max_equity - self.equity) / self.max_equity * 100, 100.0))
        return 0.0
    
    def get_equity(self) -> float:
        """Return current equity"""
        return self.equity
    
    def get_initial_equity(self) -> float:
        """Return initial equity"""
        return self.initial_equity
    
    def _check_climactic_exit(self, current_volume: float, avg_volume: float, pos: Position) -> bool:
        """Check for climactic volume exit"""
        if avg_volume == 0:
            return False
        
        volume_ratio = current_volume / avg_volume
        
        # Check if in extension (beyond TP1)
        if pos.tp1_hit and volume_ratio >= RiskManagement.CLIMACTIC_VOLUME_MULTIPLIER:
            logger.info(f"Climactic volume detected: {volume_ratio:.2f}x average")
            return True
        
        return False
    
    def get_performance_metrics(self) -> dict:
        """Calculate performance metrics"""
        total_wins = len(self.winning_trades)
        total_losses = len(self.losing_trades)
        total_trades = total_wins + total_losses
        
        if total_trades == 0:
            return {
                'profit_factor': 0.0,
                'expectancy': 0.0,
                'win_rate': 0.0,
                'total_trades': 0
            }
        
        gross_profit = sum(self.winning_trades) if self.winning_trades else 0
        gross_loss = abs(sum(self.losing_trades)) if self.losing_trades else 0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        expectancy = self.total_pnl / total_trades
        win_rate = (total_wins / total_trades) * 100
        
        return {
            'profit_factor': round(profit_factor, 2),
            'expectancy': round(expectancy, 2),
            'win_rate': round(win_rate, 2),
            'total_trades': total_trades,
            'total_wins': total_wins,
            'total_losses': total_losses,
            'total_pnl': round(self.total_pnl, 2)
        }
    
    async def update_from_exchange_position(self, position_data: dict, atr: float = None):
        """
        Recover internal state from an existing on-chain position
        position_data: dict from CCXT fetch_position
        atr: optional ATR value for proper SL calculation
        
        CCXT unified position structure:
        - 'side': 'long' or 'short' (most reliable for direction)
        - 'contracts': can be signed (+ long, - short) or unsigned depending on exchange
        - 'info': raw exchange response
        """
        async with self._lock:
            try:
                symbol = position_data.get('symbol', 'UNKNOWN')
                contracts = float(position_data.get('contracts', 0))
                
                if contracts == 0:
                    self.position = None
                    return
                
                # CCXT unified 'side' field is most reliable
                side = position_data.get('side', '').lower()
                
                if side == 'long':
                    direction = 'LONG'
                elif side == 'short':
                    direction = 'SHORT'
                else:
                    # Fallback to sign of contracts (less reliable for some exchanges)
                    direction = 'LONG' if contracts > 0 else 'SHORT'
                    logger.warning(f"Position 'side' not available, inferring from contracts sign: {direction}")
                
                entry_price = float(position_data.get('entryPrice', 0))
                size = abs(contracts)
                
                # Log full position data for debugging
                logger.info(f"📊 Exchange position data: side={side}, contracts={contracts}, entryPrice={entry_price}")
                
                # Calculate SL based on ATR if provided, otherwise fallback to 1%
                if atr and atr > 0:
                    # Use 2.5x ATR like normal trades
                    sl_distance = 2.5 * atr
                    estimated_atr = atr
                else:
                    # Fallback: 1% of entry price
                    sl_distance = entry_price * 0.01
                    estimated_atr = entry_price * 0.01
                    logger.warning(f"⚠️ ATR not available for recovered position, using 1% fallback SL")
                
                if direction == 'LONG':
                    estimated_stop = entry_price - sl_distance
                else:
                    estimated_stop = entry_price + sl_distance
                
                self.position = Position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    size=size,
                    stop_loss=estimated_stop,
                    atr_1m=estimated_atr
                )
                
                logger.warning(f"RECOVERED POSITION from exchange: {direction} {size} @ {entry_price} | SL: {estimated_stop:.2f}")
                
            except Exception as e:
                logger.error(f"Error updating from exchange position: {e}")

    def has_open_position(self) -> bool:
        """Check if position is open"""
        return self.position is not None

