"""
Metrics Router - Trading metrics and analytics

Provides:
- Performance metrics (win rate, profit factor, etc.)
- Edge health monitoring
- Session statistics
- Real-time market data (from bot health bridge)
- Prometheus metrics endpoint

ARCHITECTURE:
    Most data comes from the health bridge (bot writes, API reads).
    Historical metrics may be read from storage files.
"""

from datetime import datetime, UTC
from typing import Dict, Any, List, Optional
from pathlib import Path
import logging
import json

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from src.bootstrap.dependencies import get_settings
from src.config.health_bridge import get_health_bridge

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# SCHEMAS
# =============================================================================

class PerformanceMetrics(BaseModel):
    """Trading performance metrics."""
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: Optional[float] = None
    period: str = "all"
    
    class Config:
        json_schema_extra = {
            "example": {
                "total_trades": 150,
                "win_rate": 0.55,
                "profit_factor": 1.8,
                "expectancy_r": 0.25,
                "avg_win_r": 1.2,
                "avg_loss_r": -0.8,
                "max_drawdown_pct": 5.5,
                "sharpe_ratio": 1.5,
                "period": "month"
            }
        }


class EdgeHealthMetrics(BaseModel):
    """Edge degradation monitoring metrics."""
    edge_score: float = 0.0
    mfe_mae_ratio: float = 0.0
    be_stop_percentage: float = 0.0
    recent_win_rate: float = 0.0
    alert_level: str = "NORMAL"  # NORMAL, WARNING, CRITICAL, SEVERE
    trades_in_window: int = 0
    
    class Config:
        json_schema_extra = {
            "example": {
                "edge_score": 7.5,
                "mfe_mae_ratio": 1.8,
                "be_stop_percentage": 15.0,
                "recent_win_rate": 0.52,
                "alert_level": "NORMAL",
                "trades_in_window": 20
            }
        }


class SessionStats(BaseModel):
    """Statistics for a trading session."""
    session: str
    trades: int = 0
    win_rate: float = 0.0
    avg_pnl_r: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    total_pnl_usd: float = 0.0


class MarketDataSnapshot(BaseModel):
    """Current market data snapshot."""
    symbol: str
    price: float
    bid: float = 0.0
    ask: float = 0.0
    spread_bps: float = 0.0
    volume_24h: float = 0.0
    timestamp: str
    source: str = "health_bridge"
    
    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "BTC/USD:USD",
                "price": 97000.0,
                "bid": 96995.0,
                "ask": 97005.0,
                "spread_bps": 1.03,
                "volume_24h": 15000000000.0,
                "timestamp": "2026-01-20T22:00:00Z",
                "source": "health_bridge"
            }
        }


class RegimeInfo(BaseModel):
    """Current market regime information."""
    entry_regime: str
    management_regime: str
    edge_regime: str
    confidence: float = 0.0
    
    class Config:
        json_schema_extra = {
            "example": {
                "entry_regime": "TREND_EXPANSION",
                "management_regime": "TREND_EXPANSION",
                "edge_regime": "REGIME_A",
                "confidence": 0.85
            }
        }


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _load_metrics_file(filename: str) -> Optional[Dict[str, Any]]:
    """Load metrics from storage file."""
    paths = [
        Path(f"storage/metrics/{filename}"),
        Path(f"/app/storage/metrics/{filename}"),
    ]
    
    for path in paths:
        if path.exists():
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"Could not load {path}: {e}")
    
    return None


