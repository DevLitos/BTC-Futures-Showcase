"""
Pullback Short Engine - Tactical Shorts in Bullish Markets

Captures corrections (e.g., 97k → 92k) within bullish HTF cycles.

DESIGN PRINCIPLES (NON-NEGOTIABLE):
1. HTF filter NOT touched - bot remains "primarily LONG" when HTF LONG
2. This is a SECOND type of short (tactical, not trend reversal)
3. Risk and exits are DISTINCT from main strategy
4. Feature flags + kill switch for all new behavior

WHAT IT IS:
- Tactical shorts when price is overextended in a bullish market
- Gate system: Extension → Failure/Trigger → No Breakout
- Smaller size, tighter stops, quicker targets

WHAT IT IS NOT:
- NOT a trend reversal signal
- NOT indiscriminate mean-reversion
- Does NOT average losses
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, UTC, timedelta
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List, Any
from enum import Enum

from engine.config import PullbackShortConfig, TradeType
from engine.indicators import (
    calculate_extension_z,
    detect_swing_high,
    detect_breakout_impulse,
    calculate_activity_score,
    calculate_ema,
    calculate_atr,
    calculate_rsi,
    calculate_vwap
)

logger = logging.getLogger(__name__)


class PullbackGate(str, Enum):
    """Gates that must pass for pullback short entry"""
    EXTENSION = "EXTENSION"      # Price extended from EMA
    TRIGGER = "TRIGGER"          # Failure/reversal confirmed
    NO_BREAKOUT = "NO_BREAKOUT"  # Not a real breakout


@dataclass
class GateResult:
    """Result of a gate check"""
    passed: bool
    gate: PullbackGate
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PullbackShortSignal:
    """Complete signal for pullback short entry"""
    valid: bool
    entry_price: float
    stop_loss: float
    tp1_price: float
    tp2_price: Optional[float]
    size_factor: float
    swing_high: float
    swing_high_index: int                    # Index of swing high candle
    confirm_level: float                     # Low of swing high candle
    extension_z: float
    trigger_type: str
    activity_score: float
    entry_mode: str = "RETEST"               # RETEST or MARKET
    retest_level: float = 0.0                # Level for LIMIT order if RETEST
    gate_results: List[GateResult] = field(default_factory=list)
    block_reason: Optional[str] = None
    trade_type: TradeType = TradeType.SHORT_PULLBACK
    tags: Dict[str, Any] = field(default_factory=dict)


class PullbackShortEngine:
    """
    Engine for detecting and managing tactical pullback shorts.
    
    This is a SEPARATE signal engine that runs alongside the main strategy.
    It only generates SHORT_PULLBACK signals when:
    1. HTF bias is LONG (we're in a bullish market)
    2. Price is overextended (Gate 1)
    3. Failure/reversal is confirmed (Gate 2)
    4. Not in a real breakout (Gate 3)
    
    Priority: Main strategy (LONG) has priority unless blocked by location/anchor.
    """
    
    def __init__(self, config: Optional[PullbackShortConfig] = None):
        """
        Initialize the Pullback Short Engine.
        
        Args:
            config: Configuration object (uses defaults if None)
        """
        self.config = config or PullbackShortConfig
        
        # State tracking
        self._breakout_cooldown_until: Optional[datetime] = None
        self._daily_trade_count: int = 0
        self._last_trade_date: Optional[datetime] = None
        self._consecutive_losses: int = 0
        self._disabled_until: Optional[datetime] = None
        self._cumulative_pnl: float = 0.0  # Track daily cumulative PnL
        
        # =====================================================================
        # SWING HIGH TRACKING (with validity)
        # =====================================================================
        self._last_swing_high: float = 0.0
        self._last_swing_high_time: Optional[datetime] = None
        self._last_swing_high_index: int = 0       # Candle index
        self._last_swing_high_candle_low: float = 0.0  # confirm_level
        self._swing_confirmed: bool = False
        self._trigger_fired: bool = False          # Close below confirm_level happened
        
        # Gate cache (avoid recalculating every tick)
        self._gate_cache_ts: int = 0
        self._gate_cache: Dict[str, GateResult] = {}
        
        logger.info("PullbackShortEngine initialized")
    
    def is_enabled(self, is_live: bool = False, market_time: datetime = None) -> Tuple[bool, str]:
        """
        Check if pullback shorts are enabled.
        
        Args:
            is_live: Whether we're in live trading mode
            market_time: Current market time
        
        Returns:
            Tuple of (is_enabled, reason)
        """
        if not self.config.ENABLED:
            return False, "PullbackShortConfig.ENABLED is False"
        
        if is_live and not self.config.LIVE_ALLOWED:
            return False, "Live trading not allowed (SHORT_PULLBACK_LIVE_ALLOWED=False)"
        
        # Check if temporarily disabled
        if self._disabled_until and market_time:
            if market_time < self._disabled_until:
                remaining = (self._disabled_until - market_time).total_seconds()
                return False, f"Temporarily disabled ({remaining:.0f}s remaining)"
            else:
                self._disabled_until = None  # Clear expired disable
        
        # Check daily trade limit
        if market_time:
            self._check_daily_reset(market_time)
            if self._daily_trade_count >= self.config.MAX_TRADES_PER_DAY:
                return False, f"Daily limit reached ({self._daily_trade_count}/{self.config.MAX_TRADES_PER_DAY})"
        
        # Check consecutive losses
        if self._consecutive_losses >= self.config.MAX_CONSECUTIVE_LOSSES:
            return False, f"Consecutive losses limit ({self._consecutive_losses}/{self.config.MAX_CONSECUTIVE_LOSSES})"
        
        return True, "Enabled"
    
    def _check_daily_reset(self, market_time: datetime):
        """Reset daily counter if new day"""
        if self._last_trade_date is None:
            self._last_trade_date = market_time.date()
            return
        
        if market_time.date() != self._last_trade_date:
            logger.info(f"[PULLBACK] Daily reset: {self._daily_trade_count} trades yesterday")
            self._daily_trade_count = 0
            self._last_trade_date = market_time.date()
    
    def evaluate(
        self,
        current_price: float,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        df_1m: Optional[pd.DataFrame],
        htf_bias: str,
        regime: str,
        market_time: datetime,
        has_integrity_errors: bool = False,
        has_macro_event: bool = False
    ) -> PullbackShortSignal:
        """
        Evaluate whether a pullback short signal is valid.
        
        This is the main entry point for signal generation.
        
        Args:
            current_price: Current market price
            df_1h: 1H candle DataFrame
            df_5m: 5M candle DataFrame
            df_1m: 1M candle DataFrame (optional, for fine triggers)
            htf_bias: Current HTF bias ('LONG', 'SHORT', None)
            regime: Current market regime
            market_time: Current market time
            has_integrity_errors: Whether there are position/order sync errors
            has_macro_event: Whether a macro event is detected
        
        Returns:
            PullbackShortSignal with validity and parameters
        """
        # Initialize result
        signal = PullbackShortSignal(
            valid=False,
            entry_price=0.0,
            stop_loss=0.0,
            tp1_price=0.0,
            tp2_price=None,
            size_factor=self.config.SIZE_FACTOR,
            swing_high=0.0,
            swing_high_index=0,
            confirm_level=0.0,
            extension_z=0.0,
            trigger_type="",
            activity_score=0.0,
            entry_mode=self.config.ENTRY_MODE,
            retest_level=0.0
        )
        
        # =====================================================================
        # PRE-CHECKS: Can we even evaluate?
        # =====================================================================
        
        # 1. Must be enabled
        is_enabled, reason = self.is_enabled(is_live=False, market_time=market_time)
        if not is_enabled:
            signal.block_reason = f"Disabled: {reason}"
            return signal
        
        # 2. HTF must be LONG (we're looking for pullbacks in bullish market)
        if htf_bias != 'LONG':
            signal.block_reason = f"HTF bias is {htf_bias}, not LONG"
            return signal
        
        # 3. Check integrity errors
        if has_integrity_errors and self.config.DISABLE_ON_INTEGRITY_ERROR:
            signal.block_reason = "Integrity errors detected"
            return signal
        
        # 4. Check macro events
        if has_macro_event and self.config.DISABLE_ON_NEWS:
            signal.block_reason = "Macro event detected"
            return signal
        
        # 5. Check breakout cooldown
        if self._breakout_cooldown_until and market_time < self._breakout_cooldown_until:
            remaining = (self._breakout_cooldown_until - market_time).total_seconds() / 60
            signal.block_reason = f"Breakout cooldown ({remaining:.1f}m remaining)"
            return signal
        
        # =====================================================================
        # DATA PREPARATION
        # =====================================================================
        
        if len(df_1h) < 50 or len(df_5m) < 20:
            signal.block_reason = "Insufficient data"
            return signal
        
        # 1H indicators
        closes_1h = df_1h['close'].values
        highs_1h = df_1h['high'].values
        lows_1h = df_1h['low'].values
        ema50_1h = calculate_ema(closes_1h, 50)
        atr_1h = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
        
        # 5M indicators
        closes_5m = df_5m['close'].values
        highs_5m = df_5m['high'].values
        lows_5m = df_5m['low'].values
        opens_5m = df_5m['open'].values
        volumes_5m = df_5m['volume'].values if 'volume' in df_5m.columns else np.ones(len(df_5m))
        
        atr_5m = calculate_atr(highs_5m, lows_5m, closes_5m, 14)
        rsi_5m = calculate_rsi(closes_5m, 14)
        ema20_5m = calculate_ema(closes_5m, 20)
        
        current_atr_5m = atr_5m[-1] if len(atr_5m) > 0 and not np.isnan(atr_5m[-1]) else 100.0
        current_atr_1h = atr_1h[-1] if len(atr_1h) > 0 and not np.isnan(atr_1h[-1]) else 500.0
        current_rsi = rsi_5m[-1] if len(rsi_5m) > 0 and not np.isnan(rsi_5m[-1]) else 50.0
        
        # =====================================================================
        # GATE 1: EXTENSION DETECTION
        # =====================================================================
        
        extension_z = calculate_extension_z(current_price, ema50_1h, atr_1h)
        signal.extension_z = extension_z
        
        ext_z_threshold = self.config.get_extension_z_min(regime)
        gate1_passed = extension_z >= ext_z_threshold
        
        gate1_result = GateResult(
            passed=gate1_passed,
            gate=PullbackGate.EXTENSION,
            reason=f"ext_z={extension_z:.2f} vs min={ext_z_threshold:.2f}",
            data={
                'extension_z': extension_z,
                'threshold': ext_z_threshold,
                'rsi': current_rsi,
                'regime': regime
            }
        )
        signal.gate_results.append(gate1_result)
        
        if self.config.LOG_ALL_GATES:
            status = "✓" if gate1_passed else "✗"
            logger.info(f"[PULLBACK] Gate1 EXTENSION: {status} | ext_z={extension_z:.2f} (min={ext_z_threshold:.2f}) | RSI={current_rsi:.1f}")
        
        if not gate1_passed:
            signal.block_reason = f"Extension too low: {extension_z:.2f} < {ext_z_threshold:.2f}"
            return signal
        
        # Check complementary filters (2 of 3)
        complementary_passed = 0
        
        # RSI check (removed RSI_MIN requirement for simplicity - spec doesn't require it)
        # The extension_z is the main filter now
        complementary_passed = 2  # Auto-pass for now, extension_z is sufficient
        
        # =====================================================================
        # GATE 2: SWING HIGH + CLOSE BELOW CONFIRM_LEVEL (TRIGGER)
        # =====================================================================
        
        # Step 1: Detect swing high (structural)
        # high[i] > high[i-N:i] AND high[i] > high[i+1:i+N+1]
        swing_n = self.config.SWING_N
        swing_validity = self.config.SWING_VALIDITY_5M
        
        # Find most recent valid swing high
        swing_detected = False
        swing_high = 0.0
        swing_high_index = 0
        confirm_level = 0.0
        candles_since_swing = 0
        
        # Search backwards for swing high (need at least swing_n candles after for confirmation)
        for i in range(len(highs_5m) - swing_n - 1, swing_n - 1, -1):
            is_swing = True
            candidate_high = highs_5m[i]
            
            # Check left (highs before)
            for j in range(1, swing_n + 1):
                if highs_5m[i - j] >= candidate_high:
                    is_swing = False
                    break
            
            if not is_swing:
                continue
            
            # Check right (highs after)
            for j in range(1, swing_n + 1):
                if i + j < len(highs_5m) and highs_5m[i + j] >= candidate_high:
                    is_swing = False
                    break
            
            if is_swing:
                candles_since_swing = len(highs_5m) - 1 - i
                
                # Check validity (not too old)
                if candles_since_swing <= swing_validity:
                    swing_detected = True
                    swing_high = candidate_high
                    swing_high_index = i
                    confirm_level = lows_5m[i]  # Low of swing high candle
                    break
        
        # Update internal state
        if swing_detected:
            self._last_swing_high = swing_high
            self._last_swing_high_time = market_time
            self._last_swing_high_index = swing_high_index
            self._last_swing_high_candle_low = confirm_level
            self._swing_confirmed = True
        
        signal.swing_high = swing_high
        signal.swing_high_index = swing_high_index
        signal.confirm_level = confirm_level
        
        # Step 2: Check trigger - close below confirm_level
        trigger_fired = False
        trigger_type = ""
        
        if swing_detected and confirm_level > 0:
            current_close = closes_5m[-1]
            
            if current_close < confirm_level:
                trigger_fired = True
                trigger_type = "CLOSE_BELOW_SWING_LOW"
                self._trigger_fired = True
        
        signal.trigger_type = trigger_type
        
        gate2_result = GateResult(
            passed=trigger_fired,
            gate=PullbackGate.TRIGGER,
            reason=f"trigger={trigger_type or 'NONE'} | swing_high={swing_high:.2f} | confirm={confirm_level:.2f} | close={closes_5m[-1]:.2f}",
            data={
                'swing_detected': swing_detected,
                'swing_high': swing_high,
                'swing_high_index': swing_high_index,
                'confirm_level': confirm_level,
                'trigger_type': trigger_type,
                'candles_since_swing': candles_since_swing
            }
        )
        signal.gate_results.append(gate2_result)
        
        if self.config.LOG_ALL_GATES:
            status = "✓" if trigger_fired else "✗"
            swing_status = f"swing@{swing_high:.0f}" if swing_detected else "no_swing"
            validity_status = f"{candles_since_swing}/{swing_validity}candles" if swing_detected else ""
            logger.info(f"[PULLBACK] Gate2 TRIGGER: {status} | {swing_status} | confirm={confirm_level:.0f} | {validity_status}")
        
        if not trigger_fired:
            if not swing_detected:
                signal.block_reason = f"No valid swing high (validity={swing_validity} candles)"
            else:
                signal.block_reason = f"No trigger: close {closes_5m[-1]:.2f} >= confirm {confirm_level:.2f}"
            return signal
        
        # =====================================================================
        # GATE 3: BREAKOUT EXCLUSION (VETO)
        # =====================================================================
        
        is_breakout, breakout_details = detect_breakout_impulse(
            opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m,
            current_atr_5m,
            body_atr_ratio=self.config.BREAKOUT_BODY_ATR_RATIO,
            close_near_high_ratio=self.config.BREAKOUT_CLOSE_NEAR_HIGH,
            volume_expansion_ratio=self.config.BREAKOUT_VOLUME_EXPANSION
        )
        
        gate3_passed = not is_breakout
        
        gate3_result = GateResult(
            passed=gate3_passed,
            gate=PullbackGate.NO_BREAKOUT,
            reason=f"breakout_detected={is_breakout}",
            data=breakout_details
        )
        signal.gate_results.append(gate3_result)
        
        if self.config.LOG_ALL_GATES:
            status = "✓" if gate3_passed else "✗"
            logger.info(f"[PULLBACK] Gate3 NO_BREAKOUT: {status} | breakout={is_breakout}")
        
        if is_breakout:
            # Set cooldown
            cooldown_seconds = self.config.BREAKOUT_COOLDOWN_CANDLES * 300  # 5m candles
            self._breakout_cooldown_until = market_time + timedelta(seconds=cooldown_seconds)
            signal.block_reason = f"Breakout detected - cooldown {self.config.BREAKOUT_COOLDOWN_CANDLES} candles"
            return signal
        
        # =====================================================================
        # ALL GATES PASSED - CALCULATE SIGNAL PARAMETERS
        # =====================================================================
        
        # Calculate activity score for adaptive TP2
        # activity = clamp(volatility_ratio - 1.0, 0, 1)
        atr_avg = np.mean(atr_5m[-20:]) if len(atr_5m) >= 20 else current_atr_5m
        vol_ratio = current_atr_5m / atr_avg if atr_avg > 0 else 1.0
        activity_score = max(0.0, min(1.0, vol_ratio - 1.0))
        signal.activity_score = activity_score
        
        # -----------------------------------------------------------------
        # ENTRY MODE: RETEST (LIMIT) or MARKET
        # -----------------------------------------------------------------
        entry_mode = self.config.ENTRY_MODE
        signal.entry_mode = entry_mode
        
        # Calculate VWAP for both entry and TP
        try:
            vwap_series = calculate_vwap(df_5m)
            vwap_value = vwap_series.iloc[-1] if len(vwap_series) > 0 and not np.isnan(vwap_series.iloc[-1]) else None
        except Exception:
            vwap_value = None
        
        # Calculate EMA20 for retest level
        ema20_value = ema20_5m[-1] if len(ema20_5m) > 0 and not np.isnan(ema20_5m[-1]) else None
        
        if entry_mode == "RETEST":
            # Retest entry: LIMIT order at VWAP or EMA20
            if self.config.RETEST_LEVEL == "VWAP" and vwap_value:
                retest_level = vwap_value
            elif ema20_value:
                retest_level = ema20_value
            else:
                retest_level = current_price  # Fallback to market
            
            signal.retest_level = retest_level
            signal.entry_price = retest_level
        else:
            # Market entry
            signal.entry_price = current_price
            signal.retest_level = current_price
        
        entry_price = signal.entry_price
        
        # -----------------------------------------------------------------
        # STOP LOSS: Above swing high + buffer
        # buffer = max(MIN_TICKS_BUFFER, 0.15 * ATR_5m)
        # -----------------------------------------------------------------
        buffer = max(
            self.config.SL_MIN_TICKS_BUFFER,  # Minimum ticks
            self.config.SL_BUFFER_ATR_5M * current_atr_5m
        )
        stop_loss = swing_high + buffer
        signal.stop_loss = stop_loss
        
        # -----------------------------------------------------------------
        # TP1: FIXED at VWAP
        # Condiciones de seguridad:
        # - Si TP1 >= entry → no trade o TP1 alternativo
        # - Si (entry - TP1) < min_profit_floor → ajustar
        # -----------------------------------------------------------------
        min_profit_floor = max(
            self.config.TP1_MIN_PROFIT_FLOOR_ATR * current_atr_5m,
            50  # Minimum $50 profit target
        )
        
        if vwap_value and vwap_value < entry_price:
            tp1_price = vwap_value
        else:
            # VWAP above entry - use ATR fallback
            tp1_price = entry_price - (0.5 * current_atr_5m)
        
        # Ensure minimum profit floor
        if (entry_price - tp1_price) < min_profit_floor:
            tp1_price = entry_price - min_profit_floor
        
        signal.tp1_price = tp1_price
        
        # -----------------------------------------------------------------
        # TP2: DYNAMIC based on activity
        # k_tp2 = k_low + activity * (k_high - k_low)
        # TP2 = entry - k_tp2 * ATR_5m
        # -----------------------------------------------------------------
        if self.config.TP2_ENABLED:
            k_tp2 = self.config.get_tp2_k(activity_score)
            tp2_price = entry_price - (k_tp2 * current_atr_5m)
            signal.tp2_price = tp2_price
        else:
            signal.tp2_price = None
        
        # Add tags for analysis
        signal.tags = {
            'extension_z': extension_z,
            'rsi': current_rsi,
            'regime': regime,
            'activity_score': activity_score,
            'swing_high': swing_high,
            'swing_high_index': swing_high_index,
            'confirm_level': confirm_level,
            'candles_since_swing': candles_since_swing,
            'trigger_type': trigger_type,
            'entry_mode': entry_mode,
            'retest_level': signal.retest_level,
            'vwap': vwap_value,
            'ema20': ema20_value,
            'tp1_type': 'VWAP' if (vwap_value and vwap_value < entry_price) else 'ATR_FALLBACK',
            'tp2_k': self.config.get_tp2_k(activity_score) if self.config.TP2_ENABLED else None,
            'complementary_passed': complementary_passed
        }
        
        # Signal is valid!
        signal.valid = True
        
        logger.info(
            f"[PULLBACK] ✅ SIGNAL VALID | "
            f"Entry={entry_price:.2f} ({entry_mode}) | SL={stop_loss:.2f} | "
            f"TP1={tp1_price:.2f} (VWAP) | TP2={signal.tp2_price:.2f if signal.tp2_price else 'N/A'} | "
            f"ext_z={extension_z:.2f} | trigger={trigger_type}"
        )
        
        return signal
    
    def record_trade_result(self, pnl: float, is_winner: bool, market_time: datetime):
        """
        Record the result of a pullback short trade.
        
        Args:
            pnl: Trade PnL
            is_winner: Whether trade was profitable
            market_time: Trade close time
        """
        self._daily_trade_count += 1
        self._cumulative_pnl += pnl  # Track cumulative PnL
        
        if is_winner:
            self._consecutive_losses = 0
            logger.info(f"[PULLBACK] Trade WIN | PnL=${pnl:.2f} | Daily count={self._daily_trade_count}")
        else:
            self._consecutive_losses += 1
            logger.info(
                f"[PULLBACK] Trade LOSS | PnL=${pnl:.2f} | "
                f"Consecutive losses={self._consecutive_losses}/{self.config.MAX_CONSECUTIVE_LOSSES}"
            )
            
            if self._consecutive_losses >= self.config.MAX_CONSECUTIVE_LOSSES:
                logger.warning(f"[PULLBACK] Disabled due to consecutive losses")
    
    def disable_temporarily(self, duration_minutes: int, reason: str, market_time: datetime):
        """Temporarily disable pullback shorts"""
        self._disabled_until = market_time + timedelta(minutes=duration_minutes)
        logger.warning(f"[PULLBACK] Disabled for {duration_minutes}m: {reason}")
    
    def reset_daily(self, market_time: datetime):
        """Reset daily counters (call at day boundary)"""
        self._daily_trade_count = 0
        self._consecutive_losses = 0
        self._cumulative_pnl = 0.0
        self._last_trade_date = market_time.date()
        logger.info("[PULLBACK] Daily reset completed")
    
    def get_status(self, market_time: datetime) -> Dict[str, Any]:
        """Get current engine status"""
        is_enabled, reason = self.is_enabled(market_time=market_time)
        
        return {
            'enabled': is_enabled,
            'reason': reason,
            'daily_trades': self._daily_trade_count,
            'max_daily': self.config.MAX_TRADES_PER_DAY,
            'consecutive_losses': self._consecutive_losses,
            'max_consecutive_losses': self.config.MAX_CONSECUTIVE_LOSSES,
            'in_cooldown': self._breakout_cooldown_until is not None and market_time < self._breakout_cooldown_until,
            'last_swing_high': self._last_swing_high
        }
    
    def get_daily_stats(self) -> Dict[str, Any]:
        """Get daily statistics for metrics reporting"""
        return {
            'total_trades': self._daily_trade_count,
            'consecutive_losses': self._consecutive_losses,
            'cumulative_pnl': getattr(self, '_cumulative_pnl', 0.0)
        }


# ============================================================================
# SINGLETON PATTERN
# ============================================================================

_pullback_engine_instance: Optional[PullbackShortEngine] = None


def get_pullback_short_engine() -> PullbackShortEngine:
    """Get the singleton PullbackShortEngine instance"""
    global _pullback_engine_instance
    if _pullback_engine_instance is None:
        _pullback_engine_instance = PullbackShortEngine()
    return _pullback_engine_instance


def reset_pullback_short_engine() -> None:
    """Reset the singleton (for testing)"""
    global _pullback_engine_instance
    _pullback_engine_instance = None
