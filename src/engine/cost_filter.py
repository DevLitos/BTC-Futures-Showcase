"""
Cost Filter - Validates trade viability based on costs

Two main checks:
1. Spread vs SL distance: Ensures risk is large enough relative to spread
2. Fee-adjusted Expected R: Subtracts commission costs from expected R

Created: January 15, 2026
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime
import logging

from src.engine.filter_interface import BaseFilter, FilterDecision

logger = logging.getLogger(__name__)


class CostFilter(BaseFilter):
    """
    Cost Filter - Ensures trades are viable after costs.
    
    Rules:
    1. Spread vs Risk:
       - risk >= spread * MIN_RISK_SPREAD_MULT (e.g., 5x)
       - Otherwise: Block or heavy penalty
    
    2. Fee-adjusted R:
       - expected_R_net = expected_R_gross - fee_R
       - fee_R = (2 * fee_rate * entry_price) / risk_per_unit
       - expected_R_net >= MIN_NET_R
    
    This prevents taking trades where costs eat the edge.
    """
    
    # Spread vs Risk
    MIN_RISK_SPREAD_MULT = 5.0      # Risk must be >= 5x spread
    WEAK_RISK_SPREAD_MULT = 8.0     # Below this = warning penalty
    
    # Fee parameters
    MAKER_FEE_RATE = 0.0002         # 0.02% maker
    TAKER_FEE_RATE = 0.0005         # 0.05% taker
    DEFAULT_FEE_RATE = 0.0005       # Assume taker for safety
    
    # Expected R thresholds
    MIN_GROSS_R = 1.5               # Minimum gross R (before fees)
    MIN_NET_R = 1.3                 # Minimum net R (after fees)
    GOOD_NET_R = 2.0                # Good R threshold for bonus
    
    # Score adjustments
    LOW_R_PENALTY = -2.0
    SPREAD_RISK_PENALTY = -1.5
    GOOD_R_BONUS = 0.5
    
    def __init__(self, 
                 enabled: bool = True,
                 fee_rate: float = None,
                 min_net_r: float = None):
        super().__init__(enabled)
        if fee_rate:
            self.DEFAULT_FEE_RATE = fee_rate
        if min_net_r:
            self.MIN_NET_R = min_net_r
    
    def calculate_spread_risk_ratio(self,
                                    entry_price: float,
                                    stop_loss: float,
                                    spread: float) -> Tuple[float, bool, bool]:
        """
        Calculate risk to spread ratio.
        
        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            spread: Current bid-ask spread
        
        Returns:
            (ratio, passes_min, passes_weak)
        """
        risk = abs(entry_price - stop_loss)
        
        if spread <= 0:
            return 999.0, True, True
        
        ratio = risk / spread
        passes_min = ratio >= self.MIN_RISK_SPREAD_MULT
        passes_weak = ratio >= self.WEAK_RISK_SPREAD_MULT
        
        return ratio, passes_min, passes_weak
    
    def calculate_net_expected_r(self,
                                 entry_price: float,
                                 stop_loss: float,
                                 target_price: float,
                                 fee_rate: float = None) -> Tuple[float, float, float]:
        """
        Calculate net expected R after fees.
        
        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            target_price: Take profit target
            fee_rate: Fee rate (decimal)
        
        Returns:
            (gross_r, fee_r, net_r)
        """
        fee_rate = fee_rate or self.DEFAULT_FEE_RATE
        
        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        
        if risk <= 0:
            return 0.0, 0.0, 0.0
        
        # Gross R
        gross_r = reward / risk
        
        # Fee cost (round trip: entry + exit)
        # Using entry price for both as approximation
        fee_cost = 2 * fee_rate * entry_price
        
        # Fee expressed in R terms
        fee_r = fee_cost / risk if risk > 0 else 0
        
        # Net R
        net_r = gross_r - fee_r
        
        return gross_r, fee_r, net_r
    
    def evaluate(self,
                 entry_price: float = None,
                 stop_loss: float = None,
                 target_price: float = None,
                 spread: float = None,
                 fee_rate: float = None,
                 bias: str = None,
                 **kwargs) -> FilterDecision:
        """
        Evaluate cost viability of trade.
        
        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            target_price: Take profit target (optional for partial check)
            spread: Current bid-ask spread
            fee_rate: Fee rate (optional, uses default)
            bias: 'LONG' or 'SHORT'
        """
        if entry_price is None or stop_loss is None:
            return FilterDecision.allow_default()
        
        tags = {}
        reasons = []
        total_penalty = 0.0
        
        # ===== 1. SPREAD VS RISK CHECK =====
        if spread is not None and spread > 0:
            ratio, passes_min, passes_weak = self.calculate_spread_risk_ratio(
                entry_price, stop_loss, spread
            )
            
            tags['spread'] = round(spread, 4)
            tags['risk'] = round(abs(entry_price - stop_loss), 4)
            tags['risk_spread_ratio'] = round(ratio, 1)
            
            if not passes_min:
                # Risk too small vs spread - block
                logger.warning(f"⛔ COST BLOCK: Risk/Spread ratio {ratio:.1f}x < {self.MIN_RISK_SPREAD_MULT}x minimum")
                return FilterDecision(
                    allow=False,
                    reasons=[f"Risk too small vs spread: {ratio:.1f}x (need {self.MIN_RISK_SPREAD_MULT}x)"],
                    tags=tags
                )
            elif not passes_weak:
                # Weak ratio - penalty
                reasons.append(f"Weak risk/spread ratio: {ratio:.1f}x")
                total_penalty += self.SPREAD_RISK_PENALTY / 2
        
        # ===== 2. FEE-ADJUSTED EXPECTED R =====
        if target_price is not None:
            gross_r, fee_r, net_r = self.calculate_net_expected_r(
                entry_price, stop_loss, target_price, fee_rate
            )
            
            tags['expected_r_gross'] = round(gross_r, 2)
            tags['fee_r'] = round(fee_r, 3)
            tags['expected_r_net'] = round(net_r, 2)
            
            if gross_r < self.MIN_GROSS_R:
                # Gross R too low - block
                logger.warning(f"⛔ COST BLOCK: Gross R {gross_r:.2f} < {self.MIN_GROSS_R} minimum")
                return FilterDecision(
                    allow=False,
                    reasons=[f"Expected R too low: {gross_r:.2f}R (need {self.MIN_GROSS_R}R)"],
                    tags=tags
                )
            
            if net_r < self.MIN_NET_R:
                # Net R too low (fees eat the edge) - block
                logger.warning(f"⛔ COST BLOCK: Net R {net_r:.2f} < {self.MIN_NET_R} after fees (fee_R={fee_r:.3f})")
                return FilterDecision(
                    allow=False,
                    reasons=[f"Net R after fees too low: {net_r:.2f}R (fee cost: {fee_r:.3f}R)"],
                    tags=tags
                )
            
            if net_r < gross_r * 0.85:
                # Fees eating more than 15% of R - warning
                reasons.append(f"Fees significant: {fee_r:.3f}R ({fee_r/gross_r*100:.1f}% of gross)")
                total_penalty += self.LOW_R_PENALTY / 2
            
            if net_r >= self.GOOD_NET_R:
                # Good R - bonus
                reasons.append(f"Good net R: {net_r:.2f}R")
                total_penalty -= self.GOOD_R_BONUS
        
        # ===== FINAL DECISION =====
        if total_penalty < 0:
            # Net bonus
            return FilterDecision(
                allow=True,
                score_adjust=abs(total_penalty),
                reasons=reasons if reasons else ["Trade viable after costs"],
                tags=tags
            )
        elif total_penalty > 0:
            # Net penalty
            return FilterDecision(
                allow=True,
                score_adjust=-total_penalty,
                reasons=reasons,
                tags=tags
            )
        else:
            return FilterDecision(
                allow=True,
                reasons=["Trade cost-viable"],
                tags=tags
            )


# Singleton
_cost_filter: Optional[CostFilter] = None


def get_cost_filter(enabled: bool = True) -> CostFilter:
    global _cost_filter
    if _cost_filter is None:
        _cost_filter = CostFilter(enabled=enabled)
    return _cost_filter


def reset_cost_filter():
    global _cost_filter
    _cost_filter = None
