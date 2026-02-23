"""
Market Regime Module - Market state classification and regime detection.

╔══════════════════════════════════════════════════════════════════════════════╗
║ 📦 ACTIVE COMPONENTS                                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ • TrendIntelligence (trend_intelligence.py) - Trend cycle detection          ║
║ • EdgeRegimeClassifier (edge_regime_classifier.py) - Edge validity           ║
║ • RegimeDetector (regime_detector.py) - Entry-time regime                    ║
║ • ActivityRegimeAnalyzer (activity_regime.py) - 24/7 activity analysis       ║
║ • ManagementRegime (market_regime.py) - Position management guidance         ║
╚══════════════════════════════════════════════════════════════════════════════╝

DELETED Feb 05, 2026:
- trading_regime.py (TradingRegimeClassifier) → Use TrendIntelligence
- regime_context.py (RegimeContextManager) → Use TrendIntelligence
"""

# =============================================================================
# ✅ ACTIVE COMPONENTS
# =============================================================================

# Trend Intelligence - Cycle Detection (PRIMARY for trend analysis)
from src.domain.market.regime.trend_intelligence import (
    TrendIntelligence,
    TrendPhase,
    TrendDirection as TrendIntelDirection,
    TrendState,
    TrendTransition,
    get_trend_intelligence,
)

# Edge Regime - Edge validity classification (PRIMARY for edge analysis)
from src.domain.market.regime.edge_regime_classifier import (
    EdgeRegimeClassifier,
    EdgeRegime,
    RegimeMetrics,
)

# Entry-time regime detection (PRIMARY for entry decisions)
from src.domain.market.regime.regime_detector import (
    RegimeDetector,
    EntryRegime,
    RegimeConfig,
    REGIME_CONFIGS,
)

# Activity regime for 24/7 markets (used via edge enforcement)
from src.domain.market.regime.activity_regime import (
    ActivityRegimeAnalyzer,
    EnforcementMode,
    ActivityMetrics,
)

# Position management regime - Used by risk_middleware.py
from src.domain.market.regime.management_regime import (
    ManagementRegime,
    RegimeGuidance,
    REGIME_GUIDANCE,
    get_regime_classifier,
    get_regime_risk_multiplier,
    classify_market_regime,
)

# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Trend Intelligence
    'TrendIntelligence',
    'TrendPhase',
    'TrendIntelDirection',
    'TrendState',
    'TrendTransition',
    'get_trend_intelligence',
    # Edge Regime
    'EdgeRegimeClassifier',
    'EdgeRegime',
    'RegimeMetrics',
    # Entry regime
    'RegimeDetector',
    'EntryRegime',
    'RegimeConfig', 
    'REGIME_CONFIGS',
    # Activity regime
    'ActivityRegimeAnalyzer',
    'EnforcementMode',
    'ActivityMetrics',
    # Management regime (used by risk_middleware)
    'ManagementRegime',
    'RegimeGuidance',
    'REGIME_GUIDANCE',
    'get_regime_classifier',
    'get_regime_risk_multiplier',
    'classify_market_regime',
]
