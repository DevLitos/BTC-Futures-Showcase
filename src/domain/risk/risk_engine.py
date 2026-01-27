"""
Risk Engine for BTC Perpetual Trading System
Handles: Position sizing, TP1/TP2, Trailing stops, Exit management

NOTE: Position sizing is now centralized in core_utils.PositionSizer.
This module integrates with the centralized sizer for consistency.
"""

import logging
import asyncio
import numpy as np
from datetime import datetime, UTC
from typing import Optional, Dict, Tuple
from src.infrastructure.monitoring.metrics import ORDERS_RECOMMENDED

from src.config.trading_config import (
    RiskManagement, OrderExecution,
    get_risk_for_session, SweepFilter, TimeBasedExits, SmartExits,
    TradeType
)
from src.shared.utils import (
    ensure_utc, PositionSizer, get_position_sizer
)
from src.domain.trading.state.state_types import PositionState
from src.infrastructure.exchanges.kraken.execution import KRAKEN_BTC_MIN_SIZE
from src.domain.risk.trailing_stop import (
    HybridATRTrailing, TrailingRegime, TrailingState, create_hybrid_trailing
)

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
                 market_time: datetime | None = None,
                 trade_type: TradeType = TradeType.LONG_MAIN,
                 regime_mode: str = 'TREND'):  # 🔥 NEW: regime_mode for BE logic
        # ===== INPUT VALIDATION =====
        if not symbol:
            raise ValueError("symbol cannot be empty")
        if direction.upper() not in ('LONG', 'SHORT'):
            raise ValueError(f"direction must be LONG or SHORT, got: {direction}")
        if entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got: {entry_price}")
        if size <= 0:
            raise ValueError(f"size must be positive, got: {size}")
        if stop_loss <= 0:
            raise ValueError(f"stop_loss must be positive, got: {stop_loss}")
        
        # Validate SL direction matches trade direction
        if direction.upper() == 'LONG' and stop_loss >= entry_price:
            logger.warning(f"⚠️ LONG position SL ({stop_loss}) >= entry ({entry_price}) - invalid but continuing")
        if direction.upper() == 'SHORT' and stop_loss <= entry_price:
            logger.warning(f"⚠️ SHORT position SL ({stop_loss}) <= entry ({entry_price}) - invalid but continuing")
        
        self.symbol = symbol
        self.direction = direction  # 'LONG' or 'SHORT'
        self.trade_type = trade_type  # LONG_MAIN, SHORT_STRUCTURAL, or SHORT_PULLBACK
        self.regime_mode = regime_mode  # 🔥 NEW: 'TREND', 'RANGE', 'BREAKOUT'
        self.entry_price = entry_price
        self.size = size
        self.stop_loss = stop_loss
        self.initial_stop_loss = stop_loss
        
        # =====================================================================
        # SHORT_PULLBACK SPECIFIC TRACKING
        # =====================================================================
        self.is_pullback_short = (trade_type == TradeType.SHORT_PULLBACK)
        self.pullback_tp1_partial_done = False  # Track if 50% closed at TP1
        self.pullback_be_moved = False          # Track if BE moved after TP1
        self.pullback_candle_count = 0          # Candle counter for time stop
        self.pullback_swing_high: float = 0.0   # Store swing high for reference
        self.pullback_confirm_level: float = 0.0  # Store confirm level
        
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
        self.take_profit: Optional[float] = None  # Exchange TP price when set
        
        # Trailing stop
        self.trailing_stop = stop_loss
        
        # ADX tracking for trend exhaustion detection
        self._entry_adx: Optional[float] = None  # Store entry ADX for trend monitoring
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
        
        # =====================================================================
        # SMART EXITS TRACKING (Jan 13, 2026)
        # =====================================================================
        # Scaled partial exits tracking
        self.partial_exits_done = []  # List of (mfe_r_level, pct_exited) tuples
        self.remaining_size_pct = 1.0  # 100% initially
        
        # Time profit lock
        self.profit_locked = False
        self.locked_r = 0.0
        
        # Last RSI value (updated externally)
        self.last_rsi = 50.0
        
        # Bollinger Band width tracking
        self.last_bb_width = 0.0
        self.avg_bb_width = 0.0
        
        # =====================================================================
        # ANTI RE-ENTRY TRACKING (Jan 23, 2026)
        # Track entry conditions for "no reentry without new edge"
        # =====================================================================
        self.entry_conditions = {}  # Store entry conditions for comparison
        self.entry_atr = atr_1m  # Store ATR at entry for price range comparison
        
        # =====================================================================
        # WORST-TRADE PREVENTION (Jan 23, 2026)
        # Track thesis validity for max loss limiting
        # =====================================================================
        self.thesis_intact = True  # Assume thesis is valid until proven otherwise
        self.thesis_invalidation_reason = None
        self.thesis_last_check_candle = 0
        
        # =====================================================================
        # ADOPTED POSITION FLAG (Jan 26, 2026)
        # Positions adopted from exchange restart should NOT trigger 
        # WORST_TRADE_PREVENTION - we don't know the original thesis
        # =====================================================================
        self.is_adopted = False  # Set True for positions adopted at restart
        
        # =====================================================================
        # CHECKPOINT IDEMPOTENCY (Jan 23, 2026)
        # Prevent repeated checkpoint actions and log spam
        # =====================================================================
        self.time_checkpoint_profit_partial_done = False  # Only one TIME_IN_PROFIT partial
        self.time_checkpoint_last_action: Optional[str] = None  # Last checkpoint action taken
        self.time_checkpoint_last_r_bucket: Optional[float] = None  # Last logged R bucket (0.05R increments)
        self.last_management_action_time: Optional[datetime] = None  # Debounce management actions
        self.MANAGEMENT_COOLDOWN_SEC = 30  # Seconds to wait between management actions
        
        # =====================================================================
        # HYBRID ATR TRAILING (Jan 15, 2026)
        # 🔥 FIX (Jan 22, 2026): Use regime_mode from parameter, not hardcoded TREND
        # =====================================================================
        # Initialize hybrid trailing stop manager
        self.hybrid_trailing: Optional[HybridATRTrailing] = None
        self.use_hybrid_trailing = True  # Feature flag
        
        if self.use_hybrid_trailing:
            # 🔥 FIX: Use regime_mode passed from SignalEngine (SINGLE SOURCE OF TRUTH)
            # Map regime_mode string to TrailingRegime enum
            # Handles both simplified (TREND/RANGE) and EdgeRegime values
            regime_map = {
                # Simplified values
                'TREND': TrailingRegime.TREND,
                'TREND_EXPANSION': TrailingRegime.TREND,
                'BREAKOUT': TrailingRegime.TREND,
                'RANGE': TrailingRegime.RANGE,
                'TREND_EXHAUSTION': TrailingRegime.RANGE,
                'MEAN_REVERSION': TrailingRegime.RANGE,
                'CHOPPY_DEAD': TrailingRegime.RANGE,
                # EdgeRegime values (from EdgeRegimeClassifier)
                'EXPANSION_TREND_CONTINUATION': TrailingRegime.TREND,  # REGIME_A
                'COMPRESSION_FAKEOUTS': TrailingRegime.RANGE,          # REGIME_B
                'NEWS_DRIVEN_UNSTABLE': TrailingRegime.RANGE,          # REGIME_C
                'LOW_PARTICIPATION_CHOP': TrailingRegime.RANGE,        # REGIME_D
                'UNKNOWN': TrailingRegime.RANGE,
                'INSUFFICIENT_DATA': TrailingRegime.RANGE,
            }
            # Handle both upper and original case
            regime_key = regime_mode.upper() if regime_mode else 'RANGE'
            initial_regime = regime_map.get(regime_key, TrailingRegime.RANGE)
            
            self.hybrid_trailing = create_hybrid_trailing(
                entry_price=entry_price,
                initial_stop=stop_loss,
                direction=direction,
                regime_str=initial_regime.value
            )
            logger.info(f"🎯 Trailing using regime={initial_regime.value} (from regime_mode={regime_mode})")
        
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
    
    def mark_exchange_sl_active(self, order_id: str | None = None):
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
    
    def calculate_pnl(self, current_price: float) -> float:
        """
        Calculate PnL at a given price point.
        
        Args:
            current_price: Current market price
            
        Returns:
            float: PnL in USD (positive = profit, negative = loss)
        """
        if self.direction == 'LONG':
            return (current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - current_price) * self.size
    
    @property
    def current_stop_loss(self) -> float:
        """
        Get the current stop loss price.
        
        Returns the best (tightest) of:
        - Initial stop loss
        - Trailing stop
        - Hybrid trailing stop (if active)
        
        Returns:
            float: Current SL price
        """
        candidates = [self.stop_loss]
        
        if self.trailing_stop:
            candidates.append(self.trailing_stop)
        
        if self.hybrid_trailing and self.hybrid_trailing.is_active():
            hybrid_stop = self.hybrid_trailing.get_stop()
            if hybrid_stop:
                candidates.append(hybrid_stop)
        
        # For LONG, best SL is highest; for SHORT, best SL is lowest
        if self.direction == 'LONG':
            return max(candidates)
        else:
            return min(candidates)
    
    @property
    def current_r_multiple(self) -> float:
        """
        Calculate current R-multiple based on unrealized PnL.
        
        R = current_profit / original_risk
        Where original_risk = entry_price - original_stop_loss (1R)
        
        Returns:
            float: Current R-multiple (negative if in loss, positive if in profit)
                   e.g., -0.25 means losing 0.25R, +1.5 means up 1.5R
        """
        if self.original_stop_distance <= 0:
            return 0.0  # Safety: avoid division by zero
        
        if self.direction == 'LONG':
            current_profit = self.highest_price - self.entry_price if hasattr(self, '_last_update_price') else 0
            # Use unrealized PnL normalized by position size
            price_move = self.unrealized_pnl / self.size if self.size > 0 else 0
        else:
            price_move = self.unrealized_pnl / self.size if self.size > 0 else 0
        
        # R = price_move / original_stop_distance
        return price_move / self.original_stop_distance

    def update_trailing_stop(self, current_price: float, atr_1m: float, market_time: datetime,
                             highs: np.ndarray | None = None, lows: np.ndarray | None = None,
                             regime_hint: str | None = None):
        """
        HYBRID TRAILING - Combines R-stepped locks with ATR-adaptive trailing.
        
        NEW SYSTEM (Jan 15, 2026):
        Uses HybridATRTrailing which combines:
        1. R-stepped locks (guaranteed protection at MFE thresholds)
        2. ATR-adaptive trailing (dynamic based on regime)
        
        Falls back to legacy stepped trailing if hybrid is disabled.
        
        Args:
            current_price: Current market price
            atr_1m: Current 1-minute ATR
            market_time: Current market time
            highs: Array of recent high prices (for ATR trailing)
            lows: Array of recent low prices (for ATR trailing)
            regime_hint: Optional regime hint ('TREND', 'RANGE', 'VOLATILITY')
        """
        # RUNNER regime: NO trailing, let it run
        if self.regime == REGIME_RUNNER:
            return  # SL was fixed at RUNNER promotion
        
        if market_time is None:
            market_time = self.open_time
        
        market_time = ensure_utc(market_time)
        self.atr_1m = atr_1m
        
        # =====================================================================
        # HYBRID TRAILING (if enabled)
        # =====================================================================
        if self.use_hybrid_trailing and self.hybrid_trailing is not None:
            # Determine regime
            if regime_hint:
                try:
                    regime = TrailingRegime(regime_hint.upper())
                except ValueError:
                    regime = TrailingRegime.TREND
            else:
                regime = TrailingRegime.TREND
            
            # Convert highs/lows to numpy arrays if needed
            import numpy as np
            if highs is None or lows is None or len(highs) < 3:
                # Create synthetic arrays from MFE tracking
                highs = np.array([self.highest_price])
                lows = np.array([self.lowest_price])
            
            # Update hybrid trailing
            state = self.hybrid_trailing.update(
                current_price=current_price,
                highs=highs,
                lows=lows,
                atr=atr_1m,
                regime=regime
            )
            
            # Sync trailing_stop with hybrid result
            new_hybrid_stop = state.final_stop
            
            if self.direction == 'LONG':
                if new_hybrid_stop > self.trailing_stop:
                    logger.info(f"🎯 HYBRID TRAIL: {self.trailing_stop:.2f} → {new_hybrid_stop:.2f} ({state.stop_source}, tier={state.r_lock_tier})")
                    self.trailing_stop = new_hybrid_stop
            else:
                if new_hybrid_stop < self.trailing_stop:
                    logger.info(f"🎯 HYBRID TRAIL: {self.trailing_stop:.2f} → {new_hybrid_stop:.2f} ({state.stop_source}, tier={state.r_lock_tier})")
                    self.trailing_stop = new_hybrid_stop
            
            return  # Hybrid trailing handled everything
        
        # =====================================================================
        # LEGACY STEPPED TRAILING (fallback if hybrid disabled)
        # 🔥 FIX (Jan 22, 2026): Added regime-aware BE logic
        # =====================================================================
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
        
        elif mfe_r >= 1.2:
            # 🔥 FIX (Jan 22, 2026): BE level depends on regime_mode
            # TREND: BE at 1.2R (give runners room)
            # RANGE: BE at this level already (was 1.0R but now at 1.2R for RANGE)
            # Lock in +0.4R for TREND, BE for RANGE
            if self.regime_mode == 'RANGE':
                # RANGE: Lock BE earlier (less runners expected)
                if self.direction == 'LONG':
                    self.trailing_stop = max(self.trailing_stop, self.entry_price)
                else:
                    self.trailing_stop = min(self.trailing_stop, self.entry_price)
                logger.debug(f"📈 TRAILING (RANGE): MFE {mfe_r:.2f}R >= 1.2R → Lock BE")
            else:
                # TREND/BREAKOUT: Lock +0.4R (give room)
                if self.direction == 'LONG':
                    new_stop = self.entry_price + (risk * 0.4)
                    self.trailing_stop = max(self.trailing_stop, new_stop)
                else:
                    new_stop = self.entry_price - (risk * 0.4)
                    self.trailing_stop = min(self.trailing_stop, new_stop)
                logger.debug(f"📈 TRAILING STEP 2: MFE {mfe_r:.2f}R >= 1.2R → Lock +0.4R")
        
        elif mfe_r >= 1.0:
            # 🔥 FIX: RANGE locks BE at 1.0R (more defensive)
            if self.regime_mode == 'RANGE':
                if self.direction == 'LONG':
                    # Lock at -0.2R (slightly protective but not full BE)
                    new_stop = self.entry_price - (risk * 0.2)
                    self.trailing_stop = max(self.trailing_stop, new_stop)
                else:
                    new_stop = self.entry_price + (risk * 0.2)
                    self.trailing_stop = min(self.trailing_stop, new_stop)
                logger.debug(f"📈 TRAILING (RANGE): MFE {mfe_r:.2f}R >= 1.0R → Lock -0.2R")
            # TREND: no action at 1.0R, wait for 1.2R
    
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
                                     adx: float | None = None, atr_ratio: float | None = None,
                                     current_price: float | None = None,
                                     vwap: float | None = None, last_lh: float | None = None,
                                     vah: float | None = None) -> dict | None:
        """
        TIME_INVALIDATION INTELIGENTE (Jan 23, 2026)
        
        CAMBIO CRÍTICO: Timeout = checkpoint, NO gatillo ciego.
        
        En lugar de cerrar automáticamente, evalúa:
        1. Si hay invalidación ESTRUCTURAL real (no numérica)
        2. El estado del P&L (profit/flat/loss)
        3. Si cerrar empeoraría el trade vs esperar
        
        REGLA DE ORO: TIME_INVALIDATION NUNCA puede cerrar un trade 
        con más pérdida que -0.25R. Si estás en -0.1R y no hay invalidación
        estructural, NO cierras "por tiempo" a -0.6R después.
        
        EXCEPTION (Jan 26, 2026):
        - ADOPTED positions are EXCLUDED from time invalidation
        - We don't know when the original trade was opened
        - Let the exchange SL/TP handle these positions
        
        Args:
            n_candles: Number of candles before checking (default 6)
            mfe_threshold: Minimum MFE in R required (default 0.3R)
            adx: Current ADX value
            atr_ratio: Current ATR / Average ATR
            current_price: Current price for P&L calculation
            vwap: Current VWAP for structural invalidation
            last_lh: Last Lower High for structure check
            vah: Value Area High for structure check
        
        Returns: 
            dict with action and details, or None if no action needed
            Possible actions: 'CLOSE', 'PARTIAL', 'REDUCE_RISK', 'HOLD'
        """
        from src.config.trading_config import ExitHierarchy
        
        # =========================================================================
        # 🔒 CRITICAL FIX (Jan 26, 2026): EXCLUDE ADOPTED POSITIONS
        # Adopted positions have candle_count starting from 0 but may have been
        # open for hours/days. Time-based invalidation doesn't make sense.
        # =========================================================================
        if self.is_adopted:
            return None
        
        if self.candle_count < n_candles:
            return None  # Not yet at checkpoint
        
        mfe_r = self.calculate_mfe_r()
        
        # Calculate current R (actual P&L right now)
        if current_price is None:
            return None
        
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk <= 0:
            return None
            
        if self.direction == 'LONG':
            current_r = (current_price - self.entry_price) / initial_risk
        else:
            current_r = (self.entry_price - current_price) / initial_risk
        
        # =====================================================================
        # 🔥 FIX (Jan 27, 2026): INTELLIGENT TIME_INVALIDATION FOR RANGE
        # 
        # PROBLEM: In ranging markets, -0.3R limit was too tight.
        # Price floats around, hits -0.3R, closes for "administrative" loss.
        # This is DEATH BY A THOUSAND CUTS.
        # 
        # SOLUTION:
        # - In TRENDING (ADX >= 20): Use tight limit (-0.30R)
        # - In RANGING (ADX < 20): Use looser limit (-0.60R) OR
        #   don't close unless structure is invalid
        # 
        # The key insight: In range, let the STOP do its job.
        # TIME_INVALIDATION should only trigger if structure breaks.
        # =====================================================================
        
        # Determine market type
        is_ranging = adx is not None and adx < 20
        
        # Set max loss based on market type
        if is_ranging:
            # In range: looser limit, let stop do its job
            max_loss_r = -0.60  # 🔥 FIX: Allow more room in range
            logger.debug(f"📊 TIME checkpoint: RANGING mode (ADX={adx:.1f}) → max_loss={max_loss_r}R")
        else:
            # In trend: normal limit
            max_loss_r = ExitHierarchy.TIME_INVALIDATION_MAX_LOSS_R  # -0.30R
        
        # =====================================================================
        # REGLA CRÍTICA: Límite de pérdida por TIME_INVALIDATION
        # =====================================================================
        if current_r <= max_loss_r:
            logger.warning(
                f"☠️ TIME_INVALIDATION: Loss {current_r:.2f}R exceeds limit {max_loss_r}R | "
                f"ADX={'RANGE' if is_ranging else 'TREND'} | "
                f"Closing immediately to prevent worse loss"
            )
            return {
                'action': 'CLOSE',
                'reason': 'TIME_INVALIDATION_MAX_LOSS',
                'current_r': current_r,
                'max_loss_r': max_loss_r,
                'candles': self.candle_count,
                'market_type': 'RANGE' if is_ranging else 'TREND'
            }
        
        # =====================================================================
        # Si MFE >= threshold, trade está probándose - NO matar
        # =====================================================================
        if mfe_r >= mfe_threshold:
            return None
        
        # =====================================================================
        # Check condiciones de mercado (ADX, ATR)
        # Si mercado está trending o volatilidad OK, dar más tiempo
        # =====================================================================
        ADX_THRESHOLD = 20
        ATR_RATIO_THRESHOLD = 0.8
        
        market_alive = False
        if adx is not None and adx >= ADX_THRESHOLD:
            market_alive = True
            logger.debug(f"📊 TIME checkpoint: ADX={adx:.1f} >= {ADX_THRESHOLD} (trending)")
        
        if atr_ratio is not None and atr_ratio >= ATR_RATIO_THRESHOLD:
            market_alive = True
            logger.debug(f"📊 TIME checkpoint: ATR ratio={atr_ratio:.2f} >= {ATR_RATIO_THRESHOLD} (vol OK)")
        
        # =====================================================================
        # 🔥 FIX (Jan 27, 2026): In RANGING market, ALWAYS HOLD unless
        # we hit the max_loss or structure breaks. NO premature closes.
        # =====================================================================
        if is_ranging and not market_alive:
            # In range with dead market: HOLD unless at max loss
            # The stop will do its job if we're wrong
            if current_r > max_loss_r + 0.1:  # Still have room
                logger.info(
                    f"⏱️ TIME checkpoint RANGE: {current_r:.2f}R | "
                    f"Market dead but not at limit → HOLD (let stop work)"
                )
                return {
                    'action': 'HOLD',
                    'reason': 'RANGE_MARKET_HOLD',
                    'current_r': current_r,
                    'max_loss_r': max_loss_r,
                    'candles': self.candle_count
                }
        
        # =====================================================================
        # COMPORTAMIENTO SEGÚN P&L ACTUAL
        # =====================================================================
        
        # A) EN PROFIT (>= 0.1R): Parcial + runner
        # 🔥 FIX (Jan 23, 2026): IDEMPOTENCY - Only allow ONE TIME_IN_PROFIT partial
        if current_r >= 0.1:
            # Check if we already did the TIME_IN_PROFIT partial
            if self.time_checkpoint_profit_partial_done:
                # Already executed - return HOLD instead of repeating
                logger.debug(f"⏱️ TIME checkpoint in PROFIT: {current_r:.2f}R | Partial already done, holding runner")
                return {
                    'action': 'HOLD',
                    'reason': 'TIME_IN_PROFIT_PARTIAL_ALREADY_DONE',
                    'current_r': current_r,
                    'candles': self.candle_count
                }
            
            partial_pct = ExitHierarchy.TIME_IN_PROFIT_PARTIAL_PCT
            logger.info(
                f"⏱️ TIME checkpoint in PROFIT: {current_r:.2f}R | "
                f"Action: {partial_pct:.0%} partial, keep runner (ONE-SHOT)"
            )
            return {
                'action': 'PARTIAL',
                'reason': 'TIME_IN_PROFIT',
                'current_r': current_r,
                'partial_pct': partial_pct,
                'keep_runner': True,
                'candles': self.candle_count
            }
        
        # B) FLAT o pequeña pérdida: Evaluar si mercado está vivo
        if market_alive:
            # Mercado activo - dar más tiempo, solo ajustar stop
            # 🔥 FIX (Jan 23, 2026): Rate-limited logging - only log on R bucket change
            current_r_bucket = round(current_r * 20) / 20  # 0.05R increments
            adx_str = f"{adx:.1f}" if adx is not None else "N/A"
            
            # Only log if R bucket changed (avoid spam)
            if self.time_checkpoint_last_r_bucket != current_r_bucket:
                self.time_checkpoint_last_r_bucket = current_r_bucket
                logger.info(
                    f"⏱️ TIME checkpoint FLAT (market alive): {current_r:.2f}R | "
                    f"ADX={adx_str} | Holding"
                )
            # else: skip logging (same bucket)
            
            return {
                'action': 'HOLD',
                'reason': 'MARKET_ALIVE',
                'current_r': current_r,
                'candles': self.candle_count,
                'market_indicators': {'adx': adx, 'atr_ratio': atr_ratio}
            }
        
        # C) Mercado muerto + flat: Evaluar invalidación estructural
        structural_invalid = self._check_structural_invalidation(
            current_price, vwap, last_lh, vah
        )
        
        if structural_invalid:
            # Estructura invalidada - cerrar
            logger.warning(
                f"☠️ TIME_INVALIDATION + STRUCTURE INVALID: {current_r:.2f}R | "
                f"{structural_invalid['reason']}"
            )
            return {
                'action': 'CLOSE',
                'reason': f"TIME_STRUCTURAL_INVALID: {structural_invalid['reason']}",
                'current_r': current_r,
                'candles': self.candle_count,
                'structural_details': structural_invalid
            }
        
        # D) Mercado muerto pero estructura intacta - reducir riesgo, no cerrar
        # Solo cerrar si ya estamos cerca del límite
        if current_r <= max_loss_r + 0.1:  # Dentro de 0.1R del límite
            logger.info(
                f"⏱️ TIME checkpoint near limit: {current_r:.2f}R | "
                f"Structure intact but close to max loss - REDUCE RISK"
            )
            return {
                'action': 'REDUCE_RISK',
                'reason': 'TIME_NEAR_LIMIT',
                'current_r': current_r,
                'move_stop_to_r': max_loss_r,
                'candles': self.candle_count
            }
        
        # E) Default: Mercado muerto, estructura OK, pérdida moderada
        # Cerrar porque no hay edge ya
        adx_str = f"ADX={adx:.1f}" if adx else "ADX=N/A"
        atr_str = f"ATR_ratio={atr_ratio:.2f}" if atr_ratio else "ATR=N/A"
        logger.warning(
            f"☠️ TIME_INVALIDATION: {self.candle_count} velas, MFE={mfe_r:.2f}R, "
            f"current={current_r:.2f}R | {adx_str} | {atr_str}"
        )
        return {
            'action': 'CLOSE',
            'reason': 'TIME_INVALIDATION_DEAD_MARKET',
            'current_r': current_r,
            'mfe_r': mfe_r,
            'candles': self.candle_count
        }
    
    def _check_structural_invalidation(self, current_price: float, 
                                        vwap: float | None = None, 
                                        last_lh: float | None = None,
                                        vah: float | None = None) -> dict | None:
        """
        Verifica invalidación ESTRUCTURAL real (no numérica).
        
        SHORT invalida si:
        - Precio reclaim VWAP con aceptación
        - Rotura de último Lower High  
        - Recuperar VAH con aceptación
        
        LONG invalida si:
        - Precio pierde VWAP con aceptación
        - Rotura de último Higher Low
        - Perder VAL con aceptación
        
        Returns:
            dict con 'invalid': True/False y 'reason' si invalida
        """
        if current_price is None:
            return None
        
        # Track acceptance candles (simplified - would need candle tracking)
        # For now, check if price is clearly on wrong side
        
        if self.direction == 'SHORT':
            # SHORT invalida si precio está claramente ARRIBA de niveles clave
            if vwap and current_price > vwap * 1.001:  # 0.1% above VWAP
                self.thesis_intact = False
                self.thesis_invalidation_reason = f'VWAP_RECLAIM: {current_price:.2f} > {vwap:.2f}'
                return {
                    'invalid': True,
                    'reason': f'VWAP_RECLAIM: price {current_price:.2f} > VWAP {vwap:.2f}'
                }
            
            if last_lh and current_price > last_lh:
                self.thesis_intact = False
                self.thesis_invalidation_reason = f'LH_BROKEN: {current_price:.2f} > {last_lh:.2f}'
                return {
                    'invalid': True,
                    'reason': f'LH_BROKEN: price {current_price:.2f} > LH {last_lh:.2f}'
                }
            
            if vah and current_price > vah * 1.001:
                self.thesis_intact = False
                self.thesis_invalidation_reason = f'VAH_RECLAIM: {current_price:.2f} > {vah:.2f}'
                return {
                    'invalid': True,
                    'reason': f'VAH_RECLAIM: price {current_price:.2f} > VAH {vah:.2f}'
                }
        
        elif self.direction == 'LONG':
            # LONG invalida si precio está claramente DEBAJO de niveles clave
            if vwap and current_price < vwap * 0.999:  # 0.1% below VWAP
                self.thesis_intact = False
                self.thesis_invalidation_reason = f'VWAP_LOST: {current_price:.2f} < {vwap:.2f}'
                return {
                    'invalid': True,
                    'reason': f'VWAP_LOST: price {current_price:.2f} < VWAP {vwap:.2f}'
                }
            
            # last_lh for LONG would be last Higher Low (last_hl)
            # Simplified for now
        
        return None
    
    def check_worst_trade_prevention(self, current_price: float, market_time: datetime | None = None) -> dict | None:
        """
        WORST-TRADE PREVENTION (Jan 23, 2026) - UPDATED Jan 27, 2026 v2
        
        "Si no invalida la tesis, no puede ser el peor trade del día"
        
        INTELLIGENT REEVALUATION (Jan 27, 2026):
        Instead of binary "wait N minutes then close", we continuously evaluate:
        - Phase 1 (0-4 min): NO WTP - let setup develop
        - Phase 2 (4+ min): Evaluate based on MFE, momentum, distance to TP
          - If MFE >= 0.15R → exempt (trade proved itself)
          - If price improving (moving toward TP) → extend grace
          - If price stagnating/worsening AND loss > threshold → close
        
        Args:
            current_price: Current market price
            market_time: Current market time (for backtest compatibility). Falls back to time.time() if None.
        
        Returns:
            dict with action if should exit, None otherwise
        """
        from src.config.trading_config import ExitHierarchy
        import time
        
        if not ExitHierarchy.WORST_TRADE_PREVENTION_ENABLED:
            return None
        
        # EXCLUDE ADOPTED POSITIONS
        if self.is_adopted:
            return None
        
        # Calculate trade age using open_time (datetime) which always exists
        # FIX Jan 27, 2026: Use market_time for backtest compatibility instead of time.time()
        if self.open_time:
            if market_time is not None:
                # Use market_time for accurate backtest behavior
                trade_age_seconds = (market_time - self.open_time).total_seconds()
            else:
                # Fallback to real time for live trading if market_time not provided
                trade_age_seconds = time.time() - self.open_time.timestamp()
        else:
            trade_age_seconds = 0
        
        # Phase 1: Absolute grace period (0-4 minutes)
        ABSOLUTE_GRACE_SECONDS = 240  # 4 minutes
        if trade_age_seconds < ABSOLUTE_GRACE_SECONDS:
            return None
        
        # Calculate current R and MFE
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk <= 0:
            return None
        
        if self.direction == 'LONG':
            current_r = (current_price - self.entry_price) / initial_risk
        else:
            current_r = (self.entry_price - current_price) / initial_risk
        
        mfe_r = getattr(self, 'mfe_r', 0.0) if hasattr(self, 'mfe_r') else self.calculate_mfe_r()
        
        # =========================================================================
        # INTELLIGENT REEVALUATION
        # =========================================================================
        
        # EXEMPT: Trade already showed significant profit (proved itself)
        MFE_THRESHOLD_TO_EXEMPT = 0.15
        if mfe_r >= MFE_THRESHOLD_TO_EXEMPT:
            return None
        
        # EXEMPT: Trade is currently in profit (even small)
        if current_r > 0:
            return None
        
        # EVALUATE: Trade is in loss, check if it's improving
        # "Improving" = loss is smaller than it was at MFE low point
        mae_r = getattr(self, 'mae_r', 0.0) if hasattr(self, 'mae_r') else self.calculate_mae_r()
        
        # If current loss is smaller than worst (MAE), price is improving
        current_loss = abs(current_r) if current_r < 0 else 0
        is_improving = current_loss < mae_r * 0.9  # Allow 10% buffer
        
        # EXTEND GRACE: If improving, give more time
        EXTENDED_GRACE_SECONDS = 480  # 8 minutes total if improving
        if is_improving and trade_age_seconds < EXTENDED_GRACE_SECONDS:
            logger.debug(f"📊 WTP: Trade improving (loss {current_r:.2f}R < MAE {-mae_r:.2f}R) - extending grace")
            return None
        
        # CLOSE: Loss exceeds threshold and not improving
        max_loss_when_thesis_intact = ExitHierarchy.THESIS_INTACT_MAX_LOSS_R
        
        if self.thesis_intact and current_r <= max_loss_when_thesis_intact:
            # Final check: is distance to TP reasonable?
            distance_to_tp = abs(self.take_profit_1 - current_price) if self.take_profit_1 else float('inf')
            distance_to_sl = abs(current_price - self.current_stop_loss) if self.current_stop_loss else float('inf')
            
            # If TP is closer than SL, give more time
            if distance_to_tp < distance_to_sl * 0.5:
                logger.debug(f"📊 WTP: TP closer than SL ({distance_to_tp:.0f} vs {distance_to_sl:.0f}) - holding")
                return None
            
            logger.warning(
                f"🛡️ WORST-TRADE PREVENTION: Loss {current_r:.2f}R hit limit {max_loss_when_thesis_intact}R "
                f"(age: {trade_age_seconds:.0f}s, MFE: {mfe_r:.2f}R, MAE: {-mae_r:.2f}R, improving: {is_improving})"
            )
            return {
                'action': 'CLOSE',
                'reason': 'WORST_TRADE_PREVENTION',
                'current_r': current_r,
                'max_loss_r': max_loss_when_thesis_intact,
                'thesis_intact': True,
                'mfe_r': mfe_r,
                'mae_r': mae_r,
                'trade_age_s': trade_age_seconds,
                'is_improving': is_improving,
                'message': 'Loss limit hit after intelligent evaluation'
            }
        
        return None
    
    def check_add_to_winner(self, current_price: float) -> dict | None:
        """
        DYNAMIC SIZING: Add 0.5x position when trade proves itself
        
        Condition:
          - MFE >= 0.6R (historical high)
          - Current R >= 0.5R (price still near highs, NOT falling)
          - Never touched -0.2R (clean move)
        
        This increases R on clear winners without increasing drawdown.
        
        Returns: dict with add_size if should add, None otherwise
        """
        if self.added_to_winner:
            return None  # Already added
        
        mfe_r = self.calculate_mfe_r()
        
        # 🔒 FIX (Jan 16, 2026): Calculate CURRENT R, not just historical MFE
        # This prevents adding to a position that WAS winning but is now falling
        risk = abs(self.entry_price - self.initial_stop_loss)
        if risk <= 0:
            return None
        
        if self.direction == 'LONG':
            current_r = (current_price - self.entry_price) / risk
        else:
            current_r = (self.entry_price - current_price) / risk
        
        # Track if we've been -0.2R against
        if self.direction == 'LONG':
            worst_drawdown = (self.entry_price - self.lowest_price) / risk
        else:
            worst_drawdown = (self.highest_price - self.entry_price) / risk
        
        if worst_drawdown >= 0.2:
            self.touched_minus_02r = True
        
        # Check conditions - MUST include current_r check!
        # MFE >= 0.6R means it REACHED that level
        # current_r >= 0.5R means it's STILL there (not fallen back)
        MIN_CURRENT_R_FOR_ADD = 0.5
        
        if mfe_r >= 0.6 and current_r >= MIN_CURRENT_R_FOR_ADD and not self.touched_minus_02r:
            add_size = self.initial_size * 0.5
            
            # 🔒 FIX (Jan 14, 2026): Validate minimum order size
            if add_size < KRAKEN_BTC_MIN_SIZE:
                logger.info(f"🔄 ADD TO WINNER blocked: add_size {add_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                return None
            
            self.added_to_winner = True
            logger.info(f"💪 ADD TO WINNER: MFE {mfe_r:.2f}R, current={current_r:.2f}R >= 0.5R → Adding {add_size:.6f}")
            return {
                'action': 'ADD_POSITION',
                'add_size': add_size,
                'new_total_size': self.size + add_size,
                'reason': f'Winner add at MFE {mfe_r:.2f}R (current={current_r:.2f}R)'
            }
        elif mfe_r >= 0.6 and current_r < MIN_CURRENT_R_FOR_ADD:
            # Log why we're NOT adding (for debugging)
            logger.debug(f"🔄 ADD TO WINNER skipped: MFE {mfe_r:.2f}R but current_r {current_r:.2f}R < {MIN_CURRENT_R_FOR_ADD}R")
        
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
    
    def check_stop_hit(self, current_price: float, current_low: float | None = None, 
                       atr_1m: float | None = None, market_time: datetime | None = None) -> bool:
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
    
    def get_time_in_trade_minutes(self, market_time: datetime | None = None) -> float:
        """
        Get time in trade in minutes.
        
        Args:
            market_time: Current market time (REQUIRED - never use datetime.now())
        """
        current_time = market_time if market_time else self.open_time
        return (current_time - self.open_time).total_seconds() / 60
    
    # =========================================================================
    # SHORT_PULLBACK SPECIFIC METHODS
    # =========================================================================
    
    def pullback_increment_candle_count(self):
        """Increment candle counter for pullback time stop."""
        if self.is_pullback_short:
            self.pullback_candle_count += 1
    
    def pullback_check_time_stop(self, time_stop_candles: int = 6, min_mfe_r: float = 0.3) -> Tuple[bool, str]:
        """
        Check if pullback short should be exited due to time stop.
        
        TIME STOP: If after N candles MFE < min_mfe_r, exit.
        This prevents staying stuck in losing shorts against bull market.
        
        Args:
            time_stop_candles: Number of candles before checking (default 6 = 30min)
            min_mfe_r: Minimum MFE required after time_stop_candles
            
        Returns:
            Tuple of (should_exit, reason)
        """
        if not self.is_pullback_short:
            return False, "Not a pullback short"
        
        if self.pullback_candle_count < time_stop_candles:
            return False, f"Only {self.pullback_candle_count}/{time_stop_candles} candles"
        
        mfe_r = self.calculate_mfe_r()
        
        if mfe_r < min_mfe_r:
            return True, f"Time stop: MFE {mfe_r:.2f}R < {min_mfe_r}R after {self.pullback_candle_count} candles"
        
        return False, f"MFE {mfe_r:.2f}R >= {min_mfe_r}R"
    
    def pullback_check_be_after_tp1(self, atr_5m: float, be_offset_atr: float = 0.1) -> Optional[float]:
        """
        Check if BE (breakeven) should be moved after TP1 hit.
        
        BE = entry - offset (to cover fees)
        
        Args:
            atr_5m: Current 5m ATR
            be_offset_atr: Offset in ATR units (default 0.1 = covers fees)
            
        Returns:
            New stop loss level if BE should be moved, None otherwise
        """
        if not self.is_pullback_short:
            return None
        
        if not self.tp1_hit:
            return None
        
        if self.pullback_be_moved:
            return None  # Already moved
        
        # For SHORT: BE is above entry (we want to lock profit)
        # BE = entry - offset (gives us some room for fees)
        offset = be_offset_atr * atr_5m
        be_level = self.entry_price - offset
        
        # Mark as moved
        self.pullback_be_moved = True
        
        logger.info(f"[PULLBACK] Moving SL to BE: {self.stop_loss:.2f} → {be_level:.2f} (entry - {offset:.2f})")
        
        return be_level
    
    def pullback_get_tp1_partial_size(self, tp1_close_pct: int = 50) -> float:
        """
        Get the size to close at TP1 for partial exit.
        
        Args:
            tp1_close_pct: Percentage to close at TP1 (default 50%)
            
        Returns:
            Size to close
        """
        if not self.is_pullback_short:
            return 0.0
        
        if self.pullback_tp1_partial_done:
            return 0.0  # Already done
        
        pct = tp1_close_pct / 100.0
        close_size = self.size * pct
        
        self.pullback_tp1_partial_done = True
        
        return close_size
    
    def should_force_exit_by_time(self, market_time: datetime | None = None, current_atr: float | None = None) -> tuple[bool, str]:
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

    # =========================================================================
    # SMART EXITS - 5 Advanced Exit Strategies (Jan 13, 2026)
    # =========================================================================
    
    def check_scaled_partial_exit(self) -> Optional[Dict]:
        """
        SMART EXIT #1: Scaled partial profit taking.
        
        Exit progressively at multiple R levels instead of binary TP1/TP2.
        This locks in profits while letting a portion run.
        
        Returns:
            Dict with exit details if should take partial, None otherwise
            {'action': 'PARTIAL_EXIT', 'exit_pct': 0.33, 'mfe_r': 0.8, 'reason': str}
        """
        if not SmartExits.SCALED_PARTIALS_ENABLED:
            return None
        
        mfe_r = self.calculate_mfe_r()
        
        # Check each scale-out level
        for level_r, exit_pct in SmartExits.SCALE_OUT_LEVELS:
            # Skip if we already exited at this level
            already_done = any(done_r == level_r for done_r, _ in self.partial_exits_done)
            if already_done:
                continue
            
            # Check if MFE reached this level
            if mfe_r >= level_r:
                # Check if we have enough position left
                if self.remaining_size_pct <= SmartExits.MIN_RUNNER_PCT:
                    logger.debug(f"📊 SCALED PARTIAL: Skip level {level_r}R - only {self.remaining_size_pct:.0%} remaining (min runner)")
                    continue
                
                # Calculate actual exit percentage (adjusted for remaining)
                actual_exit_pct = min(exit_pct, self.remaining_size_pct - SmartExits.MIN_RUNNER_PCT)
                
                if actual_exit_pct > 0.01:  # At least 1%
                    # Record this partial exit
                    self.partial_exits_done.append((level_r, actual_exit_pct))
                    self.remaining_size_pct -= actual_exit_pct
                    
                    logger.info(f"💰 SCALED PARTIAL: Exit {actual_exit_pct:.0%} @ MFE {mfe_r:.2f}R (level: {level_r}R) | Remaining: {self.remaining_size_pct:.0%}")
                    
                    return {
                        'action': 'PARTIAL_EXIT',
                        'exit_pct': actual_exit_pct,
                        'mfe_r': mfe_r,
                        'level_r': level_r,
                        'remaining_pct': self.remaining_size_pct,
                        'reason': f'Scaled partial at {level_r}R'
                    }
        
        return None
    
    def check_momentum_exit(self, rsi_1m: float, volume_ratio: float = 1.0) -> Optional[Dict]:
        """
        SMART EXIT #2: Exit when momentum dies BEFORE price reverses.
        
        Detects loss of momentum via RSI divergence from price.
        Only applies when trade is already in profit.
        
        Args:
            rsi_1m: Current RSI (1-minute)
            volume_ratio: Current volume / average volume
        
        Returns:
            Dict with exit details if momentum dying, None otherwise
        """
        if not SmartExits.MOMENTUM_EXIT_ENABLED:
            return None
        
        # Update last RSI
        self.last_rsi = rsi_1m
        
        mfe_r = self.calculate_mfe_r()
        
        # Only apply if we have minimum profit
        if mfe_r < SmartExits.MOMENTUM_EXIT_MIN_MFE_R:
            return None
        
        # Check momentum death based on direction
        momentum_dead = False
        reason = ""
        
        if self.direction == 'LONG':
            if rsi_1m < SmartExits.RSI_LONG_EXIT_THRESHOLD:
                momentum_dead = True
                reason = f"RSI {rsi_1m:.1f} < {SmartExits.RSI_LONG_EXIT_THRESHOLD} (LONG momentum dying)"
        else:
            if rsi_1m > SmartExits.RSI_SHORT_EXIT_THRESHOLD:
                momentum_dead = True
                reason = f"RSI {rsi_1m:.1f} > {SmartExits.RSI_SHORT_EXIT_THRESHOLD} (SHORT momentum dying)"
        
        # Optional: Check volume confirmation
        if momentum_dead and SmartExits.REQUIRE_VOLUME_DECLINE:
            if volume_ratio >= SmartExits.VOLUME_DECLINE_THRESHOLD:
                # Volume not declining, don't exit yet
                logger.debug(f"📊 MOMENTUM EXIT skipped: Volume ratio {volume_ratio:.2f} >= {SmartExits.VOLUME_DECLINE_THRESHOLD}")
                return None
            reason += f" + Volume declining ({volume_ratio:.0%})"
        
        if momentum_dead:
            logger.warning(f"📉 MOMENTUM EXIT: {reason} | MFE: {mfe_r:.2f}R")
            return {
                'action': 'MOMENTUM_EXIT',
                'rsi': rsi_1m,
                'mfe_r': mfe_r,
                'volume_ratio': volume_ratio,
                'reason': reason
            }
        
        return None
    
    def check_vpoc_exit(self, current_price: float, vpoc_price: float) -> Optional[Dict]:
        """
        SMART EXIT #3: Exit when approaching strong resistance/support (VPOC).
        
        CAMBIO (Jan 23, 2026): VPOC_EXIT ahora es PARCIAL + RUNNER
        - Cierra 70% de la posición (configurable)
        - Deja 30% como runner con stop en BE
        - Solo cierre total si MFE < 0.5R (no hay margen para runner)
        
        Volume Profile POC (Point of Control) represents strongest resistance.
        Exit PARTIAL before getting trapped, keep runner for continuation.
        
        Args:
            current_price: Current market price
            vpoc_price: Volume Profile Point of Control price
        
        Returns:
            Dict with exit details (partial or full), None otherwise
        """
        from src.config.trading_config import ExitHierarchy
        
        if not SmartExits.VPOC_EXIT_ENABLED:
            return None
        
        if vpoc_price is None or vpoc_price <= 0:
            return None
        
        mfe_r = self.calculate_mfe_r()
        
        # Only apply if we have minimum profit
        if mfe_r < SmartExits.VPOC_EXIT_MIN_MFE_R:
            return None
        
        # Calculate distance to VPOC
        distance_pct = abs(current_price - vpoc_price) / current_price
        
        # Check if within proximity threshold
        if distance_pct > SmartExits.VPOC_PROXIMITY_PCT:
            return None  # Not close enough
        
        # Check direction - are we approaching VPOC as resistance?
        approaching_resistance = False
        reason = ""
        
        if self.direction == 'LONG' and current_price < vpoc_price:
            # LONG approaching VPOC from below = resistance
            approaching_resistance = True
            reason = f"LONG approaching VPOC resistance @ {vpoc_price:.2f}"
        elif self.direction == 'SHORT' and current_price > vpoc_price:
            # SHORT approaching VPOC from above = support
            approaching_resistance = True
            reason = f"SHORT approaching VPOC support @ {vpoc_price:.2f}"
        
        if approaching_resistance:
            # ================================================================
            # NUEVO: VPOC_EXIT como parcial + runner (Jan 23, 2026)
            # ================================================================
            use_partial = ExitHierarchy.VPOC_EXIT_AS_PARTIAL
            min_mfe_for_runner = ExitHierarchy.VPOC_MIN_MFE_FOR_RUNNER
            
            if use_partial and mfe_r >= min_mfe_for_runner:
                # Suficiente profit para runner - hacer parcial
                partial_pct = ExitHierarchy.VPOC_PARTIAL_CLOSE_PCT
                runner_pct = ExitHierarchy.VPOC_RUNNER_PCT
                runner_stop_r = ExitHierarchy.VPOC_RUNNER_STOP_R
                
                logger.warning(
                    f"🎯 VPOC PARTIAL EXIT: {reason} | Distance: {distance_pct:.2%} | "
                    f"MFE: {mfe_r:.2f}R | Closing {partial_pct:.0%}, runner {runner_pct:.0%}"
                )
                return {
                    'action': 'VPOC_PARTIAL_EXIT',
                    'current_price': current_price,
                    'vpoc_price': vpoc_price,
                    'distance_pct': distance_pct,
                    'mfe_r': mfe_r,
                    'reason': reason,
                    'partial_pct': partial_pct,
                    'runner_pct': runner_pct,
                    'runner_stop_r': runner_stop_r,
                    'is_partial': True
                }
            else:
                # No hay suficiente profit para runner - cierre total
                logger.warning(
                    f"🎯 VPOC FULL EXIT: {reason} | Distance: {distance_pct:.2%} | "
                    f"MFE: {mfe_r:.2f}R < {min_mfe_for_runner}R (no runner margin)"
                )
                return {
                    'action': 'VPOC_EXIT',
                    'current_price': current_price,
                    'vpoc_price': vpoc_price,
                    'distance_pct': distance_pct,
                    'mfe_r': mfe_r,
                    'reason': reason,
                    'is_partial': False
                }
        
        return None
    
    def check_time_profit_lock(self) -> Optional[Dict]:
        """
        SMART EXIT #4: Lock profits after time threshold.
        
        If trade has been running for N candles with profit, move stop
        to lock in minimum R gain.
        
        Returns:
            Dict with lock details if triggered, None otherwise
        """
        if not SmartExits.TIME_PROFIT_LOCK_ENABLED:
            return None
        
        if self.profit_locked:
            return None  # Already locked
        
        # Check candle count
        if self.candle_count < SmartExits.TIME_LOCK_CANDLES:
            return None
        
        mfe_r = self.calculate_mfe_r()
        
        # Only lock if above minimum MFE
        if mfe_r < SmartExits.TIME_LOCK_MIN_MFE_R:
            return None
        
        # Calculate lock price (entry + lock_R * risk)
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        lock_r = SmartExits.TIME_LOCK_FLOOR_R
        
        if self.direction == 'LONG':
            lock_price = self.entry_price + (initial_risk * lock_r)
            # Only lock if current stop is below lock price
            if self.trailing_stop < lock_price:
                self.trailing_stop = lock_price
                self.stop_loss = lock_price
                self.profit_locked = True
                self.locked_r = lock_r
        else:
            lock_price = self.entry_price - (initial_risk * lock_r)
            # Only lock if current stop is above lock price
            if self.trailing_stop > lock_price:
                self.trailing_stop = lock_price
                self.stop_loss = lock_price
                self.profit_locked = True
                self.locked_r = lock_r
        
        if self.profit_locked:
            logger.info(f"🔒 TIME PROFIT LOCK: {self.candle_count} candles, MFE {mfe_r:.2f}R → Locked +{lock_r}R @ {lock_price:.2f}")
            return {
                'action': 'TIME_PROFIT_LOCK',
                'candles': self.candle_count,
                'mfe_r': mfe_r,
                'locked_r': lock_r,
                'lock_price': lock_price,
                'reason': f'Time lock after {self.candle_count} candles'
            }
        
        return None
    
    def check_volatility_squeeze_exit(self, bb_width: float, bb_avg_width: float, current_price: float) -> Optional[Dict]:
        """
        SMART EXIT #5: Exit during volatility squeeze.
        
        When Bollinger Bands compress (squeeze), market is indecisive.
        Take small profits before squeeze resolves (could go either way).
        
        Args:
            bb_width: Current Bollinger Band width (upper - lower)
            bb_avg_width: Average BB width (for comparison)
            current_price: Current market price (for calculating current R)
        
        Returns:
            Dict with exit details if in squeeze, None otherwise
        """
        if not SmartExits.VOLATILITY_SQUEEZE_EXIT_ENABLED:
            return None
        
        # Update tracking
        self.last_bb_width = bb_width
        self.avg_bb_width = bb_avg_width
        
        if bb_avg_width <= 0:
            return None
        
        # Calculate squeeze ratio
        squeeze_ratio = bb_width / bb_avg_width
        
        # Check if in squeeze
        if squeeze_ratio >= SmartExits.BB_SQUEEZE_RATIO:
            return None  # Not a squeeze
        
        mfe_r = self.calculate_mfe_r()
        
        # Calculate current R (actual PnL right now)
        initial_risk = abs(self.entry_price - self.stop_loss)
        if initial_risk > 0:
            if self.direction == 'LONG':
                current_r = (current_price - self.entry_price) / initial_risk
            else:
                current_r = (self.entry_price - current_price) / initial_risk
        else:
            current_r = 0.0
        
        # Only exit if in the "uncertain profit" zone (based on MFE)
        if mfe_r < SmartExits.SQUEEZE_EXIT_MIN_MFE_R:
            return None  # Not enough profit reached, hold
        
        if mfe_r > SmartExits.SQUEEZE_EXIT_MAX_MFE_R:
            return None  # Good profit, let it run
        
        # 🔒 FIX (Jan 22, 2026): Don't exit in squeeze if we've already given back profit
        # If current_r is significantly negative, we've missed the exit window
        # Better to hold for breakout or let SL handle it
        if current_r < -0.2:  # Already down 0.2R or more
            logger.debug(f"📦 SQUEEZE: Skipping exit, current_r={current_r:.2f} too negative (gave back profit)")
            return None
        
        # 🔒 FIX (Jan 22, 2026): Don't call it "small profit" if current R is negative
        if current_r >= 0:
            profit_status = f"MFE {mfe_r:.2f}R, current +{current_r:.2f}R"
        else:
            profit_status = f"MFE was {mfe_r:.2f}R, now {current_r:.2f}R (gave back profit)"
        
        reason = f"Volatility squeeze (BB width {squeeze_ratio:.0%} of avg) | {profit_status}"
        logger.warning(f"📦 SQUEEZE EXIT: {reason}")
        
        return {
            'action': 'VOLATILITY_SQUEEZE_EXIT',
            'bb_width': bb_width,
            'bb_avg_width': bb_avg_width,
            'squeeze_ratio': squeeze_ratio,
            'mfe_r': mfe_r,
            'current_r': current_r,
            'reason': reason
        }
    
    def check_rsi_divergence_exit(
        self,
        current_price: float,
        rsi_values: list | np.ndarray,
        price_values: list | np.ndarray | None = None,
        lookback: int = 10
    ) -> Optional[Dict]:
        """
        RSI Divergence Exit - Exit when RSI diverges from price.
        
        Only applies when:
        1. MFE >= 1R (already profitable)
        2. Divergence is confirmed (multiple lower RSI highs for LONG)
        
        Args:
            current_price: Current market price
            rsi_values: List of recent RSI values (newest last)
            price_values: List of recent prices (newest last), if None will skip divergence check
            lookback: Number of candles to check
        
        Returns:
            Dict with divergence details if detected, None otherwise
        """
        from src.config.trading_config import DivergenceExitConfig
        
        if not DivergenceExitConfig.ENABLED:
            return None
        
        if price_values is None:
            return None
        
        mfe_r = self.calculate_mfe_r()
        
        # Only apply if we have minimum profit
        if mfe_r < DivergenceExitConfig.MIN_MFE_R:
            return None
        
        if len(rsi_values) < lookback or len(price_values) < lookback:
            return None
        
        # Get last N values
        rsi_recent = rsi_values[-lookback:]
        price_recent = price_values[-lookback:]
        
        # Detect divergence
        divergence_detected = False
        reason = ""
        
        if self.direction == 'LONG':
            # Bearish divergence: Higher highs in price, lower highs in RSI
            price_making_higher_high = max(price_recent[-3:]) >= max(price_recent[:-3])
            rsi_making_lower_high = max(rsi_recent[-3:]) < max(rsi_recent[:-3])
            
            if price_making_higher_high and rsi_making_lower_high:
                divergence_detected = True
                reason = f"Bearish RSI divergence: price HH but RSI LH (MFE={mfe_r:.2f}R)"
        
        else:  # SHORT
            # Bullish divergence: Lower lows in price, higher lows in RSI
            price_making_lower_low = min(price_recent[-3:]) <= min(price_recent[:-3])
            rsi_making_higher_low = min(rsi_recent[-3:]) > min(rsi_recent[:-3])
            
            if price_making_lower_low and rsi_making_higher_low:
                divergence_detected = True
                reason = f"Bullish RSI divergence: price LL but RSI HL (MFE={mfe_r:.2f}R)"
        
        if divergence_detected:
            logger.warning(f"📉 DIVERGENCE EXIT: {reason}")
            
            return {
                'action': 'DIVERGENCE_EXIT',
                'mfe_r': mfe_r,
                'reason': reason,
                'partial_close_pct': DivergenceExitConfig.PARTIAL_CLOSE_PCT,
                'tighten_stop_to_r': DivergenceExitConfig.TIGHTEN_STOP_TO_R
            }
        
        return None
    
    def get_volatility_regime(self, atr_ratio: float) -> str:
        """
        Get current volatility regime based on ATR ratio.
        
        Args:
            atr_ratio: current_atr / sma(atr, 100)
        
        Returns:
            Regime name: LOW_VOL, NORMAL, HIGH_VOL, or SPIKE
        """
        from src.config.trading_config import VolatilityRegime
        return VolatilityRegime.classify(atr_ratio)
    
    def get_tp1_percentage(self, regime: str) -> int:
        """
        Get TP1 close percentage based on market regime.
        
        Args:
            regime: Market regime (RANGE, TREND, etc.)
        
        Returns:
            Percentage to close at TP1 (e.g., 40, 50, 60)
        """
        from src.config.trading_config import TP1PercentageConfig
        return TP1PercentageConfig.get_tp1_pct(regime)
    
    def update_smart_exit_indicators(self, rsi: float | None = None, bb_width: float | None = None, 
                                      bb_avg: float | None = None, volume_ratio: float | None = None):
        """
        Update indicators used by smart exits.
        Call this on each candle to keep values fresh.
        
        Args:
            rsi: Current RSI value
            bb_width: Current Bollinger Band width
            bb_avg: Average BB width
            volume_ratio: Current volume / average volume
        """
        if rsi is not None:
            self.last_rsi = rsi
        if bb_width is not None:
            self.last_bb_width = bb_width
        if bb_avg is not None:
            self.avg_bb_width = bb_avg
    
    def check_all_smart_exits(self, current_price: float, rsi: float | None = None, 
                               vpoc: float | None = None, bb_width: float | None = None,
                               bb_avg: float | None = None, volume_ratio: float = 1.0,
                               rsi_values: np.ndarray | None = None) -> Optional[Dict]:
        """
        Master method: Check all smart exit conditions in priority order.
        
        Returns first triggered exit or None.
        
        Args:
            current_price: Current market price
            rsi: Current RSI (1m)
            vpoc: VPOC price level
            bb_width: Current BB width
            bb_avg: Average BB width
            volume_ratio: Volume ratio (current/avg)
            rsi_values: Array of recent RSI values for divergence detection
        
        Returns:
            Dict with highest priority exit trigger, or None
        """
        # Update indicators
        self.update_smart_exit_indicators(rsi, bb_width, bb_avg, volume_ratio)
        
        # Priority 1: Time-based profit lock (moves stop, doesn't exit)
        lock_result = self.check_time_profit_lock()
        # Note: This modifies stop, doesn't trigger full exit
        
        # Priority 2: Scaled partial exits
        partial_result = self.check_scaled_partial_exit()
        if partial_result:
            return partial_result
        
        # Priority 3: RSI Divergence exit (NEW - Jan 16, 2026)
        # Takes partial profit when RSI shows divergence at MFE >= 1R
        if rsi_values is not None and len(rsi_values) >= 5:
            divergence_result = self.check_rsi_divergence_exit(
                current_price=current_price,
                rsi_values=rsi_values
            )
            if divergence_result:
                return divergence_result
        
        # Priority 4: Momentum exit
        if rsi is not None:
            momentum_result = self.check_momentum_exit(rsi, volume_ratio)
            if momentum_result:
                return momentum_result
        
        # Priority 5: VPOC exit
        if vpoc is not None and vpoc > 0:
            vpoc_result = self.check_vpoc_exit(current_price, vpoc)
            if vpoc_result:
                return vpoc_result
        
        # Priority 6: Volatility squeeze exit
        if bb_width is not None and bb_avg is not None:
            squeeze_result = self.check_volatility_squeeze_exit(bb_width, bb_avg, current_price)
            if squeeze_result:
                return squeeze_result
        
        return None


class RiskEngine:
    """
    Manages position sizing, risk limits, and exit management
    
    UPDATED (Jan 23, 2026):
    - Anti-reentry tracking: No reentry without new edge
    - Worst-trade prevention: Cap losses when thesis intact
    - Countertrend mode specific rules
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
        
        # =====================================================================
        # ANTI RE-ENTRY TRACKING (Jan 23, 2026)
        # Track last entry to prevent reentry without new edge
        # =====================================================================
        self.last_entry_price: Optional[float] = None
        self.last_entry_direction: Optional[str] = None
        self.last_entry_atr: Optional[float] = None
        self.last_entry_time: Optional[datetime] = None
        self.last_exit_reason: Optional[str] = None
        self.last_entry_conditions: dict = {}  # Store entry conditions
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    def check_reentry_allowed(self, entry_price: float, direction: str, 
                               atr: float, new_conditions: dict | None = None) -> dict:
        """
        ANTI RE-ENTRY: Check if reentry is allowed at this price.
        
        Prohibir reentrar en mismo rango de precio sin nueva información:
        - Mismo precio = ±0.2 ATR del último entry
        - Misma dirección
        - Sin nuevo edge (LH, breakdown+retest, nuevo POC)
        
        Args:
            entry_price: Proposed entry price
            direction: Proposed direction
            atr: Current ATR for range calculation
            new_conditions: Dict of current market conditions
        
        Returns:
            dict with 'allowed': bool and 'reason': str
        """
        from src.config.trading_config import ExitHierarchy
        
        if not ExitHierarchy.ANTI_REENTRY_ENABLED:
            return {'allowed': True, 'reason': 'Anti-reentry disabled'}
        
        # No previous entry to compare
        if self.last_entry_price is None:
            return {'allowed': True, 'reason': 'No previous entry'}
        
        # Different direction is always allowed
        if direction != self.last_entry_direction:
            return {'allowed': True, 'reason': 'Different direction'}
        
        # Calculate price range (±0.2 ATR)
        use_atr = self.last_entry_atr if self.last_entry_atr else atr
        price_range = use_atr * ExitHierarchy.SAME_PRICE_RANGE_ATR
        
        price_diff = abs(entry_price - self.last_entry_price)
        
        # If price is outside the "same price" range, allowed
        if price_diff > price_range:
            return {
                'allowed': True, 
                'reason': f'Price moved {price_diff:.2f} > {price_range:.2f} (0.2 ATR)'
            }
        
        # Same price range - check for new edge
        if new_conditions and ExitHierarchy.REQUIRE_NEW_EDGE_FOR_REENTRY:
            new_edge = self._check_new_edge(new_conditions)
            if new_edge['has_new_edge']:
                return {
                    'allowed': True,
                    'reason': f"New edge detected: {new_edge['edge_type']}"
                }
        
        # No new edge, same price = blocked
        return {
            'allowed': False,
            'reason': (
                f"BLOCKED: Same price range ({entry_price:.2f} vs last {self.last_entry_price:.2f}), "
                f"same direction ({direction}), no new edge detected"
            )
        }
    
    def _check_new_edge(self, conditions: dict) -> dict:
        """
        Check if there's new edge information vs last entry.
        
        New edge conditions:
        - new_lh_confirmed: New Lower High for shorts
        - breakdown_retest: Breakdown + retest of structure
        - new_poc_displacement: New POC / value area shift
        - volume_regime_change: Significant volume change
        """
        has_edge = False
        edge_type = None
        
        # Check each condition against stored conditions
        if conditions.get('new_lh_confirmed') and not self.last_entry_conditions.get('lh_price') == conditions.get('lh_price'):
            has_edge = True
            edge_type = 'NEW_LH_CONFIRMED'
        
        elif conditions.get('breakdown_retest'):
            has_edge = True
            edge_type = 'BREAKDOWN_RETEST'
        
        elif conditions.get('poc_price') and self.last_entry_conditions.get('poc_price'):
            poc_diff = abs(conditions['poc_price'] - self.last_entry_conditions['poc_price'])
            if poc_diff > conditions.get('atr', 0) * 0.5:  # POC moved > 0.5 ATR
                has_edge = True
                edge_type = 'NEW_POC_DISPLACEMENT'
        
        elif conditions.get('volume_regime_change'):
            has_edge = True
            edge_type = 'VOLUME_REGIME_CHANGE'
        
        return {
            'has_new_edge': has_edge,
            'edge_type': edge_type
        }
    
    def record_entry(self, entry_price: float, direction: str, atr: float,
                     entry_time: datetime | None = None, conditions: dict | None = None):
        """
        Record entry details for anti-reentry tracking.
        Called after successful position open.
        """
        self.last_entry_price = entry_price
        self.last_entry_direction = direction
        self.last_entry_atr = atr
        self.last_entry_time = entry_time or datetime.now(UTC)
        self.last_entry_conditions = conditions or {}
    
    def record_exit(self, exit_reason: str):
        """Record exit reason for cooldown/reentry logic."""
        self.last_exit_reason = exit_reason
    
    def get_current_stop_loss(self) -> float | None:
        """
        Get the current stop loss price for the active position.
        
        Returns:
            float: Current SL price, or None if no position
        """
        if self.position is None:
            return None
        return self.position.current_stop_loss
    
    def create_position(self, symbol: str | None, direction: str | None, entry_price: float,
                       position_size: float, stop_loss: float, atr_1m: float,
                       market_time: datetime | None = None,
                       trade_type: TradeType = TradeType.LONG_MAIN,
                       regime_mode: str = 'RANGE',
                       entry_conditions: dict | None = None) -> Position:  # 🔥 FIX (Jan 23): Added entry_conditions
        """
        Create a new position object for tracking.
        
        UPDATED (Jan 23, 2026): Also records entry for anti-reentry tracking
        """
        self.position = Position(
            symbol=symbol or 'UNKNOWN',
            direction=direction or 'UNKNOWN',
            entry_price=entry_price,
            size=position_size,
            stop_loss=stop_loss,
            atr_1m=atr_1m,
            market_time=market_time,
            trade_type=trade_type,
            regime_mode=regime_mode  # 🔥 Pass regime for trailing
        )
        
        # Record entry for anti-reentry tracking
        if direction is not None:
            self.record_entry(
                entry_price=entry_price,
                direction=direction,
                atr=atr_1m,
                entry_time=market_time,
                conditions=entry_conditions
            )
        
        return self.position
    
    async def sync_position_size(self, new_size: float, reason: str = "exchange sync") -> bool:
        """
        🔒 Thread-safe update of position size.
        
        This method should be used instead of direct assignment to position.size
        to prevent race conditions between multiple coroutines.
        
        Args:
            new_size: New position size from exchange
            reason: Reason for the sync (for logging)
            
        Returns:
            True if sync was successful, False if no position exists
        """
        async with self._lock:
            if self.position is None:
                return False
            
            old_size = self.position.size
            if abs(old_size - new_size) > 0.00001:  # Only log if actually changed
                self.position.size = new_size
                logger.info(f"🔄 Position size sync ({reason}): {old_size:.6f} → {new_size:.6f}")
            return True
    
    async def clear_position(self, reason: str = "position closed") -> bool:
        """
        🔒 Thread-safe clearing of position.
        
        This method should be used instead of direct assignment to self.position = None
        to prevent race conditions between multiple coroutines.
        
        Args:
            reason: Reason for clearing (for logging)
            
        Returns:
            True if position was cleared, False if already None
        """
        async with self._lock:
            if self.position is None:
                logger.debug(f"Position already None: {reason}")
                return False
            
            logger.info(f"🔄 Position cleared ({reason})")
            self.position = None
            return True
    
    def clear_position_sync(self, reason: str = "position closed") -> bool:
        """
        Synchronous version for use in callbacks.
        WARNING: Not thread-safe, use clear_position() when possible.
        """
        if self.position is None:
            return False
        
        logger.info(f"🔄 Position cleared (sync: {reason})")
        self.position = None
        return True
    
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
    
    def calculate_stop_loss(self, entry_price: float, direction: str | None, 
                           support_resistance: float, atr_1m: float,
                           atr_ratio: float | None = None,
                           regime_atr_mult: float | None = None) -> float:
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
    
    async def open_position(self, symbol: str | None, direction: str | None, entry_price: float,
                           stop_loss: float, atr_1m: float, hour: int, minute: int) -> Position | None:
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
            self.position = Position(symbol or 'UNKNOWN', direction or 'UNKNOWN', entry_price, size, stop_loss, atr_1m)
            
            ORDERS_RECOMMENDED.labels(symbol=symbol or 'UNKNOWN', direction=direction or 'UNKNOWN').inc()
            
            return self.position
    
    async def manage_position(self, current_price: float, atr_1m: float, 
                             volume: float, avg_volume: float,
                             tp2_target: float | None = None,
                             ema20_5m: float | None = None,
                             has_bos_against: bool = False,
                             market_time: datetime | None = None,
                             adx: Optional[float] = None,
                             atr_ratio: Optional[float] = None,
                             rsi_1m: Optional[float] = None,
                             vpoc: Optional[float] = None,
                             bb_width: Optional[float] = None,
                             bb_avg: Optional[float] = None,
                             # 🔥 ANOMALY DETECTION PARAMS (Jan 24, 2026)
                             spread_bps: Optional[float] = None,
                             spread_normal_bps: float = 3.0,
                             last_tick_age_sec: Optional[float] = None,
                             price_gap_pct: Optional[float] = None) -> Optional[dict]:
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
            # 🔥 ANOMALY-BASED EMERGENCY EXIT (Jan 24, 2026)
            # Instead of fixed -0.25R limit that kills valid trades,
            # detect OPERATIONAL ANOMALIES that indicate system/market issues
            # where SL might fail to execute properly.
            # 
            # ANOMALY TRIGGERS:
            # 1. Spread explosion (> 5x normal) = liquidity gone
            # 2. Price gap (> 0.3% in 1 tick) = flash crash/spike
            # 3. Feed stale (> 10s without tick) = connection issue
            # 4. ATR explosion (> 4x normal) = extreme volatility
            # 
            # BACKSTOP: -1.0R hard limit as final safety net
            # ================================================================
            current_r = pos.current_r_multiple
            
            # --- ANOMALY DETECTION ---
            anomaly_detected = False
            anomaly_reason = ""
            
            # 1. Spread anomaly (liquidity evaporated)
            if spread_bps is not None and spread_normal_bps > 0:
                spread_ratio = spread_bps / spread_normal_bps
                if spread_ratio > 5.0:  # > 5x normal spread
                    anomaly_detected = True
                    anomaly_reason = f"SPREAD_ANOMALY (spread={spread_bps:.1f}bps, {spread_ratio:.1f}x normal)"
            
            # 2. Price gap anomaly (flash crash/spike)
            if price_gap_pct is not None and abs(price_gap_pct) > 0.3:
                anomaly_detected = True
                anomaly_reason = f"PRICE_GAP_ANOMALY (gap={price_gap_pct:.2f}%)"
            
            # 3. Feed stale anomaly (connection issues)
            if last_tick_age_sec is not None and last_tick_age_sec > 10.0:
                anomaly_detected = True
                anomaly_reason = f"FEED_STALE_ANOMALY (age={last_tick_age_sec:.1f}s)"
            
            # 4. ATR explosion (extreme volatility)
            if atr_ratio is not None and atr_ratio > 4.0:
                anomaly_detected = True
                anomaly_reason = f"VOLATILITY_ANOMALY (ATR_ratio={atr_ratio:.1f}x)"
            
            # EMERGENCY EXIT ON ANOMALY (any loss triggers)
            if anomaly_detected and current_r < 0:
                logger.warning(
                    f"🚨 EMERGENCY ANOMALY EXIT: {anomaly_reason} | R={current_r:.3f} | "
                    f"Closing to prevent SL failure"
                )
                return await self._close_position(current_price, f'EMERGENCY_ANOMALY ({anomaly_reason})')
            
            # HARD BACKSTOP: -1.0R absolute limit (catastrophic protection)
            HARD_BACKSTOP_R = -1.0
            if current_r < HARD_BACKSTOP_R:
                logger.error(
                    f"🚨🚨 HARD BACKSTOP EXIT: current_r={current_r:.3f}R < limit={HARD_BACKSTOP_R}R | "
                    f"CRITICAL: Trade exceeded -1R, closing IMMEDIATELY"
                )
                return await self._close_position(current_price, f'HARD_BACKSTOP (R={current_r:.3f})')
            
            # ================================================================
            # 🔥 FIX (Jan 23, 2026): MANAGEMENT COOLDOWN - Prevent action spam
            # After executing a management action, skip checks for N seconds
            # This prevents cascading partial closes and race conditions
            # ================================================================
            if pos.last_management_action_time is not None:
                cooldown_elapsed = (datetime.now(UTC) - pos.last_management_action_time).total_seconds()
                if cooldown_elapsed < pos.MANAGEMENT_COOLDOWN_SEC:
                    # Still in cooldown - skip management checks, only check critical SL
                    logger.debug(f"⏸️ Management cooldown: {cooldown_elapsed:.1f}s / {pos.MANAGEMENT_COOLDOWN_SEC}s - only checking SL")
                    # Only check stop loss during cooldown (critical safety)
                    sl_check = pos.check_stop_hit(current_price, market_time=market_time)
                    if sl_check:
                        return await self._close_position(current_price, 'STOP_LOSS')
                    return None  # Skip all other management during cooldown
            
            # ================================================================
            # PASO 3: Check promoción a RUNNER (antes de cualquier exit)
            # ================================================================
            pos.check_regime_promotion()
            
            # Update trailing stop (solo aplica en régimen NORMAL)
            if market_time is not None:
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
                # ============================================================
                # SMART EXITS (Jan 13, 2026) - Advanced profit/exit management
                # Check BEFORE normal exits to capture more profit
                # ============================================================
                volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
                
                smart_exit = pos.check_all_smart_exits(
                    current_price=current_price,
                    rsi=rsi_1m,
                    vpoc=vpoc,
                    bb_width=bb_width,
                    bb_avg=bb_avg,
                    volume_ratio=volume_ratio
                )
                
                if smart_exit:
                    action_type = smart_exit.get('action', '')
                    
                    # Handle partial exits (scaled profit taking)
                    if action_type == 'PARTIAL_EXIT':
                        exit_pct = smart_exit.get('exit_pct', 0)
                        if exit_pct > 0:
                            close_size = pos.size * exit_pct
                            remaining_size = pos.size - close_size
                            
                            # 🔒 FIX (Jan 14, 2026): Validate minimum order sizes
                            if close_size < KRAKEN_BTC_MIN_SIZE:
                                logger.info(f"🔄 SMART PARTIAL skipped: close_size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                                # Don't return, let normal exit logic continue below
                            elif remaining_size > 0 and remaining_size < KRAKEN_BTC_MIN_SIZE:
                                # Remaining too small - close ALL instead
                                logger.info(f"🔄 SMART PARTIAL: Remaining {remaining_size:.6f} < min, closing FULL")
                                return await self._close_position(current_price, 'SCALED_PARTIAL_FULL')
                            else:
                                reason = smart_exit.get('reason', 'SCALED_PARTIAL')
                                logger.info(f"💰 SMART PARTIAL: Closing {exit_pct:.0%} ({close_size:.6f}) - {reason}")
                                return {
                                    'action': 'PARTIAL_CLOSE',
                                    'size': close_size,
                                    'reason': reason,
                                    'mfe_r': smart_exit.get('mfe_r', 0)
                                }
                    
                    # Handle full exit triggers (momentum, VPOC, squeeze)
                    # 🔥 FIX (Jan 23, 2026): Handle VPOC_PARTIAL_EXIT
                    elif action_type == 'VPOC_PARTIAL_EXIT':
                        is_partial = smart_exit.get('is_partial', False)
                        if is_partial:
                            partial_pct = smart_exit.get('partial_pct', 0.7)
                            close_size = pos.size * partial_pct
                            remaining_size = pos.size - close_size
                            
                            if close_size < KRAKEN_BTC_MIN_SIZE:
                                logger.info(f"🔄 VPOC PARTIAL skipped: close_size {close_size:.6f} < min")
                            elif remaining_size > 0 and remaining_size < KRAKEN_BTC_MIN_SIZE:
                                logger.info(f"🔄 VPOC: Remaining too small, closing FULL")
                                return await self._close_position(current_price, 'VPOC_EXIT')
                            else:
                                runner_stop_r = smart_exit.get('runner_stop_r', 0.0)
                                reason = smart_exit.get('reason', 'VPOC_PARTIAL')
                                logger.info(f"💰 VPOC PARTIAL: Closing {partial_pct:.0%}, runner stop @ {runner_stop_r}R")
                                return {
                                    'action': 'PARTIAL_CLOSE',
                                    'size': close_size,
                                    'reason': 'VPOC_PARTIAL_EXIT',
                                    'mfe_r': smart_exit.get('mfe_r', 0),
                                    'move_stop_to_be': True,  # Move runner stop to BE
                                    'runner_stop_r': runner_stop_r
                                }
                        else:
                            return await self._close_position(current_price, 'VPOC_EXIT')
                    
                    elif action_type in ('MOMENTUM_EXIT', 'VPOC_EXIT', 'VOLATILITY_SQUEEZE_EXIT'):
                        reason = smart_exit.get('reason', action_type)
                        logger.info(f"🚀 SMART EXIT: {action_type} - {reason}")
                        return await self._close_position(current_price, action_type)
                
                # 0. Check mediocre trade exit (TIME_INVALIDATION INTELIGENTE)
                # 🔥 FIX (Jan 23, 2026): Now returns dict with action, not bool
                time_check = pos.check_mediocre_trade_exit(
                    n_candles=6, 
                    adx=adx, 
                    atr_ratio=atr_ratio,
                    current_price=current_price,
                    vwap=None,  # TODO: Pass VWAP from signal_engine
                    last_lh=None,  # TODO: Pass from structure analysis
                    vah=None  # TODO: Pass from volume profile
                )
                
                if time_check:
                    time_action = time_check.get('action', '')
                    time_reason = time_check.get('reason', 'TIME_INVALIDATION')
                    time_current_r = time_check.get('current_r', 0)
                    
                    # =====================================================================
                    # 🚨 AUDIT CRÍTICO: TIME_INVALIDATION nunca puede cerrar peor que STOP
                    # Si vemos R < stop_R, hay un bug grave en el sistema
                    # =====================================================================
                    stop_r = -1.0  # El stop debería ser -1R por diseño
                    if time_action == 'CLOSE' and time_current_r < stop_r - 0.1:
                        # ¡ALERTA! Cerrando peor que el stop loss teórico
                        logger.critical(
                            f"🚨🚨🚨 ILLEGAL TIME EXIT BELOW STOP 🚨🚨🚨\n"
                            f"   Exit R: {time_current_r:.2f}R\n"
                            f"   Stop R: {stop_r:.2f}R\n"
                            f"   Entry: {pos.entry_price:.2f}\n"
                            f"   SL: {pos.initial_stop_loss:.2f}\n"
                            f"   Current: {current_price:.2f}\n"
                            f"   REASON: {time_reason}\n"
                            f"   ACTION: Check stop_loss placement or sync with exchange!"
                        )
                        # NO cerrar - algo está mal, dejar que el SL real actúe
                        # El SL de exchange debería proteger
                        return None
                    
                    if time_action == 'CLOSE':
                        logger.warning(f"☠️ TIME_INVALIDATION: {time_reason}")
                        return await self._close_position(current_price, time_reason)
                    
                    elif time_action == 'PARTIAL':
                        partial_pct = time_check.get('partial_pct', 0.6)
                        close_size = pos.size * partial_pct
                        remaining_size = pos.size - close_size
                        
                        if close_size >= KRAKEN_BTC_MIN_SIZE and remaining_size >= KRAKEN_BTC_MIN_SIZE:
                            # 🔥 FIX (Jan 23, 2026): Mark partial as DONE to prevent cascading
                            pos.time_checkpoint_profit_partial_done = True
                            pos.last_management_action_time = datetime.now(UTC)
                            logger.info(f"⏱️ TIME IN PROFIT: Closing {partial_pct:.0%}, keeping runner (MARKED DONE)")
                            return {
                                'action': 'PARTIAL_CLOSE',
                                'size': close_size,
                                'reason': 'TIME_IN_PROFIT_PARTIAL',
                                'current_r': time_check.get('current_r', 0),
                                'keep_runner': True
                            }
                        else:
                            # Can't do partial due to min size - mark as done anyway to prevent spam
                            pos.time_checkpoint_profit_partial_done = True
                            logger.info(f"⏱️ TIME checkpoint: Can't do partial (size too small), holding runner")
                    
                    elif time_action == 'REDUCE_RISK':
                        # Move stop closer but don't close
                        move_to_r = time_check.get('move_stop_to_r', -0.15)
                        initial_risk = abs(pos.entry_price - pos.initial_stop_loss)
                        if pos.direction == 'LONG':
                            new_stop = pos.entry_price - (abs(move_to_r) * initial_risk)
                            if new_stop > pos.stop_loss:  # Only tighten, never loosen
                                pos.stop_loss = new_stop
                                pos.trailing_stop = new_stop
                                logger.info(f"⏱️ TIME REDUCE_RISK: Stop moved to {new_stop:.2f} ({move_to_r}R)")
                        else:
                            new_stop = pos.entry_price + (abs(move_to_r) * initial_risk)
                            if new_stop < pos.stop_loss:  # Only tighten
                                pos.stop_loss = new_stop
                                pos.trailing_stop = new_stop
                                logger.info(f"⏱️ TIME REDUCE_RISK: Stop moved to {new_stop:.2f} ({move_to_r}R)")
                    
                    elif time_action == 'HOLD':
                        # Market alive, keep holding
                        logger.debug(f"⏱️ TIME checkpoint: Market alive, holding")
                        # No action needed, continue
                
                # 0.5. ADD TO WINNER: Scale into proven winners (MFE >= 0.6R, no -0.2R touch)
                add_action = pos.check_add_to_winner(current_price)
                if add_action:
                    # Return add action to signal_engine for execution
                    return add_action
                
                # ============================================================
                # 0.75. WORST-TRADE PREVENTION (Jan 23, 2026)
                # If thesis intact but hitting loss cap, exit early
                # FIX Jan 27, 2026: Pass market_time for backtest compatibility
                # ============================================================
                worst_trade_check = pos.check_worst_trade_prevention(current_price, market_time=market_time)
                if worst_trade_check and worst_trade_check.get('action') == 'CLOSE':
                    reason = worst_trade_check.get('reason', 'WORST_TRADE_PREVENTION')
                    logger.warning(f"🛡️ WORST-TRADE PREVENTION: {worst_trade_check.get('message', '')}")
                    return await self._close_position(current_price, reason)
                
                # 1. Check stop loss (with grace period for first 10 min)
                sl_check = pos.check_stop_hit(current_price, market_time=market_time)
                if sl_check:
                    return await self._close_position(current_price, 'STOP_LOSS')
                
                # 2. Check TP1 (partial close, promotes toward RUNNER)
                if pos.check_tp1_hit(current_price):
                    # Determine volatility regime for dynamic TP1%
                    from src.config.trading_config import VolatilityRegime
                    vol_regime = None
                    if atr_ratio is not None:
                        vol_regime = VolatilityRegime.from_atr_ratio(atr_ratio)
                        vol_regime = vol_regime.name  # Get string name
                    return await self._handle_tp1(current_price, regime=vol_regime)
                
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
    
    async def _handle_tp1(self, current_price: float, regime: str | None = None) -> dict | None:
        """Handle TP1 hit - close dynamic % and move stop to BE immediately"""
        pos = self.position
        if pos is None:
            return None
        
        # Get dynamic TP1 percentage based on regime
        if regime:
            tp1_pct = pos.get_tp1_percentage(regime)
        else:
            tp1_pct = RiskManagement.TP1_CLOSE_PCT
        
        # Calculate partial close
        close_size = pos.size * (tp1_pct / 100.0)
        remaining_size = pos.size - close_size
        
        # 🔒 FIX (Jan 14, 2026): Validate minimum order sizes
        # If partial close would leave remaining below minimum, close ALL
        if remaining_size < KRAKEN_BTC_MIN_SIZE:
            logger.info(f"🔄 TP1: Remaining {remaining_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}, closing FULL position")
            return await self._close_position(current_price, 'TP1_FULL')
        
        # If close_size below minimum, skip partial and let TP2 handle full close
        if close_size < KRAKEN_BTC_MIN_SIZE:
            logger.info(f"🔄 TP1: Close size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}, skipping partial")
            pos.tp1_hit = True  # Mark as hit but don't close
            return None
        
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
        
        logger.info(f"🎯 TP1 HIT - Closed {close_size:.4f} ({tp1_pct}%) @ {current_price:.2f} | PnL: ${pnl:.2f} | Remaining: {remaining_size:.4f}")
        logger.info(f"🔒 SL moved to BE: {old_sl:.2f} → {pos.entry_price:.2f}")
        
        return {
            'action': 'PARTIAL_CLOSE',
            'reason': 'TP1',
            'price': current_price,
            'size': close_size,
            'pnl': pnl,
            'remaining_size': remaining_size,
            'tp1_pct': tp1_pct,
            'requires_validation': True  # Flag for validation
        }
    
    async def validate_partial_close(
        self, 
        execution_engine,
        symbol: str,
        expected_remaining: float,
        tolerance_pct: float = 0.5
    ) -> bool:
        """
        Validate position size after partial close.
        
        CRITICAL: After TP1 partial close, verify exchange position matches
        our expected remaining size. Fix discrepancies.
        
        Args:
            execution_engine: RESTExecutionEngine instance
            symbol: Trading pair symbol
            expected_remaining: Expected remaining position size
            tolerance_pct: Tolerance for mismatch (default 0.5%)
        
        Returns:
            True if position matches or was corrected, False on error
        """
        from src.config.trading_config import PartialCloseValidation
        
        if not PartialCloseValidation.ENABLED:
            return True
        
        try:
            # Wait for fill to settle
            import asyncio
            await asyncio.sleep(PartialCloseValidation.SYNC_DELAY_MS / 1000.0)
            
            # Get actual position from exchange
            position = await execution_engine.get_position(symbol)
            
            if not position:
                logger.error("❌ PARTIAL VALIDATION: No position found on exchange!")
                return False
            
            actual_size = abs(position.get('contracts', 0))
            
            # Calculate tolerance
            tolerance = expected_remaining * (tolerance_pct / 100.0)
            size_diff = abs(actual_size - expected_remaining)
            
            if size_diff <= tolerance or size_diff <= PartialCloseValidation.SIZE_EPSILON:
                logger.info(f"✅ PARTIAL VALIDATION: Expected {expected_remaining:.6f}, got {actual_size:.6f} (diff: {size_diff:.8f})")
                return True
            
            # Mismatch detected
            logger.warning(f"⚠️ PARTIAL MISMATCH: Expected {expected_remaining:.6f}, got {actual_size:.6f} (diff: {size_diff:.6f})")
            
            if PartialCloseValidation.RECALCULATE_LEVELS:
                # Update internal position to match exchange
                await self.sync_position_size(actual_size, "partial close correction")
                
                # Recalculate TP2 and trailing based on new size
                if self.position:
                    logger.info(f"🔄 Recalculating TP2/trailing for corrected size {actual_size:.6f}")
                    # TP2 and trailing are price-based, not size-based, so no recalc needed
                    # But we log for awareness
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error validating partial close: {e}")
            return False
    
    async def _close_position(self, current_price: float, reason: str) -> dict:
        """Close entire position and update equity/drawdown"""
        pos = self.position
        
        # Guard: position must exist
        if pos is None:
            return {'action': 'NONE', 'reason': 'No position to close'}
        
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
    
    async def update_from_exchange_position(self, position_data: dict, atr: float | None = None):
        """
        Recover internal state from an existing on-chain position
        position_data: dict from CCXT fetch_position
        atr: optional ATR value for proper SL calculation
        
        CCXT unified position structure:
        - 'side': 'long' or 'short' (most reliable for direction)
        - 'contracts': can be signed (+ long, - short) or unsigned depending on exchange
        - 'info': raw exchange response
        """
        # Minimum position size to consider valid (prevents float precision issues)
        MIN_POSITION_SIZE = 0.00001
        
        async with self._lock:
            try:
                symbol = position_data.get('symbol', 'UNKNOWN')
                contracts = float(position_data.get('contracts', 0))
                
                # 🔒 FIX: Use tolerance-based comparison instead of exact == 0
                if abs(contracts) < MIN_POSITION_SIZE:
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

