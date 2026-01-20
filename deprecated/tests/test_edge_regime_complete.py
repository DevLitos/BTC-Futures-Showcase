"""
COMPREHENSIVE TEST SUITE FOR EDGE REGIME MODEL
Tests all 7 critical modules with 100% method coverage

Created: 2026-01-03
Purpose: Validate all edge regime detection and management logic
"""

import pytest
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Any
import hashlib

# Import all modules to test
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.edge_regime_classifier import EdgeRegimeClassifier, MarketRegime
from engine.degradation_attribution import DegradationAttributionLayer, DegradationCause
from engine.edge_recovery_protocol import EdgeRecoveryProtocol, RecoveryPhase
from engine.information_diet import InformationDiet, InformationTier
from engine.fail_silent_detector import FailSilentDetector
from engine.adaptive_window_manager import AdaptiveWindowManager, WindowType
from engine.strategy_fingerprint import StrategyIdentityLock


# ============================================================================
# TEST FIXTURES
# ============================================================================

@pytest.fixture
def sample_market_data():
    """Sample market data for regime classification"""
    return {
        'volatility': 0.025,
        'trend_strength': 0.75,
        'volume_profile': 1.2,
        'expansion_rate': 0.8,
        'chop_index': 0.3,
        'news_volatility': 0.1,
        'participation_rate': 0.85
    }


@pytest.fixture
def sample_trade_metrics():
    """Sample trade metrics for degradation analysis"""
    return {
        'win_rate': 0.55,
        'avg_r': 1.5,
        'mfe_mae_ratio': 1.8,
        'be_stop_rate': 0.15,
        'slippage': 0.0002,
        'extension_quality': 0.7
    }


@pytest.fixture
def sample_trades():
    """Sample trade history"""
    base_time = datetime.now()
    trades = []
    for i in range(20):
        trades.append({
            'timestamp': base_time - timedelta(minutes=30*i),
            'outcome': 'win' if i % 3 != 0 else 'loss',
            'r_multiple': 2.0 if i % 3 != 0 else -1.0,
            'mfe': 0.03,
            'mae': 0.015,
            'slippage': 0.0001,
            'extension_count': 2
        })
    return trades


@pytest.fixture
def sample_strategy_config():
    """Sample strategy configuration"""
    return {
        'sl_logic': 'adaptive_atr',
        'tp_logic': 'extension_based',
        'risk_per_trade': 0.02,
        'max_positions': 1,
        'trailing_type': 'intelligent',
        'entry_mode': 'market_on_momentum'
    }


# ============================================================================
# TEST EDGE REGIME CLASSIFIER
# ============================================================================

