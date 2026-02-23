"""
Risk Engine for BTC Perpetual Trading System.
Handles: Position sizing, risk limits, exit management.

Position class has been extracted to position.py for maintainability.
"""

import logging
import asyncio
from datetime import datetime, UTC
from typing import Optional, Dict
from src.infrastructure.monitoring.metrics import ORDERS_RECOMMENDED

from src.config.trading_config import (
    RiskManagement, OrderExecution,
    get_risk_for_session,
    TradeType
)
from src.shared.utils import (
    ensure_utc, PositionSizer, get_position_sizer
)
from src.infrastructure.exchanges.kraken.execution import KRAKEN_BTC_MIN_SIZE
from src.domain.risk.position import (
    Position, REGIME_NORMAL, REGIME_RUNNER,
    RUNNER_PROMOTION_MFE, RUNNER_TIME_EXIT_CANDLES,
    normalize_regime_mode,
)

logger = logging.getLogger(__name__)


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
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
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
                       entry_conditions: dict | None = None,
                       edge_type: str | None = None,
                       entry_score: float | None = None,
                       is_weekend: bool = False,
                       bos_quality: str = 'NORMAL',
                       displacement_origin: float | None = None) -> Position:  # 🔥 NEW (Feb 04)
        """
        Create a new position object for tracking.
        
        UPDATED (Jan 23, 2026): Also records entry for anti-reentry tracking
        UPDATED (Jan 30, 2026): Added edge_type for TIME_INVALIDATION relaxation
        UPDATED (Feb 01, 2026): Added entry_score for TIME_INVALIDATION strictness
        UPDATED (Feb 01, 2026): Added is_weekend for stricter weekend exits
        UPDATED (Feb 03, 2026): Added bos_quality for dynamic trailing adjustment
        UPDATED (Feb 04, 2026): Added displacement_origin for reclaim exit
        """
        # =====================================================================
        # 🔥 FIX #183b (Feb 11, 2026): IDEMPOTENT GUARD
        # If position already exists with same direction/entry, return it
        # This prevents duplicate position creation during timeout recovery
        # =====================================================================
        if self.position is not None:
            existing = self.position
            # Check if this is the same position (same direction, similar price)
            price_match = abs(existing.entry_price - entry_price) < 1.0  # $1 tolerance
            direction_match = existing.direction == direction
            if price_match and direction_match:
                logger.warning(f"🔄 FIX #183b: Position already exists - returning existing ({existing.direction} @ {existing.entry_price:.2f})")
                return existing
            else:
                logger.warning(f"⚠️ FIX #183b: Position exists but different! Existing: {existing.direction} @ {existing.entry_price:.2f}, New: {direction} @ {entry_price:.2f}")
                # Return existing to prevent overwrite
                return existing
        
        self.position = Position(
            symbol=symbol or 'UNKNOWN',
            direction=direction or 'UNKNOWN',
            entry_price=entry_price,
            size=position_size,
            stop_loss=stop_loss,
            atr_1m=atr_1m,
            market_time=market_time,
            trade_type=trade_type,
            regime_mode=regime_mode,  # 🔥 Pass regime for trailing
            edge_type=edge_type,  # 🔥 FIX (Jan 30): Pass edge_type for TIME_INVALIDATION
            entry_score=entry_score,  # 🔥 FIX (Feb 01): Pass entry_score for stricter TIME_INVALIDATION
            is_weekend=is_weekend,  # 🔥 FIX (Feb 01): Pass is_weekend for stricter exits
            bos_quality=bos_quality,  # 🔥 NEW (Feb 03): Pass bos_quality for dynamic trailing
            displacement_origin=displacement_origin  # 🔥 NEW (Feb 04): Pass origin for reclaim exit
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
                    
                    # FIX #84: Reducido a DEBUG para eliminar spam
                    logger.debug(f"💰 Capital actualizado: ${self.capital:.2f} {currency} (Libre: ${free:.2f}, En uso: ${used:.2f})")
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
        
        🔥 FIX #188 (Feb 11, 2026): SL is CAPPED at 3.0 ATR (reduced from 4.0)
        - SL cannot exceed 3.0 ATR from entry (prevents absurd 1R distances)
        - Trade that lost: swing_high at 3.4× ATR → 1R=$1,200 → MFE 0.26R → TIME_INVALIDATION
        
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
        
        # 🔥 FIX #188 (Feb 11, 2026): CAP SL DISTANCE MORE AGGRESSIVELY
        # PROBLEMA: SL cap de 4× ATR era demasiado laxo.
        # - Con ATR=$354, max SL = $1,416 → 1R muy grande
        # - Si swing_high está a $1,200, 1R = $1,200
        # - MFE de $300 = solo 0.25R → TIME_INVALIDATION a -0.40R
        # SOLUCIÓN: Reducir cap a 3× ATR (en lugar de 4×)
        # - Con ATR=$354, max SL = $1,062 → trailing se activa más rápido
        max_atr_multiple = 3.0  # 🔥 FIX #188: Reduced from 4.0 to 3.0
        max_sl_distance = max_atr_multiple * atr_1m
        
        # Minimum SL distance = dynamic ATR (survive noise in BTC)
        min_sl_distance = atr_multiplier * atr_1m
        
        if direction == 'LONG':
            structure_stop = support_resistance - buffer
            # SL must be at least min_sl_distance below entry
            atr_stop = entry_price - min_sl_distance
            stop = min(structure_stop, atr_stop)  # Use the LOWER (further) one
            
            # 🔥 FIX #188: Apply max cap (3× ATR instead of 4×)
            max_stop = entry_price - max_sl_distance
            if stop < max_stop:
                logger.warning(f"⚠️ FIX #188 SL capped: {stop:.2f} → {max_stop:.2f} (max {max_atr_multiple}× ATR)")
                stop = max_stop
        else:  # SHORT
            structure_stop = support_resistance + buffer
            # SL must be at least min_sl_distance above entry  
            atr_stop = entry_price + min_sl_distance
            stop = max(structure_stop, atr_stop)  # Use the HIGHER (further) one
            
            # 🔥 FIX #188: Apply max cap (3× ATR instead of 4×)
            max_stop = entry_price + max_sl_distance
            if stop > max_stop:
                logger.warning(f"⚠️ FIX #188 SL capped: {stop:.2f} → {max_stop:.2f} (max {max_atr_multiple}× ATR)")
                stop = max_stop
        
        logger.debug(f"Stop loss calculated: {stop:.2f} (Structure: {support_resistance:.2f}±{buffer:.2f}, ATR min: {min_sl_distance:.2f} [{atr_multiplier}x])")
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
            
            # 🔥 FIX #150: Log periódico del R actual (cada ~30s)
            current_r_log = pos.current_r_multiple if hasattr(pos, 'current_r_multiple') else 0
            mfe_r_log = pos.calculate_mfe_r() if hasattr(pos, 'calculate_mfe_r') else 0
            import time
            now = time.time()
            last_log = getattr(self, '_last_r_log_time', 0)
            if now - last_log >= 30.0:  # Log cada 30 segundos
                logger.info(
                    f"📈 POSITION R: current={current_r_log:.3f}R | MFE={mfe_r_log:.3f}R | "
                    f"price={current_price:.1f} | entry={pos.entry_price:.1f} | "
                    f"dir={pos.direction}"
                )
                self._last_r_log_time = now
            
            trade_tags = getattr(pos, 'trade_tags', {}) or {}
            
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
            # 🔥 FIX (Feb 01, 2026): INTELLIGENT MULTI-TICK DETECTION
            # The price_gap_pct passed here is now pre-filtered by trading_app.py:
            # - Single tick: Only passed if > 1.5% (massive gap)
            # - Multi-tick: Passed if 2+ of last 3 ticks > 0.8% (confirmed trend)
            # This eliminates false positives from isolated tick spikes
            # 
            # The 0.8% threshold here is a SAFETY NET (should rarely trigger
            # since filtering is done upstream)
            if price_gap_pct is not None and abs(price_gap_pct) > 0.8:
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
            
            # HARD BACKSTOP: -0.50R absolute limit (catastrophic protection)
            # FIX (Feb 20): Tightened from -1.0R. With $230 capital, -1.0R = $1.50 loss = death.
            HARD_BACKSTOP_R = -0.50
            if current_r < HARD_BACKSTOP_R:
                logger.error(
                    f"🚨🚨 HARD BACKSTOP EXIT: current_r={current_r:.3f}R < limit={HARD_BACKSTOP_R}R | "
                    f"CRITICAL: Trade exceeded {HARD_BACKSTOP_R}R, closing IMMEDIATELY"
                )
                return await self._close_position(current_price, f'HARD_BACKSTOP (R={current_r:.3f})')
            
            # ================================================================
            # 🚨 FIX (Feb 20, 2026): EMERGENCY MAX LOSS — defense in depth
            # Fires during management cooldown, for adopted positions, always.
            # This is the SECOND layer (first is in trading_app.py).
            # ================================================================
            EMERGENCY_MAX_LOSS_R = -0.20
            if current_r < EMERGENCY_MAX_LOSS_R:
                logger.warning(
                    f"🚨 EMERGENCY MAX LOSS (risk_engine): R={current_r:.3f} < {EMERGENCY_MAX_LOSS_R} | "
                    f"Bypassing cooldown/adopted/regime checks"
                )
                return await self._close_position(current_price, 'TIME_INVALIDATION_MAX_LOSS')
            
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
                pos.update_trailing_stop(current_price, atr_1m, market_time,
                                         regime_hint=pos.regime_mode)
            
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
                
                # 🔥🔥🔥 FIX #101 (Feb 09, 2026): CONTEXTUAL EARLY EXIT
                # Now uses: regime, BB compression, volume, delta, session, ATR
                # Instead of just "MFE < 0.25R in 6 candles = exit"
                
                # Calculate BB width percentile (proxy for compression detection)
                bb_width_percentile = None
                if bb_width is not None and bb_avg is not None and bb_avg > 0:
                    # bb_width / bb_avg < 0.5 → ~20 percentile (compressed)
                    # bb_width / bb_avg > 1.5 → ~80 percentile (expanded)
                    bb_ratio = bb_width / bb_avg
                    bb_width_percentile = min(100, max(0, bb_ratio * 50))  # Rough percentile
                
                # Get session from market_time
                session_str = None
                if market_time:
                    hour = market_time.hour
                    if 0 <= hour < 8:
                        session_str = 'ASIA'
                    elif 8 <= hour < 13:
                        session_str = 'LONDON'
                    elif 13 <= hour < 17:
                        session_str = 'LONDON_NY_OVERLAP'
                    elif 17 <= hour < 22:
                        session_str = 'NY'
                    else:
                        session_str = 'DEAD_HOURS'
                
                # Get delta_ratio from position tags if available
                trade_tags = getattr(pos, 'trade_tags', {}) or {}
                delta_ratio = trade_tags.get('volume_delta_ratio', None)
                
                # 🔥 FIX #144b: Get micro_trend from position tags
                micro_trend = trade_tags.get('micro_trend', None)
                
                # ATR average (use atr_1m as proxy for avg if not available)
                atr_avg_val = pos.atr_1m * 1.0 if pos.atr_1m else None  # TODO: track rolling avg
                
                early_mfe_check = pos.check_early_mfe_exit(
                    current_price=current_price,
                    bb_width_percentile=bb_width_percentile,
                    volume_ratio=volume_ratio,
                    delta_ratio=delta_ratio,
                    session=session_str,
                    atr_1m=atr_1m,
                    atr_avg=atr_avg_val,
                    micro_trend=micro_trend
                )
                if early_mfe_check and early_mfe_check.get('action') == 'CLOSE':
                    exit_reason = early_mfe_check.get('reason', 'EARLY_MFE_EXIT')
                    exit_type = early_mfe_check.get('exit_type', 'unknown')
                    logger.warning(
                        f"⚡ {exit_reason} triggered: MFE={early_mfe_check.get('mfe_r', 0):.2f}R, "
                        f"current={early_mfe_check.get('current_r', 0):.2f}R | "
                        f"Type={exit_type} | Cutting loss"
                    )
                    return await self._close_position(current_price, exit_reason)
                
                # 0. Check mediocre trade exit (TIME_INVALIDATION INTELIGENTE)
                # WEEKEND: n_candles=6 (cortar pérdidas rápido con baja liquidez)
                # Normal: n_candles=12 (BTC needs 10-20 min for moves to develop)
                is_weekend_pos = getattr(pos, 'is_weekend', False)
                ti_n_candles = 6 if is_weekend_pos else 12
                time_check = pos.check_mediocre_trade_exit(
                    n_candles=ti_n_candles, 
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
                
                # ============================================================
                # 0.6. ANTI-ROUNDTRIP PROTECTION - REMOVED (Feb 4, 2026)
                # ============================================================
                # 🔥 FIX: Removed because R-LOCK tiers now use initial_entry_price
                # If MFE >= 0.5R, Tier 2 locks stop at -0.2R from ORIGINAL entry
                # This makes anti-roundtrip redundant - the SL protects us
                # The bug was: after ADD, entry_price changed so R-LOCK calculated wrong
                # Now R-LOCK uses initial_entry_price → consistent protection
                # ============================================================
                
                # ============================================================
                # 0.65. DISPLACEMENT ORIGIN RECLAIM (Feb 4, 2026)
                # If price reclaims the displacement origin, thesis invalidated
                # This is a structural exit, not just trailing stop
                # ============================================================
                reclaim_check = pos.check_displacement_reclaim(current_price)
                if reclaim_check and reclaim_check.get('action') == 'CLOSE':
                    reason = f"DISPLACEMENT_RECLAIM: {reclaim_check.get('reason', '')}"
                    return await self._close_position(current_price, reason)
                
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
                
                # 1.5 🔥 EARLY PARTIAL — RANGE closes 100% at 0.30R, others at configured level
                if pos.check_early_partial_hit(current_price):
                    is_range = pos.regime_mode in ('RANGE', 'MEAN_REVERSION', 'CHOPPY_DEAD')
                    label = "RANGE FULL CLOSE 0.30R" if is_range else "EARLY PARTIAL"
                    logger.info(f"💰 {label} @ {current_price:.2f}")
                    return await self._handle_early_partial(pos, current_price)
                
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
    
    async def _handle_early_partial(self, pos: Position, current_price: float) -> dict | None:
        """
        Handle early partial close — REGIME-AWARE.
        
        🔥 FIX (Feb 20, 2026): RANGE FULL CLOSE
        - RANGE: Close 100% at +0.30R (full position, no runner in range)
          Data: scaled partial (30%) left 70% to reverse → destroyed +$3.59 in 5 days
        - Other: Close 30% at configured level, move SL to +0.05R
        """
        if pos.early_partial_done:
            return None
        
        # RANGE: close 100% and always move SL to BE
        is_range = pos.regime_mode in ('RANGE', 'MEAN_REVERSION', 'CHOPPY_DEAD')
        early_pct = 100 if is_range else RiskManagement.EARLY_PARTIAL_PCT
        
        # Calculate partial close
        close_size = pos.size * (early_pct / 100.0)
        remaining_size = pos.size - close_size
        
        # 🔒 Validate minimum order sizes
        if remaining_size < KRAKEN_BTC_MIN_SIZE:
            logger.info(f"🔄 EARLY PARTIAL: Remaining {remaining_size:.6f} < min, closing FULL")
            return await self._close_position(current_price, 'EARLY_PARTIAL_FULL')
        
        if close_size < KRAKEN_BTC_MIN_SIZE:
            logger.info(f"🔄 EARLY PARTIAL: Close size {close_size:.6f} < min, skipping")
            pos.early_partial_done = True
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
        pos.early_partial_done = True
        
        # Move SL: ALWAYS move to +0.05R after partial (never let winner become loser)
        # Data: Trades with 0.7R partial but no SL move → 70% remaining gives back all profit
        old_sl = pos.stop_loss
        risk = abs(pos.initial_entry_price - pos.initial_stop_loss)
        if risk > 0:
            # +0.05R offset: covers spread + slippage, guarantees profit on remainder
            offset = risk * 0.05
            new_sl = pos.entry_price + offset if pos.direction == 'LONG' else pos.entry_price - offset
        else:
            new_sl = pos.entry_price
        pos.stop_loss = new_sl
        pos.trailing_stop = new_sl
        pos.be_moved = True
        logger.info(f"🔒 SL moved to +0.05R ({new_sl:.2f}) after {'RANGE' if is_range else 'early'} partial: {old_sl:.2f} → {new_sl:.2f}")
        
        partial_r_label = '0.40R' if is_range else f'+{RiskManagement.EARLY_PARTIAL_R}R'
        logger.info(
            f"💰 {'RANGE' if is_range else 'EARLY'} PARTIAL @ {partial_r_label} - "
            f"Closed {close_size:.4f} ({early_pct}%) @ {current_price:.2f} | "
            f"PnL: ${pnl:.2f} | Remaining: {remaining_size:.4f}"
        )
        
        return {
            'action': 'PARTIAL_CLOSE',
            'reason': 'RANGE_PARTIAL' if is_range else 'EARLY_PARTIAL',
            'price': current_price,
            'size': close_size,
            'pnl': pnl,
            'remaining_size': remaining_size,
            'partial_pct': early_pct,
            'requires_validation': True
        }

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
    
    async def update_from_exchange_position(self, position_data: dict, atr: float | None = None):
        """
        Recover internal state from an existing on-chain position
        position_data: dict from CCXT fetch_position
        atr: optional ATR value for proper SL calculation
        
        CCXT unified position structure:
        - 'side': 'long' or 'short' (most reliable for direction)
        - 'contracts': can be signed (+ long, - short) or unsigned depending on exchange
        - 'info': raw exchange response
        
        🔥 FIX (Feb 4, 2026): Also syncs PositionStore to prevent desync warnings
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
                
                # 🔥 FIX (Feb 4, 2026): Sync PositionStore to prevent desync warnings
                # When RiskEngine recovers a position, PositionStore must also know about it
                # Otherwise sync_from_exchange will report mismatch (local=0, exchange=X)
                try:
                    from src.infrastructure.storage import get_position_store
                    import uuid as uuid_mod
                    from src.infrastructure.storage.parquet.position_store import TrackedPosition
                    
                    position_store = get_position_store()
                    ps_pos = position_store.get_position(symbol)
                    
                    if ps_pos is None or ps_pos.is_flat:
                        # PositionStore doesn't know about this position - update it
                        position_store._positions[symbol] = TrackedPosition(
                            symbol=symbol,
                            side=direction,
                            size=size,
                            entry_price=entry_price,
                            updated_at=datetime.now(UTC),
                            trade_id=f"recovered_{uuid_mod.uuid4().hex[:8]}",
                        )
                        logger.info(f"✅ PositionStore synced with recovered position: {direction} {size:.6f}")
                    elif abs(ps_pos.size - size) > MIN_POSITION_SIZE:
                        # PositionStore has different size - correct it
                        ps_pos.size = size
                        ps_pos.side = direction
                        ps_pos.entry_price = entry_price
                        ps_pos.updated_at = datetime.now(UTC)
                        logger.info(f"✅ PositionStore size corrected: {ps_pos.size:.6f} → {size:.6f}")
                except Exception as ps_error:
                    # Don't fail recovery if PositionStore sync fails - log and continue
                    logger.warning(f"⚠️ Failed to sync PositionStore during recovery: {ps_error}")
                
            except Exception as e:
                logger.error(f"Error updating from exchange position: {e}")

    def has_open_position(self) -> bool:
        """Check if position is open"""
        return self.position is not None

