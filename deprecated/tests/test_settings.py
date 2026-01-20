"""
Tests for core.settings module - Configuration Validation

These tests verify:
1. Settings validation works correctly
2. Secrets are properly redacted
3. Client order ID generation is deterministic
4. Startup validation catches errors
"""

import os
import pytest
from datetime import datetime, UTC
from unittest.mock import patch, MagicMock

# Test with isolated environment
@pytest.fixture(autouse=True)
def clean_environment():
    """Reset settings singleton and environment before each test."""
    # Store original env
    original_env = os.environ.copy()
    
    # Clear relevant env vars
    for key in list(os.environ.keys()):
        if key.startswith(('KRAKEN', 'BINANCE', 'TELEGRAM', 'TRADING', 'EXCHANGE')):
            del os.environ[key]
    
    # Reset singleton
    from core.settings import reset_settings
    reset_settings()
    
    yield
    
    # Restore original env
    os.environ.clear()
    os.environ.update(original_env)
    reset_settings()


class TestSettingsValidation:
    """Test settings validation."""
    
    def test_default_settings_paper_mode(self):
        """Default settings should be PAPER mode without errors."""
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import get_settings, TradingMode
        
        settings = get_settings()
        
        assert settings.trading_mode == TradingMode.PAPER
        assert settings.demo_mode is True
        assert settings.exchange.value == "krakenfutures"
    
    def test_live_mode_requires_api_keys(self):
        """LIVE mode should fail without API keys."""
        os.environ['TRADING_MODE'] = 'LIVE'
        os.environ['DEMO_MODE'] = 'false'
        
        from core.settings import Settings
        
        with pytest.raises(ValueError) as exc_info:
            Settings()
        
        assert "KRAKEN_FUTURES_API_KEY" in str(exc_info.value)
    
    def test_live_mode_with_api_keys_succeeds(self):
        """LIVE mode with API keys should succeed."""
        os.environ['TRADING_MODE'] = 'LIVE'
        os.environ['DEMO_MODE'] = 'false'
        os.environ['KRAKEN_FUTURES_API_KEY'] = 'test_key_123'
        os.environ['KRAKEN_FUTURES_SECRET'] = 'test_secret_456'
        
        from core.settings import Settings, TradingMode
        
        settings = Settings()
        
        assert settings.trading_mode == TradingMode.LIVE
        assert settings.effective_api_key is not None
        assert settings.effective_api_key.get_secret_value() == 'test_key_123'
    
    def test_kraken_spot_auto_corrects_to_futures(self):
        """Exchange 'kraken' should auto-correct to 'krakenfutures'."""
        os.environ['EXCHANGE'] = 'kraken'
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import Settings, ExchangeType
        
        settings = Settings()
        
        assert settings.exchange == ExchangeType.KRAKEN_FUTURES
    
    def test_ports_must_be_unique(self):
        """Metrics and health ports must be different."""
        os.environ['DEMO_MODE'] = 'true'
        os.environ['METRICS_PORT'] = '8000'
        os.environ['HEALTH_PORT'] = '8000'
        
        from core.settings import Settings
        
        with pytest.raises(ValueError) as exc_info:
            Settings()
        
        assert "must be different" in str(exc_info.value)
    
    def test_symbols_computed_correctly(self):
        """Symbols should be computed based on exchange."""
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import Settings
        
        settings = Settings()
        
        assert settings.ws_symbol == "PF_XBTUSD"
        assert settings.ccxt_symbol == "BTC/USD:USD"
    
    def test_credentials_dict_excludes_secrets_in_demo(self):
        """In demo mode, credentials dict should not have API keys."""
        os.environ['DEMO_MODE'] = 'true'
        os.environ['KRAKEN_FUTURES_API_KEY'] = 'should_not_appear'
        
        from core.settings import Settings
        
        settings = Settings()
        creds = settings.get_credentials_dict()
        
        assert 'apiKey' not in creds
        assert 'secret' not in creds


class TestSecretRedaction:
    """Test secret redaction utilities."""
    
    def test_redact_api_key_in_text(self):
        """API keys in text should be redacted."""
        from core.settings import redact_secrets
        
        text = "KRAKEN_FUTURES_API_KEY=abc123xyz KRAKEN_FUTURES_SECRET=secret456"
        result = redact_secrets(text)
        
        assert "abc123xyz" not in result
        assert "secret456" not in result
        assert "***REDACTED***" in result
    
    def test_redact_json_style_secrets(self):
        """Secrets in JSON format should be redacted."""
        from core.settings import redact_secrets
        
        text = '{"apiKey": "my_super_secret_key_12345", "other": "value"}'
        result = redact_secrets(text)
        
        assert "my_super_secret_key" not in result
        assert "***REDACTED***" in result
        assert '"other": "value"' in result
    
    def test_mask_secret_shows_partial(self):
        """mask_secret should show first and last chars."""
        from core.settings import mask_secret
        from pydantic import SecretStr
        
        secret = SecretStr("abcdefghijklmnop")
        result = mask_secret(secret, show_chars=4)
        
        assert result == "abcd****mnop"
    
    def test_mask_secret_handles_none(self):
        """mask_secret should handle None gracefully."""
        from core.settings import mask_secret
        
        result = mask_secret(None)
        
        assert result == "<not set>"
    
    def test_mask_secret_short_values(self):
        """Short secrets should be fully masked."""
        from core.settings import mask_secret
        from pydantic import SecretStr
        
        secret = SecretStr("abc")
        result = mask_secret(secret, show_chars=4)
        
        assert result == "***"


