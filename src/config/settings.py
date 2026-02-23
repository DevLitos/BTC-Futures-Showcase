"""
Production Settings - Centralized Configuration with Validation

This module provides a single source of truth for all configuration using
pydantic-settings. It validates all settings at startup and fails fast
if required values are missing or invalid.

USAGE:
    from src.config.settings import get_settings, Settings
    
    settings = get_settings()  # Singleton, validated once at startup
    
    # Access settings
    api_key = settings.kraken_futures_api_key
    if settings.is_live_mode:
        # LIVE-specific logic
        
SECURITY:
    - All secrets are automatically redacted in logs
    - SecretStr type prevents accidental exposure
    - Validation runs BEFORE any trading operations
"""

import os
import uuid
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, UTC
from enum import Enum
from typing import Optional, Set, Literal
from functools import lru_cache
import logging

from pydantic import (
    BaseModel, 
    Field, 
    SecretStr, 
    field_validator,
    model_validator,
    computed_field
)
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


class TradingMode(str, Enum):
    """Trading mode with strict behavioral contracts."""
    LIVE = "LIVE"       # Real money, real exchange, full reconciliation
    PAPER = "PAPER"     # Real orders but simulated equity (WARNING: still real!)
    BACKTEST = "BACKTEST"  # Historical data, simulated execution


class ExchangeType(str, Enum):
    """Supported exchanges."""
    KRAKEN_FUTURES = "krakenfutures"
    BINANCE = "binance"
    KRAKEN_SPOT = "kraken"


