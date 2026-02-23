"""
Core Utilities - Helper functions for the trading system.

Alive symbols: safe_float, ensure_utc, to_datetime, validate_candle,
CandleValidationError, PositionSizer, get_position_sizer, calculate_expectancy.
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
    """Safely convert any value to float. Handles None, NaN, strings."""
    if value is None:
        return default
    try:
        result = float(value)
        if np.isnan(result) or np.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


# =============================================================================
# TIMESTAMP UTILITIES
# =============================================================================

def ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_epoch_seconds(ts: Union[int, float, datetime, None]) -> float:
    """Convert any timestamp to epoch seconds (internal helper)."""
    if ts is None:
        return datetime.now(UTC).timestamp()
    if isinstance(ts, datetime):
        return ts.timestamp()
    ts_float = float(ts)
    if ts_float > 1e12:
        return ts_float / 1000.0
    return ts_float


def to_datetime(ts: Union[int, float, datetime, None]) -> datetime:
    """Convert any timestamp to datetime with UTC timezone."""
    if ts is None:
        return datetime.now(UTC)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    epoch_seconds = _to_epoch_seconds(ts)
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


# =============================================================================
# CANDLE VALIDATION
# =============================================================================

class CandleValidationError(Exception):
    """Raised when candle data is invalid."""
    pass


def validate_candle(candle: Dict[str, Any], strict: bool = False) -> Tuple[bool, List[str]]:
    """Validate OHLCV candle data. Returns (is_valid, errors)."""
    errors = []
    required = ['open', 'high', 'low', 'close']
    for field in required:
        if field not in candle or candle[field] is None:
            errors.append(f"Missing or None: {field}")

    if errors:
        if strict:
            raise CandleValidationError(f"Invalid candle: {errors}")
        return False, errors

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

    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        errors.append(f"Non-positive price: O={o}, H={h}, L={l}, C={c}")
    if h < l:
        errors.append(f"High ({h}) < Low ({l})")
    if h < c or h < o:
        errors.append(f"High ({h}) < Close ({c}) or Open ({o})")
    if l > c or l > o:
        errors.append(f"Low ({l}) > Close ({c}) or Open ({o})")

    if 'volume' in candle and candle['volume'] is not None:
        v = safe_float(candle['volume'], -1)
        if v < 0:
            errors.append(f"Negative volume: {v}")

    is_valid = len(errors) == 0
    if not is_valid and strict:
        raise CandleValidationError(f"Invalid candle: {errors}")
    return is_valid, errors


# =============================================================================
# DYNAMIC RISK CALCULATION
# =============================================================================

def _get_dynamic_risk_pct(equity: float) -> float:
    """Get risk percentage based on current equity (internal helper)."""
    from src.config.trading_config import DynamicRiskConfig, OperatingMode

    if OperatingMode.is_discovery():
        return OperatingMode.DISCOVERY_RISK_PCT / 100.0

    for max_equity, risk_pct in DynamicRiskConfig.RISK_TIERS:
        if equity < max_equity:
            risk_pct = max(risk_pct, DynamicRiskConfig.MIN_RISK_PCT)
            risk_pct = min(risk_pct, DynamicRiskConfig.MAX_RISK_PCT)
            return risk_pct / 100.0
    return 0.005


# =============================================================================
# CENTRALIZED POSITION SIZING
# =============================================================================

class PositionSizer:
    """
    Centralized position sizing.

    Sizing Order:
    1. Base size (risk-based or margin-based, whichever smaller)
    2. State multiplier (from StrategyStateMachine)
    3. Edge multiplier (from EdgeScore)
    4. Defensive multiplier (from BlackSwanFilter)
    """

    def __init__(self,
                 default_risk_pct: float = 0.01,
                 max_position_pct: float = 0.10,
                 leverage: int = 2,
                 use_dynamic_risk: bool = True,
                 use_compounding: bool = True):
        self.default_risk_pct = default_risk_pct
        self.max_position_pct = max_position_pct
        self.leverage = leverage
        self.use_dynamic_risk = use_dynamic_risk
        self.use_compounding = use_compounding

    def calculate_base_size(self,
                            capital: float,
                            entry_price: float,
                            stop_loss: float,
                            risk_pct: Optional[float] = None,
                            leverage: Optional[int] = None) -> float:
        """Calculate base position size before multipliers."""
        if capital <= 0 or entry_price <= 0:
            logger.warning(f"Invalid capital ({capital}) or price ({entry_price})")
            return 0.0

        from src.config.trading_config import DynamicRiskConfig
        leverage = leverage or self.leverage

        # MICRO ACCOUNT: Percentage-based sizing
        if capital < DynamicRiskConfig.MICRO_ACCOUNT_THRESHOLD:
            MICRO_ACCOUNT_MARGIN_PCT = 0.80
            base_margin = capital * MICRO_ACCOUNT_MARGIN_PCT
            position_notional = base_margin * leverage
            MIN_BASE_NOTIONAL = 750.0
            position_notional = max(position_notional, MIN_BASE_NOTIONAL)
            base_size = position_notional / entry_price
            base_size = max(base_size, DynamicRiskConfig.MIN_BTC_SIZE)
            logger.info(
                f"💰 MICRO sizing: ${capital:.2f} × {MICRO_ACCOUNT_MARGIN_PCT*100:.0f}% "
                f"= ${base_margin:.2f} × {leverage}x = ${position_notional:.2f} → {base_size:.6f} BTC"
            )
            return base_size

        # NORMAL ACCOUNT: Risk-based sizing
        if risk_pct is None:
            risk_pct = _get_dynamic_risk_pct(capital) if self.use_dynamic_risk else self.default_risk_pct

        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit <= 0:
            logger.warning("Risk per unit is zero or negative")
            return 0.0

        risk_based_size = (capital * risk_pct) / risk_per_unit
        max_notional = capital * leverage * self.max_position_pct
        margin_based_size = max_notional / entry_price
        base_size = min(risk_based_size, margin_based_size)
        base_size = max(base_size, DynamicRiskConfig.MIN_BTC_SIZE)
        return base_size

    def apply_multipliers(self,
                          base_size: float,
                          state_multiplier: float = 1.0,
                          edge_multiplier: float = 1.0,
                          defensive_multiplier: float = 1.0) -> Tuple[float, Dict[str, float | str | None]]:
        """Apply all multipliers to base size."""
        if state_multiplier == 0.0:
            return 0.0, {
                'base': base_size, 'state': 0.0, 'edge': edge_multiplier,
                'defensive': defensive_multiplier, 'combined': 0.0,
                'final': 0.0, 'blocked_by': 'state'
            }

        combined = state_multiplier * edge_multiplier * defensive_multiplier
        final_size = base_size * combined
        return final_size, {
            'base': base_size, 'state': state_multiplier, 'edge': edge_multiplier,
            'defensive': defensive_multiplier, 'combined': combined,
            'final': final_size, 'blocked_by': None
        }

    def calculate_position_size(self,
                                capital: float,
                                entry_price: float,
                                stop_loss: float,
                                state_multiplier: float = 1.0,
                                edge_multiplier: float = 1.0,
                                defensive_multiplier: float = 1.0,
                                volatility_regime: str = "NORMAL",
                                session_name: str = "OTHER",
                                risk_pct: Optional[float] = None,
                                leverage: Optional[int] = None) -> Tuple[float, Dict[str, float | str | None]]:
        """Calculate final position size with all multipliers."""
        from src.config.trading_config import VolatilityRegime, SessionQuality

        base_size = self.calculate_base_size(capital, entry_price, stop_loss, risk_pct, leverage)
        volatility_mult = VolatilityRegime.get_size_multiplier(volatility_regime)
        session_mult = SessionQuality.get_session_size_mult(session_name)

        final_size, breakdown = self.apply_multipliers(
            base_size, state_multiplier, edge_multiplier, defensive_multiplier
        )
        final_size = final_size * volatility_mult * session_mult
        breakdown['volatility_mult'] = volatility_mult
        breakdown['session_mult'] = session_mult
        breakdown['final'] = final_size
        return final_size, breakdown


# =============================================================================
# EXPECTANCY
# =============================================================================

def calculate_expectancy(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Calculate trading expectancy: E = (WR * avg_win) - ((1-WR) * avg_loss)."""
    if win_rate < 0 or win_rate > 1:
        return 0.0
    return (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))


# =============================================================================
# GLOBAL SIZER INSTANCE
# =============================================================================

_position_sizer: Optional[PositionSizer] = None


def get_position_sizer(risk_pct: float = 0.01,
                       max_position_pct: float = 0.10,
                       leverage: int = 2,
                       use_dynamic_risk: bool = True,
                       use_compounding: bool = True) -> PositionSizer:
    """Get or create global PositionSizer instance."""
    global _position_sizer
    if _position_sizer is None:
        _position_sizer = PositionSizer(
            default_risk_pct=risk_pct,
            max_position_pct=max_position_pct,
            leverage=leverage,
            use_dynamic_risk=use_dynamic_risk,
            use_compounding=use_compounding
        )
    return _position_sizer
