"""
Unified Trading Constants - Single source for all magic numbers.

Created: Feb 04, 2026
Updated: Feb 05, 2026 - Added TrendDirection, RegimeConfig

Purpose: Eliminate 28+ duplicate/conflicting values found in audit

REPLACES scattered definitions in:
- trading_config.py
- trading_app.py
- entry_orchestrator.py
- edge_state_manager.py
- simplified_sizing.py
- trend_intelligence.py (TrendDirection)
- trading_regime.py (TrendDirection)
- regime_detector.py (RegimeConfig)
- regime_classifier.py (RegimeConfig)
- market_regime.py (RegimeConfig)

AUDIT FINDINGS (Feb 04):
1. TIME_INVALIDATION has 4 different values (8 velas, 6 velas, 60 min)
2. RR thresholds have 5 different values
3. Loss streak multipliers stack to 2.8% 
4. Session multipliers defined in 4 places
5. Extreme percentiles inconsistent
6. TrendDirection defined in 2 places (Feb 05)
7. RegimeConfig defined in 3 places (Feb 05)

THIS FILE IS THE SINGLE SOURCE OF TRUTH.
"""

from dataclasses import dataclass
from datetime import time
from enum import Enum
from typing import Dict, Tuple


# =============================================================================
# TREND DIRECTION - UNIFIED (Feb 05, 2026)
# =============================================================================

class TrendDirection(Enum):
    """
    Unified trend direction enum.
    
    🔥 FIX (Feb 05): Previously defined in:
    - trend_intelligence.py:46
    - trading_regime.py:43
    
    NOW: Single source here. Other files import from here.
    """
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# =============================================================================
# REGIME THRESHOLDS
# =============================================================================

@dataclass(frozen=True)
class RegimeThresholds:
    """Unified regime score thresholds."""
    
    # Regime score boundaries (0.0 to 1.0)
    RANGE_MAX: float = 0.35           # Below this = RANGE
    TRANSITION_MAX: float = 0.50      # Below this = TRANSITION
    # Above TRANSITION_MAX = TREND
    
    # Range extreme percentiles
    EXTREME_HIGH_PCT: float = 85.0    # >= this = at HIGH
    EXTREME_LOW_PCT: float = 15.0     # <= this = at LOW
    
    # Volume minimum for RANGE
    MIN_VOLUME_RATIO: float = 0.50    # 50% of average
    

REGIME = RegimeThresholds()


# =============================================================================
# TIME INVALIDATION - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class TimeInvalidation:
    """
    Unified time invalidation settings.
    
    AUDIT FIX: Previously had 4 values (8 velas, 6 velas, 60 min, etc.)
    NOW: Single source of truth.
    """
    
    # Edge expiration (how long before edge expires without entry)
    EDGE_TIMEOUT_CANDLES: int = 6       # 6 candles = 6 minutes at 1m TF
    EDGE_TIMEOUT_MINUTES: int = 10      # Fallback: 10 minutes
    
    # Direction lock timeout
    DIRECTION_LOCK_MINUTES: int = 15    # How long direction stays locked
    
    # Expansion mode timeout
    EXPANSION_TIMEOUT_MINUTES: int = 20 # How long expansion mode lasts
    
    # Cooldowns
    LOSS_COOLDOWN_MINUTES: int = 15     # After 3 losses
    REJECTION_COOLDOWN_SECONDS: int = 30  # After filter rejection


TIME = TimeInvalidation()


# =============================================================================
# RISK REWARD THRESHOLDS - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class RiskRewardThresholds:
    """
    Unified R:R thresholds.
    
    AUDIT FIX: Previously had 5 different values.
    NOW: Single hierarchy.
    """
    
    # Minimum R:R by regime
    RR_MIN_RANGE: float = 1.0         # Reversals need at least 1R
    RR_MIN_TRANSITION: float = 1.0    # Transition same as range
    RR_MIN_TREND: float = 1.0         # Trend allows 1R minimum
    RR_MIN_EXPANSION: float = 0.8     # Strong moves can accept lower
    
    # Ideal R:R targets
    RR_TARGET_RANGE: float = 2.0      # Aim for 2R in range
    RR_TARGET_TREND: float = 1.5      # Aim for 1.5R in trend
    
    # Absolute minimum (never go below this)
    RR_ABSOLUTE_MIN: float = 0.8


RR = RiskRewardThresholds()


