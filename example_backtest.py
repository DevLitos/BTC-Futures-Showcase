#!/usr/bin/env python3
"""
Ejemplo de backtest con datos sintéticos
Genera datos OHLCV realistas y ejecuta el backtest
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging

from engine.backtest import Backtester, BacktestResults

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_synthetic_data(
    start_price: float = 40000.0,
    num_candles: int = 1000,
    volatility: float = 0.02,
    trend: float = 0.0001
) -> pd.DataFrame:
    """
    Genera datos OHLCV sintéticos realistas
    
    Args:
        start_price: Precio inicial
        num_candles: Número de velas a generar
        volatility: Volatilidad diaria (2% por defecto)
        trend: Trend diario (0.01% por defecto)
    
    Returns:
        DataFrame con [timestamp, open, high, low, close, volume]
    """
    logger.info(f"Generating {num_candles} synthetic candles...")
    
    timestamps = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []
    
    current_time = datetime.now() - timedelta(minutes=num_candles)
    current_price = start_price
    
    for i in range(num_candles):
        # Random walk con trend
        daily_return = np.random.normal(trend, volatility)
        next_price = current_price * (1 + daily_return)
        
        # Generar OHLC
        price_range = abs(next_price - current_price) * 0.5
        open_price = current_price
        close_price = next_price
        high_price = max(open_price, close_price) + np.random.uniform(0, price_range)
        low_price = min(open_price, close_price) - np.random.uniform(0, price_range)
        volume = np.random.uniform(100, 1000)
        
        timestamps.append(current_time)
        opens.append(open_price)
        highs.append(high_price)
        lows.append(low_price)
        closes.append(close_price)
        volumes.append(volume)
        
        # Siguiente iteración
        current_time += timedelta(minutes=1)
        current_price = close_price
    
    df = pd.DataFrame({
        'timestamp': timestamps,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })
    
    logger.info(f"Generated data from {df['timestamp'].min()} to {df['timestamp'].max()}")
    return df


def example_simple_trend():
    """Ejemplo simple: backtest con datos trending"""
    logger.info("=" * 70)
    logger.info("EXAMPLE 1: Simple Trend Backtest")
    logger.info("=" * 70)
    
    # Generar datos con trend alcista
    df = generate_synthetic_data(
        start_price=40000.0,
        num_candles=500,
        volatility=0.01,
        trend=0.0003  # Trend alcista
    )
    
    # Crear backtester
    bt = Backtester(
        symbol='BTC/USD',
        initial_balance=10000.0,
        position_size_pct=1.0
    )
    
    # Cargar datos
    bt.load_historical_candles(df, timeframe='1m')
    
    # Ejecutar backtest
    results = bt.run()
    
    # Imprimir reporte
    print(bt.get_report())
    
    return results


def example_volatile_market():
    """Ejemplo 2: Mercado muy volátil"""
    logger.info("\n" + "=" * 70)
    logger.info("EXAMPLE 2: Volatile Market Backtest")
    logger.info("=" * 70)
    
    # Generar datos muy volátiles
    df = generate_synthetic_data(
        start_price=40000.0,
        num_candles=1000,
        volatility=0.05,  # 5% volatilidad
        trend=0.0  # Sin trend
    )
    
    bt = Backtester(
        symbol='BTC/USD',
        initial_balance=10000.0,
        position_size_pct=0.5  # Posiciones más pequeñas
    )
    
    bt.load_historical_candles(df, timeframe='1m')
    results = bt.run()
    
    print(bt.get_report())
    
    return results


def example_downtrend():
    """Ejemplo 3: Trend bajista (debe perder dinero)"""
    logger.info("\n" + "=" * 70)
    logger.info("EXAMPLE 3: Downtrend Market")
    logger.info("=" * 70)
    
    # Generar datos con trend bajista
    df = generate_synthetic_data(
        start_price=40000.0,
        num_candles=500,
        volatility=0.02,
        trend=-0.0003  # Trend bajista
    )
    
    bt = Backtester(
        symbol='BTC/USD',
        initial_balance=10000.0,
        position_size_pct=1.0
    )
    
    bt.load_historical_candles(df, timeframe='1m')
    results = bt.run()
    
    print(bt.get_report())
    
    return results


def compare_results(examples: dict):
    """Compara resultados entre ejemplos"""
    logger.info("\n" + "=" * 70)
    logger.info("COMPARISON SUMMARY")
    logger.info("=" * 70)
    
    for name, results in examples.items():
        print(f"\n{name}:")
        print(f"  Total Trades: {results.total_trades}")
        print(f"  Win Rate: {results.win_rate:.2f}%")
        print(f"  Total P&L: ${results.total_pnl:.2f}")
        print(f"  Sharpe Ratio: {results.sharpe_ratio:.2f}")
        print(f"  Max Drawdown: {results.max_drawdown_pct:.2f}%")
        print(f"  Profit Factor: {results.profit_factor:.2f}")


def main():
    """Ejecuta todos los ejemplos"""
    
    examples = {}
    
    # Ejemplo 1
    try:
        results = example_simple_trend()
        examples['Uptrend'] = results
    except Exception as e:
        logger.error(f"Error in example 1: {e}")
    
    # Ejemplo 2
    try:
        results = example_volatile_market()
        examples['Volatile'] = results
    except Exception as e:
        logger.error(f"Error in example 2: {e}")
    
    # Ejemplo 3
    try:
        results = example_downtrend()
        examples['Downtrend'] = results
    except Exception as e:
        logger.error(f"Error in example 3: {e}")
    
    # Comparar
    if examples:
        compare_results(examples)
    
    logger.info("\n✅ Backtesting examples completed!")


if __name__ == '__main__':
    main()