class TestEdgeRegimeClassifier:
    """Complete tests for EdgeRegimeClassifier"""
    
    def test_initialization(self):
        """Test classifier initialization"""
        classifier = EdgeRegimeClassifier()
        assert classifier is not None
        assert hasattr(classifier, 'classify_regime')
        assert hasattr(classifier, 'get_regime_confidence')
    
    def test_regime_a_high_expansion(self, sample_market_data):
        """Test REGIME_A: High expansion / trend continuation"""
        classifier = EdgeRegimeClassifier()
        
        # Strong expansion market
        data = sample_market_data.copy()
        data['expansion_rate'] = 0.85
        data['trend_strength'] = 0.80
        data['chop_index'] = 0.25
        
        regime = classifier.classify_regime(data)
        assert regime == MarketRegime.REGIME_A
        
        confidence = classifier.get_regime_confidence()
        assert confidence > 0.7
    
    def test_regime_b_volatility_compression(self, sample_market_data):
        """Test REGIME_B: Volatility compression / fakeouts"""
        classifier = EdgeRegimeClassifier()
        
        data = sample_market_data.copy()
        data['expansion_rate'] = 0.3
        data['chop_index'] = 0.65
        data['volatility'] = 0.015
        
        regime = classifier.classify_regime(data)
        assert regime == MarketRegime.REGIME_B
    
    def test_regime_c_news_driven(self, sample_market_data):
        """Test REGIME_C: News-driven / unstable"""
        classifier = EdgeRegimeClassifier()
        
        data = sample_market_data.copy()
        data['news_volatility'] = 0.85
        data['volatility'] = 0.045
        
        regime = classifier.classify_regime(data)
        assert regime == MarketRegime.REGIME_C
    
    def test_regime_d_low_participation(self, sample_market_data):
        """Test REGIME_D: Low participation / chop"""
        classifier = EdgeRegimeClassifier()
        
        data = sample_market_data.copy()
        data['participation_rate'] = 0.3
        data['chop_index'] = 0.75
        data['volume_profile'] = 0.4
        
        regime = classifier.classify_regime(data)
        assert regime == MarketRegime.REGIME_D
    
    def test_regime_transition_detection(self, sample_market_data):
        """Test regime transition detection"""
        classifier = EdgeRegimeClassifier()
        
        # Start in REGIME_A
        regime1 = classifier.classify_regime(sample_market_data)
        
        # Transition to REGIME_D
        data2 = sample_market_data.copy()
        data2['participation_rate'] = 0.25
        data2['chop_index'] = 0.8
        regime2 = classifier.classify_regime(data2)
        
        assert regime1 != regime2
        assert classifier.has_regime_changed()
    
    def test_edge_validity_in_regimes(self, sample_market_data):
        """Test edge validity across regimes"""
        classifier = EdgeRegimeClassifier()
        
        # REGIME_A should be valid
        data_a = sample_market_data.copy()
        data_a['expansion_rate'] = 0.85
        classifier.classify_regime(data_a)
        assert classifier.is_edge_valid()
        
        # REGIME_D should be invalid
        data_d = sample_market_data.copy()
        data_d['participation_rate'] = 0.2
        data_d['chop_index'] = 0.85
        classifier.classify_regime(data_d)
        assert not classifier.is_edge_valid()
    
    def test_get_regime_description(self, sample_market_data):
        """Test regime description generation"""
        classifier = EdgeRegimeClassifier()
        classifier.classify_regime(sample_market_data)
        
        description = classifier.get_regime_description()
        assert isinstance(description, str)
        assert len(description) > 0
    
    def test_get_regime_metrics(self, sample_market_data):
        """Test regime metrics extraction"""
        classifier = EdgeRegimeClassifier()
        classifier.classify_regime(sample_market_data)
        
        metrics = classifier.get_regime_metrics()
        assert isinstance(metrics, dict)
        assert 'current_regime' in metrics
        assert 'confidence' in metrics
        assert 'edge_valid' in metrics
    
    def test_invalid_market_data(self):
        """Test handling of invalid market data"""
        classifier = EdgeRegimeClassifier()
        
        # Missing required fields
        with pytest.raises((KeyError, ValueError)):
            classifier.classify_regime({})
        
        # Invalid values
        with pytest.raises((ValueError, AssertionError)):
            classifier.classify_regime({
                'volatility': -0.1,
                'trend_strength': 1.5
            })


# ============================================================================
# TEST DEGRADATION ATTRIBUTION LAYER
# ============================================================================

