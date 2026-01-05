"""
External Data: Funding Rate + Macro Events
REST polling (no WebSocket necesario)
"""

import asyncio
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta, UTC
import aiohttp

logger = logging.getLogger(__name__)


class FundingRateMonitor:
    """
    Monitor funding rate vía REST
    Poll cada 5 minutos (suficiente)
    """
    
    def __init__(self, symbol: str = 'BTC/USD', exchange: str = 'kraken'):
        self.symbol = symbol
        self.exchange = exchange
        self.current_rate = 0.0
        self.last_check = None
        self.check_interval = 300  # 5 min
        self.enabled = True
        self.is_running = False  # Control flag para evitar bucles infinitos
        
        self.max_funding = 0.08 / 100  # ±0.08% = 0.0008
        
        # Check if exchange is supported
        SUPPORTED_EXCHANGES = ['kraken', 'binance', 'krakenfutures']
        if exchange not in SUPPORTED_EXCHANGES:
            logger.warning(f"Unsupported exchange: {exchange}. External data disabled.")
            self.enabled = False
        
        logger.info(f"FundingRateMonitor initialized for {symbol}")
    
    async def start(self):
        """Start polling funding rate"""
        if not self.enabled:
            logger.info("Funding rate monitor disabled")
            return
            
        self.is_running = True
        while self.is_running:
            try:
                await self.poll_funding_rate()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error polling funding: {e}")
                await asyncio.sleep(60)  # Retry en 1 min
    
    async def stop(self):
        """Stop funding monitor"""
        logger.info("Stopping funding rate monitor...")
        self.is_running = False
    
    async def poll_funding_rate(self) -> float:
        """Obtener funding rate del exchange"""
        try:
            if self.exchange in ['kraken', 'krakenfutures']:
                return await self._get_kraken_funding()
            elif self.exchange == 'binance':
                return await self._get_binance_funding()
            else:
                logger.warning(f"Unsupported exchange: {self.exchange}")
                return self.current_rate
        
        except Exception as e:
            logger.error(f"Error fetching funding rate: {e}")
            return self.current_rate
    
    async def _get_kraken_funding(self) -> float:
        """Kraken funding rate"""
        try:
            # Kraken Futures API
            symbol_kraken = self.symbol.replace('/', '')  # BTC/USD -> BTCUSD
            
            async with aiohttp.ClientSession() as session:
                url = f"https://futures.kraken.com/derivatives/api/v3/fundingRates?symbol={symbol_kraken}&limit=1"
                
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rates = data.get('rates', [])
                        
                        if rates:
                            rate = float(rates[0]['fundingRate']) / 100  # Convert to decimal
                            self.current_rate = rate
                            self.last_check = datetime.now()
                            
                            logger.info(f"Funding rate: {rate*100:.4f}%")
                            return rate
        
        except Exception as e:
            logger.error(f"Error getting Kraken funding: {e}")
        
        return self.current_rate
    
    async def _get_binance_funding(self) -> float:
        """Binance funding rate"""
        try:
            async with aiohttp.ClientSession() as session:
                symbol_binance = self.symbol.replace('/', '').upper()
                url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol_binance}USDT&limit=1"
                
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        if data:
                            rate = float(data[0]['fundingRate'])
                            self.current_rate = rate
                            self.last_check = datetime.now()
                            
                            logger.info(f"Funding rate: {rate*100:.4f}%")
                            return rate
        
        except Exception as e:
            logger.error(f"Error getting Binance funding: {e}")
        
        return self.current_rate
    
    def is_funding_safe(self) -> bool:
        """Check if funding rate is safe to trade"""
        if abs(self.current_rate) > self.max_funding:
            logger.warning(f"Funding rate {self.current_rate*100:.4f}% exceeds threshold {self.max_funding*100:.4f}%")
            return False
        
        return True
    
    def get_status(self) -> Dict:
        """Status actual"""
        return {
            'current_rate': self.current_rate,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'is_safe': self.is_funding_safe(),
            'max_threshold': self.max_funding
        }


