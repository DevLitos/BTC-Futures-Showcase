"""
Configuration parameters for BTC Perpetual Trading System
All strategy parameters and thresholds
"""

from enum import Enum
from typing import Literal


# ============================================================================
# TRADE TYPE ENUM - Distinguishes between trade strategies
# ============================================================================
class TradeType(str, Enum):
    """
    Trade type classification for edge analysis and distinct risk management.
    
    LONG_MAIN: Standard long entries following HTF bias
    SHORT_STRUCTURAL: Shorts when HTF is bearish (existing behavior)
    SHORT_PULLBACK: Tactical shorts in bullish HTF (correction capture)
    """
    LONG_MAIN = "LONG_MAIN"
    SHORT_STRUCTURAL = "SHORT_STRUCTURAL"
    SHORT_PULLBACK = "SHORT_PULLBACK"


# ============================================================================
# PULLBACK SHORT ENGINE - Tactical shorts in bullish markets
# ============================================================================
class PullbackShortConfig:
    """
    Configuration for the Pullback Short Engine.
    
    DESIGN PRINCIPLES:
    1. HTF filter NOT touched - bot remains "primarily LONG" when HTF LONG
    2. This is a SECOND type of short (tactical, not trend reversal)
    3. Risk and exits are DISTINCT from main strategy
    4. Feature flags + kill switch for safety
    
    WHAT IT IS:
    - Captures corrections (e.g., 97k → 92k) within bullish cycles
    - Allowed when: HTF LONG + price extremely extended + failure confirmed
    
    WHAT IT IS NOT:
    - NOT a trend reversal signal
    - NOT indiscriminate mean-reversion
    - Does NOT average losses
    """
    
    # =========================================================================
    # FEATURE FLAGS - Control activation
    # =========================================================================
    ENABLED = False                          # Master switch for pullback shorts
    LIVE_ALLOWED = False                     # Separate flag for live trading (paper first)
    
    # =========================================================================
    # GATE A: CONTEXT (required)
    # HTF_bias == LONG, no position, no pending orders
    # =========================================================================
    # (Checked in signal_engine, not configurable here)
    
    # =========================================================================
    # GATE B: EXTENSION DETECTION (required)
    # Z-score of price extension from EMA50 in ATR units
    # extension_z = (price - EMA50_1h) / ATR_1h
    # =========================================================================
    EXT_Z_MIN = 1.8                          # Minimum extension_z (RANGE regime)
    EXT_Z_MIN_TREND = 2.3                    # Minimum in TREND regime (stricter)
    
    # Aliases for backwards compatibility
    EXTENSION_Z_MIN_RANGE = 1.8
    EXTENSION_Z_MIN_TREND = 2.3
    
    # =========================================================================
    # GATE C: ANTI-BREAKOUT (required veto)
    # If breakout detected, prohibit pullback short
    # =========================================================================
    BREAKOUT_BODY_ATR_RATIO = 0.6            # Body > 60% of ATR = impulse
    BREAKOUT_CLOSE_RATIO = 0.8               # Close >= low + 0.8*(high-low)
    BREAKOUT_VOLUME_EXPANSION = 1.5          # Volume > 150% of SMA(20) [optional]
    BREAKOUT_COOLDOWN_5M = 4                 # Velas de cooldown tras breakout (20 min)
    
    # Alias for backwards compatibility
    BREAKOUT_COOLDOWN_CANDLES = 4
    BREAKOUT_CLOSE_NEAR_HIGH = 0.8
    
    # =========================================================================
    # SWING HIGH DETECTION (structural)
    # high[i] > high[i-N:i] AND high[i] > high[i+1:i+N+1]
    # =========================================================================
    SWING_N = 3                              # Candles left/right for swing detection (5m)
    SWING_VALIDITY_5M = 12                   # Swing expires after 12 candles (1 hour)
    
    # Alias
    SWING_LOOKBACK = 3
    
    # =========================================================================
    # TRIGGER: CLOSE BELOW CONFIRM LEVEL
    # confirm_level = low of swing high candle
    # trigger = close 5m < confirm_level
    # =========================================================================
    TRIGGER_TYPE: Literal["CLOSE_BELOW", "BOS"] = "CLOSE_BELOW"  # Primary trigger
    # confirm_level is dynamically set to low of swing high candle
    
    # =========================================================================
    # ENTRY RULES
    # Modo A (conservador): Retest entry con LIMIT
    # Modo B (rápido): Market si impulso fuerte
    # =========================================================================
    ENTRY_MODE: Literal["RETEST", "MARKET"] = "RETEST"  # Start with retest
    ENTRY_TIMEOUT_5M = 2                     # Cancel LIMIT if not filled in 2 candles (10 min)
    RETEST_LEVEL: Literal["VWAP", "EMA20"] = "VWAP"  # Where to place limit order
    
    # Alias for backwards compatibility
    PREFER_LIMIT_ENTRY = True
    ENTRY_TIMEOUT_CANDLES = 2
    
    # =========================================================================
    # RISK MANAGEMENT - DISTINCT from main strategy
    # =========================================================================
    SIZE_FACTOR = 0.30                       # 30% of normal position size
    SIZE_FACTOR_PULLBACK = 0.30              # Alias
    MAX_NOTIONAL_USD = 1000                  # Cap notional for safety
    
    # Stop Loss - Above swing high + buffer
    SL_BUFFER_ATR_5M = 0.15                  # Buffer = max(MIN_TICKS_BUFFER, 0.15*ATR_5m)
    SL_MIN_TICKS_BUFFER = 5                  # Minimum buffer in ticks
    SL_MIN_TICKS = 5                         # Alias
    
    # =========================================================================
    # TAKE PROFIT - TP1 fixed VWAP, TP2 dynamic ATR
    # =========================================================================
    # TP1: Fixed at VWAP
    TP1_TARGET = "VWAP"                      # Always VWAP for TP1
    TP1_CLOSE_PCT = 50                       # Close 50% at TP1
    TP1_MIN_PROFIT_FLOOR_ATR = 0.2           # Minimum profit in ATR units
    
    # TP2: Dynamic based on activity
    # k_tp2 = k_low + activity * (k_high - k_low)
    TP2_ENABLED = True
    TP2_K_LOW = 1.2                          # k when market dead
    TP2_K_HIGH = 2.8                         # k when market active
    
    # Aliases for backwards compatibility
    TP1_K_ATR_DEAD = 0.5
    TP1_K_ATR_ACTIVE = 0.9
    TP2_K_ATR_DEAD = 1.2
    TP2_K_ATR_ACTIVE = 2.8
    
    # =========================================================================
    # BE / PROFIT LOCK (after TP1)
    # =========================================================================
    BE_AFTER_TP1 = True                      # Move SL to BE after TP1
    BE_OFFSET_ATR = 0.1                      # BE = entry - 0.1*ATR (cover fees)
    
    # =========================================================================
    # TIME STOP - Exit if no progress
    # =========================================================================
    TIME_STOP_ENABLED = True
    TIME_STOP_5M = 6                         # Exit after 6 candles (30 min)
    TIME_STOP_MIN_MFE_R = 0.3                # Exit if MFE < 0.3R after time stop
    
    # Alias
    TIME_STOP_CANDLES_5M = 6
    
    # =========================================================================
    # SAFETY GUARDRAILS
    # =========================================================================
    MAX_TRADES_PER_DAY = 3                   # Maximum pullback shorts per day
    MAX_CONSECUTIVE_LOSSES = 2               # Disable after N consecutive losses
    DISABLE_ON_NEWS = True                   # Disable if macro event detected
    DISABLE_ON_INTEGRITY_ERROR = True        # Disable if position/order desync
    
    # =========================================================================
    # METRICS & LOGGING
    # =========================================================================
    LOG_ALL_GATES = True                     # Log each gate decision
    TRACK_MFE_MAE = True                     # Track MFE/MAE for edge analysis
    
    @classmethod
    def get_extension_z_min(cls, regime: str) -> float:
        """Get minimum extension_z based on market regime."""
        if regime in ["TREND_EXPANSION", "TREND", "TREND_EXHAUSTION"]:
            return cls.EXT_Z_MIN_TREND
        return cls.EXT_Z_MIN
    
    @classmethod
    def get_tp2_k(cls, activity_score: float) -> float:
        """
        Get TP2 k-factor based on market activity.
        
        k_tp2 = k_low + activity * (k_high - k_low)
        
        Args:
            activity_score: 0.0 (dead) to 1.0 (active)
        """
        return cls.TP2_K_LOW + activity_score * (cls.TP2_K_HIGH - cls.TP2_K_LOW)
    
    @classmethod
    def get_tp_k(cls, activity_score: float, is_tp2: bool = False) -> float:
        """
        Get TP k-factor based on market activity (backwards compat).
        """
        if is_tp2:
            return cls.get_tp2_k(activity_score)
        # TP1 not used with k anymore (fixed VWAP), but keep for compat
        return cls.TP1_K_ATR_DEAD + activity_score * (cls.TP1_K_ATR_ACTIVE - cls.TP1_K_ATR_DEAD)


