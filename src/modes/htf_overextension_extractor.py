"""
HTF Overextension Extractor - Fade overextended moves with momentum decay

WHAT THIS MODULE DOES:
- Detects when HTF (1h/4h) is overextended from EMA50 with momentum decay
- Looks for failed continuation patterns on LTF (1m/5m)
- Takes counter-trend fades with tight invalidation

WHAT THIS MODULE DOES NOT:
- Modify HTF bias
- Touch trend engine
- Execute if position is open
- Use trailing/BE/scale-in

PHILOSOPHY:
- This is a SURGICAL fade, not a reversal system
- Target 1R only (no greed)
- Strict invalidation (stop above failed high/low)
- One and done per context (disable after loss)

ACTIVATION FLOW:
1. HTF: Overextended (price > 2.2 ATR from EMA50) + momentum decay
2. LTF: Failed continuation (swept high + closed back inside + volume spike)
3. Absorption confirmed (high delta, low price progress)
4. Tight invalidation (not too wide)
5. Execute with 1R target, no management

Created: January 15, 2026
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, Any, List
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

class OverextensionConfig:
    """Configuration for HTF Overextension mode"""
    
    # HTF Overextension thresholds
    OVEREXTENSION_ATR_MULT = 2.2      # Price must be > 2.2 ATR from EMA50
    OVEREXTENSION_MAX_ATR_MULT = 4.0  # Don't fade if TOO extended (parabolic)
    
    # Momentum decay thresholds
    RSI_SLOPE_DECAY = -0.15           # RSI slope threshold for decay
    RANGE_EXPANSION_WEAK_CLOSE = 0.3  # Close in bottom 30% of range = weak
    
    # LTF Failed continuation
    SWEEP_BUFFER_TICKS = 3            # How many ticks past swing = sweep
    CLOSE_BACK_INSIDE_PCT = 0.002     # 0.2% back inside range
    VOLUME_SPIKE_MULT = 1.5           # Volume > 1.5x average = spike
    
    # Absorption detection
    DELTA_HIGH_THRESHOLD = 0.4        # Delta ratio > 0.4 = high delta
    MIN_TICK_PROGRESS = 15            # Max ticks of progress (less = absorption)
    
    # Risk/Invalidation
    BUFFER_TICKS = 7                  # Ticks beyond failed high for SL
    MAX_FADE_RISK_TICKS = 50          # Max risk in ticks (if more, skip)
    TARGET_R_MULTIPLE = 1.0           # Fixed 1R target
    
    # Cooldown
    COOLDOWN_AFTER_LOSS_MINUTES = 60  # 1 hour cooldown after loss


# =============================================================================
# CONTEXT DATACLASSES
# =============================================================================

@dataclass
class HTFContext:
    """
    Higher Timeframe context for overextension detection.
    
    Populated from 1h or 4h data.
    """
    # Core data
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    bias: str = "NEUTRAL"  # "LONG", "SHORT", "NEUTRAL"
    
    # Price levels
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    
    # EMAs
    ema50: float = 0.0
    ema20: float = 0.0
    ema200: float = 0.0
    
    # Volatility
    atr: float = 0.0
    
    # Momentum indicators
    rsi: float = 50.0
    rsi_prev: float = 50.0
    rsi_slope: float = 0.0  # Calculated: (rsi - rsi_prev) / periods
    
    # Delta divergence (from external or calculated)
    delta_divergence: str = "NONE"  # "BULLISH", "BEARISH", "NONE"
    
    # Range characteristics
    range_expanding: bool = False
    close_weak: bool = False  # Close in bottom/top 30% against trend
    
    # Context signature (for locking after loss)
    signature: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp.isoformat(),
            'bias': self.bias,
            'close': round(self.close, 2),
            'ema50': round(self.ema50, 2),
            'atr': round(self.atr, 2),
            'rsi': round(self.rsi, 2),
            'rsi_slope': round(self.rsi_slope, 4),
            'delta_divergence': self.delta_divergence,
            'range_expanding': self.range_expanding,
            'close_weak': self.close_weak,
            'signature': self.signature,
        }


@dataclass
class LTFContext:
    """
    Lower Timeframe context for setup detection.
    
    Populated from 1m or 5m data.
    """
    # Core data
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Current candle
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    volume: float = 0.0
    
    # Swing references (recent highs/lows)
    recent_swing_high: float = 0.0
    recent_swing_low: float = 0.0
    range_high: float = 0.0  # Consolidation high
    range_low: float = 0.0   # Consolidation low
    
    # Sweep detection
    swept_high: bool = False
    swept_low: bool = False
    close_back_inside_range: bool = False
    
    # Volume
    volume_spike: bool = False
    avg_volume: float = 0.0
    
    # Delta (directional volume)
    delta: float = 0.0       # Buy - Sell volume (approximated)
    delta_ratio: float = 0.0 # |delta| / total volume
    
    # Price progress (how much price moved despite volume)
    price_progress_ticks: int = 0
    
    # Entry price (for trade intent)
    entry_price: float = 0.0
    
    # ATR for risk calculation
    atr: float = 0.0
    tick_size: float = 0.1  # Minimum price increment
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp.isoformat(),
            'close': round(self.close, 2),
            'swept_high': self.swept_high,
            'swept_low': self.swept_low,
            'close_back_inside_range': self.close_back_inside_range,
            'volume_spike': self.volume_spike,
            'delta_ratio': round(self.delta_ratio, 3),
            'price_progress_ticks': self.price_progress_ticks,
            'entry_price': round(self.entry_price, 2),
        }


@dataclass
class StopLevel:
    """Stop loss level with validity flag"""
    price: float = 0.0
    is_valid: bool = True
    risk_ticks: int = 0
    reason: str = ""
    
    @classmethod
    def invalid(cls, reason: str = "Invalid stop") -> 'StopLevel':
        return cls(price=0.0, is_valid=False, reason=reason)
    
    def too_wide(self, max_ticks: int) -> bool:
        return self.risk_ticks > max_ticks


@dataclass
class OverextensionTradeIntent:
    """
    Trade intent specific to HTF Overextension mode.
    
    Simpler than general TradeIntent - no scale-in, no trailing.
    """
    # Direction
    side: str = ""  # "LONG" or "SHORT" (fade direction)
    
    # Prices
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    target_r: float = 1.0
    
    # Risk
    risk_ticks: int = 0
    reward_ticks: int = 0
    
    # Context
    mode: str = "HTF_OVEREXTENSION"
    reason: str = ""
    htf_signature: str = ""
    
    # Timing
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'side': self.side,
            'entry': round(self.entry, 2),
            'stop': round(self.stop, 2),
            'target': round(self.target, 2),
            'target_r': self.target_r,
            'risk_ticks': self.risk_ticks,
            'reward_ticks': self.reward_ticks,
            'mode': self.mode,
            'reason': self.reason,
            'created_at': self.created_at.isoformat(),
        }


@dataclass
class TradeResult:
    """Result of a closed trade"""
    entry_price: float = 0.0
    exit_price: float = 0.0
    side: str = ""
    r_multiple: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""
    htf_signature: str = ""


# =============================================================================
# MAIN CLASS
# =============================================================================

class HTFOverextensionExtractor:
    """
    HTF Overextension Fade Strategy.
    
    This is a STANDALONE mode that:
    1. Detects overextension on HTF (1h/4h)
    2. Waits for failed continuation on LTF (1m/5m)
    3. Executes tight fade with 1R target
    4. Disables after loss until new HTF context
    
    CRITICAL: This mode does NOT:
    - Modify the main trend bias
    - Touch the trend engine
    - Execute if position is open
    - Use trailing/BE/scale-in
    """
    
    def __init__(self, config: OverextensionConfig | None = None):
        self.config = config or OverextensionConfig()
        
        # Lock state (after loss, lock until new HTF context)
        self._locked: bool = False
        self._locked_htf_signature: str = ""
        self._cooldown_until: Optional[datetime] = None
        
        # Statistics
        self._stats = {
            'total_evaluations': 0,
            'context_passes': 0,
            'setup_passes': 0,
            'trades_taken': 0,
            'wins': 0,
            'losses': 0,
            'total_r': 0.0,
        }
        
        # Rejection tracking (for logging)
        self._last_rejection_reasons: List[str] = []
        
        logger.info("[OVEREXT] HTFOverextensionExtractor initialized")
    
    # =========================================================================
    # PUBLIC INTERFACE
    # =========================================================================
    
    def evaluate_context(self, htf_ctx: HTFContext) -> bool:
        """
        Gate 1: Check if HTF context is suitable for fade.
        
        Returns True if:
        - Bias is LONG or SHORT (not NEUTRAL)
        - Price is overextended from EMA50
        - Momentum is decaying
        - Not in cooldown
        - Not locked to previous failed context
        
        Returns:
            True if context is valid for looking at setups
        """
        self._stats['total_evaluations'] += 1
        self._last_rejection_reasons = []
        
        # 1. Bias check
        if htf_ctx.bias not in ("LONG", "SHORT"):
            self._last_rejection_reasons.append("neutral_bias")
            self._log_rejection("no valid HTF bias")
            return False
        
        # 2. Cooldown check
        if self._cooldown_active():
            self._last_rejection_reasons.append("cooldown_active")
            self._log_rejection("cooldown active")
            return False
        
        # 3. Lock check (after loss, wait for new context)
        if self._locked and htf_ctx.signature == self._locked_htf_signature:
            self._last_rejection_reasons.append("locked_to_failed_context")
            self._log_rejection("locked to failed HTF context")
            return False
        
        # 4. Overextension check
        if not self._is_overextended(htf_ctx):
            self._last_rejection_reasons.append("not_overextended")
            self._log_rejection("not overextended")
            return False
        
        # 5. Momentum decay check
        if not self._momentum_decay(htf_ctx):
            self._last_rejection_reasons.append("no_momentum_decay")
            self._log_rejection("no momentum decay")
            return False
        
        # All checks passed
        self._stats['context_passes'] += 1
        logger.info(f"[OVEREXT] ✅ Context PASS: bias={htf_ctx.bias}, "
                   f"ext={self._calc_extension(htf_ctx):.2f}x ATR, "
                   f"rsi_slope={htf_ctx.rsi_slope:.3f}")
        
        # Clear lock if new context
        if htf_ctx.signature != self._locked_htf_signature:
            self._locked = False
            self._locked_htf_signature = ""
        
        return True
    
    def evaluate_setup(self, ltf_ctx: LTFContext, htf_ctx: HTFContext) -> Optional[OverextensionTradeIntent]:
        """
        Gate 2: Look for failed continuation setup on LTF.
        
        Returns TradeIntent if:
        - Failed continuation detected (sweep + close back inside + volume)
        - Absorption confirmed (high delta, low progress)
        - Invalidation is not too wide
        
        Returns:
            OverextensionTradeIntent if setup valid, None otherwise
        """
        self._last_rejection_reasons = []
        
        # 1. Failed continuation check
        if not self._failed_continuation(ltf_ctx, htf_ctx):
            self._last_rejection_reasons.append("no_failed_continuation")
            self._log_rejection("no failed continuation")
            return None
        
        # 2. Absorption check
        if not self._absorption_confirmed(ltf_ctx):
            self._last_rejection_reasons.append("no_absorption")
            self._log_rejection("no absorption confirmed")
            return None
        
        # 3. Compute invalidation
        stop = self._compute_invalidation(ltf_ctx, htf_ctx)
        if not stop.is_valid:
            self._last_rejection_reasons.append(f"invalid_stop:{stop.reason}")
            self._log_rejection(f"invalid stop: {stop.reason}")
            return None
        
        if stop.too_wide(self.config.MAX_FADE_RISK_TICKS):
            self._last_rejection_reasons.append("stop_too_wide")
            self._log_rejection(f"stop too wide: {stop.risk_ticks} > {self.config.MAX_FADE_RISK_TICKS}")
            return None
        
        # 4. Build trade intent
        fade_side = self._fade_side(htf_ctx)
        entry = ltf_ctx.entry_price or ltf_ctx.close
        target = self._compute_target(entry, stop.price, fade_side)
        
        intent = OverextensionTradeIntent(
            side=fade_side,
            entry=entry,
            stop=stop.price,
            target=target,
            target_r=self.config.TARGET_R_MULTIPLE,
            risk_ticks=stop.risk_ticks,
            reward_ticks=abs(int((target - entry) / ltf_ctx.tick_size)),
            reason=f"HTF overext fade: swept {'high' if ltf_ctx.swept_high else 'low'} + absorption",
            htf_signature=htf_ctx.signature,
        )
        
        self._stats['setup_passes'] += 1
        logger.info(f"[OVEREXT] 🎯 Setup PASS: {intent.side} @ {intent.entry:.2f}, "
                   f"SL={intent.stop:.2f}, TP={intent.target:.2f}, "
                   f"risk={intent.risk_ticks} ticks")
        
        return intent
    
    def on_trade_opened(self, intent: OverextensionTradeIntent):
        """
        Called when trade is executed.
        
        In this mode: just place TP/SL and wait. No management.
        """
        self._stats['trades_taken'] += 1
        logger.info(f"[OVEREXT] 📈 Trade OPENED: {intent.side} @ {intent.entry:.2f}")
    
    def on_trade_closed(self, result: TradeResult):
        """
        Called when trade is closed.
        
        If loss: disable until new HTF context.
        """
        self._stats['total_r'] += result.r_multiple
        
        if result.r_multiple >= 0:
            self._stats['wins'] += 1
            logger.info(f"[OVEREXT] ✅ Trade WIN: +{result.r_multiple:.2f}R")
        else:
            self._stats['losses'] += 1
            logger.info(f"[OVEREXT] ❌ Trade LOSS: {result.r_multiple:.2f}R → LOCKING")
            self._disable_until_new_htf_context(result.htf_signature)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get mode statistics"""
        total_trades = self._stats['wins'] + self._stats['losses']
        win_rate = self._stats['wins'] / total_trades if total_trades > 0 else 0.0
        
        return {
            **self._stats,
            'win_rate': round(win_rate, 3),
            'avg_r': round(self._stats['total_r'] / total_trades, 2) if total_trades > 0 else 0.0,
            'is_locked': self._locked,
            'locked_signature': self._locked_htf_signature,
        }
    
    def get_last_rejection(self) -> str:
        """Get last rejection reasons for debugging"""
        return " | ".join(self._last_rejection_reasons) if self._last_rejection_reasons else "none"
    
    def reset(self):
        """Reset mode state (for testing)"""
        self._locked = False
        self._locked_htf_signature = ""
        self._cooldown_until = None
        self._last_rejection_reasons = []
    
    # =========================================================================
    # INTERNAL: OVEREXTENSION DETECTION
    # =========================================================================
    
    def _is_overextended(self, htf: HTFContext) -> bool:
        """
        Check if price is overextended from EMA50.
        
        Overextension = distance from EMA50 in ATR terms.
        NOT using BB width (that's regime, not direction).
        """
        if htf.atr <= 0 or htf.ema50 <= 0:
            return False
        
        extension = self._calc_extension(htf)
        
        # Must be between min and max thresholds
        # Too little = not overextended
        # Too much = parabolic, don't fade
        is_overext = (extension >= self.config.OVEREXTENSION_ATR_MULT and 
                      extension <= self.config.OVEREXTENSION_MAX_ATR_MULT)
        
        return is_overext
    
    def _calc_extension(self, htf: HTFContext) -> float:
        """Calculate extension as ATR multiple from EMA50"""
        if htf.atr <= 0:
            return 0.0
        return abs(htf.close - htf.ema50) / htf.atr
    
    def _momentum_decay(self, htf: HTFContext) -> bool:
        """
        Check for momentum decay (exhaustion).
        
        Any of these indicate decay:
        - RSI slope negative (falling momentum)
        - Delta divergence (price up but delta bearish)
        - Range expanding with weak close (distribution)
        """
        conditions = []
        
        # 1. RSI slope decay
        if htf.bias == "LONG":
            conditions.append(htf.rsi_slope < self.config.RSI_SLOPE_DECAY)
        else:  # SHORT
            conditions.append(htf.rsi_slope > -self.config.RSI_SLOPE_DECAY)
        
        # 2. Delta divergence
        if htf.bias == "LONG" and htf.delta_divergence == "BEARISH":
            conditions.append(True)
        elif htf.bias == "SHORT" and htf.delta_divergence == "BULLISH":
            conditions.append(True)
        
        # 3. Range expanding with weak close
        if htf.range_expanding and htf.close_weak:
            conditions.append(True)
        
        return any(conditions)
    
    # =========================================================================
    # INTERNAL: FAILED CONTINUATION DETECTION
    # =========================================================================
    
    def _failed_continuation(self, ltf: LTFContext, htf: HTFContext) -> bool:
        """
        Detect failed continuation pattern.
        
        For LONG bias (fade to SHORT):
        - Price swept above recent high (liquidity grab)
        - Closed back inside the range
        - Volume spike on the sweep
        
        This catches 90% of squeezes.
        """
        if htf.bias == "LONG":
            # Looking to fade SHORT (price swept high and failed)
            return (ltf.swept_high and 
                    ltf.close_back_inside_range and 
                    ltf.volume_spike)
        else:  # SHORT
            # Looking to fade LONG (price swept low and failed)
            return (ltf.swept_low and 
                    ltf.close_back_inside_range and 
                    ltf.volume_spike)
    
    def _absorption_confirmed(self, ltf: LTFContext) -> bool:
        """
        Check for absorption (high delta, low price progress).
        
        This means big players are absorbing supply/demand
        without letting price move much.
        """
        delta_high = ltf.delta_ratio >= self.config.DELTA_HIGH_THRESHOLD
        low_progress = ltf.price_progress_ticks <= self.config.MIN_TICK_PROGRESS
        
        return delta_high and low_progress
    
    # =========================================================================
    # INTERNAL: INVALIDATION AND TARGETS
    # =========================================================================
    
    def _compute_invalidation(self, ltf: LTFContext, htf: HTFContext) -> StopLevel:
        """
        Compute stop loss level.
        
        For SHORT fade (after swept high):
        - Stop = swept high + buffer ticks
        
        For LONG fade (after swept low):
        - Stop = swept low - buffer ticks
        """
        tick_size = ltf.tick_size if ltf.tick_size > 0 else 0.1
        buffer = self.config.BUFFER_TICKS * tick_size
        
        if htf.bias == "LONG":
            # Fading SHORT, stop above the failed high
            invalidation = ltf.recent_swing_high + buffer
            entry = ltf.entry_price or ltf.close
            risk_ticks = int(abs(invalidation - entry) / tick_size)
            
        else:  # SHORT
            # Fading LONG, stop below the failed low
            invalidation = ltf.recent_swing_low - buffer
            entry = ltf.entry_price or ltf.close
            risk_ticks = int(abs(entry - invalidation) / tick_size)
        
        # Validate
        if risk_ticks <= 0:
            return StopLevel.invalid("zero or negative risk")
        
        if risk_ticks > self.config.MAX_FADE_RISK_TICKS:
            return StopLevel.invalid(f"risk {risk_ticks} > max {self.config.MAX_FADE_RISK_TICKS}")
        
        return StopLevel(
            price=invalidation,
            is_valid=True,
            risk_ticks=risk_ticks,
            reason="valid"
        )
    
    def _fade_side(self, htf: HTFContext) -> str:
        """Get the fade direction (opposite of HTF bias)"""
        if htf.bias == "LONG":
            return "SHORT"  # Fade the overextended long
        else:
            return "LONG"   # Fade the overextended short
    
    def _compute_target(self, entry: float, stop: float, side: str) -> float:
        """
        Compute 1R target.
        
        Target = Entry ± Risk (1R)
        """
        risk = abs(entry - stop)
        target_distance = risk * self.config.TARGET_R_MULTIPLE
        
        if side == "LONG":
            return entry + target_distance
        else:
            return entry - target_distance
    
    # =========================================================================
    # INTERNAL: COOLDOWN AND LOCKING
    # =========================================================================
    
    def _cooldown_active(self) -> bool:
        """Check if in cooldown period"""
        if self._cooldown_until is None:
            return False
        return datetime.now(UTC) < self._cooldown_until
    
    def _disable_until_new_htf_context(self, htf_signature: str):
        """
        Lock the mode after a loss.
        
        Won't activate again until:
        - HTF signature changes (new context)
        - Or cooldown expires
        """
        self._locked = True
        self._locked_htf_signature = htf_signature
        self._cooldown_until = datetime.now(UTC) + timedelta(
            minutes=self.config.COOLDOWN_AFTER_LOSS_MINUTES
        )
        logger.warning(f"[OVEREXT] 🔒 Mode LOCKED until new HTF context or "
                      f"{self.config.COOLDOWN_AFTER_LOSS_MINUTES}min cooldown")
    
    # =========================================================================
    # LOGGING
    # =========================================================================
    
    def _log_rejection(self, reason: str):
        """Log why we're not trading"""
        logger.debug(f"[OVEREXT] blocked: {reason}")


