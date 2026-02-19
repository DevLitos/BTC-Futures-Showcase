"""
Configuration parameters for BTC Perpetual Trading System
All strategy parameters and thresholds
"""

from enum import Enum
from typing import Literal
from datetime import datetime, UTC


# ============================================================================
# ADX THRESHOLDS (Feb 04, 2026 - AUDIT FIX)
# SINGLE SOURCE OF TRUTH for all ADX-based decisions
# ============================================================================
class ADXThresholds:
    """
    Centralized ADX thresholds used throughout the system.
    
    USAGE:
    - Import ADXThresholds and use class attributes instead of hardcoded values
    - This prevents inconsistencies (18 vs 20 vs 25 scattered across files)
    
    REGIME CLASSIFICATION:
    - RANGE: ADX < RANGE (18) → Choppy, mean-reversion trades only
    - TRANSITION: ADX 18-20 → Cautious, reduced size
    - TREND: ADX > TREND (20) → Trend-following strategies active
    - STRONG_TREND: ADX > STRONG (25) → Full conviction, can hold runners
    
    PREVIOUSLY SCATTERED ACROSS:
    - trading_config.py: 18, 20
    - edge_state_manager.py: 25
    - entry_orchestrator.py: 20, 25
    - simplified_sizing.py: 18
    - trading_app.py: 18, 20, 25
    """
    
    # Core thresholds
    RANGE = 18              # Below this = RANGE/CHOP mode
    TRANSITION = 20         # Between RANGE and TREND
    TREND = 20              # Above this = TREND mode
    STRONG_TREND = 25       # Above this = STRONG TREND (runners OK)
    
    # Edge-specific thresholds
    INTRADAY_EDGE_MIN = 25  # Minimum for intraday edge confirmation
    DISPLACEMENT_EDGE = 20  # For displacement-based edges
    
    # 🔥 NEW (Feb 04): RANGE edge more flexible
    # RANGE edge can activate when ADX < 25 (not just < 18)
    # This allows mean-reversion trades in "soft trend" conditions
    RANGE_EDGE_MAX = 25     # RANGE edge allowed when ADX < 25
    
    # Defaults for missing values
    DEFAULT = 20.0          # Default ADX when unavailable
    
    @classmethod
    def is_ranging(cls, adx: float) -> bool:
        """Check if ADX indicates ranging market."""
        return adx < cls.RANGE
    
    @classmethod
    def is_trending(cls, adx: float) -> bool:
        """Check if ADX indicates trending market."""
        return adx >= cls.TREND
    
    @classmethod
    def is_strong_trend(cls, adx: float) -> bool:
        """Check if ADX indicates strong trend."""
        return adx >= cls.STRONG_TREND
    
    @classmethod
    def get_regime(cls, adx: float) -> str:
        """Get regime name based on ADX value."""
        if adx < cls.RANGE:
            return 'RANGE'
        elif adx < cls.TREND:
            return 'TRANSITION'
        elif adx < cls.STRONG_TREND:
            return 'TREND'
        else:
            return 'STRONG_TREND'
    
    @classmethod
    def get_size_mult(cls, adx: float) -> float:
        """Get sizing multiplier based on ADX regime."""
        if adx < cls.RANGE:
            return 0.5  # Half size in range
        elif adx < cls.TREND:
            return 0.7  # 70% in transition
        else:
            return 1.0  # Full size in trend


# ============================================================================
# SCORE ADJUSTMENTS (Feb 04, 2026) - AUDIT FIX
# Centralizes ALL score adjustments dispersed across multiple files
# 
# BEFORE: Penalties defined in 3+ places with inconsistent values
# NOW: Single source of truth for all score modifications
# ============================================================================
class ScoreAdjustments:
    """
    Centralized score adjustment values and methods.
    
    Replaces scattered adjustments in:
    - entry_orchestrator.py (SOFT_MODE_SCORE_PENALTY)
    - entry_scorer.py (micro_trend_adj, filter_pipeline_adj, etc.)
    - trading_config.py (DailyRegimeDetector.score_adjust)
    - BiasStrengthConfig (SCORE_PENALTY_*)
    
    All adjustments are negative for penalties, positive for bonuses.
    """
    
    # =========================================================================
    # REGIME-BASED ADJUSTMENTS (from DailyRegimeDetector)
    # =========================================================================
    TREND_DAY_ADJUST = 0.0       # Normal threshold
    RANGE_DAY_ADJUST = +1.0      # Stricter (need higher score in range)
    TRANSITION_DAY_ADJUST = +0.5 # Slightly stricter
    
    # =========================================================================
    # MODE-BASED PENALTIES (from entry_orchestrator)
    # =========================================================================
    SOFT_MODE_PENALTY = -1.5     # When in SOFT mode (borderline entries)
    DISCOVERY_MODE_PENALTY = -0.5  # When in discovery mode
    
    # =========================================================================
    # MICRO-TREND ADJUSTMENTS (from trading_app/micro_trend_filter)
    # =========================================================================
    MICRO_TREND_ALIGNED_BONUS = +0.5   # When micro-trend aligns with bias
    MICRO_TREND_OPPOSE_PENALTY = -1.0  # When micro-trend opposes bias
    MICRO_TREND_RANGE_CAP = 0.5        # Max adjustment in range markets
    
    # =========================================================================
    # MOMENTUM PENALTIES (from BiasStrengthConfig)
    # =========================================================================
    BULLISH_CONTEXT_MILD = -0.15      # 1-2 bullish signals when shorting
    BULLISH_CONTEXT_MODERATE = -0.25  # 3 bullish signals
    BULLISH_CONTEXT_SEVERE = -0.5     # 4+ bullish signals
    
    # =========================================================================
    # FILTER PIPELINE ADJUSTMENTS (from filter decisions)
    # =========================================================================
    FILTER_BLOCK_PENALTY = -2.0    # Hard block from filters
    FILTER_WARNING_PENALTY = -0.5  # Warning from filters
    
    # =========================================================================
    # LATE CONFIRMATION PENALTIES
    # =========================================================================
    LATE_CONFIRMATION_MILD = -0.3     # Slightly late entry
    LATE_CONFIRMATION_SEVERE = -0.75  # Very late entry
    
    # =========================================================================
    # VOLUME PENALTIES
    # =========================================================================
    LOW_VOLUME_PENALTY = -0.5     # rel_vol < 0.6
    
    # =========================================================================
    # ADX SLOPE PENALTIES
    # =========================================================================
    ADX_DECLINING_PENALTY = -0.25  # ADX slope < -0.5 (momentum dying)
    
    @classmethod
    def get_regime_adjustment(cls, regime: str) -> float:
        """Get score adjustment for daily regime."""
        adjustments = {
            'TREND_DAY': cls.TREND_DAY_ADJUST,
            'RANGE_DAY': cls.RANGE_DAY_ADJUST,
            'TRANSITION_DAY': cls.TRANSITION_DAY_ADJUST,
        }
        return adjustments.get(regime, 0.0)
    
    @classmethod
    def get_bullish_context_penalty(cls, bullish_signals: int) -> float:
        """Get penalty for shorting in bullish context."""
        if bullish_signals <= 1:
            return 0.0
        elif bullish_signals == 2:
            return cls.BULLISH_CONTEXT_MILD
        elif bullish_signals == 3:
            return cls.BULLISH_CONTEXT_MODERATE
        else:
            return cls.BULLISH_CONTEXT_SEVERE
    
    @classmethod
    def calculate_total_adjustment(
        cls,
        regime: str = 'TREND_DAY',
        is_soft_mode: bool = False,
        is_discovery: bool = False,
        micro_trend_aligned: bool = False,
        micro_trend_opposed: bool = False,
        is_range: bool = False,
        bullish_signals: int = 0,
        filter_warning: bool = False,
        is_late: bool = False,
        is_very_late: bool = False,
        low_volume: bool = False,
        adx_declining: bool = False
    ) -> float:
        """
        Calculate total score adjustment from all factors.
        
        Returns total adjustment (can be positive or negative).
        """
        total = 0.0
        
        # Regime adjustment
        total += cls.get_regime_adjustment(regime)
        
        # Mode penalties
        if is_soft_mode:
            total += cls.SOFT_MODE_PENALTY
        if is_discovery:
            total += cls.DISCOVERY_MODE_PENALTY
        
        # Micro-trend adjustments
        if micro_trend_aligned:
            adj = cls.MICRO_TREND_ALIGNED_BONUS
            if is_range:
                adj = min(cls.MICRO_TREND_RANGE_CAP, adj)
            total += adj
        elif micro_trend_opposed:
            adj = cls.MICRO_TREND_OPPOSE_PENALTY
            if is_range:
                adj = max(-cls.MICRO_TREND_RANGE_CAP, adj)
            total += adj
        
        # Bullish context penalty (when shorting)
        total += cls.get_bullish_context_penalty(bullish_signals)
        
        # Filter warnings
        if filter_warning:
            total += cls.FILTER_WARNING_PENALTY
        
        # Late confirmation
        if is_very_late:
            total += cls.LATE_CONFIRMATION_SEVERE
        elif is_late:
            total += cls.LATE_CONFIRMATION_MILD
        
        # Volume penalty
        if low_volume:
            total += cls.LOW_VOLUME_PENALTY
        
        # ADX declining
        if adx_declining:
            total += cls.ADX_DECLINING_PENALTY
        
        return total


# ============================================================================
# DAILY REGIME DETECTOR (Jan 23, 2026) - IMPRESCINDIBLE
# Determina si hoy es "día de tendencia" o "día de rango" al inicio
# Esto cambia TODO el comportamiento del bot para el día
# ============================================================================
class DailyRegimeDetector:
    """
    Detector de régimen DIARIO - ejecutar una vez al inicio del día/sesión.
    
    🔥 CRÍTICO: El régimen diario debe sobreescribir comportamiento intradiario.
    
    TREND DAY (día de tendencia):
    - Range diario > 1.5x ATR promedio
    - ADX diario > 20
    - Precio se aleja de VWAP sin volver
    - Comportamiento: Trend-following, runners, stops amplios
    
    RANGE DAY (día de rango):
    - Range diario < 1.0x ATR promedio
    - ADX diario < 18
    - Precio orbita VWAP
    - Comportamiento: Mean-reversion, scalps, stops tight
    
    TRANSITION DAY:
    - Entre RANGE y TREND
    - Cautela, size reducido
    """
    
    # Classification thresholds
    RANGE_ATR_THRESHOLD = 1.0       # Daily range < 1.0x ATR = RANGE DAY
    TREND_ATR_THRESHOLD = 1.5       # Daily range > 1.5x ATR = TREND DAY
    # 🔄 REFACTOR Feb 04: Use centralized ADXThresholds
    ADX_RANGE_THRESHOLD = ADXThresholds.RANGE   # 18 - Daily ADX < 18 = RANGE DAY
    ADX_TREND_THRESHOLD = ADXThresholds.TREND   # 20 - Daily ADX > 20 = TREND DAY
    
    # Behavior modifications per daily regime
    DAILY_REGIME_CONFIG = {
        'TREND_DAY': {
            'allow_runners': True,
            'sl_atr_mult': 3.0,         # Wider stops
            'tp_r_target': 2.0,         # Higher targets
            'max_trades': 4,            # More trades allowed
            'size_mult': 1.0,           # Full size
            'score_adjust': 0,          # Normal threshold
        },
        'RANGE_DAY': {
            'allow_runners': False,     # Quick exits
            'sl_atr_mult': 2.0,         # Tight stops
            'tp_r_target': 1.0,         # Quick targets
            'max_trades': 3,            # Limited trades
            'size_mult': 0.5,           # Half size
            'score_adjust': +1,         # Stricter threshold
        },
        'TRANSITION_DAY': {
            'allow_runners': True,
            'sl_atr_mult': 2.5,
            'tp_r_target': 1.5,
            'max_trades': 3,
            'size_mult': 0.7,
            'score_adjust': +0.5,
        }
    }
    
    @classmethod
    def classify_daily_regime(cls, daily_range: float, daily_atr: float, 
                               daily_adx: float) -> str:
        """
        Classify daily regime at start of session.
        
        Args:
            daily_range: Today's high - low so far
            daily_atr: Average true range (daily)
            daily_adx: Daily ADX value
            
        Returns:
            'TREND_DAY', 'RANGE_DAY', or 'TRANSITION_DAY'
        """
        if daily_atr <= 0:
            return 'RANGE_DAY'  # Default conservative
        
        range_ratio = daily_range / daily_atr
        
        # Use both ATR ratio AND ADX for classification
        if range_ratio > cls.TREND_ATR_THRESHOLD and daily_adx > cls.ADX_TREND_THRESHOLD:
            return 'TREND_DAY'
        elif range_ratio < cls.RANGE_ATR_THRESHOLD or daily_adx < cls.ADX_RANGE_THRESHOLD:
            return 'RANGE_DAY'
        else:
            return 'TRANSITION_DAY'
    
    @classmethod
    def get_daily_config(cls, daily_regime: str) -> dict:
        """Get behavior config for daily regime."""
        return cls.DAILY_REGIME_CONFIG.get(daily_regime, cls.DAILY_REGIME_CONFIG['RANGE_DAY'])
    
    @classmethod
    def log_daily_regime(cls, daily_regime: str, daily_range: float, 
                         daily_atr: float, daily_adx: float) -> str:
        """Generate log message for daily regime classification."""
        config = cls.get_daily_config(daily_regime)
        return (
            f"📅 DAILY REGIME: {daily_regime}\\n"
            f"   Range/ATR: {daily_range/daily_atr:.2f}x\\n"
            f"   Daily ADX: {daily_adx:.1f}\\n"
            f"   Config: SL={config['sl_atr_mult']}xATR, TP={config['tp_r_target']}R, "
            f"Size={config['size_mult']*100:.0f}%"
        )


