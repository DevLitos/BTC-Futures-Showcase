#!/usr/bin/env python3
"""
Backtest Entrypoint

Runs backtesting against historical data.

Usage:
    python -m cmd.backtest --start 2025-01-01 --end 2025-12-31
    # or
    uv run cmd/backtest.py --config backtest_config.yaml
"""

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Ensure project root is in path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run backtest against historical data")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--config", type=str, help="Path to backtest config file")
    parser.add_argument("--symbol", type=str, default="BTC/USD:USD", help="Trading symbol")
    parser.add_argument("--output", type=str, help="Output directory for results")
    return parser.parse_args()


def main():
    """Run backtest."""
    args = parse_args()
    
    # Late import to avoid slow startup for help
    from src.application.backtest.backtest_app import BacktestEngine
    from src.config.settings import get_settings
    
    settings = get_settings()
    
    # Parse dates
    start_date = datetime.strptime(args.start, "%Y-%m-%d") if args.start else None
    end_date = datetime.strptime(args.end, "%Y-%m-%d") if args.end else None
    
    print(f"[BACKTEST] Starting backtest")
    print(f"  Symbol: {args.symbol}")
    print(f"  Period: {start_date} to {end_date}")
    
    # Run backtest
    try:
        engine = BacktestEngine(
            symbol=args.symbol,
            start_date=start_date,
            end_date=end_date,
        )
        
        if asyncio.iscoroutinefunction(engine.run):
            results = asyncio.run(engine.run())
        else:
            results = engine.run()
            
        print(f"\n[RESULTS]")
        print(f"  Total trades: {results.get('total_trades', 'N/A')}")
        print(f"  Win rate: {results.get('win_rate', 'N/A'):.1%}")
        print(f"  Net PnL: ${results.get('net_pnl', 0):.2f}")
        
    except Exception as e:
        print(f"[ERROR] Backtest failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
