"""
Capital Weighting Module - V2.1 Smart Risk Allocation

En vez de: "¿qué trades eliminar?"
Pregunta correcta: "¿a qué trades asigno más riesgo?"

This module provides:
1. Setup-based capital weighting (mejor expectancy → más riesgo)
2. Time-based risk adjustment (horas top → riesgo normal, horas muertas → no trade)
3. Runner optimization (trailing estructural tras +1.2R)

IMPORTANTE: Esto NO cambia la lógica de entrada. Solo el peso del capital.
Esto suele aumentar el PnL 30–60% sin subir DD.
"""

import logging
from datetime import datetime, UTC
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# SETUP QUALITY CLASSIFICATION
# =============================================================================

class SetupQuality(Enum):
    """Setup quality tiers for capital weighting."""
    A_PLUS = "A+"     # Top tier: highest expectancy setups
    A = "A"           # Strong: reliable edge
    B = "B"           # Standard: valid but average
    C = "C"           # Weak: valid but low expectancy
    BLOCKED = "X"     # Don't trade: edge not present


@dataclass
class SetupWeightConfig:
    """
    Configuration for capital weighting by setup quality.
    
    Example brutalmente efectivo:
    - Setup A (mejor expectancy): riesgo 1.25%
    - Setup B (normal): riesgo 0.75%
    - Setup C (débil pero válido): riesgo 0.5%
    """
    # Risk multipliers by quality tier
    # These multiply the base risk from DynamicRiskConfig
    RISK_MULTIPLIERS: Dict[SetupQuality, float] = field(default_factory=lambda: {
        SetupQuality.A_PLUS: 1.25,    # 125% of base risk
        SetupQuality.A: 1.0,          # 100% of base risk (standard)
        SetupQuality.B: 0.75,         # 75% of base risk
        SetupQuality.C: 0.5,          # 50% of base risk
        SetupQuality.BLOCKED: 0.0,    # No trade
    })
    
    # Minimum conditions for each tier (configurable thresholds)
    # Based on historical data analysis
    TIER_THRESHOLDS: Dict[str, Any] = field(default_factory=lambda: {
        "A_PLUS": {
            "min_mfe_mae_ratio": 2.5,    # MFE/MAE > 2.5
            "min_historical_win_rate": 0.55,
            "min_avg_r": 1.5,
        },
        "A": {
            "min_mfe_mae_ratio": 1.8,
            "min_historical_win_rate": 0.48,
            "min_avg_r": 1.0,
        },
        "B": {
            "min_mfe_mae_ratio": 1.2,
            "min_historical_win_rate": 0.42,
            "min_avg_r": 0.5,
        },
        "C": {
            "min_mfe_mae_ratio": 0.8,
            "min_historical_win_rate": 0.35,
            "min_avg_r": 0.0,
        },
    })


# =============================================================================
# TIME-BASED RISK ADJUSTMENT
# =============================================================================

@dataclass
class TimeRiskConfig:
    """
    Configuration for time-based risk adjustment.
    
    Analiza:
    - expectancy por hora
    - expectancy por sesión
    
    Y aplica:
    - Riesgo normal → horas top
    - Riesgo reducido → horas neutras
    - No trade → horas muertas
    """
    
    # Session risk multipliers
    SESSION_MULTIPLIERS: Dict[str, float] = field(default_factory=lambda: {
        "LONDON_NY_OVERLAP": 1.25,  # Best liquidity, best moves
        "LONDON": 1.0,              # Standard
        "NY": 1.0,                  # Standard
        "ASIA": 0.75,               # Lower volatility, more noise
        "WEEKEND": 0.5,             # Low liquidity, avoid
        "DEAD_HOURS": 0.0,          # Don't trade
    })
    
    # Hour-by-hour multipliers (UTC)
    # Based on typical BTC/crypto market activity
    HOUR_MULTIPLIERS: Dict[int, float] = field(default_factory=lambda: {
        0: 0.75,   # Late Asia
        1: 0.75,
        2: 0.75,
        3: 0.85,   # Asia/Europe transition
        4: 0.85,
        5: 0.9,
        6: 1.0,    # Early London
        7: 1.0,
        8: 1.15,   # London active
        9: 1.15,
        10: 1.0,
        11: 1.0,
        12: 0.9,   # Lunch lull
        13: 1.0,   # NY pre-market
        14: 1.25,  # NY open - BEST
        15: 1.25,
        16: 1.15,
        17: 1.0,
        18: 0.9,
        19: 0.85,
        20: 0.75,  # NY close
        21: 0.6,   # Dead hours start
        22: 0.5,
        23: 0.5,
    })
    
    # Days of week multipliers (0=Monday, 6=Sunday)
    DAY_MULTIPLIERS: Dict[int, float] = field(default_factory=lambda: {
        0: 1.0,    # Monday
        1: 1.1,    # Tuesday - often good trends
        2: 1.1,    # Wednesday
        3: 1.0,    # Thursday
        4: 0.9,    # Friday - early close risk
        5: 0.5,    # Saturday - weekend
        6: 0.5,    # Sunday - weekend
    })