# ============================================================================
# 🎯 ASYMMETRY DEFINITION (Jan 23, 2026) - CRITICAL
# ============================================================================
# El usuario dijo: "Ahora estás en el peor punto intermedio: no tienes wins 
# pequeños, no tienes runners, y tus losses son variables."
#
# SOLUCIÓN: Elegimos UN CAMINO y lo aplicamos consistentemente.
# 
# CAMINO ELEGIDO: HIGH WINRATE + CONTROLLED LOSSES
# - Target pequeño (1.0R) para maximizar winrate
# - Pérdida máxima controlada (-0.35R absoluto máximo con TIME_INVALIDATION)
# - NO chase runners en RANGE days
# - SÍ chase runners SOLO en TREND days (daily regime = TREND_DAY)
# 
# REGLA DE ORO: Si un trade no va en profit en X minutos, EXIT.
# No dejar que mediocre se convierta en pérdida.
# ============================================================================
class AsymmetryConfig:
    """
    ESTRATEGIA DE ASIMETRÍA: HIGH WINRATE + CONTROLLED LOSSES
    
    Filosofía:
    - Wins pequeños pero FRECUENTES
    - Losses pequeños y CONTROLADOS (nunca > 0.35R)
    - TIME_INVALIDATION es TU AMIGO, no tu enemigo
    
    El edge viene de:
    - Alta tasa de acierto (>55%)
    - Pérdidas contenidas (promedio -0.20R, máximo -0.35R)
    - NO de runners (esos son bonus, no estrategia base)
    """
    
    # === TARGETS ===
    # Target conservador para maximizar winrate
    BASE_TP_R = 1.0                  # Target base = 1.0R
    RANGE_DAY_TP_R = 0.8             # En RANGE_DAY, TP más corto (scalp)
    TREND_DAY_TP_R = 1.5             # En TREND_DAY, dejamos correr un poco más
    
    # === STOPS - LA CLAVE DE LA ASIMETRÍA ===
    # STOP NUNCA debe ser > TP. Eso destruye expectancy.
    MAX_STOP_R = 1.0                 # Stop máximo = 1.0R (igual que TP)
    
    # TIME_INVALIDATION: EXIT RÁPIDO SI NO FUNCIONA
    # 🔥 FIX #121a (Feb 09, 2026): Give trades more room
    # OLD: -0.20R → Too aggressive, trades killed before developing
    # NEW: -0.35R → Standard pullback tolerance
    TIME_INVALIDATION_CANDLES = 15  # 15 velas 1m = 15 min
    TIME_INVALIDATION_EXIT_R = 0.0  # Target: salir en breakeven o mejor
    TIME_INVALIDATION_MAX_LOSS_R = -0.35  # 🔥 FIX #121a: -0.35R (was -0.20R) - give room!
    
    # === PÉRDIDA MÁXIMA ABSOLUTA ===
    # Si llegamos aquí, algo falló (slippage, gap, etc.)
    ABSOLUTE_MAX_LOSS_R = -0.40     # 🔥 FIX #121a: NUNCA perder más de -0.40R (was -0.25R)
    
    # === RUNNERS (SOLO EN TREND DAYS) ===
    ALLOW_RUNNERS = False           # Default OFF
    RUNNER_TRIGGER_MFE = 1.5        # Solo considera runner si MFE > 1.5R
    RUNNER_TRAIL_ATR = 2.0          # Trail a 2x ATR cuando tenemos runner
    
    # === VALIDACIÓN CONTINUA ===
    @classmethod
    def validate_trade_outcome(cls, exit_r: float, exit_reason: str) -> bool:
        """
        Validate that trade outcome respects asymmetry rules.
        
        Returns True if trade respected rules, False if violated.
        Used for AUDIT and detection of bugs.
        """
        if exit_r < cls.ABSOLUTE_MAX_LOSS_R:
            # VIOLATION: Lost more than allowed
            return False
        
        if exit_reason == 'TIME_INVALIDATION' and exit_r < cls.TIME_INVALIDATION_MAX_LOSS_R:
            # VIOLATION: TIME_INVALIDATION should exit at breakeven, not loss
            return False
        
        return True
    
    @classmethod
    def get_tp_for_regime(cls, daily_regime: str) -> float:
        """Get TP target based on daily regime."""
        if daily_regime == 'TREND_DAY':
            return cls.TREND_DAY_TP_R
        elif daily_regime == 'RANGE_DAY':
            return cls.RANGE_DAY_TP_R
        else:
            return cls.BASE_TP_R


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
# BIAS STRENGTH SYSTEM (Jan 22, 2026)
# Separates "direction permitted" from "direction preferred"
# ============================================================================
class BiasStrength(str, Enum):
    """
    MACRO BIAS (Régimen Estructural) - EMA50 vs EMA200
    
    This is the MACRO context - what the market IS, not what to trade.
    - BULL_STRONG: EMA50 > EMA200 with separation
    - BEAR_STRONG: EMA50 < EMA200 with separation
    - NEUTRAL: Low ADX or no clear separation
    """
    BULL_STRONG = "BULL_STRONG"
    BEAR_STRONG = "BEAR_STRONG"
    NEUTRAL = "NEUTRAL"


class WorkingBias(str, Enum):
    """
    🔥 FIX #76 (Feb 07, 2026): WORKING BIAS - Dirección Operativa
    
    Derived from macro_bias (BiasStrength), tells us what to PREFER trading.
    This is NOT a blocker, it's a PREFERENCE.
    
    - LONG_PREF: Prefer LONGs, allow SHORTs only with strong setup
    - SHORT_PREF: Prefer SHORTs, allow LONGs only with strong setup  
    - BOTH: Both directions equally valid (ranging market)
    
    HOW IT WORKS:
    - BEAR_STRONG macro → SHORT_PREF working bias
    - BULL_STRONG macro → LONG_PREF working bias
    - NEUTRAL macro → BOTH (trade what triggers)
    
    CRITICAL: WorkingBias NEVER BLOCKS, it only:
    1. Reduces size for counter-preference trades
    2. Increases RR requirement for counter-preference
    3. Gets overridden by STRONG EDGES (Displacement, BOS, Breakout)
    """
    LONG_PREF = "LONG_PREF"    # Prefer LONGs (in BULL markets)
    SHORT_PREF = "SHORT_PREF"  # Prefer SHORTs (in BEAR markets)
    BOTH = "BOTH"              # No preference (ranging)