class MacroEventMonitor:
    """
    Detector de eventos macro
    Simple: API de calendario + timestamps
    """
    
    def __init__(self):
        self.events = []
        self.quiet_zone_minutes = 30  # 30min antes/después
        self.last_update = None
        self.is_running = False  # Control flag para evitar bucles infinitos
        
        logger.info("MacroEventMonitor initialized")
    
    async def start(self):
        """Start monitoring macro events"""
        self.is_running = True
        while self.is_running:
            try:
                await self.fetch_events()
                await asyncio.sleep(3600)  # Actualizar cada hora
            except Exception as e:
                logger.error(f"Error fetching macro events: {e}")
                await asyncio.sleep(600)
    
    async def stop(self):
        """Stop macro event monitor"""
        logger.info("Stopping macro event monitor...")
        self.is_running = False
    
    async def fetch_events(self):
        """Obtener eventos económicos"""
        try:
            # Usando API gratuita: economictimezone.com o similar
            # Simplificado: lista de eventos clave
            
            self.events = [
                {
                    'name': 'FOMC Decision',
                    'timestamp': self._next_occurrence('FOMC'),
                    'importance': 'HIGH',
                    'impact': 'Very High'
                },
                {
                    'name': 'CPI Release',
                    'timestamp': self._next_occurrence('CPI'),
                    'importance': 'HIGH',
                    'impact': 'Very High'
                },
                {
                    'name': 'NFP Release',
                    'timestamp': self._next_occurrence('NFP'),
                    'importance': 'HIGH',
                    'impact': 'Very High'
                }
            ]
            
            self.last_update = datetime.now()
            logger.info(f"Updated {len(self.events)} macro events")
            
        except Exception as e:
            logger.error(f"Error fetching events: {e}")
    
    def _next_occurrence(self, event_type: str) -> Optional[datetime]:
        """Calcular próxima ocurrencia (stub)"""
        # En producción: usar API de calendario
        now = datetime.now()
        
        if event_type == 'FOMC':
            # FOMC ocurre 8 veces al año (aproximadamente cada 6 semanas)
            # Simplificado
            return now + timedelta(days=42)
        elif event_type == 'CPI':
            # CPI el segundo miércoles de cada mes
            return now + timedelta(days=7)
        elif event_type == 'NFP':
            # NFP el primer viernes de cada mes
            return now + timedelta(days=3)
        
        return None
    
    def is_in_quiet_zone(self) -> bool:
        """Check if now is within quiet zone of any event"""
        now = datetime.now()
        quiet_delta = timedelta(minutes=self.quiet_zone_minutes)
        
        for event in self.events:
            if event['timestamp'] is None:
                continue
            
            event_start = event['timestamp'] - quiet_delta
            event_end = event['timestamp'] + quiet_delta
            
            if event_start <= now <= event_end:
                logger.warning(f"In quiet zone for {event['name']}")
                return True
        
        return False
    
    def get_upcoming_events(self, hours: int = 24) -> List[Dict]:
        """Obtener eventos próximos"""
        now = datetime.now()
        future = now + timedelta(hours=hours)
        
        upcoming = [
            e for e in self.events
            if e['timestamp'] and now <= e['timestamp'] <= future
        ]
        
        return sorted(upcoming, key=lambda e: e['timestamp'])
    
    def get_status(self) -> Dict:
        """Status actual"""
        return {
            'in_quiet_zone': self.is_in_quiet_zone(),
            'upcoming_events': self.get_upcoming_events(24),
            'last_update': self.last_update.isoformat() if self.last_update else None
        }


class ExternalDataManager:
    """
    Orquesta funding + macro
    """
    
    def __init__(self, symbol: str = 'BTC/USD', exchange: str = 'kraken'):
        self.funding = FundingRateMonitor(symbol, exchange)
        self.macro = MacroEventMonitor()
    
    async def start(self):
        """Start all monitors"""
        await asyncio.gather(
            self.funding.start(),
            self.macro.start(),
            return_exceptions=True
        )
    
    async def stop(self):
        """Stop all monitors"""
        logger.info("Stopping external data monitors...")
        await self.funding.stop()
        await self.macro.stop()
    
    def can_trade(self) -> Tuple[bool, str]:
        """
        Check if external conditions allow trading
        
        Returns: (can_trade: bool, reason: str)
        """
        # Check funding
        if not self.funding.is_funding_safe():
            return False, f"Funding rate excessive: {self.funding.current_rate*100:.4f}%"
        
        # Check macro
        if self.macro.is_in_quiet_zone():
            return False, "In macro event quiet zone (±30min)"
        
        return True, "External data OK"
    
    def get_status(self) -> Dict:
        """Get full status"""
        return {
            'funding': self.funding.get_status(),
            'macro': self.macro.get_status(),
            'can_trade': self.can_trade()[0]
        }