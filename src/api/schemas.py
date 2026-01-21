"""
API Schemas - Pydantic models for request/response validation

This module contains all Pydantic models used across API endpoints.
Centralized here to avoid duplication and ensure consistency.
"""

from datetime import datetime
from typing import Dict, Any, List, Optional
from enum import Enum

from pydantic import BaseModel, Field


# =============================================================================
# ENUMS
# =============================================================================

class TradeSide(str, Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, Enum):
    """Trade lifecycle status."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"


class AlertLevel(str, Enum):
    """Alert severity level."""
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    SEVERE = "SEVERE"


# =============================================================================
# BASE MODELS
# =============================================================================

class TimestampedModel(BaseModel):
    """Base model with timestamp."""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# =============================================================================
# TRADE MODELS
# =============================================================================

class TradeCreate(BaseModel):
    """Schema for creating a trade request."""
    side: TradeSide
    size_usd: float = Field(..., gt=0, le=10000, description="Position size in USD")
    entry_price: Optional[float] = Field(None, description="Limit price (None for market)")
    stop_loss_pct: float = Field(default=0.5, ge=0.1, le=5.0, description="Stop loss percentage")
    take_profit_pct: float = Field(default=1.0, ge=0.2, le=10.0, description="Take profit percentage")
    
    class Config:
        json_schema_extra = {
            "example": {
                "side": "LONG",
                "size_usd": 100.0,
                "stop_loss_pct": 0.5,
                "take_profit_pct": 1.5
            }
        }


class TradeInfo(BaseModel):
    """Complete trade information."""
    trade_id: str
    side: TradeSide
    status: TradeStatus
    size: float
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_r: Optional[float] = None
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None


# =============================================================================
# POSITION MODELS
# =============================================================================

class PositionInfo(BaseModel):
    """Current position information."""
    has_position: bool
    side: Optional[TradeSide] = None
    size: float = 0.0
    entry_price: Optional[float] = None
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    duration_minutes: float = 0.0


# =============================================================================
# METRICS MODELS
# =============================================================================

class PerformanceSummary(BaseModel):
    """Performance summary metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    total_pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0


class FilterStats(BaseModel):
    """Filter pipeline statistics."""
    filter_name: str
    checks: int = 0
    passes: int = 0
    blocks: int = 0
    pass_rate: float = 0.0
    avg_penalty: float = 0.0


# =============================================================================
# SYSTEM MODELS
# =============================================================================

class SystemStatus(BaseModel):
    """Overall system status."""
    status: str  # "healthy", "degraded", "unhealthy"
    trading_active: bool
    mode: str
    uptime_seconds: float
    last_trade_time: Optional[str] = None
    components: Dict[str, str] = {}


class ConfigInfo(BaseModel):
    """System configuration (safe to expose)."""
    symbol: str
    trading_mode: str
    risk_per_trade_pct: float
    max_position_size: float
    filters_enabled: List[str]


# =============================================================================
# RESPONSE WRAPPERS
# =============================================================================

class APIResponse(BaseModel):
    """Standard API response wrapper."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None


class PaginatedResponse(BaseModel):
    """Paginated list response."""
    items: List[Any]
    total: int
    page: int
    page_size: int
    has_more: bool


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Enums
    'TradeSide',
    'TradeStatus', 
    'AlertLevel',
    # Trade
    'TradeCreate',
    'TradeInfo',
    # Position
    'PositionInfo',
    # Metrics
    'PerformanceSummary',
    'FilterStats',
    # System
    'SystemStatus',
    'ConfigInfo',
    # Response
    'APIResponse',
    'PaginatedResponse',
]