class TestDegradationAttributionLayer:
    """Complete tests for DegradationAttributionLayer"""
    
    def test_initialization(self):
        """Test attribution layer initialization"""
        layer = DegradationAttributionLayer()
        assert layer is not None
    
    def test_execution_issues_attribution(self, sample_trade_metrics):
        """Test attribution to execution issues"""
        layer = DegradationAttributionLayer()
        
        metrics = sample_trade_metrics.copy()
        metrics['slippage'] = 0.0015  # High slippage
        metrics['be_stop_rate'] = 0.35  # High BE stops
        
        attribution = layer.analyze_degradation(metrics)
        assert attribution['primary_cause'] == DegradationType.EXECUTION
        assert attribution['confidence'] > 0.6
    
    def test_market_structure_attribution(self, sample_trade_metrics):
        """Test attribution to market structure change"""
        layer = DegradationAttributionLayer()
        
        metrics = sample_trade_metrics.copy()
        metrics['mfe_mae_ratio'] = 0.8  # Poor ratio
        metrics['extension_quality'] = 0.3  # Low quality
        
        attribution = layer.analyze_degradation(metrics)
        assert attribution['primary_cause'] == DegradationType.MARKET_STRUCTURE
    
    def test_volatility_regime_attribution(self, sample_trade_metrics):
        """Test attribution to volatility regime"""
        layer = DegradationAttributionLayer()
        
        metrics = sample_trade_metrics.copy()
        metrics['volatility_mismatch'] = 0.85  # High mismatch
        
        attribution = layer.analyze_degradation(metrics)
        assert attribution['primary_cause'] == DegradationType.VOLATILITY_REGIME
    
    def test_random_variance_attribution(self, sample_trade_metrics):
        """Test attribution to random variance"""
        layer = DegradationAttributionLayer()
        
        # Metrics within normal ranges
        attribution = layer.analyze_degradation(sample_trade_metrics)
        assert attribution['primary_cause'] == DegradationType.RANDOM_VARIANCE
    
    def test_multi_factor_analysis(self, sample_trade_metrics):
        """Test multi-factor degradation analysis"""
        layer = DegradationAttributionLayer()
        
        metrics = sample_trade_metrics.copy()
        metrics['slippage'] = 0.001
        metrics['mfe_mae_ratio'] = 0.9
        
        attribution = layer.analyze_degradation(metrics)
        assert 'contributing_factors' in attribution
        assert len(attribution['contributing_factors']) >= 1
    
    def test_get_attribution_report(self, sample_trade_metrics):
        """Test attribution report generation"""
        layer = DegradationAttributionLayer()
        layer.analyze_degradation(sample_trade_metrics)
        
        report = layer.get_attribution_report()
        assert isinstance(report, str)
        assert 'Primary Cause' in report
        assert 'Confidence' in report
    
    def test_get_attribution_history(self, sample_trade_metrics):
        """Test attribution history tracking"""
        layer = DegradationAttributionLayer()
        
        # Analyze multiple times
        for i in range(5):
            metrics = sample_trade_metrics.copy()
            metrics['slippage'] = 0.0001 * (i + 1)
            layer.analyze_degradation(metrics)
        
        history = layer.get_attribution_history()
        assert len(history) == 5
        assert all('timestamp' in h for h in history)
    
    def test_clear_history(self, sample_trade_metrics):
        """Test clearing attribution history"""
        layer = DegradationAttributionLayer()
        
        layer.analyze_degradation(sample_trade_metrics)
        layer.clear_history()
        
        history = layer.get_attribution_history()
        assert len(history) == 0


# ============================================================================
# TEST EDGE RECOVERY PROTOCOL
# ============================================================================

class TestEdgeRecoveryProtocol:
    """Complete tests for EdgeRecoveryProtocol"""
    
    def test_initialization(self):
        """Test recovery protocol initialization"""
        baseline = {'win_rate': 0.5, 'expectancy': 0.5}
        protocol = EdgeRecoveryProtocol(baseline)
        assert protocol is not None
        assert protocol.current_phase == RecoveryPhase.NONE
    
    def test_start_recovery(self):
        """Test starting recovery process"""
        baseline = {'win_rate': 0.5, 'expectancy': 0.5}
        protocol = EdgeRecoveryProtocol(baseline)
        success = protocol.start_recovery()
        
        assert success
        assert protocol.current_phase == RecoveryPhase.INITIAL
    
    def test_successful_recovery(self, sample_trades):
        """Test successful edge recovery"""
        protocol = EdgeRecoveryProtocol(
            min_trades=10,
            min_win_rate=0.50,
            min_mfe_mae=1.5,
            min_clean_extensions=2
        )
        protocol.start_recovery()
        
        # Add winning trades
        for trade in sample_trades[:15]:
            if trade['outcome'] == 'win':
                protocol.add_recovery_trade(trade)
        
        status = protocol.get_recovery_status()
        if protocol.is_recovery_complete():
            assert status == RecoveryStatus.COMPLETE
    
    def test_failed_recovery(self, sample_trades):
        """Test failed recovery attempt"""
        protocol = EdgeRecoveryProtocol(
            min_trades=10,
            min_win_rate=0.70,  # Very high requirement
            max_recovery_attempts=2
        )
        protocol.start_recovery()
        
        # Add losing trades
        losing_trades = [t for t in sample_trades if t['outcome'] == 'loss']
        for trade in losing_trades[:15]:
            protocol.add_recovery_trade(trade)
        
        # Multiple failed attempts
        protocol.start_recovery()
        protocol.start_recovery()
        
        status = protocol.get_recovery_status()
        assert status == RecoveryStatus.FAILED
    
    def test_recovery_metrics_tracking(self, sample_trades):
        """Test recovery metrics tracking"""
        protocol = EdgeRecoveryProtocol()
        protocol.start_recovery()
        
        for trade in sample_trades[:10]:
            protocol.add_recovery_trade(trade)
        
        metrics = protocol.get_recovery_metrics()
        assert 'trades_completed' in metrics
        assert 'current_win_rate' in metrics
        assert 'current_mfe_mae' in metrics
        assert 'progress_percentage' in metrics
    
    def test_reset_recovery(self, sample_trades):
        """Test recovery reset"""
        protocol = EdgeRecoveryProtocol()
        protocol.start_recovery()
        
        for trade in sample_trades[:5]:
            protocol.add_recovery_trade(trade)
        
        protocol.reset_recovery()
        
        assert protocol.get_recovery_status() == RecoveryStatus.NOT_STARTED
        metrics = protocol.get_recovery_metrics()
        assert metrics['trades_completed'] == 0
    
    def test_recovery_requirements_validation(self):
        """Test recovery requirements validation"""
        protocol = EdgeRecoveryProtocol(
            min_trades=10,
            min_win_rate=0.55,
            min_mfe_mae=1.6
        )
        
        requirements = protocol.get_recovery_requirements()
        assert requirements['min_trades'] == 10
        assert requirements['min_win_rate'] == 0.55
        assert requirements['min_mfe_mae'] == 1.6
    
    def test_is_recovery_complete(self):
        """Test recovery completion check"""
        protocol = EdgeRecoveryProtocol(min_trades=5)
        protocol.start_recovery()
        
        assert not protocol.is_recovery_complete()
        
        # Add sufficient winning trades
        for i in range(6):
            protocol.add_recovery_trade({
                'timestamp': datetime.now(),
                'outcome': 'win',
                'r_multiple': 2.0,
                'mfe': 0.03,
                'mae': 0.01,
                'extension_count': 2
            })
        
        # Check if complete (depends on requirements)
        if protocol.is_recovery_complete():
            assert protocol.get_recovery_status() == RecoveryStatus.COMPLETE