def _calculate_metrics_from_trades(trades: List[Dict], period: str = "all") -> Dict[str, Any]:
    """Calculate performance metrics from trade list."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
        }
    
    wins = [t for t in trades if t.get('pnl_r', t.get('pnl', 0)) > 0]
    losses = [t for t in trades if t.get('pnl_r', t.get('pnl', 0)) <= 0]
    
    total_win_r = sum(t.get('pnl_r', t.get('pnl', 0)) for t in wins)
    total_loss_r = abs(sum(t.get('pnl_r', t.get('pnl', 0)) for t in losses))
    
    return {
        "total_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "profit_factor": total_win_r / total_loss_r if total_loss_r > 0 else 0,
        "expectancy_r": (total_win_r - total_loss_r) / len(trades) if trades else 0,
        "avg_win_r": total_win_r / len(wins) if wins else 0,
        "avg_loss_r": -total_loss_r / len(losses) if losses else 0,
    }


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/metrics/performance", response_model=PerformanceMetrics)
async def get_performance_metrics(
    period: str = Query(default="all", pattern="^(today|week|month|all)$"),
):
    """
    Get trading performance metrics.
    
    Returns win rate, profit factor, expectancy, and other key metrics.
    
    Args:
        period: Time period for metrics (today, week, month, all)
    """
    # Try to load from metrics file
    metrics_data = _load_metrics_file("performance.json")
    
    if metrics_data and period in metrics_data:
        data = metrics_data[period]
        return PerformanceMetrics(
            total_trades=data.get("total_trades", 0),
            win_rate=data.get("win_rate", 0),
            profit_factor=data.get("profit_factor", 0),
            expectancy_r=data.get("expectancy_r", 0),
            avg_win_r=data.get("avg_win_r", 0),
            avg_loss_r=data.get("avg_loss_r", 0),
            max_drawdown_pct=data.get("max_drawdown_pct", 0),
            sharpe_ratio=data.get("sharpe_ratio"),
            period=period,
        )
    
    # Fallback: use today's data from health bridge
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if status and period == "today":
        return PerformanceMetrics(
            total_trades=status.trades_today,
            win_rate=status.win_rate_today,
            period="today",
        )
    
    return PerformanceMetrics(period=period)


@router.get("/metrics/edge-health", response_model=EdgeHealthMetrics)
async def get_edge_health():
    """
    Get edge health monitoring metrics.
    
    Shows if the trading edge is degrading based on recent performance.
    """
    # Try to load from metrics file
    metrics_data = _load_metrics_file("edge_health.json")
    
    if metrics_data:
        return EdgeHealthMetrics(
            edge_score=metrics_data.get("edge_score", 0),
            mfe_mae_ratio=metrics_data.get("mfe_mae_ratio", 0),
            be_stop_percentage=metrics_data.get("be_stop_percentage", 0),
            recent_win_rate=metrics_data.get("recent_win_rate", 0),
            alert_level=metrics_data.get("alert_level", "NORMAL"),
            trades_in_window=metrics_data.get("trades_in_window", 0),
        )
    
    # Fallback: use health bridge
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    return EdgeHealthMetrics(
        edge_score=7.0,
        recent_win_rate=status.win_rate_today if status else 0,
        alert_level="NORMAL" if not status or status.win_rate_today >= 0.4 else "WARNING",
        trades_in_window=status.trades_today if status else 0,
    )


@router.get("/metrics/sessions", response_model=List[SessionStats])
async def get_session_stats():
    """
    Get performance statistics by trading session.
    
    Shows how the system performs in different sessions (Asia, London, NY).
    """
    # Try to load from metrics file
    metrics_data = _load_metrics_file("session_stats.json")
    
    if metrics_data and "sessions" in metrics_data:
        return [
            SessionStats(**session_data)
            for session_data in metrics_data["sessions"]
        ]
    
    # Default empty stats
    return [
        SessionStats(session="ASIA", trades=0),
        SessionStats(session="LONDON", trades=0),
        SessionStats(session="NY", trades=0),
    ]


@router.get("/metrics/market", response_model=MarketDataSnapshot)
async def get_market_data(
    settings=Depends(get_settings),
):
    """
    Get current market data snapshot.
    
    Returns latest price, spread, and volume information from bot.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    # Bot should have market data in extended status
    # For now, return what we have
    return MarketDataSnapshot(
        symbol=settings.ccxt_symbol,
        price=0.0,  # Would need market data in bridge
        timestamp=datetime.now(UTC).isoformat(),
        source="health_bridge" if status else "unavailable",
    )


