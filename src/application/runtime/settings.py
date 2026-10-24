"""Runtime settings for trading system."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class RunMode(Enum):
    """Operating mode of the trading system."""

    LIVE = "live"  # Real trading with real money
    BACKTEST = "backtest"  # Backtest with historical data


@dataclass(frozen=True)
class RuntimeSettings:
    """Trading system configuration.

    The runtime has two explicit modes: live trading and backtesting.
    """

    exchange_name: str
    ws_symbol: str
    rest_symbol: str
    health_symbol: str
    metrics_port: int
    health_interval_seconds: int
    health_max_interval_seconds: int
    health_cb_failure_threshold: int
    health_cb_recovery_seconds: int
    kraken_api_key: str
    kraken_private_key: str
    # Trading settings
    trading_enabled: bool = False
    mode: RunMode = RunMode.BACKTEST
    position_size_btc: float = 0.001
    # Only strategies with positive expectancy from backtest
    enabled_strategies: tuple[str, ...] = ("FVG_TREND", "IFVG")  # ORDER_BLOCK disabled
    candle_intervals: tuple[int, ...] = (60, 300)  # 1m, 5m
    primary_interval: int = 300  # 5m
    # Logging verbosity
    log_every_tick: bool = False  # Set True for debug
    log_interval_seconds: int = 10  # Status log interval

    @staticmethod
    def _int_env(name: str, default: int, minimum: int = 1) -> int:
        raw = os.getenv(name, str(default))
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid integer for {name}: {raw}") from exc
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}, got {value}")
        return value

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name, str(default)).lower()
        return raw in ("true", "1", "yes", "on")

    @staticmethod
    def _float_env(name: str, default: float) -> float:
        raw = os.getenv(name, str(default))
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid float for {name}: {raw}") from exc

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        # Only profitable strategies enabled by default
        strategies_raw = os.getenv("ENABLED_STRATEGIES", "FVG_TREND,IFVG")
        strategies = tuple(s.strip() for s in strategies_raw.split(",") if s.strip())

        intervals_raw = os.getenv("CANDLE_INTERVALS", "60,300")
        intervals = tuple(int(i.strip()) for i in intervals_raw.split(",") if i.strip())

        # Mode: "live" or "backtest"
        mode_str = os.getenv("MODE", "backtest").lower()
        if mode_str not in {RunMode.LIVE.value, RunMode.BACKTEST.value}:
            raise ValueError(f"Invalid MODE={mode_str!r}; expected 'live' or 'backtest'")
        mode = RunMode(mode_str)

        return cls(
            exchange_name=os.getenv("EXCHANGE", "krakenfutures"),
            ws_symbol=os.getenv("WS_SYMBOL", "PF_XBTUSD"),
            rest_symbol=os.getenv("REST_SYMBOL", "BTC/USD:USD"),
            health_symbol=os.getenv("HEALTH_SYMBOL", "BTC/USD:USD"),
            metrics_port=cls._int_env("METRICS_PORT", 8000, minimum=1),
            health_interval_seconds=cls._int_env("HEALTH_INTERVAL_SECONDS", 15, minimum=1),
            health_max_interval_seconds=cls._int_env("HEALTH_MAX_INTERVAL_SECONDS", 60, minimum=1),
            health_cb_failure_threshold=cls._int_env("HEALTH_CB_FAILURE_THRESHOLD", 3, minimum=1),
            health_cb_recovery_seconds=cls._int_env("HEALTH_CB_RECOVERY_SECONDS", 30, minimum=1),
            kraken_api_key=os.getenv("KRAKEN_API_KEY", ""),
            kraken_private_key=os.getenv("KRAKEN_PRIVATE_KEY", ""),
            trading_enabled=cls._bool_env("TRADING_ENABLED", False),
            mode=mode,
            position_size_btc=cls._float_env("POSITION_SIZE_BTC", 0.001),
            enabled_strategies=strategies,
            candle_intervals=intervals,
            primary_interval=cls._int_env("PRIMARY_INTERVAL", 300, minimum=1),
            log_every_tick=cls._bool_env("LOG_EVERY_TICK", False),
            log_interval_seconds=cls._int_env("LOG_INTERVAL_SECONDS", 10, minimum=1),
        )