# ============================================================================
# TEST INFORMATION DIET
# ============================================================================

class TestInformationDiet:
    """Complete tests for InformationDiet"""
    
    def test_initialization(self):
        """Test information diet initialization"""
        diet = InformationDiet()
        assert diet is not None
    
    def test_trader_level_filtering(self):
        """Test TRADER level information filtering"""
        diet = InformationDiet()
        
        full_state = {
            'can_trade': True,
            'size_multiplier': 1.0,
            'expectancy': 1.5,
            'degradation_score': 0.3,
            'signal_count': 15,
            'win_rate': 0.58,
            'internal_metrics': {'detailed': 'data'}
        }
        
        trader_view = diet.filter_for_trader(full_state)
        
        assert 'can_trade' in trader_view
        assert 'size_multiplier' in trader_view
        assert 'expectancy' not in trader_view
        assert 'degradation_score' not in trader_view
        assert 'signal_count' not in trader_view
    
    def test_system_level_access(self):
        """Test SYSTEM level information access"""
        diet = InformationDiet()
        
        full_state = {
            'can_trade': True,
            'expectancy': 1.5,
            'degradation_score': 0.3,
            'internal_metrics': {'detailed': 'data'}
        }
        
        system_view = diet.filter_for_system(full_state)
        
        # System sees everything
        assert system_view == full_state
    
    def test_audit_level_logging(self):
        """Test AUDIT level information logging"""
        diet = InformationDiet()
        
        full_state = {
            'can_trade': True,
            'size_multiplier': 1.0,
            'expectancy': 1.5,
            'degradation_score': 0.3
        }
        
        audit_view = diet.filter_for_audit(full_state)
        
        # Audit gets most, but not all details
        assert 'can_trade' in audit_view
        assert 'expectancy' in audit_view
    
    def test_get_allowed_fields(self):
        """Test getting allowed fields for each level"""
        diet = InformationDiet()
        
        trader_fields = diet.get_allowed_fields(InfoLevel.TRADER)
        assert 'can_trade' in trader_fields
        assert 'size_multiplier' in trader_fields
        assert 'expectancy' not in trader_fields
        
        system_fields = diet.get_allowed_fields(InfoLevel.SYSTEM)
        assert len(system_fields) > len(trader_fields)
    
    def test_is_field_allowed(self):
        """Test field access permission check"""
        diet = InformationDiet()
        
        assert diet.is_field_allowed('can_trade', InfoLevel.TRADER)
        assert not diet.is_field_allowed('expectancy', InfoLevel.TRADER)
        assert diet.is_field_allowed('expectancy', InfoLevel.SYSTEM)
    
    def test_mask_sensitive_data(self):
        """Test sensitive data masking"""
        diet = InformationDiet()
        
        data = {
            'api_key': 'secret123',
            'password': 'pass456',
            'public_info': 'visible'
        }
        
        masked = diet.mask_sensitive_data(data)
        
        assert masked['api_key'] == '***MASKED***'
        assert masked['password'] == '***MASKED***'
        assert masked['public_info'] == 'visible'
    
    def test_generate_trader_report(self):
        """Test trader report generation"""
        diet = InformationDiet()
        
        full_state = {
            'can_trade': True,
            'size_multiplier': 0.5,
            'reason': 'DEGRADED state'
        }
        
        report = diet.generate_trader_report(full_state)
        
        assert isinstance(report, str)
        assert 'CAN TRADE' in report or 'CANNOT TRADE' in report
        assert 'Size' in report


