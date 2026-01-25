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
    
    FIX (Jan 22, 2026): Punto 13 - Calibrated with realistic Kraken values
    Based on actual fills from small orders ($100-$500 notional):
    - Limit orders: <0.3 bps average slippage
    - Market orders: ~0.8 bps average slippage
    - SL in fast moves: ~1.5 bps average slippage
    
    Previous values were too conservative, penalizing RR calculations.
    
    CALIBRATION DATA (BTC/USD on Kraken Futures):
    - Typical spread: $1-3 (~1-3 bps on $100k BTC)
    - Small orders (<$1000): virtually no slippage
    - Depth @1%: ~$10M+ (very liquid)
    """
    # Base slippage by order type (bps) - RECALIBRATED FOR ACTUAL FILLS
    LIMIT_SLIPPAGE_BPS: float = 0.3       # 0.003% for post-only limit (near zero)
    TAKER_SLIPPAGE_BPS: float = 0.8       # 0.008% for market/taker (crosses spread)
    
    # Exit slippage
    EXIT_SLIPPAGE_BPS: float = 0.8        # 0.008% for TP exits (same as taker)
    SL_SLIPPAGE_BPS: float = 1.5          # 0.015% for SL (slightly worse in fast moves)
    
    # =========================================================================
    # OPERATIONAL MODE: P90-P95 caps for DECISION making
    # FIX (Jan 25, 2026): Reduced caps - were causing ~0.65R degradation
    # With liquid BTC and small orders, real slippage is much lower
    # =========================================================================
    P95_ENTRY_SLIP_BPS: float = 0.5       # 🔥 FIX: was 1.5 → too conservative
    P95_EXIT_SLIP_BPS: float = 1.0        # 🔥 FIX: was 2.0 → too conservative
    P95_SL_SLIP_BPS: float = 2.0          # 🔥 FIX: was 4.0 → too conservative
    
    # =========================================================================
    # STRESS MODE: P99 values for ALERTING only (never blocks)
    # =========================================================================
    P99_ENTRY_SLIP_BPS: float = 4.0       # P99 extreme entry slippage (was 5.0)
    P99_EXIT_SLIP_BPS: float = 6.0        # P99 extreme exit slippage (was 8.0)
    P99_SL_SLIP_BPS: float = 12.0         # P99 extreme SL slippage (was 15.0)
    
    # Stress alert thresholds (if stress RR is X% worse than operational)
    STRESS_ALERT_THRESHOLD_PCT: float = 25.0  # Alert if stress is 25% worse (was 20%)
    
    # Volatility multiplier bounds
    VOL_MULT_MIN: float = 1.0             # Minimum volatility multiplier
    VOL_MULT_MAX: float = 1.3             # Reduced from 1.5 - BTC is very liquid
    
    # Volatility thresholds for adaptive slippage
    VOL_RATIO_NORMAL: float = 1.2         # Below this = normal volatility
    VOL_RATIO_HIGH: float = 2.0           # Above this = high volatility (was 1.8)
    
    # Commission (Kraken Futures actual fees)
    MAKER_FEE_BPS: float = 2.0            # 0.02% maker fee
    TAKER_FEE_BPS: float = 5.0            # 0.05% taker fee
    
    # =========================================================================
    # ADAPTIVE SLIPPAGE SETTINGS (Jan 23, 2026)
    # Track real slippage and adjust estimates when consistently off
    # =========================================================================
    ADAPTIVE_WINDOW: int = 50             # Number of recent fills to track
    ADAPTIVE_PERCENTILE: float = 75.0     # Use P75 of real slippage
    ADAPTIVE_BUFFER_MULT: float = 1.2     # 20% buffer on top of P75
    ADAPTIVE_UPDATE_THRESHOLD: float = 1.5  # Update if real > 1.5x estimate


class AdaptiveSlippageTracker:
    """
    Tracks real slippage from fills and provides adaptive estimates.
    
    PRINCIPLE: If real slippage consistently exceeds estimates, increase estimates.
    Uses P75 of recent fills + 20% buffer as adaptive estimate.
    """
    
    def __init__(self, config: Optional[SlippageConfig] = None):
        self.config = config or SlippageConfig()
        # Separate tracking for different scenarios
        self._entry_limit_slippages: list[float] = []
        self._entry_market_slippages: list[float] = []
        self._exit_tp_slippages: list[float] = []
        self._exit_sl_slippages: list[float] = []
        
        # Adaptive estimates (start with config defaults)
        self._adaptive_limit_bps: Optional[float] = None
        self._adaptive_market_bps: Optional[float] = None
        self._adaptive_tp_bps: Optional[float] = None
        self._adaptive_sl_bps: Optional[float] = None
        
        logger.info("AdaptiveSlippageTracker initialized")
    
    def record_fill_slippage(
        self, 
        fill_type: str,  # 'entry_limit', 'entry_market', 'exit_tp', 'exit_sl'
        expected_price: float,
        actual_fill_price: float,
        direction: str
    ) -> float:
        """
        Record actual slippage from a fill.
        
        Args:
            fill_type: Type of fill
            expected_price: Price we expected
            actual_fill_price: Actual fill price
            direction: 'LONG' or 'SHORT'
            
        Returns:
            float: Recorded slippage in bps
        """
        if expected_price <= 0:
            return 0.0
            
        # Calculate slippage (positive = adverse, negative = favorable)
        if direction.upper() == 'LONG':
            if fill_type.startswith('entry'):
                # Entry LONG: slippage if fill > expected
                slippage_pct = (actual_fill_price - expected_price) / expected_price
            else:
                # Exit LONG: slippage if fill < expected
                slippage_pct = (expected_price - actual_fill_price) / expected_price
        else:  # SHORT
            if fill_type.startswith('entry'):
                # Entry SHORT: slippage if fill < expected
                slippage_pct = (expected_price - actual_fill_price) / expected_price
            else:
                # Exit SHORT: slippage if fill > expected
                slippage_pct = (actual_fill_price - expected_price) / expected_price
        
        slippage_bps = slippage_pct * 10000  # Convert to bps
        # Only track adverse slippage (positive)
        slippage_bps = max(0, slippage_bps)
        
        # Store in appropriate list
        window = self.config.ADAPTIVE_WINDOW
        if fill_type == 'entry_limit':
            self._entry_limit_slippages.append(slippage_bps)
            self._entry_limit_slippages = self._entry_limit_slippages[-window:]
        elif fill_type == 'entry_market':
            self._entry_market_slippages.append(slippage_bps)
            self._entry_market_slippages = self._entry_market_slippages[-window:]
        elif fill_type == 'exit_tp':
            self._exit_tp_slippages.append(slippage_bps)
            self._exit_tp_slippages = self._exit_tp_slippages[-window:]
        elif fill_type == 'exit_sl':
            self._exit_sl_slippages.append(slippage_bps)
            self._exit_sl_slippages = self._exit_sl_slippages[-window:]
        
        # Update adaptive estimates if we have enough data
        self._update_adaptive_estimates()
        
        return slippage_bps
    
    def _calculate_adaptive_estimate(self, slippages: list[float], default: float) -> Optional[float]:
        """Calculate P75 + buffer from slippage history."""
        if len(slippages) < 10:  # Need minimum samples
            return None
        
        import numpy as np
        p75 = float(np.percentile(slippages, self.config.ADAPTIVE_PERCENTILE))
        buffered = p75 * self.config.ADAPTIVE_BUFFER_MULT
        
        # Only use adaptive if it's significantly higher than default
        if buffered > default * self.config.ADAPTIVE_UPDATE_THRESHOLD:
            return buffered
        return None
    
    def _update_adaptive_estimates(self):
        """Update all adaptive estimates from recorded data."""
        self._adaptive_limit_bps = self._calculate_adaptive_estimate(
            self._entry_limit_slippages, 
            self.config.LIMIT_SLIPPAGE_BPS
        )
        self._adaptive_market_bps = self._calculate_adaptive_estimate(
            self._entry_market_slippages,
            self.config.TAKER_SLIPPAGE_BPS
        )
        self._adaptive_tp_bps = self._calculate_adaptive_estimate(
            self._exit_tp_slippages,
            self.config.EXIT_SLIPPAGE_BPS
        )
        self._adaptive_sl_bps = self._calculate_adaptive_estimate(
            self._exit_sl_slippages,
            self.config.SL_SLIPPAGE_BPS
        )
        
        # Log when adaptive estimates kick in
        if self._adaptive_limit_bps:
            logger.info(f"📊 Adaptive LIMIT slippage: {self._adaptive_limit_bps:.2f} bps (default: {self.config.LIMIT_SLIPPAGE_BPS})")
        if self._adaptive_market_bps:
            logger.info(f"📊 Adaptive MARKET slippage: {self._adaptive_market_bps:.2f} bps (default: {self.config.TAKER_SLIPPAGE_BPS})")
        if self._adaptive_sl_bps:
            logger.info(f"📊 Adaptive SL slippage: {self._adaptive_sl_bps:.2f} bps (default: {self.config.SL_SLIPPAGE_BPS})")
    
    def get_entry_slippage_bps(self, order_type: OrderType) -> float:
        """Get entry slippage, preferring adaptive estimate if available."""
        if order_type == OrderType.LIMIT:
            return self._adaptive_limit_bps or self.config.LIMIT_SLIPPAGE_BPS
        else:
            return self._adaptive_market_bps or self.config.TAKER_SLIPPAGE_BPS
    
    def get_exit_slippage_bps(self, exit_type: str) -> float:
        """Get exit slippage, preferring adaptive estimate if available."""
        if exit_type in ["SL", "TRAILING"]:
            return self._adaptive_sl_bps or self.config.SL_SLIPPAGE_BPS
        else:
            return self._adaptive_tp_bps or self.config.EXIT_SLIPPAGE_BPS
    
    def get_status(self) -> dict:
        """Get current adaptive slippage status."""
        return {
            'entry_limit_samples': len(self._entry_limit_slippages),
            'entry_market_samples': len(self._entry_market_slippages),
            'exit_tp_samples': len(self._exit_tp_slippages),
            'exit_sl_samples': len(self._exit_sl_slippages),
            'adaptive_limit_bps': self._adaptive_limit_bps,
            'adaptive_market_bps': self._adaptive_market_bps,
            'adaptive_tp_bps': self._adaptive_tp_bps,
            'adaptive_sl_bps': self._adaptive_sl_bps,
        }


# Singleton for adaptive tracker
_adaptive_tracker: Optional[AdaptiveSlippageTracker] = None


def get_adaptive_slippage_tracker() -> AdaptiveSlippageTracker:
    """Get singleton AdaptiveSlippageTracker instance."""
    global _adaptive_tracker
    if _adaptive_tracker is None:
        _adaptive_tracker = AdaptiveSlippageTracker()
    return _adaptive_tracker


class SlippageModel:
    """
    Calculates realistic slippage and worst-case RR.
    
    NOW WITH ADAPTIVE SUPPORT (Jan 23, 2026):
    - Uses AdaptiveSlippageTracker when available
    - Falls back to config defaults
    - Call tracker.record_fill_slippage() on each fill to improve estimates
    
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
    
    def __init__(self, config: Optional[SlippageConfig] = None, use_adaptive: bool = True):
        self.config = config or SlippageConfig()
        self._adaptive_tracker = get_adaptive_slippage_tracker() if use_adaptive else None
    
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
            
        ADAPTIVE (Jan 23, 2026): Uses P75 of recent fills + buffer if real slippage
        consistently exceeds estimates.
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
                # Operational: use adaptive estimate if available, else config
                if self._adaptive_tracker:
                    base_slip = self._adaptive_tracker.get_entry_slippage_bps(order_type)
                else:
                    base_slip = self.config.LIMIT_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_ENTRY_SLIP_BPS)
                return adjusted_slip + fee
        else:  # MARKET
            fee = self.config.TAKER_FEE_BPS
            if mode == SlippageMode.STRESS:
                return self.config.P99_ENTRY_SLIP_BPS + fee
            else:
                # Operational: use adaptive estimate if available
                if self._adaptive_tracker:
                    base_slip = self._adaptive_tracker.get_entry_slippage_bps(order_type)
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
            
        ADAPTIVE (Jan 23, 2026): Uses P75 of recent fills + buffer if real slippage
        consistently exceeds estimates.
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
                # Operational: use adaptive estimate if available
                if self._adaptive_tracker:
                    base_slip = self._adaptive_tracker.get_exit_slippage_bps(exit_type)
                else:
                    base_slip = self.config.SL_SLIPPAGE_BPS
                adjusted_slip = base_slip * vol_mult
                adjusted_slip = min(adjusted_slip, self.config.P95_SL_SLIP_BPS)
                return adjusted_slip + fee
        else:  # TP
            if mode == SlippageMode.STRESS:
                return self.config.P99_EXIT_SLIP_BPS + fee
            else:
                if self._adaptive_tracker:
                    base_slip = self._adaptive_tracker.get_exit_slippage_bps("TP")
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
        direction: str | None,
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
        # Guard: direction required for calculation
        if direction is None:
            return 0.0
        
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
        from src.engine.execution import calculate_worst_case_rr, OrderType
        
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
