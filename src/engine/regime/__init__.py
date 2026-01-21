"""
Regime Module - Market regime classification and detection

Reorganized from engine/ (Jan 20, 2026)

Components:
- RegimeDetector: Entry-time regime (TREND_EXPANSION, RANGE, CHAOS, TREND_EXHAUSTION)
- ManagementRegimeClassifier: Position management regime (TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD)
- EdgeRegimeClassifier: Edge-based regime (REGIME_A, B, C, D)
- ActivityRegimeAnalyzer: Activity-based regime for 24/7 markets
- TradingRegimeClassifier: Trading-specific regime analysis
"""

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
    # Entry regime
    'RegimeDetector',
    'EntryRegime',
    'RegimeConfig', 
    'REGIME_CONFIGS',
    'calculate_chase_atr',
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
