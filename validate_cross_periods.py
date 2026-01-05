#!/usr/bin/env python3
"""
Validación Cruzada Multi-Período
================================
Ejecuta backtest en múltiples periodos para validar robustez del edge.

Periodos de test:
1. Último mes (datos recientes)
2. Mes anterior (out-of-sample)  
3. Periodo de alta volatilidad (si identificable)
""" 

import asyncio
import logging
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np
import ccxt

from engine.signal_engine import SignalEngine
from engine.market_data import SynchronizedMultiTimeframeBuffer
from engine.risk_engine import RiskEngine
from engine.state_machine import BotState

logging.basicConfig(
    level=logging.WARNING,  # Reduce noise for multi-run
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('CrossValidation')

# Import from main backtest runner
from run_backtest_kraken import MockExecution, BacktestRunner, download_kraken_data


def download_kraken_data_range(symbol: str, start_date: datetime, end_date: datetime) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Download data for specific date range"""
    
    exchange = ccxt.krakenfutures({'enableRateLimit': True})
    exchange.load_markets()
    
    print(f"📥 Downloading {symbol} from {start_date.date()} to {end_date.date()}...")
    
    all_1m = []
    all_5m = []
    all_1h = []
    
    # Download in chunks
    current = start_date
    since_1m = int(start_date.timestamp() * 1000)
    since_5m = int(start_date.timestamp() * 1000)
    since_1h = int(start_date.timestamp() * 1000)
    
    end_ms = int(end_date.timestamp() * 1000)
    
    # Download 1m data
    while since_1m < end_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1m', since=since_1m, limit=1000)
            if not ohlcv:
                break
            all_1m.extend(ohlcv)
            since_1m = ohlcv[-1][0] + 60000
            if since_1m >= end_ms:
                break
        except Exception as e:
            print(f"Error 1m: {e}")
            break
    
    # Download 5m data
    while since_5m < end_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '5m', since=since_5m, limit=1000)
            if not ohlcv:
                break
            all_5m.extend(ohlcv)
            since_5m = ohlcv[-1][0] + 300000
            if since_5m >= end_ms:
                break
        except Exception as e:
            print(f"Error 5m: {e}")
            break
    
    # Download 1h data
    while since_1h < end_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', since=since_1h, limit=1000)
            if not ohlcv:
                break
            all_1h.extend(ohlcv)
            since_1h = ohlcv[-1][0] + 3600000
            if since_1h >= end_ms:
                break
        except Exception as e:
            print(f"Error 1h: {e}")
            break
    
    # Convert to DataFrames - Keep timestamp as column (BacktestRunner expects this)
    def to_df(data: list) -> pd.DataFrame:
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        # Filter to date range
        df = df[(df['timestamp'] >= start_date) & (df['timestamp'] <= end_date)]
        return df.drop_duplicates(subset=['timestamp'])
    
    df_1m = to_df(all_1m)
    df_5m = to_df(all_5m)
    df_1h = to_df(all_1h)
    
    print(f"   ✅ 1m: {len(df_1m)} candles | 5m: {len(df_5m)} candles | 1h: {len(df_1h)} candles")
    
    return df_1m, df_5m, df_1h


async def run_period_backtest(
    name: str,
    start_date: datetime,
    end_date: datetime,
    symbol: str = 'BTC/USD:USD',
    capital: float = 10000
) -> Dict:
    """Run backtest for a specific period"""
    
    print(f"\n{'='*60}")
    print(f"📊 PERIOD: {name}")
    print(f"   Range: {start_date.date()} → {end_date.date()}")
    print(f"{'='*60}")
    
    # Download data
    df_1m, df_5m, df_1h = download_kraken_data_range(symbol, start_date, end_date)
    
    if len(df_1m) < 500:
        print(f"   ⚠️ Not enough data: {len(df_1m)} candles")
        return {'name': name, 'error': 'insufficient_data'}
    
    # Run backtest
    runner = BacktestRunner(symbol=symbol, capital=capital)
    results = await runner.run(df_1m, df_5m, df_1h)
    
    results['name'] = name
    results['start_date'] = start_date.date()
    results['end_date'] = end_date.date()
    
    return results


def print_comparison(all_results: List[Dict]):
    """Print comparison table of all periods"""
    
    print("\n")
    print("=" * 80)
    print("📊 CROSS-VALIDATION RESULTS COMPARISON")
    print("=" * 80)
    
    # Header
    print(f"\n{'Period':<25} {'Trades':>8} {'WinRate':>10} {'PnL':>12} {'Return':>10} {'MaxDD':>10}")
    print("-" * 80)
    
    valid_results = [r for r in all_results if 'error' not in r]
    
    for r in valid_results:
        print(f"{r['name']:<25} {r['total_trades']:>8} {r['win_rate']:>9.1f}% ${r['total_pnl']:>10.2f} {r['return_pct']:>9.2f}% {r['max_drawdown']:>9.2f}%")
    
    print("-" * 80)
    
    # Aggregates
    if valid_results:
        avg_trades = sum(r['total_trades'] for r in valid_results) / len(valid_results)
        avg_wr = sum(r['win_rate'] for r in valid_results) / len(valid_results)
        total_pnl = sum(r['total_pnl'] for r in valid_results)
        avg_return = sum(r['return_pct'] for r in valid_results) / len(valid_results)
        max_dd = max(r['max_drawdown'] for r in valid_results)
        
        print(f"{'AVERAGE/TOTAL':<25} {avg_trades:>8.0f} {avg_wr:>9.1f}% ${total_pnl:>10.2f} {avg_return:>9.2f}% {max_dd:>9.2f}%")
    
    # Consistency check
    print("\n" + "=" * 80)
    print("🔍 CONSISTENCY ANALYSIS")
    print("=" * 80)
    
    profitable_periods = sum(1 for r in valid_results if r['total_pnl'] > 0)
    print(f"\n✅ Profitable periods: {profitable_periods}/{len(valid_results)}")
    
    if all(r['total_pnl'] > 0 for r in valid_results):
        print("🎯 EDGE CONFIRMED: Positive in ALL test periods")
    elif profitable_periods >= len(valid_results) * 0.66:
        print("⚡ EDGE LIKELY: Profitable in majority of periods")
    else:
        print("⚠️ EDGE WEAK: Review strategy parameters")
    
    # Win rate consistency
    wr_std = np.std([r['win_rate'] for r in valid_results])
    print(f"📏 Win Rate StdDev: {wr_std:.2f}% (lower = more consistent)")
    
    # Drawdown consistency  
    dd_values = [r['max_drawdown'] for r in valid_results]
    print(f"📉 Max DD range: {min(dd_values):.2f}% - {max(dd_values):.2f}%")


async def main():
    now = datetime.now(UTC)
    
    # Define test periods
    periods = [
        # Period 1: Most recent 7 days (the default we tested)
        ("Last 7 days (baseline)", now - timedelta(days=7), now),
        
        # Period 2: Previous week (out-of-sample)
        ("Previous week", now - timedelta(days=14), now - timedelta(days=7)),
        
        # Period 3: 3 weeks ago
        ("3 weeks ago", now - timedelta(days=21), now - timedelta(days=14)),
        
        # Period 4: Month ago (different market regime?)
        ("1 month ago", now - timedelta(days=35), now - timedelta(days=28)),
    ]
    
    all_results = []
    
    for name, start, end in periods:
        try:
            result = await run_period_backtest(name, start, end)
            all_results.append(result)
        except Exception as e:
            print(f"❌ Error in {name}: {e}")
            all_results.append({'name': name, 'error': str(e)})
    
    # Print comparison
    print_comparison(all_results)


if __name__ == '__main__':
    asyncio.run(main())
