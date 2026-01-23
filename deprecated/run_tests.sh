#!/bin/bash
# ============================================================================
# SCRIPT TO RUN COMPREHENSIVE TEST SUITE
# ============================================================================
# 
# Purpose: Execute all tests for Edge Definition & Operations System
# Location: /home/carlo/projects/trading/daytrader2/
# 
# Usage:
#   ./run_tests.sh              # Run all tests
#   ./run_tests.sh --verbose    # Run with verbose output
#   ./run_tests.sh --coverage   # Run with coverage report
#   ./run_tests.sh --html       # Run and generate HTML report
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="/home/carlo/projects/trading/daytrader2"
TEST_FILE="test_edge_system.py"
LOG_FILE="test_results.log"
COVERAGE_DIR="htmlcov"

# Change to project directory
cd "$PROJECT_DIR"

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  EDGE DEFINITION & OPERATIONS SYSTEM - TEST SUITE${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""

# Step 1: Check if pytest is installed
echo -e "${YELLOW}[1/5] Checking dependencies...${NC}"
if ! command -v pytest &> /dev/null; then
    echo -e "${YELLOW}Installing pytest...${NC}"
    pip install pytest pytest-cov -q
fi
echo -e "${GREEN}✓ pytest is installed${NC}"
echo ""

# Step 2: Verify project structure
echo -e "${YELLOW}[2/5] Verifying project structure...${NC}"
if [ ! -f "$TEST_FILE" ]; then
    echo -e "${RED}✗ Test file not found: $TEST_FILE${NC}"
    exit 1
fi

if [ ! -d "engine" ]; then
    echo -e "${RED}✗ engine/ directory not found${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Project structure verified${NC}"
echo ""

# Step 3: Run tests
echo -e "${YELLOW}[3/5] Running test suite...${NC}"
echo "Test file: $TEST_FILE"
echo "Location: $PROJECT_DIR"
echo ""

# Determine test options
TEST_OPTIONS="-v"

if [[ "$1" == "--coverage" ]] || [[ "$1" == "--html" ]]; then
    TEST_OPTIONS="$TEST_OPTIONS --cov=engine --cov-report=term-missing"
fi

if [[ "$1" == "--html" ]]; then
    TEST_OPTIONS="$TEST_OPTIONS --cov-report=html"
fi

# Run pytest
if pytest "$TEST_FILE" $TEST_OPTIONS 2>&1 | tee "$LOG_FILE"; then
    TESTS_PASSED=true
else
    TESTS_PASSED=false
fi

echo ""

# Step 4: Process results
echo -e "${YELLOW}[4/5] Processing test results...${NC}"
echo ""

if [ "$TESTS_PASSED" = true ]; then
    echo -e "${GREEN}✓ ALL TESTS PASSED${NC}"
    
    # Extract summary from log
    SUMMARY=$(grep -E "passed|failed|error" "$LOG_FILE" | tail -1)
    echo "Summary: $SUMMARY"
    echo ""
    
    # Show coverage if requested
    if [[ "$1" == "--html" ]]; then
        echo -e "${GREEN}✓ HTML coverage report generated${NC}"
        echo "Open with: open $PROJECT_DIR/$COVERAGE_DIR/index.html"
    fi
else
    echo -e "${RED}✗ SOME TESTS FAILED${NC}"
    echo ""
    echo "Failed tests (check output above for details)"
    echo ""
fi

# Step 5: Summary
echo -e "${YELLOW}[5/5] Test execution complete${NC}"
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  SUMMARY${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "Test file:     $TEST_FILE"
echo "Log file:      $LOG_FILE"
if [[ "$1" == "--html" ]]; then
    echo "Coverage:      $COVERAGE_DIR/"
fi
echo ""

if [ "$TESTS_PASSED" = true ]; then
    echo -e "${GREEN}Status: READY FOR DEPLOYMENT${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Review test results above"
    echo "  2. Integrate EdgeEnforcement into execution.py"
    echo "  3. Configure thresholds in config.py"
    echo "  4. Deploy to staging environment"
    exit 0
else
    echo -e "${RED}Status: TESTS FAILED - FIX BEFORE DEPLOYMENT${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Review test failures above"
    echo "  2. Check implementation files in engine/"
    echo "  3. Run tests again to verify fixes"
    exit 1
fi