class BiasStrengthConfig:
    """
    Configuration for the 3-state bias system.
    
    KEY CONCEPT: 
    - With-trend trades: normal frequency, normal size, relaxed entry
    - Counter-trend trades: lower frequency, reduced size, tighter invalidation
    """
    
    # =========================================================================
    # DETECTION THRESHOLDS
    # =========================================================================
    # ADX thresholds for strength detection
    ADX_STRONG_THRESHOLD = 25.0         # Above this = strong trend
    ADX_WEAK_THRESHOLD = 18.0           # Below this = neutral/ranging
    
    # EMA relationship confirmation
    EMA_SEPARATION_PCT = 0.5            # EMA50 must be > 0.5% from EMA200 for strong
    
    # VWAP slope for direction confirmation (degrees equivalent)
    VWAP_SLOPE_STRONG = 0.0002          # Strong slope threshold
    
    # =========================================================================
    # COUNTER-TREND RULES (trading against the strong bias)
    # For intraday: CT can be profitable, don't over-restrict
    # =========================================================================
    COUNTERTREND_SIZE_MULT = 0.50       # Size = 50% of normal (reduced but meaningful)
    COUNTERTREND_MAX_RR = 1.5           # Max target = 1.5R (quick but not scalp)
    COUNTERTREND_HARD_INVALIDATION = True  # Hard stop, no trailing
    COUNTERTREND_COOLDOWN_AFTER_LOSS = 2  # Candles to wait after CT loss (10 min)
    COUNTERTREND_MAX_PER_SESSION = 4    # Max CT trades per session (allow corrections)
    
    # =========================================================================
    # WITH-TREND RULES (trading with the strong bias)
    # =========================================================================
    WITHTREND_SIZE_MULT = 1.0           # Full size
    WITHTREND_SCORE_RELAXATION = 0.5    # Score threshold reduced by 0.5
    WITHTREND_ALLOW_CHASE = True        # Allow chasing in strong trends
    
    # =========================================================================
    # NEUTRAL RULES (ranging market)
    # =========================================================================
    NEUTRAL_SIZE_MULT = 0.75            # Slightly reduced size in range
    NEUTRAL_REQUIRE_MEAN_REVERSION = True  # Prefer entries near extremes
    NEUTRAL_FEE_AWARENESS = True        # Extra churn protection
    
    @classmethod
    def get_countertrend_rules(cls, bias_strength: BiasStrength, direction: str) -> dict:
        """
        Get trading rules based on bias strength and direction.
        
        Returns dict with: is_countertrend, size_mult, max_rr, hard_stop
        """
        is_countertrend = (
            (bias_strength == BiasStrength.BULL_STRONG and direction == 'SHORT') or
            (bias_strength == BiasStrength.BEAR_STRONG and direction == 'LONG')
        )
        
        if is_countertrend:
            return {
                'is_countertrend': True,
                'size_mult': cls.COUNTERTREND_SIZE_MULT,
                'max_rr': cls.COUNTERTREND_MAX_RR,
                'hard_stop': cls.COUNTERTREND_HARD_INVALIDATION,
                'cooldown_on_loss': cls.COUNTERTREND_COOLDOWN_AFTER_LOSS,
            }
        elif bias_strength == BiasStrength.NEUTRAL:
            return {
                'is_countertrend': False,
                'size_mult': cls.NEUTRAL_SIZE_MULT,
                'max_rr': 2.0,
                'hard_stop': False,
                'cooldown_on_loss': 1,
            }
        else:
            return {
                'is_countertrend': False,
                'size_mult': cls.WITHTREND_SIZE_MULT,
                'max_rr': 5.0,
                'hard_stop': False,
                'cooldown_on_loss': 0,
            }
    
    @classmethod
    def get_working_bias(cls, bias_strength: 'BiasStrength') -> 'WorkingBias':
        """
        ⚠️ DEPRECATED: Use RegimeFusion.fuse() instead for sizing decisions.
        
        This function is kept for logging/analysis purposes only.
        The actual sizing multiplier now comes from RegimeFusion.
        
        See: src/domain/market/regime/regime_fusion.py
        """
        if bias_strength == BiasStrength.BEAR_STRONG:
            return WorkingBias.SHORT_PREF
        elif bias_strength == BiasStrength.BULL_STRONG:
            return WorkingBias.LONG_PREF
        else:
            return WorkingBias.BOTH
    
    @classmethod
    def get_counter_preference_rules(cls, working_bias: 'WorkingBias', direction: str) -> dict:
        """
        ⚠️ DEPRECATED: Use RegimeFusion.fuse() instead.
        
        RegimeFusion provides:
        - size_mult based on alignment
        - rr_min based on alignment
        - is_blocked for "stupid fades"
        
        This function is kept for backward compatibility only.
        See: src/domain/market/regime/regime_fusion.py
        """
        # Determine if trade is against preference
        is_counter_pref = (
            (working_bias == WorkingBias.LONG_PREF and direction == 'SHORT') or
            (working_bias == WorkingBias.SHORT_PREF and direction == 'LONG')
        )
        
        if is_counter_pref:
            return {
                'is_counter_preference': True,
                'size_mult': 0.5,       # 50% size (meaningful, not tiny)
                'rr_add': 0.3,          # Need 0.3R extra (1.3R min instead of 1.0R)
                'score_penalty': -0.3,  # Small score deduction
            }
        else:
            return {
                'is_counter_preference': False,
                'size_mult': 1.0,
                'rr_add': 0.0,
                'score_penalty': 0.0,
            }
    
    @classmethod
    def calculate_weighted_bias_score(
        cls,
        ema50_vs_ema200: str,      # 'BULL', 'BEAR', 'NEUTRAL'
        di_direction: str | None,  # 'LONG', 'SHORT', or None
        di_strength: float,        # abs(+DI - -DI)
        price_vs_ema50_pct: float, # % distance from EMA50
        ema50_slope: float,        # slope of EMA50
        trade_direction: str       # 'LONG' or 'SHORT'
    ) -> dict:
        """
        ⚠️ DEPRECATED: Use RegimeFusion.update_macro() and fuse() instead.
        
        This function is superseded by the 3-layer RegimeFusion architecture:
        - CAPA 1 (MACRO): EMA50/200, +DI/-DI, EMA50 slope → macro_score
        - CAPA 2 (MICRO): direction, confidence, displacement
        - CAPA 3 (FUSION): alignment → size_mult, rr_min
        
        RegimeFusion provides cleaner separation and explicit BLOCK logic.
        See: src/domain/market/regime/regime_fusion.py
        """
        scores = {}
        
        # Component 1: EMA50/200 macro (weight 0.35)
        if ema50_vs_ema200 == 'BULL':
            scores['macro'] = 1.0
        elif ema50_vs_ema200 == 'BEAR':
            scores['macro'] = -1.0
        else:
            scores['macro'] = 0.0
        
        # Component 2: DI direction (weight 0.30)
        if di_direction == 'LONG' and di_strength >= 5:
            scores['di'] = min(1.0, di_strength / 15.0)  # Scale: 5-15 → 0.33-1.0
        elif di_direction == 'SHORT' and di_strength >= 5:
            scores['di'] = -min(1.0, di_strength / 15.0)
        else:
            scores['di'] = 0.0
        
        # Component 3: Price vs EMA50 (weight 0.20)
        # Positive = price above EMA50 (bullish)
        # Negative = price below EMA50 (bearish)
        # Cap at ±5% for normalization
        scores['position'] = max(-1.0, min(1.0, price_vs_ema50_pct / 0.05))
        
        # Component 4: EMA50 slope (weight 0.15)
        # Cap at ±0.1% for normalization
        scores['slope'] = max(-1.0, min(1.0, ema50_slope / 0.001))
        
        # Calculate weighted score
        weights = {'macro': 0.35, 'di': 0.30, 'position': 0.20, 'slope': 0.15}
        weighted_score = sum(scores[k] * weights[k] for k in scores)
        
        # Calculate alignment with trade direction
        # LONG trade: positive score = aligned, negative = misaligned
        # SHORT trade: negative score = aligned, positive = misaligned
        if trade_direction == 'LONG':
            alignment = weighted_score  # LONG wants positive score
        else:
            alignment = -weighted_score  # SHORT wants negative score
        
        # Size multiplier based on alignment
        # alignment >= 0.5: Full size (1.0)
        # alignment 0 to 0.5: 0.7 to 1.0
        # alignment -0.5 to 0: 0.4 to 0.7
        # alignment < -0.5: 0.3
        if alignment >= 0.5:
            size_mult = 1.0
        elif alignment >= 0:
            size_mult = 0.7 + (alignment * 0.6)  # 0.7 to 1.0
        elif alignment >= -0.5:
            size_mult = 0.4 + ((alignment + 0.5) * 0.6)  # 0.4 to 0.7
        else:
            size_mult = 0.3
        
        return {
            'score': round(weighted_score, 3),
            'alignment': round(alignment, 3),
            'size_mult': round(size_mult, 2),
            'breakdown': {k: round(v, 3) for k, v in scores.items()},
        }


# ============================================================================
# TREND DAY / EXPANSION DETECTOR (Jan 22, 2026)
# Detects when market shifts from range to trending
# ============================================================================
class TrendExpansionConfig:
    """
    Detects "trend day" conditions where mean-reversion logic fails.
    
    SYMPTOMS OF TREND DAY:
    - BB width expanding after compression
    - ATR ratio increasing
    - Price persistently away from VWAP
    - ADX slope positive (not just value)
    
    BEHAVIOR CHANGES WHEN DETECTED:
    - Disable mean-reversion mentality
    - Relax chase penalty
    - Allow breakouts at extreme percentiles
    - Let runners run longer
    """
    
    # =========================================================================
    # DETECTION CRITERIA (need 3 of 4 for trend expansion)
    # =========================================================================
    
    # Bollinger Band Width expansion
    BB_WIDTH_EXPANSION_RATIO = 1.3      # Current width > 1.3x recent avg
    BB_WIDTH_LOOKBACK = 20              # Bars to calculate average width
    
    # ATR expansion
    ATR_EXPANSION_RATIO = 1.25          # Current ATR > 1.25x 20-period SMA
    
    # VWAP distance persistence
    VWAP_DISTANCE_ATR = 1.0             # Price > 1 ATR from VWAP
    VWAP_DISTANCE_PERSISTENCE = 5       # Must stay away for 5+ candles (5m)
    
    # ADX slope (momentum of trend strength)
    ADX_SLOPE_POSITIVE_THRESHOLD = 0.3  # ADX increasing by 0.3+ per bar
    ADX_VALUE_EXPANDING = 25.0          # ADX > 25 and rising
    
    # =========================================================================
    # MINIMUM CRITERIA MET
    # =========================================================================
    MIN_CRITERIA_FOR_EXPANSION = 3      # Need 3 of 4 signals
    
    # =========================================================================
    # BEHAVIOR MODIFICATIONS IN EXPANSION
    # =========================================================================
    DISABLE_MEAN_REVERSION = True       # Don't fade moves
    CHASE_PENALTY_MULT = 0.3            # Reduce chase penalty to 30%
    ALLOW_EXTREME_BREAKOUTS = True      # Trade at 90+ percentiles
    RUNNER_HOLD_MULT = 1.5              # Hold runners 50% longer
    
    # Anti-fade: Don't short after expansion starts in bull
    ANTI_FADE_CANDLES = 4               # No counter-trend for 4 candles (20m) - corrections can come quick
    
    @classmethod
    def get_expansion_modifiers(cls) -> dict:
        """Get behavior modifiers when expansion detected."""
        return {
            'disable_mean_reversion': cls.DISABLE_MEAN_REVERSION,
            'chase_penalty_mult': cls.CHASE_PENALTY_MULT,
            'allow_extreme_breakouts': cls.ALLOW_EXTREME_BREAKOUTS,
            'runner_hold_mult': cls.RUNNER_HOLD_MULT,
            'anti_fade_candles': cls.ANTI_FADE_CANDLES,
        }


