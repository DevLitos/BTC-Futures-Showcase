"""
Fee Calculator Module
=====================

Central source of truth for ALL fee calculations in the system.

CRITICAL: This module MUST be used for:
1. PnL calculations in notifications (Telegram)
2. Trade logs (trade_logs/)
3. R realized calculations
4. Any win/loss determination

KRAKEN FUTURES FEES (BTC):
- Maker: 0.02% (2 bps)
- Taker: 0.05% (5 bps)
- We assume TAKER for worst-case scenarios

Round-trip cost = entry fee + exit fee = 2 * fee_rate

Example with $1000 notional @ taker:
- Entry: $1000 * 0.0005 = $0.50
- Exit:  $1000 * 0.0005 = $0.50
- Total: $1.00 round-trip

RULE: A trade is ONLY profitable if:
  raw_pnl > round_trip_fees
"""

from dataclasses import dataclass
from typing import Tuple
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# KRAKEN FUTURES FEE STRUCTURE
# ============================================================================
@dataclass(frozen=True)
class KrakenFees:
    """Kraken Futures fee structure (as of Jan 2026)."""
    MAKER_RATE: float = 0.0002   # 0.02% = 2 bps
    TAKER_RATE: float = 0.0005   # 0.05% = 5 bps
    
    # For calculations, we use TAKER (worst case)
    # Most market orders hit taker, and SL/TP usually do too
    DEFAULT_RATE: float = 0.0005  # Conservative: assume taker


