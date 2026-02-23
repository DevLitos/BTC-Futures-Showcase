"""
Position class for BTC Perpetual Trading System.

Handles: Position state tracking, TP calculations, trailing stops,
stop loss management, exit logic, and smart exit strategies.

Extracted from risk_engine.py for maintainability.
"""

import logging
import numpy as np
from datetime import datetime, UTC
from typing import Optional, Dict

from src.config.trading_config import (
    RiskManagement, SweepFilter, TimeBasedExits, SmartExits,
    TradeType
)
from src.domain.trading.state.state_types import PositionState
from src.domain.risk.trailing_stop import (
    HybridATRTrailing, TrailingRegime, TrailingState, create_hybrid_trailing
)

logger = logging.getLogger(__name__)


# REGIME CONSTANTS
REGIME_NORMAL = "NORMAL"    # Cortar basura, rascar wins, proteger DD
REGIME_RUNNER = "RUNNER"    # Dejar correr, solo time exit
RUNNER_PROMOTION_MFE = 1.5  # MFE >= 1.5R promociona a RUNNER (muy selectivo)
RUNNER_TIME_EXIT_CANDLES = 90  # N velas fijas para time exit (90min)

# =====================================================================
# REGIME NORMALIZATION: Convert any regime string to canonical form.
# EdgeRegimeClassifier returns values like "low_participation_chop",
# "expansion_trend_continuation", etc. These MUST be normalized to
# one of ('RANGE', 'TREND', 'BREAKOUT') for consistent TP/SL/trailing.
# =====================================================================
_REGIME_TREND_ALIASES = frozenset({
    'TREND', 'REGIME_A', 'EXPANSION_TREND_CONTINUATION',
    'TREND_EXPANSION', 'TREND_EXHAUSTION',
})
_REGIME_BREAKOUT_ALIASES = frozenset({'BREAKOUT'})

def normalize_regime_mode(raw: str) -> str:
    """Normalize any regime string to one of: 'RANGE', 'TREND', 'BREAKOUT'.
    
    Covers EdgeRegimeClassifier enum values (lowercase), enum names (REGIME_A..D),
    HTF regime modes, and legacy strings. Default: 'RANGE' (safest).
    """
    key = (raw or 'RANGE').upper()
    if key in _REGIME_TREND_ALIASES:
        return 'TREND'
    if key in _REGIME_BREAKOUT_ALIASES:
        return 'BREAKOUT'
    return 'RANGE'


