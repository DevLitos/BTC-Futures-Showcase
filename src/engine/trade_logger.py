"""
Trade Persistence - Log trades with tags for edge analysis

Features:
1. JSON logging of all trades with metadata
2. Tags capture entry/exit conditions
3. Regime and session tracking
4. R-multiple and MFE/MAE tracking
5. Easy analysis for score optimization
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TradeLogRecord:
    """Complete trade record for persistence."""
    
    # Identity
    trade_id: str
    symbol: str
    direction: str  # LONG or SHORT
    trade_type: str  # LONG_MAIN, SHORT_STRUCTURAL, SHORT_PULLBACK
    
    # Timing
    entry_time: str
    exit_time: str
    duration_minutes: float
    
    # Prices
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    
    # Size & PnL
    position_size: float
    pnl_usd: float
    pnl_r: float
    
    # Performance
    mfe_r: float  # Maximum Favorable Excursion in R
    mae_r: float  # Maximum Adverse Excursion in R
    
    # Context
    regime: str  # RANGE, TREND, etc.
    volatility_regime: str  # NORMAL, HIGH_VOL, SPIKE
    session: str  # ASIA, LONDON, NY, etc.
    day_of_week: int  # 0=Monday
    
    # Entry score
    entry_score: float
    
    # Exit info (MOVED BEFORE fields with defaults)
    exit_reason: str
    
    # Fields with default values (must come last)
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    entry_tags: List[str] = field(default_factory=list)
    exit_tags: List[str] = field(default_factory=list)
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # =========================================================================
    # 🔥 ML DATA COLLECTION (Jan 24, 2026)
    # Enhanced fields for machine learning training data
    # =========================================================================
    
    # Feature snapshot at entry - ALL indicators for ML training
    feature_snapshot: Dict[str, float] = field(default_factory=dict)
    # Expected keys: adx_1h, adx_5m, adx_slope, atr_ratio, bb_width, 
    #                percentile_60d, vwap_distance_atr, volume_ratio,
    #                rsi_14, funding_rate, spread_pct, regime_duration_bars
    
    # MFE/MAE timing - when did best/worst occur?
    candles_to_mfe: int = 0        # How many candles until MFE was hit
    candles_to_mae: int = 0        # How many candles until MAE was hit
    mfe_time: str = ""             # Timestamp when MFE occurred
    mae_time: str = ""             # Timestamp when MAE occurred
    
    # Regime tracking - did conditions change during trade?
    entry_regime: str = ""         # Regime at entry (TREND/RANGE/TRANSITION)
    exit_regime: str = ""          # Regime at exit
    regime_changed: bool = False   # True if regime changed during trade
    
    # Trading mode at entry
    entry_trading_mode: str = ""   # TREND/RANGE/TRANSITION mode
    entry_daily_regime: str = ""   # TREND_DAY/RANGE_DAY/TRANSITION_DAY


class TradeLogger:
    """
    Persistent trade logging for edge analysis.
    
    Usage:
        logger = TradeLogger()
        
        # On trade entry
        trade_id = logger.start_trade(
            symbol='BTC/USD:USD',
            direction='LONG',
            entry_price=97000,
            entry_score=7.5,
            entry_tags=['HTF_BULLISH', 'LOCATION_OK', 'MICRO_ALIGNED'],
            regime='TREND',
            session='LONDON'
        )
        
        # On trade exit
        logger.complete_trade(
            trade_id=trade_id,
            exit_price=97500,
            exit_reason='TP1',
            pnl_usd=50.0,
            mfe_r=0.8,
            mae_r=0.2
        )
        
        # Analysis
        stats = logger.get_stats_by_tag('LOCATION_OK')
    """
    
    def __init__(self, log_dir: str = "trade_logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_file = self._get_log_file()
        self.active_trades: Dict[str, TradeLogRecord] = {}
        
        logger.info(f"TradeLogger initialized: {self.log_dir}")
    
    def _get_log_file(self) -> Path:
        """Get log file path for current day."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return self.log_dir / f"trades_{today}.jsonl"
    
    def _generate_trade_id(self) -> str:
        """Generate unique trade ID."""
        return datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")[:20]
    
    def _get_events_file(self) -> Path:
        """Get events log file path for current day."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return self.log_dir / f"events_{today}.jsonl"
    
    async def log_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """
        Log a trade event (trade_opened, trade_closed, tp1_hit, add_to_winner, etc.)
        
        This is used for detailed event tracking during trades.
        
        Args:
            event_type: Type of event (trade_opened, trade_closed, tp1_hit, add_to_winner)
            data: Event data dictionary
        """
        try:
            event = {
                'timestamp': datetime.now(UTC).isoformat(),
                'event_type': event_type,
                **data
            }
            
            events_file = self._get_events_file()
            with open(events_file, 'a') as f:
                f.write(json.dumps(event, default=str) + '\n')
            
            logger.debug(f"📝 Event logged: {event_type}")
        except Exception as e:
            # Don't fail the main trading loop due to logging errors
            logger.warning(f"Failed to log event {event_type}: {e}")
    
    def start_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        entry_score: float,
        entry_tags: List[str],
        regime: str,
        volatility_regime: str = "NORMAL",
        session: str = "OTHER",
        trade_type: str = "LONG_MAIN",
        score_breakdown: Dict[str, float] = None,
        metadata: Dict[str, Any] = None,
        # 🔥 ML Data Collection fields (Jan 24, 2026)
        feature_snapshot: Dict[str, float] = None,
        entry_trading_mode: str = "",
        entry_daily_regime: str = ""
    ) -> str:
        """
        Start tracking a new trade.
        
        Returns:
            trade_id for later reference
        """
        trade_id = self._generate_trade_id()
        now = datetime.now(UTC)
        
        record = TradeLogRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            trade_type=trade_type,
            entry_time=now.isoformat(),
            exit_time="",
            duration_minutes=0.0,
            entry_price=entry_price,
            exit_price=0.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            pnl_usd=0.0,
            pnl_r=0.0,
            mfe_r=0.0,
            mae_r=0.0,
            regime=regime,
            volatility_regime=volatility_regime,
            session=session,
            day_of_week=now.weekday(),
            entry_score=entry_score,
            score_breakdown=score_breakdown or {},
            entry_tags=entry_tags or [],
            exit_reason="",
            exit_tags=[],
            metadata=metadata or {},
            # 🔥 ML fields
            feature_snapshot=feature_snapshot or {},
            entry_regime=regime,  # Store entry regime for comparison
            entry_trading_mode=entry_trading_mode,
            entry_daily_regime=entry_daily_regime
        )
        
        self.active_trades[trade_id] = record
        
        logger.info(f"📝 Trade started: {trade_id} | {direction} @ {entry_price:.2f} | Tags: {entry_tags}")
        
        return trade_id
    
    def update_trade_mfe_mae(self, trade_id: str, mfe_r: float, mae_r: float, 
                             candle_count: int = 0, current_regime: str = ""):
        """
        Update MFE/MAE during trade.
        
        🔥 ML Enhancement: Also tracks WHEN MFE/MAE occurred and regime changes
        
        Args:
            trade_id: Trade identifier
            mfe_r: Current MFE in R
            mae_r: Current MAE in R  
            candle_count: Current candle count since entry
            current_regime: Current market regime (for drift detection)
        """
        if trade_id in self.active_trades:
            record = self.active_trades[trade_id]
            now = datetime.now(UTC)
            
            # Track MFE timing - when was best price reached?
            if mfe_r > record.mfe_r:
                record.mfe_r = mfe_r
                record.candles_to_mfe = candle_count
                record.mfe_time = now.isoformat()
            
            # Track MAE timing - when was worst price reached?
            if mae_r > record.mae_r:
                record.mae_r = mae_r
                record.candles_to_mae = candle_count
                record.mae_time = now.isoformat()
            
            # Track regime changes during trade
            if current_regime and record.entry_regime:
                if current_regime != record.entry_regime and not record.regime_changed:
                    record.regime_changed = True
                    logger.debug(f"📊 Trade {trade_id}: Regime changed {record.entry_regime} → {current_regime}")
    
    def complete_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        pnl_usd: float,
        pnl_r: float = None,
        mfe_r: float = None,
        mae_r: float = None,
        exit_tags: List[str] = None,
        # 🔥 ML Data Collection fields (Jan 24, 2026)
        exit_regime: str = "",
        final_candle_count: int = 0
    ) -> Optional[TradeLogRecord]:
        """
        Complete a trade and persist to log.
        
        Returns:
            Completed TradeLogRecord
        """
        if trade_id not in self.active_trades:
            logger.warning(f"Trade {trade_id} not found in active trades")
            return None
        
        record = self.active_trades.pop(trade_id)
        now = datetime.now(UTC)
        
        # 🔥 ML: Store exit regime for regime change detection
        if exit_regime:
            record.exit_regime = exit_regime
            if record.entry_regime and exit_regime != record.entry_regime:
                record.regime_changed = True
        
        # Calculate duration
        entry_dt = datetime.fromisoformat(record.entry_time.replace('Z', '+00:00'))
        duration = (now - entry_dt).total_seconds() / 60
        
        # Calculate R if not provided
        if pnl_r is None:
            risk = abs(record.entry_price - record.stop_loss)
            if risk > 0:
                if record.direction == 'LONG':
                    pnl_r = (exit_price - record.entry_price) / risk
                else:
                    pnl_r = (record.entry_price - exit_price) / risk
            else:
                pnl_r = 0.0
        
        # Update record
        record.exit_time = now.isoformat()
        record.duration_minutes = duration
        record.exit_price = exit_price
        record.exit_reason = exit_reason
        record.pnl_usd = pnl_usd
        record.pnl_r = pnl_r
        record.exit_tags = exit_tags or []
        
        if mfe_r is not None:
            record.mfe_r = mfe_r
        if mae_r is not None:
            record.mae_r = mae_r
        
        # Persist to file
        self._write_record(record)
        
        logger.info(
            f"📝 Trade completed: {trade_id} | {exit_reason} @ {exit_price:.2f} | "
            f"PnL: ${pnl_usd:.2f} ({pnl_r:.2f}R) | MFE: {record.mfe_r:.2f}R"
        )
        
        return record
    
    def _write_record(self, record: TradeLogRecord):
        """Write trade record to JSONL file."""
        try:
            # Refresh file path in case day changed
            self.current_file = self._get_log_file()
            
            with open(self.current_file, 'a') as f:
                f.write(json.dumps(asdict(record)) + '\n')
        except Exception as e:
            logger.error(f"Error writing trade record: {e}")
    
    def load_trades(
        self,
        start_date: str = None,
        end_date: str = None
    ) -> List[TradeLogRecord]:
        """
        Load trades from log files.
        
        Args:
            start_date: Start date (YYYY-MM-DD), None for all
            end_date: End date (YYYY-MM-DD), None for today
        
        Returns:
            List of TradeLogRecords
        """
        trades = []
        
        for log_file in sorted(self.log_dir.glob("trades_*.jsonl")):
            # Extract date from filename
            file_date = log_file.stem.replace("trades_", "")
            
            if start_date and file_date < start_date:
                continue
            if end_date and file_date > end_date:
                continue
            
            try:
                with open(log_file, 'r') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            trades.append(TradeLogRecord(**data))
            except Exception as e:
                logger.error(f"Error loading {log_file}: {e}")
        
        return trades
    
    def get_stats_by_tag(self, tag: str) -> Dict[str, Any]:
        """
        Get statistics for trades with a specific tag.
        
        Returns:
            Dict with winrate, avg_r, expectancy, etc.
        """
        trades = self.load_trades()
        
        # Filter trades with this tag
        tagged_trades = [t for t in trades if tag in t.entry_tags]
        
        if not tagged_trades:
            return {'tag': tag, 'count': 0, 'message': 'No trades with this tag'}
        
        wins = [t for t in tagged_trades if t.pnl_r > 0]
        losses = [t for t in tagged_trades if t.pnl_r <= 0]
        
        total_r = sum(t.pnl_r for t in tagged_trades)
        avg_r = total_r / len(tagged_trades)
        
        avg_mfe = sum(t.mfe_r for t in tagged_trades) / len(tagged_trades)
        avg_mae = sum(t.mae_r for t in tagged_trades) / len(tagged_trades)
        
        return {
            'tag': tag,
            'count': len(tagged_trades),
            'wins': len(wins),
            'losses': len(losses),
            'winrate': len(wins) / len(tagged_trades) * 100,
            'total_r': round(total_r, 2),
            'avg_r': round(avg_r, 3),
            'expectancy': round(avg_r, 3),
            'avg_mfe': round(avg_mfe, 2),
            'avg_mae': round(avg_mae, 2)
        }
    
    def get_stats_by_regime(self, regime: str) -> Dict[str, Any]:
        """Get statistics for trades in a specific regime."""
        trades = self.load_trades()
        regime_trades = [t for t in trades if t.regime == regime]
        
        if not regime_trades:
            return {'regime': regime, 'count': 0}
        
        wins = [t for t in regime_trades if t.pnl_r > 0]
        total_r = sum(t.pnl_r for t in regime_trades)
        
        return {
            'regime': regime,
            'count': len(regime_trades),
            'wins': len(wins),
            'winrate': len(wins) / len(regime_trades) * 100,
            'total_r': round(total_r, 2),
            'avg_r': round(total_r / len(regime_trades), 3)
        }
    
    def get_stats_by_session(self, session: str) -> Dict[str, Any]:
        """Get statistics for trades in a specific session."""
        trades = self.load_trades()
        session_trades = [t for t in trades if t.session == session]
        
        if not session_trades:
            return {'session': session, 'count': 0}
        
        wins = [t for t in session_trades if t.pnl_r > 0]
        total_r = sum(t.pnl_r for t in session_trades)
        
        return {
            'session': session,
            'count': len(session_trades),
            'wins': len(wins),
            'winrate': len(wins) / len(session_trades) * 100,
            'total_r': round(total_r, 2),
            'avg_r': round(total_r / len(session_trades), 3)
        }
    
    def get_tag_correlation(self) -> List[Dict[str, Any]]:
        """
        Analyze all tags and their correlation to positive R.
        
        Returns:
            List of tag stats sorted by expectancy
        """
        trades = self.load_trades()
        
        # Collect all unique tags
        all_tags = set()
        for t in trades:
            all_tags.update(t.entry_tags)
        
        # Get stats for each tag
        tag_stats = []
        for tag in all_tags:
            stats = self.get_stats_by_tag(tag)
            if stats.get('count', 0) > 0:
                tag_stats.append(stats)
        
        # Sort by expectancy
        tag_stats.sort(key=lambda x: x.get('avg_r', 0), reverse=True)
        
        return tag_stats


# Singleton instance
_trade_logger: Optional[TradeLogger] = None


def get_trade_logger() -> TradeLogger:
    """Get singleton TradeLogger instance."""
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger
