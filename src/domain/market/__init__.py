"""
Market Domain - Market analysis and regime classification.

Clean Architecture Layer: DOMAIN
- Contains market analysis business logic
- Regime detection, trend intelligence, filters
- No infrastructure dependencies

Migration Status: ALIAS (re-exports from src.engine.regime)
Target: Move logic here, make engine/ re-export from here

Components:
- RegimeContext: SINGLE SOURCE OF TRUTH for regime
- TrendIntelligence: Trend cycle detection (BIRTH → ACCEL → MATURE → EXHAUST → DEATH)
- RegimeDetector: Entry-time regime classification
- Filters: Market condition filters
"""

# Regime Context - SINGLE SOURCE OF TRUTH
from src.domain.market.regime.regime_context import (
    RegimeMode,
    RegimeContext,
    RegimeContextManager,
    get_regime_context_manager,
    get_current_regime_context,
    get_trailing_regime,
)

# Trend Intelligence - Cycle Detection
from src.domain.market.regime.trend_intelligence import (
    TrendIntelligence,
    TrendPhase,
    TrendDirection as TrendIntelDirection,
    TrendState,
    TrendTransition,
    get_trend_intelligence,
    reset_trend_intelligence,
)

# Entry-time regime detection
from src.domain.market.regime.regime_detector import (
    RegimeDetector,
    EntryRegime,
    RegimeConfig,
    REGIME_CONFIGS,
    calculate_chase_atr,
)

# Position management regime
from src.domain.market.regime.market_regime import (
    ManagementRegime,
    ManagementRegimeClassifier,
    RegimeGuidance,
    REGIME_GUIDANCE,
    get_regime_classifier,
    get_regime_risk_multiplier,
    classify_market_regime,
)

# Edge-based regime
from src.domain.market.regime.edge_regime_classifier import (
    EdgeRegimeClassifier,
    EdgeRegime,
    RegimeMetrics,
)

# Activity regime for 24/7 markets
from src.domain.market.regime.activity_regime import (
    ActivityRegimeAnalyzer,
    EnforcementMode,
    ActivityMetrics,
)

# Trading regime
from src.domain.market.regime.trading_regime import (
    TradingRegime,
    TradingRegimeClassifier,
    TrendDirection,
)

__all__ = [
    # Regime Context - SINGLE SOURCE OF TRUTH
    'RegimeMode',
    'RegimeContext',
    'RegimeContextManager',
    'get_regime_context_manager',
    'get_current_regime_context',
    'get_trailing_regime',
    # Entry regime
    'RegimeDetector',
    'EntryRegime',
    'RegimeConfig', 
    'REGIME_CONFIGS',
    'calculate_chase_atr',
    # Trend Intelligence
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
