from src.infrastructure.exchanges.symbol_mapper import SymbolMapper


def test_rest_symbol_krakenfutures_btc() -> None:
    assert SymbolMapper.to_rest_symbol("krakenfutures", "BTC/USD") == "BTC/USD:USD"


def test_rest_symbol_krakenfutures_ws_symbol() -> None:
    assert SymbolMapper.to_rest_symbol("krakenfutures", "PF_XBTUSD") == "BTC/USD:USD"


def test_ws_symbol_krakenfutures_btc() -> None:
    assert SymbolMapper.to_ws_symbol("krakenfutures", "BTC/USD") == "PF_XBTUSD"


def test_ws_symbol_generic_slash_removed() -> None:
    assert SymbolMapper.to_ws_symbol("krakenfutures", "ETH/USD") == "ETHUSD"
