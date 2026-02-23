"""
Entry Scorer - Score > Gates Architecture

SIMPLIFIED (Feb 2026): Removed 15+ accumulated FIXes that contradicted each other.

Score = Structure(0-2) + Timing(0-2) + Momentum(0-2) + Context(0-2) + Synergy(0-1) = max 9
Adjustments: micro_trend_adj only (capped at -3.0 total penalty)
Weak momentum → sizing penalty (0.7x), NOT score penalty.

Only Structure=0 blocks entry (the only real gate).
Displacement/Expansion/Strong ADX (>=50) bypass the structure gate.
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List, TYPE_CHECKING
from datetime import datetime

from src.shared.indicators import (
    calculate_ema,
    calculate_adx,
    calculate_atr,
    calculate_rsi,
)
from src.config.trading_config import OperatingMode, get_session_name

if TYPE_CHECKING:
    from src.domain.market.filters import EntryFilterPipeline  # Legacy caller compat
    from src.domain.market.filters import MicroTrendState

logger = logging.getLogger(__name__)


@dataclass
class ScoreBreakdown:
    """Detailed breakdown of entry score calculation."""
    structure: int = 0
    timing: int = 0
    momentum: int = 0
    context: int = 0
    synergy: int = 0
    micro_trend_adj: float = 0.0
    adx_values: np.ndarray = field(default_factory=lambda: np.array([]))

    MAX_TOTAL_PENALTY = -3.0

    @property
    def total(self) -> int:
        """Calculate total score with penalty cap."""
        base = self.structure + self.timing + self.momentum + self.context + self.synergy
        total_penalty = self.micro_trend_adj
        capped_penalty = max(self.MAX_TOTAL_PENALTY, total_penalty)
        return max(0, int(base + capped_penalty))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            'structure': self.structure,
            'timing': self.timing,
            'momentum': self.momentum,
            'context': self.context,
            'synergy': self.synergy,
            'micro_trend_adj': self.micro_trend_adj,
            'total': self.total
        }


@dataclass
class TradeTags:
    """Tags collected during scoring for trade analysis."""
    score: int = 0
    mode: str = ""
    htf_aligned: bool = False
    ltf_clean: bool = False
    bos_confirmed: bool = False
    ema_reclaim: bool = False
    adx: float = 0.0
    adx_5m: float = 0.0
    adx_slope: float = 0.0
    adx_slope_bucket: str = ""
    rsi: float = 0.0
    atr: float = 0.0
    session: str = "unknown"
    session_quality: str = ""
    volatility_ratio: float = 0.0
    rel_vol: float = 1.0
    volume_penalty: float = 0.0
    slope_penalty: float = 0.0
    # BOS Quality tracking
    bos_quality: str = "UNKNOWN"
    bos_body_atr: float = 0.0
    bos_volume_ratio: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {k: v for k, v in self.__dict__.items()}


class EntryScorer:
    """
    Entry Score System - Score > Gates architecture.

    Calculates entry quality score: Structure(0-2) + Timing(0-2) +
    Momentum(0-2) + Context(0-2) + Synergy(0-1) = max 9.
    Only Structure=0 blocks entry (the only real gate).
    """

    def __init__(self):
        self._breakdown = ScoreBreakdown()
        self._trade_tags = TradeTags()

    @property
    def breakdown(self) -> ScoreBreakdown:
        """Get last score breakdown."""
        return self._breakdown

    @property
    def trade_tags(self) -> TradeTags:
        """Get last trade tags."""
        return self._trade_tags

    def calculate_entry_score(
        self,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        df_1m: pd.DataFrame,
        bias: str,
        current_price: float,
        market_time: datetime,
        micro_trend_state: Optional['MicroTrendState'] = None,
        displacement_edge_active: bool = False,
        expansion_mode_active: bool = False,
        strong_trend_adx: float = 0.0,
        # Legacy params kept for caller compatibility (ignored)
        filter_pipeline: Optional['EntryFilterPipeline'] = None,
        bias_inverted: bool = False,
        bias_invalidated: bool = False,
        impulse_start_price: Optional[float] = None,
        impulse_age_bars: int = 0,
    ) -> Tuple[int, Dict[str, Any], bool]:
        """
        Calculate entry score using 4 blocks + synergy.

        Returns:
            (total_score, breakdown_dict, structure_blocks)
            If structure_blocks=True, DO NOT ENTER regardless of score.
        """
        # Reset state
        self._breakdown = ScoreBreakdown()
        self._trade_tags = TradeTags(mode=OperatingMode.MODE)

        # 1️⃣ STRUCTURE (0-2) — THE ONLY GATE
        structure_score, structure_blocks, htf_clean = self._score_structure(
            df_1h, df_5m, bias, current_price
        )

        # Structure gate bypasses for validated edge signals
        STRUCTURE_ADX_OVERRIDE = 50.0
        if structure_blocks and displacement_edge_active:
            logger.info(f"🌊 DISPLACEMENT BYPASS: {bias} structure gate bypassed")
            structure_blocks = False
            structure_score = 1

        if structure_blocks and expansion_mode_active:
            logger.info(f"📈 EXPANSION BYPASS: {bias} structure gate bypassed")
            structure_blocks = False
            structure_score = 1

        if structure_blocks and strong_trend_adx >= STRUCTURE_ADX_OVERRIDE:
            logger.info(f"🔥 STRONG ADX BYPASS: {bias} ADX={strong_trend_adx:.1f} >= {STRUCTURE_ADX_OVERRIDE}")
            structure_blocks = False
            structure_score = 1

        self._breakdown.structure = structure_score
        self._trade_tags.htf_aligned = structure_score > 0
        self._trade_tags.ltf_clean = structure_score == 2

        # Structure gate: if blocked and no bypass, HARD BLOCK entry
        if structure_blocks:
            self._trade_tags.score = 0
            return 0, self._breakdown.to_dict(), True

        # 2️⃣ TIMING (0-2)
        timing_score, bos_confirmed = self._score_timing(df_1m, bias)

        # Coherence: timing capped to 1 if structure = 0
        if self._breakdown.structure == 0:
            timing_score = min(timing_score, 1)

        self._breakdown.timing = timing_score
        self._trade_tags.bos_confirmed = bos_confirmed

        # 3️⃣ MOMENTUM (0-2)
        self._breakdown.momentum = self._score_momentum(df_5m, bias)

        # Coherence: weak momentum + weak structure = reduce structure
        if self._breakdown.momentum == 0 and self._breakdown.structure < 2:
            self._breakdown.structure = max(0, self._breakdown.structure - 1)

        # 4️⃣ CONTEXT (0-2)
        self._breakdown.context = self._score_context(df_5m, market_time)

        # 5️⃣ SYNERGY BONUS (0-1)
        if htf_clean and bos_confirmed:
            self._breakdown.synergy = 1

        # MOMENTUM COHERENCE: zero momentum reduces sizing, not score.
        # ADX naturally dips during pullbacks (consolidation before continuation).
        # The -2.0 penalty killed ALL pullback entries: Structure=1, Timing=2,
        # Momentum=0, Context=2 → raw 5 minus 2 = 3 → far below 5.0 floor.
        # Instead, let the sizing system apply a 0.7x multiplier for weak momentum.
        # This keeps the trade alive for validation while reducing risk.

        # 6️⃣ MICRO TREND ADJUSTMENT (applied directly)
        if micro_trend_state is not None:
            micro_adj = micro_trend_state.score_adjustment
            if micro_adj != 0:
                self._breakdown.micro_trend_adj = micro_adj

        # Final score (MAX_TOTAL_PENALTY cap applied inside ScoreBreakdown.total)
        self._trade_tags.score = self._breakdown.total
        return self._breakdown.total, self._breakdown.to_dict(), False

    def _score_structure(
        self,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        bias: str,
        current_price: float
    ) -> Tuple[int, bool, bool]:
        """
        STRUCTURE SCORE (0-2)
        +2 → HTF aligned + 5m structure clean
        +1 → HTF ok but LTF structure partial
        0 → Against structure → BLOCKS ENTRY

        Uses zone-based approach:
        - LONG: price > EMA50 (uptrend) or near EMA50 with bullish slope (dip buy)
        - SHORT: price > EMA50 (sell high!) or near EMA50 with bearish slope (breakdown)

        Returns: (score, blocks_entry, htf_is_clean)
        """
        if len(df_1h) < 50 or len(df_5m) < 20:
            return 1, False, False

        # HTF alignment check
        closes_1h = df_1h['close'].to_numpy()
        ema50_1h = calculate_ema(closes_1h, 50)

        if np.isnan(ema50_1h[-1]):
            return 1, False, False

        # EMA50 slope (change over last 3 hours)
        ema50_slope = 0.0
        if len(ema50_1h) >= 4:
            ema50_slope = (ema50_1h[-1] - ema50_1h[-4]) / ema50_1h[-4] * 100

        # Dynamic proximity tolerance using ATR (adapts to volatility)
        # Fixed 0.3% was too tight in volatile markets, too loose in quiet ones.
        # Using 1H ATR: ~$400 → 0.4%, ~$200 → 0.2%, ~$800 → 0.8%
        highs_1h = df_1h['high'].to_numpy()
        lows_1h = df_1h['low'].to_numpy()
        atr_1h_vals = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
        if len(atr_1h_vals) > 0 and not np.isnan(atr_1h_vals[-1]):
            atr_1h = atr_1h_vals[-1]
            PROXIMITY_TOLERANCE = atr_1h / ema50_1h[-1]  # ~0.3-0.8% dynamically
        else:
            PROXIMITY_TOLERANCE = 0.005  # Fallback 0.5%
        PROXIMITY_TOLERANCE = max(0.003, min(0.010, PROXIMITY_TOLERANCE))  # Clamp 0.3%–1.0%

        price_distance_pct = (current_price - ema50_1h[-1]) / ema50_1h[-1]
        is_near_ema50 = abs(price_distance_pct) <= PROXIMITY_TOLERANCE

        htf_aligned = False
        htf_slope_override = False

        if bias == 'LONG':
            # LONG: buy low, sell high
            htf_aligned = current_price > ema50_1h[-1]

            if not htf_aligned:
                # Near EMA50 with positive slope → dip buy in uptrend
                if is_near_ema50 and ema50_slope > 0:
                    htf_aligned = True
                    htf_slope_override = True
                # Very strong upward slope
                elif ema50_slope > 0.05:
                    htf_aligned = True
                    htf_slope_override = True
                # Within 5% below EMA50 → reversal / pullback zone
                elif price_distance_pct >= -0.05:
                    htf_aligned = True
                    htf_slope_override = True
        else:
            # SHORT: sell high, buy back low
            if current_price > ema50_1h[-1]:
                htf_aligned = True  # SELL FROM STRENGTH
            elif is_near_ema50 and ema50_slope < 0:
                htf_aligned = True  # Breakdown setup
                htf_slope_override = True
            elif ema50_slope < -0.02:
                htf_aligned = True  # Active downtrend
                htf_slope_override = True
            elif price_distance_pct < -0.04 and ema50_slope >= -0.02:
                htf_aligned = False  # TOO LATE: extended AND momentum fading
            else:
                # Below EMA50 with non-bearish slope = NOT a SHORT setup
                # (Pullback in uptrend or flat range → wrong direction)
                htf_aligned = ema50_slope <= 0

        if not htf_aligned:
            # Extreme distance bypass: EMA50 is stale during crash/recovery
            EXTREME_DISTANCE_THRESHOLD = 0.07
            if abs(price_distance_pct) > EXTREME_DISTANCE_THRESHOLD:
                logger.warning(
                    f"🔥 EXTREME DISTANCE BYPASS: EMA50 at {price_distance_pct:.1%} is STALE | "
                    f"{bias} allowed"
                )
                return 1, False, False

            logger.warning(
                f"⛔ STRUCTURE GATE: {bias} against HTF | "
                f"Price: {current_price:.0f}, EMA50: {ema50_1h[-1]:.0f}, "
                f"Distance: {price_distance_pct:.2%}, Slope: {ema50_slope:.3f}%"
            )
            return 0, True, False

        # 5m structure check (price vs EMA20)
        closes_5m = df_5m['close'].to_numpy()
        ema20_5m = calculate_ema(closes_5m, 20)

        if np.isnan(ema20_5m[-1]):
            return 1, False, htf_aligned

        ltf_clean = False
        if bias == 'LONG':
            ltf_clean = current_price >= ema20_5m[-1] * 0.998
        else:
            ltf_clean = current_price <= ema20_5m[-1] * 1.002

        if htf_aligned and ltf_clean:
            return 2, False, True
        elif htf_aligned:
            return 1, False, False

        return 0, True, False

    def _score_timing(
        self,
        df_1m: pd.DataFrame,
        bias: str
    ) -> Tuple[int, bool]:
        """
        TIMING SCORE (0-2)
        +2 → BOS confirmed at CLOSE + clean reclaim + STRONG BOS
        +1 → BOS + late timing OR weak BOS
        0 → Signal intra-candle / ambiguous

        BOS Quality: body vs ATR, volume vs avg, close position.
        STRONG = 3/3 signals, NORMAL = 1-2/3, WEAK = 0/3.

        Returns: (score, bos_confirmed)
        """
        if len(df_1m) < 10:
            return 1, False

        last_closed = df_1m.iloc[-2] if len(df_1m) >= 2 else df_1m.iloc[-1]
        prev_candles = df_1m.iloc[-7:-2] if len(df_1m) >= 7 else df_1m.iloc[:-1]

        if len(prev_candles) < 3:
            return 1, False

        recent_high = prev_candles['high'].max()
        recent_low = prev_candles['low'].min()

        bos_confirmed = False
        clean_close = False

        # ATR for reference
        ranges = (df_1m['high'] - df_1m['low']).tail(20)
        atr_1m = ranges.mean() if len(ranges) >= 10 else ranges.iloc[-1]

        # Average volume
        volumes = df_1m['volume'].tail(20)
        avg_volume = volumes.mean() if len(volumes) >= 10 else 1.0

        # BOS candle metrics
        bos_body = abs(last_closed['close'] - last_closed['open'])
        bos_range = last_closed['high'] - last_closed['low']
        bos_volume = last_closed.get('volume', avg_volume)

        body_atr_ratio = bos_body / atr_1m if atr_1m > 0 else 0
        volume_ratio = bos_volume / avg_volume if avg_volume > 0 else 1.0

        if bias == 'LONG':
            bos_confirmed = last_closed['close'] > recent_high
            if bos_range > 0:
                close_position = (last_closed['close'] - last_closed['low']) / bos_range
                clean_close = close_position > 0.6
        else:
            bos_confirmed = last_closed['close'] < recent_low
            if bos_range > 0:
                close_position = (last_closed['high'] - last_closed['close']) / bos_range
                clean_close = close_position > 0.6

        # BOS Quality classification
        bos_quality = 'WEAK'
        if bos_confirmed:
            quality_signals = 0
            if body_atr_ratio >= 0.8:
                quality_signals += 1
            if volume_ratio >= 1.2:
                quality_signals += 1
            if clean_close:
                quality_signals += 1

            if quality_signals >= 3:
                bos_quality = 'STRONG'
            elif quality_signals >= 1:
                bos_quality = 'NORMAL'

            self._trade_tags.bos_quality = bos_quality
            self._trade_tags.bos_body_atr = body_atr_ratio
            self._trade_tags.bos_volume_ratio = volume_ratio

        # Scoring with BOS quality
        if bos_confirmed and clean_close:
            if bos_quality == 'WEAK':
                return 1, True
            return 2, True
        elif bos_confirmed:
            return 1, True

        # Check intra-candle BOS
        current = df_1m.iloc[-1]
        if bias == 'LONG':
            intra_bos = current['high'] > recent_high
        else:
            intra_bos = current['low'] < recent_low

        if intra_bos:
            return 1, False

        return 0, False

    def _score_momentum(
        self,
        df_5m: pd.DataFrame,
        bias: str
    ) -> int:
        """
        MOMENTUM SCORE (0-2)
        +2 → ADX strong + slope favorable + volume OK
        +1 → Moderate trend
        0 → Weak momentum (but does NOT block!)
        """
        if len(df_5m) < 30:
            return 1

        closes = df_5m['close'].to_numpy()
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()

        adx, _ = calculate_adx(highs, lows, closes, 14)

        if np.isnan(adx[-1]):
            return 1

        adx_val = adx[-1]

        # ADX slope
        adx_slope = 0.0
        if len(adx) >= 5 and not np.isnan(adx[-5]):
            adx_slope = adx[-1] - adx[-5]

        # Store in trade tags
        self._trade_tags.adx_5m = float(adx_val)
        self._trade_tags.adx = float(adx_val)
        self._trade_tags.adx_slope = float(adx_slope)

        # Store ADX array for regime detection
        adx_values = adx[-10:] if len(adx) >= 10 else adx
        adx_values = adx_values[~np.isnan(adx_values)]
        self._breakdown.adx_values = adx_values

        # ADX slope bucket
        if adx_slope < -0.5:
            self._trade_tags.adx_slope_bucket = 'declining_fast'
        elif adx_slope < -0.15:
            self._trade_tags.adx_slope_bucket = 'declining'
        elif adx_slope < 0.15:
            self._trade_tags.adx_slope_bucket = 'neutral'
        else:
            self._trade_tags.adx_slope_bucket = 'rising'

        # RSI
        rsi = calculate_rsi(closes, 14)
        if not np.isnan(rsi[-1]):
            self._trade_tags.rsi = float(rsi[-1])

        # Base score
        from src.config.trading_config import ADXThresholds
        base_score = 0.0
        if ADXThresholds.is_strong_trend(adx_val) and adx_slope > 0:
            base_score = 2.0
        elif ADXThresholds.is_trending(adx_val) or (adx_val > 15 and adx_slope > 0):
            base_score = 1.0

        # Degradation penalties
        penalty = 0.0
        rel_vol = self._trade_tags.rel_vol
        if rel_vol < 0.6:
            penalty += 0.5
            self._trade_tags.volume_penalty = 0.5

        if adx_slope < -0.5:
            penalty += 0.25
            self._trade_tags.slope_penalty = 0.25

        final_score = max(0, base_score - penalty)
        return int(round(final_score))

    def _score_context(
        self,
        df_5m: pd.DataFrame,
        market_time: datetime
    ) -> int:
        """
        CONTEXT SCORE (0-2)
        +1 → Session London/NY
        +1 → Volatility healthy (ATR fast ≈ slow)
        """
        score = 0

        # Session check
        hour = market_time.hour
        minute = market_time.minute
        day_of_week = market_time.weekday()
        session_name = get_session_name(hour, minute, day_of_week)

        if session_name in ['LONDON', 'NY', 'LONDON_NY_OVERLAP']:
            score += 1
            self._trade_tags.session = 'active'
        elif session_name == 'ASIA':
            self._trade_tags.session = 'asia'
        else:
            self._trade_tags.session = 'quiet'

        self._trade_tags.session_quality = session_name  # Store name for analysis; quality tier set in entry_validators

        # Volatility check
        if len(df_5m) >= 50:
            highs = df_5m['high'].to_numpy()
            lows = df_5m['low'].to_numpy()
            closes = df_5m['close'].to_numpy()

            atr_fast = calculate_atr(highs, lows, closes, 14)
            atr_slow = calculate_atr(highs, lows, closes, 42)

            if not np.isnan(atr_fast[-1]) and not np.isnan(atr_slow[-1]):
                ratio = atr_fast[-1] / atr_slow[-1] if atr_slow[-1] > 0 else 1.0
                self._trade_tags.volatility_ratio = float(ratio)
                self._trade_tags.atr = float(atr_fast[-1])
                if 0.7 <= ratio <= 1.5:
                    score += 1

        return score

    def get_score_size_multiplier(self, score: int) -> float:
        """Score-based position sizing multiplier."""
        if score >= 7:
            return 1.2
        elif score >= 6:
            return 1.0
        else:
            return 0.8


# Singleton instance
_entry_scorer: Optional[EntryScorer] = None


def get_entry_scorer() -> EntryScorer:
    """Get global EntryScorer instance."""
    global _entry_scorer
    if _entry_scorer is None:
        _entry_scorer = EntryScorer()
    return _entry_scorer
