#!/bin/bash
# Quick Start Script for BTC Perpetual Trading System

set -e

echo "======================================================================"
echo "  BTC PERPETUAL TRADING SYSTEM - QUICK START"
echo "======================================================================"

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python version: $PYTHON_VERSION"

# Install dependencies
echo ""
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Run integration tests
echo ""
echo "======================================================================"
echo "  RUNNING INTEGRATION TESTS"
echo "======================================================================"
python3 test_integration.py

# Check if tests passed
if [ $? -eq 0 ]; then
    echo ""
    echo "======================================================================"
    echo "  ✅ SYSTEM READY FOR DEPLOYMENT"
    echo "======================================================================"
    echo ""
    echo "Next steps:"
    echo "1. Set environment variables:"
    echo "   export KRAKEN_API_KEY=your_key"
    echo "   export KRAKEN_SECRET=your_secret"
    echo "   export BINANCE_API_KEY=your_key"
    echo "   export BINANCE_SECRET=your_secret"
    echo ""
    echo "2. Start the trading system:"
    echo "   python3 main.py"
    echo ""
    echo "3. Monitor logs:"
    echo "   tail -f logs/trading_system.log"
    echo ""
else
    echo ""
    echo "======================================================================"
    echo "  ❌ TESTS FAILED - PLEASE FIX ISSUES ABOVE"
    echo "======================================================================"
    exit 1
fi