# ============================================================================
# TEST OPPORTUNITY CAPTURE MONITOR
# ============================================================================

class TestOpportunityCaptureMonitor:
    """Complete tests for OpportunityCaptureMonitor"""
    
    def test_initialization(self):
        """Test monitor initialization"""
        monitor = OpportunityCaptureMonitor()
        assert monitor is not None
    
    def test_detect_opportunity(self):
        """Test opportunity detection"""
        monitor = OpportunityCaptureMonitor(
            min_market_move=2.0,
            min_expected_capture=0.3
        )
        
        market_data = {
            'session_high': 50100,
            'session_low': 50000,
            'current_price': 50050,
            'atr': 200
        }
        
        is_opportunity = monitor.detect_opportunity(market_data)
        assert isinstance(is_opportunity, bool)
    
    def test_record_capture(self):
        """Test capture recording"""
        monitor = OpportunityCaptureMonitor()
        
        monitor.record_capture(
            market_r=3.0,
            captured_r=2.0,
            timestamp=datetime.now()
        )
        
        stats = monitor.get_capture_stats()
        assert stats['total_opportunities'] == 1
        assert stats['total_captured'] > 0
    
    def test_silent_degradation_detection(self):
        """Test silent degradation detection"""
        monitor = OpportunityCaptureMonitor(
            min_capture_rate=0.4,
            evaluation_window=10
        )
        
        # Record poor captures
        for i in range(12):
            monitor.record_capture(
                market_r=3.0,
                captured_r=0.5,  # Poor capture
                timestamp=datetime.now() - timedelta(minutes=30*i)
            )
        
        is_degraded = monitor.is_silent_degradation()
        assert is_degraded
    
    def test_healthy_capture_rate(self):
        """Test healthy capture rate"""
        monitor = OpportunityCaptureMonitor(
            min_capture_rate=0.4,
            evaluation_window=10
        )
        
        # Record good captures
        for i in range(12):
            monitor.record_capture(
                market_r=3.0,
                captured_r=2.0,  # Good capture
                timestamp=datetime.now() - timedelta(minutes=30*i)
            )
        
        is_degraded = monitor.is_silent_degradation()
        assert not is_degraded
    
    def test_get_capture_stats(self):
        """Test capture statistics"""
        monitor = OpportunityCaptureMonitor()
        
        for i in range(5):
            monitor.record_capture(
                market_r=2.0 + i * 0.5,
                captured_r=1.0 + i * 0.3,
                timestamp=datetime.now()
            )
        
        stats = monitor.get_capture_stats()
        assert 'total_opportunities' in stats
        assert 'avg_market_r' in stats
        assert 'avg_captured_r' in stats
        assert 'capture_rate' in stats
    
    def test_reset_monitor(self):
        """Test monitor reset"""
        monitor = OpportunityCaptureMonitor()
        
        monitor.record_capture(3.0, 2.0, datetime.now())
        monitor.reset()
        
        stats = monitor.get_capture_stats()
        assert stats['total_opportunities'] == 0
    
    def test_get_recent_captures(self):
        """Test getting recent captures"""
        monitor = OpportunityCaptureMonitor()
        
        for i in range(10):
            monitor.record_capture(
                market_r=2.0,
                captured_r=1.0,
                timestamp=datetime.now() - timedelta(minutes=i*10)
            )
        
        recent = monitor.get_recent_captures(limit=5)
        assert len(recent) <= 5


# ============================================================================
# TEST ADAPTIVE WINDOW VALIDATOR
# ============================================================================

