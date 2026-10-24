from src.shared.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


def test_circuit_opens_after_threshold() -> None:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout_seconds=30))
    cb.on_failure()
    cb.on_failure()
    assert cb.is_open is False
    cb.on_failure()
    assert cb.is_open is True


def test_circuit_resets_on_success() -> None:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=30))
    cb.on_failure()
    assert cb.is_open is True
    cb.on_success()
    assert cb.is_open is False
    assert cb.failures == 0
