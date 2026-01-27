#!/usr/bin/env python3
"""
MASSIVE MOCK BACKTEST - Stress Testing & Edge Discovery

Generates realistic BTC price data with edge cases designed to BREAK the bot.
Tests the complete trading system with:
- 10x leverage
- 300€ initial capital
- Realistic market scenarios + extreme edge cases

Scenario Categories:
1. NORMAL MARKET: Typical BTC behavior with trends and ranges
2. HIGH VOLATILITY: Flash crashes, pumps, wicks
3. CHOP MARKET: False breakouts, whipsaws, stop hunting
4. TREND DAYS: Strong directional moves
5. BLACK SWAN: Extreme events (liquidation cascades, gap opens)
6. EDGE CASES: NaN values, timestamp gaps, duplicate candles

Author: Backtest Stress Test Suite
Date: Jan 26, 2026
"""

import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging
import json
import random

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================
@dataclass
class BacktestConfig:
    """Configuration for the massive backtest."""
    initial_capital: float = 300.0  # 300 EUR
    leverage: int = 10              # 10x
    risk_per_trade: float = 0.01    # 1% per trade
    max_position_pct: float = 0.10  # Max 10% of capital per trade
    base_price: float = 89000.0     # Starting BTC price
    symbol: str = "BTC/USD:USD"
    
    # Scenario distribution
    num_days: int = 30              # 30 days of data
    candles_per_day: int = 1440     # 1m candles
    
    # Stress test intensity
    include_black_swan: bool = True
    include_edge_cases: bool = True
    flash_crash_probability: float = 0.001  # 0.1% chance per candle
    gap_probability: float = 0.0005  # 0.05% chance per candle


class MarketRegime(Enum):
    """Market regime types for scenario generation."""
    RANGE_QUIET = "range_quiet"       # Low volatility range
    RANGE_VOLATILE = "range_volatile" # High volatility range (chop)
    TREND_UP_WEAK = "trend_up_weak"   # Weak uptrend
    TREND_UP_STRONG = "trend_up_strong"  # Strong uptrend
    TREND_DOWN_WEAK = "trend_down_weak"  # Weak downtrend
    TREND_DOWN_STRONG = "trend_down_strong"  # Strong downtrend
    BREAKOUT = "breakout"             # Volatility expansion
    CONSOLIDATION = "consolidation"   # Tightening range before move


