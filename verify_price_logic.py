
import logging
from engine.market_data import SynchronizedMultiTimeframeBuffer

# Configure logging to see output
logging.basicConfig(level=logging.INFO)

def test_market_data_price_updates():
    print("Testing Market Data Price Updates...")
    
    # Initialize
    md = SynchronizedMultiTimeframeBuffer('BTC/USD')
    
    # Test 1: Initial state
    assert md.get_current_price() == 0.0, "Initial price should be 0.0"
    print("✓ Initial state OK")
    
    # Test 2: Update via Tick (Ticker)
    md.add_tick({'price': 50000.0, 'timestamp': 1000})
    assert md.get_current_price() == 50000.0, f"Price should be 50000.0, got {md.get_current_price()}"
    print("✓ Tick update OK")
    
    # Test 3: Update via Trade (Trades)
    md.add_trade({'price': 50100.0, 'size': 1.0, 'is_buyer_maker': False, 'timestamp': 1001})
    assert md.get_current_price() == 50100.0, f"Price should be 50100.0, got {md.get_current_price()}"
    print("✓ Trade update OK (Robustness Fix)")
    
    # Test 4: Update via Candle Close (Legacy fallback)
    # Reset manually to test fallback (though robust code keeps last_price)
    # But let's test that add_candle updates it if it was 0 or stale
    md.last_price = 0.0 
    md.add_candle({'timeframe': '1m', 'close': 50200.0, 'open': 50100, 'high': 50200, 'low': 50100, 'volume': 10, 'timestamp': 2000})
    assert md.get_current_price() == 50200.0, f"Price should be 50200.0, got {md.get_current_price()}"
    print("✓ Candle update OK")
    
    print("\n✅ All Market Data logic tests PASSED")

if __name__ == "__main__":
    test_market_data_price_updates()
