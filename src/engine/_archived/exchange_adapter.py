"""
Exchange Adapter - Abstraction layer for exchange-specific logic.

This module isolates CCXT quirks and exchange-specific behavior,
making the execution engine exchange-agnostic and easier to test.

Supported exchanges:
- Kraken Futures
- Binance Futures

Each adapter handles:
- Order parameter formatting
- Position queries
- Stop Loss / Take Profit placement
- Error handling and retries
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, List, Any
from datetime import datetime, UTC
import logging

logger = logging.getLogger(__name__)


@dataclass
class OrderParams:
    """Normalized order parameters"""
    symbol: str
    side: str  # 'buy' or 'sell'
    order_type: str  # 'limit', 'market', 'stop-loss', 'take-profit'
    amount: float
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reduce_only: bool = False
    post_only: bool = False
    time_in_force: str = 'GTC'


@dataclass  
class OrderResult:
    """Normalized order result"""
    success: bool
    order_id: Optional[str] = None
    status: Optional[str] = None
    filled: float = 0.0
    average_price: Optional[float] = None
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class PositionInfo:
    """Normalized position information"""
    symbol: str
    side: Optional[str]  # 'long', 'short', or None
    contracts: float
    entry_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    has_position: bool = False


class ExchangeAdapter(ABC):
    """
    Abstract base class for exchange adapters.
    
    Implement this for each exchange to isolate quirks.
    """
    
    def __init__(self, exchange_client: Any):
        """
        Initialize adapter with CCXT exchange client.
        
        Args:
            exchange_client: Initialized CCXT exchange instance
        """
        self.exchange = exchange_client
        self.name = "base"
        
    @abstractmethod
    async def place_order(self, params: OrderParams) -> OrderResult:
        """Place an order with exchange-specific formatting."""
        pass
    
    @abstractmethod
    async def place_stop_loss(self, symbol: str, side: str, 
                              amount: float, price: float) -> OrderResult:
        """Place a stop-loss order (reduce-only)."""
        pass
    
    @abstractmethod
    async def place_take_profit(self, symbol: str, side: str,
                                amount: float, price: float) -> OrderResult:
        """Place a take-profit order (reduce-only)."""
        pass
    
    @abstractmethod
    async def get_position(self, symbol: str) -> PositionInfo:
        """Get current position for symbol."""
        pass
    
    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order."""
        pass
    
    @abstractmethod
    async def get_open_orders(self, symbol: str) -> List[Dict]:
        """Get all open orders for symbol."""
        pass
    
    @abstractmethod
    def supports_atomic_sl_tp(self) -> bool:
        """Returns True if exchange supports SL/TP in entry order."""
        pass


