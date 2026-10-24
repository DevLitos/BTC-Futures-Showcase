"""Simple async-friendly circuit breaker with half-open probing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 30


class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        cfg = config or CircuitBreakerConfig()
        self.failure_threshold = cfg.failure_threshold
        self.recovery_timeout = timedelta(seconds=cfg.recovery_timeout_seconds)
        self.failures = 0
        self.opened_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        return datetime.now(UTC) - self.opened_at < self.recovery_timeout

    def allow_request(self) -> bool:
        if self.opened_at is None:
            return True
        if not self.is_open:
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def on_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = datetime.now(UTC)
