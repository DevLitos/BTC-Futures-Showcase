"""
Intelligent Filter System - Multi-Layer Decision Making

🧠 PHILOSOPHY:
- NO hay "entering anyway" - si no es suficiente, NO ENTRAR
- Filtros NO son binarios: son SCORES que se ACUMULAN
- Combinación de varios "amarillos" = "rojo"
- El contexto determina la severidad

LAYERS:
1. HARD BLOCKS (Red) - Instant rejection, no negotiation
2. SOFT PENALTIES (Yellow) - Accumulate, combined effect
3. CONTEXT MODIFIERS - Adjust severity based on situation
4. FINAL DECISION - Computed from accumulated state

Created: January 24, 2026
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


class FilterSeverity(Enum):
    """Severity levels for filter results"""
    GREEN = "GREEN"       # No issue
    YELLOW = "YELLOW"     # Warning, affects sizing
    ORANGE = "ORANGE"     # Serious, heavily reduced
    RED = "RED"           # Hard block


class FilterCategory(Enum):
    """Categories of filters"""
    MARKET_QUALITY = "MARKET"      # Spread, feed, volatility
    SESSION_QUALITY = "SESSION"    # ADX, time, day
    SIGNAL_QUALITY = "SIGNAL"      # Structure, microtrend, chase
    EXECUTION_QUALITY = "EXECUTION"  # Latency, slippage, fills
    SYSTEM_HEALTH = "SYSTEM"       # Timeouts, errors


@dataclass
class FilterResult:
    """Result from a single filter check"""
    name: str
    category: FilterCategory
    severity: FilterSeverity
    penalty: float  # 0.0 = no effect, 1.0 = full block
    reason: str
    context: Dict = field(default_factory=dict)
    
    def __str__(self):
        emoji = {"GREEN": "✅", "YELLOW": "⚠️", "ORANGE": "🔶", "RED": "⛔"}[self.severity.value]
        return f"{emoji} {self.name}: {self.reason} (penalty={self.penalty:.0%})"


@dataclass
class IntelligentFilterDecision:
    """Final decision from intelligent filter system"""
    allow_entry: bool
    final_size_multiplier: float  # 0.0 to 1.0
    require_limit_order: bool
    sl_tightening_factor: float  # 1.0 = normal, 0.8 = 20% tighter
    
    # Decision explanation
    decision_reason: str
    accumulated_penalty: float
    active_filters: List[FilterResult]
    
    # Categorized penalties
    penalties_by_category: Dict[str, float] = field(default_factory=dict)
    
    def __str__(self):
        status = "✅ ALLOW" if self.allow_entry else "⛔ BLOCK"
        return (
            f"{status} | Size: {self.final_size_multiplier:.0%} | "
            f"Penalty: {self.accumulated_penalty:.0%} | "
            f"Reason: {self.decision_reason}"
        )


class IntelligentFilter:
    """
    Multi-layer intelligent filtering system.
    
    Unlike binary filters, this system:
    1. Accumulates penalties from multiple sources
    2. Considers context when applying penalties
    3. Blocks based on COMBINATION of factors
    4. Provides gradual sizing adjustments
    
    CRITICAL RULE: 3 yellows = 1 red (combination block)
    """
    
    # =========================================================================
    # THRESHOLDS
    # =========================================================================
    
    # Penalty accumulation threshold for blocking
    BLOCK_PENALTY_THRESHOLD = 0.80    # 80% accumulated penalty = block
    PROBE_PENALTY_THRESHOLD = 0.50    # 50% = probe only (25% size)
    REDUCED_PENALTY_THRESHOLD = 0.30  # 30% = reduced size
    
    # Yellow combination rule
    YELLOW_COMBINATION_BLOCK = 3  # 3+ yellows = block
    
    # Category penalties (for combination rules)
    CATEGORY_MAX_YELLOWS = {
        FilterCategory.MARKET_QUALITY: 2,    # Max 2 market issues
        FilterCategory.SESSION_QUALITY: 2,   # Max 2 session issues
        FilterCategory.SIGNAL_QUALITY: 2,    # Max 2 signal issues
        FilterCategory.EXECUTION_QUALITY: 1, # Max 1 execution issue (critical)
        FilterCategory.SYSTEM_HEALTH: 1,     # Max 1 system issue (critical)
    }
    
    def __init__(self):
        self._filter_results: List[FilterResult] = []
        self._last_decision: Optional[IntelligentFilterDecision] = None
    
    def evaluate(
        self,
        # Market Quality
        spread_bps: Optional[float] = None,
        spread_normal_bps: float = 3.0,
        feed_age_sec: Optional[float] = None,
        atr_ratio: Optional[float] = None,
        
        # Session Quality
        htf_adx: Optional[float] = None,
        session_name: str = "unknown",
        day_of_week: int = 0,
        is_weekend: bool = False,
        
        # Signal Quality
        regime: str = "UNKNOWN",
        chase_atr: float = 0.0,
        micro_trend: str = "NEUTRAL",
        micro_strength: float = 0.0,
        entry_direction: str = "LONG",
        has_bos: bool = False,
        near_level: bool = False,
        
        # RSI for directional confirmation (NEW)
        rsi_value: Optional[float] = None,
        rsi_slope: Optional[float] = None,
        
        # Execution Quality
        recent_slippage_bps: float = 0.0,
        order_latency_ms: float = 0.0,
        recent_rejects: int = 0,
        
        # System Health
        consecutive_timeouts: int = 0,
        tick_latency_ms: float = 0.0,
        exchange_connected: bool = True,
        
        # Context
        market_time: datetime | None = None,
    ) -> IntelligentFilterDecision:
        """
        Evaluate all filters and return intelligent decision.
        
        This is NOT a binary pass/fail - it accumulates penalties
        and makes a nuanced decision based on overall quality.
        """
        self._filter_results = []
        
        # =====================================================================
        # LAYER 1: HARD BLOCKS (RED) - No negotiation
        # =====================================================================
        
        # Exchange disconnected - HARD BLOCK
        if not exchange_connected:
            self._add_result(FilterResult(
                name="EXCHANGE_DISCONNECTED",
                category=FilterCategory.SYSTEM_HEALTH,
                severity=FilterSeverity.RED,
                penalty=1.0,
                reason="Exchange disconnected - cannot trade"
            ))
        
        # Feed completely stale (> 60s) - HARD BLOCK
        if feed_age_sec is not None and feed_age_sec > 60:
            self._add_result(FilterResult(
                name="FEED_DEAD",
                category=FilterCategory.MARKET_QUALITY,
                severity=FilterSeverity.RED,
                penalty=1.0,
                reason=f"Feed dead ({feed_age_sec:.0f}s > 60s)"
            ))
        
        # Extreme spread (> 10x normal) - HARD BLOCK
        if spread_bps is not None and spread_normal_bps > 0:
            spread_ratio = spread_bps / spread_normal_bps
            if spread_ratio > 10:
                self._add_result(FilterResult(
                    name="SPREAD_EXTREME",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.RED,
                    penalty=1.0,
                    reason=f"Spread {spread_ratio:.1f}x normal - liquidity gone"
                ))
        
        # Multiple consecutive timeouts (> 3) - HARD BLOCK
        if consecutive_timeouts > 3:
            self._add_result(FilterResult(
                name="SYSTEM_UNSTABLE",
                category=FilterCategory.SYSTEM_HEALTH,
                severity=FilterSeverity.RED,
                penalty=1.0,
                reason=f"System unstable ({consecutive_timeouts} consecutive timeouts)"
            ))
        
        # =====================================================================
        # LAYER 2: SOFT PENALTIES (YELLOW/ORANGE) - Accumulate
        # =====================================================================
        
        # --- MARKET QUALITY ---
        
        # Spread elevated (3-10x normal)
        if spread_bps is not None and spread_normal_bps > 0:
            spread_ratio = spread_bps / spread_normal_bps
            if 5 < spread_ratio <= 10:
                self._add_result(FilterResult(
                    name="SPREAD_HIGH",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.ORANGE,
                    penalty=0.40,
                    reason=f"Spread elevated ({spread_ratio:.1f}x normal)",
                    context={"spread_ratio": spread_ratio}
                ))
            elif 2 < spread_ratio <= 5:
                self._add_result(FilterResult(
                    name="SPREAD_ELEVATED",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.20,
                    reason=f"Spread slightly elevated ({spread_ratio:.1f}x normal)",
                    context={"spread_ratio": spread_ratio}
                ))
        
        # Feed stale (10-60s)
        if feed_age_sec is not None:
            if 30 < feed_age_sec <= 60:
                self._add_result(FilterResult(
                    name="FEED_STALE",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.ORANGE,
                    penalty=0.35,
                    reason=f"Feed stale ({feed_age_sec:.0f}s)"
                ))
            elif 10 < feed_age_sec <= 30:
                self._add_result(FilterResult(
                    name="FEED_DELAYED",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.15,
                    reason=f"Feed delayed ({feed_age_sec:.0f}s)"
                ))
        
        # Volatility explosion (2-4x ATR)
        if atr_ratio is not None:
            if 3 < atr_ratio <= 4:
                self._add_result(FilterResult(
                    name="VOLATILITY_EXTREME",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.ORANGE,
                    penalty=0.40,
                    reason=f"Extreme volatility (ATR {atr_ratio:.1f}x normal)"
                ))
            elif 2 < atr_ratio <= 3:
                self._add_result(FilterResult(
                    name="VOLATILITY_HIGH",
                    category=FilterCategory.MARKET_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.20,
                    reason=f"High volatility (ATR {atr_ratio:.1f}x normal)"
                ))
        
        # --- SESSION QUALITY ---
        
        # ADX too low - CONTEXT DEPENDENT
        if htf_adx is not None:
            # Different thresholds for different sessions
            adx_threshold = self._get_adx_threshold_for_session(session_name)
            
            if htf_adx < adx_threshold * 0.8:  # 20% below threshold
                self._add_result(FilterResult(
                    name="ADX_VERY_LOW",
                    category=FilterCategory.SESSION_QUALITY,
                    severity=FilterSeverity.ORANGE,
                    penalty=0.35,
                    reason=f"ADX {htf_adx:.1f} very low for {session_name} (threshold: {adx_threshold})",
                    context={"adx": htf_adx, "threshold": adx_threshold}
                ))
            elif htf_adx < adx_threshold:  # Below threshold
                self._add_result(FilterResult(
                    name="ADX_LOW",
                    category=FilterCategory.SESSION_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.20,
                    reason=f"ADX {htf_adx:.1f} below threshold for {session_name} ({adx_threshold})",
                    context={"adx": htf_adx, "threshold": adx_threshold}
                ))
        
        # Weekend trading
        if is_weekend:
            self._add_result(FilterResult(
                name="WEEKEND",
                category=FilterCategory.SESSION_QUALITY,
                severity=FilterSeverity.YELLOW,
                penalty=0.25,
                reason="Weekend - reduced liquidity expected"
            ))
        
        # --- SIGNAL QUALITY ---
        
        # Chase in RANGE
        is_range = regime.upper() in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']
        if is_range and chase_atr > 0.5:
            if chase_atr > 1.3:
                self._add_result(FilterResult(
                    name="CHASE_EXTREME",
                    category=FilterCategory.SIGNAL_QUALITY,
                    severity=FilterSeverity.ORANGE,
                    penalty=0.45,
                    reason=f"RANGE + high chase ({chase_atr:.2f} ATR)"
                ))
            elif chase_atr > 1.0:
                self._add_result(FilterResult(
                    name="CHASE_HIGH",
                    category=FilterCategory.SIGNAL_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.25,
                    reason=f"RANGE + moderate chase ({chase_atr:.2f} ATR)"
                ))
            elif chase_atr > 0.5:
                self._add_result(FilterResult(
                    name="CHASE_MODERATE",
                    category=FilterCategory.SIGNAL_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.10,
                    reason=f"RANGE + small chase ({chase_atr:.2f} ATR)"
                ))
        
        # Microtrend contradiction
        if self._microtrend_contradicts(micro_trend, entry_direction):
            if micro_strength >= 85:
                self._add_result(FilterResult(
                    name="MICROTREND_STRONG_CONTRA",
                    category=FilterCategory.SIGNAL_QUALITY,
                    severity=FilterSeverity.RED if not (has_bos and near_level) else FilterSeverity.ORANGE,
                    penalty=0.80 if not (has_bos and near_level) else 0.50,
                    reason=f"Strong microtrend contradiction ({micro_strength:.0f}%)"
                ))
            elif micro_strength >= 70:
                self._add_result(FilterResult(
                    name="MICROTREND_CONTRA",
                    category=FilterCategory.SIGNAL_QUALITY,
                    severity=FilterSeverity.YELLOW,
                    penalty=0.30,
                    reason=f"Microtrend contradiction ({micro_strength:.0f}%)"
                ))
        
        # =========================================================================
        # 🔥 NEW (Jan 27, 2026): RSI CONTRA-DIRECTION FILTER
        # Block SHORTs when oversold, block LONGs when overbought
        # This prevents entering against extreme momentum exhaustion
        # =========================================================================
        if rsi_value is not None:
            RSI_OVERSOLD = 35  # Below this = don't SHORT
            RSI_OVERBOUGHT = 65  # Above this = don't LONG
            
            if entry_direction.upper() == 'SHORT' and rsi_value < RSI_OVERSOLD:
                # SHORT when already oversold = HIGH RISK
                # RSI slope matters: if slope is RISING, it's even worse (reversal in progress)
                if rsi_slope is not None and rsi_slope > 0:
                    # RSI is oversold AND rising = reversal confirmed, BLOCK
                    self._add_result(FilterResult(
                        name="RSI_OVERSOLD_REVERSAL",
                        category=FilterCategory.SIGNAL_QUALITY,
                        severity=FilterSeverity.RED,
                        penalty=0.90,
                        reason=f"SHORT when RSI oversold ({rsi_value:.1f}) and rising (slope={rsi_slope:.1f}) - reversal in progress"
                    ))
                else:
                    # RSI is oversold but still falling - moderate penalty
                    self._add_result(FilterResult(
                        name="RSI_OVERSOLD_SHORT",
                        category=FilterCategory.SIGNAL_QUALITY,
                        severity=FilterSeverity.ORANGE,
                        penalty=0.50,
                        reason=f"SHORT when RSI oversold ({rsi_value:.1f}) - limited downside"
                    ))
            
            elif entry_direction.upper() == 'LONG' and rsi_value > RSI_OVERBOUGHT:
                # LONG when already overbought = HIGH RISK
                if rsi_slope is not None and rsi_slope < 0:
                    # RSI is overbought AND falling = reversal confirmed, BLOCK
                    self._add_result(FilterResult(
                        name="RSI_OVERBOUGHT_REVERSAL",
                        category=FilterCategory.SIGNAL_QUALITY,
                        severity=FilterSeverity.RED,
                        penalty=0.90,
                        reason=f"LONG when RSI overbought ({rsi_value:.1f}) and falling (slope={rsi_slope:.1f}) - reversal in progress"
                    ))
                else:
                    # RSI is overbought but still rising - moderate penalty
                    self._add_result(FilterResult(
                        name="RSI_OVERBOUGHT_LONG",
                        category=FilterCategory.SIGNAL_QUALITY,
                        severity=FilterSeverity.ORANGE,
                        penalty=0.50,
                        reason=f"LONG when RSI overbought ({rsi_value:.1f}) - limited upside"
                    ))
        
        # --- EXECUTION QUALITY ---
        
        # High slippage recently
        if recent_slippage_bps > 20:
            self._add_result(FilterResult(
                name="SLIPPAGE_HIGH",
                category=FilterCategory.EXECUTION_QUALITY,
                severity=FilterSeverity.ORANGE,
                penalty=0.35,
                reason=f"High recent slippage ({recent_slippage_bps:.1f}bps)"
            ))
        elif recent_slippage_bps > 10:
            self._add_result(FilterResult(
                name="SLIPPAGE_ELEVATED",
                category=FilterCategory.EXECUTION_QUALITY,
                severity=FilterSeverity.YELLOW,
                penalty=0.15,
                reason=f"Elevated recent slippage ({recent_slippage_bps:.1f}bps)"
            ))
        
        # High order latency
        if order_latency_ms > 5000:
            self._add_result(FilterResult(
                name="LATENCY_EXTREME",
                category=FilterCategory.EXECUTION_QUALITY,
                severity=FilterSeverity.ORANGE,
                penalty=0.40,
                reason=f"Extreme order latency ({order_latency_ms:.0f}ms)"
            ))
        elif order_latency_ms > 2000:
            self._add_result(FilterResult(
                name="LATENCY_HIGH",
                category=FilterCategory.EXECUTION_QUALITY,
                severity=FilterSeverity.YELLOW,
                penalty=0.20,
                reason=f"High order latency ({order_latency_ms:.0f}ms)"
            ))
        
        # Recent order rejects
        if recent_rejects > 0:
            self._add_result(FilterResult(
                name="RECENT_REJECTS",
                category=FilterCategory.EXECUTION_QUALITY,
                severity=FilterSeverity.YELLOW,
                penalty=0.15 * recent_rejects,
                reason=f"{recent_rejects} recent order reject(s)"
            ))
        
        # --- SYSTEM HEALTH ---
        
        # Slow ticks
        if tick_latency_ms > 10000:
            self._add_result(FilterResult(
                name="TICK_SLOW",
                category=FilterCategory.SYSTEM_HEALTH,
                severity=FilterSeverity.ORANGE,
                penalty=0.30,
                reason=f"Slow tick processing ({tick_latency_ms:.0f}ms)"
            ))
        elif tick_latency_ms > 5000:
            self._add_result(FilterResult(
                name="TICK_DELAYED",
                category=FilterCategory.SYSTEM_HEALTH,
                severity=FilterSeverity.YELLOW,
                penalty=0.15,
                reason=f"Delayed tick processing ({tick_latency_ms:.0f}ms)"
            ))
        
        # =====================================================================
        # LAYER 3: COMPUTE FINAL DECISION
        # =====================================================================
        return self._compute_decision()
    
    def _add_result(self, result: FilterResult):
        """Add a filter result to the accumulator"""
        self._filter_results.append(result)
        logger.debug(f"Filter: {result}")
    
    def _compute_decision(self) -> IntelligentFilterDecision:
        """
        Compute final decision from accumulated filter results.
        
        Rules:
        1. Any RED = BLOCK
        2. Accumulated penalty > 80% = BLOCK
        3. 3+ YELLOWS = BLOCK (combination rule)
        4. Too many issues in one category = BLOCK
        """
        
        # Check for hard blocks (RED)
        red_filters = [f for f in self._filter_results if f.severity == FilterSeverity.RED]
        if red_filters:
            return IntelligentFilterDecision(
                allow_entry=False,
                final_size_multiplier=0.0,
                require_limit_order=True,
                sl_tightening_factor=1.0,
                decision_reason=f"HARD BLOCK: {red_filters[0].reason}",
                accumulated_penalty=1.0,
                active_filters=self._filter_results,
            )
        
        # Calculate accumulated penalty
        total_penalty = sum(f.penalty for f in self._filter_results)
        total_penalty = min(total_penalty, 1.0)  # Cap at 100%
        
        # Count yellows and oranges
        yellow_count = len([f for f in self._filter_results if f.severity == FilterSeverity.YELLOW])
        orange_count = len([f for f in self._filter_results if f.severity == FilterSeverity.ORANGE])
        
        # Calculate penalties by category
        penalties_by_cat = {}
        for cat in FilterCategory:
            cat_filters = [f for f in self._filter_results if f.category == cat]
            penalties_by_cat[cat.value] = sum(f.penalty for f in cat_filters)
        
        # Count issues per category
        issues_by_cat = {}
        for cat in FilterCategory:
            issues_by_cat[cat] = len([
                f for f in self._filter_results 
                if f.category == cat and f.severity in [FilterSeverity.YELLOW, FilterSeverity.ORANGE]
            ])
        
        # =====================================================================
        # COMBINATION RULES
        # =====================================================================
        
        # Rule: 3+ yellows = block
        if yellow_count >= self.YELLOW_COMBINATION_BLOCK:
            return IntelligentFilterDecision(
                allow_entry=False,
                final_size_multiplier=0.0,
                require_limit_order=True,
                sl_tightening_factor=1.0,
                decision_reason=f"COMBINATION BLOCK: {yellow_count} yellow flags",
                accumulated_penalty=total_penalty,
                active_filters=self._filter_results,
                penalties_by_category=penalties_by_cat,
            )
        
        # Rule: 2+ oranges = block
        if orange_count >= 2:
            return IntelligentFilterDecision(
                allow_entry=False,
                final_size_multiplier=0.0,
                require_limit_order=True,
                sl_tightening_factor=1.0,
                decision_reason=f"COMBINATION BLOCK: {orange_count} orange flags",
                accumulated_penalty=total_penalty,
                active_filters=self._filter_results,
                penalties_by_category=penalties_by_cat,
            )
        
        # Rule: Too many issues in critical category
        for cat, max_issues in self.CATEGORY_MAX_YELLOWS.items():
            if issues_by_cat.get(cat, 0) > max_issues:
                return IntelligentFilterDecision(
                    allow_entry=False,
                    final_size_multiplier=0.0,
                    require_limit_order=True,
                    sl_tightening_factor=1.0,
                    decision_reason=f"CATEGORY BLOCK: Too many {cat.value} issues ({issues_by_cat[cat]})",
                    accumulated_penalty=total_penalty,
                    active_filters=self._filter_results,
                    penalties_by_category=penalties_by_cat,
                )
        
        # Rule: Accumulated penalty threshold
        if total_penalty >= self.BLOCK_PENALTY_THRESHOLD:
            return IntelligentFilterDecision(
                allow_entry=False,
                final_size_multiplier=0.0,
                require_limit_order=True,
                sl_tightening_factor=1.0,
                decision_reason=f"PENALTY BLOCK: {total_penalty:.0%} accumulated",
                accumulated_penalty=total_penalty,
                active_filters=self._filter_results,
                penalties_by_category=penalties_by_cat,
            )
        
        # =====================================================================
        # ALLOW WITH ADJUSTMENTS
        # =====================================================================
        
        # Calculate size multiplier (inverse of penalty)
        size_mult = 1.0 - total_penalty
        
        # Apply thresholds
        if total_penalty >= self.PROBE_PENALTY_THRESHOLD:
            size_mult = min(size_mult, 0.25)  # Cap at 25% (probe)
            require_limit = True
            sl_tightening = 0.85  # 15% tighter
            reason = f"PROBE ONLY: {total_penalty:.0%} penalty"
        elif total_penalty >= self.REDUCED_PENALTY_THRESHOLD:
            size_mult = min(size_mult, 0.50)  # Cap at 50%
            require_limit = orange_count > 0  # Require limit if any orange
            sl_tightening = 0.90  # 10% tighter
            reason = f"REDUCED SIZE: {total_penalty:.0%} penalty"
        else:
            require_limit = False
            sl_tightening = 1.0
            reason = "ALLOW" if total_penalty == 0 else f"ALLOW with {total_penalty:.0%} penalty"
        
        self._last_decision = IntelligentFilterDecision(
            allow_entry=True,
            final_size_multiplier=size_mult,
            require_limit_order=require_limit,
            sl_tightening_factor=sl_tightening,
            decision_reason=reason,
            accumulated_penalty=total_penalty,
            active_filters=self._filter_results,
            penalties_by_category=penalties_by_cat,
        )
        
        # Log decision
        if total_penalty > 0:
            logger.info(f"🧠 Intelligent Filter: {self._last_decision}")
            for f in self._filter_results:
                logger.info(f"   {f}")
        
        return self._last_decision
    
    def _get_adx_threshold_for_session(self, session_name: str) -> float:
        """
        Get ADX threshold based on session.
        
        Different sessions have different expected ADX levels:
        - London/NY overlap: High activity expected → ADX >= 20
        - Asia: Low activity normal → ADX >= 15
        - Weekend: Very low normal → ADX >= 12
        """
        session_lower = session_name.lower()
        
        if 'london' in session_lower or 'newyork' in session_lower:
            return 18.0  # High activity expected
        elif 'asia' in session_lower or 'tokyo' in session_lower:
            return 15.0  # Lower activity normal
        elif 'quiet' in session_lower or 'dead' in session_lower:
            return 12.0  # Very low activity expected
        else:
            return 16.0  # Default
    
    def _microtrend_contradicts(self, micro_trend: str, entry_direction: str) -> bool:
        """Check if microtrend contradicts entry direction"""
        if micro_trend.upper() == 'NEUTRAL':
            return False
        if entry_direction.upper() == 'SHORT' and micro_trend.upper() == 'BULLISH':
            return True
        if entry_direction.upper() == 'LONG' and micro_trend.upper() == 'BEARISH':
            return True
        return False


# Singleton
_intelligent_filter: Optional[IntelligentFilter] = None

def get_intelligent_filter() -> IntelligentFilter:
    """Get or create Intelligent Filter singleton"""
    global _intelligent_filter
    if _intelligent_filter is None:
        _intelligent_filter = IntelligentFilter()
    return _intelligent_filter