# ============================================================================
# OPERATING MODE - Controls all system behavior
# ============================================================================
class OperatingMode:
    """
    DISCOVERY: Collect data, minimal filters, fixed low risk
    PRODUCTION: Full filters, dynamic sizing, strict gates
    
    NEVER mix logic with loose ifs - everything depends on MODE
    """
    MODE = "DISCOVERY"  # "DISCOVERY" or "PRODUCTION"
    
    # Mode-specific thresholds
    DISCOVERY_SCORE_THRESHOLD = 5   # More permissive
    PRODUCTION_SCORE_THRESHOLD = 7  # Stricter
    
    # Risk per trade by mode
    DISCOVERY_RISK_PCT = 0.25       # Fixed low risk in discovery
    PRODUCTION_RISK_PCT = 1.0       # Dynamic in production
    
    # RR minimum by mode - CRITICAL HARD FLOOR to filter bad setups
    # With fees + slippage + funding, RR < 0.9 is negative expectancy
    DISCOVERY_MIN_RR = 0.9          # HARD FLOOR - no trades below this
    PRODUCTION_MIN_RR = 1.2         # Require good RR or no trade
    
    # =========================================================================
    # RR HARD FLOOR BY REGIME (Jan 14, 2026)
    # Different regimes have different mathematical minimums:
    # - TREND: Need 0.9R+ because we're paying for momentum premium
    # - RANGE: Can accept 0.5R+ because high winrate mean-reversion
    # The regime_config.min_rr is the PRIMARY floor now
    # =========================================================================
    RR_HARD_FLOOR = 0.4             # ABSOLUTE mathematical minimum (any regime)
    RR_HARD_FLOOR_TREND = 0.9       # Trend regimes need higher RR
    RR_HARD_FLOOR_RANGE = 0.5       # Range regimes can accept lower RR
    
    # =========================================================================
    # RR PENALTY IN SCORING - PROPORTIONAL penalty (not fixed guillotine)
    # Penalty scales with how far below ideal RR we are
    # RANGE regime: lighter penalty (high winrate compensates)
    # TREND regime: heavier penalty (need RR for math to work)
    # =========================================================================
    RR_PENALTY_THRESHOLD = 1.0      # Below this RR, apply score penalty
    RR_PENALTY_MAX_POINTS = 2       # Maximum penalty at RR=0.5
    RR_PENALTY_RANGE_MULTIPLIER = 0.5  # RANGE penalty is 50% of TREND
    RR_EXCELLENT_THRESHOLD = 1.5    # Above this RR, add bonus
    RR_EXCELLENT_BONUS = 1          # Add 1 point if RR >= 1.5
    
    # =========================================================================
    # CONSECUTIVE LOSSES SIZING - Reduce size after losses
    # Progressive reduction: 1 loss = 80%, 2 losses = 60%, 3 losses = 50%
    # =========================================================================
    LOSS_SIZING_MULTIPLIERS = {
        0: 1.0,    # No losses - full size
        1: 0.80,   # 1 loss - 80% size
        2: 0.60,   # 2 losses - 60% size
        3: 0.50,   # 3+ losses - 50% size (anti-tilt)
    }
    
    # =========================================================================
    # CAPA 4: MINIMUM ACTIVITY PER SESSION - Prevent "no trades" failures
    # If trades/session is below minimum, progressively relax filters
    # =========================================================================
    MIN_TRADES_PER_SESSION = 1        # Minimum trades expected per 4h session
    MIN_TRADES_PER_DAY = 3            # Minimum trades expected per day
    
    # Filter relaxation when activity is too low
    LOW_ACTIVITY_SCORE_REDUCTION = 1  # Reduce threshold by 1 if low activity
    
    # =========================================================================
    # SESSION QUALITY SIZING - Adjust size by session quality
    # Best sessions = full size, dead hours = reduced
    # =========================================================================
    SESSION_SIZE_MULTIPLIERS = {
        'LONDON_NY_OVERLAP': 1.0,   # Best liquidity
        'NY_OPEN_VOLATILE': 0.70,   # First 15min of NY - volatile, reduce size
        'LONDON': 0.90,             # Good
        'NY': 0.90,                 # Good
        'ASIA': 0.75,               # Lower quality
        'DEAD_HOURS': 0.50,         # High risk
        'WEEKEND': 0.50,            # Low liquidity
        'OTHER': 0.75,              # Default reduced
    }
    
    @classmethod
    def get_min_rr(cls) -> float:
        return cls.DISCOVERY_MIN_RR if cls.is_discovery() else cls.PRODUCTION_MIN_RR
    
    @classmethod
    def get_loss_sizing_multiplier(cls, consecutive_losses: int) -> float:
        """Get sizing multiplier based on consecutive losses."""
        if consecutive_losses >= 3:
            return cls.LOSS_SIZING_MULTIPLIERS[3]
        return cls.LOSS_SIZING_MULTIPLIERS.get(consecutive_losses, 1.0)
    
    @classmethod
    def get_session_multiplier(cls, session_name: str) -> float:
        """Get sizing multiplier based on session."""
        return cls.SESSION_SIZE_MULTIPLIERS.get(session_name.upper(), 0.75)
    
    # =========================================================================
    # DAY OF WEEK SIZING - Reduce on riskier days
    # =========================================================================
    DAY_OF_WEEK_MULTIPLIERS = {
        0: 1.0,    # Monday - normal
        1: 1.0,    # Tuesday - good trends
        2: 1.0,    # Wednesday - normal
        3: 1.0,    # Thursday - normal
        4: 0.85,   # Friday - early close risk, reduce
        5: 0.60,   # Saturday - weekend, low liquidity
        6: 0.60,   # Sunday - weekend, low liquidity
    }
    
    @classmethod
    def get_day_multiplier(cls, day_of_week: int) -> float:
        """Get sizing multiplier based on day of week (0=Monday, 6=Sunday)."""
        return cls.DAY_OF_WEEK_MULTIPLIERS.get(day_of_week, 1.0)
    
    # SMART COOLDOWN - Based on exit reason and PnL size
    # TIME_INVALIDATION = market was dead, not our fault → short cooldown
    # SL_HIT = stop hit, need caution → medium cooldown  
    # EMERGENCY = system issue → longer cooldown
    COOLDOWN_TIME_INVALIDATION = 3   # 3 min - market was dead, not error
    COOLDOWN_SL_SMALL_LOSS = 8       # 8 min - small loss (< 0.5R)
    COOLDOWN_SL_NORMAL = 15          # 15 min - normal SL hit
    COOLDOWN_SL_LARGE_LOSS = 25      # 25 min - large loss (> 1R)
    COOLDOWN_EMERGENCY = 30          # 30 min - emergency close
    COOLDOWN_DEFAULT = 15            # 15 min - unknown reason
    
    # If signal still aligned with HTF after loss, reduce cooldown
    COOLDOWN_ALIGNED_REDUCTION = 0.5  # 50% reduction if still aligned
    
    @classmethod
    def get_smart_cooldown(cls, exit_reason: str, pnl_r: float) -> int:
        """Calculate cooldown based on exit reason and loss size"""
        if exit_reason == 'TIME_INVALIDATION':
            return cls.COOLDOWN_TIME_INVALIDATION
        elif 'EMERGENCY' in exit_reason:
            return cls.COOLDOWN_EMERGENCY
        elif 'SL' in exit_reason or 'STOP' in exit_reason:
            # Size-based cooldown for stop losses
            if abs(pnl_r) < 0.5:
                return cls.COOLDOWN_SL_SMALL_LOSS
            elif abs(pnl_r) < 1.0:
                return cls.COOLDOWN_SL_NORMAL
            else:
                return cls.COOLDOWN_SL_LARGE_LOSS
        else:
            return cls.COOLDOWN_DEFAULT
    
    # Logging level
    DISCOVERY_LOGGING = "DEBUG"     # Maximum logging
    PRODUCTION_LOGGING = "INFO"     # Normal logging
    
    @classmethod
    def is_discovery(cls) -> bool:
        return cls.MODE == "DISCOVERY"
    
    @classmethod
    def get_score_threshold(cls) -> int:
        return cls.DISCOVERY_SCORE_THRESHOLD if cls.is_discovery() else cls.PRODUCTION_SCORE_THRESHOLD
    
    @classmethod
    def get_risk_pct(cls) -> float:
        return cls.DISCOVERY_RISK_PCT if cls.is_discovery() else cls.PRODUCTION_RISK_PCT


