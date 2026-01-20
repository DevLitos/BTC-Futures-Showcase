"""
Enhanced Edge Enforcement - Integration of all advanced edge protection layers

This module integrates all critical edge protection modules:
1. EdgeRegimeClassifier - Market regime detection
2. DegradationAttributionLayer - Root cause analysis
3. EdgeRecoveryProtocol - Formal recovery validation
4. InformationDiet - Controlled information exposure
5. FailSilentDetector - Silent edge erosion detection
6. AdaptiveWindowManager - Dynamic evaluation windows
7. StrategyFingerprint - Strategy identity lock

ARCHITECTURE:
- All modules work together to protect edge
- Clear separation of concerns
- Each module has specific responsibility
- Integration layer coordinates decisions
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime, UTC
from dataclasses import dataclass

from .edge_regime_classifier import EdgeRegimeClassifier, MarketRegime
from .degradation_attribution import DegradationAttributionLayer, DegradationCause
from .edge_recovery_protocol import EdgeRecoveryProtocol, RecoveryPhase
from .information_diet import InformationDiet, InformationTier, OperatorView
from .fail_silent_detector import FailSilentDetector
from .adaptive_window_manager import AdaptiveWindowManager, WindowType
from .strategy_fingerprint import StrategyIdentityLock


@dataclass
class EnhancedEdgeState:
    """Complete edge state including all protection layers"""
    # Core state
    can_trade: bool
    position_size_multiplier: float
    system_state: str
    
    # Regime
    market_regime: MarketRegime
    regime_confidence: float
    edge_valid_in_regime: bool
    
    # Degradation
    degradation_detected: bool
    degradation_cause: Optional[DegradationCause]
    attribution_confidence: float
    
    # Recovery
    recovery_phase: RecoveryPhase
    recovery_progress: float
    
    # Silent failure
    silent_degradation: bool
    opportunity_capture_rate: float
    
    # Identity
    strategy_fingerprint: str
    requires_stats_reset: bool
    
    # Blocking reasons
    blockers: List[str]
    
    # Information for trader
    operator_view: OperatorView


class EnhancedEdgeEnforcement:
    """
    Enhanced edge enforcement with all protection layers.
    
    PHILOSOPHY:
    - Multiple layers of protection
    - Each layer catches different failure modes
    - Integration provides complete edge protection
    - Trader sees only what they need (information diet)
    """
    
    def __init__(self, baseline_metrics: Dict, strategy_config: Dict):
        """
        Initialize enhanced edge enforcement.
        
        Args:
            baseline_metrics: Historical baseline for comparison
            strategy_config: Strategy configuration for fingerprinting
        """
        # Initialize all modules
        self.regime_classifier = EdgeRegimeClassifier()
        self.attribution_layer = DegradationAttributionLayer()
        self.recovery_protocol = EdgeRecoveryProtocol(baseline_metrics)
        self.information_diet = InformationDiet(default_tier=InformationTier.OPERATOR)
        self.silent_detector = FailSilentDetector()
        self.window_manager = AdaptiveWindowManager()
        self.identity_lock = StrategyIdentityLock()
        
        # Register strategy identity
        self.identity_lock.register_full_strategy(strategy_config)
        
        # State tracking
        self.baseline_metrics = baseline_metrics
        self.last_update = datetime.now(UTC)
        
    def evaluate_trading_permission(self,
                                   market_data: Dict,
                                   degradation_signals: Dict,
                                   execution_metrics: Dict,
                                   context_valid: bool) -> EnhancedEdgeState:
        """
        Comprehensive evaluation of trading permission.
        
        This is the main decision function integrating all protection layers.
        
        Args:
            market_data: Current market metrics
            degradation_signals: Degradation detection metrics
            execution_metrics: Execution quality metrics
            context_valid: Whether edge context is valid
            
        Returns:
            EnhancedEdgeState with complete decision
        """
        blockers = []
        can_trade = True
        size_multiplier = 1.0
        
        # 1. REGIME CHECK
        regime, regime_conf = self._check_regime(market_data)
        edge_valid_regime, regime_reason = self.regime_classifier.is_edge_valid_in_regime()
        
        if not edge_valid_regime:
            blockers.append(f"Regime: {regime_reason}")
            can_trade = False
            size_multiplier = 0.0
        else:
            # Adjust size based on regime
            regime_size = self.regime_classifier.get_regime_adjusted_size_multiplier()
            size_multiplier = min(size_multiplier, regime_size)
        
        # 2. CONTEXT CHECK
        if not context_valid:
            blockers.append("Context: Edge assumptions not met")
            can_trade = False
            size_multiplier = 0.0
        
        # 3. DEGRADATION CHECK + ATTRIBUTION
        degradation_detected = degradation_signals.get('active_signals', 0) >= 2
        attribution = None
        attribution_conf = 0.0
        primary_cause = None
        
        if degradation_detected:
            # Attribute degradation to root cause
            attribution = self.attribution_layer.attribute_degradation(
                degradation_signals,
                execution_metrics,
                market_data,
                self.baseline_metrics
            )
            primary_cause = attribution.primary_cause
            attribution_conf = attribution.confidence
            
            # Decision based on attribution
            if primary_cause == DegradationCause.STRATEGY_OBSOLESCENCE:
                if attribution.primary_score > 0.70:
                    blockers.append("Degradation: Strategy likely obsolete")
                    can_trade = False
                    size_multiplier = 0.0
                else:
                    blockers.append("Degradation: Potential obsolescence - reduced size")
                    size_multiplier = min(size_multiplier, 0.3)
            
            elif primary_cause == DegradationCause.VOLATILITY_REGIME:
                blockers.append("Degradation: Volatility regime mismatch")
                # Wait for regime alignment
                can_trade = False
                size_multiplier = 0.0
            
            elif primary_cause == DegradationCause.RANDOM_VARIANCE:
                # Just variance - continue but monitor
                blockers.append("Note: Performance variance within normal range")
                # Don't reduce size for random variance
            
            else:
                # Other causes - reduce size but don't stop
                blockers.append(f"Degradation: {attribution.recommended_action}")
                size_multiplier = min(size_multiplier, 0.5)
        
        # 4. RECOVERY CHECK
        recovery_phase = self.recovery_protocol.current_phase
        recovery_progress = 0.0
        
        if recovery_phase == RecoveryPhase.CONFIRMED:
            # Recovery confirmed - can reactivate
            blockers.append("Recovery: Edge recovery confirmed")
            # Size determined by regime/context
        
        elif recovery_phase in [RecoveryPhase.INITIAL, RecoveryPhase.VALIDATION]:
            # In recovery - allow trading but monitor
            recovery_progress = len(self.recovery_protocol.recovery_trades) / \
                              self.recovery_protocol.requirements.min_trades
            blockers.append(f"Recovery: In progress ({recovery_progress:.0%})")
            size_multiplier = min(size_multiplier, 0.5)
        
        elif recovery_phase == RecoveryPhase.FAILED:
            # Recovery failed
            can_retry, retry_reason = self.recovery_protocol.can_retry_recovery()
            if not can_retry:
                blockers.append(f"Recovery failed: {retry_reason}")
                can_trade = False
                size_multiplier = 0.0
        
        # 5. SILENT DEGRADATION CHECK
        silent_degraded, silent_alert = self.silent_detector.get_current_status()
        capture_rate = 0.0
        
        if silent_degraded:
            if silent_alert:
                blockers.append(f"Silent degradation: {silent_alert.severity}")
                capture_rate = silent_alert.capture_rate
                
                if silent_alert.severity in ['critical', 'severe']:
                    can_trade = False
                    size_multiplier = 0.0
                else:
                    size_multiplier = min(size_multiplier, 0.5)
        
        # 6. FINGERPRINT CHECK
        requires_reset, reset_reason = self.identity_lock.requires_statistics_reset()
        current_fingerprint = self.identity_lock.get_current_fingerprint()
        
        if requires_reset:
            blockers.append(f"Strategy changed: {reset_reason}")
            # This should trigger external stats reset
        
        # 7. WINDOW VALIDITY CHECK
        # Check if evaluation windows are valid before making decisions
        can_eval_degradation, degradation_window_reason = self.window_manager.can_evaluate(
            WindowType.DEGRADATION
        )
        
        if degradation_detected and not can_eval_degradation:
            # Degradation detected but window not valid yet
            blockers.append(f"Window: {degradation_window_reason}")
            # Don't act on invalid window
            size_multiplier = min(size_multiplier, 0.75)
        
        # FINAL DECISION
        system_state = self._determine_system_state(
            can_trade,
            size_multiplier,
            recovery_phase,
            degradation_detected
        )
        
        # Build operator view (filtered information)
        operator_view = self.information_diet.get_operator_view(
            can_trade=can_trade,
            blocked_reasons=blockers,
            size_multiplier=size_multiplier,
            trades_today=execution_metrics.get('trades_today', 0),
            trades_week=execution_metrics.get('trades_week', 0),
            system_state=system_state
        )
        
        # Build complete state
        state = EnhancedEdgeState(
            can_trade=can_trade,
            position_size_multiplier=size_multiplier,
            system_state=system_state,
            market_regime=regime,
            regime_confidence=regime_conf,
            edge_valid_in_regime=edge_valid_regime,
            degradation_detected=degradation_detected,
            degradation_cause=primary_cause,
            attribution_confidence=attribution_conf,
            recovery_phase=recovery_phase,
            recovery_progress=recovery_progress,
            silent_degradation=silent_degraded,
            opportunity_capture_rate=capture_rate,
            strategy_fingerprint=current_fingerprint or "unknown",
            requires_stats_reset=requires_reset,
            blockers=blockers,
            operator_view=operator_view
        )
        
        return state
    
    def _check_regime(self, market_data: Dict) -> Tuple[MarketRegime, float]:
        """Check and classify market regime"""
        
        regime, confidence = self.regime_classifier.classify_regime(
            expansion_count=market_data.get('expansion_count', 0),
            expansion_sizes=market_data.get('expansion_sizes', []),
            breakout_success_rate=market_data.get('breakout_success_rate', 0.5),
            atr_trend_5d=market_data.get('atr_trend_5d', 0.0),
            atr_trend_20d=market_data.get('atr_trend_20d', 0.0),
            session_range=market_data.get('session_range', 0.0),
            session_realized_move=market_data.get('session_realized_move', 0.0),
            volume_profile_quality=market_data.get('volume_quality', 0.5),
            price_action_consistency=market_data.get('price_consistency', 0.5),
            order_flow_imbalance_std=market_data.get('order_flow_std', 0.2)
        )
        
        return regime, confidence
    
    def _determine_system_state(self,
                                can_trade: bool,
                                size_multiplier: float,
                                recovery_phase: RecoveryPhase,
                                degradation_detected: bool) -> str:
        """Determine high-level system state"""
        
        if not can_trade:
            if recovery_phase in [RecoveryPhase.INITIAL, RecoveryPhase.VALIDATION]:
                return "OBSERVATION"
            elif recovery_phase == RecoveryPhase.FAILED:
                return "DEAD"
            else:
                return "PAUSED"
        
        if size_multiplier < 0.75:
            return "DEGRADED"
        
        if degradation_detected:
            return "MONITORING"
        
        return "ACTIVE"
    
    def record_trade_result(self,
                           trade_data: Dict,
                           market_move_data: Optional[Dict] = None) -> None:
        """
        Record trade result across all modules.
        
        Args:
            trade_data: Trade execution data
            market_move_data: Market move data for opportunity tracking
        """
        # Update recovery protocol if in recovery
        if self.recovery_protocol.current_phase != RecoveryPhase.NONE:
            self.recovery_protocol.add_recovery_trade(
                entry_price=trade_data['entry_price'],
                exit_price=trade_data['exit_price'],
                mfe=trade_data['mfe'],
                mae=trade_data['mae'],
                duration_hours=trade_data['duration_hours'],
                reached_tp1=trade_data['reached_tp1'],
                reached_tp2=trade_data['reached_tp2'],
                is_be_stop=trade_data['is_be_stop']
            )
        
        # Update silent detector if market move provided
        if market_move_data:
            self.silent_detector.record_market_move(
                timestamp=market_move_data['timestamp'],
                move_size_r=market_move_data['move_size_r'],
                duration_hours=market_move_data['duration_hours'],
                strategy_participated=True,
                capture_percentage=market_move_data.get('capture_pct', 0),
                miss_reason=None
            )
        
        # Update adaptive windows
        for window_type in self.window_manager.active_windows:
            self.window_manager.update_window(
                window_type=window_type,
                trade_added=True,
                atr_value=trade_data.get('atr_at_trade'),
                current_regime=self.regime_classifier.current_regime.value
            )
    
    def record_missed_opportunity(self, opportunity_data: Dict) -> None:
        """
        Record missed market opportunity for silent degradation tracking.
        
        Args:
            opportunity_data: Market move that was missed
        """
        self.silent_detector.record_market_move(
            timestamp=opportunity_data['timestamp'],
            move_size_r=opportunity_data['move_size_r'],
            duration_hours=opportunity_data['duration_hours'],
            strategy_participated=False,
            capture_percentage=0.0,
            miss_reason=opportunity_data.get('reason', 'unknown')
        )
    
    def get_diagnostic_report(self, override_key: str = None) -> Optional[Dict]:
        """
        Get full diagnostic report (requires authorization).
        
        Args:
            override_key: Authorization key for diagnostic access
            
        Returns:
            Full diagnostic data if authorized
        """
        full_metrics = {
            'regime': self.regime_classifier.get_regime_summary(),
            'attribution': self.attribution_layer.get_attribution_summary(),
            'recovery': self.recovery_protocol.get_recovery_summary(),
            'silent_detector': self.silent_detector.get_opportunity_summary(),
            'windows': self.window_manager.get_window_summary(),
            'identity': self.identity_lock.get_identity_summary()
        }
        
        return self.information_diet.get_diagnostic_view(full_metrics, override_key)
    
    def get_operator_status(self) -> Dict:
        """
        Get simple status for operator (information diet filtered).
        
        Returns:
            Minimal operational information
        """
        # This would be called in live trading
        # Returns only what trader needs to know
        return {
            'timestamp': datetime.now(UTC).isoformat(),
            'message': self.information_diet.format_trader_message(
                can_trade=True,  # Would come from last evaluation
                state="ACTIVE",
                size=1.0
            )
        }
    
    def check_intervention_required(self) -> Tuple[bool, List[str]]:
        """
        Check if any module requires immediate intervention.
        
        Returns:
            Tuple of (requires_intervention, reasons)
        """
        reasons = []
        
        # Check silent detector
        silent_intervention, silent_reason = self.silent_detector.requires_intervention()
        if silent_intervention:
            reasons.append(silent_reason)
        
        # Check recovery failures
        if self.recovery_protocol.is_recovery_failed():
            can_retry, retry_msg = self.recovery_protocol.can_retry_recovery()
            if not can_retry:
                reasons.append(f"Recovery exhausted: {retry_msg}")
        
        # Check regime validity
        regime_valid, regime_reason = self.regime_classifier.is_edge_valid_in_regime()
        if not regime_valid and self.regime_classifier.regime_confidence > 0.70:
            reasons.append(f"Regime invalid: {regime_reason}")
        
        # Check strategy fingerprint
        requires_reset, reset_reason = self.identity_lock.requires_statistics_reset()
        if requires_reset:
            reasons.append(f"Stats reset required: {reset_reason}")
        
        return len(reasons) > 0, reasons