class FeeCalculator:
    """
    Central fee calculator for all PnL computations.
    
    USAGE:
        calc = FeeCalculator()
        
        # Get true PnL after fees
        pnl_net, fees = calc.calculate_net_pnl(
            entry_price=100000,
            exit_price=100500,
            size=0.001,
            direction='LONG'
        )
        
        # Check if trade is really profitable
        is_winner = pnl_net > 0  # NOT raw pnl!
    """
    
    def __init__(self, fee_rate: float = KrakenFees.DEFAULT_RATE):
        """
        Initialize fee calculator.
        
        Args:
            fee_rate: Fee rate per side (default: taker 0.05%)
        """
        self.fee_rate = fee_rate
    
    def calculate_round_trip_fees(
        self,
        entry_price: float,
        exit_price: float,
        size: float
    ) -> float:
        """
        Calculate round-trip fees for a trade.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price (for exit fee calculation)
            size: Position size in base currency (BTC)
            
        Returns:
            Total fees in USD (entry + exit)
        """
        if size <= 0:
            return 0.0
        
        # Entry fee (based on entry notional)
        entry_notional = entry_price * size
        entry_fee = entry_notional * self.fee_rate
        
        # Exit fee (based on exit notional)
        exit_notional = exit_price * size
        exit_fee = exit_notional * self.fee_rate
        
        total_fees = entry_fee + exit_fee
        
        return total_fees
    
    def calculate_raw_pnl(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        direction: str
    ) -> float:
        """
        Calculate raw PnL WITHOUT fees.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            size: Position size in base currency (BTC)
            direction: 'LONG' or 'SHORT'
            
        Returns:
            Raw PnL in USD (before fees)
        """
        if size <= 0:
            return 0.0
        
        if direction.upper() == 'LONG':
            return (exit_price - entry_price) * size
        else:
            return (entry_price - exit_price) * size
    
    def calculate_net_pnl(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        direction: str
    ) -> Tuple[float, float]:
        """
        Calculate NET PnL after fees.
        
        THIS IS THE METHOD TO USE FOR ALL WIN/LOSS DETERMINATIONS.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            size: Position size in base currency (BTC)
            direction: 'LONG' or 'SHORT'
            
        Returns:
            Tuple of (net_pnl, total_fees)
        """
        raw_pnl = self.calculate_raw_pnl(entry_price, exit_price, size, direction)
        fees = self.calculate_round_trip_fees(entry_price, exit_price, size)
        
        net_pnl = raw_pnl - fees
        
        return net_pnl, fees
    
    def calculate_net_r_realized(
        self,
        entry_price: float,
        exit_price: float,
        initial_stop: float,
        size: float,
        direction: str
    ) -> Tuple[float, float, float]:
        """
        Calculate R realized AFTER FEES.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            initial_stop: Initial stop loss price
            size: Position size in base currency (BTC)
            direction: 'LONG' or 'SHORT'
            
        Returns:
            Tuple of (r_realized_net, net_pnl, fees)
        """
        net_pnl, fees = self.calculate_net_pnl(
            entry_price, exit_price, size, direction
        )
        
        # Calculate initial risk per unit
        initial_risk_per_unit = abs(entry_price - initial_stop)
        if initial_risk_per_unit <= 0:
            logger.warning("Invalid initial risk, returning 0R")
            return 0.0, net_pnl, fees
        
        # Total initial risk in $
        total_initial_risk = initial_risk_per_unit * size
        
        if total_initial_risk <= 0:
            return 0.0, net_pnl, fees
        
        # R realized = net_pnl / initial_risk
        r_realized = net_pnl / total_initial_risk
        
        return r_realized, net_pnl, fees
    
    def is_trade_profitable(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        direction: str
    ) -> bool:
        """
        Determine if a trade is TRULY profitable after fees.
        
        A trade that looks like +$5 raw might be -$1 after fees!
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            size: Position size
            direction: 'LONG' or 'SHORT'
            
        Returns:
            True if net_pnl > 0, False otherwise
        """
        net_pnl, _ = self.calculate_net_pnl(entry_price, exit_price, size, direction)
        return net_pnl > 0
    
    def get_breakeven_price(
        self,
        entry_price: float,
        size: float,
        direction: str
    ) -> float:
        """
        Get the true breakeven price INCLUDING fees.
        
        For LONG: BE = entry + (fees / size)
        For SHORT: BE = entry - (fees / size)
        
        Args:
            entry_price: Entry price
            size: Position size
            direction: 'LONG' or 'SHORT'
            
        Returns:
            Breakeven price that covers all fees
        """
        if size <= 0:
            return entry_price
        
        # Estimate exit at entry (for fee calculation)
        # This is approximate but close enough
        round_trip_fees = self.calculate_round_trip_fees(
            entry_price, entry_price, size
        )
        
        fee_per_unit = round_trip_fees / size
        
        if direction.upper() == 'LONG':
            return entry_price + fee_per_unit
        else:
            return entry_price - fee_per_unit
    
    def format_pnl_summary(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        direction: str,
        initial_stop: float | None = None
    ) -> str:
        """
        Format a complete PnL summary with fees for logging/notification.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            size: Position size
            direction: 'LONG' or 'SHORT'
            initial_stop: Optional initial stop for R calculation
            
        Returns:
            Formatted string with all PnL details
        """
        raw_pnl = self.calculate_raw_pnl(entry_price, exit_price, size, direction)
        net_pnl, fees = self.calculate_net_pnl(entry_price, exit_price, size, direction)
        
        lines = [
            f"Entry: {entry_price:.2f} → Exit: {exit_price:.2f}",
            f"Size: {size:.6f} BTC",
            f"Raw PnL: ${raw_pnl:+.2f}",
            f"Fees: -${fees:.2f}",
            f"Net PnL: ${net_pnl:+.2f}",
        ]
        
        if initial_stop is not None:
            r_net, _, _ = self.calculate_net_r_realized(
                entry_price, exit_price, initial_stop, size, direction
            )
            lines.append(f"R Realized (net): {r_net:+.2f}R")
        
        result_emoji = "✅" if net_pnl > 0 else "❌" if net_pnl < 0 else "➖"
        lines.insert(0, f"{result_emoji} Trade Result ({direction})")
        
        return "\n".join(lines)


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================
_fee_calculator: FeeCalculator | None = None


def get_fee_calculator() -> FeeCalculator:
    """Get the singleton fee calculator instance."""
    global _fee_calculator
    if _fee_calculator is None:
        _fee_calculator = FeeCalculator()
    return _fee_calculator


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================
def calculate_net_pnl(
    entry_price: float,
    exit_price: float,
    size: float,
    direction: str
) -> Tuple[float, float]:
    """Convenience function for calculating net PnL."""
    return get_fee_calculator().calculate_net_pnl(
        entry_price, exit_price, size, direction
    )


def calculate_net_r(
    entry_price: float,
    exit_price: float,
    initial_stop: float,
    size: float,
    direction: str
) -> Tuple[float, float, float]:
    """Convenience function for calculating net R realized."""
    return get_fee_calculator().calculate_net_r_realized(
        entry_price, exit_price, initial_stop, size, direction
    )


def is_profitable_after_fees(
    entry_price: float,
    exit_price: float,
    size: float,
    direction: str
) -> bool:
    """Convenience function to check if trade is profitable after fees."""
    return get_fee_calculator().is_trade_profitable(
        entry_price, exit_price, size, direction
    )