# =============================================================================
# CONTEXT BUILDERS (helpers to populate contexts from DataFrames)
# =============================================================================

def build_htf_context(df_1h: pd.DataFrame, 
                      ema50_period: int = 50,
                      ema20_period: int = 20,
                      atr_period: int = 14,
                      rsi_period: int = 14) -> HTFContext:
    """
    Build HTFContext from 1h DataFrame.
    
    Args:
        df_1h: DataFrame with OHLCV columns
        
    Returns:
        Populated HTFContext
    """
    if df_1h is None or len(df_1h) < ema50_period + 10:
        return HTFContext()
    
    closes = df_1h['close'].to_numpy()
    highs = df_1h['high'].to_numpy()
    lows = df_1h['low'].to_numpy()
    
    # Current candle
    current_close = float(closes[-1])
    current_high = float(highs[-1])
    current_low = float(lows[-1])
    current_open = float(df_1h['open'].iloc[-1])
    
    # EMAs
    ema50 = _ema(closes, ema50_period)
    ema20 = _ema(closes, ema20_period)
    
    # ATR
    atr = _atr(highs, lows, closes, atr_period)
    
    # RSI and slope
    rsi_values = _rsi(closes, rsi_period)
    current_rsi = rsi_values[-1] if len(rsi_values) > 0 else 50.0
    prev_rsi = rsi_values[-2] if len(rsi_values) > 1 else current_rsi
    rsi_slope = (current_rsi - prev_rsi) / rsi_period
    
    # Bias (simple: above/below EMA50)
    if current_close > ema50:
        bias = "LONG"
    elif current_close < ema50:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
    
    # Range expansion (current range vs average)
    current_range = current_high - current_low
    avg_range = np.mean(highs[-20:] - lows[-20:])
    range_expanding = bool(current_range > avg_range * 1.3)
    
    # Weak close (bottom/top 30%)
    candle_range = current_high - current_low
    if candle_range > 0:
        close_position = (current_close - current_low) / candle_range
        close_weak = (bias == "LONG" and close_position < 0.3) or \
                     (bias == "SHORT" and close_position > 0.7)
    else:
        close_weak = False
    
    # Signature (for locking)
    signature = f"{bias}_{int(ema50)}_{int(current_rsi)}"
    
    return HTFContext(
        timestamp=datetime.now(UTC),
        bias=bias,
        close=current_close,
        high=current_high,
        low=current_low,
        open=current_open,
        ema50=ema50,
        ema20=ema20,
        atr=atr,
        rsi=current_rsi,
        rsi_prev=prev_rsi,
        rsi_slope=rsi_slope,
        range_expanding=range_expanding,
        close_weak=close_weak,
        signature=signature,
    )