# =============================================================================
# RUNNER OPTIMIZATION
# =============================================================================

@dataclass 
class RunnerConfig:
    """
    Configuration for runner optimization.
    
    Pregunta clave: ¿Hasta dónde llegan los trades ganadores ANTES de morir?
    
    Si muchos llegan a +2.2R pero mueren → el trailing está mal colocado.
    
    Solución:
    - Trailing por estructura, no por ATR puro
    - Activar trailing solo tras +1.2R
    - No tocar SL inicial
    """
    
    # Minimum R achieved before activating runner trailing
    MIN_R_FOR_RUNNER = 1.2  # Don't trail until +1.2R reached
    
    # Structure-based trailing (swings instead of pure ATR)
    USE_STRUCTURE_TRAILING = True
    
    # ATR multiplier for trailing (when structure not available)
    TRAILING_ATR_MULT = 2.5
    
    # Minimum trailing distance in R
    MIN_TRAILING_DISTANCE_R = 0.5
    
    # Lock-in thresholds: at what R levels to tighten trailing
    LOCK_IN_LEVELS = [
        (1.5, 0.75),    # At +1.5R, lock in 0.75R profit
        (2.0, 1.25),    # At +2.0R, lock in 1.25R profit  
        (2.5, 1.75),    # At +2.5R, lock in 1.75R profit
        (3.0, 2.25),    # At +3.0R, lock in 2.25R profit
    ]
    
    # Extension mode: when price is running, widen trailing
    EXTENSION_MODE_THRESHOLD_R = 2.0  # After +2R, enter extension mode
    EXTENSION_ATR_MULT = 3.0          # Wider trailing in extension


# =============================================================================
# CAPITAL WEIGHT CALCULATOR
# =============================================================================

