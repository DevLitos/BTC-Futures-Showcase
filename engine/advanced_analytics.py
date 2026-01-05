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
    from engine.capital_weighting import TimeRiskConfig
    _CAPITAL_WEIGHTING_AVAILABLE = True
except ImportError:
    _CAPITAL_WEIGHTING_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# SL SIZE BINS (Segmentación por tamaño de Stop Loss)
# =============================================================================

class SLSizeBin:
    """Stop Loss size bins for noise detection."""
    MICRO = "MICRO"       # < 0.6 ATR (probable ruido)
    NORMAL = "NORMAL"     # 0.6-1.2 ATR (estándar)
    WIDE = "WIDE"         # > 1.2 ATR (conservador)


@dataclass
class SLBinConfig:
    """Configuration for SL size binning."""
    micro_max_atr: float = 0.6      # < 0.6 ATR = MICRO
    normal_max_atr: float = 1.2     # 0.6-1.2 ATR = NORMAL
    # > 1.2 ATR = WIDE


def classify_sl_bin(sl_distance: float, atr: float) -> str:
    """
    Classify SL into size bins.
    
    Args:
        sl_distance: Distance from entry to SL in price
        atr: ATR value at entry
        
    Returns:
        SL bin category
    """
    if atr <= 0:
        return SLSizeBin.NORMAL
    
    sl_atr_ratio = sl_distance / atr
    
    if sl_atr_ratio < SLBinConfig.micro_max_atr:
        return SLSizeBin.MICRO
    elif sl_atr_ratio < SLBinConfig.normal_max_atr:
        return SLSizeBin.NORMAL
    else:
        return SLSizeBin.WIDE


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