# ============================================================================
# STATISTICAL DISCIPLINE - DO NOT MODIFY DURING DISCOVERY
# ============================================================================
class StatisticalDiscipline:
    """
    Rules to prevent premature optimization and system tampering.
    Your future self WILL try to break these. Don't let them.
    
    These are not suggestions. They are constraints.
    """
    
    # EVALUATION HORIZON
    # Evaluate by TRADE COUNT, not by time.
    # 200 trades = 1 evaluation cycle
    EVALUATION_HORIZON_TRADES = 200
    
    # MINIMUM SAMPLE SIZE
    # Do not draw ANY conclusions before this
    MIN_SAMPLE_SIZE = 100
    
    # SIGNAL DEATH CRITERIA
    # If expectancy < 0 after N trades, signal is dead
    SIGNAL_DEATH_TRADES = 500
    SIGNAL_DEATH_EXPECTANCY = 0.0  # Must be > 0 to survive
    
    # SCORE DISTRIBUTION SANITY
    # If > 35% of trades have score >= 7, something is wrong
    # (or market is exceptionally trending - verify manually)
    MAX_HIGH_SCORE_RATIO = 0.35
    HIGH_SCORE_THRESHOLD = 7
    
    # SCORE INTERPRETATION
    # 5 = mediocre but valid event (not "neutral")
    # 6-7 = core edge
    # 8-9 = tail event (rare, don't expect these often)
    SCORE_5_MEANING = "mediocre_valid"
    SCORE_6_7_MEANING = "core_edge"
    SCORE_8_9_MEANING = "tail_rare"
    
    # =========================================================================
    # EXPLICIT PROHIBITIONS
    # =========================================================================
    
    # ❌ DO NOT optimize score weights before SIGNAL_DEATH_TRADES
    # ❌ DO NOT add new synergy bonuses (max = 1)
    # ❌ DO NOT change DISCOVERY_RISK_PCT until 500+ trades
    # ❌ DO NOT switch to PRODUCTION until drawdowns are known
    # ❌ DO NOT interpret "mediocre" score (5) as "bad"
    # ❌ DO NOT discard session data that contradicts expectations
    #    (edge may be WORSE in London+NY, BETTER in "ugly" zones)
    
    # When tempted to break these rules, re-read this:
    # "If a trade hurts, it's fulfilling its function."
    # "Renaissance would accept the data. So should you."
    
    @classmethod
    def can_evaluate(cls, trade_count: int) -> bool:
        """Check if we have enough trades to evaluate."""
        return trade_count >= cls.MIN_SAMPLE_SIZE
    
    @classmethod
    def should_kill_signal(cls, trade_count: int, expectancy: float) -> bool:
        """Check if a signal should be killed based on data."""
        if trade_count < cls.SIGNAL_DEATH_TRADES:
            return False  # Not enough data
        return expectancy < cls.SIGNAL_DEATH_EXPECTANCY
    
    @classmethod
    def is_score_distribution_suspect(cls, high_score_ratio: float) -> bool:
        """Check if too many trades are scoring high (suspicious)."""
        return high_score_ratio > cls.MAX_HIGH_SCORE_RATIO


# ============================================================================
# TIMEFRAME CONFIGURATION
# ============================================================================
class Timeframes:
    HTF = '1h'      # High Timeframe - Bias and Strength
    MTF = '5m'      # Mid Timeframe - Structure and ZOI
    LTF = '1m'      # Low Timeframe - Trigger and Management


# ============================================================================
# INDICATOR PERIODS
# ============================================================================
class IndicatorPeriods:
    # HTF (1H)
    EMA_50_HTF = 50
    EMA_200_HTF = 200
    ADX_HTF = 14
    ATR_HTF = 14
    ATR_MA_HTF = 20  # Moving average of ATR for comparison
    
    # MTF (5M)
    EMA_20_MTF = 20
    EMA_50_MTF = 50
    
    # LTF (1M)
    VOLUME_MA_LTF = 20  # For relative volume calculation
    
    # Common
    ATR_1M = 14


# ============================================================================
# KILL CONDITIONS & RISK LIMITS
# ============================================================================
class KillConditions:
    # Drawdown limits
    MAX_DAILY_DRAWDOWN_PCT = 3.0  # 3% daily drawdown stops the bot
    MAX_CONSECUTIVE_LOSSES = 3     # 3 consecutive losses stops the bot
    
    # Technical resilience
    WEBSOCKET_TIMEOUT_SECONDS = 75  # Max websocket silence before emergency exit (increased for Kraken - no constant messages)
    HEARTBEAT_INTERVAL_SECONDS = 2  # Check connection every 2 seconds
    
    # Black swan filter (IMPROVED)
    # Nivel 1: Defensive mode (reduce size, tighter stops)
    DEFENSIVE_MODE_MULTIPLIER = 3.0  # >= 3x ATR -> defensive mode
    # Nivel 2: Full cooldown (only with spike + reversal)
    BLACK_SWAN_MULTIPLIER = 4.0      # >= 4x ATR + wick -> cooldown
    BLACK_SWAN_WICK_RATIO = 0.6      # Wick must be > 60% of candle
    BLACK_SWAN_COOLDOWN_MINUTES = 8  # Reduced from 30 to 8 minutes
    
    # DEBUG FLAG: Allow weak BOS confirmations
    ALLOW_WEAK_BOS = True  # Set to False for strict mode
    
    # External environment
    MAX_FUNDING_RATE = 0.08  # Don't trade if funding > ±0.08% per 8h
    
    # Spread and volume
    MAX_SPREAD_MULTIPLIER = 2.0  # Max allowed spread vs normal
    MIN_VOLUME_THRESHOLD = 0.3   # Minimum volume as fraction of average


# ============================================================================
# ENTRY GATE SYSTEM - Hard/Soft Gate Configuration (Jan 20, 2026)
# ============================================================================
class EntryGateConfig:
    """
    Unified entry decision system with graduated sizing.
    
    PHILOSOPHY:
    - HARD GATES: Must pass or trade blocked (spread, chaos, HTF veto)
    - SOFT GATES: Adjust sizing, don't block (score, RR, LTF confirmations)
    - PROBE TRADES: When edge detected but soft gates marginal (0.25x size)
    
    This fixes the "missed winners" problem while maintaining proper risk control.
    """
    
    # SCORE-BASED SIZING (replaces binary threshold)
    # Graduated sizing based on score quality
    SCORE_BLOCK_FLOOR = 4.5          # Below this = block (floor breach)
    SCORE_PROBE_MAX = 5.5            # 4.5-5.5 = probe (0.25x)
    SCORE_REDUCED_MAX = 6.5          # 5.5-6.5 = reduced (0.5x)
    # Above 6.5 = full size (1.0x)
    
    # RR-BASED SIZING
    # Graduated sizing based on risk:reward quality
    RR_BLOCK_FLOOR = 0.6             # Below 0.6 = mathematical roulette
    RR_PROBE_MAX = 1.0               # 0.6-1.0 = probe (0.25x)
    RR_FULL_MIN = 1.2                # Above 1.2 = full size
    
    # RANGE regime has different RR tolerances (high winrate)
    RR_BLOCK_FLOOR_RANGE = 0.5       # Lower floor for RANGE
    RR_PROBE_MAX_RANGE = 0.8         # Lower threshold for probe in RANGE
    
    # LTF CONFIRMATIONS
    # Never require all confirmations (anti-paralysis)
    LTF_MAX_REQUIRED = 2             # Max confirmations required (out of 3)
    LTF_WEAK_SIZE = 0.5              # Size when 1/3 confirmations
    LTF_VERY_WEAK_SIZE = 0.25        # Size when 0/3 confirmations
    
    # PROBE TRADE SETTINGS
    PROBE_SIZE_DEFAULT = 0.25        # Default probe size
    PROBE_SIZE_MIN = 0.20            # Never go below 20%
    MIN_SIZE_MULTIPLIER = 0.25       # Minimum trade size (25% of normal)
    
    # SESSION LIMIT OVERRIDE
    # High score can override session limit
    SESSION_LIMIT_OVERRIDE_SCORE = 7.0  # Score required to override limit
    SESSION_LIMIT_OVERRIDE_SIZE = 0.5   # Size reduction when overriding
    
    @classmethod
    def get_score_size_multiplier(cls, score: float) -> float:
        """Get position size multiplier based on score."""
        if score < cls.SCORE_BLOCK_FLOOR:
            return 0.0  # Block
        elif score < cls.SCORE_PROBE_MAX:
            return 0.25  # Probe
        elif score < cls.SCORE_REDUCED_MAX:
            return 0.5   # Reduced
        else:
            return 1.0   # Full
    
    @classmethod
    def get_rr_size_multiplier(cls, rr_net: float, regime: str = "NORMAL") -> float:
        """Get position size multiplier based on RR."""
        block_floor = cls.RR_BLOCK_FLOOR_RANGE if regime == "RANGE" else cls.RR_BLOCK_FLOOR
        probe_max = cls.RR_PROBE_MAX_RANGE if regime == "RANGE" else cls.RR_PROBE_MAX
        
        if rr_net < block_floor:
            return 0.0  # Block
        elif rr_net < probe_max:
            return 0.25  # Probe
        elif rr_net < cls.RR_FULL_MIN:
            return 0.5   # Reduced
        else:
            return 1.0   # Full
    
    @classmethod
    def get_ltf_size_multiplier(cls, confirmations: int, total: int = 3) -> float:
        """Get position size multiplier based on LTF confirmations."""
        required = min(cls.LTF_MAX_REQUIRED, total - 1)  # Never require all
        
        if confirmations >= required:
            return 1.0
        elif confirmations >= 1:
            return cls.LTF_WEAK_SIZE
        else:
            return cls.LTF_VERY_WEAK_SIZE