@router.get("/metrics/regime", response_model=RegimeInfo)
async def get_regime_info():
    """
    Get current market regime classification.
    
    Shows the current regime from entry, management, and edge perspectives.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if status:
        return RegimeInfo(
            entry_regime=status.current_regime,
            management_regime=status.current_regime,
            edge_regime=status.current_regime,
            confidence=status.regime_confidence,
        )
    
    return RegimeInfo(
        entry_regime="UNKNOWN",
        management_regime="UNKNOWN",
        edge_regime="UNKNOWN",
        confidence=0.0,
    )


@router.get("/metrics/filters")
async def get_filter_stats():
    """
    Get filter pipeline statistics.
    
    Shows which filters are blocking trades and why.
    """
    # Try to load from metrics file
    metrics_data = _load_metrics_file("filter_stats.json")
    
    if metrics_data:
        return metrics_data
    
    return {
        "total_checked": 0,
        "total_passed": 0,
        "pass_rate": 0.0,
        "blocks_by_filter": {},
        "top_blockers": [],
    }


@router.get("/metrics/prometheus")
async def prometheus_metrics():
    """
    Prometheus metrics endpoint.
    
    Returns metrics in Prometheus text format for scraping.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    bot_alive = bridge.is_bot_alive()
    
    lines = [
        "# HELP daytrader_bot_alive Is the trading bot alive (1=yes, 0=no)",
        "# TYPE daytrader_bot_alive gauge",
        f"daytrader_bot_alive {1 if bot_alive else 0}",
        "",
        "# HELP daytrader_bot_ready Is the trading bot ready to trade",
        "# TYPE daytrader_bot_ready gauge",
        f"daytrader_bot_ready {1 if status and status.ready else 0}",
        "",
        "# HELP daytrader_trades_today Total trades executed today",
        "# TYPE daytrader_trades_today counter",
        f"daytrader_trades_today {status.trades_today if status else 0}",
        "",
        "# HELP daytrader_pnl_today_usd P&L in USD for today",
        "# TYPE daytrader_pnl_today_usd gauge",
        f"daytrader_pnl_today_usd {status.pnl_today_usd if status else 0}",
        "",
        "# HELP daytrader_win_rate_today Win rate for today",
        "# TYPE daytrader_win_rate_today gauge",
        f"daytrader_win_rate_today {status.win_rate_today if status else 0}",
        "",
        "# HELP daytrader_has_position Whether there is an open position",
        "# TYPE daytrader_has_position gauge",
        f"daytrader_has_position {1 if status and status.has_open_position else 0}",
        "",
        "# HELP daytrader_ws_connected WebSocket connection status",
        "# TYPE daytrader_ws_connected gauge",
        f"daytrader_ws_connected {1 if status and status.ws_connected else 0}",
        "",
        "# HELP daytrader_time_drift_ms Clock drift in milliseconds",
        "# TYPE daytrader_time_drift_ms gauge",
        f"daytrader_time_drift_ms {status.time_drift_ms if status else 0}",
        "",
        "# HELP daytrader_uptime_seconds Bot uptime in seconds",
        "# TYPE daytrader_uptime_seconds counter",
        f"daytrader_uptime_seconds {status.uptime_seconds if status else 0}",
        "",
    ]
    
    return Response(
        content="\n".join(lines),
        media_type="text/plain; version=0.0.4"
    )


@router.get("/metrics/export")
async def export_metrics(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    period: str = Query(default="all", pattern="^(today|week|month|all)$"),
):
    """
    Export metrics data.
    
    Returns metrics data in specified format for analysis.
    """
    # Gather all metrics
    performance = await get_performance_metrics(period)
    edge_health = await get_edge_health()
    sessions = await get_session_stats()
    
    data = {
        "exported_at": datetime.now(UTC).isoformat(),
        "period": period,
        "performance": performance.model_dump(),
        "edge_health": edge_health.model_dump(),
        "sessions": [s.model_dump() for s in sessions],
    }
    
    if format == "json":
        return data
    
    # CSV format - flatten for export
    return Response(
        content="format,not_implemented",
        media_type="text/csv",
    )
