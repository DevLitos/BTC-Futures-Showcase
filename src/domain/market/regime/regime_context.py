"""
Regime Context - SINGLE SOURCE OF TRUTH for market regime

╔══════════════════════════════════════════════════════════════════════════════╗
║ ⛔ DEPRECATED - Feb 05, 2026                                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ REASON: Despite being designed as "SINGLE SOURCE OF TRUTH", this module     ║
║         was NEVER integrated with application/live/ code.                   ║
║                                                                              ║
║ ANALYSIS:                                                                    ║
║ - RegimeContextManager: Never imported in application layer                 ║
║ - get_regime_context_manager(): Never called from live trading code         ║
║ - RegimeContext: Good design, but superseded by TrendIntelligence           ║
║                                                                              ║
║ REPLACEMENT:                                                                 ║
║ - Use TrendIntelligence.get_state() for regime context                      ║
║ - Use EdgeRegimeClassifier for edge-specific regime                         ║
║ - Trailing logic now embedded in trailing_stop.py directly                  ║
║                                                                              ║
║ MIGRATION: File kept for backward compatibility. Will be removed in v2.2.0  ║
╚══════════════════════════════════════════════════════════════════════════════╝

OLD DESCRIPTION:
----------------
🔥 FIX (Jan 22, 2026):
"Tu bot ya sabe cuándo una entrada es mediocre,
pero todavía no alinea todos los módulos para tratarla como tal de forma consistente."

PROBLEM IDENTIFIED:
- HTF filter interprets: TREND
- Regime detector interprets: RANGE
- Trailing uses: TREND

This creates "personalidad dividida" - trade enters well but managed inconsistently.

SOLUTION:
One canonical RegimeContext object that ALL modules read from:
- SL multiplier
- Trailing logic
- Micro bonuses
- Chase penalties
- HTF permissiveness
- RR requirements
- Position sizing

The regime is determined ONCE per evaluation cycle and stored here.
All modules query this context instead of computing their own.

Created: January 22, 2026
"""

import warnings
warnings.warn(
    "regime_context.py is DEPRECATED. Use TrendIntelligence.get_state() instead.",
    DeprecationWarning,
    stacklevel=2
)

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class RegimeMode(Enum):
    """Canonical regime modes - ONE SOURCE OF TRUTH"""
    TREND = "TREND"                  # ADX >= 18, allow momentum following
    RANGE = "RANGE"                  # ADX 11-18, mean reversion only
    BREAKOUT = "BREAKOUT"            # Just broke key level, volatile
    TREND_EXHAUSTION = "TREND_EXHAUSTION"  # ADX high but falling
    NO_TRADE = "NO_TRADE"            # Chaos or extreme conditions