# ============================================================================
# HTF FILTERS (1H)
# ============================================================================
class HTFFilters:
    # RETUNED (Jan 9, 2026): ADX slope NO LONGER BLOCKS
    # ADX solo define régimen, no timing
    # Slope negativo en pullback es NORMAL, no patológico
    # PHASE A FIX (Jan 18, 2026): Reduced thresholds to allow more pullback entries
    MIN_ADX = 18                    # Reduced from 20 - allow softer trends
    MIN_ADX_SLOPE = -999            # DEPRECATED: slope never blocks
    MIN_ATR_VS_MA = 0.85            # Reduced from 1.0 - allow normal contractions
    
    # DEPRECATED: Slope filtering removed
    # ADX slope is now INFO/TAG only, never a veto
    # Colapso real = estructura (price < EMA50), no indicador
    SMART_SLOPE_FILTER = False      # DISABLED - slope is info only
    WEAK_ADX_THRESHOLD = 20         # Not used (kept for reference)
    STRONG_SLOPE_DECLINE = -0.15    # Not used (kept for reference)
    
    # Counter-trend trading in defensive mode
    ALLOW_COUNTERTREND_DEFENSIVE = True  # Allow counter-trend with reduced size
    
    # Correlation filters (for NQ/ES)
    CHECK_CORRELATION = True
    CORRELATION_TIMEFRAME = '5m'


# ============================================================================
# LOCATION FILTER (MACRO CONTEXT) - Jan 14, 2026
# ============================================================================
class LocationFilters:
    """
    Location-based veto system - prevents buying tops in ranging markets.
    
    "Location" is distinct from momentum/structure. EMA/ADX/BOS measure dynamics,
    not absolute context: WHERE you are in the HTF map (20-60 day range).
    
    This "macro veto" differentiates a bot that survives from one that
    self-liquidates by buying tops.
    
    Rules:
    1. EXTREMES_VETO: In RANGE, prohibit longs in top 10% / shorts in bottom 10%
    2. HEADROOM_VETO: If distance to structure high < k*ATR, no longs in RANGE
    3. BREAKOUT_OVERRIDE: Allow extremes if ADX high + volume expansion
    """
    # Percentile thresholds for extremes veto
    EXTREME_HIGH_PERCENTILE = 90.0   # Top 10% of 60-day range → no LONG in RANGE
    EXTREME_LOW_PERCENTILE = 10.0    # Bottom 10% of 60-day range → no SHORT in RANGE
    
    # Lookback periods
    RANGE_LOOKBACK_DAYS = 60         # Primary lookback for range calculation
    RANGE_LOOKBACK_SHORT_DAYS = 20   # Short-term confirmation
    
    # Headroom thresholds (in ATR units)
    MIN_HEADROOM_ATR = 0.5           # Minimum space to target
    MIN_HEADROOM_ATR_STRICT = 1.0    # For weaker setups
    
    # Breakout override thresholds
    BREAKOUT_ADX_MIN = 30.0          # ADX required for breakout override
    BREAKOUT_VOLUME_EXPANSION = 1.3  # Volume > 130% of avg for breakout
    
    # Enable/disable the filter
    ENABLED = True


# ============================================================================
# MTF FILTERS (5M)
# ============================================================================
class MTFFilters:
    # Fibonacci retracement range
    MIN_FIBO_RETRACEMENT = 38.2
    MAX_FIBO_RETRACEMENT = 61.8
    
    # Structure
    MAX_CORRECTION_CANDLES = 3      # Max candles in pullback
    BOS_LOOKBACK_PERIODS = 5        # Periods to look back for BOS
    
    # Anti-overextension
    MAX_DISTANCE_FROM_VWAP_ATR = 2.0  # Max 2x ATR from VWAP
    
    # Structure validation periods
    # Structure validation periods
    HL_LH_PERIODS = 3              # Number of periods to validate HL/LH
    
    # SYSTEM 2.0 - Market Regime Thresholds
    ADX_TREND_STRONG = 30
    ADX_EXTREME = 40
    
    # Pullback limits (in ATR) - increased for realistic BTC pullbacks
    MAX_PULLBACK_ATR_TREND = 1.3   # Allow deeper pullbacks in BTC
    MAX_PULLBACK_ATR_WEAK = 1.3
    
    # Consolidation
    MAX_CONSOLIDATION_BARS = 12
    
    # Weak BOS tolerance (distance from EMA20)
    BOS_WEAK_TOLERANCE_PCT = 0.0015  # 0.15%


# ============================================================================
# LTF FILTERS (1M)
# ============================================================================
class LTFFilters:
    MIN_RELATIVE_VOLUME = 1.5       # Minimum relative volume
    MIN_ORDERBOOK_IMBALANCE = 60    # Minimum 60/40 imbalance
    ORDERBOOK_DEPTH = 10            # Depth levels to analyze
    
    # Spread spike detection
    MAX_SPREAD_SPIKE_MULTIPLIER = 2.0


# ============================================================================
# DYNAMIC RISK MANAGEMENT (V2.1 - Palancas de Multiplicación)
# ============================================================================
class DynamicRiskConfig:
    """
    PALANCA 1: Riesgo dinámico por equity
    PALANCA 2: Compounding obligatorio
    PALANCA 3: Parciales inteligentes (50/30/20)
    PALANCA 4: Leverage funcional 2x
    
    Esto NO cambia el edge, solo el multiplicador.
    Cuanto menor el capital, más agresivo el riesgo porcentual.
    """
    # PALANCA 1: Risk tiers by equity (in base currency)
    # Format: (max_equity, risk_pct)
    RISK_TIERS = [
        (2000.0, 1.0),    # < 2k€: 1.0% risk per trade
        (5000.0, 0.75),   # 2k-5k€: 0.75% risk per trade  
        (float('inf'), 0.5),  # > 5k€: 0.5% risk per trade
    ]
    
    # PALANCA 4: Leverage funcional (libera capital, NO aumenta riesgo real)
    # El SL sigue determinando el riesgo máximo
    DEFAULT_LEVERAGE = 2  # 2x leverage (conservative, functional)
    MAX_LEVERAGE = 3      # Never exceed 3x
    
    # PALANCA 2: Compounding obligatorio
    COMPOUNDING_ENABLED = True  # position_size = risk_pct * equity_actual / stop_distance
    
    # PALANCA 3: Parciales inteligentes (consolidado aquí para centralización)
    SMART_PARTIALS_ENABLED = True
    PARTIAL_TP1_PCT = 50.0   # 50% → TP1 (bloqueo de riesgo)
    PARTIAL_TP2_PCT = 30.0   # 30% → TP2 (estructura)
    PARTIAL_RUNNER_PCT = 20.0  # 20% → runner (trailing estructural)
    TP1_R_TARGET = 1.0       # TP1 a 1R
    TP2_R_TARGET = 2.0       # TP2 a 2R
    
    # Minimum risk floor (never risk less than this)
    MIN_RISK_PCT = 0.25   # 0.25% minimum (avoid dust trades)
    
    # Emergency risk cap (never risk more than this regardless of tier)
    MAX_RISK_PCT = 1.5    # 1.5% absolute maximum
    
    # =========================================================================
    # MICRO ACCOUNT SIZING (for accounts < $500)
    # Uses NOTIONAL-based sizing instead of risk-based
    # This ensures minimum tradeable sizes on exchanges
    # =========================================================================
    MICRO_ACCOUNT_THRESHOLD = 500.0   # Below this, use notional-based sizing
    BASE_NOTIONAL_USD = 7.5           # Target notional per trade ($7.5)
    MIN_NOTIONAL_USD = 5.0            # Never trade below this notional
    MIN_BTC_SIZE = 0.0001             # Kraken Futures minimum (absolute floor)


