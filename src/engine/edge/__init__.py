"""
Edge Module - Edge enforcement, validation, and recovery

Reorganized from engine/ (Jan 20, 2026)

Components:
- EdgeEnforcement: Pre-trade validation & degradation monitoring
- EdgeValidator: Context validity and edge requirements
- EdgeRecoveryProtocol: Recovery procedures after edge degradation
"""

from src.engine.edge.edge_enforcement import EdgeEnforcement
from src.engine.edge.edge_validator import (
    EdgeValidator,
    ContextValidity,
)
from src.engine.edge.edge_recovery_protocol import (
    EdgeRecoveryProtocol,
    RecoveryPhase,
    RecoveryProgress,
    RecoveryRequirements,
    RecoveryAttempt,
)

__all__ = [
    'EdgeEnforcement',
    'EdgeValidator',
    'ContextValidity',
    'EdgeRecoveryProtocol',
    'RecoveryPhase',
    'RecoveryProgress',
    'RecoveryRequirements',
    'RecoveryAttempt',
]