class Settings(BaseSettings):
    """
    Centralized application settings with validation.
    
    All values come from environment variables or .env file.
    Validation runs at instantiation - fails fast on invalid config.
    """
    
    model_config = SettingsConfigDict(
        env_file=(".env.secrets", ".env.config"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra env vars
    )
    
    # =========================================================================
    # Runtime Identification
    # =========================================================================
    run_id: str = Field(
        default_factory=lambda: f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        description="Unique identifier for this run (auto-generated)"
    )
    
    # Build/version info (injected at build time)
    app_version: str = Field(default="2.1.0", alias="APP_VERSION")
    git_commit: str = Field(default="unknown", alias="GIT_COMMIT")
    build_time: str = Field(default="unknown", alias="BUILD_TIME")
    
    # =========================================================================
    # Trading Mode
    # =========================================================================
    trading_mode: TradingMode = Field(
        default=TradingMode.PAPER,
        alias="TRADING_MODE",
        description="Trading mode: LIVE, PAPER, or BACKTEST"
    )
    
    demo_mode: bool = Field(
        default=False,
        alias="DEMO_MODE",
        description="Run without API keys for testing"
    )
    
    # =========================================================================
    # Exchange Configuration
    # =========================================================================
    exchange: ExchangeType = Field(
        default=ExchangeType.KRAKEN_FUTURES,
        alias="EXCHANGE",
        description="Exchange to use"
    )
    
    # Kraken Futures credentials
    kraken_futures_api_key: Optional[SecretStr] = Field(
        default=None,
        alias="KRAKEN_FUTURES_API_KEY"
    )
    kraken_futures_secret: Optional[SecretStr] = Field(
        default=None,
        alias="KRAKEN_FUTURES_SECRET"
    )
    
    # Kraken Spot fallback (if user puts keys in wrong vars)
    kraken_api_key: Optional[SecretStr] = Field(
        default=None,
        alias="KRAKEN_API_KEY"
    )
    kraken_secret: Optional[SecretStr] = Field(
        default=None,
        alias="KRAKEN_SECRET"
    )
    
    # Binance credentials
    binance_api_key: Optional[SecretStr] = Field(
        default=None,
        alias="BINANCE_API_KEY"
    )
    binance_secret: Optional[SecretStr] = Field(
        default=None,
        alias="BINANCE_SECRET"
    )
    
    # =========================================================================
    # Telegram Notifications
    # =========================================================================
    telegram_bot_token: Optional[SecretStr] = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id: Optional[str] = Field(
        default=None,
        alias="TELEGRAM_CHAT_ID"
    )
    
    # =========================================================================
    # Persistence
    # =========================================================================
    persistence_backend: Literal["parquet", "postgresql"] = Field(
        default="parquet",
        alias="PERSISTENCE"
    )
    database_url: Optional[SecretStr] = Field(
        default=None,
        alias="DATABASE_URL"
    )
    storage_path: Path = Field(
        default=Path("storage"),
        alias="STORAGE_PATH",
        description="Storage directory path (relative or absolute)"
    )
    
    # =========================================================================
    # Server Ports
    # =========================================================================
    metrics_port: int = Field(
        default=8000,
        alias="METRICS_PORT",
        ge=1024,
        le=65535
    )
    health_port: int = Field(
        default=8001,
        alias="HEALTH_PORT",
        ge=1024,
        le=65535
    )
    
    # =========================================================================
    # Kill Switch
    # =========================================================================
    kill_switch_enabled: bool = Field(
        default=False,
        alias="TRADING_KILL_SWITCH",
        description="If True, stop accepting new trades"
    )
    kill_switch_file: Path = Field(
        default=Path("/app/KILL_SWITCH"),
        alias="KILL_SWITCH_FILE",
        description="If this file exists, stop accepting new trades"
    )
    
    # =========================================================================
    # Logging
    # =========================================================================
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL"
    )
    log_format: Literal["text", "json"] = Field(
        default="text",
        alias="LOG_FORMAT",
        description="Log format: text for development, json for production"
    )
    log_path: Path = Field(
        default=Path("logs"),
        alias="LOG_PATH",
        description="Logs directory path (relative or absolute)"
    )
    
    # =========================================================================
    # API Configuration
    # =========================================================================
    api_port: int = Field(
        default=8080,
        alias="API_PORT",
        ge=1024,
        le=65535,
        description="Port for FastAPI REST API"
    )
    api_key: Optional[SecretStr] = Field(
        default=None,
        alias="API_KEY",
        description="API key for protected endpoints (X-API-Key header)"
    )
    api_cors_origins: str = Field(
        default="*",
        alias="API_CORS_ORIGINS",
        description="Comma-separated list of allowed CORS origins"
    )
    api_rate_limit: int = Field(
        default=60,
        alias="API_RATE_LIMIT",
        ge=1,
        le=1000,
        description="Max requests per minute per IP"
    )
    
    # =========================================================================
    # Safety Limits
    # =========================================================================
    max_position_usd: float = Field(
        default=10000.0,
        alias="MAX_POSITION_USD",
        gt=0
    )
    max_daily_trades: int = Field(
        default=100,
        alias="MAX_DAILY_TRADES",
        gt=0
    )
    max_daily_loss_pct: float = Field(
        default=3.0,
        alias="MAX_DAILY_LOSS_PCT",
        gt=0,
        le=100
    )
    
    # =========================================================================
    # Time Sync
    # =========================================================================
    max_time_drift_ms: int = Field(
        default=500,
        alias="MAX_TIME_DRIFT_MS",
        description="Maximum allowed clock drift in milliseconds"
    )
    
    # =========================================================================
    # Pullback Short Engine - Feature Flags
    # =========================================================================
    enable_short_pullback: bool = Field(
        default=False,
        alias="ENABLE_SHORT_PULLBACK",
        description="Master switch for tactical pullback shorts (correction capture)"
    )
    short_pullback_live_allowed: bool = Field(
        default=False,
        alias="SHORT_PULLBACK_LIVE_ALLOWED",
        description="Allow pullback shorts in LIVE mode (test in paper first)"
    )
    short_pullback_max_daily: int = Field(
        default=5,
        alias="SHORT_PULLBACK_MAX_DAILY",
        description="Maximum pullback short trades per day",
        ge=0,
        le=20
    )
    
    # =========================================================================
    # Engine Timing Configuration
    # =========================================================================
    reconcile_interval_seconds: int = Field(
        default=60,
        alias="RECONCILE_INTERVAL_SECONDS",
        description="Interval between position reconciliation cycles",
        ge=10,
        le=300
    )
    heartbeat_interval_seconds: int = Field(
        default=60,
        alias="HEARTBEAT_INTERVAL_SECONDS",
        description="Interval between heartbeat log messages",
        ge=10,
        le=300
    )
    health_bridge_interval_seconds: float = Field(
        default=5.0,
        alias="HEALTH_BRIDGE_INTERVAL_SECONDS",
        description="Interval for health status file writes",
        ge=1.0,
        le=60.0
    )
    
    # =========================================================================
    # Exchange Timeouts
    # =========================================================================
    exchange_timeout_short: float = Field(
        default=10.0,
        alias="EXCHANGE_TIMEOUT_SHORT",
        description="Timeout for simple exchange queries (seconds)",
        ge=5.0,
        le=60.0
    )
    exchange_timeout_medium: float = Field(
        default=30.0,
        alias="EXCHANGE_TIMEOUT_MEDIUM",
        description="Timeout for order operations (seconds)",
        ge=10.0,
        le=120.0
    )
    exchange_timeout_long: float = Field(
        default=60.0,
        alias="EXCHANGE_TIMEOUT_LONG",
        description="Timeout for complex operations (seconds)",
        ge=30.0,
        le=300.0
    )
    
    # =========================================================================
    # Validators
    # =========================================================================
    
    @field_validator("exchange", mode="before")
    @classmethod
    def normalize_exchange(cls, v: str) -> str:
        """Normalize exchange name and auto-fix common mistakes."""
        if isinstance(v, str):
            v = v.lower().strip()
            # Auto-fix: kraken spot doesn't support perpetuals
            if v == "kraken":
                logger.warning("Exchange 'kraken' (Spot) doesn't support perpetuals. Forcing 'krakenfutures'.")
                return "krakenfutures"
        return v
    
    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        """Normalize log level to uppercase."""
        if isinstance(v, str):
            return v.upper().strip()
        return v
    
    @model_validator(mode="after")
    def validate_credentials(self) -> "Settings":
        """
        Validate that required credentials are present based on mode.
        
        LIVE mode requires valid API keys.
        PAPER mode warns but allows (since it still executes real orders).
        BACKTEST mode doesn't need credentials.
        """
        if self.demo_mode:
            return self
        
        if self.trading_mode == TradingMode.BACKTEST:
            return self
        
        # For LIVE/PAPER with Kraken Futures
        if self.exchange == ExchangeType.KRAKEN_FUTURES:
            api_key = self.kraken_futures_api_key or self.kraken_api_key
            secret = self.kraken_futures_secret or self.kraken_secret
            
            if self.trading_mode == TradingMode.LIVE:
                if not api_key or not secret:
                    raise ValueError(
                        "LIVE mode requires KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_SECRET. "
                        "Set these environment variables or use TRADING_MODE=PAPER for testing."
                    )
            elif self.trading_mode == TradingMode.PAPER:
                if not api_key or not secret:
                    logger.warning(
                        "⚠️ PAPER mode without API keys - will run in DEMO mode. "
                        "Note: PAPER mode with keys WILL execute real orders!"
                    )
        
        return self
    
    @model_validator(mode="after")
    def validate_telegram_in_live(self) -> "Settings":
        """Validate Telegram is configured for LIVE mode notifications."""
        if self.trading_mode == TradingMode.LIVE:
            if not self.telegram_bot_token or not self.telegram_chat_id:
                logger.warning(
                    "⚠️ LIVE mode without Telegram configuration. "
                    "Critical alerts will not be sent. "
                    "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
                )
        return self
    
    @model_validator(mode="after")
    def validate_ports_unique(self) -> "Settings":
        """Ensure metrics and health ports are different."""
        if self.metrics_port == self.health_port:
            raise ValueError(
                f"METRICS_PORT ({self.metrics_port}) and HEALTH_PORT ({self.health_port}) "
                "must be different."
            )
        return self
    
    # =========================================================================
    # Computed Properties
    # =========================================================================
    
    @computed_field
    @property
    def is_live_mode(self) -> bool:
        """True if running in LIVE mode with real money at risk."""
        return self.trading_mode == TradingMode.LIVE
    
    @computed_field
    @property
    def is_backtest_mode(self) -> bool:
        """True if running in BACKTEST mode with simulated execution."""
        return self.trading_mode == TradingMode.BACKTEST
    
    @computed_field
    @property
    def ws_symbol(self) -> str:
        """WebSocket symbol for the configured exchange."""
        if self.exchange == ExchangeType.KRAKEN_FUTURES:
            return "PF_XBTUSD"
        elif self.exchange == ExchangeType.BINANCE:
            return "btcusdt"
        else:
            return "BTCUSD"
    
    @computed_field
    @property
    def ccxt_symbol(self) -> str:
        """CCXT symbol for the configured exchange."""
        if self.exchange == ExchangeType.KRAKEN_FUTURES:
            return "BTC/USD:USD"
        elif self.exchange == ExchangeType.BINANCE:
            return "BTC/USDT:USDT"
        else:
            return "BTC/USD"
    
    @property
    def effective_api_key(self) -> Optional[SecretStr]:
        """Get the effective API key for the current exchange."""
        if self.exchange == ExchangeType.KRAKEN_FUTURES:
            return self.kraken_futures_api_key or self.kraken_api_key
        elif self.exchange == ExchangeType.BINANCE:
            return self.binance_api_key
        return None
    
    @property
    def effective_secret(self) -> Optional[SecretStr]:
        """Get the effective secret for the current exchange."""
        if self.exchange == ExchangeType.KRAKEN_FUTURES:
            return self.kraken_futures_secret or self.kraken_secret
        elif self.exchange == ExchangeType.BINANCE:
            return self.binance_secret
        return None
    
    def get_credentials_dict(self) -> dict:
        """
        Get credentials as a dictionary for CCXT.
        
        Secrets are exposed here for CCXT initialization only.
        Never log the return value!
        """
        creds: dict[str, str | bool | dict[str, str]] = {"enableRateLimit": True}
        
        api_key = self.effective_api_key
        secret = self.effective_secret
        
        if api_key and secret and not self.demo_mode:
            creds["apiKey"] = api_key.get_secret_value()
            creds["secret"] = secret.get_secret_value()
        
        if self.exchange == ExchangeType.BINANCE:
            creds["options"] = {"defaultType": "future"}
        
        return creds