# ============================================================================
# RISK MANAGEMENT
# ============================================================================
class RiskManagement:
    # Leverage (configurable via LEVERAGE env var)
    # FIX (Jan 3, 2026): Changed from 0 to 2 - 0x caused invalid position sizes
    DEFAULT_LEVERAGE = DynamicRiskConfig.DEFAULT_LEVERAGE  # Use dynamic config
    
    # Risk per session - RETUNED (Jan 4, 2026)
    # These are NOW OVERRIDDEN by DynamicRiskConfig.RISK_TIERS based on equity
    # Kept for backward compatibility and session-based adjustments
    RISK_LONDON_NY_PCT = 0.8        # Was 1.0% - reduced for higher frequency
    RISK_NY_OPEN_PCT = 0.4          # Was 0.5% - reduced for volatility
    RISK_ASIA_WEEKEND_PCT = 0.4     # Was 0.5% - reduced
    
    # Defensive mode multiplier (after losses)
    DEFENSIVE_MODE_MULTIPLIER = 0.7  # NEW: 70% size after losses (was implicit 50%)
    
    # Stop loss
    STOP_LOSS_ATR_BUFFER = 0.3      # Buffer of 0.3x ATR(1m) above SL level
    
    # Take profits - Minimum 1R to ensure positive expectancy after fees
    # With fees ~0.04% + slippage + funding, RR < 1 is negative EV
    # FIX (Jan 14, 2026): Increased from 1.0 to 1.2 for more volatility room
    TP1_RISK_MULTIPLE = 1.2         # TP at 1.2R (allows more room for market volatility)
    TP1_CLOSE_PCT = 60              # Close 60% at TP1, leave 40% runner
    
    # Exit management (FIXED - no inventos)
    USE_PARTIALS = False            # Simple TP1 + runner
    MOVE_TO_BE_AFTER_TP1 = True     # Always move SL to BE after TP1
    
    # =========================================================================
    # PALANCA 3: PARCIALES INTELIGENTES (sin tocar entrada)
    # =========================================================================
    # Redistribución de tamaño para maximizar expectancy sin cambiar edge
    # Old: 100% TP1 o trailing
    # New: 50% TP1 + 30% TP2 + 20% runner con trailing estructural
    SMART_PARTIALS_ENABLED = True
    
    # Distribución de parciales (debe sumar 100)
    PARTIAL_TP1_PCT = 50            # 50% → TP1 (bloqueas riesgo)
    PARTIAL_TP2_PCT = 30            # 30% → TP2 (estructura)
    PARTIAL_RUNNER_PCT = 20         # 20% → runner con trailing estructural
    
    # TP targets en R-múltiples
    TP1_R_TARGET = 1.0              # TP1 a 1R (bloqueo de riesgo)
    TP2_R_TARGET = 2.0              # TP2 a 2R (estructura)
    # Runner: trailing estructural (no ATR, sino swings)
    
    # Trailing estructural para runner
    RUNNER_TRAILING_ATR_MULT = 2.5  # 2.5x ATR para el runner
    RUNNER_SWING_BASED = True       # Usa swings en lugar de ATR puro
    
    # Trailing stop
    TRAILING_STOP_ATR_MULTIPLIER = 2.0  # 2x ATR(1m) - fue 1.0x (AUMENTADO para menos falsas salidas)
    
    # Climactic exit
    CLIMACTIC_VOLUME_MULTIPLIER = 2.5   # Exit if volume >= 2.5-3x average
    
    # Time filters - RELAXED to let trades run
    MAX_TRADE_DURATION_MINUTES = 90     # Max time in trade without TP1 (was 45)
    MIN_TRADE_DURATION_MINUTES = 60     # Minimum before considering time exit (was 30)


# ============================================================================
# ORDER EXECUTION
# ============================================================================
class OrderExecution:
    ORDER_TYPE = 'LIMIT'           # Use limit orders
    POST_ONLY = True               # Post-only to avoid taker fees
    ENTRY_TIMEOUT_CANDLES = 3      # Cancel order after 3 candles
    FILL_TIMEOUT_SECONDS = 180     # 3 minute timeout for order fill
    
    MIN_POSITION_SIZE_USD = 11     # Minimum position size
    
    # FIX #2 (Jan 3, 2026): Market Orders in Momentum
    # Use market order when price velocity is high
    USE_MARKET_ON_MOMENTUM = True   # Enable market orders in momentum
    MOMENTUM_VELOCITY_MULTIPLIER = 2.0  # Market if velocity > 2x ATR (fast move)
    MOMENTUM_MAX_VELOCITY = 3.5  # SKIP entry if velocity > 3.5x ATR AND ADX < 25 (chasing in dead market)
    MOMENTUM_MAX_VELOCITY_ADX_THRESHOLD = 25  # ADX below this = dead market, don't chase
    MARKET_ORDER_TIMEOUT_SECONDS = 0.5  # Wait only 500ms for market fills (must be instant)
    
    # =========================================================================
    # ANTI-CHASING GATE - Block entries after impulsive candles
    # If last N candles are > threshold ATR, we're chasing, not anticipating
    # RELAXED (Jan 12, 2026): Was too aggressive, blocking valid entries
    # =========================================================================
    ANTI_CHASING_ENABLED = True
    ANTI_CHASING_ATR_THRESHOLD = 1.5  # Candle range > 1.5x ATR = impulsive (was 1.2)
    ANTI_CHASING_LOOKBACK = 2          # Check last 2 candles (was 3)
    
    # =========================================================================
    # FUNDING DIRECTION SIZING - Reduce size when funding against position
    # LONG with positive funding = paying funding = reduce size
    # SHORT with negative funding = paying funding = reduce size
    # =========================================================================
    FUNDING_SIZING_ENABLED = True
    FUNDING_AGAINST_MULTIPLIER = 0.75  # 75% size when paying funding
    FUNDING_FAVORABLE_MULTIPLIER = 1.1  # 110% size when receiving funding
    FUNDING_NEUTRAL_THRESHOLD = 0.01   # ±0.01% = neutral


# ============================================================================
# POST-TRADE MANAGEMENT
# ============================================================================
class PostTrade:
    COOLDOWN_MINUTES = 15          # Mandatory cooldown after trade
    
    # Performance thresholds
    MIN_PROFIT_FACTOR = 1.5        # Minimum acceptable profit factor
    MIN_EXPECTANCY = 0.0           # Expectancy must be positive


# ============================================================================
# SMART EXITS - Advanced profit taking and early exit detection
# Implemented: Jan 13, 2026
# ============================================================================
class SmartExits:
    """
    Advanced exit management to maximize profits and minimize drawdowns.
    
    5 Key Improvements:
    1. Scaled partial profit taking (not just TP1/TP2)
    2. Momentum exit - exit before reversals
    3. VPOC resistance exit
    4. Time-based profit lock
    5. Volatility squeeze exit
    """
    
    # =========================================================================
    # 1. SCALED PARTIAL EXITS - Take profits progressively
    # =========================================================================
    SCALED_PARTIALS_ENABLED = True
    
    # Exit levels: (MFE in R, % of position to exit)
    # More aggressive profit taking to capture gains
    SCALE_OUT_LEVELS = [
        (0.8, 0.33),   # Exit 33% at +0.8R
        (1.5, 0.33),   # Exit 33% at +1.5R  
        (2.5, 0.20),   # Exit 20% at +2.5R (if reached)
        # Remaining 14% runs with trailing stop
    ]
    
    # Minimum position to keep for runner
    MIN_RUNNER_PCT = 0.10  # Always keep at least 10% as runner
    
    # =========================================================================
    # 2. MOMENTUM EXIT - Exit when momentum dies BEFORE price falls
    # =========================================================================
    MOMENTUM_EXIT_ENABLED = True
    
    # RSI thresholds for momentum death
    RSI_LONG_EXIT_THRESHOLD = 40   # Exit LONG if RSI drops below 40
    RSI_SHORT_EXIT_THRESHOLD = 60  # Exit SHORT if RSI rises above 60
    
    # Only apply if trade is already in profit
    MOMENTUM_EXIT_MIN_MFE_R = 0.5  # Need at least 0.5R profit before momentum exit
    
    # Volume confirmation (optional)
    REQUIRE_VOLUME_DECLINE = False  # If True, also check for volume drop
    VOLUME_DECLINE_THRESHOLD = 0.7  # Volume < 70% of average = declining
    
    # =========================================================================
    # 3. VPOC/RESISTANCE EXIT - Exit before hitting strong levels
    # =========================================================================
    VPOC_EXIT_ENABLED = True
    
    # Distance to VPOC/resistance to trigger exit (as % of price)
    VPOC_PROXIMITY_PCT = 0.002  # 0.2% = within 0.2% of VPOC
    
    # Minimum profit required before VPOC exit
    VPOC_EXIT_MIN_MFE_R = 0.3  # Need at least 0.3R profit
    
    # =========================================================================
    # 4. TIME-BASED PROFIT LOCK - Lock profits after time threshold
    # =========================================================================
    TIME_PROFIT_LOCK_ENABLED = True
    
    # After X candles in profit, tighten stop to lock minimum R
    TIME_LOCK_CANDLES = 10  # After 10 candles (10 min in 1m chart)
    TIME_LOCK_MIN_MFE_R = 0.5  # Trigger only if MFE >= 0.5R
    TIME_LOCK_FLOOR_R = 0.3   # Lock at least +0.3R profit
    
    # =========================================================================
    # 5. VOLATILITY SQUEEZE EXIT - Exit in compression before resolution
    # =========================================================================
    VOLATILITY_SQUEEZE_EXIT_ENABLED = True
    
    # Bollinger Band width ratio to detect squeeze
    BB_SQUEEZE_RATIO = 0.5  # BB width < 50% of average = squeeze
    
    # Take profit if in squeeze with small profit
    SQUEEZE_EXIT_MIN_MFE_R = 0.2  # Exit if profit between 0.2R and 0.5R in squeeze
    SQUEEZE_EXIT_MAX_MFE_R = 0.5  # Don't exit if already > 0.5R (let it run)
    
    # =========================================================================
    # GLOBAL EXIT PRIORITY (order of checks)
    # =========================================================================
    EXIT_PRIORITY = [
        'STOP_LOSS',       # Always first - risk management
        'TRAILING_STOP',   # Second - locked profits
        'TIME_PROFIT_LOCK',# Third - time-based locks
        'MOMENTUM_EXIT',   # Fourth - momentum death
        'VPOC_EXIT',       # Fifth - resistance proximity
        'VOLATILITY_SQUEEZE', # Sixth - compression exit
        'PARTIAL_SCALE',   # Seventh - partial profit taking
    ]


