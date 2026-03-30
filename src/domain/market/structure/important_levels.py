"""
Important Levels Module - Anchor Levels for Psychological Support/Resistance

This module calculates and manages "anchor levels" - price closes from important
temporal milestones that act as psychological support/resistance:
- Previous year close (Y-1 close)
- Current year open (Y open)
- Previous month close (M-1 close) and current month open
- Previous week close (W-1 close) and current week open
- Previous quarter close (Q-1 close) - optional

Usage:
- Show in logs/telegram notifications
- Filter setups (block if too close to important level)
- Adjust TP/SL (TP just before level)

Integration:
- Called from signal_engine to get level context
- Updates once per day (at 00:00 UTC)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Optional, Tuple
from enum import Enum
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)


class LevelType(Enum):
    """Types of anchor levels"""
    YEARLY_CLOSE = "Y-1"        # Previous year close
    YEARLY_OPEN = "Y"           # Current year open
    QUARTERLY_CLOSE = "Q-1"     # Previous quarter close
    MONTHLY_CLOSE = "M-1"       # Previous month close
    MONTHLY_OPEN = "M"          # Current month open
    WEEKLY_CLOSE = "W-1"        # Previous week close
    WEEKLY_OPEN = "W"           # Current week open
    YTD_HIGH = "YTD-H"          # Year-to-date high
    YTD_LOW = "YTD-L"           # Year-to-date low


@dataclass
class AnchorLevel:
    """Represents a single anchor level"""
    level_type: LevelType
    price: float
    timestamp: datetime
    description: str = ""
    
    def __post_init__(self):
        if not self.description:
            self.description = f"{self.level_type.value} @ {self.price:.2f}"


@dataclass
class LevelContext:
    """Context about nearby important levels for a given price/direction"""
    # Nearest levels
    nearest_above: Optional[AnchorLevel] = None
    nearest_below: Optional[AnchorLevel] = None
    
    # Distance metrics (normalized by ATR)
    distance_above_atr: float = float('inf')
    distance_below_atr: float = float('inf')
    distance_above_bps: float = float('inf')
    distance_below_bps: float = float('inf')
    
    # For trade direction
    direction: str = ""  # 'LONG' or 'SHORT'
    
    # Blocking/confluence signals
    blocked: bool = False
    block_reason: str = ""
    confluence_bonus: float = 0.0
    confluence_reason: str = ""
    
    # Suggested TP adjustment
    suggested_tp: Optional[float] = None
    tp_adjustment_reason: str = ""
    
    # All levels for reference
    all_levels: List[AnchorLevel] = field(default_factory=list)


class ImportantLevelsProvider:
    """
    Provides anchor levels from temporal milestones.
    
    Features:
    - Downloads 1D candles via REST at startup and once per day
    - Calculates levels and stores in memory
    - Provides level context for signal evaluation
    - Caches levels to minimize API calls
    
    Data Source:
    - Uses Kraken spot (XBT/USD) for consistency with the trading pair
    - Falls back to futures data if spot unavailable
    """
    
    # Configuration
    # Reduced blocking thresholds
    # Previous values (0.3 ATR / 15 bps) were causing too many rejections
    # The system was blocking trades when price was $100 away from a level
    # For intraday, this is too restrictive - levels are context, not gospel
    BLOCKING_DISTANCE_ATR = 0.15  # 🔥 Reduced from 0.3 - only block if VERY close
    BLOCKING_DISTANCE_BPS = 8     # 🔥 Reduced from 15 - ~0.08% is very tight
    CONFLUENCE_DISTANCE_ATR = 1.5 # Add confluence bonus if within 1.5 ATR
    TP_BUFFER_ATR = 0.1           # TP buffer before level (0.1 ATR)
    
    def __init__(self, symbol: str = 'BTC/USD', exchange: str = 'kraken'):
        self.symbol = symbol
        self.exchange = exchange
        
        # Cached levels
        self._levels: Dict[LevelType, AnchorLevel] = {}
        self._last_update: Optional[datetime] = None
        self._update_interval = timedelta(hours=1)  # Check hourly, but only update on new day
        self._last_update_day: Optional[int] = None  # Track which day we last computed
        
        # API endpoints
        self._kraken_spot_url = "https://api.kraken.com/0/public/OHLC"
        self._kraken_futures_url = "https://futures.kraken.com/derivatives/api/v4/ohlcv"
        
        # Status
        self.is_initialized = False
        self._consecutive_failures = 0
        
        logger.info(f"ImportantLevelsProvider initialized for {symbol}")
    
    async def initialize(self) -> bool:
        """
        Initialize provider - fetch initial data and compute levels.
        Call this at startup.
        """
        try:
            success = await self._fetch_and_compute_levels()
            self.is_initialized = success
            if success:
                logger.info(f"✅ Important levels initialized: {len(self._levels)} levels computed")
                self._log_all_levels()
            return success
        except Exception as e:
            logger.error(f"Failed to initialize important levels: {e}")
            return False
    
    async def update_if_needed(self) -> bool:
        """
        Check if update is needed and refresh levels.
        Call this periodically (e.g., every hour).
        """
        now = datetime.now(UTC)
        
        # Check if enough time has passed since last check
        if self._last_update and (now - self._last_update) < self._update_interval:
            return True  # No update needed
        
        # Check if we've already updated today
        current_day = now.timetuple().tm_yday
        if self._last_update_day == current_day:
            self._last_update = now  # Mark checked
            return True  # Already updated today
        
        # New day - refresh levels
        logger.info("🔄 New day detected - refreshing important levels")
        success = await self._fetch_and_compute_levels()
        
        if success:
            self._last_update_day = current_day
            self._log_all_levels()
        
        return success
    
    async def _fetch_and_compute_levels(self) -> bool:
        """Fetch OHLC data and compute all anchor levels."""
        try:
            # Fetch daily candles (need ~400 for full year + quarter)
            candles = await self._fetch_daily_candles(limit=400)
            
            if not candles or len(candles) < 30:
                logger.error(f"Insufficient daily candles: {len(candles) if candles else 0}")
                return False
            
            # Compute all levels
            self._compute_levels(candles)
            self._last_update = datetime.now(UTC)
            self._consecutive_failures = 0
            
            return True
            
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"Error computing levels (attempt {self._consecutive_failures}): {e}")
            return False
    
    async def _fetch_daily_candles(self, limit: int = 400) -> List[Dict]:
        """
        Fetch daily OHLC candles from Kraken spot.
        Returns list of candles with keys: timestamp, open, high, low, close, volume
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Kraken spot API for XBT/USD
                # OHLC interval: 1440 = 1 day
                params = {
                    'pair': 'XXBTZUSD',
                    'interval': 1440  # Daily
                }
                
                async with session.get(self._kraken_spot_url, params=params, timeout=ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.warning(f"Kraken spot API returned {resp.status}, trying futures")
                        return await self._fetch_daily_candles_futures(limit)
                    
                    data = await resp.json()
                    
                    if data.get('error') and len(data['error']) > 0:
                        logger.warning(f"Kraken API error: {data['error']}, trying futures")
                        return await self._fetch_daily_candles_futures(limit)
                    
                    result = data.get('result', {})
                    # Kraken returns pair data under various keys
                    ohlc_data = result.get('XXBTZUSD') or result.get('XBTUSD') or []
                    
                    if not ohlc_data:
                        logger.warning("No OHLC data from Kraken spot, trying futures")
                        return await self._fetch_daily_candles_futures(limit)
                    
                    # Convert to standard format
                    # Kraken format: [time, open, high, low, close, vwap, volume, count]
                    candles = []
                    for row in ohlc_data[-limit:]:
                        candles.append({
                            'timestamp': datetime.fromtimestamp(int(row[0]), tz=UTC),
                            'open': float(row[1]),
                            'high': float(row[2]),
                            'low': float(row[3]),
                            'close': float(row[4]),
                            'volume': float(row[6])
                        })
                    
                    logger.info(f"Fetched {len(candles)} daily candles from Kraken spot")
                    return candles
                    
        except Exception as e:
            logger.warning(f"Error fetching from Kraken spot: {e}, trying futures")
            return await self._fetch_daily_candles_futures(limit)
    
    async def _fetch_daily_candles_futures(self, limit: int = 400) -> List[Dict]:
        """Fallback: Fetch daily candles from Kraken futures."""
        try:
            async with aiohttp.ClientSession() as session:
                # Calculate time range
                now = datetime.now(UTC)
                from_time = now - timedelta(days=limit)
                
                params = {
                    'symbol': 'PF_XBTUSD',
                    'interval': '1d',
                    'from': int(from_time.timestamp()),
                    'to': int(now.timestamp())
                }
                
                async with session.get(self._kraken_futures_url, params=params, timeout=ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.error(f"Kraken futures API returned {resp.status}")
                        return []
                    
                    data = await resp.json()
                    
                    if data.get('result') != 'success':
                        logger.error(f"Kraken futures API error: {data}")
                        return []
                    
                    ohlc_data = data.get('candles', [])
                    
                    # Convert to standard format
                    # Futures format: {time, open, high, low, close, volume}
                    candles = []
                    for row in ohlc_data[-limit:]:
                        candles.append({
                            'timestamp': datetime.fromtimestamp(row['time'] / 1000, tz=UTC),
                            'open': float(row['open']),
                            'high': float(row['high']),
                            'low': float(row['low']),
                            'close': float(row['close']),
                            'volume': float(row.get('volume', 0))
                        })
                    
                    logger.info(f"Fetched {len(candles)} daily candles from Kraken futures")
                    return candles
                    
        except Exception as e:
            logger.error(f"Error fetching from Kraken futures: {e}")
            return []
    
    def _compute_levels(self, candles: List[Dict]) -> None:
        """Compute all anchor levels from daily candles."""
        if not candles:
            return
        
        now = datetime.now(UTC)
        current_year = now.year
        current_month = now.month
        current_quarter = (current_month - 1) // 3 + 1
        
        # Sort candles by timestamp
        candles = sorted(candles, key=lambda x: x['timestamp'])
        
        # === YEARLY LEVELS ===
        prev_year_candles = [c for c in candles if c['timestamp'].year == current_year - 1]
        if prev_year_candles:
            # Last candle of previous year = Y-1 close
            last_prev_year = prev_year_candles[-1]
            self._levels[LevelType.YEARLY_CLOSE] = AnchorLevel(
                level_type=LevelType.YEARLY_CLOSE,
                price=last_prev_year['close'],
                timestamp=last_prev_year['timestamp'],
                description=f"Year {current_year - 1} Close"
            )
            
            # First candle of current year = Y open (same as Y-1 close + 1 day)
            current_year_candles = [c for c in candles if c['timestamp'].year == current_year]
            if current_year_candles:
                first_current_year = current_year_candles[0]
                self._levels[LevelType.YEARLY_OPEN] = AnchorLevel(
                    level_type=LevelType.YEARLY_OPEN,
                    price=first_current_year['open'],
                    timestamp=first_current_year['timestamp'],
                    description=f"Year {current_year} Open"
                )
        
        # === QUARTERLY LEVELS ===
        prev_quarter = current_quarter - 1 if current_quarter > 1 else 4
        prev_quarter_year = current_year if current_quarter > 1 else current_year - 1
        
        # Find last candle of previous quarter
        prev_quarter_candles = [
            c for c in candles 
            if c['timestamp'].year == prev_quarter_year 
            and (c['timestamp'].month - 1) // 3 + 1 == prev_quarter
        ]
        if prev_quarter_candles:
            last_prev_quarter = prev_quarter_candles[-1]
            self._levels[LevelType.QUARTERLY_CLOSE] = AnchorLevel(
                level_type=LevelType.QUARTERLY_CLOSE,
                price=last_prev_quarter['close'],
                timestamp=last_prev_quarter['timestamp'],
                description=f"Q{prev_quarter} {prev_quarter_year} Close"
            )
        
        # === MONTHLY LEVELS ===
        prev_month = current_month - 1 if current_month > 1 else 12
        prev_month_year = current_year if current_month > 1 else current_year - 1
        
        prev_month_candles = [
            c for c in candles 
            if c['timestamp'].year == prev_month_year 
            and c['timestamp'].month == prev_month
        ]
        if prev_month_candles:
            last_prev_month = prev_month_candles[-1]
            self._levels[LevelType.MONTHLY_CLOSE] = AnchorLevel(
                level_type=LevelType.MONTHLY_CLOSE,
                price=last_prev_month['close'],
                timestamp=last_prev_month['timestamp'],
                description=f"{prev_month_year}-{prev_month:02d} Close"
            )
        
        # Current month open
        current_month_candles = [
            c for c in candles 
            if c['timestamp'].year == current_year 
            and c['timestamp'].month == current_month
        ]
        if current_month_candles:
            first_current_month = current_month_candles[0]
            self._levels[LevelType.MONTHLY_OPEN] = AnchorLevel(
                level_type=LevelType.MONTHLY_OPEN,
                price=first_current_month['open'],
                timestamp=first_current_month['timestamp'],
                description=f"{current_year}-{current_month:02d} Open"
            )
        
        # === WEEKLY LEVELS ===
        # Get current week number (ISO week)
        current_week = now.isocalendar()[1]
        current_week_year = now.isocalendar()[0]
        
        # Find candles from previous week
        prev_week_candles = []
        for c in candles:
            iso = c['timestamp'].isocalendar()
            # Previous week
            if iso[0] == current_week_year and iso[1] == current_week - 1:
                prev_week_candles.append(c)
            elif current_week == 1:  # Handle year boundary
                if iso[0] == current_week_year - 1 and iso[1] >= 52:
                    prev_week_candles.append(c)
        
        if prev_week_candles:
            prev_week_candles = sorted(prev_week_candles, key=lambda x: x['timestamp'])
            last_prev_week = prev_week_candles[-1]
            self._levels[LevelType.WEEKLY_CLOSE] = AnchorLevel(
                level_type=LevelType.WEEKLY_CLOSE,
                price=last_prev_week['close'],
                timestamp=last_prev_week['timestamp'],
                description=f"Week {current_week - 1} Close"
            )
        
        # Current week open
        current_week_candles = []
        for c in candles:
            iso = c['timestamp'].isocalendar()
            if iso[0] == current_week_year and iso[1] == current_week:
                current_week_candles.append(c)
        
        if current_week_candles:
            current_week_candles = sorted(current_week_candles, key=lambda x: x['timestamp'])
            first_current_week = current_week_candles[0]
            self._levels[LevelType.WEEKLY_OPEN] = AnchorLevel(
                level_type=LevelType.WEEKLY_OPEN,
                price=first_current_week['open'],
                timestamp=first_current_week['timestamp'],
                description=f"Week {current_week} Open"
            )
        
        # === YTD HIGH/LOW ===
        ytd_candles = [c for c in candles if c['timestamp'].year == current_year]
        if ytd_candles:
            ytd_high = max(ytd_candles, key=lambda x: x['high'])
            ytd_low = min(ytd_candles, key=lambda x: x['low'])
            
            self._levels[LevelType.YTD_HIGH] = AnchorLevel(
                level_type=LevelType.YTD_HIGH,
                price=ytd_high['high'],
                timestamp=ytd_high['timestamp'],
                description=f"YTD High"
            )
            
            self._levels[LevelType.YTD_LOW] = AnchorLevel(
                level_type=LevelType.YTD_LOW,
                price=ytd_low['low'],
                timestamp=ytd_low['timestamp'],
                description=f"YTD Low"
            )
    
    def _log_all_levels(self) -> None:
        """Log all computed levels for visibility."""
        if not self._levels:
            logger.warning("No important levels computed")
            return
        
        # Compact single-line format instead of multi-line
        sorted_levels = sorted(self._levels.values(), key=lambda x: x.price, reverse=True)
        level_strs = [f"{level.level_type.value}=${level.price:,.0f}" for level in sorted_levels[:5]]
        logger.info(f"📊 ANCHOR LEVELS (top 5): {' | '.join(level_strs)}")
    
    def get_all_levels(self) -> List[AnchorLevel]:
        """Get all computed levels as a list."""
        return list(self._levels.values())
    
    def get_status(self) -> Dict:
        """Get status for monitoring/debugging."""
        return {
            'initialized': self.is_initialized,
            'levels_count': len(self._levels),
            'last_update': self._last_update.isoformat() if self._last_update else None,
            'consecutive_failures': self._consecutive_failures,
            'levels': {
                lt.value: {
                    'price': al.price,
                    'timestamp': al.timestamp.isoformat(),
                    'description': al.description
                }
                for lt, al in self._levels.items()
            }
        }


# Singleton instance for global access
_important_levels_instance: Optional[ImportantLevelsProvider] = None


def get_important_levels_provider(symbol: str = 'BTC/USD') -> ImportantLevelsProvider:
    """Get or create the singleton ImportantLevelsProvider instance."""
    global _important_levels_instance
    if _important_levels_instance is None:
        _important_levels_instance = ImportantLevelsProvider(symbol=symbol)
    return _important_levels_instance


async def initialize_important_levels(symbol: str = 'BTC/USD') -> bool:
    """Initialize the important levels provider (call at startup)."""
    provider = get_important_levels_provider(symbol)
    return await provider.initialize()