# =============================================================================
# MOCK DATA GENERATOR
# =============================================================================
class RealisticBTCGenerator:
    """
    Generates realistic BTC 1-minute candle data with controlled scenarios.
    
    Based on real BTC behavior patterns:
    - Mean-reverting in ranges
    - Momentum in trends
    - Stop hunting wicks
    - Volume profiles by session
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.current_price = config.base_price
        self.current_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        
        # Volatility state (ATR-like)
        self.base_volatility = 50.0  # Base ATR in USD
        self.current_volatility = self.base_volatility
        
        # Regime tracking
        self.current_regime = MarketRegime.RANGE_QUIET
        self.regime_duration = 0
        self.regime_target_duration = 60  # Minutes
        
        # Trend tracking
        self.trend_direction = 0  # -1, 0, +1
        self.trend_strength = 0.0  # 0-1
        
        # Session volatility multipliers (UTC hours)
        self.session_volatility = {
            range(0, 4): 0.5,    # Dead hours
            range(4, 8): 0.7,    # Asia early
            range(8, 12): 1.2,   # London
            range(12, 16): 1.5,  # London/NY overlap
            range(16, 20): 1.3,  # NY
            range(20, 24): 0.8,  # Late NY
        }
        
        # Support/resistance levels (dynamic)
        self.support_levels = []
        self.resistance_levels = []
        self._update_levels()
    
    def _get_session_vol_mult(self) -> float:
        """Get volatility multiplier for current session."""
        hour = self.current_time.hour
        for hours_range, mult in self.session_volatility.items():
            if hour in hours_range:
                return mult
        return 1.0
    
    def _update_levels(self):
        """Update support/resistance levels based on current price."""
        # Create realistic S/R levels around current price
        atr = self.current_volatility * 5  # ~5 ATR band
        
        self.support_levels = [
            self.current_price - atr * 0.5,
            self.current_price - atr * 1.0,
            self.current_price - atr * 2.0,
        ]
        self.resistance_levels = [
            self.current_price + atr * 0.5,
            self.current_price + atr * 1.0,
            self.current_price + atr * 2.0,
        ]
    
    def _maybe_change_regime(self):
        """Potentially change market regime."""
        self.regime_duration += 1
        
        if self.regime_duration >= self.regime_target_duration:
            # Time to potentially change regime
            roll = random.random()
            
            if self.current_regime in [MarketRegime.RANGE_QUIET, MarketRegime.RANGE_VOLATILE]:
                # From range, can break out or continue
                if roll < 0.2:
                    self.current_regime = MarketRegime.BREAKOUT
                    self.trend_direction = random.choice([-1, 1])
                    self.trend_strength = random.uniform(0.5, 1.0)
                elif roll < 0.4:
                    self.current_regime = MarketRegime.RANGE_VOLATILE if self.current_regime == MarketRegime.RANGE_QUIET else MarketRegime.RANGE_QUIET
                # else stay in current range
                
            elif self.current_regime == MarketRegime.BREAKOUT:
                # Breakout leads to trend
                if self.trend_direction > 0:
                    self.current_regime = MarketRegime.TREND_UP_STRONG if roll < 0.5 else MarketRegime.TREND_UP_WEAK
                else:
                    self.current_regime = MarketRegime.TREND_DOWN_STRONG if roll < 0.5 else MarketRegime.TREND_DOWN_WEAK
                    
            elif "trend" in self.current_regime.value:
                # Trends can continue, weaken, or consolidate
                if roll < 0.3:
                    self.current_regime = MarketRegime.CONSOLIDATION
                elif roll < 0.5:
                    # Weaken or strengthen
                    if "strong" in self.current_regime.value:
                        self.current_regime = MarketRegime.TREND_UP_WEAK if "up" in self.current_regime.value else MarketRegime.TREND_DOWN_WEAK
                    else:
                        self.current_regime = MarketRegime.TREND_UP_STRONG if "up" in self.current_regime.value else MarketRegime.TREND_DOWN_STRONG
                # else continue
                
            elif self.current_regime == MarketRegime.CONSOLIDATION:
                # Consolidation leads to breakout or range
                if roll < 0.6:
                    self.current_regime = MarketRegime.BREAKOUT
                else:
                    self.current_regime = MarketRegime.RANGE_QUIET
            
            self.regime_duration = 0
            self.regime_target_duration = random.randint(30, 180)  # 30min to 3h
            self._update_levels()
    
    def _generate_candle(self) -> Dict:
        """Generate a single realistic 1-minute candle."""
        self._maybe_change_regime()
        
        # Base movement parameters
        session_mult = self._get_session_vol_mult()
        effective_vol = self.current_volatility * session_mult
        
        # Regime-based drift and volatility
        drift = 0.0
        vol_mult = 1.0
        
        if self.current_regime == MarketRegime.RANGE_QUIET:
            drift = 0
            vol_mult = 0.5
            # Mean reversion
            mean_price = (self.support_levels[0] + self.resistance_levels[0]) / 2
            drift = (mean_price - self.current_price) / (effective_vol * 10)
            
        elif self.current_regime == MarketRegime.RANGE_VOLATILE:
            drift = 0
            vol_mult = 1.5
            # More erratic, but still mean-reverting
            mean_price = (self.support_levels[0] + self.resistance_levels[0]) / 2
            drift = (mean_price - self.current_price) / (effective_vol * 20)
            
        elif self.current_regime == MarketRegime.TREND_UP_WEAK:
            drift = 0.3 * effective_vol / 100
            vol_mult = 0.8
            
        elif self.current_regime == MarketRegime.TREND_UP_STRONG:
            drift = 0.8 * effective_vol / 100
            vol_mult = 1.2
            
        elif self.current_regime == MarketRegime.TREND_DOWN_WEAK:
            drift = -0.3 * effective_vol / 100
            vol_mult = 0.8
            
        elif self.current_regime == MarketRegime.TREND_DOWN_STRONG:
            drift = -0.8 * effective_vol / 100
            vol_mult = 1.2
            
        elif self.current_regime == MarketRegime.BREAKOUT:
            drift = self.trend_direction * self.trend_strength * effective_vol / 50
            vol_mult = 2.0
            
        elif self.current_regime == MarketRegime.CONSOLIDATION:
            drift = 0
            vol_mult = 0.3
        
        # Generate OHLC
        open_price = self.current_price
        
        # Random walk with drift
        move = np.random.normal(drift, effective_vol * vol_mult / 100)
        close_price = open_price + move
        
        # High/Low with wicks
        wick_mult = 1.0 + random.random() * 0.5  # 1.0-1.5x
        if close_price > open_price:
            high_price = close_price + abs(move) * random.random() * wick_mult
            low_price = open_price - abs(move) * random.random() * 0.5
        else:
            high_price = open_price + abs(move) * random.random() * 0.5
            low_price = close_price - abs(move) * random.random() * wick_mult
        
        # Stop hunting wicks (occasional)
        if random.random() < 0.02:  # 2% chance
            if random.random() < 0.5:
                # Downward wick (stop hunt below)
                low_price = min(low_price, open_price - effective_vol * 1.5)
            else:
                # Upward wick (stop hunt above)
                high_price = max(high_price, open_price + effective_vol * 1.5)
        
        # Ensure OHLC validity
        high_price = max(open_price, close_price, high_price)
        low_price = min(open_price, close_price, low_price)
        
        # Volume based on volatility and session
        base_volume = 10.0  # Base volume in BTC
        vol_factor = abs(move) / (effective_vol / 100) if effective_vol > 0 else 1
        volume = base_volume * session_mult * (0.5 + vol_factor) * random.uniform(0.8, 1.2)
        
        # Update state
        self.current_price = close_price
        
        # Slowly adjust volatility
        self.current_volatility = 0.99 * self.current_volatility + 0.01 * self.base_volatility
        if "strong" in self.current_regime.value or self.current_regime == MarketRegime.BREAKOUT:
            self.current_volatility = min(self.current_volatility * 1.01, self.base_volatility * 3)
        
        candle = {
            'timestamp': self.current_time,
            'open': round(open_price, 1),
            'high': round(high_price, 1),
            'low': round(low_price, 1),
            'close': round(close_price, 1),
            'volume': round(volume, 4),
            'regime': self.current_regime.value,
        }
        
        self.current_time += timedelta(minutes=1)
        return candle
    
    def _generate_flash_crash(self) -> List[Dict]:
        """Generate a flash crash scenario (5-10 candles)."""
        candles = []
        crash_depth = random.uniform(0.03, 0.08)  # 3-8% crash
        crash_candles = random.randint(3, 7)
        recovery_candles = random.randint(5, 15)
        
        start_price = self.current_price
        bottom_price = start_price * (1 - crash_depth)
        
        # Crash phase
        for i in range(crash_candles):
            progress = (i + 1) / crash_candles
            target = start_price - (start_price - bottom_price) * progress
            
            open_price = self.current_price
            close_price = target + random.uniform(-50, 50)
            high_price = max(open_price, close_price) + random.uniform(0, 100)
            low_price = min(open_price, close_price) - random.uniform(100, 500)
            
            candles.append({
                'timestamp': self.current_time,
                'open': round(open_price, 1),
                'high': round(high_price, 1),
                'low': round(low_price, 1),
                'close': round(close_price, 1),
                'volume': round(random.uniform(50, 200), 4),
                'regime': 'flash_crash',
            })
            
            self.current_price = close_price
            self.current_time += timedelta(minutes=1)
        
        # Recovery phase (partial)
        recovery_target = bottom_price + (start_price - bottom_price) * random.uniform(0.3, 0.7)
        for i in range(recovery_candles):
            progress = (i + 1) / recovery_candles
            target = self.current_price + (recovery_target - self.current_price) * progress * 0.3
            
            open_price = self.current_price
            close_price = target + random.uniform(-30, 30)
            high_price = max(open_price, close_price) + random.uniform(0, 100)
            low_price = min(open_price, close_price) - random.uniform(0, 50)
            
            candles.append({
                'timestamp': self.current_time,
                'open': round(open_price, 1),
                'high': round(high_price, 1),
                'low': round(low_price, 1),
                'close': round(close_price, 1),
                'volume': round(random.uniform(20, 80), 4),
                'regime': 'flash_crash_recovery',
            })
            
            self.current_price = close_price
            self.current_time += timedelta(minutes=1)
        
        return candles
    
    def _generate_gap(self) -> Dict:
        """Generate a gap open (price discontinuity)."""
        gap_pct = random.choice([-1, 1]) * random.uniform(0.005, 0.02)  # 0.5-2% gap
        
        gap_price = self.current_price * (1 + gap_pct)
        
        candle = {
            'timestamp': self.current_time,
            'open': round(gap_price, 1),
            'high': round(gap_price + random.uniform(0, 50), 1),
            'low': round(gap_price - random.uniform(0, 50), 1),
            'close': round(gap_price + random.uniform(-30, 30), 1),
            'volume': round(random.uniform(30, 100), 4),
            'regime': 'gap',
        }
        
        self.current_price = candle['close']
        self.current_time += timedelta(minutes=1)
        return candle
    
    def generate_dataset(self) -> pd.DataFrame:
        """Generate the complete dataset with all scenarios."""
        logger.info(f"Generating {self.config.num_days} days of mock data...")
        
        candles = []
        total_candles = self.config.num_days * self.config.candles_per_day
        
        flash_crash_count = 0
        gap_count = 0
        
        i = 0
        while i < total_candles:
            # Check for special events
            if self.config.include_black_swan and random.random() < self.config.flash_crash_probability:
                flash_candles = self._generate_flash_crash()
                candles.extend(flash_candles)
                i += len(flash_candles)
                flash_crash_count += 1
                logger.info(f"  💥 Flash crash #{flash_crash_count} at {self.current_time}")
                continue
            
            if self.config.include_black_swan and random.random() < self.config.gap_probability:
                gap_candle = self._generate_gap()
                candles.append(gap_candle)
                i += 1
                gap_count += 1
                continue
            
            # Normal candle
            candle = self._generate_candle()
            candles.append(candle)
            i += 1
            
            if i % 10000 == 0:
                logger.info(f"  Generated {i}/{total_candles} candles...")
        
        df = pd.DataFrame(candles)
        
        logger.info(f"✅ Generated {len(df)} candles")
        logger.info(f"   Flash crashes: {flash_crash_count}")
        logger.info(f"   Gaps: {gap_count}")
        logger.info(f"   Price range: {df['low'].min():.0f} - {df['high'].max():.0f}")
        
        return df
    
    def add_edge_cases(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add edge cases to stress test error handling."""
        if not self.config.include_edge_cases:
            return df
        
        logger.info("Adding edge cases...")
        df = df.copy()
        n = len(df)
        
        # 1. NaN values (rare)
        nan_indices = random.sample(range(n), min(10, n // 1000))
        for idx in nan_indices:
            col = random.choice(['open', 'high', 'low', 'close', 'volume'])
            df.loc[idx, col] = np.nan
        logger.info(f"  Added {len(nan_indices)} NaN values")
        
        # 2. Zero volume candles
        zero_vol_indices = random.sample(range(n), min(20, n // 500))
        for idx in zero_vol_indices:
            df.loc[idx, 'volume'] = 0.0
        logger.info(f"  Added {len(zero_vol_indices)} zero-volume candles")
        
        # 3. Invalid OHLC (high < low) - should be caught
        invalid_ohlc = random.sample(range(n), min(5, n // 2000))
        for idx in invalid_ohlc:
            df.loc[idx, 'high'] = df.loc[idx, 'low'] - 10
        logger.info(f"  Added {len(invalid_ohlc)} invalid OHLC candles")
        
        # 4. Extreme wicks (10x ATR)
        extreme_wicks = random.sample(range(n), min(15, n // 700))
        for idx in extreme_wicks:
            if random.random() < 0.5:
                df.loc[idx, 'low'] = df.loc[idx, 'close'] - 500
            else:
                df.loc[idx, 'high'] = df.loc[idx, 'close'] + 500
        logger.info(f"  Added {len(extreme_wicks)} extreme wick candles")
        
        # 5. Duplicate timestamps (should be handled)
        dup_timestamps = random.sample(range(1, n), min(10, n // 1000))
        for idx in dup_timestamps:
            df.loc[idx, 'timestamp'] = df.loc[idx - 1, 'timestamp']
        logger.info(f"  Added {len(dup_timestamps)} duplicate timestamps")
        
        return df


# =============================================================================
# BACKTEST ENGINE
# =============================================================================
@dataclass
class Trade:
    """Represents a single trade."""
    entry_time: datetime
    entry_price: float
    direction: str
    size: float
    stop_loss: float
    take_profit: float
    entry_score: float = 0.0
    entry_tags: Dict = field(default_factory=dict)
    
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_usd: float = 0.0
    pnl_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    
    def calculate_pnl(self, exit_price: float):
        """Calculate PnL for this trade."""
        if self.direction == 'LONG':
            self.pnl_usd = (exit_price - self.entry_price) * self.size
        else:
            self.pnl_usd = (self.entry_price - exit_price) * self.size
        
        # Calculate R
        risk_per_unit = abs(self.entry_price - self.stop_loss)
        if risk_per_unit > 0:
            self.pnl_r = self.pnl_usd / (risk_per_unit * self.size)


@dataclass 
class BacktestState:
    """Tracks backtest state."""
    capital: float
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    current_trade: Optional[Trade] = None
    
    # Tracking
    consecutive_losses: int = 0
    daily_trades: int = 0
    daily_losses: int = 0
    last_trade_date: Optional[str] = None
    
    # Position tracking
    highest_equity: float = 0.0
    max_drawdown: float = 0.0
    
    # Statistics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0


class MassiveBacktester:
    """
    Main backtest engine that replicates live trading logic.
    
    Implements:
    - Score-based entry system (structure, timing, momentum, context)
    - Risk management (1% risk, 10x leverage)
    - Position sizing with leverage
    - SL/TP management
    - Loss streak cooldowns
    - Session-based limits
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.state = BacktestState(capital=config.initial_capital)
        self.state.highest_equity = config.initial_capital
        
        # Market data buffers
        self.candles_1m: List[Dict] = []
        self.candles_5m: List[Dict] = []
        self.candles_1h: List[Dict] = []
        
        # Indicators cache
        self._ema_20_5m = 0.0
        self._ema_50_1h = 0.0
        self._atr_1m = 0.0
        self._atr_5m = 0.0
        self._adx_1h = 0.0
        self._rsi_1m = 50.0
        
        # State
        self._cooldown_until: Optional[datetime] = None
        self._htf_bias: Optional[str] = None
        self._current_regime: str = 'RANGE'
        
    def _aggregate_candles(self, candles_1m: List[Dict], period: int) -> List[Dict]:
        """Aggregate 1m candles to higher timeframe."""
        if len(candles_1m) < period:
            return []
        
        aggregated = []
        for i in range(0, len(candles_1m) - period + 1, period):
            group = candles_1m[i:i + period]
            if not group:
                continue
            
            agg = {
                'timestamp': group[0]['timestamp'],
                'open': group[0]['open'],
                'high': max(c['high'] for c in group if not np.isnan(c.get('high', 0))),
                'low': min(c['low'] for c in group if not np.isnan(c.get('low', float('inf')))),
                'close': group[-1]['close'],
                'volume': sum(c.get('volume', 0) for c in group if not np.isnan(c.get('volume', 0))),
            }
            aggregated.append(agg)
        
        return aggregated
    
    def _calculate_ema(self, prices: List[float], period: int) -> float:
        """Calculate EMA."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        
        # Filter NaN
        prices = [p for p in prices if not np.isnan(p)]
        if not prices:
            return 0.0
        
        arr = np.array(prices[-period * 2:])  # Use 2x period for warmup
        k = 2 / (period + 1)
        ema = arr[0]
        for price in arr[1:]:
            ema = price * k + ema * (1 - k)
        return float(ema)
    
    def _calculate_atr(self, candles: List[Dict], period: int = 14) -> float:
        """Calculate ATR."""
        if len(candles) < period + 1:
            return 50.0  # Default ATR
        
        trs = []
        for i in range(1, len(candles)):
            c = candles[i]
            prev_c = candles[i - 1]
            
            # Skip invalid candles
            if any(np.isnan(v) for v in [c['high'], c['low'], c['close'], prev_c['close']]):
                continue
            
            tr = max(
                c['high'] - c['low'],
                abs(c['high'] - prev_c['close']),
                abs(c['low'] - prev_c['close'])
            )
            trs.append(tr)
        
        if len(trs) < period:
            return 50.0
        
        return float(np.mean(trs[-period:]))
    
    def _calculate_adx(self, candles: List[Dict], period: int = 14) -> float:
        """Simplified ADX calculation."""
        if len(candles) < period * 2:
            return 20.0  # Default
        
        # Calculate +DM, -DM
        plus_dm = []
        minus_dm = []
        trs = []
        
        for i in range(1, len(candles)):
            c = candles[i]
            prev_c = candles[i - 1]
            
            if any(np.isnan(v) for v in [c['high'], c['low'], prev_c['high'], prev_c['low']]):
                continue
            
            high_diff = c['high'] - prev_c['high']
            low_diff = prev_c['low'] - c['low']
            
            plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0)
            minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0)
            
            tr = max(c['high'] - c['low'], abs(c['high'] - prev_c['close']), abs(c['low'] - prev_c['close']))
            trs.append(tr)
        
        if len(trs) < period:
            return 20.0
        
        # Smoothed averages
        atr = np.mean(trs[-period:])
        if atr == 0:
            return 20.0
        
        plus_di = 100 * np.mean(plus_dm[-period:]) / atr
        minus_di = 100 * np.mean(minus_dm[-period:]) / atr
        
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 20.0
        
        dx = 100 * abs(plus_di - minus_di) / di_sum
        return float(dx)
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate RSI."""
        if len(prices) < period + 1:
            return 50.0
        
        # Filter NaN
        prices = [p for p in prices if not np.isnan(p)]
        if len(prices) < period + 1:
            return 50.0
        
        deltas = np.diff(prices[-period - 1:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))
    
    def _update_indicators(self, candle_1m: Dict):
        """Update all indicators with new candle."""
        self.candles_1m.append(candle_1m)
        
        # Keep last 1000 1m candles for more history
        if len(self.candles_1m) > 1000:
            self.candles_1m = self.candles_1m[-1000:]
        
        # Update higher timeframes
        self.candles_5m = self._aggregate_candles(self.candles_1m, 5)
        self.candles_1h = self._aggregate_candles(self.candles_1m, 60)
        
        # Calculate indicators
        if self.candles_1m:
            closes_1m = [c['close'] for c in self.candles_1m if not np.isnan(c.get('close', 0))]
            self._atr_1m = self._calculate_atr(self.candles_1m, 14)
            self._rsi_1m = self._calculate_rsi(closes_1m, 14)
        
        if len(self.candles_5m) >= 5:
            closes_5m = [c['close'] for c in self.candles_5m if not np.isnan(c.get('close', 0))]
            self._ema_20_5m = self._calculate_ema(closes_5m, min(20, len(closes_5m)))
            self._atr_5m = self._calculate_atr(self.candles_5m, 14)
        
        if len(self.candles_1h) >= 3:
            closes_1h = [c['close'] for c in self.candles_1h if not np.isnan(c.get('close', 0))]
            # Use available candles for EMA, up to 50
            ema_period = min(50, max(3, len(closes_1h)))
            self._ema_50_1h = self._calculate_ema(closes_1h, ema_period)
            self._adx_1h = self._calculate_adx(self.candles_1h, min(14, max(5, len(self.candles_1h) - 1)))
    
    def _check_htf_bias(self, current_price: float) -> Optional[str]:
        """Check HTF bias (1H EMA50)."""
        # Reduced requirement for warmup
        if len(self.candles_1h) < 10:
            return None
        
        if self._ema_50_1h <= 0:
            # Fallback to simple moving average of recent closes
            if self.candles_1h:
                closes = [c['close'] for c in self.candles_1h[-20:] if not np.isnan(c.get('close', 0))]
                if closes:
                    self._ema_50_1h = np.mean(closes)
        
        if self._ema_50_1h <= 0:
            return None
        
        # Price vs EMA50 with smaller threshold for more signals
        threshold = self._atr_1m * 0.2 if self._atr_1m > 0 else 20
        
        if current_price > self._ema_50_1h + threshold:
            return 'LONG'
        elif current_price < self._ema_50_1h - threshold:
            return 'SHORT'
        
        return None
    
    def _calculate_entry_score(self, current_price: float, direction: str, candle: Dict) -> Tuple[float, Dict]:
        """
        Calculate entry score following the 4-block system.
        Max score = 9 (4 blocks × 2 + synergy bonus)
        """
        score = 0.0
        breakdown = {
            'structure': 0,
            'timing': 0,
            'momentum': 0,
            'context': 0,
            'synergy': 0,
        }
        
        # === STRUCTURE (0-2) ===
        # HTF alignment + 5m structure
        htf_aligned = (self._htf_bias == direction)
        
        if htf_aligned:
            breakdown['structure'] += 1
            
            # Check 5m structure (price vs EMA20)
            if self._ema_20_5m > 0:
                if direction == 'LONG' and current_price > self._ema_20_5m * 0.995:
                    breakdown['structure'] += 1
                elif direction == 'SHORT' and current_price < self._ema_20_5m * 1.005:
                    breakdown['structure'] += 1
            else:
                # No EMA available, give benefit of doubt
                breakdown['structure'] += 1
        
        # === TIMING (0-2) ===
        # Recent candle behavior
        if len(self.candles_1m) >= 3:
            recent = self.candles_1m[-3:]
            
            if direction == 'LONG':
                bullish_count = sum(1 for c in recent if c.get('close', 0) > c.get('open', 0))
                if bullish_count >= 1:
                    breakdown['timing'] += 1
                if bullish_count >= 2:
                    breakdown['timing'] += 1
            else:
                bearish_count = sum(1 for c in recent if c.get('close', 0) < c.get('open', 0))
                if bearish_count >= 1:
                    breakdown['timing'] += 1
                if bearish_count >= 2:
                    breakdown['timing'] += 1
        else:
            # Not enough candles, give 1 point
            breakdown['timing'] = 1
        
        # === MOMENTUM (0-2) ===
        # ADX and RSI
        if self._adx_1h > 15:
            breakdown['momentum'] += 1
        if self._adx_1h > 25:
            breakdown['momentum'] += 1
        
        # RSI confirmation
        if direction == 'LONG' and 30 < self._rsi_1m < 75:
            breakdown['momentum'] = max(breakdown['momentum'], 1)
        elif direction == 'SHORT' and 25 < self._rsi_1m < 70:
            breakdown['momentum'] = max(breakdown['momentum'], 1)
        
        # === CONTEXT (0-2) ===
        # Session quality and volatility
        hour = candle['timestamp'].hour
        
        # Good session hours (UTC)
        good_hours = list(range(7, 22))  # Most active hours
        if hour in good_hours:
            breakdown['context'] += 1
        
        # Volatility context - give point if we have valid ATR
        if self._atr_5m > 0 and self._atr_1m > 0:
            vol_ratio = self._atr_1m / self._atr_5m if self._atr_5m > 0 else 1
            if 0.5 < vol_ratio < 2.0:  # Normal volatility
                breakdown['context'] += 1
        else:
            # No ATR data, give 1 point
            breakdown['context'] += 1
        
        # === SYNERGY BONUS (+1) ===
        # If structure=2 AND timing>=1, add synergy
        if breakdown['structure'] >= 2 and breakdown['timing'] >= 1:
            breakdown['synergy'] = 1
        
        score = sum(breakdown.values())
        return score, breakdown
    
    def _calculate_position_size(self, entry_price: float, stop_loss: float) -> float:
        """
        Calculate position size with leverage.
        
        Formula:
        risk_usd = capital * risk_pct
        risk_per_unit = abs(entry - stop)
        size_btc = risk_usd / risk_per_unit
        
        Then apply leverage limits.
        """
        risk_usd = self.state.capital * self.config.risk_per_trade
        risk_per_unit = abs(entry_price - stop_loss)
        
        if risk_per_unit <= 0:
            return 0.0
        
        # Base size from risk
        size_btc = risk_usd / risk_per_unit
        
        # Max position based on leverage
        max_notional = self.state.capital * self.config.leverage * self.config.max_position_pct
        max_size = max_notional / entry_price
        
        # Apply minimum size (Kraken: 0.0001 BTC)
        min_size = 0.0001
        
        final_size = max(min(size_btc, max_size), min_size)
        
        # Round to 4 decimals
        return round(final_size, 4)
    
    def _check_entry_signal(self, candle: Dict) -> Optional[Dict]:
        """Check for entry signal following live logic."""
        current_price = candle['close']
        current_time = candle['timestamp']
        
        # Skip if in cooldown
        if self._cooldown_until and current_time < self._cooldown_until:
            return None
        
        # Skip if already in trade
        if self.state.current_trade:
            return None
        
        # Need minimum data (reduced requirement)
        if len(self.candles_1h) < 5 or len(self.candles_5m) < 10:
            return None
        
        # Check HTF bias
        self._htf_bias = self._check_htf_bias(current_price)
        if not self._htf_bias:
            return None
        
        # Get entry score
        direction = self._htf_bias
        score, breakdown = self._calculate_entry_score(current_price, direction, candle)
        
        # Score threshold - more permissive
        threshold = 4.0  # Lowered from 6.0 for more signals
        
        # Apply daily regime adjustment
        if self._current_regime == 'RANGE':
            threshold += 0.5
        
        if score < threshold:
            return None
        
        # === ENTRY VALID ===
        # Calculate SL and TP
        atr = self._atr_1m if self._atr_1m > 0 else 50
        
        # More realistic SL/TP based on ATR
        sl_mult = 2.0
        tp_mult = 2.0  # 1:1 R:R
        
        if direction == 'LONG':
            stop_loss = current_price - atr * sl_mult
            take_profit = current_price + atr * tp_mult
        else:
            stop_loss = current_price + atr * sl_mult
            take_profit = current_price - atr * tp_mult
        
        # Calculate size
        size = self._calculate_position_size(current_price, stop_loss)
        
        if size <= 0:
            return None
        
        return {
            'direction': direction,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'size': size,
            'score': score,
            'breakdown': breakdown,
            'atr': atr,
        }
    
    def _check_exit_conditions(self, candle: Dict) -> Optional[Tuple[float, str]]:
        """Check exit conditions for open trade."""
        trade = self.state.current_trade
        if not trade:
            return None
        
        current_price = candle['close']
        current_high = candle['high']
        current_low = candle['low']
        
        # Track MFE/MAE
        if trade.direction == 'LONG':
            mfe_price = current_high
            mae_price = current_low
            current_r = (current_price - trade.entry_price) / abs(trade.entry_price - trade.stop_loss)
            mfe_r = (mfe_price - trade.entry_price) / abs(trade.entry_price - trade.stop_loss)
            mae_r = (trade.entry_price - mae_price) / abs(trade.entry_price - trade.stop_loss)
        else:
            mfe_price = current_low
            mae_price = current_high
            current_r = (trade.entry_price - current_price) / abs(trade.entry_price - trade.stop_loss)
            mfe_r = (trade.entry_price - mfe_price) / abs(trade.entry_price - trade.stop_loss)
            mae_r = (mae_price - trade.entry_price) / abs(trade.entry_price - trade.stop_loss)
        
        trade.mfe_r = max(trade.mfe_r, mfe_r)
        trade.mae_r = max(trade.mae_r, mae_r)
        
        # === STOP LOSS ===
        if trade.direction == 'LONG':
            if current_low <= trade.stop_loss:
                return (trade.stop_loss, 'STOP_LOSS')
        else:
            if current_high >= trade.stop_loss:
                return (trade.stop_loss, 'STOP_LOSS')
        
        # === TAKE PROFIT ===
        if trade.direction == 'LONG':
            if current_high >= trade.take_profit:
                return (trade.take_profit, 'TAKE_PROFIT')
        else:
            if current_low <= trade.take_profit:
                return (trade.take_profit, 'TAKE_PROFIT')
        
        # === TIME INVALIDATION ===
        duration_minutes = (candle['timestamp'] - trade.entry_time).total_seconds() / 60
        
        if duration_minutes > 8:  # 8 candles
            # Exit if not in profit
            if current_r < 0.3:  # Less than 0.3R profit
                if current_r < -0.30:
                    return (current_price, 'TIME_INVALIDATION_MAX_LOSS')
                return (current_price, 'TIME_INVALIDATION')
        
        # === EMERGENCY R LIMIT ===
        if current_r < -0.35:
            return (current_price, 'EMERGENCY_R_LIMIT')
        
        return None
    
    def _process_candle(self, candle: Dict):
        """Process a single candle."""
        # Skip invalid candles
        if np.isnan(candle.get('close', 0)) or candle.get('close', 0) <= 0:
            return
        
        # Fix invalid OHLC
        if candle['high'] < candle['low']:
            candle['high'], candle['low'] = candle['low'], candle['high']
        
        # Update indicators
        self._update_indicators(candle)
        
        # Debug logging every 1000 candles
        candle_num = len(self.state.equity_curve)
        if candle_num % 1000 == 0 and candle_num > 0:
            logger.debug(f"  Candle #{candle_num} | Price: {candle['close']:.0f} | "
                        f"EMA50_1h: {self._ema_50_1h:.0f} | HTF bias: {self._htf_bias} | "
                        f"1h candles: {len(self.candles_1h)} | 5m candles: {len(self.candles_5m)}")
        
        # Check for exit first
        if self.state.current_trade:
            exit_result = self._check_exit_conditions(candle)
            if exit_result:
                exit_price, exit_reason = exit_result
                self._close_trade(exit_price, exit_reason, candle['timestamp'])
        
        # Check for entry
        if not self.state.current_trade:
            entry_signal = self._check_entry_signal(candle)
            if entry_signal:
                self._open_trade(entry_signal, candle['timestamp'])
        
        # Update equity curve
        equity = self.state.capital
        if self.state.current_trade:
            trade = self.state.current_trade
            if trade.direction == 'LONG':
                unrealized = (candle['close'] - trade.entry_price) * trade.size
            else:
                unrealized = (trade.entry_price - candle['close']) * trade.size
            equity += unrealized
        
        self.state.equity_curve.append(equity)
        
        # Track drawdown
        if equity > self.state.highest_equity:
            self.state.highest_equity = equity
        
        current_dd = (self.state.highest_equity - equity) / self.state.highest_equity * 100
        self.state.max_drawdown = max(self.state.max_drawdown, current_dd)
    
    def _open_trade(self, signal: Dict, timestamp: datetime):
        """Open a new trade."""
        trade = Trade(
            entry_time=timestamp,
            entry_price=signal['entry_price'],
            direction=signal['direction'],
            size=signal['size'],
            stop_loss=signal['stop_loss'],
            take_profit=signal['take_profit'],
            entry_score=signal['score'],
            entry_tags=signal['breakdown'],
        )
        
        self.state.current_trade = trade
        self.state.total_trades += 1
        
        logger.debug(f"📊 OPEN {trade.direction} @ {trade.entry_price:.0f} | Size: {trade.size:.4f} | Score: {trade.entry_score}")
    
    def _close_trade(self, exit_price: float, exit_reason: str, timestamp: datetime):
        """Close the current trade."""
        trade = self.state.current_trade
        if not trade:
            return
        
        trade.exit_time = timestamp
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.calculate_pnl(exit_price)
        
        # Update capital
        self.state.capital += trade.pnl_usd
        self.state.total_pnl += trade.pnl_usd
        
        # Track win/loss
        if trade.pnl_usd > 0:
            self.state.winning_trades += 1
            self.state.consecutive_losses = 0
        else:
            self.state.losing_trades += 1
            self.state.consecutive_losses += 1
        
        # Apply loss streak cooldown
        if self.state.consecutive_losses >= 3:
            cooldown_minutes = {3: 15, 4: 60, 5: 180}.get(self.state.consecutive_losses, 180)
            self._cooldown_until = timestamp + timedelta(minutes=cooldown_minutes)
            logger.info(f"⏸️ Loss streak cooldown: {cooldown_minutes} min")
        
        # Save trade
        self.state.trades.append(trade)
        self.state.current_trade = None
        
        emoji = "✅" if trade.pnl_usd > 0 else "❌"
        logger.debug(f"{emoji} CLOSE {trade.direction} | {exit_reason} | PnL: ${trade.pnl_usd:.2f} ({trade.pnl_r:.2f}R)")
    
    def run(self, df: pd.DataFrame) -> Dict:
        """Run the backtest on the dataset."""
        logger.info(f"Starting backtest with {len(df)} candles...")
        logger.info(f"Initial capital: €{self.config.initial_capital} | Leverage: {self.config.leverage}x")
        
        for idx, row in df.iterrows():
            candle = row.to_dict()
            self._process_candle(candle)
            
            # Progress log every 5000 candles
            if idx > 0 and idx % 5000 == 0:
                logger.info(f"  Processed {idx}/{len(df)} | Trades: {self.state.total_trades} | Capital: €{self.state.capital:.2f}")
        
        # Force close any open trade
        if self.state.current_trade:
            last_candle = df.iloc[-1]
            self._close_trade(last_candle['close'], 'BACKTEST_END', last_candle['timestamp'])
        
        return self._generate_report()
    
    def _generate_report(self) -> Dict:
        """Generate backtest report."""
        trades = self.state.trades
        
        if not trades:
            return {'error': 'No trades executed'}
        
        # Calculate metrics
        pnls = [t.pnl_usd for t in trades]
        pnl_rs = [t.pnl_r for t in trades]
        
        win_rate = self.state.winning_trades / self.state.total_trades * 100 if self.state.total_trades > 0 else 0
        
        avg_win = np.mean([p for p in pnls if p > 0]) if self.state.winning_trades > 0 else 0
        avg_loss = np.mean([p for p in pnls if p < 0]) if self.state.losing_trades > 0 else 0
        
        profit_factor = abs(sum(p for p in pnls if p > 0) / sum(p for p in pnls if p < 0)) if sum(p for p in pnls if p < 0) != 0 else float('inf')
        
        # Sharpe ratio (annualized)
        if len(pnl_rs) > 1 and np.std(pnl_rs) > 0:
            sharpe = (np.mean(pnl_rs) / np.std(pnl_rs)) * np.sqrt(252)
        else:
            sharpe = 0
        
        # Exit reason breakdown
        exit_reasons = {}
        for t in trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
        
        # Score distribution analysis
        score_perf = {}
        for t in trades:
            bucket = f"{int(t.entry_score)}-{int(t.entry_score) + 1}"
            if bucket not in score_perf:
                score_perf[bucket] = {'trades': 0, 'wins': 0, 'pnl': 0}
            score_perf[bucket]['trades'] += 1
            score_perf[bucket]['pnl'] += t.pnl_usd
            if t.pnl_usd > 0:
                score_perf[bucket]['wins'] += 1
        
        report = {
            'summary': {
                'initial_capital': self.config.initial_capital,
                'final_capital': round(self.state.capital, 2),
                'total_return_pct': round((self.state.capital - self.config.initial_capital) / self.config.initial_capital * 100, 2),
                'total_pnl': round(self.state.total_pnl, 2),
                'max_drawdown_pct': round(self.state.max_drawdown, 2),
            },
            'trades': {
                'total': self.state.total_trades,
                'winners': self.state.winning_trades,
                'losers': self.state.losing_trades,
                'win_rate_pct': round(win_rate, 2),
            },
            'risk_metrics': {
                'avg_win_usd': round(avg_win, 2),
                'avg_loss_usd': round(avg_loss, 2),
                'profit_factor': round(profit_factor, 2),
                'sharpe_ratio': round(sharpe, 2),
                'avg_r': round(np.mean(pnl_rs), 3),
                'max_r': round(max(pnl_rs), 3),
                'min_r': round(min(pnl_rs), 3),
            },
            'exit_reasons': exit_reasons,
            'score_performance': score_perf,
        }
        
        return report


# =============================================================================
# MAIN
# =============================================================================
def main():
    """Run massive mock backtest."""
    print("=" * 70)
    print("🚀 MASSIVE MOCK BACKTEST - STRESS TESTING")
    print("=" * 70)
    
    # Configuration
    config = BacktestConfig(
        initial_capital=300.0,
        leverage=10,
        num_days=30,
        include_black_swan=True,
        include_edge_cases=True,
    )
    
    print(f"\n📋 Configuration:")
    print(f"   Initial Capital: €{config.initial_capital}")
    print(f"   Leverage: {config.leverage}x")
    print(f"   Days: {config.num_days}")
    print(f"   Expected candles: {config.num_days * config.candles_per_day:,}")
    
    # Generate data
    print("\n" + "=" * 70)
    print("📊 GENERATING MOCK DATA")
    print("=" * 70)
    
    generator = RealisticBTCGenerator(config)
    df = generator.generate_dataset()
    df = generator.add_edge_cases(df)
    
    # Run backtest
    print("\n" + "=" * 70)
    print("🏃 RUNNING BACKTEST")
    print("=" * 70)
    
    backtester = MassiveBacktester(config)
    report = backtester.run(df)
    
    # Print results
    print("\n" + "=" * 70)
    print("📈 BACKTEST RESULTS")
    print("=" * 70)
    
    if 'error' in report:
        print(f"\n❌ ERROR: {report['error']}")
        print("=" * 70)
        return report
    
    print("\n📊 SUMMARY:")
    for key, value in report['summary'].items():
        print(f"   {key}: {value}")
    
    print("\n📈 TRADES:")
    for key, value in report['trades'].items():
        print(f"   {key}: {value}")
    
    print("\n⚠️ RISK METRICS:")
    for key, value in report['risk_metrics'].items():
        print(f"   {key}: {value}")
    
    print("\n🚪 EXIT REASONS:")
    for reason, count in report.get('exit_reasons', {}).items():
        pct = count / report['trades']['total'] * 100 if report['trades']['total'] > 0 else 0
        print(f"   {reason}: {count} ({pct:.1f}%)")
    
    print("\n📊 SCORE PERFORMANCE:")
    for bucket, perf in report.get('score_performance', {}).items():
        wr = perf['wins'] / perf['trades'] * 100 if perf['trades'] > 0 else 0
        print(f"   Score {bucket}: {perf['trades']} trades | {wr:.1f}% WR | ${perf['pnl']:.2f} PnL")
    
    # Save results
    results_file = Path(__file__).parent.parent / 'logs' / 'backtest_results.json'
    results_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(results_file, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    print(f"\n💾 Results saved to: {results_file}")
    
    # Final verdict
    print("\n" + "=" * 70)
    
    if 'error' in report:
        print("❌ BACKTEST FAILED - No trades executed")
        print("=" * 70)
        return report
    
    final_return = report['summary']['total_return_pct']
    max_dd = report['summary']['max_drawdown_pct']
    win_rate = report['trades']['win_rate_pct']
    
    if final_return > 0 and max_dd < 30 and win_rate > 45:
        print("✅ BACKTEST PASSED - System shows positive expectancy")
    elif final_return > 0:
        print("⚠️ BACKTEST MARGINAL - Positive but with concerns")
    else:
        print("❌ BACKTEST FAILED - Negative expectancy detected")
    
    print("=" * 70)
    
    return report


if __name__ == "__main__":
    main()
