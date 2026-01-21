"""
Risk Manager Middleware - El "Interceptor" Inteligente

Arquitectura: Patrón "Middleware" de Gestión
NO toca el código que genera la señal de entrada (signal_engine.py).
Intercepta la orden justo antes de que llegue al Execution Engine.

Si el signal_engine dice "Long 1 BTC", el middleware:
1. Consulta el Régimen de Mercado
2. Aplica el Weighting del Setup
3. Aplica Risk Escalation (racha)
4. Lo transforma en "Long 0.75 BTC con TP1 al 1.1R"

Beneficio: Si algo falla, puedes desconectar el middleware 
y el bot seguirá funcionando con su configuración base (Fail-safe).

Features:
1. Setup-based Edge Multiplier (A/B/C setups)
2. Risk Escalation (racha de trades)
3. Shadow Trading (paper trade el riesgo dinámico)
4. PPO Metric (Profit per Opportunity)
5. TP1 Optimization based on MFE data
6. 3-Phase Trailing Stop integration
7. BE Real (Break-Even incluyendo comisiones)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, UTC
from enum import Enum
import json
from pathlib import Path

from src.engine.regime.market_regime import (
    ManagementRegime, get_regime_classifier, get_regime_risk_multiplier,
    RegimeGuidance, REGIME_GUIDANCE
)

logger = logging.getLogger(__name__)


# =============================================================================
# SETUP WEIGHT CONFIGURATION
# =============================================================================

class SetupGrade(Enum):
    """Setup grades based on historical performance."""
    A = "A"    # Ganador histórico: x1.25 risk
    B = "B"    # Neutral: x1.0 risk
    C = "C"    # Probabilístico: x0.5 risk


@dataclass
class SetupWeightConfig:
    """Configuration for setup-based risk weighting."""
    
    # Risk multipliers by setup grade
    GRADE_MULTIPLIERS: Dict[SetupGrade, float] = field(default_factory=lambda: {
        SetupGrade.A: 1.25,  # +25% risk for proven setups
        SetupGrade.B: 1.0,   # Base risk
        SetupGrade.C: 0.5,   # -50% risk for experimental
    })
    
    # Minimum trades to grade a setup
    MIN_TRADES_FOR_GRADING: int = 30
    
    # Thresholds for grading
    GRADE_A_MIN_EXPECTANCY: float = 0.8    # Min R expectancy
    GRADE_A_MIN_PROFIT_FACTOR: float = 1.5
    GRADE_B_MIN_EXPECTANCY: float = 0.3
    GRADE_B_MIN_PROFIT_FACTOR: float = 1.1


# =============================================================================
# RISK ESCALATION (STREAK TRACKING)
# =============================================================================

@dataclass
class RiskEscalationConfig:
    """Configuration for risk escalation based on streaks."""
    
    # Escalation parameters
    BASE_RISK_PCT: float = 0.8             # 0.8% base risk
    RISK_INCREMENT_PER_WIN: float = 0.1    # +0.1% per win in streak
    WIN_STREAK_FOR_ESCALATION: int = 5     # Need 5 wins to start escalation
    MAX_ESCALATED_RISK_PCT: float = 1.5    # Hard cap: 1.5% max
    
    # De-escalation parameters
    LOSSES_TO_RESET: int = 2               # 2 consecutive losses = reset
    
    # Safety parameters
    HARD_CAP_RISK_PCT: float = 2.0         # NEVER exceed 2% per trade
    
    # Shadow mode (paper trade the escalation)
    SHADOW_MODE_ENABLED: bool = True       # Track but don't apply
    SHADOW_TRADES_TO_ACTIVATE: int = 30    # Need 30 shadow trades before live


class StreakTracker:
    """Tracks win/loss streaks for risk escalation."""
    
    def __init__(self, config: RiskEscalationConfig = None):
        self.config = config or RiskEscalationConfig()
        
        # Current streak tracking
        self.current_streak: int = 0  # Positive = wins, negative = losses
        self.last_n_results: List[bool] = []  # True = win, False = loss
        
        # Escalation state
        self.current_escalated_risk: float = self.config.BASE_RISK_PCT
        self.is_escalated: bool = False
        
        # Shadow tracking
        self.shadow_pnl_real: float = 0.0
        self.shadow_pnl_escalated: float = 0.0
        self.shadow_trade_count: int = 0
        self.shadow_consecutive_outperform: int = 0
        
    def record_trade(self, is_win: bool, pnl_real: float, pnl_at_escalated_risk: float):
        """
        Record a trade result and update streak.
        
        Args:
            is_win: Whether trade was profitable
            pnl_real: Actual PnL
            pnl_at_escalated_risk: What PnL would have been with escalated risk
        """
        # Update streak
        if is_win:
            if self.current_streak >= 0:
                self.current_streak += 1
            else:
                self.current_streak = 1
        else:
            if self.current_streak <= 0:
                self.current_streak -= 1
            else:
                self.current_streak = -1
        
        # Keep last N results
        self.last_n_results.append(is_win)
        if len(self.last_n_results) > 20:
            self.last_n_results = self.last_n_results[-20:]
        
        # Update escalation state
        self._update_escalation_state()
        
        # Shadow tracking
        self.shadow_pnl_real += pnl_real
        self.shadow_pnl_escalated += pnl_at_escalated_risk
        self.shadow_trade_count += 1
        
        # Track consecutive outperformance
        if pnl_at_escalated_risk > pnl_real:
            self.shadow_consecutive_outperform += 1
        else:
            self.shadow_consecutive_outperform = 0
        
        logger.info(f"📊 STREAK: {self.current_streak:+d} | "
                   f"Escalated: {self.is_escalated} ({self.current_escalated_risk:.2f}%) | "
                   f"Shadow: {self.shadow_pnl_escalated:+.2f} vs Real: {self.shadow_pnl_real:+.2f}")
    
    def _update_escalation_state(self):
        """Update risk escalation based on streak."""
        # Check for de-escalation (2 consecutive losses)
        if self.current_streak <= -self.config.LOSSES_TO_RESET:
            self.is_escalated = False
            self.current_escalated_risk = self.config.BASE_RISK_PCT
            logger.info(f"⬇️ RISK RESET: {self.config.LOSSES_TO_RESET} losses → back to base risk")
            return
        
        # Check for escalation (5 consecutive wins)
        if self.current_streak >= self.config.WIN_STREAK_FOR_ESCALATION:
            self.is_escalated = True
            
            # Calculate escalated risk
            wins_above_threshold = self.current_streak - self.config.WIN_STREAK_FOR_ESCALATION + 1
            additional_risk = wins_above_threshold * self.config.RISK_INCREMENT_PER_WIN
            
            new_risk = min(
                self.config.BASE_RISK_PCT + additional_risk,
                self.config.MAX_ESCALATED_RISK_PCT
            )
            
            if new_risk > self.current_escalated_risk:
                logger.info(f"⬆️ RISK ESCALATION: {self.current_streak} wins → "
                           f"risk {self.current_escalated_risk:.2f}% → {new_risk:.2f}%")
            
            self.current_escalated_risk = new_risk
    
    def get_current_risk_pct(self, apply_escalation: bool = True) -> float:
        """
        Get current risk percentage.
        
        Args:
            apply_escalation: If False, always return base risk (shadow mode)
        """
        if not apply_escalation or self.config.SHADOW_MODE_ENABLED:
            return self.config.BASE_RISK_PCT
        
        # Check if shadow mode should be deactivated
        if self.should_activate_escalation():
            return min(self.current_escalated_risk, self.config.HARD_CAP_RISK_PCT)
        
        return self.config.BASE_RISK_PCT
    
    def should_activate_escalation(self) -> bool:
        """
        Check if escalation should be activated (shadow mode passed).
        
        Criteria: Shadow PnL must beat Real PnL for 30 consecutive trades.
        """
        if not self.config.SHADOW_MODE_ENABLED:
            return True
        
        return self.shadow_consecutive_outperform >= self.config.SHADOW_TRADES_TO_ACTIVATE
    
    def get_shadow_comparison(self) -> Dict[str, float]:
        """Get shadow vs real comparison."""
        return {
            'pnl_real': self.shadow_pnl_real,
            'pnl_escalated': self.shadow_pnl_escalated,
            'trade_count': self.shadow_trade_count,
            'consecutive_outperform': self.shadow_consecutive_outperform,
            'escalation_active': self.should_activate_escalation()
        }


# =============================================================================
# TP1 OPTIMIZER (Based on MFE Data)
# =============================================================================

class TP1Optimizer:
    """
    Optimizes TP1 based on MFE (Maximum Favorable Excursion) data.
    
    Regla: Si el 80% de trades llegan a 1.3R antes de retroceder,
    mueve TP1 de 1.0R a 1.2R.
    """
    
    def __init__(self):
        self.mfe_history: List[float] = []  # MFE in R terms
        self.current_tp1_r: float = 1.0
    
    def record_mfe(self, mfe_r: float):
        """Record MFE for a completed trade."""
        self.mfe_history.append(mfe_r)
        
        # Keep last 200 trades
        if len(self.mfe_history) > 200:
            self.mfe_history = self.mfe_history[-200:]
        
        # Update TP1 recommendation
        self._update_tp1_recommendation()
    
    def _update_tp1_recommendation(self):
        """Update TP1 recommendation based on MFE data."""
        if len(self.mfe_history) < 50:
            return  # Need more data
        
        # Check percentiles
        mfe_array = sorted(self.mfe_history)
        p80_mfe = mfe_array[int(len(mfe_array) * 0.80)]
        
        # If 80% of trades reach at least 1.3R, we can move TP1 higher
        if p80_mfe >= 1.3:
            self.current_tp1_r = 1.2
        elif p80_mfe >= 1.2:
            self.current_tp1_r = 1.1
        else:
            self.current_tp1_r = 1.0
        
        logger.debug(f"TP1 Optimizer: P80 MFE = {p80_mfe:.2f}R → TP1 = {self.current_tp1_r}R")
    
    def get_recommended_tp1_r(self) -> float:
        """Get recommended TP1 in R multiples."""
        return self.current_tp1_r
    
    def get_pf_at_tp1_only(self) -> float:
        """
        Calculate theoretical Profit Factor if we closed all at TP1.
        
        Rule: If PF at TP1 < 1.0, entries are bad.
        """
        if len(self.mfe_history) < 20:
            return 0.0
        
        # Count wins (MFE >= TP1) vs losses
        wins = sum(1 for mfe in self.mfe_history if mfe >= self.current_tp1_r)
        losses = len(self.mfe_history) - wins
        
        if losses == 0:
            return float('inf')
        
        # Approximate PF: wins pay 1R, losses lose 1R
        gross_profit = wins * self.current_tp1_r
        gross_loss = losses * 1.0  # Assume -1R losses
        
        return gross_profit / gross_loss if gross_loss > 0 else float('inf')


# =============================================================================
# PPO METRIC (Profit Per Opportunity)
# =============================================================================

class PPOTracker:
    """
    Tracks Profit Per Opportunity (PPO).
    
    Formula: PPO = PnL_Total_Real / Número_de_Señales_Válidas
    
    Interpretation:
    - PPO bajo + PnL positivo: Filtrando demasiado (dejando dinero)
    - PPO alto: Optimizando cada oportunidad al máximo
    """
    
    def __init__(self):
        self.total_pnl_real: float = 0.0
        self.valid_signals_count: int = 0
        self.executed_trades_count: int = 0
        self.filtered_signals_count: int = 0
    
    def record_signal(self, was_executed: bool, pnl: float = 0.0):
        """
        Record a signal (executed or filtered).
        
        Args:
            was_executed: True if signal resulted in a trade
            pnl: PnL if trade was executed
        """
        self.valid_signals_count += 1
        
        if was_executed:
            self.executed_trades_count += 1
            self.total_pnl_real += pnl
        else:
            self.filtered_signals_count += 1
    
    def get_ppo(self) -> float:
        """Get current PPO."""
        if self.valid_signals_count == 0:
            return 0.0
        return self.total_pnl_real / self.valid_signals_count
    
    def get_execution_rate(self) -> float:
        """Get % of signals that were executed."""
        if self.valid_signals_count == 0:
            return 0.0
        return self.executed_trades_count / self.valid_signals_count
    
    def get_summary(self) -> Dict[str, Any]:
        """Get PPO summary."""
        return {
            'ppo': self.get_ppo(),
            'total_pnl': self.total_pnl_real,
            'valid_signals': self.valid_signals_count,
            'executed_trades': self.executed_trades_count,
            'filtered_signals': self.filtered_signals_count,
            'execution_rate': self.get_execution_rate(),
            'diagnosis': self._diagnose()
        }
    
    def _diagnose(self) -> str:
        """Diagnose PPO health."""
        ppo = self.get_ppo()
        exec_rate = self.get_execution_rate()
        
        if self.valid_signals_count < 50:
            return "INSUFFICIENT_DATA"
        
        if ppo > 0 and exec_rate < 0.3:
            return "OVER_FILTERING: Dejando dinero en la mesa"
        elif ppo > 0 and exec_rate > 0.7:
            return "OPTIMAL: Maximizando oportunidades"
        elif ppo < 0:
            return "EDGE_PROBLEM: Señales no tienen edge"
        else:
            return "BALANCED"


# =============================================================================
# BREAK-EVEN CALCULATOR (Including Fees)
# =============================================================================

class BECalculator:
    """
    Calculates true Break-Even including round-trip fees.
    
    Rule: BE real = Entry + (Fees_RoundTrip / Position_Size)
    Never move to BE before covering 2x fees in profit.
    """
    
    def __init__(self, 
                 maker_fee_pct: float = 0.02,   # 0.02% = 2 bps
                 taker_fee_pct: float = 0.05):  # 0.05% = 5 bps
        self.maker_fee_pct = maker_fee_pct / 100.0
        self.taker_fee_pct = taker_fee_pct / 100.0
    
    def calculate_roundtrip_fees(self, 
                                  entry_price: float,
                                  position_size: float,
                                  use_maker: bool = True) -> float:
        """Calculate round-trip fees in quote currency."""
        notional = entry_price * position_size
        fee_pct = self.maker_fee_pct if use_maker else self.taker_fee_pct
        
        # Entry + Exit fees
        roundtrip_fees = notional * fee_pct * 2
        
        return roundtrip_fees
    
    def calculate_be_price(self,
                           entry_price: float,
                           position_size: float,
                           direction: str,
                           use_maker: bool = True) -> float:
        """
        Calculate true BE price including fees.
        
        Args:
            entry_price: Entry price
            position_size: Position size in base currency
            direction: 'LONG' or 'SHORT'
            use_maker: Whether using maker orders
            
        Returns:
            BE price that covers all fees
        """
        roundtrip_fees = self.calculate_roundtrip_fees(
            entry_price, position_size, use_maker
        )
        
        # 🔒 FIX: Guard against division by zero
        if position_size <= 0:
            return entry_price  # Return entry price if no position
        
        # Fee buffer per unit
        fee_per_unit = roundtrip_fees / position_size
        
        if direction.upper() == 'LONG':
            be_price = entry_price + fee_per_unit
        else:
            be_price = entry_price - fee_per_unit
        
        return be_price
    
    def can_move_to_be(self,
                       entry_price: float,
                       current_price: float,
                       position_size: float,
                       direction: str,
                       min_profit_multiple: float = 2.0) -> Tuple[bool, str]:
        """
        Check if we can move to BE (need at least 2x fees in profit).
        
        Returns:
            Tuple of (can_move, reason)
        """
        roundtrip_fees = self.calculate_roundtrip_fees(
            entry_price, position_size, use_maker=True
        )
        
        # Current profit
        if direction.upper() == 'LONG':
            profit = (current_price - entry_price) * position_size
        else:
            profit = (entry_price - current_price) * position_size
        
        min_profit_needed = roundtrip_fees * min_profit_multiple
        
        if profit >= min_profit_needed:
            return True, f"Profit ${profit:.2f} >= {min_profit_multiple}x fees ${min_profit_needed:.2f}"
        else:
            return False, f"Profit ${profit:.2f} < {min_profit_multiple}x fees ${min_profit_needed:.2f}"


# =============================================================================
# 3-PHASE TRAILING STOP
# =============================================================================

@dataclass
class TrailingPhaseConfig:
    """Configuration for 3-phase trailing stop."""
    
    # Phase 1: Protection (at 1.1R, move to BE + fees)
    PHASE1_TRIGGER_R: float = 1.1
    PHASE1_LOCK_R: float = 0.0  # BE (plus fees)
    
    # Phase 2: Profit (at 2.0R, lock 1.0R)
    PHASE2_TRIGGER_R: float = 2.0
    PHASE2_LOCK_R: float = 1.0
    
    # Phase 3: Extension (only in TREND_EXPANSION, structural trailing)
    PHASE3_TRIGGER_R: float = 2.5
    PHASE3_USE_STRUCTURAL: bool = True  # Use last 3 candle low/high
    PHASE3_STRUCTURAL_CANDLES: int = 3  # Number of candles for structural trailing


class ThreePhaseTrailing:
    """
    3-Phase Trailing Stop System.
    
    Phase 1 (Protection): At 1.1R → move to BE + fees
    Phase 2 (Profit): At 2.0R → lock 1.0R real profit
    Phase 3 (Extension): Only in TREND_EXPANSION → structural trailing
    """
    
    def __init__(self, 
                 config: TrailingPhaseConfig = None,
                 be_calculator: BECalculator = None):
        self.config = config or TrailingPhaseConfig()
        self.be_calculator = be_calculator or BECalculator()
    
    def calculate_trailing_stop(self,
                                 entry_price: float,
                                 current_price: float,
                                 stop_loss: float,
                                 position_size: float,
                                 direction: str,
                                 regime: ManagementRegime = None,
                                 recent_lows: List[float] = None,
                                 recent_highs: List[float] = None,
                                 atr: float = None) -> Tuple[float, str, int]:
        """
        Calculate trailing stop based on current phase.
        
        Returns:
            Tuple of (new_stop, reason, current_phase)
        """
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            return stop_loss, "INVALID_RISK", 0
        
        # Calculate current R
        if direction.upper() == 'LONG':
            current_r = (current_price - entry_price) / risk
        else:
            current_r = (entry_price - current_price) / risk
        
        # Determine phase
        phase = 0
        new_stop = stop_loss
        reason = "INITIAL"
        
        # Phase 1: Protection (BE + fees)
        if current_r >= self.config.PHASE1_TRIGGER_R:
            phase = 1
            be_price = self.be_calculator.calculate_be_price(
                entry_price, position_size, direction
            )
            
            if direction.upper() == 'LONG':
                new_stop = max(stop_loss, be_price)
            else:
                new_stop = min(stop_loss, be_price)
            
            reason = f"PHASE1_BE (at {current_r:.2f}R)"
        
        # Phase 2: Lock profit
        if current_r >= self.config.PHASE2_TRIGGER_R:
            phase = 2
            lock_price_distance = risk * self.config.PHASE2_LOCK_R
            
            if direction.upper() == 'LONG':
                lock_stop = entry_price + lock_price_distance
                new_stop = max(new_stop, lock_stop)
            else:
                lock_stop = entry_price - lock_price_distance
                new_stop = min(new_stop, lock_stop)
            
            reason = f"PHASE2_LOCK_{self.config.PHASE2_LOCK_R}R (at {current_r:.2f}R)"
        
        # Phase 3: Extension (only in TREND_EXPANSION)
        if current_r >= self.config.PHASE3_TRIGGER_R:
            if regime == ManagementRegime.TREND_EXPANSION and self.config.PHASE3_USE_STRUCTURAL:
                phase = 3
                
                # Structural trailing: use recent swing low/high
                if direction.upper() == 'LONG' and recent_lows:
                    structural_low = min(recent_lows[-self.config.PHASE3_STRUCTURAL_CANDLES:])
                    # Add small buffer
                    buffer = atr * 0.3 if atr else risk * 0.1
                    structural_stop = structural_low - buffer
                    new_stop = max(new_stop, structural_stop)
                    reason = f"PHASE3_STRUCTURAL (low={structural_low:.2f})"
                    
                elif direction.upper() == 'SHORT' and recent_highs:
                    structural_high = max(recent_highs[-self.config.PHASE3_STRUCTURAL_CANDLES:])
                    buffer = atr * 0.3 if atr else risk * 0.1
                    structural_stop = structural_high + buffer
                    new_stop = min(new_stop, structural_stop)
                    reason = f"PHASE3_STRUCTURAL (high={structural_high:.2f})"
        
        return new_stop, reason, phase


# =============================================================================
# RISK MANAGER MIDDLEWARE (Main Class)
# =============================================================================

@dataclass 
class AugmentedOrder:
    """Order after middleware processing."""
    
    # Original signal
    original_direction: str
    original_size: float
    
    # Augmented values
    adjusted_size: float
    adjusted_risk_pct: float
    tp1_r: float
    tp2_r: float
    trailing_config: TrailingPhaseConfig
    
    # Context
    regime: ManagementRegime
    setup_grade: SetupGrade
    risk_multiplier: float
    
    # Metadata
    should_trade: bool = True
    block_reason: str = ""
    
    # Shadow tracking
    shadow_risk_pct: float = 0.0  # What risk would be with escalation


class RiskManagerMiddleware:
    """
    The Intelligent Interceptor.
    
    Receives raw signal → Returns augmented order with:
    - Regime-adjusted sizing
    - Setup-weighted risk
    - Escalation-adjusted risk (or shadow tracking)
    - Optimized TP1
    - 3-phase trailing configuration
    """
    
    def __init__(self,
                 base_risk_pct: float = 0.8,
                 setup_config: SetupWeightConfig = None,
                 escalation_config: RiskEscalationConfig = None,
                 trailing_config: TrailingPhaseConfig = None):
        
        self.base_risk_pct = base_risk_pct
        self.setup_config = setup_config or SetupWeightConfig()
        
        # Initialize components
        self.streak_tracker = StreakTracker(escalation_config or RiskEscalationConfig())
        self.tp1_optimizer = TP1Optimizer()
        self.ppo_tracker = PPOTracker()
        self.be_calculator = BECalculator()
        self.trailing = ThreePhaseTrailing(trailing_config, self.be_calculator)
        
        # Setup performance tracking
        self.setup_performance: Dict[str, Dict] = {}  # setup_name -> stats
        
        # Trade log for shadow tracking
        self.shadow_log: List[Dict] = []
        
        # Enable/disable middleware (fail-safe)
        self.enabled: bool = True
    
    def classify_setup(self, setup_name: str) -> SetupGrade:
        """Classify setup based on historical performance."""
        stats = self.setup_performance.get(setup_name)
        
        if not stats or stats.get('count', 0) < self.setup_config.MIN_TRADES_FOR_GRADING:
            return SetupGrade.B  # Default to neutral
        
        expectancy = stats.get('expectancy', 0)
        pf = stats.get('profit_factor', 1.0)
        
        if (expectancy >= self.setup_config.GRADE_A_MIN_EXPECTANCY and 
            pf >= self.setup_config.GRADE_A_MIN_PROFIT_FACTOR):
            return SetupGrade.A
        elif (expectancy >= self.setup_config.GRADE_B_MIN_EXPECTANCY and 
              pf >= self.setup_config.GRADE_B_MIN_PROFIT_FACTOR):
            return SetupGrade.B
        else:
            return SetupGrade.C
    
    def process_signal(self,
                       direction: str,
                       base_size: float,
                       entry_price: float,
                       stop_loss: float,
                       setup_name: str = "DEFAULT",
                       regime: ManagementRegime = None,
                       capital: float = 10000.0) -> AugmentedOrder:
        """
        Process a raw signal through the middleware.
        
        Args:
            direction: 'LONG' or 'SHORT'
            base_size: Original position size
            entry_price: Entry price
            stop_loss: Stop loss price
            setup_name: Name of the setup for weighting
            regime: Current market regime (or will be fetched)
            capital: Current account capital
            
        Returns:
            AugmentedOrder with adjusted parameters
        """
        if not self.enabled:
            # Fail-safe: pass through unchanged
            return AugmentedOrder(
                original_direction=direction,
                original_size=base_size,
                adjusted_size=base_size,
                adjusted_risk_pct=self.base_risk_pct,
                tp1_r=1.0,
                tp2_r=2.0,
                trailing_config=TrailingPhaseConfig(),
                regime=ManagementRegime.MEAN_REVERSION,
                setup_grade=SetupGrade.B,
                risk_multiplier=1.0,
                should_trade=True
            )
        
        # Get regime if not provided
        if regime is None:
            regime = get_regime_classifier().get_current_regime() or ManagementRegime.MEAN_REVERSION
        
        # Get regime guidance
        regime_guidance = REGIME_GUIDANCE[regime]
        
        # Classify setup
        setup_grade = self.classify_setup(setup_name)
        setup_multiplier = self.setup_config.GRADE_MULTIPLIERS[setup_grade]
        
        # Calculate combined risk multiplier
        regime_multiplier = regime_guidance.risk_multiplier
        combined_multiplier = setup_multiplier * regime_multiplier
        
        # Get current risk (with or without escalation)
        base_risk = self.streak_tracker.get_current_risk_pct(apply_escalation=False)
        escalated_risk = self.streak_tracker.current_escalated_risk
        
        # Apply multipliers
        adjusted_risk_pct = base_risk * combined_multiplier
        shadow_risk_pct = escalated_risk * combined_multiplier
        
        # Hard cap
        hard_cap = self.streak_tracker.config.HARD_CAP_RISK_PCT
        adjusted_risk_pct = min(adjusted_risk_pct, hard_cap)
        shadow_risk_pct = min(shadow_risk_pct, hard_cap)
        
        # Calculate adjusted size based on risk
        risk_per_trade = capital * (adjusted_risk_pct / 100.0)
        stop_distance = abs(entry_price - stop_loss)
        
        if stop_distance > 0:
            adjusted_size = risk_per_trade / stop_distance
        else:
            adjusted_size = base_size
        
        # Check if we should trade (CHOPPY_DEAD with multiplier near 0)
        should_trade = combined_multiplier >= 0.25
        block_reason = "" if should_trade else f"Low regime multiplier ({combined_multiplier:.2f})"
        
        # Get optimized TP1
        tp1_r = self.tp1_optimizer.get_recommended_tp1_r()
        
        # Adjust trailing config based on regime
        trailing_config = TrailingPhaseConfig()
        if regime == ManagementRegime.TREND_EXPANSION:
            trailing_config.PHASE3_USE_STRUCTURAL = True
        else:
            trailing_config.PHASE3_USE_STRUCTURAL = False
        
        logger.info(f"🔧 MIDDLEWARE: {setup_name} ({setup_grade.value}) | "
                   f"Regime: {regime.value} | "
                   f"Risk: {self.base_risk_pct}% → {adjusted_risk_pct:.2f}% | "
                   f"Size: {base_size:.6f} → {adjusted_size:.6f} | "
                   f"TP1: {tp1_r}R")
        
        return AugmentedOrder(
            original_direction=direction,
            original_size=base_size,
            adjusted_size=adjusted_size,
            adjusted_risk_pct=adjusted_risk_pct,
            tp1_r=tp1_r,
            tp2_r=2.0,
            trailing_config=trailing_config,
            regime=regime,
            setup_grade=setup_grade,
            risk_multiplier=combined_multiplier,
            should_trade=should_trade,
            block_reason=block_reason,
            shadow_risk_pct=shadow_risk_pct
        )
    
    def record_trade_result(self,
                            setup_name: str,
                            pnl: float,
                            mfe_r: float,
                            base_risk_pct: float,
                            escalated_risk_pct: float,
                            entry_price: float,
                            stop_loss: float):
        """
        Record trade result for learning.
        
        Args:
            setup_name: Setup name
            pnl: Realized PnL
            mfe_r: Maximum Favorable Excursion in R
            base_risk_pct: Base risk used
            escalated_risk_pct: What risk would have been with escalation
            entry_price: Entry price
            stop_loss: Stop loss price
        """
        is_win = pnl > 0
        
        # Calculate what PnL would have been with escalated risk
        risk_ratio = escalated_risk_pct / base_risk_pct if base_risk_pct > 0 else 1.0
        pnl_at_escalated = pnl * risk_ratio
        
        # Update streak tracker
        self.streak_tracker.record_trade(is_win, pnl, pnl_at_escalated)
        
        # Update TP1 optimizer
        self.tp1_optimizer.record_mfe(mfe_r)
        
        # Update PPO tracker
        self.ppo_tracker.record_signal(True, pnl)
        
        # Update setup performance
        if setup_name not in self.setup_performance:
            self.setup_performance[setup_name] = {
                'count': 0, 'wins': 0, 'total_pnl': 0.0, 'total_r': 0.0
            }
        
        stats = self.setup_performance[setup_name]
        stats['count'] += 1
        if is_win:
            stats['wins'] += 1
        stats['total_pnl'] += pnl
        
        # Calculate R
        risk = abs(entry_price - stop_loss)
        r_realized = pnl / risk if risk > 0 else 0
        stats['total_r'] += r_realized
        
        # Update derived metrics
        stats['win_rate'] = stats['wins'] / stats['count']
        stats['expectancy'] = stats['total_r'] / stats['count']
        
        # Log shadow trade
        self.shadow_log.append({
            'timestamp': datetime.now(UTC).isoformat(),
            'setup': setup_name,
            'pnl_real': pnl,
            'pnl_escalated': pnl_at_escalated,
            'mfe_r': mfe_r,
            'is_win': is_win
        })
        
        # Keep log manageable
        if len(self.shadow_log) > 500:
            self.shadow_log = self.shadow_log[-500:]
    
    def record_filtered_signal(self, setup_name: str):
        """Record a signal that was filtered (not traded)."""
        self.ppo_tracker.record_signal(False, 0.0)
    
    def get_summary(self) -> Dict[str, Any]:
        """Get middleware summary."""
        return {
            'enabled': self.enabled,
            'streak': {
                'current': self.streak_tracker.current_streak,
                'is_escalated': self.streak_tracker.is_escalated,
                'current_risk_pct': self.streak_tracker.current_escalated_risk
            },
            'shadow': self.streak_tracker.get_shadow_comparison(),
            'ppo': self.ppo_tracker.get_summary(),
            'tp1': {
                'current_r': self.tp1_optimizer.current_tp1_r,
                'pf_at_tp1': self.tp1_optimizer.get_pf_at_tp1_only()
            },
            'setups': {
                name: {
                    'grade': self.classify_setup(name).value,
                    **stats
                }
                for name, stats in self.setup_performance.items()
            }
        }
    
    def export_shadow_log(self, filepath: str):
        """Export shadow trade log to JSON."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(self.shadow_log, f, indent=2)
        logger.info(f"Shadow log exported to {filepath}")
    
    def disable(self):
        """Disable middleware (fail-safe mode)."""
        self.enabled = False
        logger.warning("⚠️ MIDDLEWARE DISABLED - Using base risk settings")
    
    def enable(self):
        """Enable middleware."""
        self.enabled = True
        logger.info("✅ MIDDLEWARE ENABLED")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_middleware: Optional[RiskManagerMiddleware] = None


def get_risk_middleware() -> RiskManagerMiddleware:
    """Get global RiskManagerMiddleware instance."""
    global _middleware
    if _middleware is None:
        _middleware = RiskManagerMiddleware()
    return _middleware


def process_signal_through_middleware(
    direction: str,
    base_size: float,
    entry_price: float,
    stop_loss: float,
    **kwargs
) -> AugmentedOrder:
    """
    Convenience function to process signal through middleware.
    """
    middleware = get_risk_middleware()
    return middleware.process_signal(
        direction=direction,
        base_size=base_size,
        entry_price=entry_price,
        stop_loss=stop_loss,
        **kwargs
    )
