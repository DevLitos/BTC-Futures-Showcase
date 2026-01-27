#!/usr/bin/env python3
"""
Script para ejecutar backtest del sistema de trading
Carga datos históricos y simula la estrategia completa

Uso:
    python backtest_runner.py --symbol BTC/USD --days 30 --timeframe 1m
    python backtest_runner.py --file historical_data.csv --timeframe 1m
"""

import asyncio
import logging
import argparse
from datetime import datetime, timedelta
import pandas as pd
import sys
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('BacktestRunner')

# Import backtester
from src.application.backtest.backtest_app import Backtester, BacktestResults


class BacktestRunner:
    """Runner para ejecutar backtests"""
    
    def __init__(self, symbol: str = 'BTC/USD', 
                 initial_balance: float = 10000.0,
                 position_size_pct: float = 1.0):
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.position_size_pct = position_size_pct
        self.backtester = Backtester(
            symbol=symbol,
            initial_balance=initial_balance,
            position_size_pct=position_size_pct
        )
    
    def load_from_csv(self, filepath: str) -> bool:
        """Carga datos desde CSV"""
        try:
            df = pd.read_csv(filepath)
            logger.info(f"Loaded {len(df)} rows from {filepath}")
            
            # Validar columnas requeridas
            required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_cols):
                logger.error(f"CSV must contain columns: {required_cols}")
                return False
            
            # Detectar timeframe si es posible
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            if len(df) > 1:
                time_diff = (df['timestamp'].iloc[1] - df['timestamp'].iloc[0]).total_seconds() / 60
                if time_diff <= 2:
                    timeframe = '1m'
                elif time_diff <= 7:
                    timeframe = '5m'
                else:
                    timeframe = '1h'
                logger.info(f"Detected timeframe: {timeframe}")
            else:
                timeframe = '1m'
            
            self.backtester.load_historical_candles(df, timeframe)
            return True
        
        except Exception as e:
            logger.error(f"Error loading CSV: {e}")
            return False
    
    def load_from_ccxt(self, days: int = 30, timeframe: str = '1m') -> bool:
        """Carga datos históricos desde CCXT"""
        try:
            import ccxt
            
            # Mapear timeframe
            timeframe_map = {'1m': '1m', '5m': '5m', '1h': '1h'}
            if timeframe not in timeframe_map:
                logger.error(f"Unsupported timeframe: {timeframe}")
                return False
            
            # Inicializar exchange
            exchange = ccxt.binance({
                'enableRateLimit': True,
            })
            
            # Calcular fecha inicial
            since = exchange.parse8601((datetime.now() - timedelta(days=days)).isoformat())
            
            # Descargar candles
            logger.info(f"Downloading {days} days of {timeframe} data for {self.symbol}...")
            ohlcv = []
            limit = 1000  # Límite por request
            
            while since < exchange.milliseconds():
                chunk = exchange.fetch_ohlcv(self.symbol, timeframe, since, limit)
                if not chunk:
                    break
                ohlcv.extend(chunk)
                since = chunk[-1][0] + 1
            
            # Convertir a DataFrame
            df = pd.DataFrame(
                ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            logger.info(f"Downloaded {len(df)} {timeframe} candles")
            self.backtester.load_historical_candles(df, timeframe)
            return True
        
        except ImportError:
            logger.error("ccxt not installed. Install with: pip install ccxt")
            return False
        except Exception as e:
            logger.error(f"Error loading from CCXT: {e}")
            return False
    
    def run_backtest(self, start_date: str = None, end_date: str = None) -> BacktestResults:
        """Ejecuta el backtest"""
        start_dt = None
        end_dt = None
        
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid start_date format: {start_date} - {e}")
        
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid end_date format: {end_date} - {e}")
        
        results = self.backtester.run(start_date=start_dt, end_date=end_dt)
        return results
    
    def print_results(self, results: BacktestResults):
        """Imprime resultados formateados"""
        print(self.backtester.get_report())


def main():
    parser = argparse.ArgumentParser(
        description='Run backtest of BTC perpetual trading system',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Backtest last 30 days with 1m candles
  python backtest_runner.py --symbol BTC/USD --days 30 --timeframe 1m
  
  # Backtest from CSV file
  python backtest_runner.py --file data.csv
  
  # Backtest with custom capital
  python backtest_runner.py --symbol BTC/USD --days 7 --capital 50000
        '''
    )
    
    # Fuente de datos
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--file', help='Load data from CSV file')
    group.add_argument('--symbol', help='Symbol to backtest (e.g., BTC/USD)')
    
    # Parámetros
    parser.add_argument('--days', type=int, default=30, 
                       help='Number of days of history (default: 30)')
    parser.add_argument('--timeframe', default='1m', 
                       choices=['1m', '5m', '1h'],
                       help='Candle timeframe (default: 1m)')
    parser.add_argument('--capital', type=float, default=10000.0,
                       help='Initial capital (default: 10000)')
    parser.add_argument('--position-size', type=float, default=1.0,
                       help='Position size as % of capital (default: 1.0%)')
    parser.add_argument('--start-date', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', help='End date (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    # Create runner
    runner = BacktestRunner(
        symbol=args.symbol or 'BTC/USD',
        initial_balance=args.capital,
        position_size_pct=args.position_size
    )
    
    # Load data
    if args.file:
        if not Path(args.file).exists():
            logger.error(f"File not found: {args.file}")
            sys.exit(1)
        success = runner.load_from_csv(args.file)
    else:
        success = runner.load_from_ccxt(days=args.days, timeframe=args.timeframe)
    
    if not success:
        sys.exit(1)
    
    # Run backtest
    logger.info("Starting backtest...")
    results = runner.run_backtest(start_date=args.start_date, end_date=args.end_date)
    
    # Print results
    runner.print_results(results)
    
    # Export summary
    summary_file = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(summary_file, 'w') as f:
        f.write(runner.backtester.get_report())
    
    logger.info(f"Results saved to {summary_file}")


if __name__ == '__main__':
    main()