# =========================================================================
# Secret Redaction Utilities
# =========================================================================

# Patterns to redact (add more as needed)
SECRET_PATTERNS = {
    "KRAKEN_FUTURES_API_KEY",
    "KRAKEN_FUTURES_SECRET", 
    "KRAKEN_API_KEY",
    "KRAKEN_SECRET",
    "BINANCE_API_KEY",
    "BINANCE_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "DATABASE_URL",
    "apiKey",
    "secret",
    "password",
    "token",
}


def redact_secrets(text: str) -> str:
    """
    Redact any secrets from a text string.
    
    This function looks for common secret patterns and replaces them
    with ***REDACTED***.
    
    Args:
        text: Text that may contain secrets
        
    Returns:
        Text with secrets redacted
    """
    import re
    
    result = text
    
    # Redact key=value patterns (including JSON with quotes)
    for pattern in SECRET_PATTERNS:
        # Match pattern=value or pattern:value or "pattern": "value"
        # Handles: KEY=value, KEY: value, "KEY": "value", 'KEY': 'value'
        regex = re.compile(
            rf'(["\']?{pattern}["\']?\s*[=:]\s*["\']?)([^\s,\}}\]"\']+)(["\']?)',
            re.IGNORECASE
        )
        result = regex.sub(r'\1***REDACTED***\3', result)
    
    # Redact anything that looks like an API key (long alphanumeric strings)
    # This catches keys in JSON or other formats
    result = re.sub(
        r'(["\']?(?:api[_-]?key|secret|password|token)["\']?\s*[=:]\s*["\']?)([a-zA-Z0-9+/]{20,})(["\']?)',
        r'\1***REDACTED***\3',
        result,
        flags=re.IGNORECASE
    )
    
    return result