@dataclass
class RegimeContext:
    """
    Unified regime context - ALL modules read from this.
    
    This is the SINGLE SOURCE OF TRUTH.
    Updated once per evaluation cycle by SignalEngine.
    """
    
    # === CORE REGIME ===
    mode: RegimeMode = RegimeMode.RANGE  # Default conservative
    mode_value: str = "RANGE"            # String version for compatibility
    confidence: float = 0.5              # 0-1 confidence in classification
    
    # === KEY METRICS ===
    adx: float = 15.0                    # Current ADX value
    adx_slope: float = 0.0               # ADX trend (positive = strengthening)
    atr_ratio: float = 1.0               # Current ATR / average ATR
    
    # === REGIME-SPECIFIC PARAMETERS ===
    # Position sizing
    size_multiplier: float = 1.0         # Position size adjustment
    
    # Stop loss
    sl_atr_multiplier: float = 3.0       # SL = ATR * this value
    sl_tight_mode: bool = False          # Use tighter stops (RANGE)
    
    # Take profit
    tp_multiplier: float = 1.0           # TP target adjustment
    tp_partial_aggressive: bool = False  # Take partials faster (RANGE)
    
    # Trailing
    trailing_regime: str = "RANGE"       # TREND or RANGE - controls trailing logic
    trailing_aggressive: bool = False    # Lock profits faster
    trailing_atr_trigger: float = 1.0    # When to start trailing (ATR units)
    trailing_step_atr: float = 0.3       # Trailing step size (ATR units)
    
    # Entry restrictions
    chasing_allowed: bool = True         # Allow chasing price
    chasing_penalty: float = 1.0         # Chase penalty multiplier (1.0 = full)
    chase_penalty_halved_by_momentum: bool = True  # TREND: yes, RANGE: no
    
    # Micro trend adjustments
    micro_trend_weight: float = 1.0      # Weight of micro trend bonus (RANGE: reduced)
    micro_trend_max_penalty: float = 0.0 # Cap on micro trend penalty
    micro_trend_max_bonus: float = 1.0   # Cap on micro trend bonus
    
    # RR requirements
    min_rr_net: float = 1.3              # Minimum R:R after fees
    min_rr_probe: float = 1.0            # Minimum for probe entries
    rr_block_floor: float = 0.8          # Block if RR below this
    
    # Session limits
    max_trades_per_session: int = 4      # Max trades in current session
    
    # Entry types allowed
    allow_trend_entries: bool = True     # Trend-following allowed
    allow_ct_entries: bool = False       # Counter-trend allowed
    allow_range_entries: bool = False    # Mean reversion allowed
    
    # Timestamps
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))
    regime_start: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    # Diagnostic
    classification_reason: str = "Initial"
    
    def __post_init__(self):
        """Ensure mode_value stays in sync"""
        self.mode_value = self.mode.value
    
    def is_trend_regime(self) -> bool:
        """Is this a trending market?"""
        return self.mode in (RegimeMode.TREND, RegimeMode.BREAKOUT)
    
    def is_range_regime(self) -> bool:
        """Is this a ranging market?"""
        return self.mode in (RegimeMode.RANGE, RegimeMode.TREND_EXHAUSTION)
    
    def is_tradeable(self) -> bool:
        """Can we trade in this regime?"""
        return self.mode != RegimeMode.NO_TRADE


# === REGIME CONFIGURATION TABLES ===

def _build_trend_context(adx: float, confidence: float, adx_slope: float) -> RegimeContext:
    """Build context for TREND regime"""
    return RegimeContext(
        mode=RegimeMode.TREND,
        mode_value="TREND",
        confidence=confidence,
        adx=adx,
        adx_slope=adx_slope,
        
        # Sizing
        size_multiplier=1.0,
        
        # SL/TP
        sl_atr_multiplier=3.5,
        sl_tight_mode=False,
        tp_multiplier=1.0,
        tp_partial_aggressive=False,
        
        # Trailing - TREND mode
        trailing_regime="TREND",
        trailing_aggressive=False,
        trailing_atr_trigger=1.5,     # Start trailing after 1.5 ATR profit
        trailing_step_atr=0.5,        # Wide steps in trend
        
        # Entry
        chasing_allowed=True,
        chasing_penalty=0.3,          # Light penalty - momentum is expected
        chase_penalty_halved_by_momentum=True,  # Yes in trend
        
        # Micro trend
        micro_trend_weight=1.0,       # Full weight in trend
        micro_trend_max_penalty=0.0,  # No cap
        micro_trend_max_bonus=1.5,    # Can get full bonus
        
        # RR
        min_rr_net=1.3,
        min_rr_probe=1.0,
        rr_block_floor=0.8,
        
        # Session
        max_trades_per_session=4,
        
        # Entry types
        allow_trend_entries=True,
        allow_ct_entries=False,
        allow_range_entries=False,
        
        classification_reason=f"ADX={adx:.1f} >= 18, slope={adx_slope:.2f}"
    )


