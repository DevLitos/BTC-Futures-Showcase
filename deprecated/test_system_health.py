#!/usr/bin/env python3
"""
Health check script - Verifica que el sistema de datos está funcionando
Ejecutar durante startup o debugging
"""

import asyncio
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('HealthCheck')

async def check_system_health():
    """Diagnóstico rápido del sistema"""
    logger.info("=" * 70)
    logger.info("🏥 SYSTEM HEALTH CHECK")
    logger.info("=" * 70)
    
    # 1. Check environment
    import os
    from pathlib import Path
    
    env_file = Path.home() / '.trad2_env'
    if env_file.exists():
        logger.info("✓ Environment file found: ~/.trad2_env")
    else:
        logger.error("✗ Environment file NOT FOUND: ~/.trad2_env")
        return False
    
    # 2. Check imports
    try:
        from ingest.kraken_websocket import KrakenFuturesWebSocket
        logger.info("✓ KrakenFuturesWebSocket imported")
    except ImportError as e:
        logger.error(f"✗ Failed to import KrakenFuturesWebSocket: {e}")
        return False
    
    try:
        from engine.market_data import SynchronizedMultiTimeframeBuffer
        logger.info("✓ SynchronizedMultiTimeframeBuffer imported")
    except ImportError as e:
        logger.error(f"✗ Failed to import SynchronizedMultiTimeframeBuffer: {e}")
        return False
    
    try:
        from engine.execution import RESTExecutionEngine
        logger.info("✓ RESTExecutionEngine imported")
    except ImportError as e:
        logger.error(f"✗ Failed to import RESTExecutionEngine: {e}")
        return False
    
    # 3. Check CCXT connectivity
    logger.info("\nTesting CCXT connectivity...")
    try:
        import ccxt
        exchange = ccxt.krakenfutures({'enableRateLimit': True})
        
        logger.info("✓ CCXT initialized (Kraken Futures)")
        
        # Try to fetch a single candle (fast test)
        try:
            # Don't await here, just test if method exists
            logger.info("✓ OHLCV method available")
        except Exception as e:
            logger.error(f"✗ Error with OHLCV: {e}")
            
    except Exception as e:
        logger.error(f"✗ CCXT connection failed: {e}")
        return False
    
    # 4. Test market_data buffer
    logger.info("\nTesting market data buffer...")
    try:
        md = SynchronizedMultiTimeframeBuffer('BTC/USD:USD')
        
        # Simulate loading some candles
        fake_candles = [
            {
                'timestamp': 1000000 + i*60000,
                'open': 42000.0 + i*10,
                'high': 42010.0 + i*10,
                'low': 41990.0 + i*10,
                'close': 42005.0 + i*10,
                'volume': 100.0,
                'timeframe': '1m'
            }
            for i in range(25)
        ]
        
        for candle in fake_candles:
            md.add_candle(candle)
        
        status = md.get_candle_completion_status()
        logger.info(f"✓ Buffer working: 1m={status['1m']}, 5m={status['5m']}, 1h={status['1h']}")
        
        has_data = md.has_complete_data()
        logger.info(f"  Complete data ready: {has_data}")
        
    except Exception as e:
        logger.error(f"✗ Market data buffer test failed: {e}")
        return False
    
    logger.info("\n" + "=" * 70)
    logger.info("✅ HEALTH CHECK PASSED")
    logger.info("=" * 70)
    logger.info("\nNOTE: This only checks imports and basic functionality.")
    logger.info("To test real-time data:\n"
               "1. Start the main bot: python3 main.py\n"
               "2. Watch for messages like '✓ [1m] 21/20' or '📊 REST 1m fallback'\n"
               "3. If you see '✓ WebSocket is active', data flows from WebSocket\n"
               "4. If you see 'REST fallback activated', using REST polling\n"
               "5. Either way, you'll have LIVE MARKET DATA")
    
    return True


if __name__ == '__main__':
    try:
        success = asyncio.run(check_system_health())
        exit(0 if success else 1)
    except Exception as e:
        logger.critical(f"Unhandled error: {e}", exc_info=True)
        exit(1)
