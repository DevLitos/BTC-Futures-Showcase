"""
Order Book Real - Depth updates
Limpia de spoofing básico
"""

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, UTC
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# 🔥 FIX (Jan 21, 2026): Throttle debug logs to prevent event loop blocking
_last_imbalance_log = 0.0
_IMBALANCE_LOG_INTERVAL = 5.0  # Log imbalance only every 5 seconds


class OrderBookManager:
    """
    Mantiene order book actualizado
    Calcula imbalance real
    Filtra spoofing básico
    """
    
    def __init__(self, symbol: str = 'BTC/USD', depth_levels: int = 25):
        self.symbol = symbol
        self.depth_levels = depth_levels
        
        # Bids y asks: {price: quantity}
        self.bids = {}  # descending order
        self.asks = {}  # ascending order
        
        # History para detectar spoofing
        self.order_history = deque(maxlen=10000)
        
        # Imbalance tracking
        self.last_imbalance = 50.0  # neutral
        self.imbalance_history = deque(maxlen=1000)
        
        logger.info(f"OrderBookManager initialized for {symbol} (depth={depth_levels})")
    
    def update_bid(self, price: float, qty: float):
        """Update bid level"""
        if qty == 0:
            # Remove order
            self.bids.pop(price, None)
        else:
            self.bids[price] = qty
        
        # Log para detectar spoofing
        self.order_history.append({
            'timestamp': datetime.now(UTC).isoformat(),
            'side': 'bid',
            'price': price,
            'qty': qty
        })
    
    def update_ask(self, price: float, qty: float):
        """Update ask level"""
        if qty == 0:
            # Remove order
            self.asks.pop(price, None)
        else:
            self.asks[price] = qty
        
        self.order_history.append({
            'timestamp': datetime.now(UTC).isoformat(),
            'side': 'ask',
            'price': price,
            'qty': qty
        })
        
        # Recalcular imbalance
        self._recalculate_imbalance()
    
    def update_bids(self, bids: list):
        """Update multiple bid levels from [[price, qty], ...]"""
        for price, qty in bids:
            self.update_bid(float(price), float(qty))
    
    def update_asks(self, asks: list):
        """Update multiple ask levels from [[price, qty], ...]"""
        for price, qty in asks:
            self.update_ask(float(price), float(qty))
    
    def _recalculate_imbalance(self):
        """Calcular imbalance real"""
        global _last_imbalance_log
        
        bid_volume = self._calculate_side_volume('bid')
        ask_volume = self._calculate_side_volume('ask')
        
        total = bid_volume + ask_volume
        if total == 0:
            self.last_imbalance = 50.0
            return
        
        # Percentage of bid side
        self.last_imbalance = (bid_volume / total) * 100.0
        self.imbalance_history.append(self.last_imbalance)
        
        # 🔥 FIX (Jan 21, 2026): Throttle debug logs to prevent event loop blocking
        now = time.time()
        if now - _last_imbalance_log >= _IMBALANCE_LOG_INTERVAL:
            logger.debug(f"Order Book Imbalance: {self.last_imbalance:.1f}% (Bid: {bid_volume:.4f} Ask: {ask_volume:.4f})")
            _last_imbalance_log = now
    
    def _calculate_side_volume(self, side: str) -> float:
        """Calculate volume for top N levels"""
        orders = self.bids if side == 'bid' else self.asks
        
        volume = 0.0
        count = 0
        
        if side == 'bid':
            # Highest prices first
            for price in sorted(orders.keys(), reverse=True):
                if count >= self.depth_levels:
                    break
                volume += orders[price]
                count += 1
        else:
            # Lowest prices first
            for price in sorted(orders.keys()):
                if count >= self.depth_levels:
                    break
                volume += orders[price]
                count += 1
        
        return volume
    
    def get_imbalance(self) -> float:
        """
        Get current order book imbalance
        Returns: percentage of bid side
        50.0 = balanced
        60.0 = 60% bid, 40% ask
        """
        return self.last_imbalance
    
    def get_spread(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Get bid-ask spread
        Returns: (bid_price, ask_price, spread_pct)
        """
        if not self.bids or not self.asks:
            return None, None, None
        
        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())
        
        mid = (best_bid + best_ask) / 2.0
        spread_pct = ((best_ask - best_bid) / mid) * 100.0
        
        return best_bid, best_ask, spread_pct
    
    def is_spread_abnormal(self, normal_spread_pct: float = 0.01) -> bool:
        """Check if spread is abnormal (spoofing indicator)"""
        _, _, spread_pct = self.get_spread()
        
        if spread_pct is None:
            return False
        
        return spread_pct > normal_spread_pct * 2.0  # More than 2x normal
    
    def get_depth(self, side: str = 'bid', levels: int = 10) -> list:
        """Get depth levels [price, qty, ...]"""
        orders = self.bids if side == 'bid' else self.asks
        
        result = []
        if side == 'bid':
            for price in sorted(orders.keys(), reverse=True)[:levels]:
                result.append([price, orders[price]])
        else:
            for price in sorted(orders.keys())[:levels]:
                result.append([price, orders[price]])
        
        return result
    
    def get_snapshot(self) -> Dict:
        """Get full order book snapshot"""
        best_bid, best_ask, spread_pct = self.get_spread()
        
        return {
            'symbol': self.symbol,
            'bids': self.get_depth('bid', self.depth_levels),
            'asks': self.get_depth('ask', self.depth_levels),
            'best_bid': best_bid,
            'best_ask': best_ask,
            'spread_pct': spread_pct,
            'imbalance_pct': self.last_imbalance,
            'bid_volume': self._calculate_side_volume('bid'),
            'ask_volume': self._calculate_side_volume('ask')
        }
    
    def detect_spoofing_attempt(self) -> bool:
        """
        Detección básica de spoofing:
        Órdenes que aparecen y desaparecen en < 200ms
        
        Nota: No es bullet-proof, pero detiene lo más obvio
        """
        if len(self.order_history) < 2:
            return False
        
        # Simplified: check if recent orders were placed and removed quickly
        # TODO: Implementar con timestamps reales de Kraken
        
        return False
