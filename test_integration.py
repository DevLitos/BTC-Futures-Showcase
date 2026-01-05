"""
Integration test for BTC Perpetual Trading System
Validates all components work together correctly (imports + structure)
"""

import asyncio
import sys
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Component imports
try:
    from ingest.kraken_websocket import KrakenFuturesWebSocket
    from ingest.order_book import OrderBookManager
    from engine.market_data import SynchronizedMultiTimeframeBuffer
    from engine.execution import RESTExecutionEngine
    from engine.external_data import ExternalDataManager
    from engine.persistence import TradeLogger, PositionReconciler
    from engine.signal_engine import SignalEngine
    from engine.state_machine import StateMachine, BotState
    logger.info("✅ All imports successful")
except Exception as e:
    logger.error(f"❌ Import failed: {e}")
    sys.exit(1)


class IntegrationTest:
    """Test suite for all components - structure validation"""
    
    def __init__(self):
        self.symbol = 'BTC/USD'
        self.passed = 0
        self.failed = 0
    
    async def test_kraken_websocket(self):
        """Test 1: Kraken WebSocket structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 1: Kraken WebSocket")
        logger.info("="*70)
        
        try:
            ws = KrakenFuturesWebSocket([self.symbol])
            
            # Check attributes
            assert hasattr(ws, 'connect'), "Missing connect() method"
            assert hasattr(ws, 'disconnect'), "Missing disconnect() method"
            assert hasattr(ws, 'is_heartbeat_healthy'), "Missing is_heartbeat_healthy() method"
            assert hasattr(ws, 'last_tick'), "Missing last_tick attribute"
            assert hasattr(ws, 'symbols'), "Missing symbols attribute"
            
            logger.info("✅ TEST 1 PASSED: WebSocket structure valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 1 FAILED: {e}")
            self.failed += 1
    
    async def test_order_book_manager(self):
        """Test 2: Order book manager structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 2: Order Book Manager")
        logger.info("="*70)
        
        try:
            obm = OrderBookManager(self.symbol)
            
            # Test basic operations
            obm.update_bid(50000, 1.5)
            obm.update_ask(50001, 1.2)
            
            # Test methods exist
            bid, ask, spread = obm.get_spread()
            assert spread is None or spread >= 0, "Spread should be >= 0 or None"
            
            imbalance = obm.get_imbalance()
            assert 0 <= imbalance <= 100, "Imbalance should be 0-100%"
            
            logger.info(f"  Sample spread: {spread if spread else 'N/A'}%")
            logger.info(f"  Sample imbalance: {imbalance:.2f}%")
            logger.info("✅ TEST 2 PASSED: Order book structure valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 2 FAILED: {e}")
            self.failed += 1
    
    async def test_market_data_buffer(self):
        """Test 3: Market data buffer structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 3: Market Data Buffer")
        logger.info("="*70)
        
        try:
            mdb = SynchronizedMultiTimeframeBuffer(self.symbol)
            
            # Check methods
            assert hasattr(mdb, 'add_candle'), "Missing add_candle() method"
            assert hasattr(mdb, 'get_candles'), "Missing get_candles() method"
            assert hasattr(mdb, 'has_sufficient_data'), "Missing has_sufficient_data() method"
            
            # Test with sample data
            sample_candle = {
                'timestamp': int(datetime.now().timestamp() * 1000),
                'open': 50000.0,
                'high': 50100.0,
                'low': 49900.0,
                'close': 50050.0,
                'volume': 1000.0
            }
            
            mdb.add_candle(sample_candle)
            candles = mdb.get_candles('1m')
            
            assert candles is not None, "Should return candles"
            assert isinstance(candles, list) or len(candles) >= 0, "Candles should be iterable"
            
            logger.info(f"  Candles stored: {len(mdb.candles['1m'])}")
            logger.info("✅ TEST 3 PASSED: Market data buffer valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 3 FAILED: {e}")
            self.failed += 1
    
    async def test_execution_engine(self):
        """Test 4: REST execution engine structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 4: REST Execution Engine")
        logger.info("="*70)
        
        try:
            creds = {'api_key': 'test_key', 'secret': 'test_secret'}
            exec_engine = RESTExecutionEngine('binance', creds)
            
            # Check methods
            assert hasattr(exec_engine, 'place_limit_order'), "Missing place_limit_order()"
            assert hasattr(exec_engine, 'cancel_order'), "Missing cancel_order()"
            assert hasattr(exec_engine, 'poll_fill_status'), "Missing poll_fill_status()"
            assert hasattr(exec_engine, 'get_position'), "Missing get_position()"
            assert hasattr(exec_engine, 'close_position_market'), "Missing close_position_market()"
            assert hasattr(exec_engine, 'initialize'), "Missing initialize()"
            
            logger.info("✅ TEST 4 PASSED: Execution engine structure valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 4 FAILED: {e}")
            self.failed += 1
    
    async def test_external_data_manager(self):
        """Test 5: External data manager structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 5: External Data Manager")
        logger.info("="*70)
        
        try:
            edm = ExternalDataManager(self.symbol, 'binance')
            
            # Check methods
            assert hasattr(edm, 'can_trade'), "Missing can_trade()"
            assert hasattr(edm, 'start'), "Missing start()"
            assert hasattr(edm, 'stop'), "Missing stop()"
            assert hasattr(edm, 'get_status'), "Missing get_status()"
            
            # Test can_trade
            can_trade, reason = edm.can_trade()
            assert isinstance(can_trade, bool), "can_trade should return bool"
            assert isinstance(reason, str), "reason should be string"
            
            logger.info(f"  Can trade: {can_trade} ({reason})")
            logger.info("✅ TEST 5 PASSED: External data manager valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 5 FAILED: {e}")
            self.failed += 1
    
    async def test_persistence_layer(self):
        """Test 6: Persistence layer structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 6: Persistence Layer")
        logger.info("="*70)
        
        try:
            logger_obj = TradeLogger('parquet', None)
            
            # Check methods
            assert hasattr(logger_obj, 'log_event'), "Missing log_event()"
            assert hasattr(logger_obj, 'log_trade'), "Missing log_trade()"
            assert hasattr(logger_obj, 'flush_all'), "Missing flush_all()"
            
            logger.info("✅ TEST 6 PASSED: Persistence layer valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 6 FAILED: {e}")
            self.failed += 1
    
    async def test_signal_engine(self):
        """Test 7: Signal engine structure"""
        logger.info("\n" + "="*70)
        logger.info("TEST 7: Signal Engine")
        logger.info("="*70)
        
        try:
            # Create minimal components
            mdb = SynchronizedMultiTimeframeBuffer(self.symbol)
            creds = {'api_key': 'test', 'secret': 'test'}
            exec_engine = RESTExecutionEngine('binance', creds)
            edm = ExternalDataManager(self.symbol, 'binance')
            logger_obj = TradeLogger('parquet', None)
            
            # Create signal engine
            se = SignalEngine(
                market_data=mdb,
                execution=exec_engine,
                external_data=edm,
                trade_logger=logger_obj
            )
            
            # Check initialization
            assert hasattr(se, 'start'), "Missing start() method"
            assert hasattr(se, 'stop'), "Missing stop() method"
            assert hasattr(se, 'get_status'), "Missing get_status() method"
            assert hasattr(se, 'initialize'), "Missing initialize() method"
            assert se.state_machine is not None, "State machine not initialized"
            assert se.risk_engine is not None, "Risk engine not initialized"
            
            logger.info("✅ TEST 7 PASSED: Signal engine structure valid")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 7 FAILED: {e}")
            self.failed += 1
    
    async def test_state_machine(self):
        """Test 8: State machine transitions"""
        logger.info("\n" + "="*70)
        logger.info("TEST 8: State Machine")
        logger.info("="*70)
        
        try:
            sm = StateMachine()
            
            # Check initial state
            assert sm.state == BotState.IDLE, "Should start in IDLE state"
            
            # Test transitions
            sm.transition_to(BotState.WAIT_FILL, "test transition")
            assert sm.state == BotState.WAIT_FILL, "State transition to WAIT_FILL failed"
            
            sm.transition_to(BotState.MANAGE, "test transition")
            assert sm.state == BotState.MANAGE, "State transition to MANAGE failed"
            
            sm.transition_to(BotState.IDLE, "test transition")
            assert sm.state == BotState.IDLE, "State transition back to IDLE failed"
            
            logger.info("✅ TEST 8 PASSED: State machine working correctly")
            self.passed += 1
            
        except Exception as e:
            logger.error(f"❌ TEST 8 FAILED: {e}")
            self.failed += 1
    
    async def run_all(self):
        """Run all tests"""
        logger.info("\n" + "█"*70)
        logger.info("█ BTC PERPETUAL TRADING SYSTEM - INTEGRATION TESTS")
        logger.info("█"*70)
        
        await self.test_kraken_websocket()
        await self.test_order_book_manager()
        await self.test_market_data_buffer()
        await self.test_execution_engine()
        await self.test_external_data_manager()
        await self.test_persistence_layer()
        await self.test_signal_engine()
        await self.test_state_machine()
        
        # Summary
        total = self.passed + self.failed
        logger.info("\n" + "█"*70)
        logger.info("█ TEST SUMMARY")
        logger.info("█"*70)
        logger.info(f"Total Tests: {total}")
        logger.info(f"✅ Passed: {self.passed}")
        logger.info(f"❌ Failed: {self.failed}")
        
        if self.failed == 0:
            logger.info("\n✅ ALL TESTS PASSED!")
            return True
        else:
            logger.warning(f"\n⚠️  {self.failed} test(s) failed")
            return False


async def main():
    """Main entry point"""
    test = IntegrationTest()
    success = await test.run_all()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Tests cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Test error: {e}", exc_info=True)
        sys.exit(1)