# ============================================================================
# SHORT OBSTINACY PREVENTION (Jan 22, 2026)
# Prevents repeated shorts before pumps in bullish context
# ============================================================================
class ShortObstinacyConfig:
    """
    Prevents the bot from insisting on shorts when context has changed.
    
    PATTERN TO AVOID:
    - Range detected
    - Bot shorts from "top of range"
    - Market starts trending up
    - Bot keeps trying shorts → death by fees
    
    SOLUTION:
    - Penalize (not block) shorts when bullish signals accumulate
    - Cooldown after failed shorts in bullish context
    """
    
    # =========================================================================
    # BULLISH CONTEXT DETECTION
    # =========================================================================
    PRICE_ABOVE_VWAP = True             # Close > VWAP
    VWAP_SLOPE_POSITIVE = True          # VWAP slope > 0
    VWAP_DISTANCE_K = 0.5               # Close > VWAP + k*ATR triggers warning
    
    # EMA alignment check
    EMA20_ABOVE_EMA50 = True            # EMA20 > EMA50 on 5m
    
    # =========================================================================
    # PENALTIES FOR SHORTING IN BULLISH CONTEXT
    # UPDATED Jan 27, 2026: Reduced penalties - they were blocking valid shorts
    # during price drops when momentum indicators lagged behind price
    # =========================================================================
    SCORE_PENALTY_MILD = -0.15          # 1-2 bullish signals (minor warning) - was -0.25
    SCORE_PENALTY_MODERATE = -0.25      # 3 bullish signals (caution) - was -0.5
    SCORE_PENALTY_SEVERE = -0.5         # 4 bullish signals (significant but not block) - was -1.0
    
    # =========================================================================
    # COOLDOWN AFTER FAILED SHORT
    # =========================================================================
    COOLDOWN_CANDLES_5M = 3             # 15 min cooldown (not 30 - corrections can come fast)
    NO_MORE_SHORTS_IF_TREND = True      # If expansion detected, no shorts until reset
    
    @classmethod
    def calculate_short_penalty(cls, bullish_signals: int) -> float:
        """
        Calculate score penalty for shorting based on bullish signal count.
        
        Args:
            bullish_signals: Number of bullish context signals (0-4)
            
        Returns:
            Score penalty (negative number)
        """
        if bullish_signals <= 1:
            return 0.0
        elif bullish_signals == 2:
            return cls.SCORE_PENALTY_MILD
        elif bullish_signals == 3:
            return cls.SCORE_PENALTY_MODERATE
        else:
            return cls.SCORE_PENALTY_SEVERE


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
    ENABLED = True                           # Master switch for pullback shorts
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
    
    🔥 FIX (Feb 03, 2026): MODE is now DYNAMIC based on trade count
    - First 100 trades: DISCOVERY (learning, conservative)
    - After 100 trades: PRODUCTION (proven edge, full sizing)
    This is an INTRADAY system - we learn fast.
    """
    _MODE_OVERRIDE = None  # Set to "DISCOVERY" or "PRODUCTION" to force
    TRADE_THRESHOLD_FOR_PRODUCTION = 100  # Switch to PRODUCTION after this many trades
    
    @classmethod
    def get_mode(cls) -> str:
        """Get current operating mode - dynamic based on trade count."""
        if cls._MODE_OVERRIDE:
            return cls._MODE_OVERRIDE
        
        # Check trade count from storage
        try:
            from pathlib import Path
            import json
            trades_dir = Path('/home/trading/traderAgents/FideiRatioCapital/systems/btc/trade_logs')
            total_trades = 0
            for f in trades_dir.glob('trades_*.jsonl'):
                with open(f) as fh:
                    total_trades += sum(1 for _ in fh)
            
            if total_trades >= cls.TRADE_THRESHOLD_FOR_PRODUCTION:
                return "PRODUCTION"
            return "DISCOVERY"
        except Exception:
            return "DISCOVERY"
    
    # Legacy property for backward compatibility
    MODE = property(lambda self: OperatingMode.get_mode())
    
    # Mode-specific thresholds
    # 🔥 FIX #155 (Feb 10, 2026): Score threshold raised from 5 to 6
    # Analysis shows score 5.5 trades have MFE=0.00R (never go in favor)
    # Example: Trade 14:17:43 entered with score 5.5, lost -0.44R
    # Trades with score < 6 are lottery tickets, not trades.
    DISCOVERY_SCORE_THRESHOLD = 6   # 🔥 Raised from 5 - marginal trades must not enter
    PRODUCTION_SCORE_THRESHOLD = 7  # Stricter
    
    # Risk per trade by mode
    DISCOVERY_RISK_PCT = 0.50       # Fixed risk in discovery (was 0.25 - too conservative)
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
    # 🔥 FIX (Jan 25, 2026): Adjusted for micro accounts - faster reduction
    # Progressive reduction: 1 loss = 70%, 2 losses = 50%, 3 losses = 30%
    # =========================================================================
    LOSS_SIZING_MULTIPLIERS = {
        0: 1.0,    # No losses - full size
        1: 0.70,   # 1 loss - 70% size
        2: 0.50,   # 2 losses - 50% size
        3: 0.30,   # 3+ losses - 30% size (capital preservation for micro accounts)
    }
    
    # =========================================================================
    # AUTO KILL-SWITCH - 🔥 NEW (Jan 25, 2026)
    # Automatic trading halt after consecutive losses or daily DD
    # =========================================================================
    KILL_SWITCH_LOSSES = 6           # Full kill switch after 6 losses
    KILL_SWITCH_DAILY_DD_PCT = 3.0   # Kill switch if daily DD > 3%
    PAUSE_AFTER_LOSSES_3 = 15        # 15 min pause after 3 losses
    PAUSE_AFTER_LOSSES_4 = 60        # 60 min pause after 4 losses
    PAUSE_AFTER_LOSSES_5 = 180       # 3 hour pause after 5 losses
    
    # =========================================================================
    # CAPA 4: MINIMUM ACTIVITY PER SESSION - Prevent "no trades" failures
    # If trades/session is below minimum, progressively relax filters
    # FIX (Jan 24, 2026): More aggressive relaxation - bot was at 0 trades in 9h
    # =========================================================================
    MIN_TRADES_PER_SESSION = 1        # Minimum trades expected per 4h session
    MIN_TRADES_PER_DAY = 2            # Minimum trades expected per day (was 3)
    
    # Filter relaxation when activity is too low
    LOW_ACTIVITY_SCORE_REDUCTION = 1.5  # Reduce threshold by 1.5 if low activity (was 1)
    
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
        'DEAD_HOURS': 0.30,         # 🔥 FIX (Jan 25): Reduced to 30% (was 50%)
        'WEEKEND': 0.50,            # Low liquidity
        'OTHER': 0.75,              # Default reduced
    }
    
    # 🔥 NEW (Jan 25, 2026): Dead Hours Loss-Based Block
    # If 2+ losses occur during DEAD_HOURS, block trading until next session
    DEAD_HOURS_MAX_LOSSES = 2       # Block after 2 losses in dead hours
    DEAD_HOURS_UTC = (0, 8)         # 🔥 FIX #293: Aligned with get_session_name()
    
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
    # 🔥 FIX (Feb 01, 2026): Data-driven weekend reduction
    # Data analysis: Weekend 29 trades, 37.9% win, -0.62R total (avg -0.021R/trade)
    #                Weekday 33 trades, 42.4% win, +0.42R total (avg +0.013R/trade)
    # TIME_INVALIDATION_MAX_LOSS on weekend: 11 trades, -3.98R (main loss driver)
    # Solution: Reduce weekend sizing from 0.60 → 0.40 to limit exposure
    DAY_OF_WEEK_MULTIPLIERS = {
        0: 1.0,    # Monday - normal
        1: 1.0,    # Tuesday - good trends
        2: 1.0,    # Wednesday - normal
        3: 1.0,    # Thursday - normal
        4: 0.85,   # Friday - early close risk, reduce
        5: 0.40,   # Saturday - weekend, low liquidity, high TIME_INV losses
        6: 0.40,   # Sunday - weekend, low liquidity, high TIME_INV losses
    }
    
    @classmethod
    def get_day_multiplier(cls, day_of_week: int) -> float:
        """Get sizing multiplier based on day of week (0=Monday, 6=Sunday)."""
        return cls.DAY_OF_WEEK_MULTIPLIERS.get(day_of_week, 1.0)
    
    # SMART COOLDOWN - Based on exit reason and PnL size
    # 🔥 FIX #149 (Feb 10, 2026): NO COOLDOWN TRAS PROFIT
    # 
    # PROBLEMA: Cooldown tras profit pequeño = "miedo codificado"
    # Sales bien → 5min cooldown → pierdes continuación → frustración.
    # 
    # SOLUCIÓN INSTITUCIONAL:
    # - Profit (pnl_r > 0) = 0 cooldown (el edge sigue, entra de nuevo)
    # - BE/scratch = 1 min (reset mental)
    # - Pequeña pérdida = normal (recalibrar)
    # - Grande pérdida = más largo (protección emocional)
    # =====================================================================
    COOLDOWN_PROFIT = 0              # 🔥 FIX #149: Sin cooldown tras profit
    COOLDOWN_SCRATCH = 1             # 🔥 FIX #149: BE/scratch = 1 min reset
    COOLDOWN_TIME_INVALIDATION = 2   # 🔥 Reducido de 3: mercado muerto, no error
    COOLDOWN_PLANNED_EXIT = 0        # 🔥 FIX #149: TP = 0 cooldown (captura continuación)
    COOLDOWN_SL_SMALL_LOSS = 5       # 🔥 Reducido de 8: pequeña pérdida
    COOLDOWN_SL_NORMAL = 10          # 🔥 Reducido de 12: pérdida normal
    COOLDOWN_SL_LARGE_LOSS = 15      # 🔥 Reducido de 20: pérdida grande
    COOLDOWN_EMERGENCY = 30          # 30 min - emergency close
    COOLDOWN_DEFAULT = 5             # 🔥 Reducido de 10: default más bajo
    
    # If signal still aligned with HTF after loss, reduce cooldown
    COOLDOWN_ALIGNED_REDUCTION = 0.5  # 50% reduction if still aligned
    
    # 🔥 NEW: Planned exit reasons that deserve short cooldown
    PLANNED_EXIT_REASONS = {
        'VPOC_EXIT', 'TP_HIT', 'TAKE_PROFIT', 'TP1', 'TP2',
        'EXCHANGE_TAKE_PROFIT', 'MOMENTUM_EXHAUSTION', 'SMART_EXIT',
        'VOLATILITY_SQUEEZE_EXIT', 'EARLY_MFE_EXIT', 'MICRO_REVERSAL_TP'
    }
    
    @classmethod
    def get_smart_cooldown(cls, exit_reason: str, pnl_r: float) -> int:
        """
        Calculate cooldown based on exit reason and loss size.
        
        🔥 FIX #149 + FIX #298 (Feb 18, 2026):
        
        FIX #149: NO COOLDOWN TRAS PROFIT (el edge sigue activo)
        FIX #298: TRAILING_STOP con profit → 2min cooldown
        
        PROBLEMA FIX #298 (Run 20260217):
        - Trade 4 LONG cerró por TRAILING_STOP con +0.35R a $67,853
        - 0 cooldown → sistema inmediatamente disponible
        - 10 min después, BIAS FLIP → Trade 5 LONG @ $68,111 ($258 más alto)
        - TRAILING_STOP indica que el precio SE GIRÓ contra la posición
        - Re-entry inmediato en la misma dirección = CHASE
        
        SOLUCIÓN:
        - TRAILING_STOP con profit → 2min cooldown (el precio se giró, esperar)
        - Otros profits (TP, VPOC, etc.) → 0min (salida planificada, no giro)
        """
        exit_upper = exit_reason.upper()
        
        # 🔥 FIX #298: TRAILING_STOP con profit → 2min cooldown
        # El trailing se activó porque el precio se giró. No re-entrar inmediatamente.
        if pnl_r > 0.02 and 'TRAILING' in exit_upper:
            return 2  # 2 minutos — el precio se giró, esperar estabilización
        
        # 🔥 FIX #149: SI HAY PROFIT → 0 COOLDOWN
        # El edge funcionó. Puede haber continuación.
        if pnl_r > 0.02:  # >0.02R = profit real (no dust)
            return cls.COOLDOWN_PROFIT  # 0 minutos
        
        # Scratch/BE (-0.02 a +0.02)
        if -0.02 <= pnl_r <= 0.02:
            return cls.COOLDOWN_SCRATCH  # 1 minuto
        
        # Planned exits (TP, VPOC, etc.)
        if exit_upper in cls.PLANNED_EXIT_REASONS or any(r in exit_upper for r in ['VPOC', 'TP', 'PROFIT']):
            return cls.COOLDOWN_PLANNED_EXIT
        
        if exit_reason == 'TIME_INVALIDATION':
            return cls.COOLDOWN_TIME_INVALIDATION
        elif 'EMERGENCY' in exit_reason:
            return cls.COOLDOWN_EMERGENCY
        elif 'SL' in exit_reason or 'STOP' in exit_reason or 'STRUCTURE' in exit_reason:
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
        return cls.get_mode() == "DISCOVERY"
    
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
    # 
    # 🔥 FIX (Feb 01, 2026): Data-driven score thresholds from 62 trade analysis
    # Analysis results:
    # 🔥 FIX #232 (Feb 15, 2026): Score thresholds REBALANCED
    # PROBLEM: BLOCK=5.5 was too high. Edge floor gives 4.0 minimum,
    # so scores 4.0-5.4 were ALWAYS blocked = system never trades.
    # Combined with probe sizing + MIN_PROFIT check = death trap.
    #
    # NEW: BLOCK=4.5, PROBE=5.5, REDUCED=6.0, FULL>=6.0
    # This lets edge-floor trades enter as probes and decent setups trade.
    #
    SCORE_BLOCK_FLOOR = 4.5          # 🔥 FIX #232: Lowered from 5.5 - allow marginal as probe
    SCORE_PROBE_MAX = 5.5            # 4.5-5.5 = probe (0.25x)
    SCORE_REDUCED_MAX = 6.0          # 5.5-6.0 = reduced (0.5x)
    # Above 6.0 = full size (1.0x)
    
    # RR-BASED SIZING
    # Graduated sizing based on risk:reward quality
    # 
    # NOTE (Jan 27, 2026): HARD BLOCKS in entry_validators.py block trades with 
    # worst-case RR < 0.7R. These floors below 0.7R are for edge cases where
    # HARD BLOCKS are not applied (e.g., adopted positions, manual overrides).
    #
    # HIERARCHY:
    # 1. HARD BLOCK: worst_case_rr < 0.7R → NO ENTRY (entry_validators.py)
    # 2. EntryGate: worst_case_rr 0.7-0.8R → PROBE size (0.25x)
    # 3. EntryGate: worst_case_rr > 0.8R → FULL size (1.0x)
    #
    RR_BLOCK_FLOOR = 0.70            # Aligned with HARD BLOCK (was 0.30)
    RR_PROBE_MAX = 0.80              # 0.70-0.80 = probe (0.25x)
    RR_FULL_MIN = 0.80               # Above 0.8 = full size
    
    # RANGE regime: HARD BLOCK requires RR >= 0.85 in ranging markets (ADX < 20)
    RR_BLOCK_FLOOR_RANGE = 0.85      # Aligned with HARD BLOCK range check
    RR_PROBE_MAX_RANGE = 0.95        # 0.85-0.95 = probe in RANGE
    
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
    # 🔥 FIX (Jan 23, 2026): Correct ADX classification thresholds
    # ADX is a CLASSIFIER, not a gate. It determines trading MODE:
    # - ADX < 18: RANGE/CHOP → smaller stops, faster exits, mean-reversion
    # - ADX 18-25: WEAK_TREND → cautious trend-following
    # - ADX > 25: TREND → full trend-following logic
    #
    # Previous bug: ADX 15.3 was classified as "Mode: TREND" → wrong stops used
    
    # ADX Classification Thresholds (DO NOT lower these!)
    ADX_RANGE_MAX = 18              # ADX < 18 → RANGE mode
    ADX_WEAK_TREND_MAX = 25         # ADX 18-25 → WEAK_TREND mode  
    ADX_TREND_MIN = 25              # ADX >= 25 → TREND mode
    
    # Legacy (kept for backwards compatibility, but use new thresholds above)
    MIN_ADX = 20                    # 🔥 FIX (Jan 23): ADX must be >= 20 for ANY trend classification
    MIN_ADX_SLOPE = -999            # DEPRECATED: slope never blocks
    MIN_ATR_VS_MA = 0.50  # 🔥 FIX (Jan 24): Relaxed from 0.70 to allow low-vol periods            # Allow contractions
    
    # DEPRECATED: Slope filtering removed
    # ADX slope is now INFO/TAG only, never a veto
    SMART_SLOPE_FILTER = False      # DISABLED - slope is info only
    WEAK_ADX_THRESHOLD = 20         # Not used (kept for reference)
    STRONG_SLOPE_DECLINE = -0.15    # Not used (kept for reference)
    
    # Counter-trend trading in defensive mode
    ALLOW_COUNTERTREND_DEFENSIVE = True  # Allow counter-trend with reduced size
    
    # Correlation filters (for NQ/ES)
    CHECK_CORRELATION = True
    CORRELATION_TIMEFRAME = '5m'


# ============================================================================
# RANGE MODE CONFIGURATION (Jan 23, 2026)
# When ADX < 18, market is ranging - different rules apply
# ============================================================================
class RangeModeConfig:
    """
    Configuration for RANGE market regime (ADX < 18).
    
    🔥 CORRECCIÓN (Jan 23, 2026):
    NO bloquear trades en RANGE - usar reglas diferentes.
    BTC pasa 60-70% del tiempo en rango - hay que explotarlo.
    
    RANGE MODE RULES:
    - Trades SÍ permitidos, pero SOLO en extremos (VAH/VAL/bands)
    - Size reducido (50%)
    - Stop más tight (2x ATR vs 3x en trend)
    - TP corto + salida rápida
    - Score threshold más alto
    
    TRANSITION MODE (ADX 18-20):
    - Size 70%
    - Score threshold +0.5
    
    TREND MODE (ADX > 20):
    - Full size, normal rules
    """
    
    # =========================================================================
    # TRADE PERMISSIONS IN RANGE
    # =========================================================================
    ALLOW_TREND_TRADES = False      # NO trend trades at MID-RANGE
    ALLOW_EXTREME_TRADES = True     # YES at VAH/VAL/extremes (mean-reversion)
    
    # Extreme definition (percentile of 60-day range)
    EXTREME_HIGH_PERCENTILE = 90    # >= 90% = HIGH extreme (SHORT only)
    EXTREME_LOW_PERCENTILE = 10     # <= 10% = LOW extreme (LONG only)
    
    # =========================================================================
    # SIZE MULTIPLIERS BY MODE
    # =========================================================================
    RANGE_SIZE_MULT = 0.50          # 50% size in RANGE
    TRANSITION_SIZE_MULT = 0.70     # 70% size in TRANSITION
    TREND_SIZE_MULT = 1.0           # 100% size in TREND
    
    # =========================================================================
    # SCORE THRESHOLD ADJUSTMENTS
    # =========================================================================
    RANGE_SCORE_ADD = 1.0           # +1.0 to threshold in RANGE (stricter)
    TRANSITION_SCORE_ADD = 0.5      # +0.5 to threshold in TRANSITION
    TREND_SCORE_ADD = 0.0           # No adjustment in TREND
    
    # =========================================================================
    # STOP LOSS IN RANGE (tighter than trend)
    # =========================================================================
    RANGE_SL_ATR_MULTIPLIER = 2.0   # 2x ATR (vs 3x in trend)
    RANGE_SL_MAX_PCT = 0.8          # Max 0.8% stop (tighter in chop)
    
    # =========================================================================
    # TARGET/EXIT IN RANGE (faster exits)
    # =========================================================================
    RANGE_TP_R = 1.0                # TP at 1R (quick target)
    RANGE_MAX_DURATION_MIN = 30     # Max 30 min in trade (vs 90 in trend)
    RANGE_MIN_RR = 1.2              # Need RR >= 1.2 neto in range
    
    # =========================================================================
    # TIME MANAGEMENT IN RANGE (faster exits)
    # 🔥 FIX (Jan 25, 2026): 4 candles too tight - BTC needs time to develop
    # =========================================================================
    RANGE_TIME_INVALIDATION_CANDLES = 6   # 6 candles (was 4 - too aggressive)
    RANGE_MFE_THRESHOLD = 0.15            # Lower MFE threshold (faster exit if no MFE)
    
    # =========================================================================
    # TRADE LIMITS IN RANGE (prevent re-entry at same level)
    # =========================================================================
    RANGE_MAX_TRADES_PER_SESSION = 2      # Max 2 trades per session in range
    RANGE_COOLDOWN_AFTER_LOSS_MIN = 20    # 20 min cooldown after loss in range
    
    # =========================================================================
    # ADX THRESHOLDS FOR MODE CLASSIFICATION
    # 🔥 FIX (Feb 04, 2026): Aligned with RiskManagement thresholds
    # ADX < 18 = RANGE (no trend)
    # ADX 18-22 = TRANSITION (weak trend developing)
    # ADX > 22 = TREND (tradeable trend)
    # =========================================================================
    ADX_RANGE_MAX = 18              # ADX < 18 = RANGE
    ADX_TRANSITION_MAX = 22         # ADX 18-22 = TRANSITION (was 20 - too narrow)
    # ADX > 22 = TREND
    
    @classmethod
    def get_trading_mode(cls, adx: float) -> str:
        """Classify trading mode based on ADX."""
        if adx < cls.ADX_RANGE_MAX:
            return 'RANGE'
        elif adx < cls.ADX_TRANSITION_MAX:
            return 'TRANSITION'
        else:
            return 'TREND'
    
    @classmethod
    def get_size_multiplier(cls, mode: str) -> float:
        """Get position size multiplier for mode."""
        if mode == 'RANGE':
            return cls.RANGE_SIZE_MULT
        elif mode == 'TRANSITION':
            return cls.TRANSITION_SIZE_MULT
        return cls.TREND_SIZE_MULT
    
    @classmethod
    def get_score_adjustment(cls, mode: str) -> float:
        """Get score threshold adjustment for mode."""
        if mode == 'RANGE':
            return cls.RANGE_SCORE_ADD
        elif mode == 'TRANSITION':
            return cls.TRANSITION_SCORE_ADD
        return cls.TREND_SCORE_ADD
    
    @classmethod
    def is_at_extreme(cls, percentile: float, bias: str) -> tuple:
        """
        Check if at valid extreme for the given bias.
        
        Returns:
            (is_valid: bool, extreme_type: str or None)
        """
        if percentile >= cls.EXTREME_HIGH_PERCENTILE:
            # At HIGH extreme - only SHORT is valid
            if bias == 'SHORT':
                return (True, 'HIGH')
            return (False, 'HIGH')  # LONG not valid at top
        elif percentile <= cls.EXTREME_LOW_PERCENTILE:
            # At LOW extreme - only LONG is valid
            if bias == 'LONG':
                return (True, 'LOW')
            return (False, 'LOW')  # SHORT not valid at bottom
        
        # Not at extreme
        return (False, None)


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
    # FIX (Jan 22, 2026): Reduced to 1.0R to maximize winrate in Discovery
    # Many trades reach 1.0R but not 1.2R - capture more winners
    TP1_RISK_MULTIPLE = 1.0         # TP at 1.0R (maximize winrate, validate edge faster)
    TP1_CLOSE_PCT = 50              # Close 50% at TP1, leave 50% runner (was 60%)
    
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
    
    # =========================================================================
    # 🔥 EARLY PARTIAL - CALIBRADO PARA INTRADÍA BTC (Feb 2, 2026)
    # =========================================================================
    # FILOSOFÍA: Asegurar algo de profit sin cortar ganadores prematuramente
    # 
    # PROBLEMA ORIGINAL: Trades llegan a +0.5R y luego pierden
    # PERO: +0.5R es MUY temprano - muchos trades buenos hacen pullback ahí
    # 
    # SOLUCIÓN CALIBRADA:
    # - EARLY_PARTIAL_R = 0.7R (no 0.5R - dar más espacio)
    # - EARLY_PARTIAL_PCT = 30% (no 50% - dejar más para runners)
    # - EARLY_PARTIAL_MOVE_SL = False (no mover a BE aún - R_LOCK lo maneja)
    # 
    # Con estos settings:
    # - A +0.7R, tomas 30% y aseguras +0.21R
    # - El R_LOCK ya habrá movido stop a -0.20R (desde 0.5R MFE)
    # - El 70% restante tiene espacio para continuar a +1R+
    EARLY_PARTIAL_ENABLED = True
    EARLY_PARTIAL_R = 0.7           # Take partial at +0.7R (not too early)
    EARLY_PARTIAL_PCT = 30          # Close 30% (leave 70% for runners)
    EARLY_PARTIAL_MOVE_SL = False   # Don't move to BE yet - let R_LOCK handle
    
    # =========================================================================
    # 🔥 NEW (Feb 2, 2026): ADAPTIVE SIZING FILTERS
    # =========================================================================
    # These are based on Jan 20 - Feb 1 analysis but SHOULD BE RECALIBRATED
    # periodically as market conditions change. 
    # 
    # ⚠️ IMPORTANT: These use small sample sizes (2-9 trades per bucket).
    # Monitor effectiveness and adjust as needed.
    # 
    # To disable any filter, set ENABLED = False
    # =========================================================================
    
    # --- TOXIC HOURS (hours with historical negative edge) ---
    # 🔥 FIX (Feb 04, 2026): RELAXED - original hours included prime trading times (9, 13)
    # Only truly dead/low-liquidity hours should be penalized:
    # - 4-7 UTC: Dead zone between Asia close and London open
    # - 21-23 UTC: Dead zone after NY close, before Asia
    #
    # Note: We use REDUCE (0.5x) not BLOCK - always allow trades
    TOXIC_HOURS_ENABLED = True      # Keep enabled but relaxed
    TOXIC_HOURS_SET = {4, 5, 6, 21, 22}  # 🔥 Only true dead hours
    TOXIC_HOURS_MULT = 0.5          # 🔥 Increased from 0.25 to 0.5 (less aggressive)
    
    # --- TOXIC COMBOS (session+direction with negative edge) ---
    # 🔥 FIX (Feb 04, 2026): DISABLED - blocking based on 7-8 trades is statistically meaningless
    # The system was blocking 100% of trades during LONDON_NY_OVERLAP (5 hours/day!)
    # This is over-optimization on noise. Better to trade and collect more data.
    # 
    # Original data (too small sample to draw conclusions):
    # - LONDON_NY_OVERLAP + SHORT: -1.72R in 7 trades
    # - LONDON_NY_OVERLAP + LONG: Unknown (added without data)
    # - WEEKEND + LONG: -0.72R in 7 trades, 29% WR
    #
    # SOLUTION: Disable combo blocking entirely. Use REDUCE mode only if needed later.
    # 🔥🔥🔥 TOXIC COMBOS COMPLETELY DISABLED (Feb 04, 2026) 🔥🔥🔥
    # PROBLEM: Log showed "TOXIC COMBO BLOCK: LONDON_NY_OVERLAP + SHORT" blocking
    # trades even with ENABLED=False. This cost us $30+ in a clear downtrend.
    # SOLUTION: Triple-check disabled AND empty the set.
    TOXIC_COMBOS_ENABLED = False    # MUST BE FALSE - NEVER BLOCK ON COMBOS
    TOXIC_COMBOS_MODE = 'DISABLED'  # Changed from REDUCE to DISABLED
    TOXIC_COMBOS_MULT = 1.0         # No reduction at all
    TOXIC_COMBOS_SET = set()        # 🔥 EMPTY SET - no combos defined
    
    # --- WARMUP PERIOD (prevents trading immediately after startup) ---
    # 🔥 NEW (Feb 04, 2026): System was entering trades on FIRST tick after startup
    # This is dangerous - no market context gathered yet
    WARMUP_ENABLED = True
    WARMUP_MIN_TICKS = 60           # Minimum ticks before first trade (~60 seconds)
    WARMUP_MIN_CANDLES = 3          # Minimum 1m candles to observe
    
    # --- DELTA CONTRADICT PENALTY ---
    # 🔥🔥🔥 DISABLED (Feb 04, 2026): Delta is a LAGGING indicator!
    # Log showed: Delta blocked SHORT at $72,657-$72,368 (during valid move)
    #             Then allowed entry at $72,392 (the EXACT BOTTOM)
    #             Result: -0.22R loss entering at minimum
    # LESSON: Delta measures PAST volume, not FUTURE direction
    # When price drops, buyers step in (positive delta) but price keeps falling
    # This filter BLOCKS good entries and ALLOWS bad ones
    DELTA_CONTRADICT_ENABLED = False           # 🔥 DISABLED - lagging indicator
    DELTA_CONTRADICT_PENALTY_RANGE = 0.0       # No penalty
    DELTA_CONTRADICT_PENALTY_TRANSITION = 0.0  # No penalty
    DELTA_CONTRADICT_PENALTY_TREND = 0.0       # No penalty
    DELTA_CONTRADICT_BLOCK_THRESHOLD = 999.0   # 🔥 Effectively disabled (was 1.5)
    
    # --- TRANSITION MODE REQUIREMENTS ---
    # 🔥 REVISED (Feb 04, 2026): 6.0 was too high - blocking all TRANSITION trades
    # TRANSITION with structural consensus should still trade, just with smaller size
    TRANSITION_MIN_SCORE = 5.0      # Lowered from 6.0 - CONSENSUS means we CAN trade
    TRANSITION_REQUIRE_LTF_3_OF_3 = False  # Require all 3 LTF confirmations? (relaxed)
    TRANSITION_ALLOW_CONSENSUS = True  # Allow trades when structural=active even in TRANSITION
    
    # --- LOSS STREAK COOLDOWN ---
    LOSS_STREAK_COOLDOWN_ENABLED = True
    LOSS_STREAK_3_COOLDOWN_MIN = 15   # Minutes to wait after 3 losses
    LOSS_STREAK_4_COOLDOWN_MIN = 30   # Minutes to wait after 4 losses
    LOSS_STREAK_5_COOLDOWN_MIN = 60   # Minutes to wait after 5+ losses
    
    # --- MOMENTUM SIZING ---
    # 🔥 DISABLED (Feb 7, 2026): momentum_score is always 0 (never calculated)
    # This was silently reducing all sizes by 50%
    MOMENTUM_SIZING_ENABLED = False  # TODO: Fix momentum calculation first
    MOMENTUM_LOW_THRESHOLD = 0.5    # Below this = low momentum
    MOMENTUM_HIGH_THRESHOLD = 1.5   # Above this = high momentum  
    MOMENTUM_LOW_MULT = 0.5         # Sizing mult for low momentum
    MOMENTUM_HIGH_MULT = 1.2        # Sizing mult for high momentum
    
    # --- TRAILING STOP IMPROVEMENT ---
    TRAILING_STEP_07R_ENABLED = True  # Lock +0.3R at MFE >= 0.7R
    
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
    
    # =========================================================================
    # 🔥 NEW (Feb 2, 2026): FAST REVERSAL DETECTION
    # =========================================================================
    # Problem: System uses HH/HL/LL/LH scoring which requires 4+ candles to flip
    # direction. When 5 bearish candles are followed by 1 strong bullish reversal,
    # the system stays SHORT even though trend has reversed.
    #
    # Solution: Detect single-candle reversals based on:
    # 1. Candle body size >= 1.5x ATR (strong conviction)
    # 2. Candle direction opposes current bias
    # 3. Engulfing pattern (covers previous candle)
    # 4. Momentum shift (volume spike)
    #
    # When detected: Override micro_momentum to new direction
    # =========================================================================
    
    FAST_REVERSAL_ENABLED = True        # Set False to disable
    FAST_REVERSAL_MIN_BODY_ATR = 1.5    # 🔥 INCREASED from 1.2 - need stronger candle
    FAST_REVERSAL_ENGULF_REQUIRED = True  # Require engulfing pattern
    FAST_REVERSAL_VOLUME_MULT = 2.0     # 🔥 INCREASED from 1.5 - need stronger volume
    FAST_REVERSAL_OVERRIDE_DURATION_TICKS = 15  # 🔥 REDUCED from 30 - shorter override
    FAST_REVERSAL_REQUIRE_STRUCTURE_BREAK = True  # 🔥 NEW: Only flip if price breaks structure
    
    # =========================================================================
    # 🔥 NEW (Feb 2, 2026): IMMEDIATE DIRECTION FLIP
    # =========================================================================
    # When a strong displacement occurs AGAINST current bias, immediately
    # flip direction rather than waiting for EMA/structure to catch up.
    #
    # Criteria:
    # - Price moves >= 0.5x ATR(5m) in opposite direction of current bias
    # - Movement happens within 3 candles
    # - Volume above average
    # =========================================================================
    
    IMMEDIATE_FLIP_ENABLED = True        # Set False to disable
    IMMEDIATE_FLIP_MIN_MOVE_ATR = 0.5    # Min move in ATR(5m) for immediate flip
    IMMEDIATE_FLIP_MAX_CANDLES = 3       # Must happen within N candles
    IMMEDIATE_FLIP_VOLUME_MULT = 1.2     # Min volume multiplier


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
    COOLDOWN_MINUTES = 5           # Mandatory cooldown after trade (was 15)
    
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
    # 🔥 FIX #150 (Feb 10, 2026): SALIDAS ESCALONADAS INSTITUCIONALES
    # 
    # PROBLEMA ANTERIOR: Esperaba +1.0R para primer parcial → rara vez llegaba
    # RESULTADO: MFE=0.49R pero cerraba todo junto o muy tarde.
    # 
    # SOLUCIÓN INSTITUCIONAL (3 tramos):
    # - 30% @ +0.30R → Paga el riesgo temprano
    # - 40% @ +0.80R → Captura el core del movimiento
    # - 30% runner → Deja correr con trailing (sin límite superior)
    # 
    # BENEFICIOS:
    # 1. "Pagas el riesgo" rápido → psicología mejorada
    # 2. Core del movimiento capturado antes de reversión
    # 3. Runner aprovecha continuaciones (sin cortar)
    # =========================================================================
    SCALED_PARTIALS_ENABLED = True
    
    # Exit levels: (MFE in R, % of position to exit)
    SCALE_OUT_LEVELS = [
        (0.30, 0.30),   # 🔥 FIX #150: Exit 30% @ +0.30R (paga el riesgo)
        (0.80, 0.40),   # 🔥 FIX #150: Exit 40% @ +0.80R (core del movimiento)
        # Remaining 30% runs with trailing stop → NO EXIT AUTOMÁTICO
    ]
    
    # Minimum position to keep for runner
    MIN_RUNNER_PCT = 0.30  # 🔥 FIX #150: 30% como runner (era 10%)
    
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
    # 🔥 FIX (Jan 25, 2026): CRITICAL - Was killing expectancy
    # Data: Avg Win = 0.34R because VPOC exits at 0.3-0.5R MFE
    # Solution: Only exit near VPOC if we have REAL profit (0.85R+)
    # =========================================================================
    VPOC_EXIT_ENABLED = True
    
    # Distance to VPOC/resistance to trigger exit (as % of price)
    VPOC_PROXIMITY_PCT = 0.0005  # 🔥 FIX (Jan 25): 0.05% = much tighter (only exit if VERY close)
    
    # Minimum profit required before VPOC exit
    VPOC_EXIT_MIN_MFE_R = 0.85  # 🔥 FIX (Jan 25): Need 0.85R profit minimum (was 0.6R)
    
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
    # 🔥 FIX (Jan 27, 2026): Tightened from 0.5 to 0.4 to avoid premature exits
    # 49% width was triggering exit at +0.23R when trade still had room
    BB_SQUEEZE_RATIO = 0.4  # BB width < 40% of average = squeeze (was 50%)
    
    # Take profit if in squeeze with small profit
    # 🔥 FIX (Jan 27, 2026): Raised min MFE from 0.2R to 0.3R
    # Don't exit at +0.23R, wait until at least +0.3R in squeeze
    SQUEEZE_EXIT_MIN_MFE_R = 0.3  # Exit if profit between 0.3R and 0.5R in squeeze (was 0.2R)
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
    # 🔧 FIX #25 (Feb 02, 2026): Aumentar a 0.50% - funding de 0.12% bloqueaba casi todo
    #    Kraken tiene funding rates más altos que Binance/Bybit
    #    0.79% ocurre en mercados bullish y no debería bloquear completamente
    ENABLED = True
    MAX_FUNDING_RATE_PCT = 0.50      # Entry threshold - block new LONGs above this (was 0.12)
    FORCE_EXIT_FUNDING_PCT = 0.80    # Force exit only at extreme funding (was 0.20)
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
    
    # =========================================================================
    # 🔥 NEW (Jan 25, 2026): FUNDING RATE AS DIRECTIONAL BIAS
    # Use funding rate to suggest trade direction - market imbalance signal
    # =========================================================================
    FUNDING_BIAS_THRESHOLD_SHORT = 0.0003   # > 0.03% → market favors SHORT
    FUNDING_BIAS_THRESHOLD_LONG = -0.0001   # < -0.01% → market favors LONG
    FUNDING_EXTREME_THRESHOLD = 0.0007      # > 0.07% → EXTREME, increase size
    FUNDING_BIAS_SCORE_BOOST = 0.5          # Score boost when funding aligns
    FUNDING_BIAS_SCORE_PENALTY = -0.5       # Score penalty when funding contradicts
    
    @classmethod
    def get_funding_directional_bias(cls, funding_rate: float) -> tuple:
        """
        Use funding rate as directional bias signal.
        
        Returns: (preferred_direction, confidence, is_extreme)
        - preferred_direction: 'SHORT', 'LONG', or 'NEUTRAL'
        - confidence: 0.0-1.0
        - is_extreme: True if funding is extreme (> 0.07%)
        """
        abs_rate = abs(funding_rate)
        is_extreme = abs_rate >= cls.FUNDING_EXTREME_THRESHOLD
        
        if funding_rate >= cls.FUNDING_BIAS_THRESHOLD_SHORT:
            # High positive funding = longs paying = bearish pressure
            confidence = min(1.0, funding_rate / cls.FUNDING_EXTREME_THRESHOLD)
            return ('SHORT', confidence, is_extreme)
        elif funding_rate <= cls.FUNDING_BIAS_THRESHOLD_LONG:
            # Negative funding = shorts paying = bullish pressure  
            confidence = min(1.0, abs(funding_rate) / cls.FUNDING_EXTREME_THRESHOLD)
            return ('LONG', confidence, is_extreme)
        
        return ('NEUTRAL', 0.0, False)
    
    @classmethod
    def get_funding_score_adjustment(cls, funding_rate: float, bias: str) -> float:
        """
        Get score adjustment based on funding alignment with trade direction.
        
        Returns: Score adjustment (-0.5 to +0.5)
        """
        preferred_dir, confidence, is_extreme = cls.get_funding_directional_bias(funding_rate)
        
        if preferred_dir == 'NEUTRAL':
            return 0.0
        
        # Funding aligns with our bias → boost score
        if preferred_dir == bias:
            adjustment = cls.FUNDING_BIAS_SCORE_BOOST * confidence
            if is_extreme:  # Extreme funding alignment → double boost
                adjustment *= 1.5
            return adjustment
        
        # Funding contradicts our bias → penalty
        adjustment = cls.FUNDING_BIAS_SCORE_PENALTY * confidence
        return adjustment


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
    # AUDITORÍA (Jan 22, 2026): RE-ENABLED as SIZING modifier (not blocker)
    # Punto 9: Use session quality for position sizing, not blocking
    # BTC trades 24/7 but session quality affects size, not permission
    ENABLED = True  # Re-enabled for sizing adjustment
    
    # Allow weekend trading - BTC is 24/7
    WEEKEND_TRADING = True
    
    # =========================================================================
    # SESSION SIZE MULTIPLIERS (NEW)
    # Instead of blocking, adjust position size by session quality
    # =========================================================================
    SESSION_SIZE_MULT = {
        'LONDON_NY_OVERLAP': 1.0,  # Best session = full size
        'LONDON': 1.0,             # Good liquidity
        'NY': 1.0,                 # Good liquidity
        'NY_OPEN_VOLATILE': 0.70,  # First 15min = volatile, reduce
        'ASIA': 0.80,              # Lower quality but still tradeable
        'DEAD_HOURS': 0.60,        # Low activity
        'WEEKEND': 0.65,           # Low liquidity
        'OTHER': 0.75,             # Default reduced
    }
    
    # =========================================================================
    # QUALITY → MULTIPLIER MAP (Feb 04, 2026 - AUDIT FIX)
    # Single source of truth for quality-based sizing
    # Used by: entry_orchestrator.py, simplified_sizing.py, edge_state_manager.py
    # =========================================================================
    QUALITY_SIZE_MULT = {
        'good': 1.0,       # Full size - LONDON, NY_OVERLAP, NY
        'moderate': 0.90,  # 🔥 FIX #121c: 0.80 → 0.90 - ASIA almost full size
        'poor': 0.0,       # BLOCKED - Dead hours, extreme times
    }
    
    # =========================================================================
    # 🔥 FIX #293 (Feb 18, 2026): SESSION NAME → QUALITY TIER MAPPING
    #
    # ROOT CAUSE: session_quality tag stored SESSION NAMES ("ASIA") instead of
    # quality tiers ("moderate"). QUALITY_SIZE_MULT.get("asia", 0.80) always
    # returned the 0.80 default, making 'poor':0.0 DEAD CODE.
    # The blocking path NEVER fired.
    #
    # This is the SINGLE SOURCE OF TRUTH for session→quality mapping.
    # =========================================================================
    SESSION_TO_QUALITY = {
        'LONDON_NY_OVERLAP': 'good',
        'LONDON': 'good',
        'NY': 'good',
        'NY_OPEN_VOLATILE': 'moderate',
        'ASIA': 'moderate',
        'DEAD_HOURS': 'poor',        # BLOCKED (0:00-7:59 UTC)
        'WEEKEND': 'moderate',
        'OTHER': 'moderate',
    }
    
    @classmethod
    def session_to_quality(cls, session_name: str) -> str:
        """Convert session name to quality tier ('good', 'moderate', 'poor')."""
        return cls.SESSION_TO_QUALITY.get(session_name.upper(), 'moderate')
    
    @classmethod
    def get_session_size_mult(cls, session_name: str) -> float:
        """Get position size multiplier for current session."""
        return cls.SESSION_SIZE_MULT.get(session_name.upper(), 0.75)
    
    @classmethod
    def get_quality_size_mult(cls, quality: str) -> float:
        """
        Get position size multiplier for session quality.
        
        SINGLE SOURCE OF TRUTH - use this instead of hardcoded dicts.
        
        Args:
            quality: 'good', 'moderate', or 'poor'
            
        Returns:
            Multiplier (0.0-1.0), where 0.0 means BLOCKED
        """
        return cls.QUALITY_SIZE_MULT.get(quality.lower(), 0.80)
    
    @classmethod
    def is_quality_blocked(cls, quality: str) -> bool:
        """Check if quality means trading is blocked."""
        return cls.get_quality_size_mult(quality) <= 0.0
    
    # Legacy configs (not used for blocking anymore)
    MIN_ADX_BY_SESSION = {
        'london': 20,
        'newyork': 18,
        'asia': 15,
        'weekend': 0
    }
    
    MIN_VOLATILITY_BPS = {
        'london': 80,
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


def get_session_name(hour: int, minute: int, day_of_week: int | None = None) -> str:
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
    
    # 🔥 FIX #293 (Feb 18, 2026): Dead hours = 0:00-7:59 UTC (real dead zone)
    # Root cause of overnight losses: "ASIA" covered 0-7 UTC with only 0.75x
    # multiplier. System took 3 losses between 02:00-06:00 UTC.
    # Split: 0-7 UTC = DEAD_HOURS (blocked), 21-23 UTC = ASIA (reduced)
    if hour < 8:
        return "DEAD_HOURS"
    
    # Asia/Late session: 21:00-23:59 UTC (post-NY, reduced liquidity)
    if hour >= 21:
        return "ASIA"
    
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
    
    # 🔥 FIX (Jan 22, 2026): Size multiplier by volatility regime
    SIZE_MULTIPLIER = {
        "LOW_VOL": 1.0,      # Normal size in low vol (tighter SL compensates)
        "NORMAL": 1.0,       # Standard
        "HIGH_VOL": 0.7,     # Reduce size in high vol
        "SPIKE": 0.5,        # Much smaller in spikes (risk management)
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
    def get_size_multiplier(cls, regime: str) -> float:
        """Get position size multiplier based on volatility regime."""
        return cls.SIZE_MULTIPLIER.get(regime, 1.0)
    
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
    # FIX (Jan 24, 2026): Lowered all thresholds - bot was at 0 trades in 9h
    # 🔥 FIX (Feb 04, 2026): Added TRANSITION explicitly with low threshold
    # 🔥 FIX #211 (Feb 12, 2026): Lowered ALL thresholds to allow exploitation
    # PROBLEMA: EDGE_FLOOR = 4.0 pero threshold >= 4.5 → nunca entra
    # SOLUCIÓN: Threshold = 4.0 base (momentum adjustments still apply)
    REGIME_THRESHOLD = {
        "RANGE": 4.5,            # 🔥 FIX #211: Was 5.0 - can't reach with floor 4.0
        "TREND": 4.0,            # 🔥 FIX #211: Was 4.5 - momentum should exploit
        "TRANSITION": 4.0,       # 🔥 FIX #211: Was 4.5 - transition edge is validated
        "TREND_EXPANSION": 4.0,  # 🔥 FIX #211: Was 5.0 - breakouts need FAST entry
        "TREND_EXHAUSTION": 5.0, # 🔥 FIX #211: Was 5.5 - still be careful
        "CONSOLIDATION": 4.5,    # 🔥 FIX #211: Was 5.0
    }
    
    # Session adjustment (added to base threshold)
    # FIX (Jan 24, 2026): Reduced penalties, bot was paralyzed
    SESSION_ADJUSTMENT = {
        "ASIA": 0.25,          # Was 0.5
        "LONDON": 0.0,         # No adjustment
        "NY": 0.0,             # No adjustment
        "LONDON_NY_OVERLAP": -0.5,  # -0.5 in best session
        "NY_OPEN_VOLATILE": 0.25,   # Was 0.5
        "DEAD_HOURS": 0.5,     # Was 1.0
        "WEEKEND": 0.25,       # Was 0.5
        "OTHER": 0.0,
    }
    
    # Fallback for discovery mode
    # FIX (Jan 24, 2026): Lowered from 5.0 - bot was at 0 trades in 9h
    DISCOVERY_BASE = 4.0
    
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
        # 🔥 FIX #211 (Feb 12, 2026): Lowered minimum from 5.0 to 4.0
        # EDGE_FLOOR = 4.0, threshold must allow trading at floor
        return max(4.0, min(8.0, threshold))


# ============================================================================
# SESSION TRADE LIMITS
# ============================================================================
class SessionTradeLimits:
    """
    Limit trades per session to prevent overtrading.
    High-score setups can override.
    
    🔥 FIX (Jan 22, 2026): Limits vary by regime_mode
    TREND: More trades allowed (4)
    RANGE: Fewer trades allowed (3)
    """
    ENABLED = True
    
    # Max trades per session (DEFAULT - adjusted by regime)
    # 🔥 FIX #10: Increased base to 3 for Hot Hand system
    MAX_TRADES_PER_SESSION = {
        "ASIA": 3,
        "LONDON": 3,
        "NY": 3,
        "LONDON_NY_OVERLAP": 4,
        "NY_OPEN_VOLATILE": 1,  # Just 1 in first 15 min
        "DEAD_HOURS": 1,
        "WEEKEND": 2,
        "OTHER": 3,
    }
    
    # 🔥 NEW: Mode-based multipliers
    # 🔥 FIX #10: RANGE mode cap increased to 3 (was 2)
    MODE_TRADE_LIMIT_CAP = {
        "TREND": 4,      # Max 4 trades per session in TREND
        "RANGE": 3,      # 🔥 FIX #10: Max 3 trades base in RANGE (Hot Hand can extend)
        "BREAKOUT": 4,   # Max 4 trades per session in BREAKOUT
        "NO_TRADE": 0,   # No trades allowed
    }
    
    # Override threshold: if score >= this, allow trade even if at limit
    OVERRIDE_SCORE_THRESHOLD = 7.5
    # 🔥 NEW: Even stricter threshold in RANGE (more selective)
    OVERRIDE_IF_SCORE_GTE = 7.5  # Alias for compatibility
    OVERRIDE_SCORE_THRESHOLD_RANGE = 8.5  # 🔥 FIX: 8.5 in RANGE (was 8.0)
    
    # Max trades per day (across all sessions)
    # PASO 5 — REDUCE EJECUCIONES (NO TRADES): target 2–4 trades/day
    MAX_TRADES_PER_DAY = 4
    
    @classmethod
    def get_limit(cls, session: str, regime_mode: str | None = None) -> int:
        base_limit = cls.MAX_TRADES_PER_SESSION.get(session.upper(), 2)
        if regime_mode:
            mode_cap = cls.MODE_TRADE_LIMIT_CAP.get(regime_mode, 5)
            return min(base_limit, mode_cap)
        return base_limit
    
    @classmethod
    def can_override(cls, score: float, regime_mode: str | None = None) -> bool:
        threshold = cls.OVERRIDE_SCORE_THRESHOLD
        if regime_mode == 'RANGE':
            threshold = cls.OVERRIDE_SCORE_THRESHOLD_RANGE
        return score >= threshold


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
        # Administrative/smart exits - not edge failures
        "VOLATILITY_SQUEEZE_EXIT": 0.0,  # Protective exit, not loss
        "EXCHANGE_STOP_LOSS": 0.5,       # SL hit on exchange (half count)
        "EXCHANGE_TAKE_PROFIT": 0.0,     # TP hit on exchange (profit)
        "FUNDING_EXIT": 0.0,             # Funding-based exit (administrative)
        "DEFENSIVE_EXIT": 0.25,          # Defensive mode exit (small count)
        "STRUCTURE_BREAK_EXIT": 0.25,    # Structure changed (small count)
        # 🔥 FIX (Feb 1, 2026): Administrative closes - NOT edge failures
        # Must match state_machine.py behavior ("NOT counted as loss")
        "EMERGENCY_ANOMALY": 0.0,        # System anomaly, not edge failure
        "PRICE_GAP_ANOMALY": 0.0,        # Flash crash protection
        "SPREAD_ANOMALY": 0.0,           # Liquidity protection
        "FEED_STALE_ANOMALY": 0.0,       # Connection protection
        "VOLATILITY_ANOMALY": 0.0,       # ATR explosion protection
        "HARD_BACKSTOP": 0.25,           # Catastrophic protection (small count)
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
    
    FIX (Jan 22, 2026): Punto 10 - Penalización gradual en zona 20-30 ADX
    """
    ENABLED = True
    
    # ADX thresholds
    STRONG_TREND_ADX = 30   # ADX >= 30 = strong trend
    WEAK_TREND_ADX = 20     # ADX <= 20 = weak/ranging
    MID_TREND_ADX_LOW = 20  # Start of mid zone
    MID_TREND_ADX_HIGH = 25 # End of "high penalty" mid zone
    
    # Behavior in strong trend (ADX >= 30)
    STRONG_TREND_ACTION = "OVERRIDE"   # Override block, just warn
    STRONG_TREND_PENALTY = 0           # No penalty (pullbacks are opportunities)
    
    # Behavior in weak trend (ADX <= 20)
    WEAK_TREND_ACTION = "BLOCK"        # Hard block
    WEAK_TREND_PENALTY = 2             # -2 score if somehow bypassed
    
    # Behavior in mid-low zone (20 < ADX <= 25)
    MID_LOW_TREND_ACTION = "PENALTY"
    MID_LOW_TREND_PENALTY = 1.5        # -1.5 score
    
    # Behavior in mid-high zone (25 < ADX < 30)
    MID_HIGH_TREND_ACTION = "PENALTY"
    MID_HIGH_TREND_PENALTY = 0.5       # -0.5 score (minor)
    
    # Legacy aliases
    MID_TREND_ACTION = "PENALTY"
    MID_TREND_PENALTY = 1


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


