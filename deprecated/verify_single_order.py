import asyncio
from unittest.mock import AsyncMock, MagicMock
from engine.signal_engine import SignalEngine, BotState, RiskEngine
from engine.execution import RESTExecutionEngine

async def test_strict_checks():
    print("🧪 Starting Strict Order Check Verification...")
    
    # Mocks
    market_data = MagicMock()
    execution = AsyncMock(spec=RESTExecutionEngine)
    external_data = MagicMock()
    trade_logger = AsyncMock()
    
    # Mock execution methods
    execution.get_open_orders = AsyncMock(return_value=[])
    execution.get_position = AsyncMock(return_value={'contracts': 0})
    
    # Initialize Engine
    engine = SignalEngine(market_data, execution, external_data, trade_logger)
    engine.state_machine.state = BotState.IDLE
    engine.current_bias = 'LONG'  # Fake bias to bypass filters
    engine.symbol = 'BTC/USDT'
    
    # TWEAK: Bypass Internal Checks to reach Exchange Checks
    engine.risk_engine.has_open_position = MagicMock(return_value=False)
    # Bypass market data checks
    engine.market_data.get_candles = MagicMock(return_value=[{'close': 100, 'high': 100, 'low': 100, 'volume': 100}] * 50)
    
    print("\n[TEST 1] No orders, No position -> Should proceed (fail at order placement mock)")
    # Expected: Passes checks, tries to place order (we mock place_limit_order to fail to stop there)
    execution.place_limit_order = AsyncMock(return_value=None) 
    await engine._execute_entry(100.0, 1.0)
    
    # Verify get_open_orders and get_position were called
    execution.get_open_orders.assert_called()
    execution.get_position.assert_called()
    print("✅ Strict checks were called")

    print("\n[TEST 2] Existing Open Orders -> Should ABORT")
    execution.get_open_orders = AsyncMock(return_value=[{'id': '123'}])
    execution.place_limit_order.reset_mock()
    
    await engine._execute_entry(100.0, 1.0)
    
    execution.place_limit_order.assert_not_called()
    print("✅ Aborted due to open orders")

    print("\n[TEST 3] Existing Position -> Should ABORT")
    execution.get_open_orders = AsyncMock(return_value=[])
    execution.get_position = AsyncMock(return_value={'contracts': 1})
    execution.place_limit_order.reset_mock()
    
    await engine._execute_entry(100.0, 1.0)
    
    execution.place_limit_order.assert_not_called()
    print("✅ Aborted due to open position")
    
    print("\n🎉 Verification Complete!")

if __name__ == "__main__":
    asyncio.run(test_strict_checks())
