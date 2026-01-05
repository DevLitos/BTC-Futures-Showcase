"""
Trade Metrics Module - V2.1 Observability Layer

This module provides:
1. Per-trade metrics tracking (MAE, MFE, R-multiples, duration, time-of-day)
2. Friction simulation (slippage, fees)
3. Structured logging for post-trade analysis

PURPOSE: Sin estos datos, todo lo demás es a ciegas.
These metrics reveal:
- Where we're leaving money on the table
- Which setups are worth more
- Which hours degrade the edge

NOTE: This module is OBSERVATION ONLY. It does NOT change trading rules.
"""

import logging
import json
from datetime import datetime, UTC
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# FRICTION SIMULATION CONFIG
# =============================================================================

@dataclass
class FrictionConfig:
    """
    Configuration for realistic friction simulation.
    
    Backtest sin fricción miente. Añade:
    - Slippage fijo (0.5-1 tick)
    - Comisiones reales del exchange
    - Pequeñas pérdidas en stops flash
    """
    # Slippage (as percentage of price)
    slippage_entry_pct: float = 0.0005    # 0.05% entry slippage (5 bps)
    slippage_exit_pct: float = 0.0005     # 0.05% exit slippage
    slippage_stop_pct: float = 0.001      # 0.10% stop slippage (worse in panic)
    
    # Exchange fees (Kraken Futures taker fees)
    fee_maker_pct: float = 0.0002         # 0.02% maker
    fee_taker_pct: float = 0.0005         # 0.05% taker
    
    # Assume we pay taker on entry (momentum) and maker on exit (limit TP)
    fee_entry_pct: float = 0.0005         # Taker on entry
    fee_exit_pct: float = 0.0002          # Maker on exit
    
    # Stop loss flash penalty (additional slippage in fast markets)
    flash_stop_penalty_pct: float = 0.002  # 0.2% extra on flash crashes
    flash_stop_probability: float = 0.05   # 5% of stops experience flash


# =============================================================================
# TRADE RECORD (per-trade metrics)
# =============================================================================

@dataclass
class TradeRecord:
    """
    Complete record of a single trade with all metrics.
    
    Tracks:
    - R esperado vs R alcanzado real
    - MAE (maximum adverse excursion)
    - MFE (maximum favorable excursion)
    - % veces que toca TP1 y vuelve a SL
    - profit factor (per trade: win/risk)
    - expectancy por trade
    - duración media del trade
    - time-of-day
    """
    # Identification
    trade_id: str = ""
    symbol: str = ""
    direction: str = ""  # LONG or SHORT
    
    # Entry
    entry_time: datetime = None
    entry_price: float = 0.0
    entry_size: float = 0.0
    
    # Exit
    exit_time: datetime = None
    exit_price: float = 0.0
    exit_reason: str = ""  # TP1, TP2, TRAILING, SL, TIME, MANUAL
    
    # Risk parameters
    stop_loss: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    
    # R-multiples
    r_expected: float = 0.0       # Expected R based on setup
    r_realized: float = 0.0       # Actual R achieved
    risk_amount: float = 0.0      # $ risked
    
    # Excursions
    mae: float = 0.0              # Maximum adverse excursion (worst drawdown)
    mfe: float = 0.0              # Maximum favorable excursion (best profit)
    mae_r: float = 0.0            # MAE in R-multiples
    mfe_r: float = 0.0            # MFE in R-multiples
    
    # Partial tracking
    tp1_hit: bool = False
    tp1_to_sl: bool = False       # Did it hit TP1 then come back to SL?
    tp2_hit: bool = False
    
    # Timing
    duration_minutes: float = 0.0
    entry_hour: int = 0           # UTC hour of entry
    entry_day_of_week: int = 0    # 0=Monday, 6=Sunday
    session: str = ""             # LONDON, NY, ASIA, OTHER
    
    # Friction (simulated)
    slippage_entry: float = 0.0
    slippage_exit: float = 0.0
    fees_paid: float = 0.0
    
    # P&L
    gross_pnl: float = 0.0        # Before fees
    net_pnl: float = 0.0          # After fees and slippage
    
    # Market context
    atr_at_entry: float = 0.0
    volatility_regime: str = ""   # LOW, NORMAL, HIGH, EXTREME
    
    # Metadata
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        # Convert datetime to ISO format
        if d['entry_time']:
            d['entry_time'] = d['entry_time'].isoformat()
        if d['exit_time']:
            d['exit_time'] = d['exit_time'].isoformat()
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradeRecord':
        """Create from dictionary."""
        if isinstance(data.get('entry_time'), str):
            data['entry_time'] = datetime.fromisoformat(data['entry_time'])
        if isinstance(data.get('exit_time'), str):
            data['exit_time'] = datetime.fromisoformat(data['exit_time'])
        return cls(**data)