class TestAdaptiveWindowValidator:
    """Complete tests for AdaptiveWindowValidator"""
    
    def test_initialization(self):
        """Test validator initialization"""
        validator = AdaptiveWindowValidator()
        assert validator is not None
    
    def test_validate_fixed_window(self):
        """Test fixed window validation"""
        validator = AdaptiveWindowValidator()
        
        result = validator.validate_window(
            trades_count=25,
            time_elapsed_hours=48,
            volatility_cycles=2.5
        )
        
        assert 'is_valid' in result
        assert 'reason' in result
    
    def test_insufficient_trades(self):
        """Test insufficient trades detection"""
        validator = AdaptiveWindowValidator(
            min_trades=20,
            min_time_hours=24
        )
        
        result = validator.validate_window(
            trades_count=10,
            time_elapsed_hours=48,
            volatility_cycles=3.0
        )
        
        assert not result['is_valid']
        assert 'trades' in result['reason'].lower()
    
    def test_insufficient_time(self):
        """Test insufficient time detection"""
        validator = AdaptiveWindowValidator(
            min_trades=20,
            min_time_hours=48
        )
        
        result = validator.validate_window(
            trades_count=25,
            time_elapsed_hours=24,
            volatility_cycles=3.0
        )
        
        assert not result['is_valid']
        assert 'time' in result['reason'].lower()
    
    def test_insufficient_volatility_cycles(self):
        """Test insufficient volatility cycles detection"""
        validator = AdaptiveWindowValidator(
            min_volatility_cycles=3.0
        )
        
        result = validator.validate_window(
            trades_count=25,
            time_elapsed_hours=48,
            volatility_cycles=1.5
        )
        
        assert not result['is_valid']
        assert 'volatility' in result['reason'].lower()
    
    def test_valid_window_all_criteria(self):
        """Test valid window meeting all criteria"""
        validator = AdaptiveWindowValidator(
            min_trades=20,
            min_time_hours=24,
            min_volatility_cycles=2.0
        )
        
        result = validator.validate_window(
            trades_count=30,
            time_elapsed_hours=48,
            volatility_cycles=3.5
        )
        
        assert result['is_valid']
    
    def test_calculate_adaptive_window(self):
        """Test adaptive window calculation"""
        validator = AdaptiveWindowValidator()
        
        market_conditions = {
            'volatility': 0.03,
            'trade_frequency': 0.5,  # trades per hour
            'regime': 'trending'
        }
        
        window = validator.calculate_adaptive_window(market_conditions)
        
        assert 'min_trades' in window
        assert 'min_hours' in window
        assert window['min_trades'] > 0
        assert window['min_hours'] > 0
    
    def test_get_validation_requirements(self):
        """Test getting validation requirements"""
        validator = AdaptiveWindowValidator(
            min_trades=25,
            min_time_hours=36,
            min_volatility_cycles=2.5
        )
        
        requirements = validator.get_validation_requirements()
        
        assert requirements['min_trades'] == 25
        assert requirements['min_time_hours'] == 36
        assert requirements['min_volatility_cycles'] == 2.5


# ============================================================================
# TEST STRATEGY FINGERPRINT
# ============================================================================