def mask_secret(secret: Optional[SecretStr], show_chars: int = 4) -> str:
    """
    Mask a secret for safe display.
    
    Shows first and last N characters, masks the rest.
    
    Args:
        secret: The secret to mask
        show_chars: Number of characters to show at start and end
        
    Returns:
        Masked string like "abc1****xyz9"
    """
    if not secret:
        return "<not set>"
    
    value = secret.get_secret_value()
    if len(value) <= show_chars * 2:
        return "*" * len(value)
    
    return f"{value[:show_chars]}****{value[-show_chars:]}"


# =========================================================================
# Singleton Access (Thread-Safe)
# =========================================================================

import threading

_settings_instance: Optional[Settings] = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    """
    Get the global settings instance (thread-safe).
    
    Settings are validated once at first access and cached.
    Subsequent calls return the same instance.
    Uses double-checked locking pattern for thread safety.
    
    Raises:
        ValidationError: If settings are invalid
        
    Returns:
        Validated Settings instance
    """
    global _settings_instance
    
    # Fast path: already initialized
    if _settings_instance is not None:
        return _settings_instance
    
    # Slow path: acquire lock and initialize
    with _settings_lock:
        # Double-check after acquiring lock
        if _settings_instance is None:
            instance = Settings()
            
            # Log configuration summary (with secrets redacted)
            logger.info("=" * 70)
            logger.info("⚙️  CONFIGURATION LOADED")
            logger.info("=" * 70)
            logger.info(f"   Run ID: {instance.run_id}")
            logger.info(f"   Version: {instance.app_version} ({instance.git_commit})")
            logger.info(f"   Mode: {instance.trading_mode.value}")
            logger.info(f"   Exchange: {instance.exchange.value}")
            logger.info(f"   Symbol (WS): {instance.ws_symbol}")
            logger.info(f"   Symbol (CCXT): {instance.ccxt_symbol}")
            logger.info(f"   API Key: {mask_secret(instance.effective_api_key)}")
            logger.info(f"   Telegram: {'configured' if instance.telegram_chat_id else 'not configured'}")
            logger.info(f"   Metrics Port: {instance.metrics_port}")
            logger.info(f"   Health Port: {instance.health_port}")
            logger.info(f"   Log Level: {instance.log_level}")
            logger.info(f"   Log Format: {instance.log_format}")
            logger.info("=" * 70)
            
            _settings_instance = instance
    
    return _settings_instance