class KrakenFuturesAdapter(ExchangeAdapter):
    """
    Adapter for Kraken Futures.
    
    Quirks handled:
    - SL/TP NOT supported in LIMIT orders (only MARKET)
    - fetch_order() not supported - use fallback
    - Position contracts may have different precision
    - reduceOnly parameter required for SL/TP
    """
    
    def __init__(self, exchange_client: Any):
        super().__init__(exchange_client)
        self.name = "kraken_futures"
        self.MIN_POSITION_SIZE = 0.0001
        
    def supports_atomic_sl_tp(self) -> bool:
        """Kraken only supports SL/TP in MARKET orders."""
        return False
    
    async def place_order(self, params: OrderParams) -> OrderResult:
        """Place order with Kraken-specific params."""
        try:
            ccxt_params = {
                'timeInForce': params.time_in_force
            }
            
            if params.reduce_only:
                ccxt_params['reduceOnly'] = True
                
            if params.post_only and params.order_type == 'limit':
                ccxt_params['postOnly'] = True
            
            order = await self.exchange.create_order(
                symbol=params.symbol,
                type=params.order_type,
                side=params.side.lower(),
                amount=params.amount,
                price=params.price,
                params=ccxt_params
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                filled=order.get('filled', 0.0),
                average_price=order.get('average'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Kraken order failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def place_stop_loss(self, symbol: str, side: str,
                              amount: float, price: float) -> OrderResult:
        """
        Place stop-loss on Kraken Futures.
        
        CRITICAL: Kraken requires:
        - type='stop-loss' 
        - price in 'stopLossPrice' or 'price' param
        - reduceOnly=True
        """
        try:
            # Side for SL is OPPOSITE of position
            sl_side = 'sell' if side == 'buy' else 'buy'
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='stop-loss',
                side=sl_side,
                amount=amount,
                price=None,  # Stop-loss uses stopPrice
                params={
                    'stopLossPrice': price,
                    'reduceOnly': True,
                    'triggerPrice': price
                }
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Kraken SL failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def place_take_profit(self, symbol: str, side: str,
                                amount: float, price: float) -> OrderResult:
        """
        Place take-profit on Kraken Futures.
        
        CRITICAL: Kraken requires:
        - type='take-profit' or limit with reduceOnly
        - price specified
        - reduceOnly=True
        """
        try:
            # Side for TP is OPPOSITE of position
            tp_side = 'sell' if side == 'buy' else 'buy'
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='take-profit',
                side=tp_side,
                amount=amount,
                price=price,
                params={
                    'reduceOnly': True,
                    'takeProfitPrice': price
                }
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Kraken TP failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def get_position(self, symbol: str) -> PositionInfo:
        """Get position with Kraken-specific handling."""
        try:
            positions = await self.exchange.fetch_positions([symbol])
            
            if not positions:
                return PositionInfo(symbol=symbol, side=None, contracts=0.0)
            
            pos = positions[0]
            contracts_raw = pos.get('contracts')
            contracts = abs(float(contracts_raw)) if contracts_raw is not None else 0.0
            
            has_position = contracts >= self.MIN_POSITION_SIZE
            
            side = None
            if has_position:
                side_raw = pos.get('side', '')
                side = 'long' if side_raw.lower() in ('long', 'buy') else 'short'
            
            return PositionInfo(
                symbol=symbol,
                side=side,
                contracts=contracts,
                entry_price=pos.get('entryPrice'),
                unrealized_pnl=pos.get('unrealizedPnl', 0.0),
                leverage=pos.get('leverage', 1.0),
                has_position=has_position
            )
            
        except Exception as e:
            logger.error(f"Kraken position fetch failed: {e}")
            return PositionInfo(symbol=symbol, side=None, contracts=0.0)
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel order on Kraken."""
        try:
            await self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"Kraken cancel failed: {e}")
            return False
    
    async def get_open_orders(self, symbol: str) -> List[Dict]:
        """Get open orders from Kraken."""
        try:
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.error(f"Kraken open orders fetch failed: {e}")
            return []


class BinanceFuturesAdapter(ExchangeAdapter):
    """
    Adapter for Binance Futures.
    
    Quirks handled:
    - Supports SL/TP in some order types
    - Different parameter names
    - Position mode (hedge vs one-way)
    """
    
    def __init__(self, exchange_client: Any):
        super().__init__(exchange_client)
        self.name = "binance_futures"
        self.MIN_POSITION_SIZE = 0.001
        
    def supports_atomic_sl_tp(self) -> bool:
        """Binance supports SL/TP with bracket orders."""
        return True
    
    async def place_order(self, params: OrderParams) -> OrderResult:
        """Place order with Binance-specific params."""
        try:
            ccxt_params = {
                'timeInForce': params.time_in_force
            }
            
            if params.reduce_only:
                ccxt_params['reduceOnly'] = True
                
            # Binance supports stopLoss/takeProfit in bracket orders
            if params.stop_loss:
                ccxt_params['stopLoss'] = {'triggerPrice': params.stop_loss}
            if params.take_profit:
                ccxt_params['takeProfit'] = {'triggerPrice': params.take_profit}
            
            order = await self.exchange.create_order(
                symbol=params.symbol,
                type=params.order_type,
                side=params.side.lower(),
                amount=params.amount,
                price=params.price,
                params=ccxt_params
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                filled=order.get('filled', 0.0),
                average_price=order.get('average'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Binance order failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def place_stop_loss(self, symbol: str, side: str,
                              amount: float, price: float) -> OrderResult:
        """Place stop-loss on Binance."""
        try:
            sl_side = 'SELL' if side.upper() == 'BUY' else 'BUY'
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='STOP_MARKET',
                side=sl_side,
                amount=amount,
                params={
                    'stopPrice': price,
                    'reduceOnly': True
                }
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Binance SL failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def place_take_profit(self, symbol: str, side: str,
                                amount: float, price: float) -> OrderResult:
        """Place take-profit on Binance."""
        try:
            tp_side = 'SELL' if side.upper() == 'BUY' else 'BUY'
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='TAKE_PROFIT_MARKET',
                side=tp_side,
                amount=amount,
                params={
                    'stopPrice': price,
                    'reduceOnly': True
                }
            )
            
            return OrderResult(
                success=True,
                order_id=order['id'],
                status=order.get('status'),
                raw_response=order
            )
            
        except Exception as e:
            logger.error(f"Binance TP failed: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def get_position(self, symbol: str) -> PositionInfo:
        """Get position from Binance."""
        try:
            positions = await self.exchange.fetch_positions([symbol])
            
            if not positions:
                return PositionInfo(symbol=symbol, side=None, contracts=0.0)
            
            pos = positions[0]
            contracts = abs(float(pos.get('contracts', 0)))
            has_position = contracts >= self.MIN_POSITION_SIZE
            
            side = None
            if has_position:
                side = 'long' if pos.get('side', '').lower() == 'long' else 'short'
            
            return PositionInfo(
                symbol=symbol,
                side=side,
                contracts=contracts,
                entry_price=pos.get('entryPrice'),
                unrealized_pnl=pos.get('unrealizedPnl', 0.0),
                leverage=pos.get('leverage', 1.0),
                has_position=has_position
            )
            
        except Exception as e:
            logger.error(f"Binance position fetch failed: {e}")
            return PositionInfo(symbol=symbol, side=None, contracts=0.0)
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel order on Binance."""
        try:
            await self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"Binance cancel failed: {e}")
            return False
    
    async def get_open_orders(self, symbol: str) -> List[Dict]:
        """Get open orders from Binance."""
        try:
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.error(f"Binance open orders fetch failed: {e}")
            return []


def get_adapter(exchange_name: str, exchange_client: Any) -> ExchangeAdapter:
    """
    Factory function to get appropriate adapter.
    
    Args:
        exchange_name: Name of exchange ('kraken', 'binance', etc.)
        exchange_client: Initialized CCXT exchange instance
        
    Returns:
        Appropriate ExchangeAdapter subclass
    """
    name_lower = exchange_name.lower()
    
    if 'kraken' in name_lower:
        return KrakenFuturesAdapter(exchange_client)
    elif 'binance' in name_lower:
        return BinanceFuturesAdapter(exchange_client)
    else:
        # Default to Kraken-like behavior
        logger.warning(f"Unknown exchange '{exchange_name}', using Kraken adapter")
        return KrakenFuturesAdapter(exchange_client)
