"""
Configuration parameters for BTC Perpetual Trading System
All strategy parameters and thresholds
"""

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
    
    # ABSOLUTE MINIMUM - never trade below this regardless of mode
    RR_HARD_FLOOR = 0.8             # Mathematical minimum - below this = guaranteed loss
    
    # =========================================================================
    # RR PENALTY IN SCORING - Penalize low RR setups BEFORE score evaluation
    # This ensures RR < 1.0 trades are mathematically harder to pass
    # =========================================================================
    RR_PENALTY_THRESHOLD = 1.0      # Below this RR, apply score penalty
    RR_PENALTY_POINTS = 2           # Subtract 2 points from score if RR < 1.0
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
# HTF FILTERS (1H)
# ============================================================================
class HTFFilters:
    # RETUNED (Jan 9, 2026): ADX slope NO LONGER BLOCKS
    # ADX solo define régimen, no timing
    # Slope negativo en pullback es NORMAL, no patológico
    MIN_ADX = 12                    # Minimum ADX for trend regime
    MIN_ADX_SLOPE = -999            # DEPRECATED: slope never blocks
    MIN_ATR_VS_MA = 0.4             # Allow low vol compression
    
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
    TP1_RISK_MULTIPLE = 1.0         # TP at 1.0R (minimum for positive expectancy)
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
    # FIX (Jan 3, 2026): DISABLED temporarily for diagnosis
    ENABLED = False  # TODO: Re-enable after confirming bot generates trades
    BTC_TREND_ENABLED = False        # Check BTC trend before LONG
    BTC_TIMEFRAME = '1h'
    BTC_EMA_PERIOD = 20


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
