"""ICT Trading System - BTC Futures.

Just run: uv run main.py

Modes (set via MODE env var):
- MODE=live    : Live trading with real money (default)
- MODE=backtest: Run backtest with historical Kraken data

Strategies: FVG_TREND, IFVG, ORDER_BLOCK

Configuration (optional env vars):
- TRADING_ENABLED: true/false (default: true)
- POSITION_SIZE_BTC: position size (default: 0.001)
- LOG_INTERVAL_SECONDS: status log interval (default: 10)
- LOG_EVERY_TICK: true/false for debug (default: false)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path


def setup_logging() -> logging.Logger:
    """Configure logging to both console and unique run file."""
    # Create logs directory
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    # Create logger
    logger = logging.getLogger("trading")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # Remove any existing handlers
    
    # Format with local timezone
    log_format = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)
    logger.addHandler(console_handler)
    
    # UNIQUE file per run: trading_run_YYYYMMDD_HHMMSS_uuid.log
    run_id = uuid.uuid4().hex[:8]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"trading_run_{timestamp}_{run_id}.log"
    
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)
    
    # Also update latest.log symlink for easy access
    latest_link = logs_dir / "latest.log"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(log_file.name)
    
    logger.info("=== NEW RUN: %s ===", run_id)
    logger.info("Log file: %s", log_file)
    
    return logger


logger = setup_logging()


async def run_live() -> None:
    """Run live trading mode."""
    from src.application.runtime.connectors_runtime import Runtime
    runtime = Runtime()
    await runtime.run()


async def run_backtest() -> None:
    """Run backtest with historical data."""
    from src.application.backtest.engine import BacktestEngine, BacktestConfig
    
    days = int(os.getenv("BACKTEST_DAYS", "30"))
    
    logger.info("=" * 70)
    logger.info("  ICT TRADING SYSTEM - BACKTEST MODE")
    logger.info("=" * 70)
    logger.info("Running %d-day backtest with historical Kraken data...", days)
    logger.info("=" * 70)
    
    config = BacktestConfig(
        days=days,
        position_size_btc=float(os.getenv("POSITION_SIZE_BTC", "0.1")),
        timeframe=os.getenv("TIMEFRAME", "5m")
    )
    
    engine = BacktestEngine(config)
    result = await engine.run()
    
    # Print results
    print(result.summary())
    
    # Save detailed results
    if result.trades:
        import json
        from pathlib import Path
        
        results_dir = Path("storage/backtest")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        # Save trades
        trades_file = results_dir / f"trades_{result.start_date.strftime('%Y%m%d') if result.start_date else 'unknown'}.json"
        with open(trades_file, "w") as f:
            json.dump(result.trades, f, indent=2, default=str)
        logger.info("Trades saved to %s", trades_file)
        
        # Print per-strategy results
        strategy_stats: dict[str, list[float]] = {}
        for trade in result.trades:
            strat = trade.get("strategy", "unknown")
            if strat not in strategy_stats:
                strategy_stats[strat] = []
            strategy_stats[strat].append(trade["pnl"])
        
        print("\n=== PER-STRATEGY RESULTS ===")
        for strat, pnls in strategy_stats.items():
            wins = len([p for p in pnls if p > 0])
            total = len(pnls)
            wr = (wins / total * 100) if total > 0 else 0
            total_pnl = sum(pnls)
            print(f"{strat}: {total} trades, {wr:.1f}% WR, ${total_pnl:.2f} PnL")


async def main() -> None:
    """Main entry point."""
    mode = os.getenv("MODE", "live").lower()
    
    if mode == "backtest":
        await run_backtest()
    else:
        await run_live()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
        sys.exit(0)