# =============================================================================
# POSITION SIZING - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class PositionSizing:
    """
    Unified position sizing parameters.
    
    AUDIT FIX: Loss streak multipliers were stacking to 2.8%.
    NOW: Clear hierarchy with caps.
    """
    
    # Base risk per trade
    BASE_RISK_PCT: float = 1.0        # 1% of capital
    
    # Regime multipliers
    SIZE_MULT_RANGE_EXTREME: float = 0.50   # 50% at extremes
    SIZE_MULT_RANGE_MID: float = 0.40       # 40% mid-range
    SIZE_MULT_TRANSITION: float = 0.60      # 60% transition
    SIZE_MULT_TREND: float = 1.00           # 100% trend
    SIZE_MULT_EXPANSION: float = 0.80       # 80% expansion
    
    # Loss streak adjustments (NOT CUMULATIVE)
    SIZE_AFTER_1_LOSS: float = 0.80         # 80% after 1 loss
    SIZE_AFTER_2_LOSSES: float = 0.60       # 60% after 2 losses
    SIZE_AFTER_3_LOSSES: float = 0.40       # 40% after 3 losses (+ cooldown)
    
    # Absolute floor (never below this)
    SIZE_FLOOR: float = 0.25                # 25% minimum
    
    # Trend sizing (for trading_app.py compatibility)
    TREND_BASE: float = 0.80                # 80% base in trend
    TREND_ADX_BONUS: float = 0.20           # +20% max bonus based on ADX


SIZING = PositionSizing()


# =============================================================================
# SESSION MULTIPLIERS - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class SessionConfig:
    """
    Unified session configuration.
    
    AUDIT FIX: Had 4 different definitions with different values.
    NOW: Single source.
    """
    
    # Session times (UTC)
    ASIA_START: time = time(0, 0)
    ASIA_END: time = time(8, 0)
    LONDON_START: time = time(8, 0)
    LONDON_END: time = time(12, 0)
    NY_START: time = time(13, 0)
    NY_END: time = time(21, 0)
    DEAD_START: time = time(21, 0)
    DEAD_END: time = time(0, 0)
    
    # Session multipliers
    MULT_ASIA: float = 0.70           # Low volatility
    MULT_LONDON: float = 1.00         # Full size
    MULT_LONDON_NY_OVERLAP: float = 1.00  # Best session
    MULT_NY: float = 0.90             # Slightly reduced
    MULT_DEAD: float = 0.50           # Very low
    
    # Trade limits per session
    MAX_TRADES_ASIA: int = 2
    MAX_TRADES_LONDON: int = 4
    MAX_TRADES_NY: int = 3
    MAX_TRADES_DEAD: int = 1


SESSION = SessionConfig()


# =============================================================================
# DISPLACEMENT / MOMENTUM
# =============================================================================

@dataclass(frozen=True)
class MomentumConfig:
    """Displacement and momentum detection thresholds."""
    
    # Displacement detection (ATR multiples)
    DISPLACEMENT_ATR_THRESHOLD: float = 1.5   # 1.5x ATR = displacement
    STRONG_DISPLACEMENT_ATR: float = 2.5      # 2.5x ATR = strong
    EXTREME_DISPLACEMENT_ATR: float = 4.0     # 4x ATR = extreme
    
    # Bias invalidation thresholds
    INVALIDATION_ATR_THRESHOLD: float = 1.0   # 1x ATR move invalidates
    INVALIDATION_USD_MIN: float = 250.0       # Or $250 minimum
    
    # Direction lock thresholds
    LOCK_DISPLACEMENT_ATR: float = 2.0        # Lock after 2x ATR move


MOMENTUM = MomentumConfig()


# =============================================================================
# SCORE THRESHOLDS - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class ScoreThresholds:
    """
    Unified entry score thresholds.
    
    AUDIT FIX: Had 4 parallel scoring systems.
    NOW: Single hierarchy.
    """
    
    # Minimum scores by regime (0-10 scale)
    MIN_SCORE_RANGE: float = 5.0
    MIN_SCORE_TRANSITION: float = 4.5
    MIN_SCORE_TREND: float = 4.5
    MIN_SCORE_EXPANSION: float = 4.0   # Lower for momentum trades
    
    # Fast path minimum (Feb 04 analysis: 7.0 = MAS CONSERVADOR, reduces TIME_INVALIDATION)
    FAST_PATH_MIN_SCORE: float = 7.0
    
    # Confidence thresholds
    HIGH_CONFIDENCE: float = 0.70
    MEDIUM_CONFIDENCE: float = 0.55
    LOW_CONFIDENCE: float = 0.40


