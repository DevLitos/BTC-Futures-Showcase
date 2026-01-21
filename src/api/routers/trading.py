"""
Trading Router - Trading operations and position management

Provides:
- Current position status (from bot via health bridge)
- Trade history (from trade logs)
- Signal generation status
- Manual trade requests (paper mode only)

ARCHITECTURE:
    Position data comes from the health bridge (bot writes, API reads).
    Trade history is read from trade log files in storage/.
"""

from datetime import datetime, UTC
from typing import Dict, Any, List, Optional
from pathlib import Path
import logging
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.dependencies import get_settings, get_kill_switch
from src.core.health_bridge import get_health_bridge
from src.api.middleware import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# SCHEMAS
# =============================================================================

class PositionResponse(BaseModel):
    """Current position information."""
    has_position: bool
    side: Optional[str] = None  # "LONG", "SHORT", or None
    size: float = 0.0
    entry_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    source: str = "health_bridge"  # Where data came from
    
    class Config:
        json_schema_extra = {
            "example": {
                "has_position": True,
                "side": "LONG",
                "size": 0.001,
                "entry_price": 97000.0,
                "unrealized_pnl": 15.50,
                "stop_loss": 96500.0,
                "take_profit": 98000.0,
                "source": "health_bridge"
            }
        }


class TradeHistoryItem(BaseModel):
    """Single trade in history."""
    trade_id: str
    direction: str
    entry_time: str
    exit_time: Optional[str] = None
    entry_price: float
    exit_price: Optional[float] = None
    size: float = 0.0
    pnl_usd: Optional[float] = None
    pnl_r: Optional[float] = None
    exit_reason: Optional[str] = None


class SignalStatus(BaseModel):
    """Current signal generation status."""
    is_active: bool
    bot_alive: bool
    current_regime: str
    last_signal_time: Optional[str] = None
    signals_today: int = 0
    blocked_reason: Optional[str] = None


class ManualTradeRequest(BaseModel):
    """Request for manual trade (paper mode only)."""
    side: str = Field(..., pattern="^(LONG|SHORT)$")
    size_usd: float = Field(..., gt=0, le=10000)
    stop_loss_pct: float = Field(default=0.5, ge=0.1, le=5.0)
    take_profit_pct: float = Field(default=1.0, ge=0.2, le=10.0)
    
    class Config:
        json_schema_extra = {
            "example": {
                "side": "LONG",
                "size_usd": 100.0,
                "stop_loss_pct": 0.5,
                "take_profit_pct": 1.5
            }
        }


class TradeResponse(BaseModel):
    """Response after trade request."""
    success: bool
    trade_id: Optional[str] = None
    message: str
    details: Optional[Dict[str, Any]] = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _read_trade_logs(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """
    Read trade history from log files.
    
    Looks for trade logs in storage/trades/ directory.
    """
    trades = []
    
    # Try multiple possible locations
    log_paths = [
        Path("storage/trades"),
        Path("/app/storage/trades"),
        Path("trade_logs"),
        Path("/app/trade_logs"),
    ]
    
    for log_path in log_paths:
        if not log_path.exists():
            continue
        
        # Find JSON log files
        json_files = sorted(log_path.glob("*.json"), reverse=True)
        
        for json_file in json_files:
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)
                    
                # Handle different log formats
                if isinstance(data, list):
                    trades.extend(data)
                elif isinstance(data, dict):
                    if 'trades' in data:
                        trades.extend(data['trades'])
                    else:
                        trades.append(data)
                        
            except Exception as e:
                logger.debug(f"Could not read {json_file}: {e}")
                continue
        
        if trades:
            break
    
    # Sort by time (newest first) and apply pagination
    trades.sort(key=lambda x: x.get('entry_time', x.get('timestamp', '')), reverse=True)
    return trades[offset:offset + limit]


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/position", response_model=PositionResponse)
async def get_position():
    """
    Get current position status.
    
    Returns the current open position if any, including entry price,
    size, P&L, and stop/take-profit levels.
    
    Data source: Bot health bridge (real-time from bot).
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if not status:
        # Bot not responding - return empty position
        return PositionResponse(
            has_position=False,
            size=0.0,
            unrealized_pnl=0.0,
            source="unavailable",
        )
    
    if status.has_open_position:
        return PositionResponse(
            has_position=True,
            side=status.position_side.upper() if status.position_side else None,
            size=status.position_size,
            entry_price=status.position_entry_price if status.position_entry_price > 0 else None,
            unrealized_pnl=status.position_unrealized_pnl,
            source="health_bridge",
        )
    
    return PositionResponse(
        has_position=False,
        size=0.0,
        unrealized_pnl=0.0,
        source="health_bridge",
    )


@router.get("/trades", response_model=List[TradeHistoryItem])
async def get_trade_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    Get trade history.
    
    Returns list of recent trades with P&L information.
    Data is read from trade log files in storage.
    """
    raw_trades = _read_trade_logs(limit, offset)
    
    trades = []
    for raw in raw_trades:
        try:
            trade = TradeHistoryItem(
                trade_id=raw.get('trade_id', raw.get('id', 'unknown')),
                direction=raw.get('direction', raw.get('side', 'unknown')).upper(),
                entry_time=raw.get('entry_time', raw.get('timestamp', '')),
                exit_time=raw.get('exit_time'),
                entry_price=float(raw.get('entry_price', 0)),
                exit_price=float(raw.get('exit_price', 0)) if raw.get('exit_price') else None,
                size=float(raw.get('size', raw.get('quantity', 0))),
                pnl_usd=float(raw.get('pnl_usd', raw.get('pnl', 0))) if raw.get('pnl_usd') or raw.get('pnl') else None,
                pnl_r=float(raw.get('pnl_r', 0)) if raw.get('pnl_r') else None,
                exit_reason=raw.get('exit_reason'),
            )
            trades.append(trade)
        except Exception as e:
            logger.debug(f"Could not parse trade: {e}")
            continue
    
    return trades


