"""
Regime Classifier - Converts ADX from gate to mode selector

⚠️ PARTIAL DEPRECATION (Feb 05, 2026):
RegimeConfig defined here is a DUPLICATE. 
Prefer using UnifiedRegimeConfig from src/config/unified_constants.py
This file's RegimeConfig is kept for backward compatibility but should be migrated.

ADX should NOT be a binary filter. It's a REGIME CLASSIFIER.

MODES:
- TREND (ADX >= 18): Full trend-following, wider stops, bigger targets
- RANGE (12 <= ADX < 18): Mean reversion, tighter RR, smaller targets, fewer trades
- NO_TRADE (ADX < 12 AND bad liquidity): Only skip if spreads/liquidity are bad

KEY INSIGHT:
ADX bajo ≠ mercado inoperable
ADX bajo = rango / compresión / transición

These are contexts where mean reversion and first expansion legs work BEST.

Created: January 22, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime
import logging

from src.shared.indicators import calculate_adx, calculate_atr

# 🔥 NEW (Feb 05): Import unified config for future migration
from src.config.unified_constants import UnifiedRegimeConfig, UNIFIED_REGIMES, get_unified_regime

logger = logging.getLogger(__name__)


# ⚠️ LEGACY (Feb 05): This RegimeConfig is LOCAL to this file.
# For new code, prefer using UnifiedRegimeConfig from unified_constants.py
@dataclass
class RegimeConfig:
    """Configuration for each trading regime - LOCAL LEGACY VERSION"""
    mode: str  # 'TREND', 'RANGE', 'NO_TRADE'
    
    # Position sizing
    size_multiplier: float = 1.0
    
    # Entry restrictions
    allow_trend_entries: bool = True
    allow_ct_entries: bool = False       # Counter-trend
    allow_range_entries: bool = False    # Mean reversion
    
    # Risk adjustments
    min_rr_net: float = 1.5              # Minimum R:R after fees/slippage
    max_trades_per_session: int = 10
    
    # Exit adjustments
    tp_multiplier: float = 1.0           # TP target adjustment
    sl_tighter: bool = False             # Use tighter stops
    trailing_aggressive: bool = False     # More aggressive trailing


# Regime configurations
# Adjusted for $250 capital - more selective, structured limits
REGIME_CONFIGS: Dict[str, RegimeConfig] = {
    'TREND': RegimeConfig(
        mode='TREND',
        size_multiplier=1.0,
        allow_trend_entries=True,
        allow_ct_entries=False,
        allow_range_entries=False,
        min_rr_net=1.3,                   # 🔥 Raised from 1.5 (net of fees)
        max_trades_per_session=4,         # 🔥 Reduced from 10 (quality over quantity)
        tp_multiplier=1.0,
        sl_tighter=False,
        trailing_aggressive=False
    ),
    'RANGE': RegimeConfig(
        mode='RANGE',
        size_multiplier=0.6,              # 🔥 Reduced from 0.7
        allow_trend_entries=False,        # No trend-following in range
        allow_ct_entries=True,            # Allow counter-trend scalps
        allow_range_entries=True,         # Mean reversion allowed
        min_rr_net=1.5,                   # 🔥 Was 2.0 (too restrictive)
        max_trades_per_session=3,         # 🔥 Reduced from 5
        tp_multiplier=0.7,                # Shorter targets
        sl_tighter=True,                  # Tighter stops
        trailing_aggressive=True          # Lock profits faster
    ),
    'NO_TRADE': RegimeConfig(
        mode='NO_TRADE',
        size_multiplier=0.0,
        allow_trend_entries=False,
        allow_ct_entries=False,
        allow_range_entries=False,
        min_rr_net=999,
        max_trades_per_session=0,
        tp_multiplier=0.0,
        sl_tighter=True,
        trailing_aggressive=True
    )
}


class RegimeClassifier:
    """
    Classifies market regime based on ADX and other conditions.
    
    This replaces the binary ADX filter with a multi-mode system that:
    1. Never blocks based on ADX alone (unless extreme low ADX + bad liquidity)
    2. Adapts trading style to the regime
    3. Allows profitable range trading with appropriate restrictions
    
    Lowered thresholds for more participation
    With more capital, want to catch moderate trends, not just perfect ones.
    """
    
    # ADX thresholds for regime classification
    # 🔥 FIX: Lowered from 18/12 to 16/11 for more trend participation
    TREND_THRESHOLD = 16.0      # ADX >= 16 → TREND mode (was 18)
    RANGE_THRESHOLD = 11.0      # 11 <= ADX < 16 → RANGE mode (was 12)
    # ADX < 11 → only NO_TRADE if liquidity is bad
    
    # Liquidity thresholds for NO_TRADE decision
    MIN_SPREAD_BPS = 15.0       # Max acceptable spread in basis points
    
    def __init__(self):
        self._current_regime: str = 'TREND'
        self._regime_since: Optional[datetime] = None
        self._last_adx: float = 0.0
        self._regime_history: list = []
    
    def classify(self,
                 df_1h: pd.DataFrame,
                 spread_bps: float = 0.0,
                 market_time: datetime | None = None) -> Tuple[str, RegimeConfig, Dict]:
        """
        Classify market regime based on ADX and liquidity.
        
        Args:
            df_1h: 1-hour OHLC data for ADX calculation
            spread_bps: Current spread in basis points (optional)
            market_time: Current market time
        
        Returns:
            (regime_name, regime_config, metadata)
        """
        metadata = {
            'adx': None,
            'adx_slope': None,
            'spread_bps': spread_bps,
            'previous_regime': self._current_regime,
            'regime_changed': False
        }
        
        # Need enough data for ADX
        if df_1h is None or len(df_1h) < 30:
            logger.debug("RegimeClassifier: Insufficient data, defaulting to TREND")
            return 'TREND', REGIME_CONFIGS['TREND'], metadata
        
        highs = df_1h['high'].to_numpy()
        lows = df_1h['low'].to_numpy()
        closes = df_1h['close'].to_numpy()
        
        # Calculate ADX
        adx_values, adx_slope = calculate_adx(highs, lows, closes, 14)
        
        if len(adx_values) == 0 or np.isnan(adx_values[-1]):
            logger.debug("RegimeClassifier: ADX calculation failed, defaulting to TREND")
            return 'TREND', REGIME_CONFIGS['TREND'], metadata
        
        adx = float(adx_values[-1])
        slope = float(adx_slope[-1]) if len(adx_slope) > 0 and not np.isnan(adx_slope[-1]) else 0.0
        
        self._last_adx = adx
        metadata['adx'] = round(adx, 2)
        metadata['adx_slope'] = round(slope, 3)
        
        # Classify regime
        new_regime: str
        
        if adx >= self.TREND_THRESHOLD:
            new_regime = 'TREND'
        elif adx >= self.RANGE_THRESHOLD:
            new_regime = 'RANGE'
        else:
            # ADX < 12: only NO_TRADE if liquidity is also bad
            if spread_bps > self.MIN_SPREAD_BPS:
                new_regime = 'NO_TRADE'
                logger.warning(f"⚠️ NO_TRADE: ADX {adx:.1f} < 12 AND spread {spread_bps:.1f}bps > {self.MIN_SPREAD_BPS}")
            else:
                # Low ADX but acceptable liquidity → still RANGE (can trade mean reversion)
                new_regime = 'RANGE'
                logger.info(f"📊 RANGE mode (low ADX {adx:.1f} but spread OK)")
        
        # Track regime changes
        if new_regime != self._current_regime:
            metadata['regime_changed'] = True
            self._regime_since = market_time
            self._regime_history.append({
                'time': market_time,
                'from': self._current_regime,
                'to': new_regime,
                'adx': adx
            })
            # Keep history bounded
            if len(self._regime_history) > 100:
                self._regime_history = self._regime_history[-50:]
            
            logger.info(f"🔄 REGIME CHANGE: {self._current_regime} → {new_regime} (ADX={adx:.1f})")
            self._current_regime = new_regime
        
        config = REGIME_CONFIGS[new_regime]
        
        # Log current state
        logger.debug(f"📊 Regime: {new_regime} | ADX={adx:.1f} | Slope={slope:.3f} | "
                    f"Trend={config.allow_trend_entries} | Range={config.allow_range_entries} | CT={config.allow_ct_entries}")
        
        return new_regime, config, metadata
    
    def get_current_regime(self) -> str:
        """Get current regime name"""
        return self._current_regime
    
    def get_current_config(self) -> RegimeConfig:
        """Get current regime configuration"""
        return REGIME_CONFIGS[self._current_regime]
    
    def get_last_adx(self) -> float:
        """Get last calculated ADX value"""
        return self._last_adx
    
    def allows_entry_type(self, entry_type: str) -> bool:
        """
        Check if current regime allows a specific entry type.
        
        Args:
            entry_type: 'trend', 'range', or 'ct' (counter-trend)
        """
        config = REGIME_CONFIGS[self._current_regime]
        
        if entry_type == 'trend':
            return config.allow_trend_entries
        elif entry_type == 'range':
            return config.allow_range_entries
        elif entry_type == 'ct':
            return config.allow_ct_entries
        else:
            return False
    
    def get_size_multiplier(self) -> float:
        """Get position size multiplier for current regime"""
        return REGIME_CONFIGS[self._current_regime].size_multiplier
    
    def get_min_rr(self) -> float:
        """Get minimum R:R requirement for current regime"""
        return REGIME_CONFIGS[self._current_regime].min_rr_net


# Singleton
_regime_classifier: Optional[RegimeClassifier] = None


def get_regime_classifier() -> RegimeClassifier:
    """Get or create the global regime classifier"""
    global _regime_classifier
    if _regime_classifier is None:
        _regime_classifier = RegimeClassifier()
    return _regime_classifier


def reset_regime_classifier():
    """Reset the regime classifier (for testing)"""
    global _regime_classifier
    _regime_classifier = None
