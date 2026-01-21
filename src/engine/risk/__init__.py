"""
Risk Module - Risk management engine and middleware

Reorganized from engine/ (Jan 20, 2026)

Components:
- RiskEngine: Core risk calculations and position sizing
- RiskManagerMiddleware: Risk-based filtering and adjustments
- RiskProfile: Risk profiles for different market conditions
"""

from src.engine.risk.risk_engine import RiskEngine
from src.engine.risk.risk_middleware import (
    RiskManagerMiddleware,
    SetupGrade,
    get_risk_middleware,
)
from src.engine.risk.risk_profile import (
    RiskProfile,
    RiskAssessment,
    assess_risk,
)

__all__ = [
    'RiskEngine',
    'RiskManagerMiddleware',
    'SetupGrade',
    'get_risk_middleware',
    'RiskProfile',
    'RiskAssessment',
    'assess_risk',
]
