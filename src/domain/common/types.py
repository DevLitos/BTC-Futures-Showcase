"""
Common domain types.

This module contains fundamental types used across all domain modules:
- Side: Trade direction (LONG/SHORT/FLAT)
- MarketRegime: Market classification
- MarketSnapshot: Immutable market state
- FeatureVector: ML feature set
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from enum import Enum
from typing import Dict, Any, List


class Side(Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"  # No position
    
    def opposite(self) -> 'Side':
        if self == Side.LONG:
            return Side.SHORT
        elif self == Side.SHORT:
            return Side.LONG
        return Side.FLAT
    
    @classmethod
    def from_string(cls, s: str) -> 'Side':
        """Parse side from string (case insensitive)."""
        mapping = {
            'long': cls.LONG, 'buy': cls.LONG, 'bid': cls.LONG,
            'short': cls.SHORT, 'sell': cls.SHORT, 'ask': cls.SHORT,
            'flat': cls.FLAT, 'none': cls.FLAT, '': cls.FLAT,
        }
        return mapping.get(s.lower(), cls.FLAT)


class MarketRegime(Enum):
    """Market regime classification."""
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    DEAD = "DEAD"  # Low activity


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Immutable snapshot of market state at a point in time.
    
    This is the input to all strategy decisions. It contains:
    - Current price and spread
    - OHLCV from multiple timeframes
    - Order book state
    - Derived indicators
    """
    timestamp: datetime
    symbol: str
    
    # Current state
    price: float
    bid: float
    ask: float
    
    # Volume
    volume_24h: float = 0.0
    
    # From 1m candles (last N)
    atr_1m: float = 0.0
    
    # From 5m candles
    atr_5m: float = 0.0
    ema_20_5m: float = 0.0
    ema_50_5m: float = 0.0
    
    # From 1h candles
    atr_1h: float = 0.0
    ema_50_1h: float = 0.0
    ema_200_1h: float = 0.0
    adx_1h: float = 0.0
    adx_slope_1h: float = 0.0
    
    # Order book
    orderbook_imbalance: float = 0.5  # 0 = all bids, 1 = all asks
    spread_bps: float = 0.0
    
    # Regime
    regime: MarketRegime = MarketRegime.RANGE
    
    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2
    
    @property
    def spread_pct(self) -> float:
        if self.mid_price == 0:
            return 0.0
        return (self.ask - self.bid) / self.mid_price * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/storage."""
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        result['regime'] = self.regime.value
        return result


@dataclass(frozen=True)
class FeatureVector:
    """
    Feature vector for ML/scoring models.
    
    Contains all features used for trade decisions.
    This is saved with every trade for post-hoc analysis.
    """
    timestamp: datetime
    
    # HTF features (1h)
    htf_trend: float = 0.0         # -1 (down) to +1 (up)
    htf_strength: float = 0.0      # 0 to 1 (ADX normalized)
    htf_momentum: float = 0.0      # ADX slope
    
    # MTF features (5m)
    mtf_structure: float = 0.0     # HL/LH quality
    mtf_distance_ema: float = 0.0  # Distance from EMA in ATR units
    
    # LTF features (1m)
    ltf_trigger: float = 0.0       # Trigger quality 0-1
    ltf_volume_ratio: float = 0.0  # Relative volume
    ltf_velocity: float = 0.0      # Price velocity in ATR/candle
    
    # Context features
    session_quality: float = 0.0   # 0-1
    funding_rate: float = 0.0      # Current funding
    regime_score: float = 0.0      # Regime quality for trading
    
    # Anchor levels
    distance_to_support: float = 0.0  # In ATR units
    distance_to_resistance: float = 0.0
    
    # Computed score (from scoring model)
    raw_score: float = 0.0
    calibrated_probability: float = 0.0  # P(win|features)
    expected_r: float = 0.0  # E[R|features]
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        return result
    
    def as_array(self) -> List[float]:
        """Convert to numpy-compatible array for ML models."""
        return [
            self.htf_trend, self.htf_strength, self.htf_momentum,
            self.mtf_structure, self.mtf_distance_ema,
            self.ltf_trigger, self.ltf_volume_ratio, self.ltf_velocity,
            self.session_quality, self.funding_rate, self.regime_score,
            self.distance_to_support, self.distance_to_resistance,
        ]
