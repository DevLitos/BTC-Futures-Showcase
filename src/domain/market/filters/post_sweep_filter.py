"""
Post-Sweep Confirmation Filter - Requires break+retest after sweep detection

After detecting a liquidity sweep:
1. Records the sweep zone (high/low)
2. Blocks entries until confirmation:
   - Break: Close above/below sweep zone
   - Retest: Price returns to zone and closes favorably
3. Times out after N candles if no confirmation

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import logging

from src.domain.market.filters.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)

# =========================================================================
# 🔥 FIX #180 (Feb 11, 2026): LOG RATE LIMITING
# =========================================================================
_sweep_rate_limiter: dict[str, datetime] = {}
_SWEEP_RATE_LIMIT_SECONDS = 60

def _should_log_sweep(key: str) -> bool:
    """Rate limit sweep logs - only log once per minute per key."""
    now = datetime.utcnow()
    last_log = _sweep_rate_limiter.get(key)
    if last_log is None or (now - last_log).total_seconds() >= _SWEEP_RATE_LIMIT_SECONDS:
        _sweep_rate_limiter[key] = now
        return True
    return False


@dataclass
class SweepZone:
    """Represents a detected sweep zone waiting for confirmation"""
    sweep_type: str               # 'BULLISH' (swept lows) or 'BEARISH' (swept highs)
    sweep_high: float            # High of the sweep candle
    sweep_low: float             # Low of the sweep candle
    sweep_time: datetime         # When sweep was detected
    pre_sweep_level: float       # Level that was swept (prior swing low/high)
    candles_since_sweep: int = 0 # Counter for timeout (legacy)
    break_confirmed: bool = False
    retest_confirmed: bool = False
    confirmation_complete: bool = False
    expired: bool = False
    # 🔥 FIX #117: Time-based expiration
    expires_at: datetime = None  # type: ignore


class PostSweepConfirmationFilter(BaseFilter):
    """
    Post-Sweep Confirmation Filter.
    
    After a sweep is detected:
    1. For BULLISH sweep (wicked below lows, closed above):
       - Wait for price to break above sweep_high (Break)
       - Then wait for price to retest and close above (Retest)
    2. For BEARISH sweep (wicked above highs, closed below):
       - Wait for price to break below sweep_low (Break)
       - Then wait for price to retest and close below (Retest)
    
    Entry is blocked until both phases complete or timeout.
    """
    
    # Detection parameters
    MIN_WICK_ATR_MULT = 0.3      # Min wick size relative to ATR to count as sweep
    MAX_BODY_PCT = 0.4           # Max body as % of range (sweep = long wick)
    
    # Confirmation parameters
    BREAK_BUFFER_PCT = 0.0005    # 0.05% buffer for break confirmation
    RETEST_TOLERANCE_PCT = 0.002 # 0.2% tolerance for retest zone
    
    # 🔥 FIX #117 (Feb 09, 2026): DYNAMIC TIMEOUT
    # ANTES: Timeout fijo de 15 velas = 15 minutos en cualquier volatilidad
    # AHORA: Timeout basado en ATR - alta vol = más tiempo para confirmar
    # Filosofía: El mercado dicta el tiempo, no un número mágico
    #
    # 🔥 FIX #176 (Feb 11, 2026): REDUCIR TIMEOUT MÁXIMO
    # ANTES: Max 20 minutos → bloqueos de hasta 45 velas (45 min!) observados
    # AHORA: Max 10 minutos = 10 velas → evitar bloqueos excesivos
    # Log Feb 11: BULLISH sweep bloqueó SHORTs por 45 velas (05:18-05:56)
    CONFIRMATION_TIMEOUT_CANDLES = 10  # Fallback (legacy) - was 15
    CONFIRMATION_TIMEOUT_MIN_MINUTES = 3   # Mínimo 3 minutos (was 5)
    CONFIRMATION_TIMEOUT_MAX_MINUTES = 10  # Máximo 10 minutos (was 20)
    CONFIRMATION_ATR_MULT = 2.0  # Timeout = ATR * mult / volatilidad esperada
    
    # 🔥 FIX #176: Límite absoluto de velas para cross-blocking
    CROSS_BLOCK_MAX_CANDLES = 10  # Máximo 10 velas para bloqueo cruzado
    
    # Score adjustments
    PENDING_SWEEP_PENALTY = -2.0    # While waiting for confirmation
    CONFIRMED_SWEEP_BONUS = 0.5     # After confirmation (high-probability entry)
    
    def __init__(self, enabled: bool = True):
        super().__init__(enabled)
        
        # Active sweep zones
        self._pending_sweeps: List[SweepZone] = []
        self._confirmed_sweeps: List[SweepZone] = []
        self._max_pending = 3  # Max sweeps to track
    
    def detect_sweep(self,
                     candle_high: float,
                     candle_low: float,
                     candle_open: float,
                     candle_close: float,
                     prior_swing_low: float,
                     prior_swing_high: float,
                     atr: float,
                     market_time: datetime | None = None) -> Optional[SweepZone]:
        """
        Detect if current candle is a sweep.
        
        A bullish sweep: wicks below prior lows but closes above
        A bearish sweep: wicks above prior highs but closes below
        
        Args:
            market_time: Current market time (important for backtesting)
        """
        candle_range = candle_high - candle_low
        if candle_range <= 0:
            return None
        
        body = abs(candle_close - candle_open)
        body_pct = body / candle_range
        
        # Check body size (sweeps have long wicks, small bodies)
        if body_pct > self.MAX_BODY_PCT:
            return None
        
        # Use market_time for backtest compatibility, fallback to utcnow for live
        now = market_time or datetime.utcnow()
        
        # 🔥 FIX #117: Calculate dynamic timeout based on ATR
        # Alta volatilidad (ATR grande) → más tiempo para confirmar
        # Baja volatilidad (ATR pequeño) → menos tiempo
        # Fórmula: timeout_minutes = clamp(ATR / price * 10000, min, max)
        # Para BTC con ATR típico de $150 y precio $70k: ~21 → clamped a 20min
        # Para ATR bajo ($50): ~7min
        atr_pct = (atr / max(candle_close, 1)) * 10000  # Normalizar
        timeout_minutes = max(
            self.CONFIRMATION_TIMEOUT_MIN_MINUTES,
            min(self.CONFIRMATION_TIMEOUT_MAX_MINUTES, atr_pct)
        )
        expires_at = now + timedelta(minutes=timeout_minutes)
        
        # BULLISH SWEEP: wicked below prior low, closed above it
        if candle_low < prior_swing_low and candle_close > prior_swing_low:
            lower_wick = min(candle_open, candle_close) - candle_low
            if lower_wick >= atr * self.MIN_WICK_ATR_MULT:
                logger.debug(f"BULLISH SWEEP detected: wicked to {candle_low:.2f}, closed at {candle_close:.2f} | timeout={timeout_minutes:.0f}min")
                return SweepZone(
                    sweep_type='BULLISH',
                    sweep_high=candle_high,
                    sweep_low=candle_low,
                    sweep_time=now,
                    pre_sweep_level=prior_swing_low,
                    expires_at=expires_at
                )
        
        # BEARISH SWEEP: wicked above prior high, closed below it
        if candle_high > prior_swing_high and candle_close < prior_swing_high:
            upper_wick = candle_high - max(candle_open, candle_close)
            if upper_wick >= atr * self.MIN_WICK_ATR_MULT:
                logger.debug(f"BEARISH SWEEP detected: wicked to {candle_high:.2f}, closed at {candle_close:.2f} | timeout={timeout_minutes:.0f}min")
                return SweepZone(
                    sweep_type='BEARISH',
                    sweep_high=candle_high,
                    sweep_low=candle_low,
                    sweep_time=now,
                    pre_sweep_level=prior_swing_high,
                    expires_at=expires_at
                )
        
        return None
    
    def check_confirmation(self,
                           sweep: SweepZone,
                           current_high: float,
                           current_low: float,
                           current_close: float,
                           market_time: datetime | None = None) -> SweepZone:
        """
        Check if a pending sweep has been confirmed.
        
        🔥 FIX #117: Now uses time-based expiration instead of fixed candles.
        
        Returns updated SweepZone.
        """
        sweep.candles_since_sweep += 1
        now = market_time or datetime.utcnow()
        
        # 🔥 FIX #117: Time-based timeout (preferred if expires_at is set)
        if sweep.expires_at and now > sweep.expires_at:
            sweep.expired = True
            elapsed = (now - sweep.sweep_time).total_seconds() / 60
            logger.debug(f"⏰ FIX #117: Sweep expired after {elapsed:.1f}min (ATR-based timeout)")
            return sweep
        # Legacy fallback for sweeps without expires_at
        elif not sweep.expires_at and sweep.candles_since_sweep > self.CONFIRMATION_TIMEOUT_CANDLES:
            sweep.expired = True
            logger.debug(f"⏰ Sweep expired after {sweep.candles_since_sweep} candles (legacy)")
            return sweep
        
        if sweep.sweep_type == 'BULLISH':
            # BREAK: Close above sweep_high
            break_level = sweep.sweep_high * (1 + self.BREAK_BUFFER_PCT)
            if not sweep.break_confirmed and current_close > break_level:
                sweep.break_confirmed = True
                logger.info(f"✅ BULLISH SWEEP BREAK confirmed: close {current_close:.2f} > {break_level:.2f}")
            
            # RETEST: After break, price returns to zone and closes above
            if sweep.break_confirmed and not sweep.retest_confirmed:
                retest_zone_top = sweep.sweep_high * (1 + self.RETEST_TOLERANCE_PCT)
                retest_zone_bottom = sweep.pre_sweep_level * (1 - self.RETEST_TOLERANCE_PCT)
                
                # Price touched the zone and closed above
                if current_low <= retest_zone_top and current_close > sweep.pre_sweep_level:
                    sweep.retest_confirmed = True
                    sweep.confirmation_complete = True
                    logger.info(f"✅ BULLISH SWEEP RETEST confirmed: tested {current_low:.2f}, closed {current_close:.2f}")
        
        elif sweep.sweep_type == 'BEARISH':
            # BREAK: Close below sweep_low
            break_level = sweep.sweep_low * (1 - self.BREAK_BUFFER_PCT)
            if not sweep.break_confirmed and current_close < break_level:
                sweep.break_confirmed = True
                logger.info(f"✅ BEARISH SWEEP BREAK confirmed: close {current_close:.2f} < {break_level:.2f}")
            
            # RETEST: After break, price returns to zone and closes below
            if sweep.break_confirmed and not sweep.retest_confirmed:
                retest_zone_bottom = sweep.sweep_low * (1 - self.RETEST_TOLERANCE_PCT)
                retest_zone_top = sweep.pre_sweep_level * (1 + self.RETEST_TOLERANCE_PCT)
                
                if current_high >= retest_zone_bottom and current_close < sweep.pre_sweep_level:
                    sweep.retest_confirmed = True
                    sweep.confirmation_complete = True
                    logger.info(f"✅ BEARISH SWEEP RETEST confirmed: tested {current_high:.2f}, closed {current_close:.2f}")
        
        return sweep
    
    def get_prior_swings(self, 
                         highs: np.ndarray, 
                         lows: np.ndarray,
                         lookback: int = 20) -> Tuple[float, float]:
        """
        Get prior swing high and low from recent data.
        
        Simple method: use min/max of recent data excluding current candle.
        """
        if len(highs) < lookback + 1 or len(lows) < lookback + 1:
            return highs[-2] if len(highs) > 1 else 0, lows[-2] if len(lows) > 1 else 0
        
        prior_high = np.max(highs[-lookback-1:-1])
        prior_low = np.min(lows[-lookback-1:-1])
        
        return prior_high, prior_low
    
    def evaluate(self,
                 df: pd.DataFrame | None = None,
                 current_price: float | None = None,
                 atr: float | None = None,
                 bias: str | None = None,
                 displacement_bypass: bool = False,
                 **kwargs) -> FilterDecision:
        """
        Evaluate post-sweep confirmation conditions.
        
        Args:
            df: OHLC DataFrame (1m or 5m)
            current_price: Current price
            atr: Current ATR
            bias: 'LONG' or 'SHORT'
            displacement_bypass: If True, bypass sweep blocking (strong momentum)
        """
        # =====================================================================
        # 🔥 DISPLACEMENT BYPASS: Strong momentum overrides sweep blocking
        # =====================================================================
        if displacement_bypass:
            return FilterDecision(
                allow=True,
                score_adjust=0.0,
                reasons=["🔥 DISPLACEMENT BYPASS: Sweep filter disabled"],
                tags={'displacement_bypass': True}
            )
        
        if df is None or len(df) < 25 or current_price is None:
            return FilterDecision.allow_default()
        
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        opens = df['open'].to_numpy()
        closes = df['close'].to_numpy()
        
        current_high = highs[-1]
        current_low = lows[-1]
        current_open = opens[-1]
        current_close = closes[-1]
        
        if atr is None:
            # Estimate ATR
            tr_values = []
            for i in range(1, min(15, len(highs))):
                tr = max(highs[-i] - lows[-i], 
                        abs(highs[-i] - closes[-i-1]),
                        abs(lows[-i] - closes[-i-1]))
                tr_values.append(tr)
            atr_value = float(np.mean(tr_values)) if tr_values else (current_high - current_low)
        else:
            atr_value = float(atr)
        
        # Get prior swings
        prior_swing_high, prior_swing_low = self.get_prior_swings(highs, lows)
        
        # Check for new sweep
        new_sweep = self.detect_sweep(
            current_high, current_low, current_open, current_close,
            prior_swing_low, prior_swing_high, atr_value
        )
        
        if new_sweep and len(self._pending_sweeps) < self._max_pending:
            self._pending_sweeps.append(new_sweep)
        
        # Update pending sweeps
        updated_pending = []
        confirmed_this_tick = []  # Collect all confirmations for consolidated log
        
        for sweep in self._pending_sweeps:
            sweep = self.check_confirmation(sweep, current_high, current_low, current_close)
            
            if sweep.confirmation_complete:
                self._confirmed_sweeps.append(sweep)
                confirmed_this_tick.append(sweep.sweep_type)
            elif not sweep.expired:
                updated_pending.append(sweep)
        
        # Consolidated log for all confirmations this tick
        if confirmed_this_tick:
            if len(confirmed_this_tick) == 1:
                logger.info(f"🎯 SWEEP CONFIRMATION COMPLETE: {confirmed_this_tick[0]}")
            else:
                logger.info(f"🎯 SWEEP CONFIRMATION COMPLETE: {len(confirmed_this_tick)}x ({', '.join(confirmed_this_tick)})")
        
        self._pending_sweeps = updated_pending
        
        # Keep only recent confirmed sweeps
        self._confirmed_sweeps = [s for s in self._confirmed_sweeps 
                                  if s.candles_since_sweep < 30]
        
        # Build tags
        tags: dict[str, int | str | bool] = {
            'sweep_pending_count': len(self._pending_sweeps),
            'sweep_confirmed_count': len(self._confirmed_sweeps),
        }
        
        # Check if any pending sweep blocks entry
        for sweep in self._pending_sweeps:
            if bias == 'LONG' and sweep.sweep_type == 'BULLISH' and not sweep.confirmation_complete:
                # Block LONG until bullish sweep confirms
                tags['sweep_blocking'] = True
                tags['sweep_type'] = 'BULLISH'
                tags['sweep_break_confirmed'] = sweep.break_confirmed
                tags['sweep_candles_pending'] = sweep.candles_since_sweep
                
                if not sweep.break_confirmed:
                    return FilterDecision(
                        allow=False,
                        reasons=[f"Bullish sweep pending BREAK confirmation ({sweep.candles_since_sweep} candles)"],
                        tags=tags
                    )
                else:
                    # Break confirmed, waiting for retest - allow with penalty
                    return FilterDecision(
                        allow=True,
                        score_adjust=self.PENDING_SWEEP_PENALTY / 2,
                        reasons=[f"Bullish sweep pending RETEST ({sweep.candles_since_sweep} candles)"],
                        tags=tags
                    )
            
            elif bias == 'SHORT' and sweep.sweep_type == 'BEARISH' and not sweep.confirmation_complete:
                tags['sweep_blocking'] = True
                tags['sweep_type'] = 'BEARISH'
                tags['sweep_break_confirmed'] = sweep.break_confirmed
                tags['sweep_candles_pending'] = sweep.candles_since_sweep
                
                if not sweep.break_confirmed:
                    return FilterDecision(
                        allow=False,
                        reasons=[f"Bearish sweep pending BREAK confirmation ({sweep.candles_since_sweep} candles)"],
                        tags=tags
                    )
                else:
                    return FilterDecision(
                        allow=True,
                        score_adjust=self.PENDING_SWEEP_PENALTY / 2,
                        reasons=[f"Bearish sweep pending RETEST ({sweep.candles_since_sweep} candles)"],
                        tags=tags
                    )
            
            # =========================================================================
            # 🔥 CRITICAL FIX (Jan 17, 2026): Cross-direction blocking
            # BEARISH sweep = upside liquidity grab → DON'T BUY (bulls trapped)
            # BULLISH sweep = downside liquidity grab → DON'T SHORT (bears trapped)
            #
            # 🔥 FIX #176 (Feb 11, 2026): Límite de tiempo para cross-blocking
            # ANTES: Cross-blocking duraba hasta que el sweep expiraba (hasta 45+ velas)
            # AHORA: Cross-blocking máximo 10 velas, luego permite con penalty
            # Log Feb 11: BULLISH sweep bloqueó SHORTs por 45 velas innecesariamente
            # =========================================================================
            elif bias == 'LONG' and sweep.sweep_type == 'BEARISH' and not sweep.confirmation_complete:
                # BEARISH sweep = bears in control, bulls trapped → BLOCK LONGS
                tags['sweep_cross_blocking'] = True
                tags['sweep_type'] = 'BEARISH'
                tags['sweep_break_confirmed'] = sweep.break_confirmed
                tags['sweep_candles_pending'] = sweep.candles_since_sweep
                
                # 🔥 FIX #176: Después de CROSS_BLOCK_MAX_CANDLES, permitir con penalty
                if sweep.candles_since_sweep > self.CROSS_BLOCK_MAX_CANDLES:
                    # 🔥 FIX #180: Rate limit - only log once per minute
                    if _should_log_sweep('cross_block_expired_long'):
                        logger.info(f"⚠️ FIX #176: Cross-block expired after {sweep.candles_since_sweep} candles → allowing LONG with penalty")
                    return FilterDecision(
                        allow=True,
                        score_adjust=self.PENDING_SWEEP_PENALTY,  # -2.0 penalty
                        reasons=[f"⚠️ BEARISH sweep cross-block EXPIRED ({sweep.candles_since_sweep} > {self.CROSS_BLOCK_MAX_CANDLES}) → allowing with penalty"],
                        tags=tags
                    )
                
                return FilterDecision(
                    allow=False,
                    reasons=[f"❌ BEARISH sweep in progress → LONGs BLOCKED (bulls trapped) ({sweep.candles_since_sweep} candles)"],
                    tags=tags
                )
            
            elif bias == 'SHORT' and sweep.sweep_type == 'BULLISH' and not sweep.confirmation_complete:
                # BULLISH sweep = bulls in control, bears trapped → BLOCK SHORTS
                tags['sweep_cross_blocking'] = True
                tags['sweep_type'] = 'BULLISH'
                tags['sweep_break_confirmed'] = sweep.break_confirmed
                tags['sweep_candles_pending'] = sweep.candles_since_sweep
                
                # 🔥 FIX #176: Después de CROSS_BLOCK_MAX_CANDLES, permitir con penalty
                if sweep.candles_since_sweep > self.CROSS_BLOCK_MAX_CANDLES:
                    # 🔥 FIX #180: Rate limit - only log once per minute
                    if _should_log_sweep('cross_block_expired_short'):
                        logger.info(f"⚠️ FIX #176: Cross-block expired after {sweep.candles_since_sweep} candles → allowing SHORT with penalty")
                    return FilterDecision(
                        allow=True,
                        score_adjust=self.PENDING_SWEEP_PENALTY,  # -2.0 penalty
                        reasons=[f"⚠️ BULLISH sweep cross-block EXPIRED ({sweep.candles_since_sweep} > {self.CROSS_BLOCK_MAX_CANDLES}) → allowing with penalty"],
                        tags=tags
                    )
                
                return FilterDecision(
                    allow=False,
                    reasons=[f"❌ BULLISH sweep in progress → SHORTs BLOCKED (bears trapped) ({sweep.candles_since_sweep} candles)"],
                    tags=tags
                )
        
        # Check if we have a recent confirmed sweep (bonus)
        for sweep in self._confirmed_sweeps:
            if bias == 'LONG' and sweep.sweep_type == 'BULLISH' and sweep.candles_since_sweep < 10:
                tags['sweep_confirmed'] = True
                tags['sweep_type'] = 'BULLISH'
                return FilterDecision(
                    allow=True,
                    score_adjust=self.CONFIRMED_SWEEP_BONUS,
                    reasons=["Recent bullish sweep confirmed"],
                    tags=tags
                )
            elif bias == 'SHORT' and sweep.sweep_type == 'BEARISH' and sweep.candles_since_sweep < 10:
                tags['sweep_confirmed'] = True
                tags['sweep_type'] = 'BEARISH'
                return FilterDecision(
                    allow=True,
                    score_adjust=self.CONFIRMED_SWEEP_BONUS,
                    reasons=["Recent bearish sweep confirmed"],
                    tags=tags
                )
        
        # No active sweep situation
        return FilterDecision(
            allow=True,
            reasons=["No sweep situation"],
            tags=tags
        )
    
    def clear_sweeps(self):
        """Clear all tracked sweeps (for testing)"""
        self._pending_sweeps = []
        self._confirmed_sweeps = []


# Singleton
_post_sweep_filter: Optional[PostSweepConfirmationFilter] = None


def get_post_sweep_filter(enabled: bool = True) -> PostSweepConfirmationFilter:
    global _post_sweep_filter
    if _post_sweep_filter is None:
        _post_sweep_filter = PostSweepConfirmationFilter(enabled=enabled)
    return _post_sweep_filter


def reset_post_sweep_filter():
    global _post_sweep_filter
    _post_sweep_filter = None