SCORES = ScoreThresholds()


# =============================================================================
# PRICE CONFIRMATION
# =============================================================================

@dataclass(frozen=True)
class PriceConfirmation:
    """Price confirmation thresholds."""
    
    # How much price can move against direction before rejection
    CONTRADICT_THRESHOLD_PCT: float = 0.25    # 0.25% move
    
    # Lookback for confirmation
    LOOKBACK_CANDLES: int = 3                 # 3 candles = 3 minutes
    
    # Strong confirmation threshold
    CONFIRM_THRESHOLD_PCT: float = 0.10       # 0.10% in direction


PRICE = PriceConfirmation()


# =============================================================================
# ADX THRESHOLDS - UNIFIED
# =============================================================================

@dataclass(frozen=True)
class ADXThresholds:
    """
    ADX zone thresholds.
    
    AUDIT FIX: Had gap between 18-25 (undefined zone).
    NOW: Continuous zones.
    """
    
    # Zone boundaries
    RANGE_MAX: float = 20.0           # Below 20 = RANGE
    TRANSITION_MAX: float = 25.0      # 20-25 = TRANSITION
    WEAK_TREND_MAX: float = 35.0      # 25-35 = WEAK_TREND
    # Above 35 = STRONG_TREND
    
    # Aliases for compatibility
    TREND_MIN: float = 20.0           # Alias: Above 20 = TREND (same as RANGE_MAX)


ADX = ADXThresholds()


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_session_name(hour: int) -> str:
    """Get session name from hour (UTC)."""
    if 0 <= hour < 8:
        return 'ASIA'
    elif 8 <= hour < 12:
        return 'LONDON'
    elif 12 <= hour < 14:
        return 'LONDON_NY_OVERLAP'
    elif 14 <= hour < 21:
        return 'NY'
    else:
        return 'DEAD'


def get_session_multiplier(session: str) -> float:
    """Get size multiplier for session."""
    return {
        'ASIA': SESSION.MULT_ASIA,
        'LONDON': SESSION.MULT_LONDON,
        'LONDON_NY_OVERLAP': SESSION.MULT_LONDON_NY_OVERLAP,
        'NY': SESSION.MULT_NY,
        'DEAD': SESSION.MULT_DEAD,
    }.get(session, 1.0)


def get_session_max_trades(session: str) -> int:
    """Get max trades for session."""
    return {
        'ASIA': SESSION.MAX_TRADES_ASIA,
        'LONDON': SESSION.MAX_TRADES_LONDON,
        'LONDON_NY_OVERLAP': SESSION.MAX_TRADES_LONDON,  # Same as London
        'NY': SESSION.MAX_TRADES_NY,
        'DEAD': SESSION.MAX_TRADES_DEAD,
    }.get(session, 3)


def get_size_after_losses(loss_count: int) -> float:
    """Get size multiplier after losses (NOT CUMULATIVE)."""
    if loss_count <= 0:
        return 1.0
    elif loss_count == 1:
        return SIZING.SIZE_AFTER_1_LOSS
    elif loss_count == 2:
        return SIZING.SIZE_AFTER_2_LOSSES
    else:
        return SIZING.SIZE_AFTER_3_LOSSES


def get_regime_min_score(regime: str) -> float:
    """Get minimum score for regime."""
    return {
        'RANGE': SCORES.MIN_SCORE_RANGE,
        'TRANSITION': SCORES.MIN_SCORE_TRANSITION,
        'TREND': SCORES.MIN_SCORE_TREND,
        'EXPANSION': SCORES.MIN_SCORE_EXPANSION,
    }.get(regime, SCORES.MIN_SCORE_TREND)


def get_regime_size_mult(regime: str) -> float:
    """Get size multiplier for regime."""
    return {
        'RANGE_EXTREME': SIZING.SIZE_MULT_RANGE_EXTREME,
        'RANGE_MID': SIZING.SIZE_MULT_RANGE_MID,
        'TRANSITION': SIZING.SIZE_MULT_TRANSITION,
        'TREND': SIZING.SIZE_MULT_TREND,
        'EXPANSION': SIZING.SIZE_MULT_EXPANSION,
    }.get(regime, SIZING.SIZE_MULT_TREND)


