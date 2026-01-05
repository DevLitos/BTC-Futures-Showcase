"""
Complete test suite for Edge Regime System modules
Tests all 7 critical components with correct API signatures
"""

import pytest
from datetime import datetime, timedelta, UTC
from typing import Dict, List

# Import all modules
from engine.edge_regime_classifier import EdgeRegimeClassifier, MarketRegime
from engine.degradation_attribution import DegradationAttributionLayer, DegradationCause
from engine.edge_recovery_protocol import EdgeRecoveryProtocol, RecoveryPhase
from engine.information_diet import InformationDiet, InformationTier
from engine.fail_silent_detector import FailSilentDetector
from engine.adaptive_window_manager import AdaptiveWindowManager, WindowType
from engine.strategy_fingerprint import StrategyFingerprint


class TestEdgeRegimeClassifier:
    """Test Edge Regime Classifier"""
    
    def test_initialization(self):
        """Test classifier initialization"""
        classifier = EdgeRegimeClassifier()
        assert classifier is not None
        assert hasattr(classifier, 'current_regime')
        assert hasattr(classifier, 'regime_confidence')
    
    def test_regime_a_classification(self):
        """Test Regime A classification (high expansion, high participation)"""
        classifier = EdgeRegimeClassifier()
        
        regime, confidence = classifier.classify_regime(
            expansion_count=5,
            expansion_sizes=[2.5, 3.0, 2.8, 3.2, 2.9],
            breakout_success_rate=0.75,
            atr_trend_5d=0.8,
            atr_trend_20d=0.6,
            session_range=0.045,
            session_realized_move=0.038,
            volume_profile_quality=0.85,
            price_action_consistency=0.80,
            order_flow_imbalance_std=0.15
        )
        
        assert regime == MarketRegime.REGIME_A
        assert confidence > 0.6
    
    def test_regime_d_classification(self):
        """Test Regime D classification (low participation)"""
        classifier = EdgeRegimeClassifier()
        
        regime, confidence = classifier.classify_regime(
            expansion_count=1,
            expansion_sizes=[0.8, 0.6, 0.5],
            breakout_success_rate=0.30,
            atr_trend_5d=-0.3,
            atr_trend_20d=-0.4,
            session_range=0.012,
            session_realized_move=0.008,
            volume_profile_quality=0.35,
            price_action_consistency=0.40,
            order_flow_imbalance_std=0.35
        )
        
        assert regime == MarketRegime.REGIME_D
    
    def test_edge_validity_in_regimes(self):
        """Test edge validity check in different regimes"""
        classifier = EdgeRegimeClassifier()
        
        # Edge should be valid in Regime A
        assert classifier.is_edge_valid_in_regime(MarketRegime.REGIME_A)
        
        # Edge may not be valid in Regime D
        valid_d = classifier.is_edge_valid_in_regime(MarketRegime.REGIME_D)
        assert isinstance(valid_d, bool)
    
    def test_get_regime_adjusted_size(self):
        """Test size adjustment based on regime"""
        classifier = EdgeRegimeClassifier()
        
        # Regime A should have multiplier close to 1.0
        multiplier_a = classifier.get_regime_adjusted_size_multiplier(MarketRegime.REGIME_A)
        assert 0.8 <= multiplier_a <= 1.2
        
        # Regime D should have reduced multiplier
        multiplier_d = classifier.get_regime_adjusted_size_multiplier(MarketRegime.REGIME_D)
        assert multiplier_d < multiplier_a


class TestDegradationAttributionLayer:
    """Test Degradation Attribution Layer"""
    
    def test_initialization(self):
        """Test attribution layer initialization"""
        layer = DegradationAttributionLayer()
        assert layer is not None
        assert hasattr(layer, 'attribution_history')
    
    def test_attribute_degradation_execution_issues(self):
        """Test attribution to execution quality"""
        layer = DegradationAttributionLayer()
        
        degradation_signals = {
            'mfe_mae_ratio': 1.2,
            'be_stop_rate': 0.25,
            'extension_rate': 0.45,
            'avg_duration_hours': 4.5
        }
        
        execution_metrics = {
            'slippage': 0.0018,  # High slippage
            'fill_rate': 0.75,   # Low fill rate
            'execution_time': 2.5
        }
        
        market_metrics = {
            'atr': 0.025,
            'volume': 1500,
            'spread': 0.0002,
            'participation_rate': 0.85
        }
        
        baseline = {
            'execution': {'slippage': 0.0005, 'fill_rate': 0.95},
            'market': {'atr': 0.025, 'volume': 1500},
            'volatility': {'atr_avg': 0.025}
        }
        
        attribution = layer.attribute_degradation(
            degradation_signals, execution_metrics, market_metrics, baseline
        )
        
        assert attribution is not None
        assert attribution.primary_cause == DegradationCause.EXECUTION_QUALITY
    
    def test_get_last_attribution(self):
        """Test getting last attribution analysis"""
        layer = DegradationAttributionLayer()
        
        # Should return None if no attributions yet
        assert layer.get_last_attribution() is None


