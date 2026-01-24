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
        
        self.max_funding = 0.10 / 100  # ±0.10% = 0.001 (slightly relaxed from 0.08%)
        
        # Track last funding bucket to avoid log spam
        self._last_logged_bucket = None
        
        # 🔒 FIX (Jan 13, 2026): Reusable HTTP session to prevent resource leaks
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Check if exchange is supported
        SUPPORTED_EXCHANGES = ['kraken', 'binance', 'krakenfutures']
        if exchange not in SUPPORTED_EXCHANGES:
            logger.warning(f"Unsupported exchange: {exchange}. External data disabled.")
            self.enabled = False
        
        logger.info(f"FundingRateMonitor initialized for {symbol}")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create reusable HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def _close_session(self):
        """Close HTTP session if open."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def start(self):
        """Start polling funding rate"""
        if not self.enabled:
            logger.info("Funding rate monitor disabled")
            return
            
        self.is_running = True
        try:
            while self.is_running:
                try:
                    await self.poll_funding_rate()
                    await asyncio.sleep(self.check_interval)
                except Exception as e:
                    logger.error(f"Error polling funding: {e}")
                    await asyncio.sleep(60)  # Retry en 1 min
        finally:
            # 🔒 Always close session when stopping
            await self._close_session()
    
    async def stop(self):
        """Stop funding monitor"""
        logger.info("Stopping funding rate monitor...")
        self.is_running = False
        await self._close_session()
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
            # Kraken Futures API - uses PF_XBTUSD format for BTC perpetual
            # Map common symbol formats to Kraken Futures format
            symbol_map = {
                'BTC/USD': 'PF_XBTUSD',
                'BTCUSD': 'PF_XBTUSD',
                'BTC/USD:USD': 'PF_XBTUSD',  # Added full perpetual symbol
                'BTCUSD:USD': 'PF_XBTUSD',   # Added variant
                'BTC/USDT': 'PF_XBTUSD',
                'ETH/USD': 'PF_ETHUSD',
                'ETHUSD': 'PF_ETHUSD',
                'ETH/USD:USD': 'PF_ETHUSD',
            }
            # Clean the symbol before mapping
            clean_symbol = self.symbol.replace('/', '').upper().replace('USDT', 'USD')
            symbol_kraken = symbol_map.get(self.symbol, symbol_map.get(clean_symbol, 'PF_XBTUSD'))
            
            # 🔒 FIX: Use reusable session instead of creating new one each time
            session = await self._get_session()
            url = f"https://futures.kraken.com/derivatives/api/v3/tickers"
            
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tickers = data.get('tickers', [])
                    
                    # Find the matching ticker
                    for ticker in tickers:
                        if ticker.get('symbol') == symbol_kraken:
                            raw_rate = float(ticker.get('fundingRate', 0))
                            
                            # KRAKEN FUNDING RATE FIX:
                            # Kraken returns funding rate as annualized rate (e.g., 0.88 = 88% annual)
                            # Convert to 8-hour rate for comparison with other exchanges
                            # Annual → 8h: divide by (365 * 3) = 1095 funding periods per year
                            # Or check if rate is already in correct format (very small number)
                            if abs(raw_rate) > 0.01:  # Likely annualized format
                                # Convert annual to 8h rate: rate / 1095
                                rate = raw_rate / 1095.0
                                logger.debug(f"Kraken funding rate converted: {raw_rate:.4f} annual → {rate:.6f} (8h)")
                            else:
                                rate = raw_rate
                            
                            self.current_rate = rate
                            self.last_check = datetime.now()
                            
                            # Only log at INFO if funding rate is significant
                            if abs(rate) > 0.001:  # > 0.1%
                                logger.info(f"Funding rate ({symbol_kraken}): {rate*100:.4f}% (8h)")
                            else:
                                logger.debug(f"Funding rate ({symbol_kraken}): {rate*100:.4f}% (8h)")
                            return rate
                    
                    logger.warning(f"Symbol {symbol_kraken} not found in Kraken tickers")
        
        except Exception as e:
            logger.error(f"Error getting Kraken funding: {e}")
        
        return self.current_rate
    
    async def _get_binance_funding(self) -> float:
        """Binance funding rate"""
        try:
            # 🔒 FIX: Use reusable session instead of creating new one each time
            session = await self._get_session()
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
        """
        DEPRECATED: Funding rate should NOT be used as a binary gate.
        Use get_funding_bucket() for classification and get_funding_sizing_factor() for sizing.
        
        This method is kept for backward compatibility but ALWAYS returns True.
        """
        # Log elevated funding only when bucket changes (avoid log spam)
        current_bucket = self.get_funding_bucket()
        if current_bucket != 'normal' and current_bucket != self._last_logged_bucket:
            logger.info(f"📊 Funding elevated: {self.current_rate*100:.4f}% (sizing will be adjusted)")
            self._last_logged_bucket = current_bucket
        elif current_bucket == 'normal' and self._last_logged_bucket and self._last_logged_bucket != 'normal':
            logger.info(f"📊 Funding normalized: {self.current_rate*100:.4f}%")
            self._last_logged_bucket = current_bucket
        
        return True  # Never block on funding
    
    def get_funding_bucket(self) -> str:
        """
        Categorize funding rate into buckets for analysis.
        Funding NO LONGER BLOCKS - it's a tag for edge discovery.
        
        Returns:
            'normal': 0 - 0.15%
            'elevated': 0.15 - 0.30%
            'high': 0.30 - 0.60%
            'critical': 0.60 - 1.00%
            'extreme': > 1.00%
        """
        rate_pct = abs(self.current_rate) * 100  # Convert to percentage
        
        if rate_pct <= 0.15:
            return 'normal'
        elif rate_pct <= 0.30:
            return 'elevated'
        elif rate_pct <= 0.60:
            return 'high'
        elif rate_pct <= 1.00:
            return 'critical'
        else:
            return 'extreme'
    
    def get_funding_sizing_factor(self) -> float:
        """
        Get position sizing factor based on funding rate.
        Used in PRODUCTION mode only - DISCOVERY uses fixed sizing.
        
        Funding 8h       Factor
        0 - 0.15%        1.00 (normal)
        0.15 - 0.30%     0.85
        0.30 - 0.60%     0.65
        0.60 - 1.00%     0.50
        > 1.00%          0.35 (minimal size, not blocked)
        
        Returns:
            float: Sizing multiplier (0.35 to 1.0)
        """
        rate_pct = abs(self.current_rate) * 100
        
        if rate_pct > 1.00:
            return 0.35
        elif rate_pct > 0.60:
            return 0.50
        elif rate_pct > 0.30:
            return 0.65
        elif rate_pct > 0.15:
            return 0.85
        return 1.0
    
    def get_funding_score_penalty(self, bias: str | None = None) -> float:
        """
        Get score adjustment for funding rate.
        DIRECTIONAL: funding affects longs and shorts differently.
        
        Positive funding (longs pay, shorts receive):
          - LONG: penalty
          - SHORT: bonus (collecting funding)
        
        Negative funding (shorts pay, longs receive):
          - SHORT: penalty  
          - LONG: bonus
        
        Args:
            bias: 'LONG' or 'SHORT' - if None, returns generic penalty
            
        Returns:
            float: Score adjustment (-0.5 to +0.5)
        """
        rate = self.current_rate  # Can be positive or negative
        rate_pct = abs(rate) * 100
        
        # Base penalty based on magnitude (regardless of direction)
        if rate_pct <= 0.05:
            base_score = 0.0  # Normal funding, no adjustment
        elif rate_pct <= 0.10:
            base_score = -0.25  # Slight penalty
        elif rate_pct <= 0.15:
            base_score = -0.50  # Moderate penalty
        else:
            base_score = -0.75  # High penalty
        
        # Directional adjustment: if funding FAVORS our direction, reduce or reverse penalty
        if bias is not None:
            is_paying = (
                (rate > 0.0001 and bias == 'LONG') or   # Longs pay positive funding
                (rate < -0.0001 and bias == 'SHORT')    # Shorts pay negative funding
            )
            is_receiving = (
                (rate > 0.0001 and bias == 'SHORT') or  # Shorts receive positive funding
                (rate < -0.0001 and bias == 'LONG')     # Longs receive negative funding
            )
            
            if is_receiving and rate_pct > 0.05:
                # Receiving funding = bonus (counteracts base penalty)
                return min(0.5, base_score + 0.75)  # Can go positive
            elif is_paying:
                # Paying = full penalty applies
                return base_score
        
        return base_score
    
    def get_next_funding_time(self) -> datetime:
        """
        Get the next funding timestamp.
        
        Kraken Futures funding occurs every 8 hours at:
        - 00:00 UTC
        - 08:00 UTC
        - 16:00 UTC
        
        Returns:
            datetime: Next funding time in UTC
        """
        now = datetime.now(UTC)
        
        # Funding times are at 0, 8, 16 hours UTC
        funding_hours = [0, 8, 16]
        
        current_hour = now.hour
        current_date = now.date()
        
        # Find next funding hour
        for fh in funding_hours:
            if current_hour < fh:
                # Next funding is today at this hour
                return datetime(
                    current_date.year, current_date.month, current_date.day,
                    fh, 0, 0, tzinfo=UTC
                )
        
        # All funding hours today have passed - next is tomorrow at 00:00 UTC
        next_day = current_date + timedelta(days=1)
        return datetime(
            next_day.year, next_day.month, next_day.day,
            0, 0, 0, tzinfo=UTC
        )
    
    def get_time_to_next_funding_minutes(self) -> float:
        """
        Get minutes until next funding event.
        
        Returns:
            float: Minutes until next 8-hour funding settlement
        """
        now = datetime.now(UTC)
        next_funding = self.get_next_funding_time()
        delta = next_funding - now
        return max(0.0, delta.total_seconds() / 60.0)
    
    def get_funding_info(self, bias: str | None = None) -> Dict:
        """
        Get complete funding information for tags and analysis.
        
        Args:
            bias: 'LONG' or 'SHORT' for directional score calculation
        """
        time_to_funding = self.get_time_to_next_funding_minutes()
        return {
            'rate': self.current_rate,
            'rate_pct': abs(self.current_rate) * 100,
            'bucket': self.get_funding_bucket(),
            'sizing_factor': self.get_funding_sizing_factor(),
            'score_penalty': self.get_funding_score_penalty(bias),
            'direction': 'positive' if self.current_rate > 0 else 'negative',
            'time_to_next_funding_min': time_to_funding,
            'next_funding_time': self.get_next_funding_time().isoformat()
        }
    
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
        Check if external conditions allow trading.
        
        CRITICAL CHANGE (Jan 9, 2026):
        Funding rate is NO LONGER a hard gate.
        Funding affects SIZING and SCORE, but NEVER blocks entry.
        This is professional trading - costs are managed, not avoided.
        
        Returns: (can_trade: bool, reason: str)
        """
        # FUNDING: No longer blocks - it's a cost, not an edge detector
        # Sizing and score penalties are applied in signal_engine
        funding_info = self.funding.get_funding_info()
        funding_note = ""
        if funding_info['bucket'] in ['high', 'critical', 'extreme']:
            funding_note = f" | Funding {funding_info['bucket']}: {funding_info['rate_pct']:.4f}%"
        
        # Check macro - this CAN block (major events cause real edge loss)
        if self.macro.is_in_quiet_zone():
            return False, "In macro event quiet zone (±30min)"
        
        return True, f"External data OK{funding_note}"
    
    async def get_funding_rate(self, symbol: str | None = None) -> float:
        """Get current funding rate (for backward compatibility)"""
        return self.funding.current_rate
    
    def get_status(self) -> Dict:
        """Get full status"""
        return {
            'funding': self.funding.get_status(),
            'macro': self.macro.get_status(),
            'can_trade': self.can_trade()[0]
        }