#!/usr/bin/env python3
"""
Live Trading Entrypoint

This is a thin wrapper that delegates to the actual application logic.
NO business logic should be here - only composition and startup.

Usage:
    python -m cmd.live
    # or
    uv run cmd/live.py
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is in path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    """Start live trading system."""
    # Import main module (which handles everything)
    from main import main as run_main
    
    try:
        asyncio.run(run_main())
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] Fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
