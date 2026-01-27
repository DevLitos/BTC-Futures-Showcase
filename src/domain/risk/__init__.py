"""
Risk Domain - Risk management business logic.

Clean Architecture Layer: DOMAIN
- Contains pure business logic for risk calculations
- No infrastructure dependencies
- Re-exports from legacy engine/ for backward compatibility

Migration Status: ALIAS (re-exports from src.engine.risk)
Target: Move logic here, make engine/ re-export from here
"""

# Re-export from legacy location for backward compatibility
from src.domain.risk.risk_engine import (
    RiskEngine,
    Position,
    REGIME_NORMAL,
    REGIME_RUNNER,
    RUNNER_PROMOTION_MFE,
    RUNNER_TIME_EXIT_CANDLES,
)
from src.domain.risk.risk_middleware import (
    RiskManagerMiddleware,
    SetupGrade,
    get_risk_middleware,
)
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
    # Middleware
    'RiskManagerMiddleware',
    'SetupGrade',
    'get_risk_middleware',
    # Profile
    'RiskProfile',
    'RiskAssessment',
    'assess_risk',
]