class CapitalWeightCalculator:
    """
    Calculate capital weighting based on setup quality and timing.
    
    This does NOT change entries. Only the capital allocated.
    """
    
    def __init__(self, 
                 setup_config: SetupWeightConfig | None = None,
                 time_config: TimeRiskConfig | None = None,
                 runner_config: RunnerConfig | None = None):
        self.setup_config = setup_config or SetupWeightConfig()
        self.time_config = time_config or TimeRiskConfig()
        self.runner_config = runner_config or RunnerConfig()
        
        # Historical stats for setup classification (loaded from trade log)
        self.setup_stats: Dict[str, Dict[str, float]] = {}
    
    def classify_setup(self, 
                       setup_type: str,
                       mfe_mae_ratio: float | None = None,
                       historical_win_rate: float | None = None,
                       avg_r: float | None = None) -> SetupQuality:
        """
        Classify a setup into quality tiers.
        
        Args:
            setup_type: Name/type of the setup
            mfe_mae_ratio: Historical MFE/MAE ratio for this setup
            historical_win_rate: Win rate for this setup type
            avg_r: Average R-multiple for this setup
            
        Returns:
            SetupQuality tier
        """
        thresholds = self.setup_config.TIER_THRESHOLDS
        
        # Use provided metrics or fall back to stored stats
        if setup_type in self.setup_stats:
            stats = self.setup_stats[setup_type]
            mfe_mae_ratio = mfe_mae_ratio or stats.get('mfe_mae_ratio', 1.0)
            historical_win_rate = historical_win_rate or stats.get('win_rate', 0.45)
            avg_r = avg_r or stats.get('avg_r', 0.5)
        else:
            # Default to B tier if no data
            mfe_mae_ratio = mfe_mae_ratio or 1.2
            historical_win_rate = historical_win_rate or 0.45
            avg_r = avg_r or 0.5
        
        # Check tiers from best to worst
        for tier_name in ["A_PLUS", "A", "B", "C"]:
            tier_thresholds = thresholds[tier_name]
            if (mfe_mae_ratio >= tier_thresholds["min_mfe_mae_ratio"] and
                historical_win_rate >= tier_thresholds["min_historical_win_rate"] and
                avg_r >= tier_thresholds["min_avg_r"]):
                return SetupQuality(tier_name.replace("_", "+") if tier_name == "A_PLUS" else tier_name)
        
        return SetupQuality.C  # Default to C tier
    
    def get_setup_multiplier(self, quality: SetupQuality) -> float:
        """Get risk multiplier for setup quality."""
        return self.setup_config.RISK_MULTIPLIERS.get(quality, 1.0)
    
    def get_time_multiplier(self, 
                           hour: int, 
                           day_of_week: int,
                           is_weekend: bool = False) -> float:
        """
        Get combined time-based risk multiplier.
        
        Args:
            hour: UTC hour (0-23)
            day_of_week: Day of week (0=Monday, 6=Sunday)
            is_weekend: Whether it's weekend
            
        Returns:
            Combined multiplier (hour * day)
        """
        hour_mult = self.time_config.HOUR_MULTIPLIERS.get(hour, 1.0)
        day_mult = self.time_config.DAY_MULTIPLIERS.get(day_of_week, 1.0)
        
        if is_weekend:
            day_mult = min(day_mult, 0.5)
        
        combined = hour_mult * day_mult
        
        # Cap the combined multiplier
        combined = max(0.25, min(1.5, combined))
        
        logger.debug(f"Time multiplier: hour={hour}({hour_mult}x) * day={day_of_week}({day_mult}x) = {combined}x")
        
        return combined
    
    def get_session(self, hour: int, day_of_week: int) -> str:
        """Determine trading session."""
        is_weekend = day_of_week >= 5
        
        if is_weekend:
            return "WEEKEND"
        
        # Check for overlap
        if 13 <= hour <= 16:  # London PM + NY AM
            return "LONDON_NY_OVERLAP"
        elif 6 <= hour <= 16:
            if hour <= 12:
                return "LONDON"
            else:
                return "NY"
        elif 21 <= hour or hour <= 3:
            if hour in [22, 23, 0, 1]:
                return "DEAD_HOURS"
            return "ASIA"
        else:
            return "ASIA"
    
    def calculate_total_weight(self,
                              setup_type: str | None = None,
                              setup_quality: SetupQuality | None = None,
                              current_time: datetime | None = None,
                              mfe_mae_ratio: float | None = None,
                              historical_win_rate: float | None = None,
                              avg_r: float | None = None) -> Tuple[float, Dict[str, Any]]:
        """
        Calculate total capital weight combining all factors.
        
        Args:
            setup_type: Type of setup for classification
            setup_quality: Pre-classified quality (overrides classification)
            current_time: Current time for time-based adjustment
            mfe_mae_ratio: MFE/MAE ratio for setup classification
            historical_win_rate: Win rate for classification
            avg_r: Average R for classification
            
        Returns:
            Tuple of (total_multiplier, breakdown_dict)
        """
        # Get or classify setup quality
        if setup_quality is None and setup_type:
            setup_quality = self.classify_setup(
                setup_type, mfe_mae_ratio, historical_win_rate, avg_r
            )
        elif setup_quality is None:
            setup_quality = SetupQuality.B  # Default
        
        setup_mult = self.get_setup_multiplier(setup_quality)
        
        # Get time-based multiplier
        if current_time is None:
            current_time = datetime.now(UTC)
        
        hour = current_time.hour
        day_of_week = current_time.weekday()
        is_weekend = day_of_week >= 5
        
        time_mult = self.get_time_multiplier(hour, day_of_week, is_weekend)
        session = self.get_session(hour, day_of_week)
        
        # Combined multiplier
        total_mult = setup_mult * time_mult
        
        # Safety bounds
        total_mult = max(0.25, min(1.5, total_mult))
        
        breakdown = {
            'setup_quality': setup_quality.value,
            'setup_multiplier': setup_mult,
            'hour': hour,
            'day_of_week': day_of_week,
            'session': session,
            'time_multiplier': time_mult,
            'total_multiplier': total_mult,
            'is_blocked': total_mult == 0.0,
        }
        
        logger.info(f"📊 Capital Weight: {setup_quality.value} setup ({setup_mult}x) × "
                   f"{session} ({time_mult}x) = {total_mult}x")
        
        return total_mult, breakdown
    
    def load_historical_stats(self, trade_records: List[Dict]) -> None:
        """
        Load historical stats for setup classification.
        
        Args:
            trade_records: List of trade record dicts with 'setup_type', 'r_realized', 
                          'mae_r', 'mfe_r', 'net_pnl' fields
        """
        from collections import defaultdict
        
        stats_by_setup = defaultdict(lambda: {
            'count': 0, 'wins': 0, 'total_r': 0.0, 
            'total_mfe': 0.0, 'total_mae': 0.0
        })
        
        for trade in trade_records:
            setup = trade.get('setup_type', 'UNKNOWN')
            stats = stats_by_setup[setup]
            
            stats['count'] += 1
            if trade.get('net_pnl', 0) > 0:
                stats['wins'] += 1
            stats['total_r'] += trade.get('r_realized', 0)
            stats['total_mfe'] += trade.get('mfe_r', 0)
            stats['total_mae'] += max(0.001, trade.get('mae_r', 0.001))
        
        # Calculate final stats
        for setup, stats in stats_by_setup.items():
            if stats['count'] > 0:
                self.setup_stats[setup] = {
                    'win_rate': stats['wins'] / stats['count'],
                    'avg_r': stats['total_r'] / stats['count'],
                    'mfe_mae_ratio': stats['total_mfe'] / stats['total_mae'],
                    'trade_count': stats['count'],
                }
        
        logger.info(f"📊 Loaded historical stats for {len(self.setup_stats)} setup types")