@router.get("/signal/status", response_model=SignalStatus)
async def get_signal_status(
    kill_switch=Depends(get_kill_switch),
):
    """
    Get current signal generation status.
    
    Shows if signals are being generated, the current market regime,
    and any blocking conditions.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    bot_alive = bridge.is_bot_alive()
    
    is_blocked = False
    blocked_reason = None
    
    # Check kill switch
    if kill_switch:
        ks_state = kill_switch.get_state()
        if ks_state.is_active:
            is_blocked = True
            blocked_reason = f"Kill switch: {ks_state.reason.value}"
    
    # Check bot status
    if not bot_alive:
        is_blocked = True
        blocked_reason = blocked_reason or "Bot not responding"
    elif status and not status.ready:
        is_blocked = True
        blocked_reason = blocked_reason or f"Bot not ready: {status.ready_reason}"
    
    return SignalStatus(
        is_active=not is_blocked,
        bot_alive=bot_alive,
        current_regime=status.current_regime if status else "UNKNOWN",
        signals_today=status.trades_today if status else 0,
        blocked_reason=blocked_reason,
    )


@router.post("/trade", response_model=TradeResponse)
async def request_manual_trade(
    trade: ManualTradeRequest,
    settings=Depends(get_settings),
    kill_switch=Depends(get_kill_switch),
    api_key: str = Depends(verify_api_key),
):
    """
    Request a manual trade (paper mode only).
    
    ⚠️ PROTECTED ENDPOINT - Requires X-API-Key header.
    
    This endpoint allows manual trade entry for testing purposes.
    Only available in PAPER mode to prevent accidental live trades.
    
    NOTE: This creates a signal file that the bot picks up.
    The actual trade execution happens in the bot process.
    """
    # Check if we're in paper mode
    from src.core.settings import TradingMode
    if settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(
            status_code=403,
            detail="Manual trades not allowed in LIVE mode. Use PAPER mode for testing."
        )
    
    # Check kill switch
    if kill_switch:
        ks_state = kill_switch.get_state()
        if ks_state.is_active:
            return TradeResponse(
                success=False,
                message=f"Kill switch active: {ks_state.reason.value}"
            )
    
    # Check bot is alive
    bridge = get_health_bridge()
    if not bridge.is_bot_alive():
        return TradeResponse(
            success=False,
            message="Bot not responding - cannot execute trade"
        )
    
    # Create signal file for bot to pick up
    trade_id = f"manual_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    
    signal_data = {
        "trade_id": trade_id,
        "type": "MANUAL_ENTRY",
        "side": trade.side,
        "size_usd": trade.size_usd,
        "stop_loss_pct": trade.stop_loss_pct,
        "take_profit_pct": trade.take_profit_pct,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "api",
    }
    
    # Write signal file
    signal_path = Path("storage/signals") / f"{trade_id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(signal_path, 'w') as f:
            json.dump(signal_data, f, indent=2)
        
        logger.info(f"📝 Manual trade signal created: {trade.side} ${trade.size_usd}")
        
        return TradeResponse(
            success=True,
            trade_id=trade_id,
            message=f"Trade signal created: {trade.side} ${trade.size_usd}",
            details=signal_data,
        )
    except Exception as e:
        logger.error(f"Failed to create trade signal: {e}")
        return TradeResponse(
            success=False,
            message=f"Failed to create signal: {str(e)}"
        )


@router.delete("/position", response_model=TradeResponse)
async def close_position(
    settings=Depends(get_settings),
    kill_switch=Depends(get_kill_switch),
    api_key: str = Depends(verify_api_key),
):
    """
    Request to close current position.
    
    ⚠️ PROTECTED ENDPOINT - Requires X-API-Key header.
    
    Sends a close signal for the current position.
    Only available in PAPER mode.
    """
    # Check if we're in paper mode
    from src.core.settings import TradingMode
    if settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(
            status_code=403,
            detail="Manual position close not allowed in LIVE mode via API"
        )
    
    # Check bot has position
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if not status or not status.has_open_position:
        return TradeResponse(
            success=False,
            message="No open position to close"
        )
    
    # Create close signal
    signal_id = f"close_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    
    signal_data = {
        "signal_id": signal_id,
        "type": "CLOSE_POSITION",
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "api",
        "position_side": status.position_side,
        "position_size": status.position_size,
    }
    
    signal_path = Path("storage/signals") / f"{signal_id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(signal_path, 'w') as f:
            json.dump(signal_data, f, indent=2)
        
        logger.info(f"📝 Position close signal created for {status.position_side} {status.position_size}")
        
        return TradeResponse(
            success=True,
            trade_id=signal_id,
            message="Position close request submitted",
            details=signal_data,
        )
    except Exception as e:
        logger.error(f"Failed to create close signal: {e}")
        return TradeResponse(
            success=False,
            message=f"Failed to create signal: {str(e)}"
        )


@router.get("/daily-summary")
async def get_daily_summary():
    """
    Get today's trading summary.
    
    Returns trades, P&L, and win rate for today.
    """
    bridge = get_health_bridge()
    status = bridge.read_status()
    
    if not status:
        raise HTTPException(status_code=503, detail="Bot status unavailable")
    
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "trades": status.trades_today,
        "pnl_usd": status.pnl_today_usd,
        "pnl_r": status.pnl_today_r,
        "win_rate": status.win_rate_today,
        "has_open_position": status.has_open_position,
        "current_regime": status.current_regime,
    }
