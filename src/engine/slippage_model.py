"""
Slippage Model - Adjusts RR calculations for real-world execution costs

Features:
1. Order-type aware slippage (LIMIT vs MARKET/taker)
2. Volatility-adaptive slippage adjustment
3. Worst-case RR calculation for entry decisions
4. Spread cost integration
5. DUAL MODE: Operational (P90-P95) vs Stress (P99)
   - Operational: Used for trade DECISIONS (blocking)
   - Stress: Used for ALERTS only (never blocks)

CRITICAL PRINCIPLE (Jan 20, 2026):
- Stress slippage is informational, NEVER blocking
- If stress shows danger but operational passes → trade with warning
- Only operational slippage affects entry decisions
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class OrderType(str, Enum):
    """Order execution type."""
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class SlippageMode(str, Enum):
    """Slippage calculation mode."""
    OPERATIONAL = "OPERATIONAL"   # P90-P95 - for decisions
    STRESS = "STRESS"             # P99 - for alerts only, never blocks


@dataclass
class SlippageConfig:
    """
    Slippage configuration by order type.
    Values in basis points (1 bps = 0.01%).
    
    UPDATED (Jan 20, 2026): 
    - Ultra-realistic values for BTC/USD perpetual on Kraken Futures
    - Kraken BTC perpetuals have tight spreads (~$1-2 on $90k = 1-2bps)
    - Small orders (<$1000) see essentially zero slippage
    
    CALIBRATION NOTE:
    For $90,000 BTC with $258 risk (0.29% ATR):
    - 1 bps = $9 slippage
    - Total slippage impact should be <5% of risk for liquid markets
    - So max total slippage ~ $13 = ~1.5 bps total
    
    PRINCIPLE: Operational slippage = decision filter
               Stress slippage = information/warning only
    """
    # Base slippage by order type (bps) - CALIBRATED FOR BTC/USD
    # Kraken Futures BTC/USD has <$5 spread typically
    LIMIT_SLIPPAGE_BPS: float = 0.5       # 0.005% for post-only limit (essentially zero)
    TAKER_SLIPPAGE_BPS: float = 1.0       # 0.01% for market/taker (crosses spread)
    
    # Exit slippage
    EXIT_SLIPPAGE_BPS: float = 1.0        # 0.01% for TP exits
    SL_SLIPPAGE_BPS: float = 2.0          # 0.02% for SL (slightly worse in fast moves)
    
    # =========================================================================
    # OPERATIONAL MODE: P90-P95 caps for DECISION making
    # =========================================================================
    P95_ENTRY_SLIP_BPS: float = 2.0       # Cap entry slippage at P95
    P95_EXIT_SLIP_BPS: float = 3.0        # Cap exit slippage at P95
    P95_SL_SLIP_BPS: float = 5.0          # Cap SL slippage at P95
    
    # =========================================================================
    # STRESS MODE: P99 values for ALERTING only (never blocks)
    # Even P99 should be reasonable for BTC
    # =========================================================================
    P99_ENTRY_SLIP_BPS: float = 5.0       # P99 extreme entry slippage
    P99_EXIT_SLIP_BPS: float = 8.0        # P99 extreme exit slippage
    P99_SL_SLIP_BPS: float = 15.0         # P99 extreme SL slippage (flash crash)
    
    # Stress alert thresholds (if stress RR is X% worse than operational)
    STRESS_ALERT_THRESHOLD_PCT: float = 20.0  # Alert if stress is 20% worse
    
    # Volatility multiplier bounds
    VOL_MULT_MIN: float = 1.0             # Minimum volatility multiplier
    VOL_MULT_MAX: float = 1.5             # Reduced further - BTC is liquid
    
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
        volatility_ratio: float = 1.0,
        mode: SlippageMode = SlippageMode.OPERATIONAL
    ) -> float:
        """
        Get entry slippage in basis points.
        
        Args:
            order_type: LIMIT or MARKET
            volatility_ratio: current_atr / avg_atr
            mode: OPERATIONAL (P95 based) or STRESS (P99 based, never blocks)
        
        Returns:
            Slippage in bps (including fees)
            
        STRESS MODE: Uses P99 values directly (not as caps)
        This shows worst-case scenario for information only.
        """
        vol_mult = self.get_volatility_multiplier(volatility_ratio)
        
        if order_type == OrderType.LIMIT:
            fee = self.config.MAKER_FEE_BPS
            if mode == SlippageMode.STRESS:
                # Stress: use P99 value directly
                return self.config.P99_ENTRY_SLIP_BPS + fee
            else:
                # Operational: normal calculation with P95 cap
                base_slip = self.config.LIMIT_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_ENTRY_SLIP_BPS)
                return adjusted_slip + fee
        else:  # MARKET
            fee = self.config.TAKER_FEE_BPS
            if mode == SlippageMode.STRESS:
                return self.config.P99_ENTRY_SLIP_BPS + fee
            else:
                base_slip = self.config.TAKER_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_ENTRY_SLIP_BPS)
                return adjusted_slip + fee
    
    def get_exit_slippage_bps(
        self, 
        exit_type: Literal["TP", "SL", "TRAILING"],
        volatility_ratio: float = 1.0,
        mode: SlippageMode = SlippageMode.OPERATIONAL
    ) -> float:
        """
        Get exit slippage in basis points.
        
        Args:
            exit_type: Type of exit
            volatility_ratio: current_atr / avg_atr
            mode: OPERATIONAL (P95 based) or STRESS (P99 based, never blocks)
        
        Returns:
            Slippage in bps (including fees)
            
        STRESS MODE: Uses P99 values directly for worst-case analysis.
        """
        vol_mult = self.get_volatility_multiplier(volatility_ratio)
        fee = self.config.TAKER_FEE_BPS  # Exits usually taker
        
        if exit_type == "SL" or exit_type == "TRAILING":
            # SL/trailing usually execute as market in fast moves
            if mode == SlippageMode.STRESS:
                # Stress: use P99 value directly
                return self.config.P99_SL_SLIP_BPS + fee
            else:
                base_slip = self.config.SL_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_SL_SLIP_BPS)
                return adjusted_slip + fee
        else:  # TP
            if mode == SlippageMode.STRESS:
                return self.config.P99_EXIT_SLIP_BPS + fee
            else:
                base_slip = self.config.EXIT_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_EXIT_SLIP_BPS)
                return adjusted_slip + fee
    
    def calculate_worst_case_entry(
        self,
        entry_price: float,
        direction: str,
        order_type: OrderType,
        volatility_ratio: float = 1.0,
        mode: SlippageMode = SlippageMode.OPERATIONAL
    ) -> float:
        """
        Calculate worst-case entry price after slippage.
        
        For LONG: entry_wc = entry_price * (1 + slip)
        For SHORT: entry_wc = entry_price * (1 - slip)
        """
        slip_bps = self.get_entry_slippage_bps(order_type, volatility_ratio, mode)
        slip_pct = slip_bps / 10000  # Convert bps to percentage
        
        if direction.upper() == 'LONG':
            return entry_price * (1 + slip_pct)
        else:  # SHORT
            return entry_price * (1 - slip_pct)
    
    def calculate_worst_case_sl(
        self,
        stop_loss: float,
        direction: str,
        volatility_ratio: float = 1.0,
        mode: SlippageMode = SlippageMode.OPERATIONAL
    ) -> float:
        """
        Calculate worst-case SL execution price.
        
        For LONG: sl_wc = stop_loss * (1 - slip) (fills lower)
        For SHORT: sl_wc = stop_loss * (1 + slip) (fills higher)
        """
        slip_bps = self.get_exit_slippage_bps("SL", volatility_ratio, mode)
        slip_pct = slip_bps / 10000
        
        if direction.upper() == 'LONG':
            return stop_loss * (1 - slip_pct)
        else:  # SHORT
            return stop_loss * (1 + slip_pct)
    
    def calculate_worst_case_tp(
        self,
        take_profit: float,
        direction: str,
        volatility_ratio: float = 1.0,
        mode: SlippageMode = SlippageMode.OPERATIONAL
    ) -> float:
        """
        Calculate worst-case TP execution price.
        
        For LONG: tp_wc = take_profit * (1 - slip) (fills lower)
        For SHORT: tp_wc = take_profit * (1 + slip) (fills higher)
        """
        slip_bps = self.get_exit_slippage_bps("TP", volatility_ratio, mode)
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
        include_detailed_log: bool = False,
        mode: SlippageMode = SlippageMode.OPERATIONAL
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
            mode: OPERATIONAL (P95 cap, for decisions) or STRESS (P99, alerts only)
        
        Returns:
            Worst-case RR (may be negative if setup is trash)
        """
        # Calculate worst-case prices
        entry_wc = self.calculate_worst_case_entry(
            entry_price, direction, order_type, volatility_ratio, mode
        )
        sl_wc = self.calculate_worst_case_sl(stop_loss, direction, volatility_ratio, mode)
        tp_wc = self.calculate_worst_case_tp(take_profit, direction, volatility_ratio, mode)
        
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
                f"📊 RR Analysis [{mode.value}] | Ideal: {rr_ideal:.2f} → Worst-case: {rr_wc:.2f} "
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
    
    def calculate_dual_rr(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str,
        order_type: OrderType = OrderType.LIMIT,
        volatility_ratio: float = 1.0
    ) -> Tuple[float, float, bool, str]:
        """
        Calculate BOTH operational and stress RR in one call.
        
        CRITICAL PRINCIPLE:
        - Operational RR (P95) is used for ENTRY DECISIONS
        - Stress RR (P99) is for ALERTS only, NEVER BLOCKS
        
        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            take_profit: Take profit price (TP1)
            direction: 'LONG' or 'SHORT'
            order_type: LIMIT or MARKET entry
            volatility_ratio: current_atr / avg_atr
        
        Returns:
            Tuple of:
            - rr_operational: P95 RR for entry decision
            - rr_stress: P99 RR for information only
            - stress_alert: True if stress RR is significantly worse
            - alert_message: Warning message if alert triggered
        """
        rr_operational = self.calculate_worst_case_rr(
            entry_price, stop_loss, take_profit, direction,
            order_type, volatility_ratio,
            mode=SlippageMode.OPERATIONAL
        )
        
        rr_stress = self.calculate_worst_case_rr(
            entry_price, stop_loss, take_profit, direction,
            order_type, volatility_ratio,
            mode=SlippageMode.STRESS
        )
        
        # Check if stress is significantly worse
        stress_alert = False
        alert_message = ""
        
        if rr_operational > 0:
            degradation_pct = ((rr_operational - rr_stress) / rr_operational) * 100
            if degradation_pct > self.config.STRESS_ALERT_THRESHOLD_PCT:
                stress_alert = True
                alert_message = (
                    f"⚠️ STRESS ALERT: RR degrades {degradation_pct:.0f}% under P99 "
                    f"(operational={rr_operational:.2f} → stress={rr_stress:.2f}). "
                    f"Trade proceeds but monitor closely."
                )
                logger.warning(alert_message)
        
        return rr_operational, rr_stress, stress_alert, alert_message


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
        from src.engine.slippage_model import calculate_worst_case_rr, OrderType
        
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
