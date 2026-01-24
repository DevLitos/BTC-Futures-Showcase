"""
Degradation Attribution Layer - Determines WHY degradation is occurring

CRITICAL PROBLEM:
Your DegradationMonitor correctly detects degradation.
But it doesn't know WHY.

This causes dangerous confusion:
- BE stops ↑ → Is it execution issues or volatility compression?
- MFE/MAE ↓ → Is it market structure change or random variance?
- Duration ↑ → Is it regime shift or temporary ranging?

Without attribution, the state machine makes correct decisions for WRONG REASONS.
That kills you in live trading.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, UTC, timedelta
import numpy as np


class DegradationCause(Enum):
    """Root causes of degradation"""
    EXECUTION_QUALITY = "execution_issues"          # Slippage, fills, timing
    MARKET_STRUCTURE = "market_structure_change"    # Liquidity, participation shift
    VOLATILITY_REGIME = "volatility_regime_shift"   # ATR compression/expansion mismatch
    RANDOM_VARIANCE = "statistical_noise"           # Normal variance, not real degradation
    STRATEGY_OBSOLESCENCE = "edge_actually_broken"  # True edge death
    EXTERNAL_SHOCK = "macro_event_impact"           # News, events, unusual conditions


@dataclass
class AttributionScore:
    """Attribution confidence for each cause"""
    execution_quality: float = 0.0       # 0-1
    market_structure: float = 0.0        # 0-1
    volatility_regime: float = 0.0       # 0-1
    random_variance: float = 0.0         # 0-1
    strategy_obsolescence: float = 0.0   # 0-1
    external_shock: float = 0.0          # 0-1
    
    def get_primary_cause(self) -> Tuple[DegradationCause, float]:
        """Returns most likely cause and its score"""
        scores = {
            DegradationCause.EXECUTION_QUALITY: self.execution_quality,
            DegradationCause.MARKET_STRUCTURE: self.market_structure,
            DegradationCause.VOLATILITY_REGIME: self.volatility_regime,
            DegradationCause.RANDOM_VARIANCE: self.random_variance,
            DegradationCause.STRATEGY_OBSOLESCENCE: self.strategy_obsolescence,
            DegradationCause.EXTERNAL_SHOCK: self.external_shock
        }
        cause = max(scores, key=lambda k: scores.get(k, 0.0) or 0.0)
        return cause, scores[cause]


@dataclass
class AttributionAnalysis:
    """Complete attribution analysis result"""
    timestamp: datetime
    primary_cause: DegradationCause
    primary_score: float
    all_scores: AttributionScore
    confidence: float  # Overall confidence in attribution (0-1)
    recommended_action: str
    reasoning: List[str]


class DegradationAttributionLayer:
    """
    Attributes degradation signals to root causes.
    
    PHILOSOPHY:
    - Degradation is a symptom, not a diagnosis
    - Same symptom can have different causes
    - Different causes require different responses
    - Attribution prevents wrong decisions for right metrics
    """
    
    def __init__(self):
        """Initialize attribution layer"""
        self.attribution_history: List[AttributionAnalysis] = []
        
        # Thresholds for cause detection
        self.SLIPPAGE_THRESHOLD = 0.15          # 15% avg slippage = execution issue
        self.FILL_RATE_THRESHOLD = 0.85         # <85% fills = execution issue
        self.VOLUME_DROP_THRESHOLD = 0.30       # 30% volume drop = structure change
        self.ATR_COMPRESSION_THRESHOLD = 0.25   # 25% ATR drop = regime shift
        self.VARIANCE_ZSCORE_THRESHOLD = 1.5    # Z-score for random variance
        self.CORRELATION_DECAY_THRESHOLD = 0.40  # Setup correlation decay = obsolescence
        
    def attribute_degradation(self,
                            degradation_signals: Dict,
                            execution_metrics: Dict,
                            market_metrics: Dict,
                            historical_baseline: Dict) -> AttributionAnalysis:
        """
        Attribute degradation to root causes.
        
        Args:
            degradation_signals: Dict with MFE/MAE, BE stops, extension rate, duration
            execution_metrics: Dict with slippage, fill_rate, execution_time
            market_metrics: Dict with ATR, volume, spread, participation
            historical_baseline: Dict with historical averages for comparison
            
        Returns:
            AttributionAnalysis with primary cause and recommended action
        """
        scores = AttributionScore()
        reasoning = []
        
        # 1. Check for execution quality issues
        exec_score, exec_reasons = self._score_execution_quality(
            execution_metrics,
            historical_baseline.get('execution', {})
        )
        scores.execution_quality = exec_score
        reasoning.extend(exec_reasons)
        
        # 2. Check for market structure changes
        structure_score, structure_reasons = self._score_market_structure(
            market_metrics,
            historical_baseline.get('market', {})
        )
        scores.market_structure = structure_score
        reasoning.extend(structure_reasons)
        
        # 3. Check for volatility regime shift
        volatility_score, vol_reasons = self._score_volatility_regime(
            market_metrics,
            degradation_signals,
            historical_baseline.get('volatility', {})
        )
        scores.volatility_regime = volatility_score
        reasoning.extend(vol_reasons)
        
        # 4. Check if it's random variance
        variance_score, var_reasons = self._score_random_variance(
            degradation_signals,
            historical_baseline.get('performance', {})
        )
        scores.random_variance = variance_score
        reasoning.extend(var_reasons)
        
        # 5. Check for strategy obsolescence
        obsolescence_score, obs_reasons = self._score_strategy_obsolescence(
            degradation_signals,
            market_metrics,
            historical_baseline
        )
        scores.strategy_obsolescence = obsolescence_score
        reasoning.extend(obs_reasons)
        
        # 6. Check for external shocks
        shock_score, shock_reasons = self._score_external_shock(
            market_metrics,
            degradation_signals
        )
        scores.external_shock = shock_score
        reasoning.extend(shock_reasons)
        
        # Determine primary cause
        primary_cause, primary_score = scores.get_primary_cause()
        
        # Calculate overall confidence
        confidence = self._calculate_attribution_confidence(scores)
        
        # Determine recommended action
        action = self._recommend_action(primary_cause, primary_score, confidence)
        
        # Build analysis
        analysis = AttributionAnalysis(
            timestamp=datetime.now(UTC),
            primary_cause=primary_cause,
            primary_score=primary_score,
            all_scores=scores,
            confidence=confidence,
            recommended_action=action,
            reasoning=reasoning
        )
        
        # Store in history
        self.attribution_history.append(analysis)
        if len(self.attribution_history) > 100:
            self.attribution_history = self.attribution_history[-100:]
        
        return analysis
    
    def _score_execution_quality(self,
                                execution_metrics: Dict,
                                baseline: Dict) -> Tuple[float, List[str]]:
        """Score likelihood that degradation is due to execution issues"""
        score = 0.0
        reasons = []
        
        # Check slippage
        current_slippage = execution_metrics.get('avg_slippage_pct', 0.0)
        baseline_slippage = baseline.get('avg_slippage_pct', 0.05)
        
        if current_slippage > baseline_slippage * 2:
            score += 0.40
            reasons.append(f"Slippage {current_slippage:.1%} vs baseline {baseline_slippage:.1%}")
        
        # Check fill rate
        fill_rate = execution_metrics.get('fill_rate', 1.0)
        if fill_rate < self.FILL_RATE_THRESHOLD:
            score += 0.35
            reasons.append(f"Fill rate dropped to {fill_rate:.1%}")
        
        # Check execution timing
        exec_time = execution_metrics.get('avg_execution_time_ms', 0)
        baseline_exec = baseline.get('avg_execution_time_ms', 100)
        
        if exec_time > baseline_exec * 2:
            score += 0.25
            reasons.append(f"Execution time degraded: {exec_time}ms vs {baseline_exec}ms")
        
        return min(score, 1.0), reasons
    
    def _score_market_structure(self,
                               market_metrics: Dict,
                               baseline: Dict) -> Tuple[float, List[str]]:
        """Score likelihood of market structure change"""
        score = 0.0
        reasons = []
        
        # Check volume participation
        current_volume = market_metrics.get('avg_volume', 0)
        baseline_volume = baseline.get('avg_volume', current_volume)
        
        if baseline_volume > 0:
            volume_change = (current_volume - baseline_volume) / baseline_volume
            if volume_change < -self.VOLUME_DROP_THRESHOLD:
                score += 0.40
                reasons.append(f"Volume dropped {volume_change:.1%}")
        
        # Check spread widening
        current_spread = market_metrics.get('avg_spread_bps', 0)
        baseline_spread = baseline.get('avg_spread_bps', current_spread)
        
        if current_spread > baseline_spread * 1.5:
            score += 0.30
            reasons.append(f"Spread widened: {current_spread} vs {baseline_spread} bps")
        
        # Check liquidity depth
        depth_change = market_metrics.get('depth_change_pct', 0.0)
        if depth_change < -0.25:
            score += 0.30
            reasons.append(f"Liquidity depth reduced {depth_change:.1%}")
        
        return min(score, 1.0), reasons
    
    def _score_volatility_regime(self,
                                market_metrics: Dict,
                                degradation_signals: Dict,
                                baseline: Dict) -> Tuple[float, List[str]]:
        """Score likelihood of volatility regime shift"""
        score = 0.0
        reasons = []
        
        # Check ATR compression
        current_atr = market_metrics.get('current_atr', 0)
        baseline_atr = baseline.get('avg_atr', current_atr)
        atr_change = 0.0
        if baseline_atr > 0:
            atr_change = (current_atr - baseline_atr) / baseline_atr
            
            # ATR compression with BE stops increase = regime shift
            if atr_change < -self.ATR_COMPRESSION_THRESHOLD:
                be_stop_rate = degradation_signals.get('be_stop_percentage', 0.0)
                if be_stop_rate > 0.35:
                    score += 0.50
                    reasons.append(f"ATR compressed {atr_change:.1%} + BE stops {be_stop_rate:.1%}")
        
        # Check if trade duration increased with volatility drop
        duration_increase = degradation_signals.get('duration_change_pct', 0.0)
        if duration_increase > 0.30 and atr_change < -0.15:
            score += 0.30
            reasons.append(f"Duration up {duration_increase:.1%} during volatility compression")
        
        # Check MFE/MAE decline with volatility change
        mfe_mae = degradation_signals.get('mfe_mae_ratio', 1.5)
        if mfe_mae < 1.3 and abs(atr_change) > 0.20:
            score += 0.20
            reasons.append(f"MFE/MAE degraded during volatility regime shift")
        
        return min(score, 1.0), reasons
    
    def _score_random_variance(self,
                              degradation_signals: Dict,
                              baseline: Dict) -> Tuple[float, List[str]]:
        """Score likelihood that degradation is just statistical noise"""
        score = 0.0
        reasons = []
        
        # Check sample size
        sample_size = degradation_signals.get('sample_size', 20)
        if sample_size < 15:
            score += 0.40
            reasons.append(f"Small sample size: {sample_size} trades")
        
        # Check variance consistency
        current_wr = degradation_signals.get('win_rate', 0.5)
        baseline_wr = baseline.get('win_rate', 0.5)
        wr_std = baseline.get('win_rate_std', 0.10)
        
        if wr_std > 0:
            z_score = abs(current_wr - baseline_wr) / wr_std
            if z_score < self.VARIANCE_ZSCORE_THRESHOLD:
                score += 0.35
                reasons.append(f"Win rate deviation within {z_score:.1f} std devs")
        
        # Check if degradation is gradual vs sudden
        degradation_slope = degradation_signals.get('degradation_slope', 0.0)
        if abs(degradation_slope) < 0.05:
            score += 0.25
            reasons.append("Gradual degradation suggests variance vs structural break")
        
        return min(score, 1.0), reasons
    
    def _score_strategy_obsolescence(self,
                                    degradation_signals: Dict,
                                    market_metrics: Dict,
                                    baseline: Dict) -> Tuple[float, List[str]]:
        """Score likelihood that edge is actually broken"""
        score = 0.0
        reasons = []
        
        # Check persistent degradation
        consecutive_bad_windows = degradation_signals.get('consecutive_bad_windows', 0)
        if consecutive_bad_windows >= 3:
            score += 0.40
            reasons.append(f"Persistent degradation: {consecutive_bad_windows} consecutive windows")
        
        # Check setup correlation decay
        setup_correlation = degradation_signals.get('setup_correlation', 1.0)
        if setup_correlation < self.CORRELATION_DECAY_THRESHOLD:
            score += 0.35
            reasons.append(f"Setup correlation decayed to {setup_correlation:.2f}")
        
        # Check if ALL metrics degraded simultaneously
        metrics_degraded = 0
        if degradation_signals.get('mfe_mae_ratio', 1.5) < 1.3:
            metrics_degraded += 1
        if degradation_signals.get('be_stop_percentage', 0.0) > 0.40:
            metrics_degraded += 1
        if degradation_signals.get('non_extending_winners_pct', 0.0) > 0.60:
            metrics_degraded += 1
        if degradation_signals.get('avg_trade_duration_hours', 0.0) > 14:
            metrics_degraded += 1
        
        if metrics_degraded >= 3:
            score += 0.25
            reasons.append(f"Multiple metrics degraded simultaneously: {metrics_degraded}/4")
        
        return min(score, 1.0), reasons
    
    def _score_external_shock(self,
                             market_metrics: Dict,
                             degradation_signals: Dict) -> Tuple[float, List[str]]:
        """Score likelihood of external shock causing degradation"""
        score = 0.0
        reasons = []
        
        # Check for sudden volatility spike
        volatility_spike = market_metrics.get('volatility_spike_pct', 0.0)
        if volatility_spike > 0.50:
            score += 0.40
            reasons.append(f"Volatility spike: {volatility_spike:.1%}")
        
        # Check for unusual spread behavior
        spread_spike = market_metrics.get('spread_spike_pct', 0.0)
        if spread_spike > 0.40:
            score += 0.30
            reasons.append(f"Spread spike: {spread_spike:.1%}")
        
        # Check for sudden degradation onset
        degradation_onset_speed = degradation_signals.get('onset_speed', 0.0)
        if degradation_onset_speed > 0.70:  # Rapid onset
            score += 0.30
            reasons.append("Sudden degradation onset suggests external shock")
        
        return min(score, 1.0), reasons
    
    def _calculate_attribution_confidence(self, scores: AttributionScore) -> float:
        """
        Calculate confidence in attribution.
        
        High confidence when one cause clearly dominates.
        Low confidence when multiple causes are similar.
        """
        score_values = [
            scores.execution_quality,
            scores.market_structure,
            scores.volatility_regime,
            scores.random_variance,
            scores.strategy_obsolescence,
            scores.external_shock
        ]
        
        # Sort scores
        sorted_scores = sorted(score_values, reverse=True)
        
        if sorted_scores[0] == 0:
            return 0.0  # No clear signal
        
        # Confidence is based on separation between top scores
        if len(sorted_scores) > 1:
            separation = sorted_scores[0] - sorted_scores[1]
            confidence = min(separation * 2, 1.0)
        else:
            confidence = sorted_scores[0]
        
        # Boost confidence if top score is very high
        if sorted_scores[0] > 0.70:
            confidence = max(confidence, 0.70)
        
        return confidence
    
    def _recommend_action(self,
                         cause: DegradationCause,
                         score: float,
                         confidence: float) -> str:
        """Recommend action based on attributed cause"""
        
        if confidence < 0.40:
            return "MONITOR - Attribution uncertain, continue observation"
        
        if cause == DegradationCause.EXECUTION_QUALITY:
            return "CHECK_EXECUTION - Review broker, slippage, fills"
        
        elif cause == DegradationCause.MARKET_STRUCTURE:
            return "AWAIT_STRUCTURE - Wait for liquidity/participation recovery"
        
        elif cause == DegradationCause.VOLATILITY_REGIME:
            return "AWAIT_REGIME - Wait for volatility regime alignment"
        
        elif cause == DegradationCause.RANDOM_VARIANCE:
            return "CONTINUE - Normal variance, maintain course"
        
        elif cause == DegradationCause.STRATEGY_OBSOLESCENCE:
            if score > 0.70:
                return "KILL_STRATEGY - Edge likely broken"
            else:
                return "DEEP_OBSERVATION - Potential obsolescence, extended monitoring"
        
        elif cause == DegradationCause.EXTERNAL_SHOCK:
            return "PAUSE - Wait for market normalization post-shock"
        
        return "MONITOR - Default monitoring mode"
    
    def get_attribution_summary(self) -> Dict:
        """Get summary of recent attributions"""
        if not self.attribution_history:
            return {'status': 'no_attributions'}
        
        recent = self.attribution_history[-10:]
        
        # Count primary causes
        cause_counts = {}
        for analysis in recent:
            cause = analysis.primary_cause.value
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
        
        # Average confidence
        avg_confidence = np.mean([a.confidence for a in recent])
        
        return {
            'total_attributions': len(self.attribution_history),
            'recent_primary_causes': cause_counts,
            'avg_confidence': round(avg_confidence, 3),
            'last_attribution': {
                'cause': recent[-1].primary_cause.value,
                'score': round(recent[-1].primary_score, 3),
                'confidence': round(recent[-1].confidence, 3),
                'action': recent[-1].recommended_action
            }
        }
