"""
Configuration parameters for BTC Perpetual Trading System
All strategy parameters and thresholds
"""

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
    # RETUNED (Jan 4, 2026): Relaxed to allow intraday frequency
    # Previous MIN_ADX=15 was still blocking weak-but-valid trends
    MIN_ADX = 12                    # Was 15 - allow weak bias (intraday doesn't need strong HTF)
    MIN_ADX_SLOPE = -0.8            # Was -0.5 - allow deeper pullbacks
    MIN_ATR_VS_MA = 0.4             # Was 0.5 - allow low vol compression
    
    # IMPROVED: Smart slope filtering
    # Block ONLY when ADX is weak AND declining (true trend death)
    # High ADX + negative slope = consolidation within trend = OK for intraday
    SMART_SLOPE_FILTER = True       # Use smart slope logic
    WEAK_ADX_THRESHOLD = 20         # Was 25 - more lenient
    STRONG_SLOPE_DECLINE = -0.15    # Was -0.10 - allow more slope decline
    
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
    
    # Take profits - Based on empirical MFE analysis
    # Data shows: avg MFE = 0.52R, so TP1 at 0.6R targets upper MFE range
    # This captures wins while letting more trades hit target
    TP1_RISK_MULTIPLE = 0.6         # TP at 0.6R (slightly above avg MFE)
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
    MARKET_ORDER_TIMEOUT_SECONDS = 0.5  # Wait only 500ms for market fills (must be instant)


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
    # FIX (Jan 3, 2026): DISABLED temporarily for diagnosis - was blocking too many signals
    ENABLED = False  # TODO: Re-enable after confirming bot generates trades
    MAX_FUNDING_RATE_PCT = 0.03      # Do not trade LONG if > 0.03% (3 bps)
    FORCE_EXIT_FUNDING_PCT = 0.05    # Force exit if funding reaches 0.05%
    CHECK_INTERVAL_MINUTES = 60      # Check funding rate every hour


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
    # FIX (Jan 3, 2026): DISABLED temporarily for diagnosis - was blocking 50%+ of signals
    ENABLED = False  # TODO: Re-enable after confirming bot generates trades
    # FIX (Jan 3, 2026): ENABLED weekend trading for diagnosis
    WEEKEND_TRADING = True            # Allow weekend trading (reduced risk)
    
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


def get_session_name(hour: int, minute: int) -> str:
    """Get the name of the current trading session"""
    if hour == 9 and 30 <= minute <= 45:
        return "NY_OPEN_VOLATILE"
    elif 3 <= hour < 11:
        return "LONDON"
    elif 9 <= hour < 16:
        return "NY"
    elif 18 <= hour or hour < 3:
        return "ASIA"
    else:
        return "OTHER"