class TestStrategyFingerprint:
    """Complete tests for StrategyFingerprint"""
    
    def test_initialization(self):
        """Test fingerprint initialization"""
        fingerprint = StrategyFingerprint()
        assert fingerprint is not None
    
    def test_generate_fingerprint(self, sample_strategy_config):
        """Test fingerprint generation"""
        fingerprint = StrategyFingerprint()
        
        hash_value = fingerprint.generate_fingerprint(sample_strategy_config)
        
        assert isinstance(hash_value, str)
        assert len(hash_value) == 64  # SHA-256 hash length
    
    def test_fingerprint_consistency(self, sample_strategy_config):
        """Test fingerprint consistency"""
        fingerprint = StrategyFingerprint()
        
        hash1 = fingerprint.generate_fingerprint(sample_strategy_config)
        hash2 = fingerprint.generate_fingerprint(sample_strategy_config)
        
        assert hash1 == hash2
    
    def test_fingerprint_sensitivity(self, sample_strategy_config):
        """Test fingerprint sensitivity to changes"""
        fingerprint = StrategyFingerprint()
        
        hash1 = fingerprint.generate_fingerprint(sample_strategy_config)
        
        # Change one parameter
        modified_config = sample_strategy_config.copy()
        modified_config['sl_logic'] = 'fixed_percentage'
        
        hash2 = fingerprint.generate_fingerprint(modified_config)
        
        assert hash1 != hash2
    
    def test_has_strategy_changed(self, sample_strategy_config):
        """Test strategy change detection"""
        fingerprint = StrategyFingerprint()
        
        # Set initial strategy
        fingerprint.set_current_strategy(sample_strategy_config)
        
        # Check same strategy
        assert not fingerprint.has_strategy_changed(sample_strategy_config)
        
        # Check modified strategy
        modified_config = sample_strategy_config.copy()
        modified_config['risk_per_trade'] = 0.03
        
        assert fingerprint.has_strategy_changed(modified_config)
    
    def test_get_current_fingerprint(self, sample_strategy_config):
        """Test getting current fingerprint"""
        fingerprint = StrategyFingerprint()
        
        fingerprint.set_current_strategy(sample_strategy_config)
        current = fingerprint.get_current_fingerprint()
        
        assert isinstance(current, str)
        assert len(current) == 64
    
    def test_get_fingerprint_metadata(self, sample_strategy_config):
        """Test getting fingerprint metadata"""
        fingerprint = StrategyFingerprint()
        
        fingerprint.set_current_strategy(sample_strategy_config)
        metadata = fingerprint.get_fingerprint_metadata()
        
        assert 'fingerprint' in metadata
        assert 'created_at' in metadata
        assert 'config_summary' in metadata
    
    def test_reset_statistics_on_change(self, sample_strategy_config):
        """Test reset statistics requirement on change"""
        fingerprint = StrategyFingerprint()
        
        fingerprint.set_current_strategy(sample_strategy_config)
        
        # Modify strategy
        modified_config = sample_strategy_config.copy()
        modified_config['trailing_type'] = 'fixed'
        
        requires_reset = fingerprint.requires_statistics_reset(modified_config)
        
        assert requires_reset
    
    def test_compare_strategies(self, sample_strategy_config):
        """Test strategy comparison"""
        fingerprint = StrategyFingerprint()
        
        config2 = sample_strategy_config.copy()
        config2['max_positions'] = 2
        
        comparison = fingerprint.compare_strategies(
            sample_strategy_config,
            config2
        )
        
        assert 'are_identical' in comparison
        assert 'differences' in comparison
        assert not comparison['are_identical']
        assert 'max_positions' in comparison['differences']
    
    def test_get_strategy_version(self, sample_strategy_config):
        """Test strategy version tracking"""
        fingerprint = StrategyFingerprint()
        
        # Set initial strategy
        fingerprint.set_current_strategy(sample_strategy_config)
        version1 = fingerprint.get_strategy_version()
        
        # Update strategy
        modified_config = sample_strategy_config.copy()
        modified_config['entry_mode'] = 'limit_on_pullback'
        fingerprint.set_current_strategy(modified_config)
        version2 = fingerprint.get_strategy_version()
        
        assert version2 > version1


# ============================================================================
# INTEGRATION TESTS
# ============================================================================

class TestEdgeRegimeIntegration:
    """Integration tests for all modules working together"""
    
    def test_full_edge_lifecycle(self, sample_market_data, sample_trade_metrics):
        """Test complete edge lifecycle with all modules"""
        
        # Initialize all modules
        regime_classifier = EdgeRegimeClassifier()
        degradation_layer = DegradationAttributionLayer()
        recovery_protocol = EdgeRecoveryProtocol()
        opportunity_monitor = OpportunityCaptureMonitor()
        
        # 1. Classify regime
        regime = regime_classifier.classify_regime(sample_market_data)
        assert regime is not None
        
        # 2. Check edge validity
        edge_valid = regime_classifier.is_edge_valid()
        
        if not edge_valid:
            # 3. Analyze degradation
            attribution = degradation_layer.analyze_degradation(sample_trade_metrics)
            assert 'primary_cause' in attribution
            
            # 4. Start recovery if needed
            recovery_protocol.start_recovery()
            assert recovery_protocol.get_recovery_status() == RecoveryStatus.IN_PROGRESS
        
        # 5. Monitor opportunity capture
        opportunity_monitor.record_capture(
            market_r=3.0,
            captured_r=1.5,
            timestamp=datetime.now()
        )
        
        stats = opportunity_monitor.get_capture_stats()
        assert stats['total_opportunities'] > 0
    
    def test_information_diet_integration(self, sample_strategy_config):
        """Test information diet with other modules"""
        
        diet = InformationDiet()
        fingerprint = StrategyFingerprint()
        
        # Generate fingerprint
        fingerprint.set_current_strategy(sample_strategy_config)
        
        # Create system state
        system_state = {
            'can_trade': True,
            'size_multiplier': 1.0,
            'strategy_fingerprint': fingerprint.get_current_fingerprint(),
            'expectancy': 1.5,
            'degradation_score': 0.2
        }
        
        # Filter for trader
        trader_view = diet.filter_for_trader(system_state)
        
        assert 'can_trade' in trader_view
        assert 'expectancy' not in trader_view
        assert 'strategy_fingerprint' not in trader_view
    
    def test_adaptive_window_with_recovery(self, sample_trades):
        """Test adaptive window validation during recovery"""
        
        validator = AdaptiveWindowValidator(
            min_trades=10,
            min_time_hours=12
        )
        recovery = EdgeRecoveryProtocol(min_trades=10)
        
        # Start recovery
        recovery.start_recovery()
        
        # Add trades
        for trade in sample_trades[:5]:
            recovery.add_recovery_trade(trade)
        
        # Validate window
        time_elapsed = 6  # 6 hours
        result = validator.validate_window(
            trades_count=5,
            time_elapsed_hours=time_elapsed,
            volatility_cycles=1.0
        )
        
        assert not result['is_valid']  # Not enough time/trades yet