# ============================================================================
# EXIT HIERARCHY CONFIG (Jan 23, 2026)
# Jerarquía correcta de reglas de salida - PRIORIDAD CLARA
# ============================================================================
class ExitHierarchy:
    """
    Define la jerarquía de reglas de salida por prioridad.
    
    Orden de prioridad (1 = máxima, manda sobre todo):
    1. HARD_STOP (invalidation stop): siempre manda, no negociable
    2. RISK_CAP (max loss dinámico): segunda capa de protección
    3. STRUCTURE_MANAGEMENT: gestión por estructura (breakdown/reclaim)
    4. TIME_LOGIC: nunca cierra a ciegas, cambia modo de gestión
    5. TAKE_PROFIT: salidas planificadas (VPOC, TP, parciales)
    
    CRITICAL: TIME_INVALIDATION NUNCA puede cerrar un trade con más pérdida
    que el límite configurado. Si el trade está en -0.25R, TIME no lo puede
    empeorar a -0.6R esperando.
    """
    
    # =========================================================================
    # 1. PRIORITY LEVELS
    # =========================================================================
    PRIORITY_HARD_STOP = 1      # Invalidation stop - absolute
    PRIORITY_RISK_CAP = 2       # Dynamic loss cap
    PRIORITY_STRUCTURE = 3      # Structure-based exits
    PRIORITY_TIME_LOGIC = 4     # Time-based evaluations
    PRIORITY_TAKE_PROFIT = 5    # Planned profit exits
    
    # =========================================================================
    # 2. TIME_INVALIDATION INTELIGENTE (Jan 23, 2026)
    # Convierte timeout en checkpoint, no gatillo ciego
    # =========================================================================
    TIME_INVALIDATION_ENABLED = True
    
    # A) Timeout = checkpoint, no cierre automático
    # En timeout, evalúa si hay invalidación ESTRUCTURAL real
    TIME_AS_CHECKPOINT = True
    
    # B) Candles para el checkpoint (no para cierre)
    TIME_CHECKPOINT_CANDLES = 6  # Después de 6 velas, hacer evaluación
    
    # C) REGLA CRÍTICA: Max loss por TIME_INVALIDATION
    # Si timeout y PnL <= -0.25R → cerrar inmediatamente
    # Si timeout y PnL > -0.25R → evaluar tesis, no cerrar peor
    # 🔥 FIX (Jan 24, 2026): Use AsymmetryConfig as single source of truth
    TIME_INVALIDATION_MAX_LOSS_R = AsymmetryConfig.TIME_INVALIDATION_MAX_LOSS_R  # Nunca cerrar peor que esto
    
    # D) Comportamiento según estado de P&L en timeout
    TIME_IN_PROFIT_ACTION = "PARTIAL_AND_RUNNER"  # Reduce + dejar runner
    TIME_IN_PROFIT_PARTIAL_PCT = 0.6              # 60% sale, 40% runner
    
    TIME_FLAT_ACTION = "REDUCE_RISK"              # Ajustar stop, no cerrar
    TIME_FLAT_MOVE_STOP_R = -0.15                 # Mover stop a -0.15R max
    
    TIME_IN_LOSS_ACTION = "EVALUATE_THESIS"       # Evaluar tesis antes de cerrar
    TIME_IN_LOSS_MAX_R = -0.25                    # Límite absoluto
    
    # E) Invalidación estructural REAL (no numérica)
    # SHORT invalida si: reclaim VWAP + aceptación, rotura LH, recuperar VAH
    STRUCTURAL_INVALIDATION_CONDITIONS = [
        "VWAP_RECLAIM_ACCEPTANCE",  # X velas cerrando por encima de VWAP
        "LAST_LH_BROKEN",           # Rotura de último Lower High
        "VAH_POC_RECLAIMED",        # Recuperar VAH/POC con aceptación
        "HTF_REGIME_FLIP",          # Cambio de régimen HTF
    ]
    
    VWAP_ACCEPTANCE_CANDLES = 3   # N velas para confirmar aceptación
    
    # =========================================================================
    # 3. VPOC_EXIT COMO PARCIAL (Jan 23, 2026)
    # VPOC_EXIT = TP1 (parcial) + runner, NO cierre total
    # =========================================================================
    VPOC_EXIT_AS_PARTIAL = True
    VPOC_PARTIAL_CLOSE_PCT = 0.70   # Cerrar 70% en VPOC
    VPOC_RUNNER_PCT = 0.30          # Dejar 30% como runner
    VPOC_RUNNER_STOP_R = 0.0        # Stop del runner a BE (0R)
    VPOC_MIN_MFE_FOR_RUNNER = 0.5   # Solo dejar runner si MFE >= 0.5R
    
    # =========================================================================
    # 4. ANTI RE-ENTRY SIN NUEVO EDGE (Jan 23, 2026)
    # Prohibir reentrar en mismo rango de precio sin nueva información
    # =========================================================================
    ANTI_REENTRY_ENABLED = True
    
    # Rango de precio "mismo setup" (en ATR)
    SAME_PRICE_RANGE_ATR = 0.2   # ±0.2 ATR = mismo precio
    
    # Cooldown por precio, no por tiempo
    REQUIRE_NEW_EDGE_FOR_REENTRY = True
    
    # Condiciones que permiten reentrar:
    NEW_EDGE_CONDITIONS = [
        "NEW_LH_CONFIRMED",         # Nuevo Lower High confirmado
        "BREAKDOWN_RETEST",         # Breakdown + retest de estructura
        "NEW_POC_DISPLACEMENT",     # Nuevo POC / desplazamiento de valor
        "VOLUME_REGIME_CHANGE",     # Cambio de régimen de volumen
    ]
    
    # Trackear última entrada para comparar
    TRACK_LAST_ENTRY_PRICE = True
    TRACK_LAST_ENTRY_CONDITIONS = True
    
    # =========================================================================
    # 5. WORST-TRADE PREVENTION (Jan 23, 2026)
    # "Si no invalida la tesis, no puede ser el peor trade del día"
    # =========================================================================
    # 🔥 SIMPLIFIED MODE (Jan 27, 2026): Check SimplifiedFeatureFlags first
    # If in simplified mode, WTP is DISABLED to measure true edge
    @classmethod
    def is_wtp_enabled(cls) -> bool:
        """Check if WTP should be enabled based on mode"""
        try:
            from src.config.simplified_sizing import SimplifiedFeatureFlags
            if SimplifiedFeatureFlags.DISABLE_WTP:
                return False
        except ImportError:
            pass
        return cls.WORST_TRADE_PREVENTION_ENABLED
    
    WORST_TRADE_PREVENTION_ENABLED = True  # Legacy default (override via simplified_sizing)
    
    # Si trade está dentro de tesis (no invalida estructura):
    # - Pérdida máxima = hard stop O cap de -0.25R
    # - Lo que llegue primero
    
    # Si llega a -0.35R ANTES de invalidar estructuralmente:
    # - El setup no era el correcto → salir
    # 🔥 FIX (Jan 27, 2026): Raised from -0.25R to -0.35R
    # Reason: -0.25R was too tight considering fees (~0.08R round trip)
    # This caused premature exits before trades had chance to work
    THESIS_INTACT_MAX_LOSS_R = -0.35  # Nunca perder más si tesis válida
    
    # Evaluar "dentro de tesis" cada candle
    EVALUATE_THESIS_EACH_CANDLE = True
    
    @classmethod
    def get_time_action_for_pnl(cls, current_r: float) -> dict:
        """
        Determina acción en timeout según P&L actual.
        
        Returns:
            dict con 'action', 'details'
        """
        if current_r >= 0.1:
            # En profit: parcial + runner
            return {
                'action': cls.TIME_IN_PROFIT_ACTION,
                'partial_pct': cls.TIME_IN_PROFIT_PARTIAL_PCT,
                'allow_close': False,  # No cerrar todo
                'reason': f'Profit {current_r:.2f}R - keeping runner'
            }
        elif current_r >= cls.TIME_INVALIDATION_MAX_LOSS_R:
            # Flat o pérdida pequeña: evaluar tesis
            return {
                'action': cls.TIME_IN_LOSS_ACTION,
                'evaluate_structure': True,
                'allow_close': False,  # Solo cerrar si estructura invalida
                'max_loss': cls.TIME_IN_LOSS_MAX_R,
                'reason': f'Flat/small loss {current_r:.2f}R - evaluating thesis'
            }
        else:
            # En pérdida > límite: cerrar inmediatamente
            return {
                'action': 'CLOSE_IMMEDIATELY',
                'allow_close': True,
                'reason': f'Loss {current_r:.2f}R exceeds limit {cls.TIME_INVALIDATION_MAX_LOSS_R}R'
            }