# =============================================================================
# TRADE METRICS CALCULATOR
# =============================================================================

class TradeMetricsCalculator:
    """
    Calculate comprehensive metrics for a trade.
    
    This class takes raw trade data and computes all the metrics
    needed for proper edge analysis.
    """
    
    def __init__(self, friction_config: FrictionConfig = None):
        self.friction = friction_config or FrictionConfig()
    
    def calculate_r_multiple(self, 
                            entry_price: float,
                            exit_price: float,
                            stop_loss: float,
                            direction: str) -> float:
        """
        Calculate R-multiple for a trade.
        
        R = (profit or loss) / (risk per unit)
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            stop_loss: Stop loss price
            direction: LONG or SHORT
        """
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit <= 0:
            return 0.0
        
        if direction.upper() == 'LONG':
            profit_per_unit = exit_price - entry_price
        else:
            profit_per_unit = entry_price - exit_price
        
        return profit_per_unit / risk_per_unit
    
    def calculate_mae_mfe(self,
                          entry_price: float,
                          high_price: float,
                          low_price: float,
                          stop_loss: float,
                          direction: str) -> Dict[str, float]:
        """
        Calculate Maximum Adverse/Favorable Excursion.
        
        MAE: How far did price go against us (worst moment)
        MFE: How far did price go for us (best moment)
        
        Returns in both absolute and R terms.
        """
        risk_per_unit = abs(entry_price - stop_loss)
        
        if direction.upper() == 'LONG':
            mae = entry_price - low_price   # How far it dropped
            mfe = high_price - entry_price  # How far it rose
        else:
            mae = high_price - entry_price  # How far it rose (against short)
            mfe = entry_price - low_price   # How far it dropped (for short)
        
        mae_r = mae / risk_per_unit if risk_per_unit > 0 else 0
        mfe_r = mfe / risk_per_unit if risk_per_unit > 0 else 0
        
        return {
            'mae': max(0, mae),
            'mfe': max(0, mfe),
            'mae_r': max(0, mae_r),
            'mfe_r': max(0, mfe_r),
            'mfe_mae_ratio': mfe_r / mae_r if mae_r > 0 else float('inf')
        }
    
    def apply_friction(self,
                       entry_price: float,
                       exit_price: float,
                       size: float,
                       is_stop_exit: bool = False,
                       is_flash_stop: bool = False) -> Dict[str, float]:
        """
        Apply realistic friction (slippage + fees).
        
        Args:
            entry_price: Intended entry price
            exit_price: Intended exit price
            size: Position size
            is_stop_exit: Whether exit was via stop loss
            is_flash_stop: Whether it was a flash crash stop
        """
        # Entry slippage (always pay)
        slippage_entry = entry_price * self.friction.slippage_entry_pct
        
        # Exit slippage (worse for stops)
        if is_flash_stop:
            slippage_exit = exit_price * (
                self.friction.slippage_stop_pct + 
                self.friction.flash_stop_penalty_pct
            )
        elif is_stop_exit:
            slippage_exit = exit_price * self.friction.slippage_stop_pct
        else:
            slippage_exit = exit_price * self.friction.slippage_exit_pct
        
        # Fees
        fee_entry = entry_price * size * self.friction.fee_entry_pct
        fee_exit = exit_price * size * self.friction.fee_exit_pct
        
        # Adjusted prices
        adjusted_entry = entry_price + slippage_entry  # Worse entry
        adjusted_exit = exit_price - slippage_exit     # Worse exit (for longs)
        
        total_friction = (slippage_entry + slippage_exit) * size + fee_entry + fee_exit
        
        return {
            'slippage_entry': slippage_entry,
            'slippage_exit': slippage_exit,
            'fee_entry': fee_entry,
            'fee_exit': fee_exit,
            'total_friction': total_friction,
            'adjusted_entry': adjusted_entry,
            'adjusted_exit': adjusted_exit,
            'friction_pct': total_friction / (entry_price * size) * 100 if size > 0 else 0
        }
    
    def get_session(self, hour: int) -> str:
        """Determine trading session from UTC hour."""
        if 3 <= hour < 11:
            return "LONDON"
        elif 13 <= hour < 21:
            return "NY"
        elif 21 <= hour or hour < 3:
            return "ASIA"
        else:
            return "OVERLAP"
    
    def get_volatility_regime(self, atr: float, avg_atr: float) -> str:
        """Classify volatility regime."""
        if avg_atr <= 0:
            return "UNKNOWN"
        
        ratio = atr / avg_atr
        if ratio < 0.5:
            return "LOW"
        elif ratio < 1.5:
            return "NORMAL"
        elif ratio < 2.5:
            return "HIGH"
        else:
            return "EXTREME"
    
    def build_trade_record(self,
                           trade_id: str,
                           symbol: str,
                           direction: str,
                           entry_time: datetime,
                           entry_price: float,
                           entry_size: float,
                           exit_time: datetime,
                           exit_price: float,
                           exit_reason: str,
                           stop_loss: float,
                           tp1_price: float,
                           tp2_price: float = None,
                           high_during_trade: float = None,
                           low_during_trade: float = None,
                           tp1_hit: bool = False,
                           tp2_hit: bool = False,
                           atr_at_entry: float = None,
                           avg_atr: float = None,
                           apply_friction: bool = True,
                           notes: str = "",
                           tags: List[str] = None) -> TradeRecord:
        """
        Build a complete TradeRecord with all calculated metrics.
        
        This is the main entry point for creating trade records.
        """
        record = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_time=entry_time,
            entry_price=entry_price,
            entry_size=entry_size,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            stop_loss=stop_loss,
            tp1_price=tp1_price,
            tp2_price=tp2_price or 0.0,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            notes=notes,
            tags=tags or []
        )
        
        # Calculate R-multiples
        risk_per_unit = abs(entry_price - stop_loss)
        record.risk_amount = risk_per_unit * entry_size
        record.r_expected = 1.0  # Default expected R (can be overridden)
        record.r_realized = self.calculate_r_multiple(
            entry_price, exit_price, stop_loss, direction
        )
        
        # Calculate MAE/MFE if we have high/low data
        if high_during_trade is not None and low_during_trade is not None:
            excursions = self.calculate_mae_mfe(
                entry_price, high_during_trade, low_during_trade,
                stop_loss, direction
            )
            record.mae = excursions['mae']
            record.mfe = excursions['mfe']
            record.mae_r = excursions['mae_r']
            record.mfe_r = excursions['mfe_r']
        
        # Check TP1-to-SL pattern
        if tp1_hit and exit_reason == 'SL':
            record.tp1_to_sl = True
        
        # Duration
        if entry_time and exit_time:
            duration = (exit_time - entry_time).total_seconds() / 60
            record.duration_minutes = duration
        
        # Time of day
        if entry_time:
            record.entry_hour = entry_time.hour
            record.entry_day_of_week = entry_time.weekday()
            record.session = self.get_session(entry_time.hour)
        
        # Volatility regime
        if atr_at_entry:
            record.atr_at_entry = atr_at_entry
            if avg_atr:
                record.volatility_regime = self.get_volatility_regime(
                    atr_at_entry, avg_atr
                )
        
        # Apply friction
        if apply_friction:
            is_stop = exit_reason in ('SL', 'TRAILING')
            friction = self.apply_friction(
                entry_price, exit_price, entry_size, 
                is_stop_exit=is_stop
            )
            record.slippage_entry = friction['slippage_entry']
            record.slippage_exit = friction['slippage_exit']
            record.fees_paid = friction['fee_entry'] + friction['fee_exit']
        
        # P&L
        if direction.upper() == 'LONG':
            record.gross_pnl = (exit_price - entry_price) * entry_size
        else:
            record.gross_pnl = (entry_price - exit_price) * entry_size
        
        record.net_pnl = record.gross_pnl - record.fees_paid - (
            record.slippage_entry + record.slippage_exit
        ) * entry_size
        
        return record


