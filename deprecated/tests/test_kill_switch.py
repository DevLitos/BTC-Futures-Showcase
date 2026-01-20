"""
Tests for core.kill_switch module - Kill Switch Functionality

These tests verify:
1. Kill switch activates/deactivates correctly
2. Multiple activation sources work (env, file, programmatic)
3. Callbacks are triggered
4. Decorator works
"""

import os
import pytest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

@pytest.fixture(autouse=True)
def clean_kill_switch():
    """Reset kill switch singleton before each test."""
    # Reset singleton using the official reset function
    from core.kill_switch import reset_kill_switch_for_testing, KillSwitch
    reset_kill_switch_for_testing()
    
    # Clear env var
    if 'TRADING_KILL_SWITCH' in os.environ:
        del os.environ['TRADING_KILL_SWITCH']
    
    yield
    
    # Cleanup after test
    reset_kill_switch_for_testing()
    if 'TRADING_KILL_SWITCH' in os.environ:
        del os.environ['TRADING_KILL_SWITCH']


class TestKillSwitchBasic:
    """Test basic kill switch operations."""
    
    def test_initially_inactive(self):
        """Kill switch should be inactive by default."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        assert not ks.is_active
        assert ks.reason == ""
    
    def test_activate_programmatic(self):
        """Should be able to activate programmatically."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Test reason", "test")
        
        assert ks.is_active
        assert ks.reason == "Test reason"
        assert ks.state.activated_by == "test"
    
    def test_deactivate(self):
        """Should be able to deactivate."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Test reason", "test")
        
        result = ks.deactivate("test")
        
        assert result is True
        assert not ks.is_active
        assert ks.reason == ""
    
    def test_deactivation_not_allowed(self):
        """Deactivation can be blocked."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Critical error", "system", allow_deactivation=False)
        
        result = ks.deactivate("user")
        
        assert result is False
        assert ks.is_active  # Still active


class TestKillSwitchEnvVar:
    """Test kill switch activation via environment variable."""
    
    def test_env_var_true(self):
        """TRADING_KILL_SWITCH=true should activate."""
        os.environ['TRADING_KILL_SWITCH'] = 'true'
        
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        assert ks.is_active
    
    def test_env_var_1(self):
        """TRADING_KILL_SWITCH=1 should activate."""
        os.environ['TRADING_KILL_SWITCH'] = '1'
        
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        assert ks.is_active
    
    def test_env_var_false(self):
        """TRADING_KILL_SWITCH=false should not activate."""
        os.environ['TRADING_KILL_SWITCH'] = 'false'
        
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        assert not ks.is_active


class TestKillSwitchFile:
    """Test kill switch activation via file."""
    
    def test_file_activates(self, tmp_path):
        """File presence should activate kill switch."""
        kill_file = tmp_path / "KILL_SWITCH"
        
        from core.kill_switch import init_kill_switch
        
        ks = init_kill_switch(file_path=kill_file)
        
        # Not active without file
        assert not ks.is_active
        
        # Create file
        kill_file.write_text("test")
        
        # Now active
        assert ks.is_active
    
    def test_create_file_flag(self, tmp_path):
        """create_file_flag should create the kill switch file."""
        kill_file = tmp_path / "KILL_SWITCH"
        
        from core.kill_switch import init_kill_switch
        
        ks = init_kill_switch(file_path=kill_file)
        ks.create_file_flag()
        
        assert kill_file.exists()
    
    def test_deactivate_removes_file(self, tmp_path):
        """Deactivation should remove the file flag."""
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("test")
        
        from core.kill_switch import init_kill_switch
        
        ks = init_kill_switch(file_path=kill_file)
        
        # Activate first (file creates state)
        assert ks.is_active
        
        # Deactivate
        ks.deactivate("test")
        
        # File should be removed
        assert not kill_file.exists()


class TestKillSwitchCallbacks:
    """Test kill switch callbacks."""
    
    def test_callback_called_on_activate(self):
        """Registered callback should be called on activation."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        callback = MagicMock()
        ks.register_callback(callback)
        
        ks.activate("Test", "test")
        
        callback.assert_called_once()
        state = callback.call_args[0][0]
        assert state.active is True
        assert state.reason == "Test"
    
    def test_callback_exception_handled(self):
        """Callback exceptions should be handled gracefully."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        
        def bad_callback(state):
            raise ValueError("Callback error")
        
        ks.register_callback(bad_callback)
        
        # Should not raise
        ks.activate("Test", "test")
        
        assert ks.is_active


class TestKillSwitchDecorator:
    """Test @check_kill_switch decorator."""
    
    def test_decorator_allows_when_inactive(self):
        """Decorated function should run when kill switch inactive."""
        from core.kill_switch import check_kill_switch, get_kill_switch
        
        @check_kill_switch
        def my_func():
            return "success"
        
        result = my_func()
        
        assert result == "success"
    
    def test_decorator_blocks_when_active(self):
        """Decorated function should return None when kill switch active."""
        from core.kill_switch import check_kill_switch, get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Test", "test")
        
        @check_kill_switch
        def my_func():
            return "success"
        
        result = my_func()
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_decorator_async_allows_when_inactive(self):
        """Async decorated function should run when kill switch inactive."""
        from core.kill_switch import check_kill_switch, get_kill_switch
        
        @check_kill_switch
        async def my_async_func():
            return "async success"
        
        result = await my_async_func()
        
        assert result == "async success"
    
    @pytest.mark.asyncio
    async def test_decorator_async_blocks_when_active(self):
        """Async decorated function should return None when kill switch active."""
        from core.kill_switch import check_kill_switch, get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Test", "test")
        
        @check_kill_switch
        async def my_async_func():
            return "async success"
        
        result = await my_async_func()
        
        assert result is None


class TestKillSwitchStatus:
    """Test kill switch status reporting."""
    
    def test_get_status_inactive(self):
        """get_status should report inactive state."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        status = ks.get_status()
        
        assert status['active'] is False
        assert status['reason'] == ""
        assert status['activated_at'] is None
    
    def test_get_status_active(self):
        """get_status should report active state with details."""
        from core.kill_switch import get_kill_switch
        
        ks = get_kill_switch()
        ks.activate("Test reason", "test_component")
        
        status = ks.get_status()
        
        assert status['active'] is True
        assert status['reason'] == "Test reason"
        assert status['activated_by'] == "test_component"
        assert status['activated_at'] is not None
        assert status['deactivation_allowed'] is True


class TestKillSwitchSingleton:
    """Test singleton behavior."""
    
    def test_singleton(self):
        """get_kill_switch should return same instance."""
        from core.kill_switch import get_kill_switch
        
        ks1 = get_kill_switch()
        ks2 = get_kill_switch()
        
        assert ks1 is ks2
    
    def test_init_preserves_state(self):
        """Repeated get_kill_switch should preserve state."""
        from core.kill_switch import get_kill_switch
        
        ks1 = get_kill_switch()
        ks1.activate("Test", "test")
        
        ks2 = get_kill_switch()
        
        assert ks2.is_active
        assert ks2.reason == "Test"