# ============================================================================
# COUNTERTREND MODE CONFIG (Jan 23, 2026)
# Reglas específicas para modo CT - stop quirúrgico + target ambicioso
# ============================================================================
class CountertrendConfig:
    """
    Modo CT = fade táctico, NO countertrend puro.
    Requiere: stop quirúrgico + target más ambicioso (o parcial).
    
    Size CT: subir a 40-50% solo si:
    - Stop efectivo (incluyendo slippage) está por debajo de un umbral
    - Volatilidad no está en expansión extrema
    
    RR CT: permitir 1.5-2R pero vía runner, no obligatoriamente TP fijo.
    Límite CT: "1 por contexto" = mismo balance area / sesión / zona de valor
    """
    ENABLED = True
    
    # =========================================================================
    # SIZING CT - Más agresivo solo bajo condiciones óptimas
    # =========================================================================
    CT_BASE_SIZE_MULTIPLIER = 0.35     # Base: 35% del size normal
    CT_OPTIMAL_SIZE_MULTIPLIER = 0.50  # Óptimo: 50% si condiciones met
    
    # Condiciones para size óptimo (50%)
    CT_OPTIMAL_CONDITIONS = {
        'max_stop_distance_atr': 1.0,  # Stop < 1 ATR
        'max_volatility_ratio': 1.2,   # Volatilidad < 120% promedio
        'min_adx': 15,                 # ADX mínimo (tendencia presente)
        'max_adx': 35,                 # ADX máximo (no sobreextendido)
    }
    
    # =========================================================================
    # TARGETS CT - Más ambiciosos que trend
    # =========================================================================
    CT_TP1_R = 1.0                    # TP1 a 1R (igual que trend)
    CT_TP2_R = 2.0                    # TP2 a 2R (runner objetivo)
    CT_RUNNER_TARGET_R = 2.5          # Target ideal del runner
    
    # Parciales CT
    CT_TP1_CLOSE_PCT = 0.50           # 50% en TP1
    CT_RUNNER_PCT = 0.50              # 50% como runner
    
    # =========================================================================
    # STOP CT - Quirúrgico (más tight que trend)
    # =========================================================================
    CT_STOP_ATR_MULTIPLIER = 0.8      # 0.8 ATR (vs 1.0-1.2 en trend)
    CT_STOP_MAX_R = 0.6               # Max 0.6R de stop (tight)
    
    # =========================================================================
    # LÍMITES CT - 1 por contexto
    # =========================================================================
    CT_MAX_PER_BALANCE_AREA = 1       # Max 1 CT por balance area
    CT_MAX_PER_SESSION = 2            # Max 2 CT por sesión
    CT_COOLDOWN_AFTER_LOSS_MIN = 15   # 15 min cooldown tras CT perdedor
    
    # =========================================================================
    # BE LOGIC CT - Diferente a trend
    # =========================================================================
    CT_MOVE_BE_AFTER_R = 0.6          # Mover BE después de +0.6R (no 0.4)
    CT_BE_BUFFER_R = 0.1              # BE a entry + 0.1R (lock some profit)
    
    @classmethod
    def get_ct_size_multiplier(cls, stop_distance_atr: float, 
                                volatility_ratio: float, adx: float) -> float:
        """
        Calcula multiplier de size para CT basado en condiciones.
        """
        conditions = cls.CT_OPTIMAL_CONDITIONS
        
        # Verificar todas las condiciones óptimas
        stop_ok = stop_distance_atr <= conditions['max_stop_distance_atr']
        vol_ok = volatility_ratio <= conditions['max_volatility_ratio']
        adx_ok = conditions['min_adx'] <= adx <= conditions['max_adx']
        
        if stop_ok and vol_ok and adx_ok:
            return cls.CT_OPTIMAL_SIZE_MULTIPLIER
        elif stop_ok and vol_ok:
            # Sin ADX óptimo pero otras condiciones OK
            return (cls.CT_BASE_SIZE_MULTIPLIER + cls.CT_OPTIMAL_SIZE_MULTIPLIER) / 2
        else:
            return cls.CT_BASE_SIZE_MULTIPLIER