class TestClientOrderIdGeneration:
    """Test deterministic client order ID generation."""
    
    def test_same_inputs_same_id(self):
        """Same inputs should produce same ID."""
        from core.settings import generate_client_order_id
        
        ts = datetime(2026, 1, 14, 12, 0, 0, tzinfo=UTC)
        
        id1 = generate_client_order_id("BTC/USD:USD", "buy", ts)
        id2 = generate_client_order_id("BTC/USD:USD", "buy", ts)
        
        assert id1 == id2
    
    def test_different_side_different_id(self):
        """Different side should produce different ID."""
        from core.settings import generate_client_order_id
        
        ts = datetime(2026, 1, 14, 12, 0, 0, tzinfo=UTC)
        
        id_buy = generate_client_order_id("BTC/USD:USD", "buy", ts)
        id_sell = generate_client_order_id("BTC/USD:USD", "sell", ts)
        
        assert id_buy != id_sell
    
    def test_different_bucket_different_id(self):
        """Different time buckets should produce different IDs."""
        from core.settings import generate_client_order_id
        
        ts1 = datetime(2026, 1, 14, 12, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 1, 14, 12, 2, 0, tzinfo=UTC)  # 2 min later
        
        id1 = generate_client_order_id("BTC/USD:USD", "buy", ts1, bucket_seconds=60)
        id2 = generate_client_order_id("BTC/USD:USD", "buy", ts2, bucket_seconds=60)
        
        assert id1 != id2
    
    def test_same_bucket_same_id(self):
        """Same time bucket should produce same ID (for idempotency)."""
        from core.settings import generate_client_order_id
        
        ts1 = datetime(2026, 1, 14, 12, 0, 10, tzinfo=UTC)
        ts2 = datetime(2026, 1, 14, 12, 0, 50, tzinfo=UTC)  # Same minute
        
        id1 = generate_client_order_id("BTC/USD:USD", "buy", ts1, bucket_seconds=60)
        id2 = generate_client_order_id("BTC/USD:USD", "buy", ts2, bucket_seconds=60)
        
        assert id1 == id2
    
    def test_id_format(self):
        """ID should have expected format."""
        from core.settings import generate_client_order_id
        
        ts = datetime(2026, 1, 14, 12, 0, 0, tzinfo=UTC)
        
        order_id = generate_client_order_id("BTC/USD:USD", "buy", ts)
        
        assert order_id.startswith("dt_")
        assert "_b_" in order_id  # buy -> b
        parts = order_id.split("_")
        assert len(parts) == 4


class TestKillSwitch:
    """Test kill switch functionality."""
    
    def test_kill_switch_env_var(self):
        """Kill switch should activate from env var."""
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import Settings
        
        settings = Settings()
        
        # Not active by default
        assert not settings.is_kill_switch_active()
        
        # Set env var
        os.environ['TRADING_KILL_SWITCH'] = 'true'
        
        assert settings.is_kill_switch_active()
    
    def test_kill_switch_file(self, tmp_path):
        """Kill switch should activate from file presence."""
        os.environ['DEMO_MODE'] = 'true'
        
        kill_file = tmp_path / "KILL_SWITCH"
        os.environ['KILL_SWITCH_FILE'] = str(kill_file)
        
        from core.settings import Settings
        
        settings = Settings()
        
        # Not active without file
        assert not settings.is_kill_switch_active()
        
        # Create file
        kill_file.write_text("kill")
        
        assert settings.is_kill_switch_active()


class TestComputedProperties:
    """Test computed properties."""
    
    def test_is_live_mode_property(self):
        """is_live_mode should reflect trading mode."""
        os.environ['TRADING_MODE'] = 'LIVE'
        os.environ['KRAKEN_FUTURES_API_KEY'] = 'test'
        os.environ['KRAKEN_FUTURES_SECRET'] = 'test'
        
        from core.settings import Settings
        
        settings = Settings()
        
        assert settings.is_live_mode is True
        assert settings.is_paper_mode is False
        assert settings.is_backtest_mode is False
    
    def test_is_paper_mode_property(self):
        """is_paper_mode should reflect trading mode."""
        os.environ['TRADING_MODE'] = 'PAPER'
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import Settings
        
        settings = Settings()
        
        assert settings.is_live_mode is False
        assert settings.is_paper_mode is True
        assert settings.is_backtest_mode is False
    
    def test_run_id_unique(self):
        """Each Settings instance should have unique run_id."""
        os.environ['DEMO_MODE'] = 'true'
        
        from core.settings import Settings
        
        settings1 = Settings()
        settings2 = Settings()
        
        assert settings1.run_id != settings2.run_id
