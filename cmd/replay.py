#!/usr/bin/env python3
"""
Replay Entrypoint

Replays historical data through the live trading system for validation.
This is different from backtest - it runs real-time simulation.

Usage:
    python -m cmd.replay --date 2025-01-15 --speed 10
    # or
    uv run cmd/replay.py --file historical_data.parquet
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
    parser = argparse.ArgumentParser(description="Replay historical data through live system")
    parser.add_argument("--date", type=str, help="Date to replay (YYYY-MM-DD)")
    parser.add_argument("--file", type=str, help="Path to historical data file")
    parser.add_argument("--speed", type=float, default=1.0, help="Replay speed multiplier (e.g., 10 = 10x faster)")
    parser.add_argument("--pause-on-trade", action="store_true", help="Pause when trade is detected")
    return parser.parse_args()


def main():
    """Run replay."""
    args = parse_args()
    
    # Late import to avoid slow startup for help
    from src.application.backtest.walk_forward import WalkForwardValidator
    from src.config.settings import get_settings
    
    settings = get_settings()
    
    print(f"[REPLAY] Starting historical replay")
    print(f"  Speed: {args.speed}x")
    
    if args.date:
        replay_date = datetime.strptime(args.date, "%Y-%m-%d")
        print(f"  Date: {replay_date.date()}")
    elif args.file:
        print(f"  File: {args.file}")
    else:
        print("[ERROR] Either --date or --file required")
        sys.exit(1)
    
    try:
        # TODO: Implement replay logic using WalkForwardValidator
        # For now, this is a placeholder
        print("[INFO] Replay mode not yet fully implemented")
        print("[INFO] Use backtest.py for historical analysis")
        
    except Exception as e:
        print(f"[ERROR] Replay failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
