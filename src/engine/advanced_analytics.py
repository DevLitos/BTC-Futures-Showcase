"""
Advanced Analytics Module - V2.1 Observabilidad Completa

FASE A: Observabilidad (Prioridad Inmediata)
- Expectancy por sesión horaria y por tamaño de SL
- Segmentación de SL en bins (<0.6 ATR, 0.6-1.2 ATR, >1.2 ATR)
- Curvas de capital por setup

FASE B: Redistribución de Capital  
- Capital weighting por setup (Top: 1.0%, Medio: 0.75%, Marginal: 0.5%)
- Risk scaling por horario (TOP: 1.0x, NEUTRA: 0.7x, MUERTA: 0.3x)

FASE C: Optimización del Runner
- Activación solo tras +1.2R
- Filtro: no aplicar si MFE histórico < 2R

REGLA DE ORO: No actuar sobre métricas hasta n > 100-150 trades por setup.

NEW FEATURES:
- Trade Instrumenter: "Caja Negra" que captura todo en el momento de entrada
- SL Bin Classification: MICRO (<0.6 ATR), NORMAL (0.6-1.2 ATR), WIDE (>1.2 ATR)
- Real-time Edge Health Tracking
- PPO Integration
"""

import logging
from datetime import datetime, UTC
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
import json
import csv

# Integration with Capital Weighting
try:
    from src.engine.capital_weighting import TimeRiskConfig
    _CAPITAL_WEIGHTING_AVAILABLE = True
except ImportError:
    _CAPITAL_WEIGHTING_AVAILABLE = False

# Integration with Market Regime
try:
    from src.engine.market_regime import MarketRegime, get_regime_classifier
    _MARKET_REGIME_AVAILABLE = True
except ImportError:
    _MARKET_REGIME_AVAILABLE = False

# Integration with Risk Middleware
try:
    from src.engine.risk_middleware import SetupGrade, get_risk_middleware
    _RISK_MIDDLEWARE_AVAILABLE = True
except ImportError:
    _RISK_MIDDLEWARE_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# SL SIZE BINS (Segmentación por tamaño de Stop Loss)
# =============================================================================

class SLSizeBin:
    """
    Stop Loss size bins for noise detection.
    
    Lógica:
    - Bin MICRO (< 0.6 ATR): Alta precisión, alta probabilidad de ser barridos por wick
    - Bin NORMAL (0.6-1.2 ATR): Sweet spot donde estructura suele aguantar
    - Bin WIDE (> 1.2 ATR): Trades conservadores o cambios de tendencia mayor
    
    Objetivo: Si Bin MICRO tiene Expectancy negativo tras 100 trades, 
    deja de operarlos. No cambies el sistema, elimina el ruido.
    """
    MICRO = "MICRO"       # < 0.6 ATR (probable ruido)
    NORMAL = "NORMAL"     # 0.6-1.2 ATR (estándar)
    WIDE = "WIDE"         # > 1.2 ATR (conservador)


@dataclass
class SLBinConfig:
    """Configuration for SL size binning."""
    micro_max_atr: float = 0.6      # < 0.6 ATR = MICRO
    normal_max_atr: float = 1.2     # 0.6-1.2 ATR = NORMAL
    # > 1.2 ATR = WIDE
    
    # Action thresholds
    min_trades_for_action: int = 100
    negative_expectancy_threshold: float = 0.0  # Below this = problem


def classify_sl_bin(sl_distance: float, atr: float, config: SLBinConfig = None) -> str:
    """
    Classify SL into size bins at trade entry.
    
    Args:
        sl_distance: Distance from entry to SL in price (absolute value)
        atr: ATR value at entry
        config: Optional configuration
        
    Returns:
        SL bin category: MICRO, NORMAL, or WIDE
    """
    config = config or SLBinConfig()
    
    if atr <= 0:
        return SLSizeBin.NORMAL
    
    sl_atr_ratio = sl_distance / atr
    
    if sl_atr_ratio < config.micro_max_atr:
        return SLSizeBin.MICRO
    elif sl_atr_ratio < config.normal_max_atr:
        return SLSizeBin.NORMAL
    else:
        return SLSizeBin.WIDE


# =============================================================================
# TRADE INSTRUMENTER - "CAJA NEGRA"
# =============================================================================

@dataclass
class TradeInstrumentationData:
    """
    Complete instrumentation data captured at trade entry.
    
    This is the "black box" that records everything we need to analyze later.
    """
    # Trade identification
    trade_id: str = ""
    symbol: str = ""
    
    # Entry details
    entry_time: datetime = None
    entry_price: float = 0.0
    direction: str = ""  # LONG or SHORT
    position_size: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    
    # SL Classification
    sl_distance: float = 0.0
    sl_atr_ratio: float = 0.0
    sl_bin: str = ""  # MICRO, NORMAL, WIDE
    
    # Market Context at Entry
    atr_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    market_regime: str = ""  # TREND_EXPANSION, MEAN_REVERSION, CHOPPY_DEAD
    atr_ratio: float = 0.0  # Current ATR / Baseline ATR
    
    # Session Context
    entry_hour: int = 0
    entry_minute: int = 0
    entry_day_of_week: int = 0
    session: str = ""  # ASIA, LONDON, OVERLAP, NY, DEAD
    
    # Setup Classification
    setup_type: str = ""
    setup_grade: str = ""  # A, B, C
    
    # Risk Parameters Applied
    base_risk_pct: float = 0.0
    adjusted_risk_pct: float = 0.0
    risk_multiplier: float = 0.0
    regime_multiplier: float = 0.0
    setup_multiplier: float = 0.0
    
    # Streak State
    streak_at_entry: int = 0
    escalation_active: bool = False
    
    # Candle Analysis
    avg_body_ratio: float = 0.0  # Recent candles body/range
    wick_dominance: float = 0.0  # % of wicky candles
    
    # Volume Context
    volume_ratio: float = 0.0  # Current / Average volume
    volume_decaying: bool = False