# =============================================================================
# UNIFIED REGIME CONFIG (Feb 05, 2026)
# =============================================================================

@dataclass
class UnifiedRegimeConfig:
    """
    Unified regime configuration.
    
    🔥 FIX (Feb 05): Previously defined in 3 places:
    - regime_detector.py:40 (entry behavior)
    - regime_classifier.py:34 (trading restrictions)
    - market_regime.py:41 (ADX thresholds)
    
    NOW: Single unified config here.
    """
    # Core mode
    mode: str  # 'TREND', 'RANGE', 'TRANSITION', 'NO_TRADE', 'EXPANSION'
    
    # ADX thresholds for detection
    adx_min: float = 0.0              # Minimum ADX for this regime
    adx_max: float = 100.0            # Maximum ADX for this regime
    
    # Position sizing
    size_multiplier: float = 1.0
    
    # Entry restrictions
    allow_trend_entries: bool = True
    allow_counter_trend: bool = False
    allow_range_entries: bool = False
    chasing_allowed: bool = False
    
    # Risk/Reward
    min_rr: float = 1.0
    max_trades_per_session: int = 5
    
    # Stop loss
    sl_atr_multiplier: float = 2.5
    
    # Order type
    order_type: str = 'LIMIT'         # 'MARKET', 'LIMIT', 'LIMIT_FALLBACK'
    
    # Description
    description: str = ""


# Pre-configured regimes
UNIFIED_REGIMES: Dict[str, UnifiedRegimeConfig] = {
    'TREND_EXPANSION': UnifiedRegimeConfig(
        mode='TREND',
        adx_min=25.0,
        adx_max=100.0,
        size_multiplier=1.0,
        allow_trend_entries=True,
        allow_counter_trend=False,
        allow_range_entries=False,
        chasing_allowed=True,
        min_rr=0.9,
        max_trades_per_session=4,
        sl_atr_multiplier=3.5,
        order_type='LIMIT_FALLBACK',
        description="Strong trend - full aggression allowed"
    ),
    'TREND_NORMAL': UnifiedRegimeConfig(
        mode='TREND',
        adx_min=20.0,
        adx_max=25.0,
        size_multiplier=0.8,
        allow_trend_entries=True,
        allow_counter_trend=False,
        allow_range_entries=False,
        chasing_allowed=True,
        min_rr=1.0,
        max_trades_per_session=3,
        sl_atr_multiplier=2.5,
        order_type='LIMIT_FALLBACK',
        description="Normal trend - standard risk"
    ),
    'TRANSITION': UnifiedRegimeConfig(
        mode='TRANSITION',
        adx_min=18.0,
        adx_max=20.0,
        size_multiplier=0.6,
        allow_trend_entries=True,
        allow_counter_trend=True,
        allow_range_entries=True,
        chasing_allowed=False,
        min_rr=1.0,
        max_trades_per_session=3,
        sl_atr_multiplier=2.5,
        order_type='LIMIT',
        description="Transition zone - reduced size, wait for clarity"
    ),
    'RANGE': UnifiedRegimeConfig(
        mode='RANGE',
        adx_min=0.0,
        adx_max=18.0,
        size_multiplier=0.5,
        allow_trend_entries=False,
        allow_counter_trend=True,
        allow_range_entries=True,
        chasing_allowed=False,
        min_rr=1.5,
        max_trades_per_session=2,
        sl_atr_multiplier=2.0,
        order_type='LIMIT',
        description="Range mode - mean reversion, extremes only"
    ),
    'NO_TRADE': UnifiedRegimeConfig(
        mode='NO_TRADE',
        adx_min=0.0,
        adx_max=0.0,
        size_multiplier=0.0,
        allow_trend_entries=False,
        allow_counter_trend=False,
        allow_range_entries=False,
        chasing_allowed=False,
        min_rr=999.0,
        max_trades_per_session=0,
        sl_atr_multiplier=0.0,
        order_type='NONE',
        description="No trade - market conditions unfavorable"
    ),
}


def get_unified_regime(adx: float) -> UnifiedRegimeConfig:
    """Get regime config based on ADX value."""
    if adx >= 25:
        return UNIFIED_REGIMES['TREND_EXPANSION']
    elif adx >= 20:
        return UNIFIED_REGIMES['TREND_NORMAL']
    elif adx >= 18:
        return UNIFIED_REGIMES['TRANSITION']
    else:
        return UNIFIED_REGIMES['RANGE']

