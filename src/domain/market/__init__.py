"""
Market Domain - Market analysis and regime classification.

Clean Architecture Layer: DOMAIN
- Contains market analysis business logic
- Regime detection, trend intelligence, filters
- No infrastructure dependencies

Components:
- TrendIntelligence: Trend cycle detection (BIRTH → ACCEL → MATURE → EXHAUST → DEATH)
- RegimeDetector: Entry-time regime classification
- ManagementRegime: Position management guidance (used by risk_middleware)
- Filters: Market condition filters

DELETED Feb 05, 2026:
- trading_regime.py (TradingRegimeClassifier)
- regime_context.py (RegimeContextManager)
"""

# Trend Intelligence - Cycle Detection
from src.domain.market.regime.trend_intelligence import (
    TrendIntelligence,
    TrendPhase,
    TrendDirection as TrendIntelDirection,
    TrendState,
    TrendTransition,
    get_trend_intelligence,
)

# Entry-time regime detection
from src.domain.market.regime.regime_detector import (
    RegimeDetector,
    EntryRegime,
    RegimeConfig,
    REGIME_CONFIGS,
)

# Position management regime (used by risk_middleware.py)
from src.domain.market.regime.management_regime import (
    ManagementRegime,
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

__all__ = [
    # Entry regime
    'RegimeDetector',
    'EntryRegime',
    'RegimeConfig', 
    'REGIME_CONFIGS',
    # Trend Intelligence
    'TrendIntelligence',
    'TrendPhase',
    'TrendIntelDirection',
    'TrendState',
    'TrendTransition',
    'get_trend_intelligence',
    # Management regime
    'ManagementRegime',
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
]
