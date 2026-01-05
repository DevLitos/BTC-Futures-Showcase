"""
Core Utilities - Centralized helper functions for the trading system.

This module provides:
1. Data validation and safe conversions
2. Centralized position sizing calculation
3. Timestamp standardization
4. Candle validation

All modules should use these functions for consistency.
"""

from datetime import datetime, UTC
from typing import Optional, Union, Dict, Any, List, Tuple
import numpy as np
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# SAFE DATA CONVERSIONS
# =============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert any value to float.
    
    Handles None, NaN, strings, and invalid values gracefully.
    
    Args:
        value: Value to convert
        default: Default if conversion fails
        
    Returns:
        Float value or default
    """
    if value is None:
        return default
    try:
        result = float(value)
        if np.isnan(result) or np.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert any value to int."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# =============================================================================
# TIMESTAMP STANDARDIZATION
# =============================================================================

def to_epoch_seconds(ts: Union[int, float, datetime, None]) -> float:
    """
    Convert any timestamp to epoch seconds.
    
    Handles:
    - datetime objects
    - Milliseconds (CCXT default)
    - Seconds
    - None
    
    Args:
        ts: Timestamp in various formats
        
    Returns:
        Epoch time in seconds (float)
    """
    if ts is None:
        return datetime.now(UTC).timestamp()
    
    if isinstance(ts, datetime):
        return ts.timestamp()
    
    ts_float = float(ts)
    
    # If > 1e12, it's likely milliseconds (after year 2001)
    if ts_float > 1e12:
        return ts_float / 1000.0
    
    return ts_float


def to_datetime(ts: Union[int, float, datetime, None]) -> datetime:
    """
    Convert any timestamp to datetime with UTC timezone.
    
    Args:
        ts: Timestamp in various formats
        
    Returns:
        datetime with UTC timezone
    """
    if ts is None:
        return datetime.now(UTC)
    
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    
    epoch_seconds = to_epoch_seconds(ts)
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


def ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# =============================================================================
# CANDLE VALIDATION
# =============================================================================

class CandleValidationError(Exception):
    """Raised when candle data is invalid."""
    pass


def validate_candle(candle: Dict[str, Any], strict: bool = False) -> Tuple[bool, List[str]]:
    """
    Validate OHLCV candle data.
    
    Checks:
    - Required fields exist (timestamp, open, high, low, close)
    - High >= Low
    - High >= Close and High >= Open
    - Low <= Close and Low <= Open
    - Prices are positive
    - Volume >= 0 (if present)
    
    Args:
        candle: Dictionary with candle data
        strict: If True, raise exception on invalid; if False, return status
        
    Returns:
        Tuple (is_valid: bool, errors: List[str])
    """
    errors = []
    
    # Required fields
    required = ['open', 'high', 'low', 'close']
    for field in required:
        if field not in candle or candle[field] is None:
            errors.append(f"Missing or None: {field}")
    
    if errors:
        if strict:
            raise CandleValidationError(f"Invalid candle: {errors}")
        return False, errors
    
    # Convert to floats
    try:
        o = safe_float(candle['open'])
        h = safe_float(candle['high'])
        l = safe_float(candle['low'])
        c = safe_float(candle['close'])
    except Exception as e:
        errors.append(f"Cannot convert OHLC to float: {e}")
        if strict:
            raise CandleValidationError(errors[0])
        return False, errors
    
    # Validate prices are positive
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        errors.append(f"Non-positive price: O={o}, H={h}, L={l}, C={c}")
    
    # Validate OHLC relationships
    if h < l:
        errors.append(f"High ({h}) < Low ({l})")
    
    if h < c or h < o:
        errors.append(f"High ({h}) < Close ({c}) or Open ({o})")
    
    if l > c or l > o:
        errors.append(f"Low ({l}) > Close ({c}) or Open ({o})")
    
    # Validate volume if present
    if 'volume' in candle and candle['volume'] is not None:
        v = safe_float(candle['volume'], -1)
        if v < 0:
            errors.append(f"Negative volume: {v}")
    
    is_valid = len(errors) == 0
    
    if not is_valid and strict:
        raise CandleValidationError(f"Invalid candle: {errors}")
    
    return is_valid, errors


def get_validated_ohlcv(candle: Dict[str, Any], 
                        defaults: Optional[Dict[str, float]] = None) -> Tuple[float, float, float, float, float]:
    """
    Extract validated OHLCV values from candle.
    
    Args:
        candle: Candle dictionary
        defaults: Optional defaults for missing values
        
    Returns:
        Tuple (open, high, low, close, volume)
    """
    defaults = defaults or {}
    
    o = safe_float(candle.get('open'), defaults.get('open', 0.0))
    h = safe_float(candle.get('high'), defaults.get('high', 0.0))
    l = safe_float(candle.get('low'), defaults.get('low', 0.0))
    c = safe_float(candle.get('close'), defaults.get('close', 0.0))
    v = safe_float(candle.get('volume'), defaults.get('volume', 0.0))
    
    return o, h, l, c, v


# =============================================================================
# CENTRALIZED POSITION SIZING
# =============================================================================

class PositionSizer:
    """
    Centralized position sizing calculation.
    
    SINGLE SOURCE OF TRUTH for position size calculation.
    All sizing should go through this class.
    
    Sizing Order:
    1. Base size (risk-based or margin-based, whichever is smaller)
    2. State multiplier (from StrategyStateMachine: 0.0, 0.5, 1.0)
    3. Edge multiplier (from EdgeScore: 0.0-1.0)
    4. Defensive multiplier (from BlackSwanFilter: 0.25-1.0)
    
    Method: Multiplicative with minimum check
    """
    
    def __init__(self, 
                 default_risk_pct: float = 0.01,
                 max_position_pct: float = 0.10,
                 leverage: int = 10):
        """
        Initialize sizer.
        
        Args:
            default_risk_pct: Default risk per trade (1% = 0.01)
            max_position_pct: Maximum position as % of capital
            leverage: Default leverage
        """
        self.default_risk_pct = default_risk_pct
        self.max_position_pct = max_position_pct
        self.leverage = leverage
    
    def calculate_base_size(self,
                           capital: float,
                           entry_price: float,
                           stop_loss: float,
                           risk_pct: Optional[float] = None,
                           leverage: Optional[int] = None) -> float:
        """
        Calculate base position size before multipliers.
        
        Uses the more conservative of:
        1. Risk-based: (capital * risk%) / risk_per_unit
        2. Margin-based: (capital * leverage) / entry_price * max_position_pct
        
        Args:
            capital: Available capital
            entry_price: Entry price
            stop_loss: Stop loss price
            risk_pct: Risk percentage (default: self.default_risk_pct)
            leverage: Leverage to use (default: self.leverage)
            
        Returns:
            Base position size in contracts/units
        """
        if capital <= 0 or entry_price <= 0:
            logger.warning(f"Invalid capital ({capital}) or price ({entry_price})")
            return 0.0
        
        risk_pct = risk_pct or self.default_risk_pct
        leverage = leverage or self.leverage
        
        # Risk per unit (distance to stop)
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit <= 0:
            logger.warning("Risk per unit is zero or negative")
            return 0.0
        
        # Risk-based sizing: lose risk_pct of capital if SL hit
        risk_based_size = (capital * risk_pct) / risk_per_unit
        
        # Margin-based sizing: respect leverage and max position
        max_notional = capital * leverage * self.max_position_pct
        margin_based_size = max_notional / entry_price
        
        # Use smaller of the two for safety
        base_size = min(risk_based_size, margin_based_size)
        
        logger.debug(f"Base size: risk-based={risk_based_size:.6f}, margin-based={margin_based_size:.6f}, final={base_size:.6f}")
        
        return base_size
    
    def apply_multipliers(self,
                         base_size: float,
                         state_multiplier: float = 1.0,
                         edge_multiplier: float = 1.0,
                         defensive_multiplier: float = 1.0) -> Tuple[float, Dict[str, float]]:
        """
        Apply all multipliers to base size.
        
        Order of application (multiplicative):
        1. State multiplier (blocking: if 0, final is 0)
        2. Edge multiplier (scaling)
        3. Defensive multiplier (scaling)
        
        Args:
            base_size: Base position size
            state_multiplier: From StrategyStateMachine (0.0, 0.5, 1.0)
            edge_multiplier: From EdgeScore (0.0-1.0)
            defensive_multiplier: From BlackSwanFilter (0.25-1.0)
            
        Returns:
            Tuple (final_size, multiplier_breakdown)
        """
        # State multiplier is blocking - if 0, no trade
        if state_multiplier == 0.0:
            return 0.0, {
                'base': base_size,
                'state': state_multiplier,
                'edge': edge_multiplier,
                'defensive': defensive_multiplier,
                'combined': 0.0,
                'final': 0.0,
                'blocked_by': 'state'
            }
        
        # Combined multiplier (multiplicative)
        combined = state_multiplier * edge_multiplier * defensive_multiplier
        final_size = base_size * combined
        
        breakdown = {
            'base': base_size,
            'state': state_multiplier,
            'edge': edge_multiplier,
            'defensive': defensive_multiplier,
            'combined': combined,
            'final': final_size,
            'blocked_by': None
        }
        
        logger.debug(f"Size calculation: {base_size:.6f} * {state_multiplier} * {edge_multiplier} * {defensive_multiplier} = {final_size:.6f}")
        
        return final_size, breakdown
    
    def calculate_position_size(self,
                               capital: float,
                               entry_price: float,
                               stop_loss: float,
                               state_multiplier: float = 1.0,
                               edge_multiplier: float = 1.0,
                               defensive_multiplier: float = 1.0,
                               risk_pct: Optional[float] = None,
                               leverage: Optional[int] = None) -> Tuple[float, Dict[str, float]]:
        """
        Calculate final position size with all multipliers.
        
        This is the main entry point for position sizing.
        
        Args:
            capital: Available capital
            entry_price: Entry price
            stop_loss: Stop loss price
            state_multiplier: From StrategyStateMachine
            edge_multiplier: From EdgeScore
            defensive_multiplier: From BlackSwanFilter
            risk_pct: Override risk percentage
            leverage: Override leverage
            
        Returns:
            Tuple (final_size, breakdown_dict)
        """
        base_size = self.calculate_base_size(
            capital=capital,
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_pct=risk_pct,
            leverage=leverage
        )
        
        return self.apply_multipliers(
            base_size=base_size,
            state_multiplier=state_multiplier,
            edge_multiplier=edge_multiplier,
            defensive_multiplier=defensive_multiplier
        )


# =============================================================================
# EXPECTANCY CALCULATION
# =============================================================================

def calculate_expectancy(win_rate: float, 
                        avg_win: float, 
                        avg_loss: float) -> float:
    """
    Calculate trading expectancy.
    
    Canonical formula:
    E = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    
    Args:
        win_rate: Win rate as decimal (0.55 = 55%)
        avg_win: Average winning trade (positive)
        avg_loss: Average losing trade (positive, will be treated as loss)
        
    Returns:
        Expectancy value (positive = profitable edge)
    """
    if win_rate < 0 or win_rate > 1:
        logger.warning(f"Invalid win_rate: {win_rate}")
        return 0.0
    
    loss_rate = 1 - win_rate
    avg_loss = abs(avg_loss)  # Ensure positive
    
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    
    return expectancy


def calculate_expectancy_r(trades: List[Dict[str, Any]]) -> float:
    """
    Calculate expectancy in R-multiples from trade list.
    
    Each trade should have:
    - 'pnl_r': P&L in R-multiples (1R = risked amount)
    - OR 'pnl' and 'risk': to calculate R
    
    Args:
        trades: List of trade dictionaries
        
    Returns:
        Expectancy in R
    """
    if not trades:
        return 0.0
    
    r_values = []
    for trade in trades:
        if 'pnl_r' in trade:
            r_values.append(safe_float(trade['pnl_r']))
        elif 'pnl' in trade and 'risk' in trade:
            risk = safe_float(trade['risk'])
            if risk > 0:
                r_values.append(safe_float(trade['pnl']) / risk)
    
    if not r_values:
        return 0.0
    
    return sum(r_values) / len(r_values)


# =============================================================================
# GLOBAL SIZER INSTANCE
# =============================================================================

_position_sizer: Optional[PositionSizer] = None


def get_position_sizer(risk_pct: float = 0.01, 
                       max_position_pct: float = 0.10,
                       leverage: int = 10) -> PositionSizer:
    """
    Get or create global PositionSizer instance.
    
    Args:
        risk_pct: Default risk per trade
        max_position_pct: Max position size
        leverage: Default leverage
        
    Returns:
        PositionSizer instance
    """
    global _position_sizer
    
    if _position_sizer is None:
        _position_sizer = PositionSizer(
            default_risk_pct=risk_pct,
            max_position_pct=max_position_pct,
            leverage=leverage
        )
    
    return _position_sizer


def reset_position_sizer():
    """Reset global sizer (for testing)."""
    global _position_sizer
    _position_sizer = None
