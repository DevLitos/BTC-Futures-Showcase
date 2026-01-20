"""
Validation module - Research harness for strategy validation.

Provides:
- Walk-forward cross-validation
- Ablation testing
- Statistical significance tests
- Overfitting detection
"""

from .walk_forward import (
    WalkForwardValidator, WalkForwardResult, FoldResult,
    ValidationCriteria, ValidationStatus, AblationResult,
    get_walk_forward_validator, reset_walk_forward_validator
)

__all__ = [
    'WalkForwardValidator', 'WalkForwardResult', 'FoldResult',
    'ValidationCriteria', 'ValidationStatus', 'AblationResult',
    'get_walk_forward_validator', 'reset_walk_forward_validator',
]
