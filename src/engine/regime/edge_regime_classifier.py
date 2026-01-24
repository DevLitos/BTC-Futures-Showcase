"""
Edge Regime Classifier - Identifies market regime for edge validity

CRITICAL CONCEPT:
The edge doesn't die - it migrates between regimes.
Your strategy edge is ONLY valid in specific market regimes.

REGIME_A: High expansion / trend continuation (EDGE VALID)
REGIME_B: Volatility compression / potential fakeouts (EDGE PARTIAL)
REGIME_C: News-driven / unstable microstructure (EDGE INVALID)
REGIME_D: Low participation / chop (EDGE INVALID)

This is NOT an indicator. This is regime identification logic.
The state machine must know WHAT REGIME we're in, not just if edge is "degraded".
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC
import numpy as np


class EdgeRegime(Enum):
    """Market regime classification"""
    REGIME_A = "expansion_trend_continuation"  # Edge valid
    REGIME_B = "compression_fakeouts"           # Edge partial
    REGIME_C = "news_driven_unstable"           # Edge invalid
    REGIME_D = "low_participation_chop"         # Edge invalid
    UNKNOWN = "insufficient_data"


@dataclass
class RegimeMetrics:
    """Metrics that define current regime"""
    expansion_count: int          # Consecutive expansions > 1R
    expansion_quality: float      # Average expansion size / avg move
    fakeout_rate: float          # Breakouts failing / total breakouts
    volatility_trend: str        # 'expanding', 'compressing', 'stable'
    session_efficiency: float    # Realized move / total range
    participation_score: float   # Volume quality metric (0-1)
    price_action_type: str       # 'trending', 'ranging', 'erratic'
    microstructure_stable: bool  # Order flow stability
    
    
@dataclass
class RegimeTransition:
    """Records regime changes"""
    from_regime: EdgeRegime
    to_regime: EdgeRegime
    transition_time: datetime
    confidence: float  # 0-1
    metrics_snapshot: Dict


class EdgeRegimeClassifier:
    """
    Classifies current market regime to determine edge validity.
    
    PHILOSOPHY:
    - Edge degradation might be regime migration, not edge death
    - Strategy must adapt or wait, not force trades in wrong regime
    - Clear separation: "is edge broken?" vs "are we in the right regime?"
    """
    
    def __init__(self):
        """Initialize regime classifier"""
        self.current_regime: EdgeRegime = EdgeRegime.UNKNOWN
        self.regime_confidence: float = 0.0
        self.regime_start_time: Optional[datetime] = None
        self.regime_history: List[RegimeTransition] = []
        
        # Regime thresholds
        self.EXPANSION_COUNT_THRESHOLD = 2      # For REGIME_A
        self.EXPANSION_QUALITY_MIN = 1.5        # Expansion / normal move
        self.FAKEOUT_RATE_MAX = 0.30            # Max 30% fakeouts for REGIME_A
        self.EFFICIENCY_MIN = 0.60              # Min 60% session efficiency
        self.PARTICIPATION_MIN = 0.50           # Min volume participation
        
        # Tracking windows
        self.recent_expansions: List[Dict] = []
        self.recent_breakouts: List[Dict] = []
        self.session_metrics: List[Dict] = []
        
    def classify_regime(self,
                       expansion_count: int,
                       expansion_sizes: List[float],
                       breakout_success_rate: float,
                       atr_trend_5d: float,
                       atr_trend_20d: float,
                       session_range: float,
                       session_realized_move: float,
                       volume_profile_quality: float,
                       price_action_consistency: float,
                       order_flow_imbalance_std: float) -> Tuple[EdgeRegime, float]:
        """
        Classify current market regime based on structural characteristics.
        
        Args:
            expansion_count: Consecutive sessions with expansion > 1R
            expansion_sizes: List of recent expansion sizes (in R)
            breakout_success_rate: % of breakouts that didn't fail (1 - fakeout_rate)
            atr_trend_5d: ATR direction last 5 days (-1 to +1)
            atr_trend_20d: ATR direction last 20 days (-1 to +1)
            session_range: Current session high-low range
            session_realized_move: Actual directional move in session
            volume_profile_quality: Volume concentration metric (0-1)
            price_action_consistency: Price bar consistency (0-1)
            order_flow_imbalance_std: Std dev of order flow imbalance
            
        Returns:
            Tuple of (EdgeRegime, confidence_score)
        """
        # Calculate derived metrics
        expansion_quality = np.mean(expansion_sizes) if expansion_sizes else 0.0
        fakeout_rate = 1.0 - breakout_success_rate
        session_efficiency = session_realized_move / session_range if session_range > 0 else 0.0
        
        # Determine volatility trend
        volatility_trend = self._classify_volatility_trend(atr_trend_5d, atr_trend_20d)
        
        # Determine price action type
        price_action_type = self._classify_price_action(
            session_efficiency,
            price_action_consistency,
            expansion_count
        )
        
        # Microstructure stability
        microstructure_stable = order_flow_imbalance_std < 0.3
        
        # Build metrics object
        metrics = RegimeMetrics(
            expansion_count=expansion_count,
            expansion_quality=float(expansion_quality),
            fakeout_rate=fakeout_rate,
            volatility_trend=volatility_trend,
            session_efficiency=session_efficiency,
            participation_score=volume_profile_quality,
            price_action_type=price_action_type,
            microstructure_stable=microstructure_stable
        )
        
        # Classify regime
        regime, confidence = self._determine_regime(metrics)
        
        # Record transition if regime changed
        if regime != self.current_regime and self.current_regime != EdgeRegime.UNKNOWN:
            self._record_transition(regime, confidence, metrics)
        
        # Update state
        if regime != self.current_regime:
            self.current_regime = regime
            self.regime_start_time = datetime.now(UTC)
        
        self.regime_confidence = confidence
        
        return regime, confidence
    
    def _classify_volatility_trend(self, atr_5d: float, atr_20d: float) -> str:
        """Classify volatility trend"""
        if atr_5d > 0.15 and atr_20d > 0.10:
            return 'expanding'
        elif atr_5d < -0.15 and atr_20d < -0.10:
            return 'compressing'
        else:
            return 'stable'
    
    def _classify_price_action(self,
                              efficiency: float,
                              consistency: float,
                              expansion_count: int) -> str:
        """Classify price action type"""
        if efficiency > 0.65 and consistency > 0.60 and expansion_count >= 2:
            return 'trending'
        elif efficiency < 0.40 or consistency < 0.40:
            return 'erratic'
        else:
            return 'ranging'
    
    def _determine_regime(self, metrics: RegimeMetrics) -> Tuple[EdgeRegime, float]:
        """
        Determine regime based on metrics.
        
        Logic:
        - REGIME_A: Clean trending with expansion
        - REGIME_B: Compression phase, potential setup
        - REGIME_C: Erratic / unstable
        - REGIME_D: Low participation chop
        """
        scores = {
            EdgeRegime.REGIME_A: 0.0,
            EdgeRegime.REGIME_B: 0.0,
            EdgeRegime.REGIME_C: 0.0,
            EdgeRegime.REGIME_D: 0.0
        }
        
        # REGIME_A scoring (expansion/trend continuation)
        if metrics.expansion_count >= self.EXPANSION_COUNT_THRESHOLD:
            scores[EdgeRegime.REGIME_A] += 30
        if metrics.expansion_quality >= self.EXPANSION_QUALITY_MIN:
            scores[EdgeRegime.REGIME_A] += 25
        if metrics.fakeout_rate <= self.FAKEOUT_RATE_MAX:
            scores[EdgeRegime.REGIME_A] += 20
        if metrics.session_efficiency >= self.EFFICIENCY_MIN:
            scores[EdgeRegime.REGIME_A] += 15
        if metrics.price_action_type == 'trending':
            scores[EdgeRegime.REGIME_A] += 10
        
        # REGIME_B scoring (compression/fakeouts)
        if metrics.volatility_trend == 'compressing':
            scores[EdgeRegime.REGIME_B] += 30
        if 0.30 < metrics.fakeout_rate <= 0.50:
            scores[EdgeRegime.REGIME_B] += 25
        if metrics.price_action_type == 'ranging':
            scores[EdgeRegime.REGIME_B] += 20
        if metrics.microstructure_stable:
            scores[EdgeRegime.REGIME_B] += 15
        if 0.40 <= metrics.session_efficiency < 0.60:
            scores[EdgeRegime.REGIME_B] += 10
        
        # REGIME_C scoring (news-driven/unstable)
        if not metrics.microstructure_stable:
            scores[EdgeRegime.REGIME_C] += 35
        if metrics.price_action_type == 'erratic':
            scores[EdgeRegime.REGIME_C] += 30
        if metrics.fakeout_rate > 0.50:
            scores[EdgeRegime.REGIME_C] += 20
        if metrics.volatility_trend == 'expanding' and metrics.session_efficiency < 0.40:
            scores[EdgeRegime.REGIME_C] += 15
        
        # REGIME_D scoring (low participation/chop)
        if metrics.participation_score < self.PARTICIPATION_MIN:
            scores[EdgeRegime.REGIME_D] += 40
        if metrics.expansion_count == 0 and metrics.session_efficiency < 0.35:
            scores[EdgeRegime.REGIME_D] += 30
        if metrics.price_action_type == 'ranging' and metrics.participation_score < 0.40:
            scores[EdgeRegime.REGIME_D] += 20
        if metrics.volatility_trend == 'compressing' and metrics.expansion_quality < 1.0:
            scores[EdgeRegime.REGIME_D] += 10
        
        # Find highest scoring regime
        best_regime = max(scores, key=lambda k: scores.get(k, 0.0) or 0.0)
        confidence = min(scores[best_regime] / 100.0, 1.0)
        
        # Require minimum confidence
        if confidence < 0.40:
            return EdgeRegime.UNKNOWN, confidence
        
        return best_regime, confidence
    
    def _record_transition(self,
                          new_regime: EdgeRegime,
                          confidence: float,
                          metrics: RegimeMetrics) -> None:
        """Record regime transition"""
        transition = RegimeTransition(
            from_regime=self.current_regime,
            to_regime=new_regime,
            transition_time=datetime.now(UTC),
            confidence=confidence,
            metrics_snapshot={
                'expansion_count': metrics.expansion_count,
                'expansion_quality': metrics.expansion_quality,
                'fakeout_rate': metrics.fakeout_rate,
                'volatility_trend': metrics.volatility_trend,
                'session_efficiency': metrics.session_efficiency,
                'participation_score': metrics.participation_score,
                'price_action_type': metrics.price_action_type,
                'microstructure_stable': metrics.microstructure_stable
            }
        )
        self.regime_history.append(transition)
        
        # Limit history
        if len(self.regime_history) > 100:
            self.regime_history = self.regime_history[-100:]
    
    def is_edge_valid_in_regime(self) -> Tuple[bool, str]:
        """
        Determine if edge is valid in current regime.
        
        CRITICAL RULE:
        Edge is ONLY valid in REGIME_A
        Edge is PARTIALLY valid in REGIME_B (reduced size)
        Edge is INVALID in REGIME_C and REGIME_D
        
        Returns:
            Tuple of (is_valid, reason)
        """
        if self.current_regime == EdgeRegime.REGIME_A:
            if self.regime_confidence >= 0.60:
                return True, "Regime A confirmed: expansion/trend continuation"
            else:
                return False, "Regime A but low confidence"
        
        elif self.current_regime == EdgeRegime.REGIME_B:
            if self.regime_confidence >= 0.60:
                return False, "Regime B: compression phase - edge degraded"
            else:
                return False, "Regime B with low confidence"
        
        elif self.current_regime == EdgeRegime.REGIME_C:
            return False, "Regime C: news-driven/unstable - no edge"
        
        elif self.current_regime == EdgeRegime.REGIME_D:
            return False, "Regime D: low participation/chop - no edge"
        
        else:  # UNKNOWN
            return False, "Regime unknown - insufficient data"
    
    def get_regime_adjusted_size_multiplier(self) -> float:
        """
        Returns position size multiplier based on regime.
        
        Returns:
            0.0 to 1.0 multiplier
        """
        if self.current_regime == EdgeRegime.REGIME_A:
            if self.regime_confidence >= 0.70:
                return 1.0
            elif self.regime_confidence >= 0.60:
                return 0.75
            else:
                return 0.5
        
        elif self.current_regime == EdgeRegime.REGIME_B:
            # Partial validity - small size for regime transition trades
            if self.regime_confidence >= 0.70:
                return 0.3  # Reduced size in compression
            else:
                return 0.0
        
        else:  # REGIME_C, REGIME_D, UNKNOWN
            return 0.0
    
    def get_regime_summary(self) -> Dict:
        """Get current regime summary for logging"""
        return {
            'regime': self.current_regime.value,
            'confidence': round(self.regime_confidence, 3),
            'regime_duration_hours': (
                (datetime.now(UTC) - self.regime_start_time).total_seconds() / 3600
                if self.regime_start_time else 0
            ),
            'edge_valid': self.is_edge_valid_in_regime()[0],
            'size_multiplier': self.get_regime_adjusted_size_multiplier(),
            'recent_transitions': len([
                t for t in self.regime_history
                if t.transition_time > datetime.now(UTC) - timedelta(days=7)
            ])
        }
    
    def requires_regime_reassessment(self) -> bool:
        """
        Check if regime should be reassessed.
        
        Triggers:
        - Been in same regime > 48 hours (might have changed)
        - Confidence dropped below 0.50
        - Multiple recent transitions (regime instability)
        """
        if self.regime_start_time is None:
            return True
        
        hours_in_regime = (datetime.now(UTC) - self.regime_start_time).total_seconds() / 3600
        
        # Been too long in same regime
        if hours_in_regime > 48:
            return True
        
        # Low confidence
        if self.regime_confidence < 0.50:
            return True
        
        # Recent regime instability (3+ transitions in 24h)
        recent_transitions = [
            t for t in self.regime_history
            if t.transition_time > datetime.now(UTC) - timedelta(hours=24)
        ]
        if len(recent_transitions) >= 3:
            return True
        
        return False
    
    def get_dynamic_rr(self, atr_ratio: float | None = None) -> Tuple[float, str]:
        """
        CAPA 2: Get dynamic Risk/Reward ratio based on current market regime.
        
        RR is determined by OBSERVABLE REGIME, not by signal quality.
        This prevents overoptimization and keeps RR decisions data-driven.
        
        Args:
            atr_ratio: Current ATR / median ATR (for volatility adjustment)
        
        Returns:
            Tuple of (rr_target, reason)
        
        Logic:
            REGIME_A (Trend): RR 1.2-1.5 (let winners run)
            REGIME_B (Compression): RR 0.9-1.0 (fakeout risk, take profits early)
            REGIME_C (News): RR 0.8 (reduced, just survive)
            REGIME_D (Chop): RR 0.8-0.9 (reduced targets)
            Vol extrema: RR reduced + trailing recommended
        """
        base_rr = 1.0
        reason = "default"
        
        # Regime-based RR
        if self.current_regime == EdgeRegime.REGIME_A:
            # Trend - let winners run
            base_rr = 1.3
            reason = "REGIME_A: trend continuation"
        elif self.current_regime == EdgeRegime.REGIME_B:
            # Compression - fakeout risk
            base_rr = 0.95
            reason = "REGIME_B: compression/fakeouts"
        elif self.current_regime == EdgeRegime.REGIME_C:
            # News-driven - reduced
            base_rr = 0.8
            reason = "REGIME_C: news/unstable"
        elif self.current_regime == EdgeRegime.REGIME_D:
            # Chop - reduced
            base_rr = 0.85
            reason = "REGIME_D: low participation"
        
        # Volatility adjustment (optional layer)
        if atr_ratio is not None:
            if atr_ratio > 1.5:
                # Extreme volatility - reduce RR and recommend trailing
                base_rr = min(base_rr, 0.9)
                reason += " + high vol (trailing recommended)"
            elif atr_ratio < 0.5:
                # Very low volatility - slightly wider RR
                base_rr = min(base_rr * 1.1, 1.5)
                reason += " + low vol"
        
        return (round(base_rr, 2), reason)
    
    def get_be_conditions(self) -> dict:
        """
        CAPA 2: Get Break-Even conditions based on regime.
        
        Returns dict with BE parameters that vary by regime.
        """
        # Default conservative BE
        be_config = {
            'min_r_for_be': 0.6,  # Minimum +0.6R before considering BE
            'momentum_required': True,  # Require momentum still in direction
            'volume_check': True,  # Check volume hasn't collapsed
            'enabled': True
        }
        
        if self.current_regime == EdgeRegime.REGIME_A:
            # Trending - give more room, BE at 0.8R
            be_config['min_r_for_be'] = 0.8
            be_config['momentum_required'] = False  # Trust trend
        elif self.current_regime == EdgeRegime.REGIME_B:
            # Compression - tighter BE at 0.5R
            be_config['min_r_for_be'] = 0.5
        elif self.current_regime in [EdgeRegime.REGIME_C, EdgeRegime.REGIME_D]:
            # Unfavorable - aggressive BE at 0.4R
            be_config['min_r_for_be'] = 0.4
        
        return be_config