# ============================================================================
# SESSION DEFINITIONS (EST)
# ============================================================================
class TradingSessions:
    # Session windows (hour, minute)
    LONDON_START = (3, 0)
    LONDON_END = (11, 0)
    
    NY_START = (9, 30)
    NY_END = (16, 0)
    
    NY_OPEN_VOLATILE_START = (9, 30)
    NY_OPEN_VOLATILE_END = (9, 45)
    
    ASIA_START = (18, 0)
    ASIA_END = (3, 0)


# ============================================================================
# DATA & MONITORING
# ============================================================================
class DataConfig:
    # Minimum bars for warm-up
    MIN_BARS_1H = 200              # For EMA 200 on 1H
    MIN_BARS_5M = 50               # For EMA 50 on 5M
    MIN_BARS_1M = 50               # For indicators on 1M
    
    # Kafka topics
    RAW_DATA_TOPIC = 'raw_trades'
    SIGNALS_TOPIC = 'trading_signals'
    ORDERS_TOPIC = 'trading_orders'
    
    # Symbols
    PRIMARY_SYMBOL = 'BTC/USDT'
    CORRELATION_SYMBOLS = ['NQ', 'ES']  # For macro correlation


# ============================================================================
# FIX #6: FUNDING RATE CONTROL
# ============================================================================
class FundingRateControl:
    # AUDITORÍA (Jan 9, 2026): RE-ENABLED with relaxed thresholds
    # Critical for live trading - protects from funding spikes
    # FIX (Jan 12, 2026): Raised thresholds - Kraken normal funding is ~0.10%
    ENABLED = True
    MAX_FUNDING_RATE_PCT = 0.12      # Entry threshold - block new LONGs above this
    FORCE_EXIT_FUNDING_PCT = 0.20    # Force exit only at extreme funding (was 0.08 - too aggressive)
    CHECK_INTERVAL_MINUTES = 60      # Check funding rate every hour
    
    # =========================================================================
    # FUNDING BUCKETS for sizing decisions
    # =========================================================================
    FUNDING_BUCKETS = {
        'extreme_positive': 0.05,   # >= 0.05% = extremely bullish (bearish for longs)
        'high_positive': 0.03,      # >= 0.03% = high positive
        'normal': 0.01,             # ±0.01% = normal range
        'high_negative': -0.03,     # <= -0.03% = high negative  
        'extreme_negative': -0.05,  # <= -0.05% = extremely bearish
    }
    
    @classmethod
    def get_funding_bucket(cls, funding_rate: float) -> str:
        """Classify funding rate into a bucket."""
        if funding_rate >= cls.FUNDING_BUCKETS['extreme_positive']:
            return 'extreme_positive'
        elif funding_rate >= cls.FUNDING_BUCKETS['high_positive']:
            return 'high_positive'
        elif funding_rate <= cls.FUNDING_BUCKETS['extreme_negative']:
            return 'extreme_negative'
        elif funding_rate <= cls.FUNDING_BUCKETS['high_negative']:
            return 'high_negative'
        else:
            return 'normal'
    
    @classmethod
    def get_funding_sizing_factor(cls, funding_rate: float, bias: str) -> float:
        """
        Get sizing factor based on funding rate and position direction.
        
        LONG + positive funding = paying = reduce size
        LONG + negative funding = receiving = can increase
        SHORT + negative funding = paying = reduce size  
        SHORT + positive funding = receiving = can increase
        """
        bucket = cls.get_funding_bucket(funding_rate)
        
        # Determine if funding is favorable or against
        if bias == 'LONG':
            if funding_rate > 0.01:  # Paying funding
                if bucket in ['extreme_positive', 'high_positive']:
                    return 0.70  # Significant reduction
                return 0.85  # Moderate reduction
            elif funding_rate < -0.01:  # Receiving funding
                return 1.10  # Slight boost
        else:  # SHORT
            if funding_rate < -0.01:  # Paying funding
                if bucket in ['extreme_negative', 'high_negative']:
                    return 0.70
                return 0.85
            elif funding_rate > 0.01:  # Receiving funding
                return 1.10
        
        return 1.0  # Neutral


# ============================================================================
# FIX #7: TRAIN-TEST SPLIT VALIDATION
# ============================================================================
class BacktestValidation:
    ENABLED = True
    TRAIN_SPLIT_PCT = 0.70           # 70% training, 30% test
    MAX_OVERFITTING_PCT = 10.0       # Alert if overfitting > 10%
    MIN_TEST_DAYS = 7                # Minimum test period


# ============================================================================
# FIX #8: LIQUIDEZ BARRIDA (SWEEP) FILTER
# ============================================================================
class SweepFilter:
    ENABLED = True
    MAX_SWEEP_DEPTH_ATR = 0.5        # Don't exit if wick < 50% ATR below SL
    RECOVERY_CHECK_ENABLED = True    # Verify close is above SL after sweep


# ============================================================================
# FIX #9: SESSION QUALITY FILTER
# ============================================================================
class SessionQuality:
    # AUDITORÍA (Jan 9, 2026): PERMANENTLY DISABLED for 24/7 BTC trading
    # BTC trades 24/7 with institutional activity around the clock
    # Session-based filtering is designed for equity markets, NOT crypto
    ENABLED = False  # Crypto markets don't follow traditional session patterns
    # Allow weekend trading - BTC is 24/7
    WEEKEND_TRADING = True
    
    MIN_ADX_BY_SESSION = {
        'london': 20,                 # 8am-4pm UTC
        'newyork': 18,               # 2pm-11pm UTC
        'asia': 15,                  # Relaxed, accept noise
        'weekend': 0                 # Disabled
    }
    
    MIN_VOLATILITY_BPS = {
        'london': 80,                # Min 80 basis points
        'newyork': 60,
        'asia': 40,
        'weekend': 0
    }


# ============================================================================
# FIX #10: HTF DIVERGENCE CHECK
# ============================================================================
class DivergenceFilters:
    ENABLED = True
    LOOKBACK_PERIODS = 20            # Check divergence over last 20 candles
    RSI_PERIOD = 14


# ============================================================================
# FIX #11: VPOC (VOLUME POINT OF CONTROL)
# ============================================================================
class VPOCFilters:
    ENABLED = True
    VPOC_LOOKBACK = 20               # Calculate VPOC from last 20 1H candles
    PRICE_BIN_SIZE = 100             # BTC: 100 USD per bin


# ============================================================================
# FIX #12: BTC CORRELATION CHECK
# ============================================================================
class CorrelationFilters:
    # ENABLED: Now using BTCCorrelationFilter in filter_pipeline (Jan 15, 2026)
    ENABLED = True  # Enable correlation checking
    BTC_TREND_ENABLED = True         # Check BTC trend before LONG
    BTC_TIMEFRAME = '1h'
    BTC_EMA_PERIOD = 20
    BTC_CORRELATION_MODE = 'PENALTY' # STRICT, PENALTY, or CONTEXT_ONLY


# ============================================================================
# FIX #13: TIME-BASED EXIT - REDESIGNED (Jan 4, 2026)
# ============================================================================
class TimeBasedExits:
    """TIME EXIT redesign: Exit on stagnation, NOT on clock.
    
    Old logic: Exit after X minutes = bad (cuts winners)
    New logic: Exit if trade stagnates (no progress + vol death)
    
    A trade should exit by TIME only if:
    1. No progress toward TP (< 0.35R move in 60 min)
    2. ATR is contracting (volatility dying)
    3. Momentum has died (price stuck in range)
    """
    ENABLED = True
    
    # Max hold ONLY as absolute safety net (not primary exit)
    MAX_HOLD_MINUTES = 180          # Was 120 - give more room for winners
    
    # Overnight handling (keep but less aggressive)
    NIGHT_EXIT_HOUR = 22            # Was 20 - later exit (10pm UTC)
    AVOID_OVERNIGHT = False         # DISABLED - BTC trades 24/7
    
    # NEW: Stagnation-based exit (primary logic)
    STAGNATION_EXIT_ENABLED = True
    MIN_PROGRESS_R = 0.35           # Must make 0.35R progress
    STAGNATION_CHECK_MINUTES = 60   # Check progress after 60 min
    REQUIRE_ATR_CONTRACTION = True  # Only exit if vol dying
    ATR_CONTRACTION_RATIO = 0.7     # ATR < 70% of entry ATR
    REQUIRE_MOMENTUM_LOSS = True    # Only exit if momentum dead
    MOMENTUM_LOSS_THRESHOLD = 0.2   # Price velocity < 0.2 ATR/candle


