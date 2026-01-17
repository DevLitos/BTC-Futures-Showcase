"""
Trading Modes - Specialized strategy modules that run independently

Each mode has its own:
- Activation conditions (context gate)
- Setup detection (trigger)
- Risk/exit rules
- Statistics tracking

Modes do NOT:
- Modify the main trend bias
- Interfere with the main trend engine
- Execute while a position is open

Current modes:
- HTFOverextensionExtractor: Fade overextended moves with momentum decay
"""

from modes.htf_overextension_extractor import (
    HTFOverextensionExtractor,
    HTFContext,
    LTFContext,
    OverextensionTradeIntent,
    get_htf_overextension_extractor,
)

__all__ = [
    'HTFOverextensionExtractor',
    'HTFContext',
    'LTFContext',
    'OverextensionTradeIntent',
    'get_htf_overextension_extractor',
]
