"""
Risk Domain - Risk management business logic.

Clean Architecture Layer: DOMAIN
- Contains pure business logic for risk calculations
- No infrastructure dependencies
- Re-exports from legacy engine/ for backward compatibility

Migration Status: ALIAS (re-exports from src.engine.risk)
Target: Move logic here, make engine/ re-export from here
"""

# Position class extracted to position.py for maintainability
from src.domain.risk.position import (
    Position,
    REGIME_NORMAL,
    REGIME_RUNNER,
    RUNNER_PROMOTION_MFE,
    RUNNER_TIME_EXIT_CANDLES,
    normalize_regime_mode,
)
from src.domain.risk.risk_engine import RiskEngine
from src.domain.risk.risk_profile import (
    RiskProfile,
    RiskAssessment,
    assess_risk,
)

__all__ = [
    # Risk Engine
    'RiskEngine',
    'Position',
    'REGIME_NORMAL',
    'REGIME_RUNNER',
    'RUNNER_PROMOTION_MFE',
    'RUNNER_TIME_EXIT_CANDLES',
    'normalize_regime_mode',
    # Profile
    'RiskProfile',
    'RiskAssessment',
    'assess_risk',
]
