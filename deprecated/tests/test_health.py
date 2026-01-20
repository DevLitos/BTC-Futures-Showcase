"""
Tests for core.health module - Health Check Endpoints

These tests verify:
1. Liveness checks work correctly
2. Readiness checks work correctly
3. Health status aggregation
4. HTTP endpoints return correct status codes
"""

import pytest
import json
import threading
import time
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch


class TestHealthStatus:
    """Test HealthStatus class."""
    
    def test_initial_state(self):
        """Initial state should be not ready."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        
        is_live, _ = status.is_live()
        is_ready, reason = status.is_ready()
        
        # Live check should pass (just started)
        # Ready should fail (no WS connected)
        assert not is_ready
        assert "WebSocket" in reason
    
    def test_becomes_ready_with_components(self):
        """Should become ready when all components are healthy."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_ws_connected(True)
        status.set_exchange_reachable(True)
        status.set_time_drift_ms(50)
        status.update_ws_heartbeat()
        
        is_ready, reason = status.is_ready()
        
        assert is_ready
        assert reason == "OK"
    
    def test_not_ready_without_ws(self):
        """Should not be ready without WebSocket."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_exchange_reachable(True)
        status.set_ws_connected(False)
        
        is_ready, reason = status.is_ready()
        
        assert not is_ready
        assert "WebSocket" in reason
    
    def test_not_ready_with_time_drift(self):
        """Should not be ready with excessive time drift."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_ws_connected(True)
        status.set_exchange_reachable(True)
        status.set_time_drift_ms(1000)  # 1 second drift
        status.update_ws_heartbeat()
        status.max_time_drift_ms = 500
        
        is_ready, reason = status.is_ready()
        
        assert not is_ready
        assert "drift" in reason.lower()
    
    def test_not_ready_with_integrity_error(self):
        """Should not be ready with integrity error."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_ws_connected(True)
        status.set_exchange_reachable(True)
        status.set_time_drift_ms(50)
        status.update_ws_heartbeat()
        status.set_integrity_error(True)
        
        is_ready, reason = status.is_ready()
        
        assert not is_ready
        assert "Integrity" in reason
    
    def test_live_with_recent_loop(self):
        """Should be live with recent strategy loop."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.update_loop_heartbeat()
        
        is_live, reason = status.is_live()
        
        assert is_live
        assert reason == "OK"
    
    def test_not_live_with_stale_loop(self):
        """Should not be live with stale strategy loop."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status._last_strategy_loop_at = datetime.now(UTC) - timedelta(seconds=120)
        status.max_loop_silence_seconds = 60
        
        is_live, reason = status.is_live()
        
        assert not is_live
        assert "loop silent" in reason.lower()
    
    def test_position_tracking(self):
        """Should track position info."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_position("BTC/USD:USD", "LONG", 0.5)
        
        full_status = status.get_full_status()
        
        assert full_status['position'] is not None
        assert full_status['position']['has_open'] is True
        assert full_status['position']['symbol'] == "BTC/USD:USD"
        assert full_status['position']['size'] == 0.5
    
    def test_clear_position(self):
        """Should clear position info."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        status.set_position("BTC/USD:USD", "LONG", 0.5)
        status.clear_position()
        
        full_status = status.get_full_status()
        
        assert full_status['position'] is None
    
    def test_get_full_status_structure(self):
        """Full status should have expected structure."""
        from core.health import HealthStatus
        
        status = HealthStatus(
            version="1.0.0",
            git_commit="abc123",
            run_id="test_run"
        )
        
        full_status = status.get_full_status()
        
        assert 'live' in full_status
        assert 'ready' in full_status
        assert 'uptime_seconds' in full_status
        assert 'version' in full_status
        assert 'components' in full_status
        assert 'timestamps' in full_status
        
        assert full_status['version'] == "1.0.0"
        assert full_status['git_commit'] == "abc123"


class TestHealthStatusThreadSafety:
    """Test thread safety of HealthStatus."""
    
    def test_concurrent_updates(self):
        """Concurrent updates should not cause issues."""
        from core.health import HealthStatus
        
        status = HealthStatus()
        errors = []
        
        def updater():
            try:
                for _ in range(100):
                    status.set_ws_connected(True)
                    status.update_ws_heartbeat()
                    status.set_exchange_reachable(True)
                    status.is_ready()
                    status.get_full_status()
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=updater) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0


class TestHealthHandler:
    """Test HTTP health handler."""
    
    def test_healthz_returns_200_when_live(self):
        """GET /healthz should return 200 when live."""
        from core.health import HealthHandler, HealthStatus
        
        status = HealthStatus()
        status.update_loop_heartbeat()
        HealthHandler.health_status = status
        
        # Create mock request/response
        handler = MagicMock(spec=HealthHandler)
        handler.health_status = status
        handler.path = "/healthz"
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        
        # Mock methods
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        
        # Call the actual method bound to mock
        HealthHandler._handle_liveness(handler)
        
        handler.send_response.assert_called_once_with(200)
    
    def test_readyz_returns_503_when_not_ready(self):
        """GET /readyz should return 503 when not ready."""
        from core.health import HealthHandler, HealthStatus
        
        status = HealthStatus()
        status.set_ws_connected(False)  # Not ready
        HealthHandler.health_status = status
        
        handler = MagicMock(spec=HealthHandler)
        handler.health_status = status
        handler.path = "/readyz"
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        
        HealthHandler._handle_readiness(handler)
        
        handler.send_response.assert_called_once_with(503)


class TestHealthStatusGlobal:
    """Test global health status functions."""
    
    def test_init_health_status(self):
        """init_health_status should create and return status."""
        from core.health import init_health_status, get_health_status
        
        status = init_health_status(
            version="2.0.0",
            git_commit="def456",
            run_id="test_123"
        )
        
        assert status is not None
        assert status.version == "2.0.0"
        
        # Should be retrievable
        retrieved = get_health_status()
        assert retrieved is status
    
    def test_get_health_status_none_before_init(self):
        """get_health_status should return None before init."""
        import core.health as health_module
        
        # Reset global
        health_module._health_status = None
        
        result = health_module.get_health_status()
        
        assert result is None
