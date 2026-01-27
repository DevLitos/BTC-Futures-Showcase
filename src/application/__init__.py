"""
Application Layer - Use Cases and Orchestration

Clean Architecture Layer: APPLICATION
This layer contains:
- Use cases that orchestrate domain logic
- Application services that coordinate between layers
- NO business logic (that belongs in domain)
- NO infrastructure details (that belongs in infrastructure)

Migration Status: ALIAS (re-exports from src.engine)
Target: Gradually move orchestration logic here

Application services:
- SignalEngine: Main trading orchestration
- SynchronizedMultiTimeframeBuffer: Market data buffer
"""

# Re-export main trading application components
from src.application.live.trading_app import SignalEngine
from src.application.services.market_data import SynchronizedMultiTimeframeBuffer

__all__ = [
    'SignalEngine',
    'SynchronizedMultiTimeframeBuffer',
]