class Position:
    """Represents an open position with execution integrity invariants"""
    
    def __init__(self, symbol: str, direction: str, entry_price: float, 
                 size: float, stop_loss: float, atr_1m: float,
                 market_time: datetime | None = None,
                 trade_type: TradeType = TradeType.LONG_MAIN,
                 regime_mode: str = 'RANGE',  # Normalized to RANGE/TREND/BREAKOUT
                 edge_type: str | None = None,  # 🔥 FIX (Jan 30): Track edge type for TIME_INVALIDATION
                 entry_score: float | None = None,  # 🔥 FIX (Feb 01): Track entry score for TIME_INVALIDATION
                 is_weekend: bool = False,  # 🔥 FIX (Feb 01): Track weekend for stricter exits
                 bos_quality: str = 'NORMAL',  # 🔥 NEW (Feb 03): BOS quality for dynamic trailing
                 displacement_origin: float | None = None):  # 🔥 NEW (Feb 04): Origin for reclaim exit
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
        self.regime_mode = normalize_regime_mode(regime_mode)  # Always 'RANGE', 'TREND', or 'BREAKOUT'
        self.entry_price = entry_price
        self.initial_entry_price = entry_price  # 🔥 FIX (Feb 4): NEVER changes, used for R/MFE calculations
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
        self.integrity_errors: list[str] = []  # List of integrity issues
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
        
        # DYNAMIC SIZING: Track original size before adds
        self.initial_size = size
        
        # 🔥 FIX (Jan 30): Edge type tracking for TIME_INVALIDATION relaxation
        self.edge_type = edge_type  # 'DISPLACEMENT', 'BREAKOUT', 'RANGE', 'TREND', or None
        
        # 🔥 FIX (Feb 01): Entry score tracking for TIME_INVALIDATION strictness
        # score=0 means FAST_PATH entry without quality validation → stricter exit
        self.entry_score = entry_score
        
        # 🔥 FIX (Feb 01): Weekend tracking for stricter TIME_INVALIDATION exits
        # Weekend has lower liquidity, more gaps, less follow-through
        # Data: 29 trades, 37.9% win, -0.62R total (vs weekday 42.4% win, +0.42R)
        self.is_weekend = is_weekend
        
        # Candle counter for mediocre trade detection
        self.candle_count = 0
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): EARLY PARTIAL AT +0.5R
        # =====================================================================
        # Take 50% at +0.5R to secure profits, then let rest run
        self.early_partial_done = False  # Track if early partial at +0.5R was taken
        self.early_partial_price: Optional[float] = None  # Price for early partial
        
        # =====================================================================
        # 🔥 NEW (Feb 4, 2026): TRADE TYPE MODE SWITCH
        # =====================================================================
        # EVENT_DRIVEN trades (DISPLACEMENT, FAST_PATH, compression_fakeout):
        #   - Use EARLY_PARTIAL, NO ADD_TO_WINNER
        #   - These are shock trades that often reverse quickly
        #
        # STRUCTURED trades (TREND, BOS, pullback):
        #   - Allow ADD_TO_WINNER, Early partial at 1.0R (later)
        #   - These have structural confirmation
        # =====================================================================
        self.is_event_driven = edge_type in ['DISPLACEMENT', 'FAST_REVERSAL'] if edge_type else False
        
        # =====================================================================
        # 🔥 NEW (Feb 4, 2026): DISPLACEMENT ORIGIN TRACKING
        # =====================================================================
        # For DISPLACEMENT trades, if price reclaims origin → thesis invalidated
        # Origin is where the displacement candle started, passed from signal
        self.displacement_origin: Optional[float] = displacement_origin  # 🔥 FIX: Use parameter
        self.origin_reclaimed = False  # Track if we already triggered reclaim exit
        
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
        # 🔥 FIX #43 (Feb 05, 2026): Changed from R-bucket to TIME-based rate limiting
        self.time_checkpoint_last_log_time: Optional[datetime] = None  # Last time we logged
        self.TIME_CHECKPOINT_LOG_INTERVAL_SEC = 60  # Only log every 60 seconds
        self.last_management_action_time: Optional[datetime] = None  # Debounce management actions
        self.MANAGEMENT_COOLDOWN_SEC = 30  # Seconds to wait between management actions
        
        # =====================================================================
        # HYBRID ATR TRAILING (Jan 15, 2026)
        # 🔥 FIX (Jan 22, 2026): Use regime_mode from parameter, not hardcoded TREND
        # =====================================================================
        # Initialize hybrid trailing stop manager
        self.hybrid_trailing: Optional[HybridATRTrailing] = None
        self.use_hybrid_trailing = True  # Feature flag
        self.bos_quality = bos_quality  # 🔥 NEW (Feb 03): Store for trailing adjustment
        
        if self.use_hybrid_trailing:
            # 🔥 FIX: Use regime_mode passed from SignalEngine (SINGLE SOURCE OF TRUTH)
            # Map regime_mode string to TrailingRegime enum
            # Handles both simplified (TREND/RANGE) and EdgeRegime values
            # 🔥 FIX #64 (Feb 6, 2026): TREND_EXHAUSTION → TREND
            # PROBLEMA: Trade con TrendPhase=TREND_EXHAUSTION tenía Edge=TREND
            # pero trailing usaba RANGE tiers (muy agresivos), causando exit prematuro
            # SOLUCIÓN: TREND_EXHAUSTION debería usar TREND tiers porque
            # aunque el trend está agotándose, todavía hay momentum para pullbacks normales
            regime_map = {
                # Simplified values
                'TREND': TrailingRegime.TREND,
                'TREND_EXPANSION': TrailingRegime.TREND,
                'BREAKOUT': TrailingRegime.TREND,
                'RANGE': TrailingRegime.RANGE,
                'TREND_EXHAUSTION': TrailingRegime.TREND,  # 🔥 FIX #64: was RANGE → TREND
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
                regime_str=initial_regime.value,
                bos_quality=bos_quality
            )
            logger.info(f"🎯 Trailing using regime={initial_regime.value}, bos_quality={bos_quality}")
        
        logger.info(f"Position opened: {direction} {size} {symbol} @ {entry_price:.2f} | SL: {stop_loss:.2f} | TP1: {self.tp1_price:.2f}")
    
    def increment_candle_count(self):
        """Increment candle counter for time-based exit logic (TIME_EXIT, RUNNER, mediocre detection)"""
        self.candle_count += 1
    
    def calculate_r_realized(self, exit_price: float) -> float:
        """
        Calculate realized R multiple for this trade.
        R = PnL / Initial Risk
        """
        # 🔥 FIX (Feb 4): Use initial_entry_price for consistent R calculation after adds
        initial_risk_per_unit = abs(self.initial_entry_price - self.initial_stop_loss)
        if initial_risk_per_unit == 0:
            return 0.0
        
        if self.direction == 'LONG':
            price_move = exit_price - self.initial_entry_price
        else:
            price_move = self.initial_entry_price - exit_price
        
        return price_move / initial_risk_per_unit
    
    def calculate_mfe_r(self) -> float:
        """Calculate Maximum Favorable Excursion in R.
        
        🔥 FIX (Feb 4, 2026): Use initial_entry_price, NOT entry_price
        After ADD_TO_WINNER, entry_price becomes avg_entry which would
        artificially reduce MFE and cause inconsistent reporting.
        """
        # 🔥 FIX: Use initial values for consistent MFE tracking
        initial_risk = abs(self.initial_entry_price - self.initial_stop_loss)
        if initial_risk == 0:
            return 0.0
        
        if self.direction == 'LONG':
            mfe = self.highest_price - self.initial_entry_price
        else:
            mfe = self.initial_entry_price - self.lowest_price
        
        return mfe / initial_risk
    
    def calculate_mae_r(self) -> float:
        """Calculate Maximum Adverse Excursion in R."""
        # 🔥 FIX: Use initial values for consistent MAE tracking
        initial_risk = abs(self.initial_entry_price - self.initial_stop_loss)
        if initial_risk == 0:
            return 0.0
        
        if self.direction == 'LONG':
            mae = self.initial_entry_price - self.lowest_price
        else:
            mae = self.highest_price - self.initial_entry_price
        
        return mae / initial_risk
    
    def check_displacement_reclaim(self, current_price: float) -> dict | None:
        """
        🔥 FIX (Feb 4, 2026): DISPLACEMENT ORIGIN RECLAIM EXIT
        
        For DISPLACEMENT trades, if price reclaims the origin level,
        the thesis is invalidated. The "displacement" was just noise.
        
        Example from Feb 3 trade:
          - Origin was $76,441
          - Entry SHORT @ $76,210 (below origin ✓)
          - If price goes back ABOVE origin → displacement reclaimed → exit
        
        This is a structural invalidation, not just a trailing stop.
        
        Returns: Close signal dict if origin reclaimed, None otherwise
        """
        if self.origin_reclaimed:
            return None  # Already handled
        
        if not self.displacement_origin:
            return None  # Not a displacement trade or origin not set
        
        if self.edge_type != 'DISPLACEMENT':
            return None  # Only applies to displacement trades
        
        origin = self.displacement_origin
        
        # Check if price reclaimed origin
        if self.direction == 'SHORT':
            # For SHORT: If price goes above origin, displacement reclaimed
            if current_price > origin:
                self.origin_reclaimed = True
                logger.warning(
                    f"🔄 DISPLACEMENT RECLAIM: Price {current_price:.2f} > origin {origin:.2f} | "
                    f"Thesis invalidated - closing SHORT"
                )
                return {
                    'action': 'CLOSE',
                    'reason': f'Displacement reclaim: price {current_price:.2f} > origin {origin:.2f}',
                    'origin': origin,
                    'current_price': current_price
                }
        else:
            # For LONG: If price goes below origin, displacement reclaimed
            if current_price < origin:
                self.origin_reclaimed = True
                logger.warning(
                    f"🔄 DISPLACEMENT RECLAIM: Price {current_price:.2f} < origin {origin:.2f} | "
                    f"Thesis invalidated - closing LONG"
                )
                return {
                    'action': 'CLOSE',
                    'reason': f'Displacement reclaim: price {current_price:.2f} < origin {origin:.2f}',
                    'origin': origin,
                    'current_price': current_price
                }
        
        return None

    def _calculate_tp1(self, entry: float, stop: float, direction: str) -> float:
        """Calculate TP1 — regime-aware.
        
        RANGE: 0.30R (validated Feb 20 sim: 38% hit rate, captures $0.83/win avg)
        Other: 1.0R (standard TP1)
        """
        risk = abs(entry - stop)
        if self.regime_mode in ('RANGE', 'MEAN_REVERSION', 'CHOPPY_DEAD'):
            tp_mult = 0.30  # RANGE scalp TP
        else:
            tp_mult = RiskManagement.TP1_RISK_MULTIPLE  # 1.0R
        if direction == 'LONG':
            return entry + (risk * tp_mult)
        else:
            return entry - (risk * tp_mult)
    
    def _calculate_early_partial_price(self) -> float:
        """
        Calculate price for early partial — REGIME-AWARE.
        
        CRITICAL FEE ANALYSIS (Feb 19, 2026):
        Avg round-trip fees ≈ 0.17R. Any partial below 0.35R LOSES money.
        
        - RANGE/MEAN_REVERSION: partial at 0.40R (was 0.15R — LOST money after fees!)
        - EVENT_DRIVEN (DISPLACEMENT): partial at 0.70R
        - STRUCTURED (TREND, BOS): partial at 1.0R (TP1 level)
        """
        risk = abs(self.initial_entry_price - self.initial_stop_loss)
        
        if self.regime_mode in ('RANGE', 'MEAN_REVERSION', 'CHOPPY_DEAD'):
            # RANGE: TP at 0.30R — full close (no runner in range)
            # Data: avg RANGE MFE=0.26R, 38% reach 0.30R
            partial_r = 0.30
        elif self.is_event_driven:
            partial_r = RiskManagement.EARLY_PARTIAL_R  # 0.7R
        else:
            partial_r = 1.0
        
        if self.direction == 'LONG':
            return self.entry_price + (risk * partial_r)
        else:
            return self.entry_price - (risk * partial_r)
    
    def check_early_partial_hit(self, current_price: float) -> bool:
        """
        Check if early partial price (+0.5R) was reached.
        Simple and clean - let R_LOCK_TIERS handle the intelligent protection.
        """
        if not RiskManagement.EARLY_PARTIAL_ENABLED:
            return False
        
        if self.early_partial_done:
            return False  # Already taken
        
        if self.early_partial_price is None:
            self.early_partial_price = self._calculate_early_partial_price()
        
        # Check if price reached the level
        if self.direction == 'LONG':
            if current_price >= self.early_partial_price:
                logger.info(f"💰 EARLY PARTIAL HIT at {current_price:.2f} (target: {self.early_partial_price:.2f})")
                return True
        else:
            if current_price <= self.early_partial_price:
                logger.info(f"💰 EARLY PARTIAL HIT at {current_price:.2f} (target: {self.early_partial_price:.2f})")
                return True
        
        return False

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
                    regime = TrailingRegime.RANGE
            else:
                # Use position's regime_mode, default RANGE (conservative)
                try:
                    regime = TrailingRegime(self.regime_mode.upper()) if self.regime_mode else TrailingRegime.RANGE
                except ValueError:
                    regime = TrailingRegime.RANGE
            
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
                    move = new_hybrid_stop - self.trailing_stop
                    if move > 10:  # Only log significant moves (>$10)
                        logger.info(f"🎯 HYBRID TRAIL: {self.trailing_stop:.2f} → {new_hybrid_stop:.2f} ({state.stop_source}, tier={state.r_lock_tier})")
                    self.trailing_stop = new_hybrid_stop
            else:
                if new_hybrid_stop < self.trailing_stop:
                    move = self.trailing_stop - new_hybrid_stop
                    if move > 10:  # Only log significant moves (>$10)
                        logger.info(f"🎯 HYBRID TRAIL: {self.trailing_stop:.2f} → {new_hybrid_stop:.2f} ({state.stop_source}, tier={state.r_lock_tier})")
                    self.trailing_stop = new_hybrid_stop
            
            return  # Hybrid trailing handled everything
        
        # =====================================================================
        # LEGACY STEPPED TRAILING (fallback if hybrid disabled)
        # 🔥 FIX (Jan 22, 2026): Added regime-aware BE logic
        # 🔥 FIX (Feb 4, 2026): Use initial_entry_price for consistent R after ADD
        # =====================================================================
        risk = abs(self.initial_entry_price - self.initial_stop_loss)
        if risk <= 0:
            return
        
        mfe_r = self.calculate_mfe_r()
        
        # =====================================================================
        # 🔥 FIX #139 (Feb 10, 2026): IMPROVED STEPPED TRAILING
        # 
        # CAMBIOS vs anterior:
        # 1. Añadido nivel 1.8R → lock +1.2R (cierra gap 1.5-2.2)
        # 2. Mejorado nivel 1.0R para TREND: ahora lock +0.2R (no esperar hasta 1.2R)
        # 3. Los gaps ahora son max 0.3R vs 0.5R anterior
        #
        # STEPPED TRAILING LEVELS (based on MFE reached, not current price)
        # Once MFE hits a level, lock in that stop - don't give it back
        # =====================================================================
        
        if mfe_r >= 2.2:
            # Lock in +1.6R (big runners)
            lock_r = 1.6
            if self.direction == 'LONG':
                new_stop = self.initial_entry_price + (risk * lock_r)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.initial_entry_price - (risk * lock_r)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 5: MFE {mfe_r:.2f}R >= 2.2R → Lock +{lock_r}R")
        
        elif mfe_r >= 1.8:
            # 🔥 FIX #139: Nuevo nivel para cerrar gap 1.5-2.2
            lock_r = 1.2
            if self.direction == 'LONG':
                new_stop = self.initial_entry_price + (risk * lock_r)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.initial_entry_price - (risk * lock_r)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 4: MFE {mfe_r:.2f}R >= 1.8R → Lock +{lock_r}R")
        
        elif mfe_r >= 1.5:
            # Lock in +0.9R
            lock_r = 0.9
            if self.direction == 'LONG':
                new_stop = self.initial_entry_price + (risk * lock_r)
                self.trailing_stop = max(self.trailing_stop, new_stop)
            else:
                new_stop = self.initial_entry_price - (risk * lock_r)
                self.trailing_stop = min(self.trailing_stop, new_stop)
            logger.debug(f"📈 TRAILING STEP 3: MFE {mfe_r:.2f}R >= 1.5R → Lock +{lock_r}R")
        
        elif mfe_r >= 1.2:
            # Regime-dependent: RANGE=BE, TREND=+0.4R
            if self.regime_mode == 'RANGE':
                if self.direction == 'LONG':
                    self.trailing_stop = max(self.trailing_stop, self.initial_entry_price)
                else:
                    self.trailing_stop = min(self.trailing_stop, self.initial_entry_price)
                logger.debug(f"📈 TRAILING (RANGE): MFE {mfe_r:.2f}R >= 1.2R → Lock BE")
            else:
                lock_r = 0.5  # 🔥 FIX #139: Subido de 0.4R a 0.5R
                if self.direction == 'LONG':
                    new_stop = self.initial_entry_price + (risk * lock_r)
                    self.trailing_stop = max(self.trailing_stop, new_stop)
                else:
                    new_stop = self.initial_entry_price - (risk * lock_r)
                    self.trailing_stop = min(self.trailing_stop, new_stop)
                logger.debug(f"📈 TRAILING STEP 2: MFE {mfe_r:.2f}R >= 1.2R → Lock +{lock_r}R")
        
        elif mfe_r >= 1.0:
            # 🔥 FIX #139: TREND ahora protege a 1.0R (no espera hasta 1.2R)
            if self.regime_mode == 'RANGE':
                # RANGE: Lock at -0.1R (slightly protective)
                lock_r = -0.1  # 🔥 FIX #139: Mejorado de -0.2R a -0.1R
                if self.direction == 'LONG':
                    new_stop = self.initial_entry_price - (risk * abs(lock_r))
                    self.trailing_stop = max(self.trailing_stop, new_stop)
                else:
                    new_stop = self.initial_entry_price + (risk * abs(lock_r))
                    self.trailing_stop = min(self.trailing_stop, new_stop)
                logger.debug(f"📈 TRAILING (RANGE): MFE {mfe_r:.2f}R >= 1.0R → Lock {lock_r}R")
            else:
                # 🔥 FIX #139: TREND protege +0.2R a 1.0R (nuevo)
                lock_r = 0.2
                if self.direction == 'LONG':
                    new_stop = self.initial_entry_price + (risk * lock_r)
                    self.trailing_stop = max(self.trailing_stop, new_stop)
                else:
                    new_stop = self.initial_entry_price - (risk * lock_r)
                    self.trailing_stop = min(self.trailing_stop, new_stop)
                logger.debug(f"📈 TRAILING (TREND): MFE {mfe_r:.2f}R >= 1.0R → Lock +{lock_r}R")
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): AGGRESSIVE TRAILING AT +0.7R
        # 
        # Configurable via RiskManagement.TRAILING_STEP_07R_ENABLED
        # Locks +0.3R when MFE reaches 0.7R
        # =====================================================================
        elif mfe_r >= 0.7:
            if RiskManagement.TRAILING_STEP_07R_ENABLED:
                # Lock in +0.3R (early protection for intermediate moves)
                if self.direction == 'LONG':
                    new_stop = self.initial_entry_price + (risk * 0.3)
                    if new_stop > self.trailing_stop:
                        old_stop = self.trailing_stop
                        self.trailing_stop = new_stop
                        logger.debug(f"📈 TRAILING: MFE {mfe_r:.2f}R >= 0.7R → Lock +0.3R (was {old_stop:.2f})")
                else:
                    new_stop = self.initial_entry_price - (risk * 0.3)
                    if new_stop < self.trailing_stop:
                        old_stop = self.trailing_stop
                        self.trailing_stop = new_stop
                        logger.debug(f"📈 TRAILING: MFE {mfe_r:.2f}R >= 0.7R → Lock +0.3R (was {old_stop:.2f})")
        
        # =====================================================================
        # 🔥 FIX (Feb 01, 2026): EARLY PROFIT PROTECTION
        # 
        # Data shows 8 trades with MFE > 0.2R ended in losses = +5.82R lost
        # Problem: Trailing starts at 1.0R+ but many trades peak at 0.3-0.5R
        # 
        # Solution: After MFE >= 0.35R, tighten max allowed loss to -0.15R
        # This catches trades that showed momentum but reversed
        # 
        # LOGIC:
        # - MFE >= 0.35R: Move stop to -0.15R (was potentially at -0.30R or worse)
        # - This is NOT full BE, but limits the downside after showing promise
        # - Only for RANGE/WEEKEND - TREND trades get more room
        # =====================================================================
        # =====================================================================
        # 🔥 FIX #42 (Feb 05, 2026): PROTECT AT 0.20R
        # 
        # Trade reached MFE=0.24R but lost -0.22R because no protection kicked in.
        # Add protection at 0.20R: tighten stop to -0.10R
        # This catches trades that showed promise but reversed early
        # =====================================================================
        elif mfe_r >= 0.20 and not self.be_moved:
            # Tighten stop to -0.10R when MFE shows trade was right
            if self.direction == 'LONG':
                new_stop = self.initial_entry_price - (risk * 0.10)
                if new_stop > self.trailing_stop:
                    old_stop = self.trailing_stop
                    self.trailing_stop = new_stop
                    self.be_moved = True
                    logger.info(
                        f"🛡️ MFE PROTECT: MFE {mfe_r:.2f}R >= 0.20R → "
                        f"Tighten SL to {new_stop:.2f} (-0.10R) (was {old_stop:.2f})"
                    )
            else:  # SHORT
                new_stop = self.initial_entry_price + (risk * 0.10)
                if new_stop < self.trailing_stop:
                    old_stop = self.trailing_stop
                    self.trailing_stop = new_stop
                    self.be_moved = True
                    logger.info(
                        f"🛡️ MFE PROTECT: MFE {mfe_r:.2f}R >= 0.20R → "
                        f"Tighten SL to {new_stop:.2f} (-0.10R) (was {old_stop:.2f})"
                    )
        
        elif mfe_r >= 0.35:
            is_weekend = getattr(self, 'is_weekend', False)
            
            # Apply early protection in RANGE mode OR weekend
            if self.regime_mode == 'RANGE' or is_weekend:
                # Tighten stop to -0.15R (not full BE, but protected)
                if self.direction == 'LONG':
                    new_stop = self.initial_entry_price - (risk * 0.15)
                    if new_stop > self.trailing_stop:
                        old_stop = self.trailing_stop
                        self.trailing_stop = new_stop
                        self.be_moved = True  # Mark as having early protection
                        logger.info(
                            f"🛡️ EARLY PROFIT PROTECT: MFE {mfe_r:.2f}R >= 0.35R → "
                            f"Tighten SL to {new_stop:.2f} (-0.15R) (was {old_stop:.2f})"
                        )
                else:  # SHORT
                    new_stop = self.initial_entry_price + (risk * 0.15)
                    if new_stop < self.trailing_stop:
                        old_stop = self.trailing_stop
                        self.trailing_stop = new_stop
                        self.be_moved = True
                        logger.info(
                            f"🛡️ EARLY PROFIT PROTECT: MFE {mfe_r:.2f}R >= 0.35R → "
                            f"Tighten SL to {new_stop:.2f} (-0.15R) (was {old_stop:.2f})"
                        )
    
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
            # 🔥 FIX (Feb 4): Use initial_entry_price for consistent BE after ADD
            if self.direction == 'LONG':
                # Para LONG, mayor = mejor
                runner_stop = max(self.trailing_stop, self.initial_entry_price)
                self.stop_loss = runner_stop
                self.trailing_stop = runner_stop
            else:
                # Para SHORT, menor = mejor
                runner_stop = min(self.trailing_stop, self.initial_entry_price)
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
    
    def check_early_mfe_exit(self, current_price: float | None = None,
                               bb_width_percentile: float | None = None,
                               volume_ratio: float | None = None,
                               delta_ratio: float | None = None,
                               session: str | None = None,
                               atr_1m: float | None = None,
                               atr_avg: float | None = None,
                               micro_trend: str | None = None) -> dict | None:
        """
        🔥🔥🔥 FIX #160 (Feb 10, 2026): SIMPLIFIED EXIT SYSTEM
        
        PROBLEMA RAÍZ: 9 capas de exit que se pisan entre sí, matando trades buenos.
        - STRUCTURE_EXIT_NO_DELTA: Trade score 8.6, MFE 0.09R, cortado en 4.5min
        - EARLY_VELOCITY_EXIT: 4 de 8 trades hoy cortados por esto
        - Resultado: Win rate 25%, avg winner 0.18R, avg loser -0.39R
        
        FILOSOFÍA NUEVA: El SL existe por una razón. CONFIAR en él.
        
        Solo 2 condiciones de early exit válidas:
        1. FLASH_CRASH: Caída > 0.7R en < 3 candles → algo muy malo pasó
        2. En profit con micro reversing → asegurar ganancia (opcional)
        
        TODO LO DEMÁS: Dejar que el SL/TP/TRAILING hagan su trabajo.
        Los pullbacks son NORMALES. El MFE bajo inicial es NORMAL.
        
        ELIMINADO:
        - STRUCTURE_EXIT_NO_DELTA (hindsight bias, mata trades jóvenes)
        - EARLY_VELOCITY_EXIT (mata trades que necesitan tiempo para desarrollar)
        - DETERIORATION_EXIT (asume que "lento = malo", falso en BTC)
        - PROFIT_DRAWDOWN_EXIT (devuelve todo el edge a pullbacks normales)
        - Todas las capas de "protección" con timeouts
        
        RESULTADO ESPERADO:
        - Trades duran más (avg 30min vs 7min actual)
        - SL hace su trabajo cuando tesis se invalida
        - Winners corren hasta TP o trailing
        """
        # Skip adopted positions - we don't know their history
        if self.is_adopted:
            return None
        
        if current_price is None:
            return None
        
        # =====================================================================
        # CALCULAR R ACTUAL
        # =====================================================================
        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk <= 0:
            return None
            
        if self.direction == 'LONG':
            current_r = (current_price - self.entry_price) / initial_risk
        else:
            current_r = (self.entry_price - current_price) / initial_risk
        
        mfe_r = self.calculate_mfe_r()
        
        # =====================================================================
        # 🚨 ÚNICO EXIT DE EMERGENCIA: FLASH CRASH
        # Si perdió > 0.7R en < 3 candles, algo catastrófico pasó
        # (exchange glitch, flash crash, etc.)
        # Umbral subido de 0.5R a 0.7R - queremos solo verdaderas emergencias
        # =====================================================================
        if self.candle_count <= 3 and current_r < -0.7:
            logger.warning(
                f"🚨 FLASH_CRASH_EXIT: Lost {current_r:.2f}R in only {self.candle_count} candles | "
                f"Catastrophic move, exiting immediately"
            )
            return {
                'action': 'CLOSE',
                'reason': 'FLASH_CRASH_EXIT',
                'mfe_r': mfe_r,
                'current_r': current_r,
                'candles': self.candle_count,
                'exit_type': 'EMERGENCY'
            }
        
        # =====================================================================
        # 🎯 OPCIONAL: MICRO REVERSAL TP (solo si está en profit)
        # Si MFE >= 0.60R Y current_r >= 0.50R Y micro es CONTRARIA:
        # → TOMAR BENEFICIOS
        # 
        # Esto es OPCIONAL - solo asegura ganancias que ya tienes
        # NO corta trades que van mal (eso lo hace el SL)
        # =====================================================================
        if mfe_r >= 0.60 and current_r >= 0.50:
            micro_is_contrary = False
            if self.direction == 'SHORT' and micro_trend == 'BULLISH':
                micro_is_contrary = True
            elif self.direction == 'LONG' and micro_trend == 'BEARISH':
                micro_is_contrary = True
            
            if micro_is_contrary:
                logger.info(
                    f"🎯 MICRO_REVERSAL_TP: MFE={mfe_r:.2f}R, current={current_r:.2f}R | "
                    f"Micro={micro_trend} reversing | Securing profit"
                )
                return {
                    'action': 'CLOSE',
                    'reason': 'MICRO_REVERSAL_TP',
                    'mfe_r': mfe_r,
                    'current_r': current_r,
                    'candles': self.candle_count,
                    'exit_type': 'PROFIT_SECURED',
                    'micro_trend': micro_trend
                }
        
        # =====================================================================
        # FIN - NINGUNA CONDICIÓN DE EARLY EXIT MET
        # 
        # El SL, TP, y ATR Trailing manejarán la salida normal.
        # Los pullbacks son normales. Los trades lentos son normales.
        # La única emergencia real es un flash crash (> 0.7R en 3 candles).
        # =====================================================================
        return None
    
    def check_mediocre_trade_exit(self, n_candles: int = 12, mfe_threshold: float = 0.3,
                                     adx: float | None = None, atr_ratio: float | None = None,
                                     current_price: float | None = None,
                                     vwap: float | None = None, last_lh: float | None = None,
                                     vah: float | None = None) -> dict | None:
        """
        TIME_INVALIDATION: Exit trades that show no conviction.

        Data (Feb 10-19): 23 TIME_INVALIDATION trades, 0 wins, avg -0.40R.
        - 11/23 had MFE < 0.05R (price NEVER went the right way)
        - 14/23 in ASIA/WEEKEND/DEAD_HOURS (low quality sessions)

        Algorithm:
        1. HARD LIMIT: -0.25R max loss (tighter than old -0.35R)
           - MFE >= 0.15R → extend to -0.30R (showed some conviction)
           - MFE >= 0.30R → extend to -0.40R (proved direction)
           - MFE >= 0.50R → extend to -0.50R (clearly correct)
           - Weekend/low quality: -0.15R always
        2. FAST CUT (6 candles): MFE < 0.05R + losing → dead trade
        3. NORMAL CHECK (12 candles): Evaluate based on MFE and market state
        """
        from src.config.trading_config import ExitHierarchy, ADXThresholds

        if self.is_adopted or current_price is None:
            return None

        initial_risk = abs(self.entry_price - self.initial_stop_loss)
        if initial_risk <= 0:
            return None

        if self.direction == 'LONG':
            current_r = (current_price - self.entry_price) / initial_risk
        else:
            current_r = (self.entry_price - current_price) / initial_risk

        mfe_r = self.calculate_mfe_r()
        is_weekend = getattr(self, 'is_weekend', False)
        is_low_quality = self.entry_score is not None and self.entry_score < 4

        # === MAX LOSS CALCULATION (tight limits for $230 account) ===
        if is_weekend or is_low_quality:
            max_loss_r = -0.10  # Weekend/low quality: cut fast
        else:
            max_loss_r = -0.15  # Base: -0.15R (was -0.20R — tightened to preserve capital)

        # MFE tolerance: trade proved direction → give proportional room
        if mfe_r >= 0.50:
            max_loss_r = min(max_loss_r, -0.30)  # Strong conviction
        elif mfe_r >= 0.30:
            max_loss_r = min(max_loss_r, -0.20)  # Good conviction
        elif mfe_r >= 0.15:
            max_loss_r = min(max_loss_r, -0.15)  # Some conviction

        # ADX bonus: strong trend pullbacks are normal
        if adx is not None and adx >= 60 and not is_weekend:
            max_loss_r = min(max_loss_r, -0.30)  # Was -0.40
        elif adx is not None and adx >= 50 and not is_weekend:
            max_loss_r = min(max_loss_r, -0.25)  # Was -0.35

        # === HARD LIMIT: enforce max loss at all times ===
        if current_r <= max_loss_r:
            logger.warning(
                f"☠️ TIME_INVALIDATION: {current_r:.2f}R exceeds limit {max_loss_r:.2f}R | "
                f"MFE={mfe_r:.2f}R | candles={self.candle_count}"
            )
            return {
                'action': 'CLOSE', 'reason': 'TIME_INVALIDATION_MAX_LOSS',
                'current_r': current_r, 'max_loss_r': max_loss_r,
                'candles': self.candle_count
            }

        # === PHASE 1: FAST CUT (4 candles) ===
        # Trade never went the right direction → wrong entry, cut FAST
        # Tightened from 6 candles/0.05R/0.10R → 4 candles/0.03R/0.08R
        if 4 <= self.candle_count < n_candles:
            if mfe_r < 0.03 and current_r < -0.08:
                logger.warning(
                    f"☠️ TIME_FAST_CUT: {self.candle_count} candles, MFE={mfe_r:.2f}R | "
                    f"current={current_r:.2f}R | Zero conviction, cutting dead trade"
                )
                return {
                    'action': 'CLOSE', 'reason': 'TIME_INVALIDATION_MAX_LOSS',
                    'current_r': current_r, 'max_loss_r': max_loss_r,
                    'candles': self.candle_count
                }
            return None  # Not at normal checkpoint yet

        if self.candle_count < n_candles:
            return None

        # === PHASE 2: NORMAL CHECK (12+ candles) ===

        # MFE >= 0.30R: trade proved direction → let trailing/partial handle it
        if mfe_r >= 0.30:
            return None

        # Currently in decent profit → partial if not done
        if current_r >= 0.30 and not self.time_checkpoint_profit_partial_done:
            self.time_checkpoint_profit_partial_done = True
            return {
                'action': 'PARTIAL', 'reason': 'TIME_IN_PROFIT',
                'current_r': current_r,
                'partial_pct': ExitHierarchy.TIME_IN_PROFIT_PARTIAL_PCT,
                'keep_runner': True, 'candles': self.candle_count,
                'use_limit': True
            }

        # In any profit → hold
        if current_r > 0:
            return None

        # === IN LOSS after 12+ candles ===

        # MFE < 0.15R + losing → trade showed zero conviction
        if mfe_r < 0.15 and current_r < -0.10:
            logger.warning(
                f"☠️ TIME_INVALIDATION: {self.candle_count} candles, MFE={mfe_r:.2f}R | "
                f"current={current_r:.2f}R | No conviction"
            )
            return {
                'action': 'CLOSE', 'reason': 'TIME_INVALIDATION_MAX_LOSS',
                'current_r': current_r, 'max_loss_r': max_loss_r,
                'candles': self.candle_count
            }

        # MFE 0.15-0.30R but now losing: check if market still alive
        is_ranging = adx is not None and adx < ADXThresholds.TREND
        market_alive = (adx is not None and adx >= ADXThresholds.TREND) or \
                       (atr_ratio is not None and atr_ratio >= 0.8)

        if market_alive:
            _last = getattr(self, '_time_checkpoint_last_log', 0)
            if (self.candle_count - _last) >= 30:
                self._time_checkpoint_last_log = self.candle_count
                logger.info(f"⏱️ TIME: {current_r:.2f}R | MFE={mfe_r:.2f}R | Market alive, holding")
            return {'action': 'HOLD', 'reason': 'MARKET_ALIVE',
                    'current_r': current_r, 'candles': self.candle_count}

        # Dead market + structure check
        structural_invalid = self._check_structural_invalidation(current_price, vwap, last_lh, vah)
        if structural_invalid:
            logger.warning(f"☠️ TIME + STRUCTURE INVALID: {current_r:.2f}R | {structural_invalid['reason']}")
            return {
                'action': 'CLOSE',
                'reason': f"TIME_STRUCTURAL_INVALID: {structural_invalid['reason']}",
                'current_r': current_r, 'candles': self.candle_count
            }

        # Dead market, near limit → tighten stop
        if current_r <= max_loss_r + 0.10:
            return {
                'action': 'REDUCE_RISK', 'reason': 'TIME_NEAR_LIMIT',
                'current_r': current_r, 'move_stop_to_r': max_loss_r,
                'candles': self.candle_count
            }

        # Dead market, structure OK, moderate loss → close (no edge left)
        logger.warning(
            f"☠️ TIME_INVALIDATION: {self.candle_count} candles, MFE={mfe_r:.2f}R, "
            f"current={current_r:.2f}R | Market dead"
        )
        return {
            'action': 'CLOSE', 'reason': 'TIME_INVALIDATION_MAX_LOSS',
            'current_r': current_r, 'mfe_r': mfe_r, 'candles': self.candle_count
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
        
        🔥 SIMPLIFIED MODE (Jan 27, 2026):
        If SimplifiedFeatureFlags.DISABLE_WTP is True, WTP is completely disabled
        to measure true edge without "fear-based" exits.
        
        Args:
            current_price: Current market price
            market_time: Current market time (for backtest compatibility). Falls back to time.time() if None.
        
        Returns:
            dict with action if should exit, None otherwise
        """
        from src.config.trading_config import ExitHierarchy
        import time
        
        # 🔥 Check if WTP is disabled in simplified mode
        if not ExitHierarchy.is_wtp_enabled():
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
            distance_to_tp = abs(self.take_profit - current_price) if self.take_profit else float('inf')
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
    
    def get_time_in_trade_minutes(self, market_time: datetime | None = None) -> float:
        """
        Get time in trade in minutes.
        
        Args:
            market_time: Current market time (REQUIRED - never use datetime.now())
        """
        current_time = market_time if market_time else self.open_time
        return (current_time - self.open_time).total_seconds() / 60
    
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
    
    def check_scaled_partial_exit(self, current_price: float) -> Optional[Dict]:
        """
        SMART EXIT #1: Scaled partial profit taking.
        
        🔥 FIX #150 (Feb 10, 2026): Usa current_r, NO MFE
        
        Exit progressively at multiple R levels instead of binary TP1/TP2.
        This locks in profits while letting a portion run.
        
        CAMBIO CRÍTICO: Ahora usa current_r (PnL actual) en lugar de MFE.
        - Antes: Si MFE=0.49R pero ahora=0.05R → cerraba (mal)
        - Ahora: Solo cierra cuando precio ACTUAL está en el nivel
        
        Returns:
            Dict with exit details if should take partial, None otherwise
            {'action': 'PARTIAL_EXIT', 'exit_pct': 0.33, 'current_r': 0.8, 'reason': str}
        """
        if not SmartExits.SCALED_PARTIALS_ENABLED:
            return None
        
        # 🔥 FIX #150: Usar current_r, NO MFE
        # Queremos cerrar cuando AHORA estamos en profit, no cuando estuvimos antes
        current_r = self.calculate_r_realized(current_price)
        
        # No cerrar si estamos en pérdida
        if current_r < 0:
            return None
        
        # Check each scale-out level
        for level_r, exit_pct in SmartExits.SCALE_OUT_LEVELS:
            # Skip if we already exited at this level
            already_done = any(done_r == level_r for done_r, _ in self.partial_exits_done)
            if already_done:
                continue
            
            # 🔥 FIX #150: Check if CURRENT profit reached this level
            if current_r >= level_r:
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
                    
                    logger.info(
                        f"💰 FIX #150 SCALED PARTIAL: Exit {actual_exit_pct:.0%} @ current {current_r:.2f}R | "
                        f"Level: {level_r}R | Remaining: {self.remaining_size_pct:.0%}"
                    )
                    
                    return {
                        'action': 'PARTIAL_EXIT',
                        'exit_pct': actual_exit_pct,
                        'current_r': current_r,
                        'level_r': level_r,
                        'remaining_pct': self.remaining_size_pct,
                        'reason': f'FIX #150 Scaled partial at {level_r}R'
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
        # 🔥 FIX (Feb 4): Use initial_entry_price for consistent R after ADD
        initial_risk = abs(self.initial_entry_price - self.initial_stop_loss)
        lock_r = SmartExits.TIME_LOCK_FLOOR_R
        
        if self.direction == 'LONG':
            lock_price = self.initial_entry_price + (initial_risk * lock_r)
            # Only lock if current stop is below lock price
            if self.trailing_stop < lock_price:
                self.trailing_stop = lock_price
                self.stop_loss = lock_price
                self.profit_locked = True
                self.locked_r = lock_r
        else:
            lock_price = self.initial_entry_price - (initial_risk * lock_r)
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
        
        RANGE BYPASS (Feb 20, 2026):
        In RANGE mode, smart exits are SKIPPED entirely.
        _handle_early_partial() closes 100% at 0.30R instead.
        Data: Scaled partials (30% at 0.3R) destroyed +$3.59 over 5 days
        by leaving 70% exposed to mean reversion.
        """
        # 🔥 RANGE: Skip ALL smart exits — early_partial handles 100% close at 0.30R
        # Data: 11/25 trades reached 0.3R MFE, but scaled partials only closed 30%,
        # leaving 70% to reverse. Simulated improvement: +$3.59 over 5 days.
        if self.regime_mode in ('RANGE', 'MEAN_REVERSION', 'CHOPPY_DEAD'):
            return None
        
        # Update indicators
        self.update_smart_exit_indicators(rsi, bb_width, bb_avg, volume_ratio)
        
        # Priority 1: Time-based profit lock (moves stop, doesn't exit)
        lock_result = self.check_time_profit_lock()
        # Note: This modifies stop, doesn't trigger full exit
        
        # Priority 2: Scaled partial exits (🔥 FIX #150: now uses current_price)
        partial_result = self.check_scaled_partial_exit(current_price)
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