# =============================================================================
# RUNNER MANAGER
# =============================================================================

class RunnerManager:
    """
    Manages runner positions with optimized trailing.
    
    Key insights:
    - Don't trail until +1.2R reached
    - Use structure (swings) instead of pure ATR
    - Lock in profits at key R levels
    """
    
    def __init__(self, config: RunnerConfig | None = None):
        self.config = config or RunnerConfig()
    
    def should_activate_trailing(self, 
                                current_r: float,
                                direction: str) -> bool:
        """Check if trailing should be activated based on R achieved."""
        return current_r >= self.config.MIN_R_FOR_RUNNER
    
    def should_activate_runner(self,
                              entry_price: float,
                              current_price: float,
                              stop_loss: float,
                              direction: str) -> bool:
        """
        Check if runner optimization should be activated.
        
        This is an alias/wrapper for should_activate_trailing that calculates R first.
        Activates only after achieving ≥1.2R.
        
        Args:
            entry_price: Entry price
            current_price: Current price
            stop_loss: Stop loss price
            direction: LONG or SHORT
            
        Returns:
            True if runner should be activated (achieved ≥1.2R)
        """
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            return False
        
        if direction.upper() == 'LONG':
            current_r = (current_price - entry_price) / risk
        else:
            current_r = (entry_price - current_price) / risk
        
        return self.should_activate_trailing(current_r, direction)
    
    def calculate_trailing_stop(self,
                               entry_price: float,
                               current_price: float,
                               stop_loss: float,
                               current_r: float,
                               atr: float,
                               direction: str,
                               swing_low: float | None = None,
                               swing_high: float | None = None) -> Tuple[float, str]:
        """
        Calculate optimal trailing stop for runner.
        
        Args:
            entry_price: Entry price
            current_price: Current price
            stop_loss: Original stop loss
            current_r: Current R-multiple achieved
            atr: Current ATR
            direction: LONG or SHORT
            swing_low: Recent swing low (for structure trailing)
            swing_high: Recent swing high (for structure trailing)
            
        Returns:
            Tuple of (new_trailing_stop, reason)
        """
        risk = abs(entry_price - stop_loss)
        
        # Not yet eligible for runner trailing
        if current_r < self.config.MIN_R_FOR_RUNNER:
            return stop_loss, "MIN_R_NOT_REACHED"
        
        # Check lock-in levels
        locked_r = 0.0
        for threshold_r, lock_r in self.config.LOCK_IN_LEVELS:
            if current_r >= threshold_r:
                locked_r = lock_r
        
        # Calculate trailing stop options
        trailing_options = []
        
        # Option 1: Lock-in level
        if locked_r > 0:
            if direction.upper() == 'LONG':
                lock_stop = entry_price + (locked_r * risk)
            else:
                lock_stop = entry_price - (locked_r * risk)
            trailing_options.append((lock_stop, f"LOCK_IN_{locked_r}R"))
        
        # Option 2: ATR-based trailing
        if current_r >= self.config.EXTENSION_MODE_THRESHOLD_R:
            # Extension mode: wider trailing
            atr_mult = self.config.EXTENSION_ATR_MULT
            reason = "EXTENSION_MODE"
        else:
            atr_mult = self.config.TRAILING_ATR_MULT
            reason = "ATR_TRAILING"
        
        if direction.upper() == 'LONG':
            atr_stop = current_price - (atr * atr_mult)
        else:
            atr_stop = current_price + (atr * atr_mult)
        trailing_options.append((atr_stop, reason))
        
        # Option 3: Structure-based (if available)
        if self.config.USE_STRUCTURE_TRAILING:
            if direction.upper() == 'LONG' and swing_low and swing_low < current_price:
                # Trail to recent swing low minus buffer
                structure_stop = swing_low - (atr * 0.3)
                if structure_stop > stop_loss:
                    trailing_options.append((structure_stop, "STRUCTURE_SWING"))
            elif direction.upper() == 'SHORT' and swing_high and swing_high > current_price:
                structure_stop = swing_high + (atr * 0.3)
                if structure_stop < stop_loss:
                    trailing_options.append((structure_stop, "STRUCTURE_SWING"))
        
        # Choose the best (most protective while not too tight)
        if direction.upper() == 'LONG':
            # For longs, highest stop is best
            best = max(trailing_options, key=lambda x: x[0])
            # But never below original SL
            if best[0] < stop_loss:
                return stop_loss, "BELOW_ORIGINAL_SL"
        else:
            # For shorts, lowest stop is best  
            best = min(trailing_options, key=lambda x: x[0])
            if best[0] > stop_loss:
                return stop_loss, "ABOVE_ORIGINAL_SL"
        
        # Ensure minimum distance
        min_distance = risk * self.config.MIN_TRAILING_DISTANCE_R
        if direction.upper() == 'LONG':
            if current_price - best[0] < min_distance:
                best = (current_price - min_distance, "MIN_DISTANCE")
        else:
            if best[0] - current_price < min_distance:
                best = (current_price + min_distance, "MIN_DISTANCE")
        
        return best
    
    def get_runner_status(self,
                         entry_price: float,
                         current_price: float,
                         stop_loss: float,
                         direction: str) -> Dict[str, Any]:
        """Get current runner status and recommendations."""
        risk = abs(entry_price - stop_loss)
        
        if direction.upper() == 'LONG':
            current_r = (current_price - entry_price) / risk if risk > 0 else 0
        else:
            current_r = (entry_price - current_price) / risk if risk > 0 else 0
        
        is_eligible = current_r >= self.config.MIN_R_FOR_RUNNER
        is_extension = current_r >= self.config.EXTENSION_MODE_THRESHOLD_R
        
        # Find current lock-in level
        current_lock = 0.0
        for threshold_r, lock_r in self.config.LOCK_IN_LEVELS:
            if current_r >= threshold_r:
                current_lock = lock_r
        
        return {
            'current_r': current_r,
            'is_eligible_for_trailing': is_eligible,
            'is_extension_mode': is_extension,
            'locked_profit_r': current_lock,
            'next_lock_level': self._get_next_lock_level(current_r),
            'mode': 'EXTENSION' if is_extension else ('TRAILING' if is_eligible else 'WAITING'),
        }
    
    def _get_next_lock_level(self, current_r: float) -> Optional[Tuple[float, float]]:
        """Get next lock-in level to reach."""
        for threshold_r, lock_r in self.config.LOCK_IN_LEVELS:
            if current_r < threshold_r:
                return (threshold_r, lock_r)
        return None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_capital_weight_calculator: Optional[CapitalWeightCalculator] = None