def build_ltf_context(df_5m: pd.DataFrame,
                      swing_lookback: int = 20,
                      volume_avg_period: int = 20,
                      tick_size: float = 0.1) -> LTFContext:
    """
    Build LTFContext from 5m DataFrame.
    
    Args:
        df_5m: DataFrame with OHLCV columns
        swing_lookback: Bars to look back for swings
        volume_avg_period: Bars for volume average
        tick_size: Minimum price increment
        
    Returns:
        Populated LTFContext
    """
    if df_5m is None or len(df_5m) < swing_lookback:
        return LTFContext()
    
    closes = df_5m['close'].to_numpy()
    highs = df_5m['high'].to_numpy()
    lows = df_5m['low'].to_numpy()
    opens = df_5m['open'].to_numpy()
    volumes = df_5m['volume'].to_numpy() if 'volume' in df_5m.columns else np.ones(len(closes))
    
    # Current candle
    current_close = float(closes[-1])
    current_high = float(highs[-1])
    current_low = float(lows[-1])
    current_open = float(opens[-1])
    current_volume = float(volumes[-1])
    
    # Swings (recent high/low)
    recent_high = float(np.max(highs[-swing_lookback:-1]))
    recent_low = float(np.min(lows[-swing_lookback:-1]))
    
    # Range (consolidation bounds)
    range_high = float(np.max(highs[-swing_lookback:]))
    range_low = float(np.min(lows[-swing_lookback:]))
    
    # Sweep detection
    sweep_buffer = 3 * tick_size
    swept_high = current_high > recent_high + sweep_buffer
    swept_low = current_low < recent_low - sweep_buffer
    
    # Close back inside
    close_inside = (current_close < recent_high and current_close > recent_low)
    
    # Volume spike
    avg_volume = float(np.mean(volumes[-volume_avg_period:-1]))
    volume_spike = current_volume > avg_volume * 1.5
    
    # Delta (approximation using candle body)
    body = current_close - current_open
    candle_range = current_high - current_low
    if candle_range > 0:
        body_ratio = abs(body) / candle_range
        delta = current_volume * body_ratio * (1 if body > 0 else -1)
        delta_ratio = abs(delta) / current_volume if current_volume > 0 else 0
    else:
        delta = 0
        delta_ratio = 0
    
    # Price progress (how many ticks did price move?)
    price_progress = abs(current_close - current_open)
    price_progress_ticks = int(price_progress / tick_size)
    
    # ATR
    atr = _atr(highs, lows, closes, 14)
    
    return LTFContext(
        timestamp=datetime.now(UTC),
        close=current_close,
        high=current_high,
        low=current_low,
        open=current_open,
        volume=current_volume,
        recent_swing_high=recent_high,
        recent_swing_low=recent_low,
        range_high=range_high,
        range_low=range_low,
        swept_high=swept_high,
        swept_low=swept_low,
        close_back_inside_range=close_inside,
        volume_spike=volume_spike,
        avg_volume=avg_volume,
        delta=delta,
        delta_ratio=delta_ratio,
        price_progress_ticks=price_progress_ticks,
        entry_price=current_close,
        atr=atr,
        tick_size=tick_size,
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _ema(values: np.ndarray, period: int) -> float:
    """Calculate EMA"""
    if len(values) < period:
        return float(np.mean(values)) if len(values) > 0 else 0.0
    
    alpha = 2 / (period + 1)
    ema = float(np.mean(values[:period]))
    for val in values[period:]:
        ema = alpha * val + (1 - alpha) * ema
    return ema


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    """Calculate ATR"""
    if len(highs) < period + 1:
        return float(np.mean(highs - lows)) if len(highs) > 0 else 0.0
    
    tr_values = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_values.append(tr)
    
    if len(tr_values) < period:
        return float(np.mean(tr_values)) if tr_values else 0.0
    
    # RMA
    atr = float(np.mean(tr_values[:period]))
    alpha = 1 / period
    for tr in tr_values[period:]:
        atr = alpha * tr + (1 - alpha) * atr
    
    return atr


def _rsi(closes: np.ndarray, period: int) -> np.ndarray:
    """Calculate RSI array"""
    if len(closes) < period + 1:
        return np.full(len(closes), 50.0)
    
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    rsi_values = [50.0] * (period + 1)
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        rsi_values.append(rsi)
    
    return np.array(rsi_values)


# =============================================================================
# SINGLETON
# =============================================================================

_htf_overextension_extractor: Optional[HTFOverextensionExtractor] = None


def get_htf_overextension_extractor() -> HTFOverextensionExtractor:
    """Get or create the HTFOverextensionExtractor singleton"""
    global _htf_overextension_extractor
    if _htf_overextension_extractor is None:
        _htf_overextension_extractor = HTFOverextensionExtractor()
    return _htf_overextension_extractor


def reset_htf_overextension_extractor():
    """Reset the singleton (for testing)"""
    global _htf_overextension_extractor
    if _htf_overextension_extractor:
        _htf_overextension_extractor.reset()
    _htf_overextension_extractor = None