class TestEdgeRecoveryProtocol:
    """Test Edge Recovery Protocol"""
    
    def test_initialization(self):
        """Test recovery protocol initialization"""
        baseline = {
            'min_win_rate': 0.55,
            'min_mfe_mae': 1.5,
            'min_extension_rate': 0.50
        }
        protocol = EdgeRecoveryProtocol(baseline_metrics=baseline)
        assert protocol is not None
        assert protocol.current_phase == RecoveryPhase.INITIAL
    
    def test_start_recovery(self):
        """Test starting recovery process"""
        baseline = {
            'min_win_rate': 0.55,
            'min_mfe_mae': 1.5,
            'min_extension_rate': 0.50
        }
        protocol = EdgeRecoveryProtocol(baseline_metrics=baseline)
        
        success = protocol.start_recovery()
        assert success is True
        assert protocol.current_phase != RecoveryPhase.INITIAL
    
    def test_add_recovery_trade(self):
        """Test adding trade to recovery tracking"""
        baseline = {
            'min_win_rate': 0.55,
            'min_mfe_mae': 1.5,
            'min_extension_rate': 0.50
        }
        protocol = EdgeRecoveryProtocol(baseline_metrics=baseline)
        protocol.start_recovery()
        
        trade = {
            'outcome': 'win',
            'mfe': 0.03,
            'mae': 0.015,
            'extension_count': 2,
            'clean_extension': True
        }
        
        protocol.add_recovery_trade(trade)
        assert len(protocol.recovery_trades) == 1
    
    def test_is_recovery_confirmed(self):
        """Test recovery confirmation check"""
        baseline = {
            'min_win_rate': 0.55,
            'min_mfe_mae': 1.5,
            'min_extension_rate': 0.50
        }
        protocol = EdgeRecoveryProtocol(baseline_metrics=baseline)
        protocol.start_recovery()
        
        # Not confirmed with no trades
        assert protocol.is_recovery_confirmed() is False


class TestInformationDiet:
    """Test Information Diet"""
    
    def test_initialization(self):
        """Test information diet initialization"""
        diet = InformationDiet()
        assert diet is not None
        assert diet.current_tier == InformationTier.OPERATOR
    
    def test_get_operator_view(self):
        """Test getting operator view"""
        diet = InformationDiet()
        
        view = diet.get_operator_view(
            can_trade=True,
            blocked_reasons=[],
            size_multiplier=1.0,
            trades_today=5,
            trades_week=18,
            system_state='ACTIVE'
        )
        
        assert view is not None
        assert view.can_trade is True
        assert view.position_size_multiplier == 1.0
        assert view.trades_today == 5
    
    def test_blocked_reason_simplification(self):
        """Test simplification of blocked reasons"""
        diet = InformationDiet()
        
        view = diet.get_operator_view(
            can_trade=False,
            blocked_reasons=['DEGRADATION_DETECTED', 'LOW_MFE_MAE', 'HIGH_BE_STOPS'],
            size_multiplier=0.0,
            trades_today=0,
            trades_week=0,
            system_state='DEGRADED'
        )
        
        assert view.can_trade is False
        assert view.reason_if_blocked is not None


class TestFailSilentDetector:
    """Test Fail-Silent Detector"""
    
    def test_initialization(self):
        """Test fail-silent detector initialization"""
        detector = FailSilentDetector()
        assert detector is not None
        assert hasattr(detector, 'opportunities')
    
    def test_record_market_move(self):
        """Test recording market move"""
        detector = FailSilentDetector()
        
        detector.record_market_move(
            move_size_r=2.5,
            duration_hours=4.0,
            was_captured=True,
            capture_percentage=65.0,
            reason_if_missed=None
        )
        
        assert len(detector.opportunities) == 1
    
    def test_get_current_status(self):
        """Test getting current capture status"""
        detector = FailSilentDetector()
        
        # Record some opportunities
        detector.record_market_move(2.5, 4.0, True, 70.0, None)
        detector.record_market_move(2.0, 3.0, False, 0.0, "No setup")
        detector.record_market_move(3.0, 5.0, True, 80.0, None)
        
        status = detector.get_current_status()
        assert status is not None
        assert status.total_opportunities == 3
        assert status.opportunities_captured == 2
    
    def test_silent_degradation_detection(self):
        """Test detection of silent degradation"""
        detector = FailSilentDetector(
            min_capture_rate=0.60,
            evaluation_window=10
        )
        
        # Record mostly missed opportunities
        for i in range(8):
            detector.record_market_move(2.0, 3.0, False, 0.0, "No trade")
        
        for i in range(2):
            detector.record_market_move(2.0, 3.0, True, 50.0, None)
        
        status = detector.get_current_status()
        assert status.silent_degradation_score > 0.5