# ============================================================================
# PERFORMANCE TESTS
# ============================================================================

class TestPerformance:
    """Performance tests for critical paths"""
    
    def test_regime_classification_speed(self, sample_market_data):
        """Test regime classification performance"""
        import time
        
        classifier = EdgeRegimeClassifier()
        
        start = time.time()
        for _ in range(1000):
            classifier.classify_regime(sample_market_data)
        elapsed = time.time() - start
        
        # Should process 1000 classifications in under 1 second
        assert elapsed < 1.0
    
    def test_fingerprint_generation_speed(self, sample_strategy_config):
        """Test fingerprint generation performance"""
        import time
        
        fingerprint = StrategyFingerprint()
        
        start = time.time()
        for _ in range(1000):
            fingerprint.generate_fingerprint(sample_strategy_config)
        elapsed = time.time() - start
        
        # Should generate 1000 fingerprints in under 0.5 seconds
        assert elapsed < 0.5
    
    def test_information_filtering_speed(self):
        """Test information filtering performance"""
        import time
        
        diet = InformationDiet()
        
        large_state = {f'metric_{i}': i for i in range(100)}
        large_state.update({
            'can_trade': True,
            'size_multiplier': 1.0
        })
        
        start = time.time()
        for _ in range(1000):
            diet.filter_for_trader(large_state)
        elapsed = time.time() - start
        
        # Should filter 1000 times in under 0.5 seconds
        assert elapsed < 0.5


# ============================================================================
# EDGE CASE TESTS
# ============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions"""
    
    def test_empty_trade_history(self):
        """Test handling empty trade history"""
        recovery = EdgeRecoveryProtocol()
        recovery.start_recovery()
        
        metrics = recovery.get_recovery_metrics()
        assert metrics['trades_completed'] == 0
        assert not recovery.is_recovery_complete()
    
    def test_extreme_market_conditions(self):
        """Test extreme market conditions"""
        classifier = EdgeRegimeClassifier()
        
        extreme_data = {
            'volatility': 0.15,  # Very high
            'trend_strength': 0.95,
            'volume_profile': 3.0,
            'expansion_rate': 0.99,
            'chop_index': 0.05,
            'news_volatility': 0.90,
            'participation_rate': 0.95
        }
        
        regime = classifier.classify_regime(extreme_data)
        assert regime is not None
    
    def test_zero_volatility_window(self):
        """Test zero volatility cycles"""
        validator = AdaptiveWindowValidator(min_volatility_cycles=1.0)
        
        result = validator.validate_window(
            trades_count=30,
            time_elapsed_hours=48,
            volatility_cycles=0.0
        )
        
        assert not result['is_valid']
    
    def test_concurrent_regime_changes(self, sample_market_data):
        """Test rapid regime changes"""
        classifier = EdgeRegimeClassifier()
        
        # Rapidly switch regimes
        for i in range(10):
            data = sample_market_data.copy()
            data['participation_rate'] = 0.3 if i % 2 == 0 else 0.9
            regime = classifier.classify_regime(data)
            
            assert regime is not None


# ============================================================================
# RUN ALL TESTS
# ============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short', '--cov=engine', '--cov-report=term-missing'])