# =============================================================================
# TRADE LOGGER (structured logging for analysis)
# =============================================================================

class TradeLogger:
    """
    Structured logger for trade records.
    
    Writes trade records to:
    1. JSON file (for analysis)
    2. Python logger (for monitoring)
    
    Purpose: Enable post-trade analysis to find where money is left.
    """
    
    def __init__(self, 
                 log_dir: str = "output/trades",
                 filename: str = "trade_log.jsonl"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / filename
        self.trades: List[TradeRecord] = []
        
        logger.info(f"📊 TradeLogger initialized: {self.log_file}")
    
    def log_trade(self, record: TradeRecord) -> None:
        """Log a completed trade."""
        self.trades.append(record)
        
        # Append to JSONL file
        with open(self.log_file, 'a') as f:
            f.write(json.dumps(record.to_dict()) + '\n')
        
        # Log summary to Python logger
        emoji = "✅" if record.net_pnl > 0 else "❌"
        logger.info(
            f"{emoji} TRADE CLOSED: {record.direction} {record.symbol} | "
            f"R={record.r_realized:.2f} | PnL=${record.net_pnl:.2f} | "
            f"Duration={record.duration_minutes:.0f}min | "
            f"MAE={record.mae_r:.2f}R MFE={record.mfe_r:.2f}R | "
            f"Session={record.session} | Exit={record.exit_reason}"
        )
        
        if record.tp1_to_sl:
            logger.warning(f"   ⚠️ TP1→SL Pattern detected (hit TP1 then stopped out)")
    
    def load_trades(self) -> List[TradeRecord]:
        """Load all trades from log file."""
        trades = []
        if self.log_file.exists():
            with open(self.log_file, 'r') as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        trades.append(TradeRecord.from_dict(data))
        return trades
    
    def get_summary_stats(self) -> Dict[str, Any]:
        """Calculate summary statistics from logged trades."""
        if not self.trades:
            self.trades = self.load_trades()
        
        if not self.trades:
            return {}
        
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        
        total_pnl = sum(t.net_pnl for t in self.trades)
        total_gross = sum(t.gross_pnl for t in self.trades)
        total_fees = sum(t.fees_paid for t in self.trades)
        
        win_rate = len(wins) / len(self.trades) if self.trades else 0
        
        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.net_pnl for t in losses) / len(losses)) if losses else 0
        
        profit_factor = (
            sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))
            if losses and sum(t.net_pnl for t in losses) != 0 else float('inf')
        )
        
        # R-multiple stats
        avg_r = sum(t.r_realized for t in self.trades) / len(self.trades)
        
        # MAE/MFE stats
        avg_mae_r = sum(t.mae_r for t in self.trades) / len(self.trades)
        avg_mfe_r = sum(t.mfe_r for t in self.trades) / len(self.trades)
        
        # TP1→SL pattern
        tp1_to_sl_count = sum(1 for t in self.trades if t.tp1_to_sl)
        tp1_to_sl_pct = tp1_to_sl_count / len(self.trades) * 100 if self.trades else 0
        
        # Duration stats
        avg_duration = sum(t.duration_minutes for t in self.trades) / len(self.trades)
        
        # By session
        session_stats = {}
        for session in ['LONDON', 'NY', 'ASIA', 'OVERLAP', 'OTHER']:
            session_trades = [t for t in self.trades if t.session == session]
            if session_trades:
                session_stats[session] = {
                    'count': len(session_trades),
                    'win_rate': len([t for t in session_trades if t.net_pnl > 0]) / len(session_trades),
                    'avg_r': sum(t.r_realized for t in session_trades) / len(session_trades),
                    'total_pnl': sum(t.net_pnl for t in session_trades)
                }
        
        # By hour
        hour_stats = {}
        for hour in range(24):
            hour_trades = [t for t in self.trades if t.entry_hour == hour]
            if hour_trades:
                hour_stats[hour] = {
                    'count': len(hour_trades),
                    'win_rate': len([t for t in hour_trades if t.net_pnl > 0]) / len(hour_trades),
                    'avg_r': sum(t.r_realized for t in hour_trades) / len(hour_trades)
                }
        
        return {
            'total_trades': len(self.trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'expectancy': avg_r,  # Average R
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'total_pnl': total_pnl,
            'total_gross_pnl': total_gross,
            'total_fees': total_fees,
            'avg_mae_r': avg_mae_r,
            'avg_mfe_r': avg_mfe_r,
            'mfe_mae_ratio': avg_mfe_r / avg_mae_r if avg_mae_r > 0 else 0,
            'tp1_to_sl_pct': tp1_to_sl_pct,
            'avg_duration_minutes': avg_duration,
            'by_session': session_stats,
            'by_hour': hour_stats
        }
    
    def print_summary(self) -> None:
        """Print formatted summary to console."""
        stats = self.get_summary_stats()
        if not stats:
            print("No trades to summarize.")
            return
        
        print("\n" + "="*60)
        print("📊 TRADE METRICS SUMMARY")
        print("="*60)
        print(f"Total Trades: {stats['total_trades']}")
        print(f"Win Rate: {stats['win_rate']*100:.1f}%")
        print(f"Profit Factor: {stats['profit_factor']:.2f}")
        print(f"Expectancy (avg R): {stats['expectancy']:.2f}R")
        print(f"\nAvg Winner: ${stats['avg_win']:.2f}")
        print(f"Avg Loser: ${stats['avg_loss']:.2f}")
        print(f"\nTotal P&L: ${stats['total_pnl']:.2f}")
        print(f"Total Fees: ${stats['total_fees']:.2f}")
        print(f"\n--- EXCURSION ANALYSIS ---")
        print(f"Avg MAE: {stats['avg_mae_r']:.2f}R")
        print(f"Avg MFE: {stats['avg_mfe_r']:.2f}R")
        print(f"MFE/MAE Ratio: {stats['mfe_mae_ratio']:.2f}")
        print(f"TP1→SL Pattern: {stats['tp1_to_sl_pct']:.1f}%")
        print(f"\nAvg Duration: {stats['avg_duration_minutes']:.0f} minutes")
        
        print(f"\n--- BY SESSION ---")
        for session, data in stats.get('by_session', {}).items():
            print(f"  {session}: {data['count']} trades, "
                  f"WR={data['win_rate']*100:.0f}%, "
                  f"AvgR={data['avg_r']:.2f}, "
                  f"PnL=${data['total_pnl']:.2f}")
        
        print("\n" + "="*60)


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_trade_logger: Optional[TradeLogger] = None
_metrics_calculator: Optional[TradeMetricsCalculator] = None
_friction_config: Optional[FrictionConfig] = None


def get_friction_config() -> FrictionConfig:
    """Get global FrictionConfig instance."""
    global _friction_config
    if _friction_config is None:
        _friction_config = FrictionConfig()
    return _friction_config


def get_trade_logger() -> TradeLogger:
    """Get global TradeLogger instance."""
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger


def get_metrics_calculator(friction_config: FrictionConfig = None) -> TradeMetricsCalculator:
    """Get global TradeMetricsCalculator instance."""
    global _metrics_calculator
    if _metrics_calculator is None:
        if friction_config is None:
            friction_config = get_friction_config()
        _metrics_calculator = TradeMetricsCalculator(friction_config)
    return _metrics_calculator


def log_trade(record: TradeRecord) -> None:
    """Convenience function to log a trade."""
    get_trade_logger().log_trade(record)


def print_trade_summary() -> None:
    """Print summary of all logged trades."""
    get_trade_logger().print_summary()
