from src.application.runtime.settings import RunMode, RuntimeSettings


def test_settings_default_is_safe(monkeypatch) -> None:
    for name in (
        "MODE",
        "TRADING_ENABLED",
        "EXCHANGE",
        "WS_SYMBOL",
        "REST_SYMBOL",
        "HEALTH_SYMBOL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = RuntimeSettings.from_env()

    assert settings.mode is RunMode.BACKTEST
    assert settings.trading_enabled is False
    assert settings.ws_symbol == "PF_XBTUSD"
    assert settings.rest_symbol == "BTC/USD:USD"
