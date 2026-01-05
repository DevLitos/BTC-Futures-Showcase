#!/usr/bin/env python3
"""
Professional Backtest Runner - REAL STRATEGY
Ejecuta backtest con la estrategia REAL de signal_engine.py
"""

import logging
from datetime import datetime, timedelta
import numpy as np

from engine.backtest_professional import ProfessionalBacktester

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_synthetic_data(
    start_price: float = 40000.0,
    num_candles: int = 1000,
    volatility: float = 0.02,
    trend: float = 0.0001,
    pattern: str = 'random'  # 'random', 'strong_uptrend', 'strong_downtrend', 'ranging'
):
    """Genera datos sintéticos con patrones definidos"""
    import pandas as pd
    
    logger.info(f"Generating {num_candles} synthetic candles (pattern={pattern})...")
    
    timestamps = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []
    
    current_time = datetime.now() - timedelta(minutes=num_candles)
    current_price = start_price
    
    for i in range(num_candles):
        # Aplicar patrón
        if pattern == 'strong_uptrend':
            # Uptrend fuerte: cada 5 candles, subida de 0.5%
            base_trend = 0.0005 if (i % 5) == 0 else 0.00005
            daily_return = np.random.normal(base_trend, volatility * 0.5)
        elif pattern == 'strong_downtrend':
            # Downtrend fuerte
            base_trend = -0.0005 if (i % 5) == 0 else -0.00005
            daily_return = np.random.normal(base_trend, volatility * 0.5)
        elif pattern == 'ranging':
            # Rango lateral
            daily_return = np.random.normal(0, volatility * 0.3)
        else:
            # Random con trend
            daily_return = np.random.normal(trend, volatility)
        
        next_price = current_price * (1 + daily_return)
        
        # OHLC con patrón realista
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
    logger.info(f"Price range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    return df


def run_professional_backtest():
    """Ejecuta backtest profesional con estrategia real"""
    
    logger.info("="*70)
    logger.info("PROFESSIONAL BACKTEST - REAL STRATEGY")
    logger.info("="*70)
    
    # Generar datos sintéticos con uptrend
    df = generate_synthetic_data(
        start_price=40000.0,
        num_candles=2000,
        volatility=0.015,
        trend=0.0002  # Trend alcista
    )
    
    # Crear backtester profesional
    bt = ProfessionalBacktester(
        symbol='BTC/USD',
        initial_balance=10000.0,
        position_size_pct=1.0
    )
    
    # Cargar datos
    bt.load_historical_candles(df, timeframe='1m')
    
    # Ejecutar backtest
    results = bt.run()
    
    # Reporte
    print(bt.get_report())
    
    return results


def run_multiple_scenarios():
    """Ejecuta backtest en múltiples escenarios"""
    
    logger.info("="*70)
    logger.info("RUNNING MULTIPLE SCENARIOS WITH DEFINED PATTERNS")
    logger.info("="*70)
    
    scenarios = [
        {'name': 'Strong Uptrend', 'pattern': 'strong_uptrend', 'vol': 0.015},
        {'name': 'Strong Downtrend', 'pattern': 'strong_downtrend', 'vol': 0.015},
        {'name': 'Ranging Market', 'pattern': 'ranging', 'vol': 0.012},
    ]
    
    all_results = {}
    
    for scenario in scenarios:
        logger.info(f"\n{'='*70}")
        logger.info(f"Scenario: {scenario['name']}")
        logger.info(f"{'='*70}")
        
        df = generate_synthetic_data(
            start_price=40000.0,
            num_candles=3000,
            volatility=scenario['vol'],
            pattern=scenario['pattern']
        )
        
        bt = ProfessionalBacktester(
            symbol='BTC/USD',
            initial_balance=10000.0,
            position_size_pct=1.0
        )
        
        bt.load_historical_candles(df, timeframe='1m')
        results = bt.run()
        
        all_results[scenario['name']] = results
        
        print(bt.get_report())
    
    # Summary
    logger.info("\n" + "="*70)
    logger.info("SUMMARY")
    logger.info("="*70)
    
    for name, results in all_results.items():
        print(f"\n{name}:")
        print(f"  Total Trades: {results.total_trades}")
        print(f"  Win Rate: {results.win_rate:.2f}%")
        print(f"  Total P&L: ${results.total_pnl:.2f}")
        print(f"  Sharpe Ratio: {results.sharpe_ratio:.2f}")
        print(f"  Max Drawdown: {results.max_drawdown_pct:.2f}%")
        print(f"  Profit Factor: {results.profit_factor:.2f}")


if __name__ == '__main__':
    # Run multiple scenarios
    run_multiple_scenarios()
    
    logger.info("\n✅ Professional backtesting completed!")