# ============================================================================
# LOGGING & MONITORING
# ============================================================================
class Monitoring:
    LOG_LEVEL = 'INFO'
    METRICS_PORT = 8000            # Prometheus metrics port
    
    # Performance tracking
    TRACK_SLIPPAGE = True
    TRACK_LATENCY = True


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
def get_risk_for_session(hour: int, minute: int) -> float:
    """
    Get risk percentage based on current session
    Returns risk as decimal (e.g., 0.01 for 1%)
    """
    # NY Open volatile period
    if (hour == 9 and minute >= 30 and minute <= 45):
        return RiskManagement.RISK_NY_OPEN_PCT / 100.0
    
    # London/NY sessions
    if ((3 <= hour < 11) or (9 <= hour < 16)):
        return RiskManagement.RISK_LONDON_NY_PCT / 100.0
    
    # Asia/Weekend
    return RiskManagement.RISK_ASIA_WEEKEND_PCT / 100.0


def get_session_name(hour: int, minute: int, day_of_week: int = None) -> str:
    """
    Get the name of the current trading session.
    
    Args:
        hour: Hour in UTC (0-23)
        minute: Minute (0-59)
        day_of_week: Day of week (0=Monday, 6=Sunday). If None, won't check weekend.
    
    Returns: Session name matching SESSION_SIZE_MULTIPLIERS keys
    """
    # Check weekend first (if day_of_week provided)
    if day_of_week is not None:
        # Weekend: Friday 21:00 UTC to Sunday 21:00 UTC (approx CME close)
        if day_of_week == 4 and hour >= 21:  # Friday evening
            return "WEEKEND"
        elif day_of_week in (5, 6):  # Saturday or Sunday
            return "WEEKEND"
    
    # NY volatile open window
    if hour == 14 and 30 <= minute <= 45:  # 9:30-9:45 EST = 14:30-14:45 UTC
        return "NY_OPEN_VOLATILE"
    
    # London/NY overlap: 13:30-16:00 UTC (8:30 AM - 11:00 AM EST)
    # This is the best liquidity window
    if 13 <= hour < 16 or (hour == 13 and minute >= 30):
        return "LONDON_NY_OVERLAP"
    
    # London session: 8:00-16:00 UTC
    if 8 <= hour < 16:
        return "LONDON"
    
    # NY session: 13:30-21:00 UTC (8:30 AM - 4:00 PM EST)
    if (hour == 13 and minute >= 30) or (14 <= hour < 21):
        return "NY"
    
    # Asia session: 23:00-8:00 UTC
    if hour >= 23 or hour < 8:
        return "ASIA"
    
    # Dead hours: 21:00-23:00 UTC (after NY close, before Asia)
    if 21 <= hour < 23:
        return "DEAD_HOURS"
    
    return "OTHER"


# ============================================================================
# VOLATILITY REGIME CLASSIFICATION
# ============================================================================
class VolatilityRegime:
    """
    Classify volatility into buckets for adaptive behavior.
    Based on ATR z-score: (current_ATR - SMA_ATR) / STD_ATR
    Or simpler: ATR / SMA(ATR, 100)
    """
    # Regime thresholds (ATR ratio: current_atr / sma_atr_100)
    NORMAL_MIN: float = 0.8
    NORMAL_MAX: float = 1.2
    HIGH_VOL_MIN: float = 1.2
    HIGH_VOL_MAX: float = 1.8
    SPIKE_THRESHOLD: float = 1.8
    
    # Regime names
    LOW = "LOW_VOL"
    NORMAL = "NORMAL"
    HIGH = "HIGH_VOL"
    SPIKE = "SPIKE"
    
    # =========================================================================
    # REGIME-SPECIFIC ADJUSTMENTS
    # =========================================================================
    
    # Trailing stop ATR multiplier by regime
    TRAILING_ATR_MULT = {
        "LOW_VOL": 1.5,      # Tighter trailing in low vol
        "NORMAL": 2.0,       # Standard
        "HIGH_VOL": 2.5,     # Wider trailing in high vol
        "SPIKE": 3.0,        # Much wider in spikes
    }
    
    # Minimum SL distance (ATR units) by regime
    MIN_SL_ATR = {
        "LOW_VOL": 0.8,
        "NORMAL": 1.0,
        "HIGH_VOL": 1.5,
        "SPIKE": 2.0,
    }
    
    # Slippage buffer multiplier by regime
    SLIPPAGE_MULT = {
        "LOW_VOL": 0.8,
        "NORMAL": 1.0,
        "HIGH_VOL": 1.5,
        "SPIKE": 2.5,
    }
    
    # Cooldown multiplier after exit by regime
    COOLDOWN_MULT = {
        "LOW_VOL": 0.8,
        "NORMAL": 1.0,
        "HIGH_VOL": 1.5,
        "SPIKE": 2.0,
    }
    
    @classmethod
    def classify(cls, atr_ratio: float) -> str:
        """
        Classify volatility regime from ATR ratio.
        
        Args:
            atr_ratio: current_atr / sma(atr, 100)
        
        Returns:
            Regime name: LOW_VOL, NORMAL, HIGH_VOL, or SPIKE
        """
        if atr_ratio >= cls.SPIKE_THRESHOLD:
            return cls.SPIKE
        elif atr_ratio >= cls.HIGH_VOL_MIN:
            return cls.HIGH
        elif atr_ratio >= cls.NORMAL_MIN:
            return cls.NORMAL
        else:
            return cls.LOW
    
    @classmethod
    def get_trailing_mult(cls, regime: str) -> float:
        return cls.TRAILING_ATR_MULT.get(regime, 2.0)
    
    @classmethod
    def get_min_sl_atr(cls, regime: str) -> float:
        return cls.MIN_SL_ATR.get(regime, 1.0)
    
    @classmethod
    def get_slippage_mult(cls, regime: str) -> float:
        return cls.SLIPPAGE_MULT.get(regime, 1.0)
    
    @classmethod
    def get_cooldown_mult(cls, regime: str) -> float:
        return cls.COOLDOWN_MULT.get(regime, 1.0)
    
    @classmethod
    def from_atr_ratio(cls, atr_ratio: float) -> 'VolatilityRegimeValue':
        """
        Get VolatilityRegimeValue from ATR ratio.
        This is an alias for classify() that returns an object with a .name attribute.
        """
        regime_name = cls.classify(atr_ratio)
        return VolatilityRegimeValue(regime_name)


class VolatilityRegimeValue:
    """
    Simple wrapper to provide .name attribute for volatility regime.
    Makes it behave like an enum value.
    """
    def __init__(self, name: str):
        self._name = name
    
    @property
    def name(self) -> str:
        return self._name
    
    def __str__(self) -> str:
        return self._name
    
    def __eq__(self, other) -> bool:
        if isinstance(other, VolatilityRegimeValue):
            return self._name == other._name
        if isinstance(other, str):
            return self._name == other
        return False


# ============================================================================
# DYNAMIC SCORE THRESHOLDS
# ============================================================================
class DynamicScoreThreshold:
    """
    Score threshold varies by:
    1. Market regime (RANGE/TREND/EXPANSION)
    2. Trading session (ASIA/LONDON/NY)
    
    This prevents low-quality trades from slipping through.
    """
    # Base threshold by regime
    REGIME_THRESHOLD = {
        "RANGE": 6.5,        # Stricter in range (need clean setup)
        "TREND": 6.0,        # More permissive in trend (momentum helps)
        "TREND_EXPANSION": 6.5,  # Stricter in expansion (more noise/slip)
        "TREND_EXHAUSTION": 7.0,  # Very strict near exhaustion
        "CONSOLIDATION": 6.5,
    }
    
    # Session adjustment (added to base threshold)
    SESSION_ADJUSTMENT = {
        "ASIA": 0.5,           # +0.5 in Asia (lower quality)
        "LONDON": 0.0,         # No adjustment
        "NY": 0.0,             # No adjustment
        "LONDON_NY_OVERLAP": -0.5,  # -0.5 in best session (can be looser)
        "NY_OPEN_VOLATILE": 0.5,    # +0.5 in volatile open
        "DEAD_HOURS": 1.0,     # +1.0 in dead hours (rarely trade)
        "WEEKEND": 0.5,        # +0.5 on weekends
        "OTHER": 0.0,
    }
    
    # Fallback for discovery mode
    DISCOVERY_BASE = 5.0
    
    @classmethod
    def get_threshold(cls, regime: str, session: str, is_discovery: bool = False) -> float:
        """
        Calculate dynamic score threshold.
        
        Args:
            regime: Market regime name
            session: Trading session name
            is_discovery: Whether in discovery mode
        
        Returns:
            Score threshold (typically 5.0-8.0)
        """
        if is_discovery:
            base = cls.DISCOVERY_BASE
        else:
            base = cls.REGIME_THRESHOLD.get(regime, 6.5)
        
        adjustment = cls.SESSION_ADJUSTMENT.get(session.upper(), 0.0)
        
        threshold = base + adjustment
        
        # Clamp to reasonable range
        return max(5.0, min(8.0, threshold))


