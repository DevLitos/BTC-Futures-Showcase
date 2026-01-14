"""
Strategy module - Trade intent generation and strategy logic.

Provides:
- Trade intent generation (separated from execution)
- Strategy state management
- Signal filtering and validation
"""

from .trade_intent import (
    TradeIntentGenerator, TradeIntentRecord,
    IntentType, IntentStatus, RejectionReason,
    get_trade_intent_generator, reset_trade_intent_generator
)

__all__ = [
    'TradeIntentGenerator', 'TradeIntentRecord',
    'IntentType', 'IntentStatus', 'RejectionReason',
    'get_trade_intent_generator', 'reset_trade_intent_generator',
]
