"""
Risk Profile System - Unified risk management with clear return types

PRINCIPLE (Jan 20, 2026):
The risk engine returns a CLEAR INSTRUCTION, not a suggestion.

RiskProfile ENUM:
- FULL: Full position size allowed
- REDUCED: Half position size
- PROBE: 25% position (probe entry)
- NO_NEW_TRADES_TODAY: Stop trading for the day (max loss, system issues, etc.)

The rest of the system MUST respect this return.
No second-guessing, no overrides (except emergency kill switch).
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta, UTC
import logging

logger = logging.getLogger(__name__)


class RiskProfile(str, Enum):
    """
    Risk profile returned by risk engine.
    This is an INSTRUCTION, not a suggestion.
    """
    FULL = "FULL"                           # Full position size
    REDUCED = "REDUCED"                     # 50% position size
    PROBE = "PROBE"                         # 25% position (test the water)
    NO_NEW_TRADES_TODAY = "NO_NEW_TRADES"   # Stop trading for the day


@dataclass
class RiskAssessment:
    """Complete risk assessment from the risk engine."""
    profile: RiskProfile
    size_multiplier: float           # 1.0, 0.5, 0.25, 0.0
    reason: str                      # Human-readable reason
    
    # Contributing factors
    daily_pnl_factor: float = 1.0    # Factor from daily P&L
    consecutive_losses_factor: float = 1.0
    drawdown_factor: float = 1.0
    session_factor: float = 1.0
    regime_factor: float = 1.0
    
    # Limits
    remaining_trades_today: int = 10
    max_position_usd: float = 0.0
    
    # Flags
    is_recovery_mode: bool = False
    is_max_loss_hit: bool = False
    is_drawdown_limit: bool = False
    
    def __str__(self) -> str:
        emoji = {
            RiskProfile.FULL: "✅",
            RiskProfile.REDUCED: "⚠️",
            RiskProfile.PROBE: "🔶",
            RiskProfile.NO_NEW_TRADES_TODAY: "🚫"
        }
        return f"{emoji.get(self.profile, '❓')} Risk: {self.profile.value} ({self.size_multiplier:.0%}) - {self.reason}"


class RiskProfileEngine:
    """
    Risk Profile Assessment Engine.
    
    ⚠️ NOTE: Renamed from RiskEngine to avoid confusion with
    src.domain.risk.risk_engine.RiskEngine (the main position manager).
    
    Evaluates multiple risk factors and returns a clear RiskProfile.
    
    Risk factors considered:
    1. Daily P&L (max loss per day)
    2. Consecutive losses (drawdown mode)
    3. Peak drawdown (absolute limit)
    4. Session quality (time-based risks)
    5. Regime (market conditions)
    6. Position concentration
    
    Usage:
        engine = RiskEngine(config)
        assessment = engine.assess_risk(
            daily_pnl=-500,
            consecutive_losses=2,
            peak_drawdown=0.05,
            current_regime="RANGE_PASIVO",
            ...
        )
        
        if assessment.profile == RiskProfile.NO_NEW_TRADES_TODAY:
            return  # Stop trading
        
        position_size *= assessment.size_multiplier
    """
    
    def __init__(self, config: Optional[Dict] = None, capital: float = 0.0):
        self.config = config or {}
        
        # Daily limits — derived from actual capital when available
        # Old defaults ($1000/$500/$750) were 430% of $230 → completely inert
        if capital > 0:
            self.MAX_DAILY_LOSS_USD = self.config.get('max_daily_loss_usd', capital * 0.03)   # 3% of capital
            self.DAILY_LOSS_REDUCED = self.config.get('daily_loss_reduced', capital * 0.015)   # 1.5% → REDUCED
            self.DAILY_LOSS_PROBE = self.config.get('daily_loss_probe', capital * 0.02)        # 2% → PROBE
        else:
            self.MAX_DAILY_LOSS_USD = self.config.get('max_daily_loss_usd', 1000)
            self.DAILY_LOSS_REDUCED = self.config.get('daily_loss_reduced', 500)
            self.DAILY_LOSS_PROBE = self.config.get('daily_loss_probe', 750)
        
        # Consecutive losses
        self.CONSECUTIVE_LOSS_REDUCED = self.config.get('consecutive_loss_reduced', 2)
        self.CONSECUTIVE_LOSS_PROBE = self.config.get('consecutive_loss_probe', 3)
        self.CONSECUTIVE_LOSS_STOP = self.config.get('consecutive_loss_stop', 4)
        
        # Drawdown limits
        self.DRAWDOWN_REDUCED = self.config.get('drawdown_reduced', 0.03)    # 3%
        self.DRAWDOWN_PROBE = self.config.get('drawdown_probe', 0.05)        # 5%
        self.DRAWDOWN_STOP = self.config.get('drawdown_stop', 0.07)          # 7%
        
        # Trade frequency limits
        self.MAX_TRADES_PER_DAY = self.config.get('max_trades_per_day', 10)
        
        # Session quality
        self.RISKY_HOURS_UTC = self.config.get('risky_hours', [3, 4, 5, 6])  # Low liquidity
        
        # Tracking
        self._today_trades = 0
        self._today_pnl = 0.0
        self._last_reset_date: Optional[date] = None
        self._consecutive_losses = 0
        self._recovery_mode_until: Optional[datetime] = None
    
    def assess_risk(self,
                    # P&L metrics
                    daily_pnl_usd: float = 0.0,
                    realized_pnl_usd: float = 0.0,
                    unrealized_pnl_usd: float = 0.0,
                    
                    # Trade metrics
                    consecutive_losses: int = 0,
                    trades_today: int = 0,
                    
                    # Drawdown
                    peak_drawdown_pct: float = 0.0,
                    
                    # Context
                    current_regime: str = "UNKNOWN",
                    current_hour_utc: int = 12,
                    
                    # Position
                    current_position_usd: float = 0.0,
                    max_position_usd: float = 10000.0,
                    
                    **kwargs) -> RiskAssessment:
        """
        Assess current risk and return appropriate profile.
        
        Returns:
            RiskAssessment with profile, size_multiplier, and details
        """
        # Check for new day
        self._check_daily_reset()
        
        # Update tracking
        self._today_trades = trades_today
        self._today_pnl = daily_pnl_usd
        self._consecutive_losses = consecutive_losses
        
        # Initialize factors
        factors = {
            'daily_pnl': 1.0,
            'consecutive_losses': 1.0,
            'drawdown': 1.0,
            'session': 1.0,
            'regime': 1.0
        }
        reasons = []
        
        # =====================================================================
        # 1. DAILY P&L CHECK (highest priority)
        # =====================================================================
        if daily_pnl_usd <= -self.MAX_DAILY_LOSS_USD:
            return RiskAssessment(
                profile=RiskProfile.NO_NEW_TRADES_TODAY,
                size_multiplier=0.0,
                reason=f"Max daily loss hit: ${daily_pnl_usd:.0f}",
                is_max_loss_hit=True,
                remaining_trades_today=0
            )
        elif daily_pnl_usd <= -self.DAILY_LOSS_PROBE:
            factors['daily_pnl'] = 0.25
            reasons.append(f"Daily loss ${abs(daily_pnl_usd):.0f} → PROBE")
        elif daily_pnl_usd <= -self.DAILY_LOSS_REDUCED:
            factors['daily_pnl'] = 0.5
            reasons.append(f"Daily loss ${abs(daily_pnl_usd):.0f} → REDUCED")
        
        # =====================================================================
        # 2. CONSECUTIVE LOSSES CHECK
        # =====================================================================
        if consecutive_losses >= self.CONSECUTIVE_LOSS_STOP:
            return RiskAssessment(
                profile=RiskProfile.NO_NEW_TRADES_TODAY,
                size_multiplier=0.0,
                reason=f"{consecutive_losses} consecutive losses - cooling off",
                is_recovery_mode=True,
                remaining_trades_today=0
            )
        elif consecutive_losses >= self.CONSECUTIVE_LOSS_PROBE:
            factors['consecutive_losses'] = 0.25
            reasons.append(f"{consecutive_losses} losses → PROBE")
        elif consecutive_losses >= self.CONSECUTIVE_LOSS_REDUCED:
            factors['consecutive_losses'] = 0.5
            reasons.append(f"{consecutive_losses} losses → REDUCED")
        
        # =====================================================================
        # 3. DRAWDOWN CHECK
        # =====================================================================
        if peak_drawdown_pct >= self.DRAWDOWN_STOP:
            return RiskAssessment(
                profile=RiskProfile.NO_NEW_TRADES_TODAY,
                size_multiplier=0.0,
                reason=f"Drawdown {peak_drawdown_pct:.1%} exceeds limit",
                is_drawdown_limit=True,
                remaining_trades_today=0
            )
        elif peak_drawdown_pct >= self.DRAWDOWN_PROBE:
            factors['drawdown'] = 0.25
            reasons.append(f"Drawdown {peak_drawdown_pct:.1%} → PROBE")
        elif peak_drawdown_pct >= self.DRAWDOWN_REDUCED:
            factors['drawdown'] = 0.5
            reasons.append(f"Drawdown {peak_drawdown_pct:.1%} → REDUCED")
        
        # =====================================================================
        # 4. SESSION QUALITY CHECK
        # =====================================================================
        if current_hour_utc in self.RISKY_HOURS_UTC:
            factors['session'] = 0.5
            reasons.append(f"Low liquidity hour ({current_hour_utc}:00 UTC)")
        
        # =====================================================================
        # 5. REGIME CHECK
        # =====================================================================
        regime_factors = {
            'TREND': 1.0,
            'RANGE_ACTIVO': 0.8,
            'RANGE_PASIVO': 0.5,
            'EXHAUSTION': 0.5,
            'TRANSITIONAL': 0.25,
            'UNKNOWN': 0.25
        }
        factors['regime'] = regime_factors.get(current_regime, 0.5)
        if factors['regime'] < 1.0:
            reasons.append(f"Regime {current_regime} → {factors['regime']:.0%}")
        
        # =====================================================================
        # 6. TRADE FREQUENCY CHECK
        # =====================================================================
        remaining_trades = self.MAX_TRADES_PER_DAY - trades_today
        if remaining_trades <= 0:
            return RiskAssessment(
                profile=RiskProfile.NO_NEW_TRADES_TODAY,
                size_multiplier=0.0,
                reason=f"Max trades ({self.MAX_TRADES_PER_DAY}) reached for today",
                remaining_trades_today=0
            )
        
        # =====================================================================
        # CALCULATE FINAL PROFILE
        # =====================================================================
        combined_factor = min(factors.values())  # Use most restrictive
        
        if combined_factor <= 0:
            profile = RiskProfile.NO_NEW_TRADES_TODAY
            size_mult = 0.0
        elif combined_factor <= 0.25:
            profile = RiskProfile.PROBE
            size_mult = 0.25
        elif combined_factor <= 0.5:
            profile = RiskProfile.REDUCED
            size_mult = 0.5
        else:
            profile = RiskProfile.FULL
            size_mult = 1.0
        
        # Build reason string
        if not reasons:
            reason = "All risk factors nominal"
        else:
            reason = "; ".join(reasons)
        
        return RiskAssessment(
            profile=profile,
            size_multiplier=size_mult,
            reason=reason,
            daily_pnl_factor=factors['daily_pnl'],
            consecutive_losses_factor=factors['consecutive_losses'],
            drawdown_factor=factors['drawdown'],
            session_factor=factors['session'],
            regime_factor=factors['regime'],
            remaining_trades_today=remaining_trades,
            max_position_usd=max_position_usd,
            is_recovery_mode=(consecutive_losses >= self.CONSECUTIVE_LOSS_REDUCED)
        )
    
    def _check_daily_reset(self):
        """Reset daily counters if it's a new day."""
        today = date.today()
        if self._last_reset_date != today:
            self._today_trades = 0
            self._today_pnl = 0.0
            self._consecutive_losses = 0
            self._last_reset_date = today
            logger.info("🔄 Risk engine daily reset")
    
    def record_trade_result(self, pnl_usd: float, is_win: bool):
        """Record a trade result for tracking."""
        self._today_trades += 1
        self._today_pnl += pnl_usd
        
        if is_win:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            
        logger.info(
            f"📝 Trade recorded: {'WIN' if is_win else 'LOSS'} ${pnl_usd:.2f} | "
            f"Today: {self._today_trades} trades, ${self._today_pnl:.2f} | "
            f"Consecutive losses: {self._consecutive_losses}"
        )
    
    def force_no_trades_today(self, reason: str):
        """Manually force no more trades today."""
        self._today_pnl = -self.MAX_DAILY_LOSS_USD * 2  # Force stop
        logger.warning(f"🚫 Forced no trades: {reason}")
    
    def get_status(self) -> Dict:
        """Get current risk engine status."""
        return {
            'today_trades': self._today_trades,
            'today_pnl': self._today_pnl,
            'consecutive_losses': self._consecutive_losses,
            'max_daily_loss': self.MAX_DAILY_LOSS_USD,
            'remaining_trades': self.MAX_TRADES_PER_DAY - self._today_trades,
            'last_reset': self._last_reset_date
        }


# =============================================================================
# SINGLETON
# =============================================================================

_risk_profile_engine: Optional[RiskProfileEngine] = None


def get_risk_profile_engine(config: Optional[Dict] = None, capital: float = 0.0) -> RiskProfileEngine:
    """Get or create the risk profile engine singleton."""
    global _risk_profile_engine
    if _risk_profile_engine is None:
        _risk_profile_engine = RiskProfileEngine(config, capital=capital)
    elif capital > 0 and _risk_profile_engine.MAX_DAILY_LOSS_USD > capital:
        # Re-derive limits when capital is known and current limits are too loose
        _risk_profile_engine = RiskProfileEngine(config, capital=capital)
    return _risk_profile_engine


def reset_risk_profile_engine():
    """Reset the singleton."""
    global _risk_profile_engine
    _risk_profile_engine = None


def assess_risk(**kwargs) -> RiskAssessment:
    """Convenience function to assess risk."""
    return get_risk_profile_engine().assess_risk(**kwargs)
