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

logger = logging.getLogger(__name__)


class Position:
    """Represents an open position"""
    
    def __init__(self, symbol: str, direction: str, entry_price: float, 
                 size: float, stop_loss: float, atr_1m: float,
                 market_time: datetime = None):
        self.symbol = symbol
        self.direction = direction  # 'LONG' or 'SHORT'
        self.entry_price = entry_price
        self.size = size
        self.stop_loss = stop_loss
        self.initial_stop_loss = stop_loss
        
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
        FIX #3: 3-PHASE INTELLIGENT TRAILING STOP
        
        Phase 1 (0-5min after entry): Protection mode - tight SL (1.5x ATR)
        Phase 2 (5-20min): Structure-based - use structure swings
        Phase 3 (20min+): Extension mode - let winners run
        
        Args:
            current_price: Current market price
            atr_1m: Current ATR value
            market_time: Current market time (REQUIRED - never use datetime.now())
        
        Raises:
            ValueError if market_time is None
        """
        if not self.tp1_hit:
            return
        
        # CRITICAL FIX: market_time is now REQUIRED
        if market_time is None:
            logger.error("market_time is required for trailing stop calculation")
            # Fallback to open_time which gives duration=0 (Phase 1)
            market_time = self.open_time
        
        market_time = ensure_utc(market_time)
        self.atr_1m = atr_1m
        
        # Determine phase based on trade duration - use MARKET TIME
        duration_minutes = (market_time - self.open_time).total_seconds() / 60
        
        # RELAXED TRAILING (Jan 4, 2026): All phases use wider stops to let winners run
        # Phase 1 was 1.5x (too tight), now 2.5x
        # Phase 2 was 2.0x, now 2.5x  
        # Phase 3 was 2.5x, now 3.0x
        if self.direction == 'LONG':
            if duration_minutes < 5:
                # PHASE 1: Entry Protection (now 2.5x ATR - was 1.5x)
                new_trailing = current_price - (atr_1m * 2.5)
                self.trailing_stop = max(self.trailing_stop, new_trailing)
                
            elif duration_minutes < 20:
                # PHASE 2: Profit Protection (now 2.5x ATR - was 2.0x)
                new_trailing = current_price - (atr_1m * 2.5)
                self.trailing_stop = max(self.trailing_stop, new_trailing)
                
            else:
                # PHASE 3: Extension Mode (now 3.0x ATR - was 2.5x)
                new_trailing = current_price - (atr_1m * 3.0)
                self.trailing_stop = max(self.trailing_stop, new_trailing)
                
        else:  # SHORT
            if duration_minutes < 5:
                # PHASE 1: Entry Protection (now 2.5x ATR - was 1.5x)
                new_trailing = current_price + (atr_1m * 2.5)
                self.trailing_stop = min(self.trailing_stop, new_trailing)
                
            elif duration_minutes < 20:
                # PHASE 2: Profit Protection (now 2.5x ATR - was 2.0x)
                new_trailing = current_price + (atr_1m * 2.5)
                self.trailing_stop = min(self.trailing_stop, new_trailing)
                
            else:
                # PHASE 3: Extension Mode (now 3.0x ATR - was 2.5x)
                new_trailing = current_price + (atr_1m * 3.0)
                self.trailing_stop = min(self.trailing_stop, new_trailing)
    
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
        Check SL hit with grace period and sweep detection
        
        GRACE PERIOD: In the first 10 minutes, only exit if price moves > 1.5x SL distance
        This gives the trade time to develop without getting stopped on noise.
        
        Args:
            current_price: Current price
            current_low: Candle low (optional, for sweep detection)
            atr_1m: ATR for sweep validation
            market_time: Current market time (for grace period check)
        
        Returns: True if SL hit (and it's not a sweep, and not in grace period)
        """
        if self.direction == 'LONG':
            sl_hit = current_price <= self.stop_loss
        else:
            sl_hit = current_price >= self.stop_loss
        
        if not sl_hit:
            return False
        
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
        """Check if trailing stop hit"""
        if not self.tp1_hit:
            return False
        
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
                           support_resistance: float, atr_1m: float) -> float:
        """
        Calculate technical stop loss with ATR buffer
        support_resistance: HL for LONG, LH for SHORT
        
        EDGE FIX: Use minimum 1.2 ATR distance to avoid noise stops
        The SL is the FURTHER of: structure level OR ATR minimum
        """
        buffer = RiskManagement.STOP_LOSS_ATR_BUFFER * atr_1m
        
        # Minimum SL distance = 2.5 ATR (survive 3-4 candles of noise in BTC)
        min_sl_distance = 2.5 * atr_1m
        
        if direction == 'LONG':
            structure_stop = support_resistance - buffer
            # SL must be at least min_sl_distance below entry
            atr_stop = entry_price - min_sl_distance
            stop = min(structure_stop, atr_stop)  # Use the LOWER (further) one
        else:  # SHORT
            structure_stop = support_resistance + buffer
            # SL must be at least min_sl_distance above entry  
            atr_stop = entry_price + min_sl_distance
            stop = max(structure_stop, atr_stop)  # Use the HIGHER (further) one
        
        logger.info(f"Stop loss calculated: {stop:.2f} (Structure: {support_resistance:.2f}±{buffer:.2f}, ATR min: {min_sl_distance:.2f})")
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
                             market_time: datetime = None) -> Optional[dict]:
        """
        Manage open position - check stops, TPs, exits
        
        Args:
            market_time: Current market time (REQUIRED - used for trailing stop phases and time-based exits)
        
        Returns: exit_action dict if position should be closed, None otherwise
        
        SMART BE Parameters:
        - ema20_5m: EMA20 del 5m para detectar pérdida de estructura
        - has_bos_against: Flag si hay BOS en contra de la posición
        """
        if not self.position:
            return None
        
        async with self._lock:
            pos = self.position
            pos.update_unrealized_pnl(current_price)
            pos.update_trailing_stop(current_price, atr_1m, market_time)
            
            # Set TP2 if provided and not yet set
            if tp2_target and not pos.tp2_price:
                pos.update_tp2(tp2_target)
            
            # ⚠️ BOS INVALIDATION: Tighten SL BEFORE TP1 (protective exit)
            if has_bos_against and not pos.tp1_hit and not pos.sl_tightened:
                logger.critical("🚨 BOS INVALIDATION: Tesis LONG invalidada - tightening SL")
                pos.tighten_stop_loss(tighten_pct=0.5)  # Move SL 50% closer
            
            # SMART BE: Check si debemos mover SL a BE por rotura de estructura (AFTER TP1)
            if ema20_5m is not None and pos.tp1_hit and not pos.be_moved:
                should_move, reason = pos.check_structure_break(current_price, ema20_5m, has_bos_against)
                if should_move:
                    pos.move_stop_to_breakeven(reason)
            
            # 1. Check stop loss (with grace period for first 10 min)
            sl_check = pos.check_stop_hit(current_price, market_time=market_time)
            # Log SL check periodically for diagnosis (every 1000 ticks to avoid spam)
            if not hasattr(self, '_sl_check_count'):
                self._sl_check_count = 0
            self._sl_check_count += 1
            if self._sl_check_count % 1000 == 0 or sl_check:
                logger.info(f"[MANAGE] #{self._sl_check_count} Price: {current_price:.2f} | SL: {pos.stop_loss:.2f} | Dir: {pos.direction} | SL_Hit: {sl_check}")
            if sl_check:
                return await self._close_position(current_price, 'STOP_LOSS')
            
            # 2. Check TP1
            if pos.check_tp1_hit(current_price):
                return await self._handle_tp1(current_price)
            
            # 3. Check trailing stop (after TP1)
            if pos.check_trailing_stop_hit(current_price):
                return await self._close_position(current_price, 'TRAILING_STOP')
            
            # 4. Check TP2
            if pos.check_tp2_hit(current_price):
                return await self._close_position(current_price, 'TP2')
            
            # 5. Check climactic volume exit
            if self._check_climactic_exit(volume, avg_volume, pos):
                return await self._close_position(current_price, 'CLIMACTIC_VOLUME')
            
            # 6. SMART STAGNATION EXIT - Based on MFE (Maximum Favorable Excursion), not just time
            time_in_trade = pos.get_time_in_trade_minutes(market_time)
            
            # Calculate current R and MFE (max favorable excursion in R terms)
            risk = abs(pos.entry_price - pos.initial_stop_loss)
            if risk > 0:
                if pos.direction == 'LONG':
                    current_r = (current_price - pos.entry_price) / risk
                    mfe_r = (pos.highest_price - pos.entry_price) / risk  # Best price reached
                else:
                    current_r = (pos.entry_price - current_price) / risk
                    mfe_r = (pos.entry_price - pos.lowest_price) / risk
            else:
                current_r = 0
                mfe_r = 0
            
            # SMART STAGNATION LOGIC:
            # - Only exit if trade NEVER showed promise (MFE < 0.3R)
            # - If MFE >= 0.5R, the trade showed expansion - let it play out
            # - Time threshold: 45 min minimum before considering stagnation
            
            if not pos.tp1_hit:
                # STAGNATION: 45+ min AND never made real progress (MFE < 0.3R)
                # This catches truly dead trades, not ones that are building
                if time_in_trade >= 45 and mfe_r < 0.3:
                    logger.warning(f"⏱️ STAGNATION: {time_in_trade:.0f}min, MFE={mfe_r:.2f}R (never expanded) - cutting")
                    return await self._close_position(current_price, 'STAGNATION_EXIT')
                
                # MOMENTUM_DEAD: 60+ min, showed some promise (MFE 0.3-0.8R) but faded
                # The trade tried but failed to continue
                if time_in_trade >= 60 and mfe_r >= 0.3 and mfe_r < 0.8 and current_r < 0.3:
                    logger.warning(f"⏱️ MOMENTUM_DEAD: {time_in_trade:.0f}min, MFE={mfe_r:.2f}R but now {current_r:.2f}R - fading")
                    return await self._close_position(current_price, 'MOMENTUM_DEAD')
                
                # If MFE >= 0.8R, trade showed real expansion - only exit via SL/trailing
                # This lets winners run
                
                # Hard time exit: 90 min - if still no TP1 after 90 min, thesis failed
                if time_in_trade >= RiskManagement.MAX_TRADE_DURATION_MINUTES:
                    logger.warning(f"⏱️ TIME_EXIT: {time_in_trade:.0f}min, MFE={mfe_r:.2f}R, current={current_r:.2f}R")
                    return await self._close_position(current_price, 'TIME_EXIT')
            
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
    
    async def update_from_exchange_position(self, position_data: dict):
        """
        Recover internal state from an existing on-chain position
        position_data: dict from CCXT fetch_position
        """
        async with self._lock:
            try:
                symbol = position_data.get('symbol', 'UNKNOWN')
                contracts = float(position_data.get('contracts', 0))
                
                if contracts == 0:
                    self.position = None
                    return
                
                direction = 'LONG' if contracts > 0 else 'SHORT'
                entry_price = float(position_data.get('entryPrice', 0))
                size = abs(contracts)
                
                # Approximate ATR/Stop since we don't have history
                # This is a fallback recovery mode
                estimated_atr = entry_price * 0.01
                estimated_stop = entry_price * 0.99 if direction == 'LONG' else entry_price * 1.01
                
                self.position = Position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    size=size,
                    stop_loss=estimated_stop,
                    atr_1m=estimated_atr
                )
                
                logger.warning(f"RECOVERED POSITION from exchange: {direction} {size} @ {entry_price}")
                
            except Exception as e:
                logger.error(f"Error updating from exchange position: {e}")

    def has_open_position(self) -> bool:
        """Check if position is open"""
        return self.position is not None

