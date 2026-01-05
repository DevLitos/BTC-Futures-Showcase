#!/usr/bin/env python3
"""
Ejemplo de backtest sin dependencias externas
Demuestra el funcionamiento del sistema
"""

import sys
from datetime import datetime, timedelta
from typing import List, Dict
import logging

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_csv_data(
    filename: str = '/tmp/btc_backtest_data.csv',
    num_candles: int = 100,
    start_price: float = 40000.0,
    volatility: float = 0.02,
    trend: float = 0.0001
):
    """Genera archivo CSV con datos sintéticos"""
    logger.info(f"Generating {num_candles} synthetic candles...")
    
    import random
    
    current_time = datetime.now() - timedelta(minutes=num_candles)
    current_price = start_price
    
    csv_lines = ['timestamp,open,high,low,close,volume']
    
    for i in range(num_candles):
        # Random walk
        daily_return = random.gauss(trend, volatility)
        next_price = current_price * (1 + daily_return)
        
        # OHLC
        price_range = abs(next_price - current_price) * 0.5
        open_price = current_price
        close_price = next_price
        high_price = max(open_price, close_price) + random.uniform(0, price_range)
        low_price = min(open_price, close_price) - random.uniform(0, price_range)
        volume = random.uniform(100, 1000)
        
        # Escribir CSV
        csv_line = f"{current_time.isoformat()},{open_price:.2f},{high_price:.2f},{low_price:.2f},{close_price:.2f},{volume:.0f}"
        csv_lines.append(csv_line)
        
        current_time += timedelta(minutes=1)
        current_price = close_price
    
    # Guardar archivo
    with open(filename, 'w') as f:
        f.write('\n'.join(csv_lines))
    
    logger.info(f"CSV saved to {filename}")
    return filename


def test_backtest_module():
    """Test que el módulo de backtest puede ser importado"""
    logger.info("\n" + "="*70)
    logger.info("Testing Backtest Module Import")
    logger.info("="*70)
    
    try:
        from engine.backtest import Backtester, BacktestResults, BacktestTrade
        logger.info("✅ Successfully imported: Backtester")
        logger.info("✅ Successfully imported: BacktestResults")
        logger.info("✅ Successfully imported: BacktestTrade")
        
        # Test instantiation
        bt = Backtester(symbol='BTC/USD', initial_balance=10000.0)
        logger.info(f"✅ Created Backtester instance: {bt}")
        
        return True
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_backtest_with_csv():
    """Test backtest con datos CSV"""
    logger.info("\n" + "="*70)
    logger.info("Testing Backtest with CSV Data")
    logger.info("="*70)
    
    try:
        # Generar datos CSV
        csv_file = generate_csv_data(num_candles=100)
        
        # Importar y crear backtester
        from engine.backtest import Backtester
        
        # Parse CSV manualmente (sin pandas)
        data = []
        with open(csv_file, 'r') as f:
            lines = f.readlines()[1:]  # Skip header
            for line in lines:
                parts = line.strip().split(',')
                data.append({
                    'timestamp': datetime.fromisoformat(parts[0]),
                    'open': float(parts[1]),
                    'high': float(parts[2]),
                    'low': float(parts[3]),
                    'close': float(parts[4]),
                    'volume': float(parts[5]),
                })
        
        logger.info(f"Loaded {len(data)} candles from CSV")
        
        # Crear backtester
        bt = Backtester(symbol='BTC/USD', initial_balance=10000.0)
        
        # Cargar datos
        for candle in data:
            bt.candles_1m.append(candle)
        
        logger.info(f"✅ Loaded {len(bt.candles_1m)} candles into backtester")
        
        # Ejecutar backtest
        logger.info("Running backtest...")
        results = bt.run()
        
        logger.info(f"✅ Backtest completed")
        logger.info(f"  Total trades: {results.total_trades}")
        logger.info(f"  Winning trades: {results.winning_trades}")
        logger.info(f"  Losing trades: {results.losing_trades}")
        
        if results.total_trades > 0:
            logger.info(f"  Win rate: {results.win_rate:.2f}%")
            logger.info(f"  Total P&L: ${results.total_pnl:.2f}")
            logger.info(f"  Sharpe Ratio: {results.sharpe_ratio:.2f}")
            logger.info(f"  Max Drawdown: {results.max_drawdown_pct:.2f}%")
        
        print(bt.get_report())
        
        return True
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_backtest_classes():
    """Test las clases del backtest"""
    logger.info("\n" + "="*70)
    logger.info("Testing Backtest Classes")
    logger.info("="*70)
    
    try:
        from engine.backtest import BacktestTrade, BacktestResults
        
        # Create test trades
        trade1 = BacktestTrade(
            entry_time=datetime.now(),
            entry_price=40000.0,
            direction='LONG',
            size=1.0,
            stop_loss=39000.0
        )
        trade1.exit_time = datetime.now() + timedelta(minutes=10)
        trade1.exit_price = 41000.0
        trade1.exit_reason = 'TP'
        
        logger.info(f"✅ Created trade: {trade1.direction} @ {trade1.entry_price}")
        logger.info(f"   Exit: {trade1.exit_price} ({trade1.exit_reason})")
        logger.info(f"   P&L: ${trade1.pnl:.2f} ({trade1.pnl_pct:.2f}%)")
        
        # Test results
        results = BacktestResults([trade1])
        logger.info(f"✅ Created results")
        logger.info(f"   Total trades: {results.total_trades}")
        logger.info(f"   Win rate: {results.win_rate:.2f}%")
        
        return True
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Ejecuta todos los tests"""
    logger.info("\n" + "="*70)
    logger.info("BACKTEST MODULE VALIDATION")
    logger.info("="*70)
    
    tests = [
        ("Module Import", test_backtest_module),
        ("Classes", test_backtest_classes),
        ("CSV Data", test_backtest_with_csv),
    ]
    
    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            logger.error(f"❌ Test '{name}' failed: {e}")
            results[name] = False
    
    # Summary
    logger.info("\n" + "="*70)
    logger.info("TEST SUMMARY")
    logger.info("="*70)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, passed_test in results.items():
        status = "✅ PASS" if passed_test else "❌ FAIL"
        logger.info(f"{name:.<40} {status}")
    
    logger.info("="*70)
    logger.info(f"Total: {passed}/{total} tests passed")
    
    if passed == total:
        logger.info("\n✅ ALL TESTS PASSED!")
        return 0
    else:
        logger.error(f"\n❌ {total - passed} tests failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
