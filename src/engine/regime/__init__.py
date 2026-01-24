"""
Regime Module - Market regime classification and detection

Reorganized from engine/ (Jan 20, 2026)

Components:
- RegimeContext: SINGLE SOURCE OF TRUTH for regime (Jan 22, 2026)
- RegimeDetector: Entry-time regime (TREND_EXPANSION, RANGE, CHAOS, TREND_EXHAUSTION)
- ManagementRegimeClassifier: Position management regime (TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD)
- EdgeRegimeClassifier: Edge-based regime (REGIME_A, B, C, D)
- ActivityRegimeAnalyzer: Activity-based regime for 24/7 markets
- TradingRegimeClassifier: Trading-specific regime analysis
- TrendIntelligence: Detección de INICIO/FIN de tendencias (Jan 23, 2026)

🔥 FIX (Jan 22, 2026): Added RegimeContext as single source of truth
All modules should read from RegimeContext instead of computing their own regime.

🔥 NEW (Jan 23, 2026): TrendIntelligence for cycle detection
Detects trend birth, acceleration, exhaustion, death - not just current state.
"""

# 🔥 NEW: Regime Context - SINGLE SOURCE OF TRUTH (Jan 22, 2026)
from src.engine.regime.regime_context import (
    RegimeMode,
    RegimeContext,
    RegimeContextManager,
    get_regime_context_manager,
    get_current_regime_context,
    get_trailing_regime,
)

# 🔥 NEW: Trend Intelligence - Cycle Detection (Jan 23, 2026)
from src.engine.regime.trend_intelligence import (
    TrendIntelligence,
    TrendPhase,
    TrendDirection as TrendIntelDirection,
    TrendState,
    TrendTransition,
    get_trend_intelligence,
    reset_trend_intelligence,
)

# Entry-time regime detection
from src.engine.regime.regime_detector import (
    RegimeDetector,
    EntryRegime,
    RegimeConfig,
    REGIME_CONFIGS,
    calculate_chase_atr,
)

# Position management regime
from src.engine.regime.market_regime import (
    ManagementRegime,
    ManagementRegimeClassifier,
    RegimeGuidance,
    REGIME_GUIDANCE,
    get_regime_classifier,
    get_regime_risk_multiplier,
    classify_market_regime,
)

# Edge-based regime
from src.engine.regime.edge_regime_classifier import (
    EdgeRegimeClassifier,
    EdgeRegime,
    RegimeMetrics,
)

# Activity regime for 24/7 markets
from src.engine.regime.activity_regime import (
    ActivityRegimeAnalyzer,
    EnforcementMode,
    ActivityMetrics,
)

# Trading regime
from src.engine.regime.trading_regime import (
    TradingRegime,
    TradingRegimeClassifier,
    TrendDirection,
)

__all__ = [
    # 🔥 NEW: Regime Context - SINGLE SOURCE OF TRUTH
    'RegimeMode',
    'RegimeContext',
    'RegimeContextManager',
    'get_regime_context_manager',
    'get_current_regime_context',
    'get_trailing_regime',
    'RegimeDetector',
    'EntryRegime',
    'RegimeConfig', 
    'REGIME_CONFIGS',
    'calculate_chase_atr',
    # 🔥 NEW: Trend Intelligence (Jan 23, 2026)
    'TrendIntelligence',
    'TrendPhase',
    'TrendIntelDirection',
    'TrendState',
    'TrendTransition',
    'get_trend_intelligence',
    'reset_trend_intelligence',
    # Management regime
    'ManagementRegime',
    'ManagementRegimeClassifier',
    'RegimeGuidance',
    'REGIME_GUIDANCE',
    'get_regime_classifier',
    'get_regime_risk_multiplier',
    'classify_market_regime',
    # Edge regime
    'EdgeRegimeClassifier',
    'EdgeRegime',
    'RegimeMetrics',
    # Activity regime
    'ActivityRegimeAnalyzer',
    'EnforcementMode',
    'ActivityMetrics',
    # Trading regime
    'TradingRegime',
    'TradingRegimeClassifier',
    'TrendDirection',
]
