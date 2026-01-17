"""
Slippage Model - Adjusts RR calculations for real-world execution costs

Features:
1. Order-type aware slippage (LIMIT vs MARKET/taker)
2. Volatility-adaptive slippage adjustment
3. Worst-case RR calculation for entry decisions
4. Spread cost integration
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class OrderType(str, Enum):
    """Order execution type."""
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass
class SlippageConfig:
    """
    Slippage configuration by order type.
    Values in basis points (1 bps = 0.01%).
    """
    # Base slippage by order type (bps)
    LIMIT_SLIPPAGE_BPS: float = 3.0       # 0.03% for post-only limit
    TAKER_SLIPPAGE_BPS: float = 10.0      # 0.10% for market/taker orders
    
    # Exit slippage (usually market for speed)
    EXIT_SLIPPAGE_BPS: float = 8.0        # 0.08% for exits (often market)
    SL_SLIPPAGE_BPS: float = 15.0         # 0.15% for SL (market in volatility)
    
    # Volatility multiplier bounds
    VOL_MULT_MIN: float = 1.0             # Minimum volatility multiplier
    VOL_MULT_MAX: float = 2.5             # Maximum volatility multiplier
    
    # Volatility thresholds for adaptive slippage
    # Based on ATR ratio: current_atr / sma(atr, 100)
    VOL_RATIO_NORMAL: float = 1.2         # Below this = normal volatility
    VOL_RATIO_HIGH: float = 1.8           # Above this = high volatility
    
    # Commission (added to slippage for total cost)
    MAKER_FEE_BPS: float = 2.0            # 0.02% maker fee
    TAKER_FEE_BPS: float = 5.0            # 0.05% taker fee


class SlippageModel:
    """
    Calculates realistic slippage and worst-case RR.
    
    Usage:
        model = SlippageModel()
        rr_wc = model.calculate_worst_case_rr(
            entry_price=97000,
            stop_loss=96500,
            take_profit=98000,
            direction='LONG',
            order_type=OrderType.LIMIT,
            volatility_ratio=1.3
        )
        if rr_wc >= MIN_RR:
            # Proceed with entry
    """
    
    def __init__(self, config: Optional[SlippageConfig] = None):
        self.config = config or SlippageConfig()
    
    def get_volatility_multiplier(self, volatility_ratio: float) -> float:
        """
        Get slippage multiplier based on current volatility.
        
        Args:
            volatility_ratio: current_atr / sma(atr, 100)
        
        Returns:
            Multiplier between VOL_MULT_MIN and VOL_MULT_MAX
        """
        if volatility_ratio <= self.config.VOL_RATIO_NORMAL:
            return self.config.VOL_MULT_MIN
        elif volatility_ratio >= self.config.VOL_RATIO_HIGH:
            return self.config.VOL_MULT_MAX
        else:
            # Linear interpolation
            ratio_range = self.config.VOL_RATIO_HIGH - self.config.VOL_RATIO_NORMAL
            mult_range = self.config.VOL_MULT_MAX - self.config.VOL_MULT_MIN
            progress = (volatility_ratio - self.config.VOL_RATIO_NORMAL) / ratio_range
            return self.config.VOL_MULT_MIN + progress * mult_range
    
    def get_entry_slippage_bps(
        self, 
        order_type: OrderType,
        volatility_ratio: float = 1.0
    ) -> float:
        """
        Get entry slippage in basis points.
        
        Args:
            order_type: LIMIT or MARKET
            volatility_ratio: current_atr / avg_atr
        
        Returns:
            Slippage in bps (including fees)
        """
        vol_mult = self.get_volatility_multiplier(volatility_ratio)
        
        if order_type == OrderType.LIMIT:
            base_slip = self.config.LIMIT_SLIPPAGE_BPS
            fee = self.config.MAKER_FEE_BPS
        else:  # MARKET
            base_slip = self.config.TAKER_SLIPPAGE_BPS
            fee = self.config.TAKER_FEE_BPS
        
        # Apply volatility multiplier to slippage (not fees)
        adjusted_slip = base_slip * vol_mult
        
        return adjusted_slip + fee
    
    def get_exit_slippage_bps(
        self, 
        exit_type: Literal["TP", "SL", "TRAILING"],
        volatility_ratio: float = 1.0
    ) -> float:
        """
        Get exit slippage in basis points.
        
        Args:
            exit_type: Type of exit
            volatility_ratio: current_atr / avg_atr
        
        Returns:
            Slippage in bps (including fees)
        """
        vol_mult = self.get_volatility_multiplier(volatility_ratio)
        
        if exit_type == "SL" or exit_type == "TRAILING":
            # SL/trailing usually execute as market in fast moves
            base_slip = self.config.SL_SLIPPAGE_BPS
        else:  # TP
            base_slip = self.config.EXIT_SLIPPAGE_BPS
        
        fee = self.config.TAKER_FEE_BPS  # Exits usually taker
        
        return (base_slip * vol_mult) + fee
    
    def calculate_worst_case_entry(
        self,
        entry_price: float,
        direction: str,
        order_type: OrderType,
        volatility_ratio: float = 1.0
    ) -> float:
        """
        Calculate worst-case entry price after slippage.
        
        For LONG: entry_wc = entry_price * (1 + slip)
        For SHORT: entry_wc = entry_price * (1 - slip)
        """
        slip_bps = self.get_entry_slippage_bps(order_type, volatility_ratio)
        slip_pct = slip_bps / 10000  # Convert bps to percentage
        
        if direction.upper() == 'LONG':
            return entry_price * (1 + slip_pct)
        else:  # SHORT
            return entry_price * (1 - slip_pct)
    
    def calculate_worst_case_sl(
        self,
        stop_loss: float,
        direction: str,
        volatility_ratio: float = 1.0
    ) -> float:
        """
        Calculate worst-case SL execution price.
        
        For LONG: sl_wc = stop_loss * (1 - slip) (fills lower)
        For SHORT: sl_wc = stop_loss * (1 + slip) (fills higher)
        """
        slip_bps = self.get_exit_slippage_bps("SL", volatility_ratio)
        slip_pct = slip_bps / 10000
        
        if direction.upper() == 'LONG':
            return stop_loss * (1 - slip_pct)
        else:  # SHORT
            return stop_loss * (1 + slip_pct)
    
    def calculate_worst_case_tp(
        self,
        take_profit: float,
        direction: str,
        volatility_ratio: float = 1.0
    ) -> float:
        """
        Calculate worst-case TP execution price.
        
        For LONG: tp_wc = take_profit * (1 - slip) (fills lower)
        For SHORT: tp_wc = take_profit * (1 + slip) (fills higher)
        """
        slip_bps = self.get_exit_slippage_bps("TP", volatility_ratio)
        slip_pct = slip_bps / 10000
        
        if direction.upper() == 'LONG':
            return take_profit * (1 - slip_pct)
        else:  # SHORT
            return take_profit * (1 + slip_pct)
    
    def calculate_worst_case_rr(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str,
        order_type: OrderType = OrderType.LIMIT,
        volatility_ratio: float = 1.0,
        include_detailed_log: bool = False
    ) -> float:
        """
        Calculate worst-case Risk:Reward ratio accounting for slippage.
        
        This is the REAL RR you should use for entry decisions.
        
        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            take_profit: Take profit price (TP1)
            direction: 'LONG' or 'SHORT'
            order_type: LIMIT or MARKET entry
            volatility_ratio: current_atr / avg_atr
            include_detailed_log: Log detailed breakdown
        
        Returns:
            Worst-case RR (may be negative if setup is trash)
        """
        # Calculate worst-case prices
        entry_wc = self.calculate_worst_case_entry(
            entry_price, direction, order_type, volatility_ratio
        )
        sl_wc = self.calculate_worst_case_sl(stop_loss, direction, volatility_ratio)
        tp_wc = self.calculate_worst_case_tp(take_profit, direction, volatility_ratio)
        
        # Calculate risk and reward
        if direction.upper() == 'LONG':
            risk_wc = entry_wc - sl_wc
            reward_wc = tp_wc - entry_wc
        else:  # SHORT
            risk_wc = sl_wc - entry_wc
            reward_wc = entry_wc - tp_wc
        
        # Avoid division by zero
        if risk_wc <= 0:
            logger.warning(
                f"⚠️ Invalid worst-case risk: entry_wc={entry_wc:.2f}, "
                f"sl_wc={sl_wc:.2f}, risk={risk_wc:.4f}"
            )
            return 0.0
        
        rr_wc = reward_wc / risk_wc
        
        if include_detailed_log:
            # Calculate theoretical RR for comparison
            if direction.upper() == 'LONG':
                risk_ideal = entry_price - stop_loss
                reward_ideal = take_profit - entry_price
            else:
                risk_ideal = stop_loss - entry_price
                reward_ideal = entry_price - take_profit
            
            rr_ideal = reward_ideal / risk_ideal if risk_ideal > 0 else 0
            rr_degradation = ((rr_ideal - rr_wc) / rr_ideal * 100) if rr_ideal > 0 else 0
            
            logger.info(
                f"📊 RR Analysis | Ideal: {rr_ideal:.2f} → Worst-case: {rr_wc:.2f} "
                f"(−{rr_degradation:.1f}%) | Vol ratio: {volatility_ratio:.2f}"
            )
            logger.debug(
                f"   Entry: {entry_price:.2f} → {entry_wc:.2f} | "
                f"SL: {stop_loss:.2f} → {sl_wc:.2f} | "
                f"TP: {take_profit:.2f} → {tp_wc:.2f}"
            )
        
        return rr_wc
    
    def get_slippage_adjusted_size(
        self,
        base_size: float,
        volatility_ratio: float
    ) -> float:
        """
        Optionally reduce position size in high volatility.
        High slippage environments = reduce size for same dollar risk.
        
        Args:
            base_size: Base position size
            volatility_ratio: current_atr / avg_atr
        
        Returns:
            Adjusted position size
        """
        vol_mult = self.get_volatility_multiplier(volatility_ratio)
        
        # If slippage is 2x, reduce size by ~15% to maintain same dollar exposure
        if vol_mult > 1.5:
            size_reduction = 1 - (vol_mult - 1) * 0.15
            size_reduction = max(0.7, size_reduction)  # Min 70% of size
            return base_size * size_reduction
        
        return base_size


# Singleton instance for easy import
_slippage_model: Optional[SlippageModel] = None


def get_slippage_model() -> SlippageModel:
    """Get singleton SlippageModel instance."""
    global _slippage_model
    if _slippage_model is None:
        _slippage_model = SlippageModel()
    return _slippage_model


def calculate_worst_case_rr(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
    order_type: OrderType = OrderType.LIMIT,
    volatility_ratio: float = 1.0
) -> float:
    """
    Convenience function to calculate worst-case RR.
    
    Usage:
        from engine.slippage_model import calculate_worst_case_rr, OrderType
        
        rr_wc = calculate_worst_case_rr(
            entry_price=97000,
            stop_loss=96500,
            take_profit=98000,
            direction='LONG',
            volatility_ratio=1.3
        )
    """
    return get_slippage_model().calculate_worst_case_rr(
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        direction=direction,
        order_type=order_type,
        volatility_ratio=volatility_ratio
    )