class TradeInstrumenter:
    """
    The "Black Box" recorder for trades.
    
    Captures all relevant data at trade entry for later analysis.
    """
    
    def __init__(self, output_dir: str = "logs/instrumentation"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Current trade being instrumented
        self.current_trade: Optional[TradeInstrumentationData] = None
        
        # All recorded trades
        self.trades: List[Dict] = []
        
        # Stats by SL bin (for quick lookup)
        self.sl_bin_stats: Dict[str, Dict] = {
            SLSizeBin.MICRO: {'count': 0, 'wins': 0, 'total_r': 0.0, 'total_pnl': 0.0},
            SLSizeBin.NORMAL: {'count': 0, 'wins': 0, 'total_r': 0.0, 'total_pnl': 0.0},
            SLSizeBin.WIDE: {'count': 0, 'wins': 0, 'total_r': 0.0, 'total_pnl': 0.0}
        }
    
    def instrument_entry(self,
                         trade_id: str,
                         symbol: str,
                         entry_price: float,
                         direction: str,
                         position_size: float,
                         stop_loss: float,
                         take_profit_1: float = 0.0,
                         take_profit_2: float = 0.0,
                         atr: float = 0.0,
                         adx: float = 0.0,
                         setup_type: str = "DEFAULT",
                         market_time: datetime = None,
                         **kwargs) -> TradeInstrumentationData:
        """
        Instrument a trade at entry time.
        
        This captures the "snapshot" of all conditions when the trade was taken.
        """
        market_time = market_time or datetime.now(UTC)
        
        # Calculate SL classification
        sl_distance = abs(entry_price - stop_loss)
        sl_atr_ratio = sl_distance / atr if atr > 0 else 0.0
        sl_bin = classify_sl_bin(sl_distance, atr)
        
        # Get market regime if available
        regime_str = ""
        atr_ratio = 1.0
        regime_mult = 1.0
        
        if _MARKET_REGIME_AVAILABLE:
            classifier = get_regime_classifier()
            regime = classifier.get_current_regime()
            if regime:
                regime_str = regime.value
            atr_ratio = classifier.update_atr_baseline(atr) if atr > 0 else 1.0
            guidance = classifier.get_regime_guidance(regime)
            regime_mult = guidance.risk_multiplier if guidance else 1.0
        
        # Get setup grade if middleware available
        setup_grade = "B"
        setup_mult = 1.0
        streak = 0
        escalation = False
        base_risk = 0.8
        adjusted_risk = 0.8
        
        if _RISK_MIDDLEWARE_AVAILABLE:
            middleware = get_risk_middleware()
            grade = middleware.classify_setup(setup_type)
            setup_grade = grade.value
            setup_mult = middleware.setup_config.GRADE_MULTIPLIERS.get(grade, 1.0)
            streak = middleware.streak_tracker.current_streak
            escalation = middleware.streak_tracker.is_escalated
            base_risk = middleware.streak_tracker.config.BASE_RISK_PCT
            adjusted_risk = middleware.streak_tracker.get_current_risk_pct() * regime_mult * setup_mult
        
        # Session classification
        hour = market_time.hour
        session = SessionAnalyzer.get_session(hour)
        
        # Build instrumentation data
        self.current_trade = TradeInstrumentationData(
            trade_id=trade_id,
            symbol=symbol,
            entry_time=market_time,
            entry_price=entry_price,
            direction=direction,
            position_size=position_size,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            sl_distance=sl_distance,
            sl_atr_ratio=sl_atr_ratio,
            sl_bin=sl_bin,
            atr_at_entry=atr,
            adx_at_entry=adx,
            market_regime=regime_str,
            atr_ratio=atr_ratio,
            entry_hour=hour,
            entry_minute=market_time.minute,
            entry_day_of_week=market_time.weekday(),
            session=session,
            setup_type=setup_type,
            setup_grade=setup_grade,
            base_risk_pct=base_risk,
            adjusted_risk_pct=adjusted_risk,
            risk_multiplier=regime_mult * setup_mult,
            regime_multiplier=regime_mult,
            setup_multiplier=setup_mult,
            streak_at_entry=streak,
            escalation_active=escalation,
            avg_body_ratio=kwargs.get('avg_body_ratio', 0.0),
            wick_dominance=kwargs.get('wick_dominance', 0.0),
            volume_ratio=kwargs.get('volume_ratio', 1.0),
            volume_decaying=kwargs.get('volume_decaying', False)
        )
        
        logger.info(f"📦 INSTRUMENTED: {trade_id} | SL Bin: {sl_bin} ({sl_atr_ratio:.2f} ATR) | "
                   f"Regime: {regime_str} | Setup: {setup_type} ({setup_grade})")
        
        return self.current_trade
    
    def instrument_exit(self,
                        exit_price: float,
                        exit_time: datetime,
                        exit_reason: str,
                        pnl: float,
                        mfe_r: float = 0.0,
                        mae_r: float = 0.0,
                        duration_minutes: float = 0.0) -> Dict:
        """
        Complete instrumentation at trade exit.
        
        Returns complete trade record.
        """
        if not self.current_trade:
            logger.warning("No current trade to instrument exit")
            return {}
        
        # Calculate R value
        # Risk per unit = distance from entry to SL
        # Total risk = risk_per_unit * position_size
        risk_per_unit = abs(self.current_trade.entry_price - self.current_trade.stop_loss)
        total_risk = risk_per_unit * self.current_trade.position_size
        r_realized = pnl / total_risk if total_risk > 0 else 0.0
        
        # Build complete record
        record = {
            # Entry data
            **{k: v for k, v in self.current_trade.__dict__.items() if not k.startswith('_')},
            
            # Exit data
            'exit_time': exit_time.isoformat() if exit_time else '',
            'exit_price': exit_price,
            'exit_reason': exit_reason,
            
            # Performance
            'pnl': pnl,
            'net_pnl': pnl,
            'r_realized': r_realized,
            'mfe_r': mfe_r,
            'mae_r': mae_r,
            'duration_minutes': duration_minutes,
            'is_win': pnl > 0
        }
        
        # Convert datetime fields to strings
        if 'entry_time' in record and record['entry_time']:
            record['entry_time'] = record['entry_time'].isoformat() if isinstance(record['entry_time'], datetime) else record['entry_time']
        
        # Store trade
        self.trades.append(record)
        
        # Update SL bin stats
        sl_bin = self.current_trade.sl_bin
        if sl_bin in self.sl_bin_stats:
            stats = self.sl_bin_stats[sl_bin]
            stats['count'] += 1
            if pnl > 0:
                stats['wins'] += 1
            stats['total_r'] += r_realized
            stats['total_pnl'] += pnl
        
        # Write to log file
        self._append_to_log(record)
        
        # Clear current trade
        self.current_trade = None
        
        logger.info(f"📦 EXIT INSTRUMENTED: {record.get('trade_id')} | "
                   f"R: {r_realized:+.2f} | MFE: {mfe_r:.2f}R | Exit: {exit_reason}")
        
        return record
    
    def _append_to_log(self, record: Dict):
        """Append trade record to JSONL log."""
        log_file = self.output_dir / "trades.jsonl"
        try:
            with open(log_file, 'a') as f:
                f.write(json.dumps(record, default=str) + '\n')
        except Exception as e:
            logger.error(f"Error writing to trade log: {e}")
    
    def get_sl_bin_analysis(self) -> Dict[str, Dict]:
        """
        Get analysis by SL bin.
        
        Returns expectancy and stats for each bin.
        """
        analysis = {}
        
        for bin_name, stats in self.sl_bin_stats.items():
            count = stats['count']
            if count == 0:
                analysis[bin_name] = {
                    'count': 0,
                    'expectancy': 0.0,
                    'win_rate': 0.0,
                    'should_trade': True,
                    'reason': 'NO_DATA'
                }
                continue
            
            expectancy = stats['total_r'] / count
            win_rate = stats['wins'] / count
            
            # Check if should continue trading this bin
            should_trade = True
            reason = 'OK'
            
            config = SLBinConfig()
            if count >= config.min_trades_for_action:
                if expectancy < config.negative_expectancy_threshold:
                    should_trade = False
                    reason = f'NEGATIVE_EXPECTANCY ({expectancy:.2f}R)'
            else:
                reason = f'INSUFFICIENT_DATA ({count} < {config.min_trades_for_action})'
            
            analysis[bin_name] = {
                'count': count,
                'expectancy': expectancy,
                'win_rate': win_rate,
                'total_pnl': stats['total_pnl'],
                'should_trade': should_trade,
                'reason': reason
            }
        
        return analysis
    
    def should_block_sl_bin(self, sl_bin: str) -> Tuple[bool, str]:
        """
        Check if an SL bin should be blocked based on performance.
        
        Returns:
            Tuple of (should_block, reason)
        """
        analysis = self.get_sl_bin_analysis()
        bin_data = analysis.get(sl_bin, {})
        
        should_trade = bin_data.get('should_trade', True)
        reason = bin_data.get('reason', 'OK')
        
        return not should_trade, reason


# =============================================================================
# GLOBAL TRADE INSTRUMENTER
# =============================================================================

_trade_instrumenter: Optional[TradeInstrumenter] = None


def get_trade_instrumenter() -> TradeInstrumenter:
    """Get global TradeInstrumenter instance."""
    global _trade_instrumenter
    if _trade_instrumenter is None:
        _trade_instrumenter = TradeInstrumenter()
    return _trade_instrumenter


# =============================================================================
# SESSION ANALYSIS
# =============================================================================

class SessionAnalyzer:
    """
    Analyze performance by trading session.
    
    NOTE: También existe lógica similar en capital_weighting.py.
    Ambas implementaciones son compatibles pero se deben mantener sincronizadas.
    
    Sessions:
    - ASIA: 00:00-07:00 UTC
    - LONDON: 07:00-12:00 UTC  
    - OVERLAP: 12:00-16:00 UTC (London + NY)
    - NY: 16:00-21:00 UTC
    - DEAD: 21:00-00:00 UTC
    """
    
    SESSIONS = {
        "ASIA": (0, 7),
        "LONDON": (7, 12),
        "OVERLAP": (12, 16),
        "NY": (16, 21),
        "DEAD": (21, 24),
    }
    
    # Risk scaling by session (Fase B)
    # Default values if capital_weighting is not available
    _DEFAULT_RISK_SCALE = {
        "OVERLAP": 1.0,    # TOP session
        "LONDON": 1.0,     # TOP session
        "NY": 0.85,        # Good but more volatile
        "ASIA": 0.7,       # NEUTRA
        "DEAD": 0.3,       # MUERTA (no eliminar, para no perder "colas derechas")
    }
    
    @classmethod
    def get_session(cls, hour: int) -> str:
        """Get session name from hour."""
        for session, (start, end) in cls.SESSIONS.items():
            if start <= hour < end:
                return session
        return "DEAD"  # Default for hours 21-24
    
    @classmethod
    def get_risk_scale(cls, session: str) -> float:
        """Get risk scaling factor for session."""
        if _CAPITAL_WEIGHTING_AVAILABLE:
            config = TimeRiskConfig()
            multipliers = config.SESSION_MULTIPLIERS
            
            # Map local session names to capital_weighting names
            mapping = {
                "ASIA": "ASIA",
                "LONDON": "LONDON",
                "OVERLAP": "LONDON_NY_OVERLAP",
                "NY": "NY",
                "DEAD": "DEAD_HOURS"
            }
            
            cw_name = mapping.get(session)
            if cw_name and cw_name in multipliers:
                return multipliers[cw_name]
                
        return cls._DEFAULT_RISK_SCALE.get(session, 0.7)


# =============================================================================
# TRADE ANALYTICS ENGINE
# =============================================================================

@dataclass
class TradeAnalytics:
    """Container for computed analytics on a trade set."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0           # Average R
    profit_factor: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    mfe_mae_ratio: float = 0.0
    avg_duration: float = 0.0
    total_pnl: float = 0.0
    sample_sufficient: bool = False   # n > 100-150


class TradeAnalyticsEngine:
    """
    Engine for computing comprehensive trade analytics.
    
    REGLA DE ORO: No actuar sobre métricas hasta tener
    una muestra significativa (n > 100-150 trades por setup).
    """
    
    MIN_SAMPLE_SIZE = 100  # Minimum trades for actionable insights
    
    def __init__(self):
        self.trades: List[Dict] = []
    
    def add_trade(self, trade: Dict) -> None:
        """Add a trade record for analysis."""
        self.trades.append(trade)
    
    def load_trades(self, filepath: str) -> int:
        """Load trades from JSONL file."""
        count = 0
        path = Path(filepath)
        if path.exists():
            with open(path, 'r') as f:
                for line in f:
                    if line.strip():
                        self.trades.append(json.loads(line))
                        count += 1
        return count
    
    def compute_analytics(self, trades: List[Dict]) -> TradeAnalytics:
        """Compute analytics for a trade set."""
        if not trades:
            return TradeAnalytics()
        
        analytics = TradeAnalytics()
        analytics.total_trades = len(trades)
        
        # Basic stats
        wins = [t for t in trades if t.get('net_pnl', t.get('pnl', 0)) > 0]
        losses = [t for t in trades if t.get('net_pnl', t.get('pnl', 0)) <= 0]
        
        analytics.wins = len(wins)
        analytics.losses = len(losses)
        analytics.win_rate = len(wins) / len(trades) if trades else 0
        
        # P&L
        analytics.total_pnl = sum(t.get('net_pnl', t.get('pnl', 0)) for t in trades)
        
        # Winners/Losers
        if wins:
            analytics.avg_winner = sum(t.get('net_pnl', t.get('pnl', 0)) for t in wins) / len(wins)
        if losses:
            analytics.avg_loser = abs(sum(t.get('net_pnl', t.get('pnl', 0)) for t in losses) / len(losses))
        
        # Profit Factor
        gross_profit = sum(t.get('net_pnl', t.get('pnl', 0)) for t in wins) if wins else 0
        gross_loss = abs(sum(t.get('net_pnl', t.get('pnl', 0)) for t in losses)) if losses else 0
        analytics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Expectancy (average R)
        r_values = [t.get('r_realized', t.get('pnl_r', 0)) for t in trades if 'r_realized' in t or 'pnl_r' in t]
        analytics.expectancy = sum(r_values) / len(r_values) if r_values else 0
        
        # MFE/MAE
        mfe_values = [t.get('mfe_r', 0) for t in trades if 'mfe_r' in t]
        mae_values = [t.get('mae_r', 0) for t in trades if 'mae_r' in t]
        
        analytics.avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0
        analytics.avg_mae = sum(mae_values) / len(mae_values) if mae_values else 0
        analytics.mfe_mae_ratio = analytics.avg_mfe / analytics.avg_mae if analytics.avg_mae > 0 else 0
        
        # Duration
        durations = [t.get('duration_minutes', 0) for t in trades if 'duration_minutes' in t]
        analytics.avg_duration = sum(durations) / len(durations) if durations else 0
        
        # Sample sufficiency
        analytics.sample_sufficient = len(trades) >= self.MIN_SAMPLE_SIZE
        
        return analytics
    
    def analyze_by_session(self) -> Dict[str, TradeAnalytics]:
        """Analyze performance by trading session."""
        session_trades = defaultdict(list)
        
        for trade in self.trades:
            hour = trade.get('entry_hour', 0)
            session = SessionAnalyzer.get_session(hour)
            session_trades[session].append(trade)
        
        return {
            session: self.compute_analytics(trades)
            for session, trades in session_trades.items()
        }
    
    def analyze_by_hour(self) -> Dict[int, TradeAnalytics]:
        """Analyze performance by hour."""
        hour_trades = defaultdict(list)
        
        for trade in self.trades:
            hour = trade.get('entry_hour', 0)
            hour_trades[hour].append(trade)
        
        return {
            hour: self.compute_analytics(trades)
            for hour, trades in sorted(hour_trades.items())
        }
    
    def analyze_by_sl_bin(self) -> Dict[str, TradeAnalytics]:
        """
        Analyze performance by SL size bin.
        
        Objetivo: Detectar "ruido" por stops microscópicos.
        """
        bin_trades = defaultdict(list)
        
        for trade in self.trades:
            sl_distance = abs(trade.get('entry_price', 0) - trade.get('stop_loss', 0))
            atr = trade.get('atr_at_entry', sl_distance)  # Fallback to SL if no ATR
            
            sl_bin = classify_sl_bin(sl_distance, atr)
            bin_trades[sl_bin].append(trade)
        
        return {
            sl_bin: self.compute_analytics(trades)
            for sl_bin, trades in bin_trades.items()
        }
    
    def analyze_by_setup(self, setup_field: str = 'setup_type') -> Dict[str, TradeAnalytics]:
        """Analyze performance by setup type."""
        setup_trades = defaultdict(list)
        
        for trade in self.trades:
            setup = trade.get(setup_field, 'UNKNOWN')
            setup_trades[setup].append(trade)
        
        return {
            setup: self.compute_analytics(trades)
            for setup, trades in setup_trades.items()
        }
    
    def analyze_by_exit_reason(self) -> Dict[str, TradeAnalytics]:
        """Analyze performance by exit reason."""
        exit_trades = defaultdict(list)
        
        for trade in self.trades:
            reason = trade.get('exit_reason', 'UNKNOWN')
            exit_trades[reason].append(trade)
        
        return {
            reason: self.compute_analytics(trades)
            for reason, trades in exit_trades.items()
        }
    
    def identify_top_setups(self, min_trades: int = 30) -> List[Tuple[str, TradeAnalytics]]:
        """
        Identify top-performing setups for capital weighting.
        
        Clasificación:
        - TOP: expectancy > 1.0R, profit_factor > 1.5
        - MEDIO: expectancy > 0.5R, profit_factor > 1.2
        - MARGINAL: resto
        """
        setup_analytics = self.analyze_by_setup()
        
        ranked = []
        for setup, analytics in setup_analytics.items():
            if analytics.total_trades >= min_trades:
                # Score based on expectancy and profit factor
                score = analytics.expectancy * analytics.profit_factor
                ranked.append((setup, analytics, score))
        
        # Sort by score descending
        ranked.sort(key=lambda x: x[2], reverse=True)
        
        return [(setup, analytics) for setup, analytics, _ in ranked]
    
    def get_risk_multiplier_recommendations(self) -> Dict[str, float]:
        """
        Get recommended risk multipliers by setup.
        
        Capital Weighting:
        - Setup TOP: 1.0% (1.0x)
        - Setup MEDIO: 0.75% (0.75x)
        - Setup MARGINAL: 0.5% (0.5x)
        """
        setup_analytics = self.analyze_by_setup()
        recommendations = {}
        
        for setup, analytics in setup_analytics.items():
            if analytics.total_trades < 30:
                # Not enough data, use default
                recommendations[setup] = 0.75
                continue
            
            # Classify based on expectancy and profit factor
            if analytics.expectancy >= 1.0 and analytics.profit_factor >= 1.5:
                recommendations[setup] = 1.0  # TOP
            elif analytics.expectancy >= 0.5 and analytics.profit_factor >= 1.2:
                recommendations[setup] = 0.75  # MEDIO
            else:
                recommendations[setup] = 0.5  # MARGINAL
        
        return recommendations
    
    def should_apply_runner(self, setup: str) -> Tuple[bool, str]:
        """
        Determine if runner optimization should be applied to a setup.
        
        Filtro: No aplicar runner si MFE histórico < 2R o si la 
        expansión carece de momentum.
        """
        setup_analytics = self.analyze_by_setup().get(setup)
        
        if not setup_analytics:
            return False, "NO_DATA"
        
        if setup_analytics.total_trades < 30:
            return False, "INSUFFICIENT_SAMPLE"
        
        if setup_analytics.avg_mfe < 2.0:
            return False, f"LOW_MFE ({setup_analytics.avg_mfe:.2f}R < 2R)"
        
        # Check if MFE/MAE ratio indicates good extension
        if setup_analytics.mfe_mae_ratio < 1.5:
            return False, f"LOW_MFE_MAE_RATIO ({setup_analytics.mfe_mae_ratio:.2f})"
        
        return True, f"OK (MFE={setup_analytics.avg_mfe:.2f}R)"


# =============================================================================
# EQUITY CURVE GENERATOR
# =============================================================================

class EquityCurveGenerator:
    """
    Generate equity curves for visual analysis.
    
    Objetivo: Generar plots independientes para identificar 
    cuáles setups lateralizan o degradan el sistema.
    """
    
    def __init__(self, initial_equity: float = 10000.0):
        self.initial_equity = initial_equity
    
    def generate_curve(self, trades: List[Dict]) -> List[Dict]:
        """
        Generate equity curve data.
        
        Returns list of points: {timestamp, equity, drawdown, trade_id}
        """
        curve = []
        equity = self.initial_equity
        peak = equity
        
        # Initial point
        curve.append({
            'timestamp': trades[0].get('entry_time', '') if trades else '',
            'equity': equity,
            'drawdown': 0.0,
            'drawdown_pct': 0.0,
            'trade_id': None
        })
        
        for trade in sorted(trades, key=lambda t: t.get('exit_time', '')):
            pnl = trade.get('net_pnl', trade.get('pnl', 0))
            equity += pnl
            peak = max(peak, equity)
            drawdown = peak - equity
            drawdown_pct = drawdown / peak * 100 if peak > 0 else 0
            
            curve.append({
                'timestamp': trade.get('exit_time', ''),
                'equity': equity,
                'drawdown': drawdown,
                'drawdown_pct': drawdown_pct,
                'trade_id': trade.get('trade_id', '')
            })
        
        return curve
    
    def generate_curves_by_setup(self, trades: List[Dict]) -> Dict[str, List[Dict]]:
        """Generate separate equity curves for each setup type."""
        setup_trades = defaultdict(list)
        
        for trade in trades:
            setup = trade.get('setup_type', 'UNKNOWN')
            setup_trades[setup].append(trade)
        
        return {
            setup: self.generate_curve(trades)
            for setup, trades in setup_trades.items()
        }
    
    def export_curve_csv(self, curve: List[Dict], filepath: str) -> None:
        """Export equity curve to CSV for plotting."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['timestamp', 'equity', 'drawdown', 'drawdown_pct', 'trade_id'])
            writer.writeheader()
            writer.writerows(curve)


# =============================================================================
# COMPREHENSIVE REPORT GENERATOR
# =============================================================================

class AnalyticsReportGenerator:
    """Generate comprehensive analytics reports."""
    
    def __init__(self, engine: TradeAnalyticsEngine):
        self.engine = engine
    
    def generate_report(self) -> str:
        """Generate full text report."""
        lines = []
        
        lines.append("="*70)
        lines.append("  TRADE ANALYTICS REPORT - V2.1")
        lines.append("="*70)
        
        # Overall stats
        overall = self.engine.compute_analytics(self.engine.trades)
        lines.append(f"\n📊 OVERALL STATISTICS")
        lines.append("-"*40)
        lines.append(f"Total Trades: {overall.total_trades}")
        lines.append(f"Win Rate: {overall.win_rate*100:.1f}%")
        lines.append(f"Profit Factor: {overall.profit_factor:.2f}")
        lines.append(f"Expectancy: {overall.expectancy:.2f}R")
        lines.append(f"Avg MFE: {overall.avg_mfe:.2f}R | Avg MAE: {overall.avg_mae:.2f}R")
        lines.append(f"MFE/MAE Ratio: {overall.mfe_mae_ratio:.2f}")
        lines.append(f"Total PnL: ${overall.total_pnl:,.2f}")
        
        if not overall.sample_sufficient:
            lines.append(f"\n⚠️  ADVERTENCIA: Muestra insuficiente ({overall.total_trades} < {self.engine.MIN_SAMPLE_SIZE})")
            lines.append("    No actuar sobre métricas hasta n > 100-150 trades")
        
        # By Session
        lines.append(f"\n📅 BY SESSION")
        lines.append("-"*40)
        session_stats = self.engine.analyze_by_session()
        for session in ["OVERLAP", "LONDON", "NY", "ASIA", "DEAD"]:
            if session in session_stats:
                s = session_stats[session]
                risk_scale = SessionAnalyzer.get_risk_scale(session)
                lines.append(f"  {session:8}: {s.total_trades:3} trades | "
                           f"WR={s.win_rate*100:5.1f}% | "
                           f"Exp={s.expectancy:+.2f}R | "
                           f"PF={s.profit_factor:.2f} | "
                           f"Risk: {risk_scale}x")
        
        # By SL Bin
        lines.append(f"\n📏 BY STOP LOSS SIZE")
        lines.append("-"*40)
        sl_stats = self.engine.analyze_by_sl_bin()
        for sl_bin in [SLSizeBin.MICRO, SLSizeBin.NORMAL, SLSizeBin.WIDE]:
            if sl_bin in sl_stats:
                s = sl_stats[sl_bin]
                warning = " ⚠️ RUIDO?" if sl_bin == SLSizeBin.MICRO and s.expectancy < 0 else ""
                lines.append(f"  {sl_bin:6}: {s.total_trades:3} trades | "
                           f"WR={s.win_rate*100:5.1f}% | "
                           f"Exp={s.expectancy:+.2f}R | "
                           f"PF={s.profit_factor:.2f}{warning}")
        
        # By Setup
        lines.append(f"\n🎯 BY SETUP TYPE")
        lines.append("-"*40)
        recommendations = self.engine.get_risk_multiplier_recommendations()
        setup_stats = self.engine.analyze_by_setup()
        
        for setup, analytics in sorted(setup_stats.items(), 
                                       key=lambda x: x[1].expectancy, 
                                       reverse=True):
            risk_mult = recommendations.get(setup, 0.75)
            tier = "TOP" if risk_mult >= 1.0 else ("MEDIO" if risk_mult >= 0.75 else "MARGINAL")
            should_runner, reason = self.engine.should_apply_runner(setup)
            runner_str = "✓" if should_runner else "✗"
            
            lines.append(f"  {setup:15}: {analytics.total_trades:3} trades | "
                        f"Exp={analytics.expectancy:+.2f}R | "
                        f"{tier:8} ({risk_mult}x) | "
                        f"Runner: {runner_str}")
        
        # By Exit Reason
        lines.append(f"\n🚪 BY EXIT REASON")
        lines.append("-"*40)
        exit_stats = self.engine.analyze_by_exit_reason()
        for reason, analytics in sorted(exit_stats.items(), 
                                        key=lambda x: x[1].total_trades, 
                                        reverse=True):
            lines.append(f"  {reason:12}: {analytics.total_trades:3} trades | "
                        f"WR={analytics.win_rate*100:5.1f}% | "
                        f"Exp={analytics.expectancy:+.2f}R")
        
        # Recommendations
        lines.append(f"\n💡 RECOMMENDATIONS")
        lines.append("-"*40)
        
        # Find problematic patterns
        if SLSizeBin.MICRO in sl_stats:
            micro = sl_stats[SLSizeBin.MICRO]
            if micro.expectancy < 0 and micro.total_trades >= 20:
                lines.append(f"  ⚠️  SL MICRO (<0.6 ATR): Expectancy negativa. Considerar widening SL.")
        
        for session in ["DEAD", "ASIA"]:
            if session in session_stats:
                s = session_stats[session]
                if s.expectancy < 0 and s.total_trades >= 20:
                    lines.append(f"  ⚠️  Sesión {session}: Expectancy negativa. Reducir riesgo.")
        
        lines.append("\n" + "="*70)
        
        return "\n".join(lines)
    
    def export_report(self, filepath: str) -> None:
        """Export report to file."""
        report = self.generate_report()
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            f.write(report)
        logger.info(f"Report exported to {filepath}")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_analytics_engine: Optional[TradeAnalyticsEngine] = None


def get_analytics_engine() -> TradeAnalyticsEngine:
    """Get global TradeAnalyticsEngine instance."""
    global _analytics_engine
    if _analytics_engine is None:
        _analytics_engine = TradeAnalyticsEngine()
    return _analytics_engine


def analyze_trades(trades: List[Dict] = None, 
                   filepath: str = None) -> TradeAnalytics:
    """
    Convenience function to analyze trades.
    
    Args:
        trades: List of trade dicts, or
        filepath: Path to JSONL file with trades
        
    Returns:
        TradeAnalytics object
    """
    # Use a fresh engine instance to avoid side effects on global state
    engine = TradeAnalyticsEngine()
    
    if filepath:
        engine.load_trades(filepath)
    elif trades:
        for trade in trades:
            engine.add_trade(trade)
    
    return engine.compute_analytics(engine.trades)


def print_analytics_report(filepath: str = None) -> None:
    """Print comprehensive analytics report."""
    engine = get_analytics_engine()
    
    if filepath:
        engine.load_trades(filepath)
    
    generator = AnalyticsReportGenerator(engine)
    print(generator.generate_report())


def get_session_risk_scale(hour: int) -> float:
    """Get risk scaling factor for a given hour."""
    session = SessionAnalyzer.get_session(hour)
    return SessionAnalyzer.get_risk_scale(session)


def classify_setup_tier(setup: str, 
                       expectancy: float, 
                       profit_factor: float) -> Tuple[str, float]:
    """
    Classify setup into tier and return risk multiplier.
    
    Returns: (tier_name, risk_multiplier)
    """
    if expectancy >= 1.0 and profit_factor >= 1.5:
        return ("TOP", 1.0)
    elif expectancy >= 0.5 and profit_factor >= 1.2:
        return ("MEDIO", 0.75)
    else:
        return ("MARGINAL", 0.5)


# =============================================================================
# EDGE HEALTH TRACKER (Real-time monitoring for Grafana)
# =============================================================================

class EdgeHealthTracker:
    """
    Real-time Edge Health monitoring for Grafana integration.
    
    Tracks:
    - Rolling expectancy (last 20 vs historical)
    - Edge degradation alerts
    - Automatic risk reduction when edge is compromised
    
    Alert: If rolling expectancy < 50% of historical → Observation Mode
    """
    
    def __init__(self, 
                 alert_threshold: float = 0.5,
                 rolling_window: int = 20,
                 min_trades_for_historical: int = 100):
        self.alert_threshold = alert_threshold
        self.rolling_window = rolling_window
        self.min_trades_for_historical = min_trades_for_historical
        
        # Trade R values
        self.r_history: List[float] = []
        
        # Historical baseline (calculated once we have enough data)
        self.historical_expectancy: Optional[float] = None
        
        # Alert state
        self.in_observation_mode: bool = False
        self.observation_risk_mult: float = 0.5  # Reduce risk by 50% in observation
        
    def record_trade_r(self, r_value: float):
        """Record a trade's R-multiple."""
        self.r_history.append(r_value)
        
        # Update historical once we have enough data
        if len(self.r_history) >= self.min_trades_for_historical:
            if self.historical_expectancy is None:
                self.historical_expectancy = sum(self.r_history) / len(self.r_history)
            else:
                # Slowly update historical (EWMA)
                alpha = 0.01
                self.historical_expectancy = (
                    alpha * r_value + (1 - alpha) * self.historical_expectancy
                )
        
        # Check for alert
        self._check_alert()
    
    def _check_alert(self):
        """Check if we should enter observation mode."""
        if len(self.r_history) < self.rolling_window:
            return
        
        if self.historical_expectancy is None:
            return
        
        rolling = self.r_history[-self.rolling_window:]
        rolling_expectancy = sum(rolling) / len(rolling)
        
        ratio = rolling_expectancy / self.historical_expectancy if self.historical_expectancy > 0 else 1.0
        
        if ratio < self.alert_threshold:
            if not self.in_observation_mode:
                logger.warning(f"⚠️ EDGE HEALTH ALERT: Rolling expectancy {rolling_expectancy:.2f}R "
                             f"< {self.alert_threshold*100:.0f}% of historical {self.historical_expectancy:.2f}R")
                logger.warning(f"   → Entering OBSERVATION MODE (risk x{self.observation_risk_mult})")
            self.in_observation_mode = True
        else:
            if self.in_observation_mode:
                logger.info(f"✅ EDGE HEALTH RECOVERED: Exiting observation mode")
            self.in_observation_mode = False
    
    def get_risk_multiplier(self) -> float:
        """Get risk multiplier based on edge health."""
        if self.in_observation_mode:
            return self.observation_risk_mult
        return 1.0
    
    def get_health_metrics(self) -> Dict[str, Any]:
        """Get current health metrics."""
        rolling_expectancy = 0.0
        if len(self.r_history) >= self.rolling_window:
            rolling = self.r_history[-self.rolling_window:]
            rolling_expectancy = sum(rolling) / len(rolling)
        
        ratio = 1.0
        if self.historical_expectancy and self.historical_expectancy > 0:
            ratio = rolling_expectancy / self.historical_expectancy
        
        return {
            'rolling_expectancy': rolling_expectancy,
            'historical_expectancy': self.historical_expectancy or 0.0,
            'health_ratio': ratio,
            'in_observation_mode': self.in_observation_mode,
            'trade_count': len(self.r_history),
            'risk_multiplier': self.get_risk_multiplier()
        }
    
    def update_prometheus_metrics(self, symbol: str = "BTC/USDT"):
        """Update Prometheus metrics for Grafana."""
        try:
            from src.monitoring.metrics import (
                update_edge_health, 
                check_edge_health_alert
            )
            
            metrics = self.get_health_metrics()
            update_edge_health(
                symbol=symbol,
                expectancy_last_20=metrics['rolling_expectancy'],
                expectancy_historical=metrics['historical_expectancy']
            )
        except ImportError:
            pass  # Metrics not available


# Global Edge Health Tracker
_edge_health_tracker: Optional[EdgeHealthTracker] = None


def get_edge_health_tracker() -> EdgeHealthTracker:
    """Get global EdgeHealthTracker instance."""
    global _edge_health_tracker
    if _edge_health_tracker is None:
        _edge_health_tracker = EdgeHealthTracker()
    return _edge_health_tracker


def record_trade_for_edge_health(r_value: float, symbol: str = "BTC/USDT"):
    """
    Convenience function to record a trade and update edge health.
    
    Call this after every trade closes.
    """
    tracker = get_edge_health_tracker()
    tracker.record_trade_r(r_value)
    tracker.update_prometheus_metrics(symbol)
    
    return tracker.get_health_metrics()


def get_edge_health_risk_multiplier() -> float:
    """Get current risk multiplier based on edge health."""
    tracker = get_edge_health_tracker()
    return tracker.get_risk_multiplier()


def is_in_observation_mode() -> bool:
    """Check if system is in observation mode due to edge degradation."""
    tracker = get_edge_health_tracker()
    return tracker.in_observation_mode