def _build_range_context(adx: float, confidence: float, adx_slope: float) -> RegimeContext:
    """Build context for RANGE regime"""
    return RegimeContext(
        mode=RegimeMode.RANGE,
        mode_value="RANGE",
        confidence=confidence,
        adx=adx,
        adx_slope=adx_slope,
        
        # Sizing
        size_multiplier=0.7,          # Smaller size in range
        
        # SL/TP
        # 🔥 FIX (Jan 22, 2026): RANGE needs TIGHT stops, NOT 3.0x
        # RANGE = scalps, quick in/out. 3.0x converts scalp into "fade con stop enorme"
        # FULL entry: max 2.0x ATR | PROBE: max 1.6x ATR (applied in signal_engine)
        sl_atr_multiplier=2.0,        # 🔥 REDUCED from 3.0 to 2.0 for RANGE scalps
        sl_tight_mode=True,
        tp_multiplier=0.7,            # Shorter targets
        tp_partial_aggressive=True,   # Take partials faster
        
        # Trailing - RANGE mode
        trailing_regime="RANGE",      # 🔥 FIX: Must be RANGE not TREND
        trailing_aggressive=True,     # Lock profits fast
        trailing_atr_trigger=0.8,     # Start trailing earlier
        trailing_step_atr=0.2,        # Tighter steps in range
        
        # Entry
        chasing_allowed=True,
        chasing_penalty=0.5,
        chase_penalty_halved_by_momentum=False,  # 🔥 NO in range! Momentum often fake
        
        # Micro trend
        micro_trend_weight=0.5,       # 🔥 Reduced weight - micro trends ephemeral in range
        micro_trend_max_penalty=-0.3, # Cap penalty at -0.3
        micro_trend_max_bonus=0.5,    # Cap bonus at 0.5 (instead of 1.0)
        
        # RR
        min_rr_net=1.5,               # Higher requirement in range
        min_rr_probe=1.2,
        rr_block_floor=0.9,           # Stricter floor
        
        # Session
        max_trades_per_session=3,     # Fewer trades in range
        
        # Entry types
        allow_trend_entries=False,    # No trend-following in range
        allow_ct_entries=True,        # Allow counter-trend scalps
        allow_range_entries=True,     # Mean reversion allowed
        
        classification_reason=f"ADX={adx:.1f} < 18, slope={adx_slope:.2f}"
    )


def _build_breakout_context(adx: float, confidence: float, adx_slope: float) -> RegimeContext:
    """Build context for BREAKOUT regime (just broke level)"""
    return RegimeContext(
        mode=RegimeMode.BREAKOUT,
        mode_value="BREAKOUT",
        confidence=confidence,
        adx=adx,
        adx_slope=adx_slope,
        
        # Sizing - careful during breakout
        size_multiplier=0.8,
        
        # SL/TP - breakout specific
        sl_atr_multiplier=2.5,        # Tighter SL (breakout = conviction)
        sl_tight_mode=True,
        tp_multiplier=1.2,            # Larger target potential
        tp_partial_aggressive=False,
        
        # Trailing - momentum mode
        trailing_regime="TREND",      # Breakout follows trend trailing
        trailing_aggressive=False,
        trailing_atr_trigger=1.0,
        trailing_step_atr=0.4,
        
        # Entry
        chasing_allowed=True,         # Can chase breakout
        chasing_penalty=0.2,          # Light penalty
        chase_penalty_halved_by_momentum=True,
        
        # Micro trend - full weight during breakout
        micro_trend_weight=1.0,
        micro_trend_max_penalty=0.0,
        micro_trend_max_bonus=1.5,
        
        # RR
        min_rr_net=1.2,
        min_rr_probe=0.9,
        rr_block_floor=0.7,
        
        # Session
        max_trades_per_session=5,     # More trades allowed during breakout
        
        # Entry types
        allow_trend_entries=True,
        allow_ct_entries=False,
        allow_range_entries=False,
        
        classification_reason=f"Breakout detected, ADX={adx:.1f}"
    )


def _build_exhaustion_context(adx: float, confidence: float, adx_slope: float) -> RegimeContext:
    """Build context for TREND_EXHAUSTION (trend ending)"""
    return RegimeContext(
        mode=RegimeMode.TREND_EXHAUSTION,
        mode_value="TREND_EXHAUSTION",
        confidence=confidence,
        adx=adx,
        adx_slope=adx_slope,
        
        # Sizing - reduced for exhaustion
        size_multiplier=0.6,
        
        # SL/TP
        sl_atr_multiplier=2.5,        # Tight SL
        sl_tight_mode=True,
        tp_multiplier=0.6,            # Short targets
        tp_partial_aggressive=True,
        
        # Trailing - aggressive protection
        trailing_regime="RANGE",       # 🔥 Use RANGE trailing despite high ADX
        trailing_aggressive=True,
        trailing_atr_trigger=0.6,
        trailing_step_atr=0.2,
        
        # Entry - very conservative
        chasing_allowed=False,        # No chasing in exhaustion
        chasing_penalty=2.0,          # Heavy penalty
        chase_penalty_halved_by_momentum=False,
        
        # Micro trend - reduced
        micro_trend_weight=0.3,
        micro_trend_max_penalty=-0.5,
        micro_trend_max_bonus=0.3,
        
        # RR - strict
        min_rr_net=1.8,
        min_rr_probe=1.5,
        rr_block_floor=1.0,
        
        # Session
        max_trades_per_session=2,
        
        # Entry types
        allow_trend_entries=False,    # No trend following
        allow_ct_entries=True,        # CT may work as trend reverses
        allow_range_entries=True,
        
        classification_reason=f"Trend exhaustion: ADX={adx:.1f} high but falling, slope={adx_slope:.2f}"
    )


