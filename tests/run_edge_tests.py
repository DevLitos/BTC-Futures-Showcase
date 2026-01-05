#!/usr/bin/env python3
"""
Test runner simplificado para módulos de Edge Regime
No requiere pytest - usa unittest estándar de Python
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from datetime import datetime, timedelta, UTC

# Import modules
from engine.edge_regime_classifier import EdgeRegimeClassifier, MarketRegime
from engine.degradation_attribution import DegradationAttributionLayer, DegradationCause
from engine.edge_recovery_protocol import EdgeRecoveryProtocol, RecoveryPhase
from engine.information_diet import InformationDiet, InformationTier, OperatorView
from engine.fail_silent_detector import FailSilentDetector
from engine.adaptive_window_manager import AdaptiveWindowManager, WindowType
from engine.strategy_fingerprint import StrategyIdentityLock


class TestEdgeRegimeClassifier(unittest.TestCase):
    """Tests for EdgeRegimeClassifier"""
    
    def test_initialization(self):
        """Test classifier initialization"""
        classifier = EdgeRegimeClassifier()
        self.assertIsNotNone(classifier)
        self.assertEqual(classifier.current_regime, MarketRegime.UNKNOWN)
    
    def test_regime_classification(self):
        """Test basic regime classification"""
        classifier = EdgeRegimeClassifier()
        
        # REGIME_A: High expansion
        regime, confidence = classifier.classify_regime(
            expansion_count=3,
            expansion_sizes=[1.8, 2.1, 2.5],
            breakout_success_rate=0.75,
            atr_trend_5d=0.15,
            atr_trend_20d=0.10,
            session_range=800,
            session_realized_move=550,
            volume_profile_quality=0.70,
            price_action_consistency=0.75,
            order_flow_imbalance_std=0.20
        )
        
        self.assertEqual(regime, MarketRegime.REGIME_A)
        self.assertGreater(confidence, 0.4)
    
    def test_edge_validity(self):
        """Test edge validity check"""
        classifier = EdgeRegimeClassifier()
        
        # Classify to REGIME_A
        classifier.classify_regime(
            expansion_count=2,
            expansion_sizes=[1.8, 2.0],
            breakout_success_rate=0.70,
            atr_trend_5d=0.10,
            atr_trend_20d=0.05,
            session_range=700,
            session_realized_move=500,
            volume_profile_quality=0.65,
            price_action_consistency=0.70,
            order_flow_imbalance_std=0.25
        )
        
        is_valid, reason = classifier.is_edge_valid_in_regime()
        self.assertIsInstance(is_valid, bool)
        self.assertIsInstance(reason, str)
    
    def test_size_multiplier(self):
        """Test position size multiplier"""
        classifier = EdgeRegimeClassifier()
        
        classifier.classify_regime(
            expansion_count=2,
            expansion_sizes=[2.0],
            breakout_success_rate=0.70,
            atr_trend_5d=0.10,
            atr_trend_20d=0.05,
            session_range=700,
            session_realized_move=500,
            volume_profile_quality=0.65,
            price_action_consistency=0.70,
            order_flow_imbalance_std=0.25
        )
        
        multiplier = classifier.get_regime_adjusted_size_multiplier()
        self.assertGreaterEqual(multiplier, 0.0)
        self.assertLessEqual(multiplier, 1.0)


class TestDegradationAttribution(unittest.TestCase):
    """Tests for DegradationAttributionLayer"""
    
    def test_initialization(self):
        """Test attribution layer initialization"""
        layer = DegradationAttributionLayer()
        self.assertIsNotNone(layer)
    
    def test_attribution_analysis(self):
        """Test degradation attribution"""
        layer = DegradationAttributionLayer()
        
        analysis = layer.attribute_degradation(
            degradation_signals={
                'mfe_mae_ratio': 1.2,
                'be_stop_percentage': 0.40,
                'sample_size': 20
            },
            execution_metrics={
                'avg_slippage_pct': 0.08,
                'fill_rate': 0.92
            },
            market_metrics={
                'current_atr': 400,
                'avg_spread_bps': 2.0
            },
            historical_baseline={
                'execution': {'avg_slippage_pct': 0.05},
                'market': {'avg_volume': 1000000},
                'volatility': {'avg_atr': 500}
            }
        )
        
        self.assertIn('primary_cause', analysis.__dict__)
        self.assertIn('confidence', analysis.__dict__)


class TestEdgeRecoveryProtocol(unittest.TestCase):
    """Tests for EdgeRecoveryProtocol"""
    
    def test_initialization(self):
        """Test recovery protocol initialization"""
        baseline = {'win_rate': 0.50, 'expectancy': 0.5}
        protocol = EdgeRecoveryProtocol(baseline)
        self.assertIsNotNone(protocol)
        self.assertEqual(protocol.current_phase, RecoveryPhase.NONE)
    
    def test_start_recovery(self):
        """Test starting recovery"""
        baseline = {'win_rate': 0.50, 'expectancy': 0.5}
        protocol = EdgeRecoveryProtocol(baseline)
        
        success = protocol.start_recovery()
        self.assertTrue(success)
        self.assertEqual(protocol.current_phase, RecoveryPhase.INITIAL)
    
    def test_add_recovery_trade(self):
        """Test adding recovery trades"""
        baseline = {'win_rate': 0.50, 'expectancy': 0.5}
        protocol = EdgeRecoveryProtocol(baseline)
        protocol.start_recovery()
        
        progress = protocol.add_recovery_trade(
            entry_price=50000,
            exit_price=51000,
            mfe=1500,
            mae=300,
            duration_hours=4.0,
            reached_tp1=True,
            reached_tp2=True,
            is_be_stop=False
        )
        
        self.assertEqual(len(protocol.recovery_trades), 1)
        self.assertIsNotNone(progress)


class TestInformationDiet(unittest.TestCase):
    """Tests for InformationDiet"""
    
    def test_initialization(self):
        """Test information diet initialization"""
        diet = InformationDiet()
        self.assertIsNotNone(diet)
        self.assertEqual(diet.current_tier, InformationTier.OPERATOR)
    
    def test_operator_view(self):
        """Test operator view filtering"""
        diet = InformationDiet()
        
        view = diet.get_operator_view(
            can_trade=True,
            blocked_reasons=[],
            size_multiplier=1.0,
            trades_today=3,
            trades_week=12,
            system_state="ACTIVE"
        )
        
        self.assertIsInstance(view, OperatorView)
        self.assertEqual(view.can_trade, True)
        self.assertEqual(view.position_size_multiplier, 1.0)


class TestFailSilentDetector(unittest.TestCase):
    """Tests for FailSilentDetector"""
    
    def test_initialization(self):
        """Test detector initialization"""
        detector = FailSilentDetector()
        self.assertIsNotNone(detector)
    
    def test_record_market_move(self):
        """Test recording market move"""
        detector = FailSilentDetector()
        
        detector.record_market_move(
            timestamp=datetime.now(UTC),
            move_size_r=2.0,
            duration_hours=3.0,
            strategy_participated=True,
            capture_percentage=75.0,
            miss_reason=None
        )
        
        self.assertEqual(len(detector.opportunities), 1)
    
    def test_get_current_status(self):
        """Test getting current status"""
        detector = FailSilentDetector()
        
        # Add some opportunities
        for i in range(3):
            detector.record_market_move(
                timestamp=datetime.now(UTC),
                move_size_r=2.0,
                duration_hours=3.0,
                strategy_participated=True,
                capture_percentage=50.0,
                miss_reason=None
            )
        
        is_degraded, alert = detector.get_current_status()
        self.assertIsInstance(is_degraded, bool)


class TestAdaptiveWindowManager(unittest.TestCase):
    """Tests for AdaptiveWindowManager"""
    
    def test_initialization(self):
        """Test window manager initialization"""
        manager = AdaptiveWindowManager()
        self.assertIsNotNone(manager)
    
    def test_start_window(self):
        """Test starting a window"""
        manager = AdaptiveWindowManager()
        
        window = manager.start_window(WindowType.DEGRADATION)
        self.assertIsNotNone(window)
        self.assertEqual(window.window_type, WindowType.DEGRADATION)
    
    def test_update_window(self):
        """Test updating a window"""
        manager = AdaptiveWindowManager()
        manager.start_window(WindowType.DEGRADATION)
        
        window = manager.update_window(
            window_type=WindowType.DEGRADATION,
            trade_added=True,
            atr_value=500,
            current_regime="REGIME_A"
        )
        
        self.assertEqual(window.trades_count, 1)


class TestStrategyFingerprint(unittest.TestCase):
    """Tests for StrategyIdentityLock"""
    
    def test_initialization(self):
        """Test fingerprint initialization"""
        lock = StrategyIdentityLock()
        self.assertIsNotNone(lock)
    
    def test_register_component(self):
        """Test component registration"""
        lock = StrategyIdentityLock()
        
        component_hash = lock.register_component(
            'entry_logic',
            {'type': 'expansion_momentum', 'threshold': 1.0}
        )
        
        self.assertIsInstance(component_hash, str)
        self.assertEqual(len(component_hash), 16)
    
    def test_get_current_fingerprint(self):
        """Test getting current fingerprint"""
        lock = StrategyIdentityLock()
        
        lock.register_component('entry_logic', {'type': 'expansion'})
        fingerprint = lock.get_current_fingerprint()
        
        self.assertIsInstance(fingerprint, str)


class TestIntegration(unittest.TestCase):
    """Integration tests"""
    
    def test_basic_integration(self):
        """Test basic module integration"""
        # Initialize modules
        classifier = EdgeRegimeClassifier()
        detector = FailSilentDetector()
        
        # Classify regime
        regime, conf = classifier.classify_regime(
            expansion_count=2,
            expansion_sizes=[2.0],
            breakout_success_rate=0.70,
            atr_trend_5d=0.10,
            atr_trend_20d=0.05,
            session_range=700,
            session_realized_move=500,
            volume_profile_quality=0.65,
            price_action_consistency=0.70,
            order_flow_imbalance_std=0.25
        )
        
        # Record opportunity
        detector.record_market_move(
            timestamp=datetime.now(UTC),
            move_size_r=2.5,
            duration_hours=4.0,
            strategy_participated=True,
            capture_percentage=70.0
        )
        
        # Both should work together
        self.assertIsNotNone(regime)
        self.assertEqual(len(detector.opportunities), 1)


def run_tests():
    """Run all tests"""
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestEdgeRegimeClassifier))
    suite.addTests(loader.loadTestsFromTestCase(TestDegradationAttribution))
    suite.addTests(loader.loadTestsFromTestCase(TestEdgeRecoveryProtocol))
    suite.addTests(loader.loadTestsFromTestCase(TestInformationDiet))
    suite.addTests(loader.loadTestsFromTestCase(TestFailSilentDetector))
    suite.addTests(loader.loadTestsFromTestCase(TestAdaptiveWindowManager))
    suite.addTests(loader.loadTestsFromTestCase(TestStrategyFingerprint))
    suite.addTests(loader.loadTestsFromTestCase(TestIntegration))
    
    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Print summary
    print("\n" + "="*70)
    print(f"Tests run: {result.testsRun}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Success: {result.wasSuccessful()}")
    print("="*70)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