def reset_settings() -> None:
    """
    Reset settings instance (for testing only).
    
    WARNING: This should never be called in production!
    """
    global _settings_instance
    with _settings_lock:
        _settings_instance = None


# =========================================================================
# Client Order ID Generation
# =========================================================================

def generate_client_order_id(
    symbol: str,
    side: str,
    timestamp: datetime,
    strategy_id: str = "main",
    bucket_seconds: int = 60
) -> str:
    """
    Generate a deterministic client order ID for idempotency.
    
    The ID is based on:
    - Symbol (e.g., "BTC/USD:USD")
    - Side (e.g., "buy", "sell")
    - Timestamp bucket (rounded to bucket_seconds)
    - Strategy ID
    
    This ensures that retrying the same order within the same time bucket
    will use the same client order ID, preventing duplicates.
    
    Args:
        symbol: Trading symbol
        side: Order side (buy/sell)
        timestamp: Order timestamp
        strategy_id: Strategy identifier
        bucket_seconds: Time bucket size for deduplication
        
    Returns:
        Deterministic client order ID
    """
    # Round timestamp to bucket
    bucket = int(timestamp.timestamp() / bucket_seconds) * bucket_seconds
    
    # Create deterministic hash
    data = f"{symbol}:{side}:{bucket}:{strategy_id}"
    hash_bytes = hashlib.sha256(data.encode()).hexdigest()[:16]
    
    # Format: dt_<symbol_short>_<side>_<hash>
    symbol_short = symbol.replace("/", "").replace(":", "")[:6]
    side_short = side[0].lower()  # b or s
    
    return f"dt_{symbol_short}_{side_short}_{hash_bytes}"