def _build_no_trade_context() -> RegimeContext:
    """Build context for NO_TRADE regime"""
    return RegimeContext(
        mode=RegimeMode.NO_TRADE,
        mode_value="NO_TRADE",
        confidence=1.0,
        
        size_multiplier=0.0,
        sl_atr_multiplier=3.0,
        sl_tight_mode=True,
        tp_multiplier=0.0,
        tp_partial_aggressive=True,
        
        trailing_regime="RANGE",
        trailing_aggressive=True,
        trailing_atr_trigger=0.5,
        trailing_step_atr=0.1,
        
        chasing_allowed=False,
        chasing_penalty=999.0,
        chase_penalty_halved_by_momentum=False,
        
        micro_trend_weight=0.0,
        micro_trend_max_penalty=-99.0,
        micro_trend_max_bonus=0.0,
        
        min_rr_net=999.0,
        min_rr_probe=999.0,
        rr_block_floor=999.0,
        
        max_trades_per_session=0,
        
        allow_trend_entries=False,
        allow_ct_entries=False,
        allow_range_entries=False,
        
        classification_reason="No trade conditions"
    )


class RegimeContextManager:
    """
    Manages the canonical RegimeContext.
    
    This is the SINGLE SOURCE OF TRUTH for market regime.
    All modules should query this instead of computing their own regime.
    """
    
    # Thresholds
    ADX_TREND_THRESHOLD = 18.0       # ADX >= 18 → TREND
    ADX_RANGE_THRESHOLD = 11.0       # ADX 11-18 → RANGE
    ADX_SLOPE_EXHAUSTION = -0.5      # Slope < -0.5 when ADX high → EXHAUSTION
    
    def __init__(self):
        self._current_context: RegimeContext = _build_range_context(15.0, 0.5, 0.0)
        self._context_history: list = []
        self._max_history = 20
        
    @property
    def context(self) -> RegimeContext:
        """Get current regime context - READ-ONLY for other modules"""
        return self._current_context
    
    @property
    def mode(self) -> RegimeMode:
        """Get current regime mode"""
        return self._current_context.mode
    
    @property
    def mode_value(self) -> str:
        """Get current regime mode as string"""
        return self._current_context.mode_value
    
    @property
    def trailing_regime(self) -> str:
        """Get trailing regime string - for trailing stop module"""
        return self._current_context.trailing_regime
    
    def classify_and_update(
        self,
        adx: float,
        adx_slope: float = 0.0,
        atr_ratio: float = 1.0,
        has_breakout: bool = False,
        spread_bps: float = 0.0,
        is_chaos: bool = False
    ) -> RegimeContext:
        """
        Classify market regime and update the canonical context.
        
        This is called ONCE per evaluation cycle by SignalEngine.
        All other modules read from the resulting context.
        
        Args:
            adx: Current ADX value
            adx_slope: ADX change over recent bars
            atr_ratio: Current ATR / avg ATR
            has_breakout: Whether a breakout was just detected
            spread_bps: Current spread in basis points
            is_chaos: Whether chaotic conditions detected
            
        Returns:
            Updated RegimeContext
        """
        old_mode = self._current_context.mode
        
        # Chaos takes priority
        if is_chaos or spread_bps > 20.0:
            new_context = _build_no_trade_context()
            new_context.classification_reason = f"Chaos detected: spread={spread_bps:.1f}bps"
        
        # Breakout mode if just broke level and ADX not too low
        elif has_breakout and adx >= 12.0:
            confidence = min(1.0, adx / 30.0)
            new_context = _build_breakout_context(adx, confidence, adx_slope)
        
        # TREND_EXHAUSTION: ADX high but falling
        elif adx >= self.ADX_TREND_THRESHOLD and adx_slope < self.ADX_SLOPE_EXHAUSTION:
            confidence = min(1.0, adx / 25.0)
            new_context = _build_exhaustion_context(adx, confidence, adx_slope)
        
        # TREND: ADX high and stable/rising
        elif adx >= self.ADX_TREND_THRESHOLD:
            confidence = min(1.0, adx / 30.0)
            new_context = _build_trend_context(adx, confidence, adx_slope)
        
        # RANGE: ADX below threshold
        elif adx >= self.ADX_RANGE_THRESHOLD:
            confidence = 0.5 + (self.ADX_TREND_THRESHOLD - adx) / 20.0
            new_context = _build_range_context(adx, confidence, adx_slope)
        
        # Very low ADX - still RANGE but might be NO_TRADE if liquidity bad
        else:
            if spread_bps > 10.0:
                new_context = _build_no_trade_context()
                new_context.classification_reason = f"Low ADX={adx:.1f} + high spread={spread_bps:.1f}bps"
            else:
                new_context = _build_range_context(adx, 0.3, adx_slope)
        
        # Update ATR ratio
        new_context.atr_ratio = atr_ratio
        
        # Log regime change
        if new_context.mode != old_mode:
            logger.info(
                f"🔄 REGIME CONTEXT CHANGE: {old_mode.value} → {new_context.mode.value} | "
                f"ADX={adx:.1f}, slope={adx_slope:.2f} | "
                f"trailing={new_context.trailing_regime}"
            )
            new_context.regime_start = datetime.now(UTC)
        else:
            # Keep the regime start time from before
            new_context.regime_start = self._current_context.regime_start
        
        new_context.last_update = datetime.now(UTC)
        
        # Store history
        self._context_history.append(self._current_context)
        if len(self._context_history) > self._max_history:
            self._context_history.pop(0)
        
        self._current_context = new_context
        return new_context
    
    def get_trailing_params(self) -> Dict[str, Any]:
        """Get trailing stop parameters from current context"""
        ctx = self._current_context
        return {
            'regime': ctx.trailing_regime,
            'aggressive': ctx.trailing_aggressive,
            'trigger_atr': ctx.trailing_atr_trigger,
            'step_atr': ctx.trailing_step_atr,
        }
    
    def get_entry_params(self) -> Dict[str, Any]:
        """Get entry parameters from current context"""
        ctx = self._current_context
        return {
            'chasing_allowed': ctx.chasing_allowed,
            'chasing_penalty': ctx.chasing_penalty,
            'chase_halved_by_momentum': ctx.chase_penalty_halved_by_momentum,
            'micro_weight': ctx.micro_trend_weight,
            'micro_max_bonus': ctx.micro_trend_max_bonus,
            'micro_max_penalty': ctx.micro_trend_max_penalty,
        }
    
    def get_rr_params(self) -> Dict[str, Any]:
        """Get R:R parameters from current context"""
        ctx = self._current_context
        return {
            'min_rr_net': ctx.min_rr_net,
            'min_rr_probe': ctx.min_rr_probe,
            'block_floor': ctx.rr_block_floor,
        }
    
    def get_sizing_params(self) -> Dict[str, Any]:
        """Get position sizing parameters from current context"""
        ctx = self._current_context
        return {
            'size_multiplier': ctx.size_multiplier,
            'max_trades_session': ctx.max_trades_per_session,
        }


# === SINGLETON INSTANCE ===

_regime_context_manager: Optional[RegimeContextManager] = None


def get_regime_context_manager() -> RegimeContextManager:
    """Get singleton instance of RegimeContextManager"""
    global _regime_context_manager
    if _regime_context_manager is None:
        _regime_context_manager = RegimeContextManager()
    return _regime_context_manager


def get_current_regime_context() -> RegimeContext:
    """Convenience function to get current regime context"""
    return get_regime_context_manager().context


def get_trailing_regime() -> str:
    """Convenience function for trailing stop module"""
    return get_regime_context_manager().trailing_regime