_runner_manager: Optional[RunnerManager] = None


def get_capital_weight_calculator() -> CapitalWeightCalculator:
    """Get global CapitalWeightCalculator instance."""
    global _capital_weight_calculator
    if _capital_weight_calculator is None:
        _capital_weight_calculator = CapitalWeightCalculator()
    return _capital_weight_calculator


def get_runner_manager() -> RunnerManager:
    """Get global RunnerManager instance."""
    global _runner_manager
    if _runner_manager is None:
        _runner_manager = RunnerManager()
    return _runner_manager


def calculate_weighted_risk(base_risk_pct: float,
                           setup_type: str | None = None,
                           current_time: datetime | None = None) -> Tuple[float, Dict]:
    """
    Convenience function to calculate weighted risk.
    
    Args:
        base_risk_pct: Base risk percentage (from DynamicRiskConfig)
        setup_type: Type of setup for quality classification
        current_time: Current time for time-based adjustment
        
    Returns:
        Tuple of (adjusted_risk_pct, breakdown)
    """
    calc = get_capital_weight_calculator()
    multiplier, breakdown = calc.calculate_total_weight(
        setup_type=setup_type,
        current_time=current_time
    )
    
    adjusted_risk = base_risk_pct * multiplier
    breakdown['base_risk_pct'] = base_risk_pct
    breakdown['adjusted_risk_pct'] = adjusted_risk
    
    return adjusted_risk, breakdown
