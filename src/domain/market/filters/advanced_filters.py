"""
Advanced Filters for Tier 2-3 Optimizations (FIX #6-13)
Includes: Funding rate, VPOC, divergence, BTC correlation, session quality
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, UTC
from typing import Tuple, Optional

from src.config.trading_config import (
    FundingRateControl, VPOCFilters, DivergenceFilters,
    CorrelationFilters, SessionQuality, TimeBasedExits
)
from src.shared.indicators import calculate_rsi, calculate_ema

logger = logging.getLogger(__name__)


# ============================================================================
# FIX #6: FUNDING RATE CONTROL
# ============================================================================

async def check_funding_rate_safe(external_data_manager, symbol: str | None, bias: str | None) -> Tuple[bool, str]:
    """
    FIX #6: Check if funding rate is safe for entry
    
    Returns: (is_safe: bool, reason: str)
    """
    if not FundingRateControl.ENABLED:
        return True, ""
    
    if not symbol:
        return True, ""  # Can't check without symbol
    
    try:
        current_funding = await external_data_manager.get_funding_rate(symbol)
        
        # Only check for LONG entries (SHORT benefits from high funding)
        if bias == 'LONG':
            if current_funding > FundingRateControl.MAX_FUNDING_RATE_PCT / 100:
                return False, f"Funding too high: {current_funding*100:.4f}% > {FundingRateControl.MAX_FUNDING_RATE_PCT:.4f}%"
        
        return True, ""
        
    except Exception as e:
        logger.warning(f"Failed to check funding rate: {e}")
        return True, ""  # If we can't check, don't block entry


async def should_exit_on_funding(
    external_data_manager, 
    symbol: str | None, 
    bias: str,
    unrealized_pnl: float = 0.0,  # Pass current PnL
    time_to_funding_min: float | None = None  # Minutes until next funding settlement
) -> Tuple[bool, str]:
    """
    FIX #6: Force exit if funding becomes EXTREMELY dangerous
    
    IMPROVED (Jan 12, 2026):
    - Uses REAL TIME until next funding settlement (not arbitrary position age)
    - Only exit on EXTREME funding (>0.20%)
    - Never exit positions in profit from funding alone
    - Only consider exiting if funding settlement is imminent (<30 min)
    
    Logic:
    - If time_to_funding < 30 min AND funding extreme AND position losing → EXIT
    - Otherwise: hold (funding is just a cost, not immediate danger)
    
    Returns: (should_exit: bool, reason: str)
    """
    if not FundingRateControl.ENABLED or bias != 'LONG':
        return False, ""
    
    try:
        current_funding = await external_data_manager.get_funding_rate(symbol)
        funding_pct = current_funding * 100  # Convert to percentage for display
        threshold_pct = FundingRateControl.FORCE_EXIT_FUNDING_PCT
        
        # Get time to next funding if not provided
        if time_to_funding_min is None:
            try:
                time_to_funding_min = external_data_manager.funding.get_time_to_next_funding_minutes()
            except (AttributeError, TypeError, ValueError) as e:
                logger.debug(f"Could not get funding time: {e}")
                time_to_funding_min = 60.0  # Default: assume not imminent
        
        # GATE 1: Funding must exceed threshold
        if current_funding <= threshold_pct / 100:
            return False, ""
        
        # PROTECTION 1: Never close positions in profit due to funding
        if unrealized_pnl > 0:
            logger.debug(f"Funding extreme ({funding_pct:.4f}%) but position in profit ({unrealized_pnl:.2f}) - holding")
            return False, ""
        
        # PROTECTION 2: Only exit if funding settlement is imminent (<30 min)
        # If settlement is far away, we might recover the loss before paying
        if time_to_funding_min is None:
            time_to_funding_min = 60.0
        if time_to_funding_min > 30.0:
            logger.debug(f"Funding extreme ({funding_pct:.4f}%) but settlement in {time_to_funding_min:.0f}min - holding")
            return False, ""
        
        # EXIT: Extreme funding + losing position + settlement imminent
        return True, f"FUNDING EXIT: {funding_pct:.4f}% > {threshold_pct:.4f}%, PnL={unrealized_pnl:.2f}, settlement in {time_to_funding_min:.0f}min"
        
    except Exception as e:
        logger.warning(f"Failed to check exit funding: {e}")
        return False, ""


# ============================================================================
# FIX #9: SESSION QUALITY FILTER
# ============================================================================

def get_session_name_utc(hour_utc: int) -> str:
    """Get session name based on UTC hour
    
    Sessions (non-overlapping):
    - Asia: 23:00-08:00 UTC (Tokyo/HK)
    - London: 08:00-14:00 UTC (before NY opens)
    - New York: 14:00-23:00 UTC (US session)
    """
    if 14 <= hour_utc < 23:
        return 'newyork'
    elif 8 <= hour_utc < 14:
        return 'london'
    elif 23 <= hour_utc or hour_utc < 8:
        return 'asia'
    else:
        return 'other'


def is_weekend(hour_utc: int, day_of_week: int) -> bool:
    """Check if current time is weekend (Friday 5pm+ UTC)"""
    if day_of_week == 4 and hour_utc >= 17:  # Friday 5pm+
        return True
    if day_of_week >= 5:  # Saturday, Sunday
        return True
    return False


def check_session_quality(adx: float, volatility_bps: float, market_time: datetime | None = None) -> Tuple[bool, str]:
    """
    FIX #9: Check if current session has adequate quality
    
    Args:
        adx: ADX value (1H)
        volatility_bps: Volatility in basis points (high - low) / close * 10000
        market_time: Current market time (REQUIRED for backtest - prevents lookahead bias)
    
    Returns: (is_quality_ok: bool, reason: str)
    """
    if not SessionQuality.ENABLED:
        return True, ""
    
    # 🔒 FIX: Use market_time to prevent lookahead bias in backtest
    if market_time is None:
        from src.shared.time_provider import current_time
        market_time = current_time()
    
    current_hour = market_time.hour
    current_day = market_time.weekday()
    
    # Check weekend
    if SessionQuality.WEEKEND_TRADING == False and is_weekend(current_hour, current_day):
        return False, f"Weekend trading disabled ({current_day}-{current_hour}:00)"
    
    # Get session and requirements
    session = get_session_name_utc(current_hour)
    
    if session not in SessionQuality.MIN_ADX_BY_SESSION:
        return False, f"Unknown session: {session}"
    
    min_adx = SessionQuality.MIN_ADX_BY_SESSION[session]
    min_vol = SessionQuality.MIN_VOLATILITY_BPS[session]
    
    # Check ADX
    if adx < min_adx:
        return False, f"ADX {adx:.1f} < {min_adx} ({session})"
    
    # Check volatility
    if volatility_bps < min_vol:
        return False, f"Volatility {volatility_bps:.0f} bps < {min_vol} ({session})"
    
    return True, f"Session {session} OK: ADX {adx:.1f}, Vol {volatility_bps:.0f} bps"


# ============================================================================
# FIX #10: HTF DIVERGENCE DETECTION
# ============================================================================

def check_htf_divergence(df_1h: pd.DataFrame, bias: str | None) -> Tuple[bool, str]:
    """
    FIX #10: Detect bearish (for LONG) or bullish (for SHORT) divergence
    
    Divergence = New high/low in price but RSI doesn't confirm
    This is HIGH RISK for reversals
    
    Returns: (divergence_detected: bool, reason: str)
    """
    if not DivergenceFilters.ENABLED or len(df_1h) < 30 or not bias:
        return False, ""
    
    try:
        closes = df_1h['close'].to_numpy()
        highs = df_1h['high'].to_numpy()
        lows = df_1h['low'].to_numpy()
        
        rsi = calculate_rsi(closes, DivergenceFilters.RSI_PERIOD)
        lookback = DivergenceFilters.LOOKBACK_PERIODS
        
        if bias == 'LONG':
            # Bearish divergence: price makes new high but RSI doesn't
            current_high = highs[-1]
            prev_high = float(np.amax(np.asarray(highs[-lookback:-1])))
            
            current_rsi = rsi[-1]
            prev_rsi = float(np.amax(np.asarray(rsi[-lookback:-1])))
            
            if current_high > prev_high and current_rsi < prev_rsi:
                return True, f"Bearish divergence: Price high {current_high:.2f} > {prev_high:.2f} but RSI {current_rsi:.1f} < {prev_rsi:.1f}"
        
        else:  # SHORT
            # Bullish divergence: price makes new low but RSI doesn't
            current_low = lows[-1]
            prev_low = float(np.amin(np.asarray(lows[-lookback:-1])))
            
            current_rsi = rsi[-1]
            prev_rsi = float(np.amin(np.asarray(rsi[-lookback:-1])))
            
            if current_low < prev_low and current_rsi > prev_rsi:
                return True, f"Bullish divergence: Price low {current_low:.2f} < {prev_low:.2f} but RSI {current_rsi:.1f} > {prev_rsi:.1f}"
        
        return False, ""
        
    except Exception as e:
        logger.warning(f"Failed to check divergence: {e}")
        return False, ""


# ============================================================================
# FIX #11: VPOC (VOLUME POINT OF CONTROL)
# ============================================================================

def calculate_vpoc(df_1h: pd.DataFrame) -> Optional[float]:
    """
    FIX #11: Calculate VPOC (Volume Point of Control)
    
    VPOC = Price level where most volume was traded
    
    Returns: VPOC price or None if calculation fails
    """
    if not VPOCFilters.ENABLED or len(df_1h) < VPOCFilters.VPOC_LOOKBACK:
        return None
    
    try:
        df = df_1h.tail(VPOCFilters.VPOC_LOOKBACK)
        
        low = df['low'].min()
        high = df['high'].max()
        
        # Create price bins
        bins = np.arange(low, high + VPOCFilters.PRICE_BIN_SIZE, VPOCFilters.PRICE_BIN_SIZE)
        
        volume_by_price = np.zeros(len(bins) - 1)
        
        # Count volume in each bin
        for i in range(len(bins) - 1):
            mask = (df['high'] > bins[i]) & (df['low'] <= bins[i+1])
            volume_by_price[i] = df.loc[mask, 'volume'].sum()
        
        vpoc_idx = np.argmax(volume_by_price)
        vpoc_price = (bins[vpoc_idx] + bins[vpoc_idx + 1]) / 2
        
        return vpoc_price
        
    except Exception as e:
        logger.warning(f"Failed to calculate VPOC: {e}")
        return None


def check_vpoc_validity(current_price: float, vpoc: float | None, bias: str | None) -> Tuple[bool, str]:
    """
    FIX #11: Check if entry price is valid relative to VPOC
    
    Returns: (is_valid: bool, reason: str)
    """
    if not VPOCFilters.ENABLED or vpoc is None or not bias:
        return True, ""
    
    if bias == 'LONG':
        # LONG above VPOC is risky - should support below
        if current_price > vpoc:
            distance_to_vpoc = current_price - vpoc
            if distance_to_vpoc > (vpoc * 0.02):  # >2% above
                return False, f"LONG {current_price:.2f} too far above VPOC {vpoc:.2f} - no support"
        return True, f"Entry below/near VPOC {vpoc:.2f} ✓"
    
    else:  # SHORT
        # SHORT below VPOC is risky - should resistance above
        if current_price < vpoc:
            distance_to_vpoc = vpoc - current_price
            if distance_to_vpoc > (vpoc * 0.02):  # >2% below
                return False, f"SHORT {current_price:.2f} too far below VPOC {vpoc:.2f} - no resistance"
        return True, f"Entry above/near VPOC {vpoc:.2f} ✓"


# ============================================================================
# FIX #12: BTC CORRELATION CHECK
# ============================================================================

async def check_btc_trend(external_data_manager, bias: str | None) -> Tuple[bool, str]:
    """
    FIX #12: Check BTC trend - don't trade LONG if BTC is DOWN
    
    BTC and altcoin correlation is 0.95+ (very correlated)
    
    NOTE: This function requires the external_data_manager to have a method
    that can fetch OHLCV data. If not available, it returns True (allow).
    
    Returns: (is_ok_to_trade: bool, reason: str)
    """
    if not CorrelationFilters.ENABLED or not CorrelationFilters.BTC_TREND_ENABLED:
        return True, ""
    
    try:
        # Check if the manager has get_ohlcv method
        if not hasattr(external_data_manager, 'get_ohlcv'):
            # Try alternative: check if there's a direct BTC price getter
            if hasattr(external_data_manager, 'get_btc_price'):
                current_price = await external_data_manager.get_btc_price()
                # Without historical data, we can't calculate EMA - just return True
                logger.debug("check_btc_trend: No OHLCV method, only current price - skipping trend check")
                return True, "BTC trend check skipped (no historical data)"
            else:
                logger.debug("check_btc_trend: No get_ohlcv or get_btc_price method available")
                return True, "BTC data unavailable"
        
        btc_candles = await external_data_manager.get_ohlcv(
            'BTC/USDT',
            timeframe=CorrelationFilters.BTC_TIMEFRAME
        )
        
        if btc_candles is None or len(btc_candles) < 20:
            return True, "BTC data unavailable"
        
        closes = btc_candles['close'].to_numpy()
        ema = calculate_ema(closes, CorrelationFilters.BTC_EMA_PERIOD)[-1]
        current_price = closes[-1]
        
        btc_trend = 'UP' if current_price > ema else 'DOWN'
        
        # Only restrict LONG trades
        if bias == 'LONG' and btc_trend == 'DOWN':
            return False, f"BTC downtrend ({current_price:.2f} < EMA {ema:.2f}) - LONG risky"
        
        return True, f"BTC trend {btc_trend} OK ✓"
        
    except AttributeError as e:
        # Specific handling for missing methods
        logger.debug(f"check_btc_trend: Method not available - {e}")
        return True, "BTC check skipped (method unavailable)"
    except Exception as e:
        logger.warning(f"Failed to check BTC trend: {e}")
        return True, ""  # Don't block if check fails


# ============================================================================
# FIX #7: TRAIN-TEST VALIDATION HELPERS
# ============================================================================

def validate_train_test_split(train_results: dict, test_results: dict, 
                             max_overfitting_pct: float = 10.0) -> Tuple[bool, str]:
    """
    FIX #7: Validate that backtest doesn't have excessive overfitting
    
    Returns: (is_valid: bool, assessment: str)
    """
    train_pnl = train_results.get('pnl_pct', 0)
    test_pnl = test_results.get('pnl_pct', 0)
    
    if train_pnl == 0:
        return False, "No training PnL"
    
    overfitting_pct = abs(train_pnl - test_pnl) / abs(train_pnl) * 100
    
    if overfitting_pct > max_overfitting_pct:
        return False, f"OVERFITTING: Train {train_pnl:.1f}% vs Test {test_pnl:.1f}% (diff {overfitting_pct:.1f}% > {max_overfitting_pct}%)"
    
    return True, f"Good generalization: Train {train_pnl:.1f}% vs Test {test_pnl:.1f}% (diff {overfitting_pct:.1f}%)"