# =========================================================================
# Startup Validation
# =========================================================================

async def validate_startup(settings: Settings) -> dict:
    """
    Perform comprehensive startup validation.
    
    This should be called during initialization to verify:
    1. API connectivity
    2. Symbol validity
    3. Time synchronization
    4. Telegram connectivity
    
    Args:
        settings: Application settings
        
    Returns:
        Dictionary with validation results
    """
    results = {
        "valid": True,
        "checks": {},
        "warnings": [],
        "errors": []
    }
    
    # Check API connectivity (if not demo/backtest)
    if not settings.demo_mode and not settings.is_backtest_mode:
        try:
            import ccxt.async_support as ccxt
            
            STARTUP_TIMEOUT = 30.0  # 30 seconds for startup validation
            
            ExchangeClass = getattr(ccxt, settings.exchange.value)
            config = settings.get_credentials_dict()
            config['timeout'] = 10000  # 10s internal CCXT timeout
            exchange = ExchangeClass(config)
            
            # 🔥 FIX (Jan 21, 2026): Add timeout to prevent blocking
            await asyncio.wait_for(exchange.load_markets(), timeout=STARTUP_TIMEOUT)
            
            # Verify symbol exists
            if settings.ccxt_symbol not in exchange.markets:
                results["errors"].append(
                    f"Symbol {settings.ccxt_symbol} not found on {settings.exchange.value}"
                )
                results["valid"] = False
            else:
                results["checks"]["symbol_valid"] = True
            
            # Check time sync (some exchanges don't support fetch_time)
            try:
                # 🔥 FIX (Jan 21, 2026): Add timeout to prevent blocking
                server_time = await asyncio.wait_for(exchange.fetch_time(), timeout=10.0)
                local_time = int(datetime.now(UTC).timestamp() * 1000)
                drift_ms = abs(server_time - local_time)
                
                results["checks"]["time_drift_ms"] = drift_ms
                
                if drift_ms > settings.max_time_drift_ms:
                    results["warnings"].append(
                        f"Clock drift detected: {drift_ms}ms > {settings.max_time_drift_ms}ms threshold"
                    )
            except asyncio.TimeoutError:
                results["warnings"].append("Time sync check timed out (10s)")
            except Exception as time_error:
                # Some exchanges (krakenfutures) don't support fetch_time
                results["warnings"].append(f"Time sync check not available: {str(time_error)[:50]}")
            
            # 🔥 FIX (Jan 21, 2026): Add timeout to prevent blocking
            try:
                await asyncio.wait_for(exchange.close(), timeout=5.0)
            except asyncio.TimeoutError:
                pass  # Ignore timeout on close
            results["checks"]["exchange_connected"] = True
            
        except Exception as e:
            results["errors"].append(f"Exchange connection failed: {str(e)}")
            results["valid"] = False
    
    # Check Telegram (if configured)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            import telegram
            
            bot = telegram.Bot(token=settings.telegram_bot_token.get_secret_value())
            # 🔥 FIX (Jan 21, 2026): Add timeout to Telegram validation
            try:
                bot_info = await asyncio.wait_for(bot.get_me(), timeout=10.0)
                results["checks"]["telegram_valid"] = True
                results["checks"]["telegram_bot_name"] = bot_info.username
            except asyncio.TimeoutError:
                results["warnings"].append("Telegram validation timed out (10s)")
            
        except Exception as e:
            results["warnings"].append(f"Telegram validation failed: {str(e)}")
    
    # Check storage paths
    try:
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        settings.log_path.mkdir(parents=True, exist_ok=True)
        results["checks"]["storage_writable"] = True
    except Exception as e:
        results["errors"].append(f"Storage path not writable: {str(e)}")
        results["valid"] = False
    
    return results