# ============================================================================
# SESSION TRADE LIMITS
# ============================================================================
class SessionTradeLimits:
    """
    Limit trades per session to prevent overtrading.
    High-score setups can override.
    """
    ENABLED = True
    
    # Max trades per session
    MAX_TRADES_PER_SESSION = {
        "ASIA": 2,
        "LONDON": 3,
        "NY": 3,
        "LONDON_NY_OVERLAP": 4,
        "NY_OPEN_VOLATILE": 1,  # Just 1 in first 15 min
        "DEAD_HOURS": 1,
        "WEEKEND": 2,
        "OTHER": 2,
    }
    
    # Override threshold: if score >= this, allow trade even if at limit
    OVERRIDE_SCORE_THRESHOLD = 7.5
    
    # Max trades per day (across all sessions)
    MAX_TRADES_PER_DAY = 8
    
    @classmethod
    def get_limit(cls, session: str) -> int:
        return cls.MAX_TRADES_PER_SESSION.get(session.upper(), 2)
    
    @classmethod
    def can_override(cls, score: float) -> bool:
        return score >= cls.OVERRIDE_SCORE_THRESHOLD


# ============================================================================
# TP1 PERCENTAGE BY REGIME
# ============================================================================
class TP1PercentageConfig:
    """
    TP1 close percentage varies by regime.
    RANGE = higher close % (take profits)
    TREND = lower close % (let runners run)
    """
    TP1_PCT_BY_REGIME = {
        "RANGE": 60,           # 60% close at TP1
        "CONSOLIDATION": 60,
        "TREND": 40,           # 40% close, let 60% run
        "TREND_EXPANSION": 35, # 35% close, more runner
        "TREND_EXHAUSTION": 50, # Take more profit near exhaustion
    }
    
    DEFAULT_TP1_PCT = 50
    
    @classmethod
    def get_tp1_pct(cls, regime: str) -> int:
        return cls.TP1_PCT_BY_REGIME.get(regime, cls.DEFAULT_TP1_PCT)


# ============================================================================
# LOSS STREAK CONFIGURATION
# ============================================================================
class LossStreakConfig:
    """
    Only count REAL losses for loss streak sizing.
    TIME_EXIT and other non-SL exits don't indicate edge failure.
    """
    # Exit types that COUNT as loss streak
    LOSS_STREAK_EXIT_TYPES = {
        "SL_HIT": 1.0,           # Full count
        "TRAILING_STOP_LOSS": 0.5,  # Half count (was profit, lost it)
        "EMERGENCY_CLOSE": 0.5,   # Half count
    }
    
    # Exit types that DON'T count (or count less)
    NON_LOSS_EXIT_TYPES = {
        "TIME_EXIT": 0.0,         # No count (market was dead)
        "TIME_INVALIDATION": 0.0,
        "TP1_PARTIAL": 0.0,       # Profit!
        "TP2_FULL": 0.0,
        "TRAILING_STOP_PROFIT": 0.0,  # Profit exit
        "MANUAL_CLOSE": 0.25,     # Small count
        "SESSION_END": 0.0,
    }
    
    @classmethod
    def get_loss_weight(cls, exit_type: str, pnl_r: float) -> float:
        """
        Get weight for loss streak calculation.
        
        Args:
            exit_type: Type of exit
            pnl_r: Realized P&L in R
        
        Returns:
            Weight 0.0-1.0 to add to loss streak
        """
        # If profitable, never count as loss
        if pnl_r > 0:
            return 0.0
        
        # Check loss types
        if exit_type in cls.LOSS_STREAK_EXIT_TYPES:
            return cls.LOSS_STREAK_EXIT_TYPES[exit_type]
        
        # Check non-loss types
        if exit_type in cls.NON_LOSS_EXIT_TYPES:
            return cls.NON_LOSS_EXIT_TYPES[exit_type]
        
        # Unknown exit with loss = count as full
        if pnl_r < -0.5:  # Significant loss
            return 1.0
        
        return 0.5  # Default half count


# ============================================================================
# RSI DIVERGENCE EXIT CONFIG
# ============================================================================
class DivergenceExitConfig:
    """
    Exit on RSI divergence to capture profits before reversal.
    Only applies when trade is already profitable.
    """
    ENABLED = True
    
    # Minimum MFE before divergence exit triggers
    MIN_MFE_R = 1.0  # Need at least 1R profit
    
    # Timeframes to check
    CHECK_HTF = True   # 1H divergence
    CHECK_MTF = True   # 5m divergence
    
    # Lookback for divergence detection
    LOOKBACK_CANDLES = 10
    
    # Actions on divergence
    PARTIAL_CLOSE_PCT = 25  # Close 25% additional on divergence
    TIGHTEN_STOP_TO_R = 0.2  # Move SL to entry + 0.2R
    
    # Don't exit on divergence if we're losing
    ONLY_IN_PROFIT = True


# ============================================================================
# LOCATION FILTER BREAKOUT OVERRIDE
# ============================================================================
class LocationFilterOverride:
    """
    Override location filter's high-percentile block in breakout conditions.
    Prevents missing valid trend continuation entries.
    """
    ENABLED = True
    
    # Conditions required for override (must meet ALL)
    # 1. HTF trend is strong
    MIN_HTF_ADX = 25
    REQUIRE_EMA_ALIGNED = True  # EMA50 > EMA200 (LONG) or vice versa
    
    # 2. Breakout confirmed
    REQUIRE_BOS = True  # Break of structure
    
    # 3. Volatility expanding
    CHECK_BB_EXPANSION = True
    MIN_BB_WIDTH_RATIO = 1.2  # BB width > 120% of average
    
    # Override action
    CONVERT_BLOCK_TO_PENALTY = True  # Instead of HARD BLOCK, apply penalty
    OVERRIDE_PENALTY_POINTS = 1  # Score penalty when overriding


# ============================================================================
# MICRO TREND FILTER ADX CONDITIONING
# ============================================================================
class MicroTrendADXConfig:
    """
    Make MicroTrend filter behavior conditional on HTF ADX.
    Strong trends = penalty only (pullbacks are buying opportunities)
    Weak trends = hard block (micro trend is more meaningful)
    """
    ENABLED = True
    
    # ADX thresholds
    STRONG_TREND_ADX = 30   # ADX >= 30 = strong trend
    WEAK_TREND_ADX = 20     # ADX <= 20 = weak/ranging
    
    # Behavior in strong trend (ADX >= 30)
    STRONG_TREND_ACTION = "PENALTY"  # Just penalize, don't block
    STRONG_TREND_PENALTY = 1         # -1 score
    
    # Behavior in weak trend (ADX <= 20)
    WEAK_TREND_ACTION = "BLOCK"      # Hard block
    WEAK_TREND_PENALTY = 3           # -3 score if not blocking
    
    # Behavior in between (20 < ADX < 30)
    MID_TREND_ACTION = "PENALTY"
    MID_TREND_PENALTY = 2


# ============================================================================
# EXCHANGE SL UPDATE CONFIG
# ============================================================================
class ExchangeSLConfig:
    """
    Configuration for updating SL on exchange after trailing.
    """
    ENABLED = True
    
    # Minimum movement before updating SL on exchange
    # Prevents spamming exchange with tiny updates
    MIN_MOVE_R = 0.3        # At least 0.3R move
    MIN_MOVE_SPREAD_MULT = 0.5  # At least 0.5 * spread
    MIN_MOVE_ATR_MULT = 0.2  # At least 0.2 * ATR
    
    # Use the MAX of all three thresholds
    
    # Watchdog: on restart, sync local SL with exchange SL
    WATCHDOG_SYNC = True
    
    # Always favor the BETTER (tighter) SL
    FAVOR_TIGHTER_SL = True


# ============================================================================
# PARTIAL CLOSE VALIDATION CONFIG
# ============================================================================
class PartialCloseValidation:
    """
    Validate position size after partial close.
    """
    ENABLED = True
    
    # Tolerance for size mismatch
    SIZE_EPSILON = 1e-6  # BTC precision
    SIZE_TOLERANCE_PCT = 0.5  # 0.5% tolerance
    
    # Max retries for sync
    MAX_SYNC_RETRIES = 3
    SYNC_DELAY_MS = 500
    
    # Action on mismatch
    RECALCULATE_LEVELS = True  # Recalculate TP2/trailing if mismatch