class TestAdaptiveWindowManager:
    """Test Adaptive Window Manager"""
    
    def test_initialization(self):
        """Test adaptive window manager initialization"""
        manager = AdaptiveWindowManager()
        assert manager is not None
    
    def test_start_window(self):
        """Test starting new window"""
        manager = AdaptiveWindowManager()
        
        window = manager.start_window(
            window_type=WindowType.DEGRADATION,
            min_trades=20,
            min_days=7
        )
        
        assert window is not None
    
    def test_update_window(self):
        """Test updating window with new trade"""
        manager = AdaptiveWindowManager()
        
        window = manager.start_window(
            window_type=WindowType.DEGRADATION,
            min_trades=20,
            min_days=7
        )
        
        trade = {
            'timestamp': datetime.now(UTC),
            'outcome': 'win',
            'size_r': 2.0
        }
        
        manager.update_window(window.window_id, trade)
        # Should not raise error
    
    def test_can_evaluate(self):
        """Test window evaluation readiness"""
        manager = AdaptiveWindowManager()
        
        window = manager.start_window(
            window_type=WindowType.DEGRADATION,
            min_trades=5,
            min_days=1
        )
        
        # New window should not be ready
        can_eval, reason = manager.can_evaluate(window.window_id)
        assert can_eval is False


class TestStrategyFingerprint:
    """Test Strategy Fingerprint"""
    
    def test_initialization(self):
        """Test fingerprint initialization"""
        fingerprint = StrategyFingerprint()
        assert fingerprint is not None
    
    def test_register_component(self):
        """Test registering strategy component"""
        fingerprint = StrategyFingerprint()
        
        component = {
            'entry_mode': 'market_on_momentum',
            'sl_logic': 'adaptive_atr',
            'max_positions': 1
        }
        
        hash_value = fingerprint.register_component('core_strategy', component)
        assert hash_value is not None
        assert len(hash_value) == 64  # SHA-256 hex length
    
    def test_has_strategy_changed(self):
        """Test strategy change detection"""
        fingerprint = StrategyFingerprint()
        
        component_v1 = {
            'entry_mode': 'market_on_momentum',
            'sl_logic': 'adaptive_atr'
        }
        
        fingerprint.register_component('core', component_v1)
        
        # Same component should not trigger change
        changed = fingerprint.has_strategy_changed('core', component_v1)
        assert changed is False
        
        # Modified component should trigger change
        component_v2 = {
            'entry_mode': 'limit_on_pullback',  # Changed!
            'sl_logic': 'adaptive_atr'
        }
        
        changed = fingerprint.has_strategy_changed('core', component_v2)
        assert changed is True
    
    def test_requires_statistics_reset(self):
        """Test statistics reset requirement"""
        fingerprint = StrategyFingerprint()
        
        component = {'param': 'value'}
        fingerprint.register_component('test', component)
        
        # Should not require reset if no change
        assert fingerprint.requires_statistics_reset('test', component) is False
        
        # Should require reset if changed
        changed_component = {'param': 'new_value'}
        assert fingerprint.requires_statistics_reset('test', changed_component) is True


class TestIntegration:
    """Integration tests for combined module usage"""
    
    def test_classifier_with_fingerprint(self):
        """Test using classifier with fingerprint"""
        classifier = EdgeRegimeClassifier()
        fingerprint = StrategyFingerprint()
        
        strategy = {'entry': 'market', 'sl': 'atr'}
        fingerprint.register_component('strategy', strategy)
        
        regime, confidence = classifier.classify_regime(
            expansion_count=3,
            expansion_sizes=[2.0, 2.2, 1.9],
            breakout_success_rate=0.65,
            atr_trend_5d=0.5,
            atr_trend_20d=0.3,
            session_range=0.030,
            session_realized_move=0.024,
            volume_profile_quality=0.70,
            price_action_consistency=0.65,
            order_flow_imbalance_std=0.20
        )
        
        assert regime is not None
        assert fingerprint.get_current_fingerprint('strategy') is not None
    
    def test_attribution_with_recovery(self):
        """Test attribution layer with recovery protocol"""
        attribution_layer = DegradationAttributionLayer()
        
        baseline = {
            'min_win_rate': 0.55,
            'min_mfe_mae': 1.5,
            'min_extension_rate': 0.50
        }
        recovery = EdgeRecoveryProtocol(baseline_metrics=baseline)
        
        # Attribute degradation
        degradation = {
            'mfe_mae_ratio': 1.1,
            'be_stop_rate': 0.30,
            'extension_rate': 0.40,
            'avg_duration_hours': 3.5
        }
        
        execution = {'slippage': 0.0008, 'fill_rate': 0.92, 'execution_time': 1.5}
        market = {'atr': 0.025, 'volume': 1500, 'spread': 0.0002, 'participation_rate': 0.80}
        baseline_metrics = {
            'execution': {'slippage': 0.0005, 'fill_rate': 0.95},
            'market': {'atr': 0.025, 'volume': 1500},
            'volatility': {'atr_avg': 0.025}
        }
        
        attribution = attribution_layer.attribute_degradation(
            degradation, execution, market, baseline_metrics
        )
        
        # Start recovery if attributed
        if attribution:
            recovery.start_recovery()
            assert recovery.current_phase != RecoveryPhase.INITIAL


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
