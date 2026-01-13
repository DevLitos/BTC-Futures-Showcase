"""
Tests for Important Levels (Anchor Levels) Module

Tests the ImportantLevelsProvider and its integration with signal_engine.
"""

import pytest
import asyncio
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from engine.important_levels import (
    ImportantLevelsProvider,
    AnchorLevel,
    LevelContext,
    LevelType,
    get_important_levels_provider,
    initialize_important_levels
)


class TestAnchorLevel:
    """Test AnchorLevel dataclass"""
    
    def test_anchor_level_creation(self):
        """Test creating an anchor level"""
        level = AnchorLevel(
            level_type=LevelType.YEARLY_CLOSE,
            price=42000.0,
            timestamp=datetime(2024, 12, 31, tzinfo=UTC),
            description="Year 2024 Close"
        )
        
        assert level.level_type == LevelType.YEARLY_CLOSE
        assert level.price == 42000.0
        assert "Year 2024 Close" in level.description
    
    def test_anchor_level_auto_description(self):
        """Test auto-generated description"""
        level = AnchorLevel(
            level_type=LevelType.MONTHLY_CLOSE,
            price=95000.0,
            timestamp=datetime.now(UTC)
        )
        
        assert "M-1" in level.description
        assert "95000" in level.description


class TestLevelContext:
    """Test LevelContext dataclass"""
    
    def test_level_context_defaults(self):
        """Test default values"""
        context = LevelContext()
        
        assert context.blocked is False
        assert context.confluence_bonus == 0.0
        assert context.suggested_tp is None
        assert context.nearest_above is None
        assert context.nearest_below is None
    
    def test_level_context_with_levels(self):
        """Test context with levels"""
        above = AnchorLevel(LevelType.WEEKLY_CLOSE, 100000.0, datetime.now(UTC))
        below = AnchorLevel(LevelType.MONTHLY_CLOSE, 90000.0, datetime.now(UTC))
        
        context = LevelContext(
            nearest_above=above,
            nearest_below=below,
            distance_above_atr=1.5,
            distance_below_atr=2.0,
            direction='LONG'
        )
        
        assert context.nearest_above.price == 100000.0
        assert context.nearest_below.price == 90000.0
        assert context.distance_above_atr == 1.5


class TestImportantLevelsProvider:
    """Test ImportantLevelsProvider class"""
    
    @pytest.fixture
    def provider(self):
        """Create a provider instance"""
        return ImportantLevelsProvider(symbol='BTC/USD')
    
    def test_provider_initialization(self, provider):
        """Test provider initializes correctly"""
        assert provider.symbol == 'BTC/USD'
        assert provider.is_initialized is False
        assert len(provider._levels) == 0
    
    def test_compute_levels_with_mock_data(self, provider):
        """Test level computation with mock daily candles"""
        now = datetime.now(UTC)
        current_year = now.year
        
        # Create mock candles spanning current and previous year
        mock_candles = []
        
        # Previous year candles (last 10 days of December)
        for i in range(10):
            day = 22 + i
            mock_candles.append({
                'timestamp': datetime(current_year - 1, 12, day, tzinfo=UTC),
                'open': 40000 + i * 100,
                'high': 40500 + i * 100,
                'low': 39500 + i * 100,
                'close': 40200 + i * 100,
                'volume': 1000000
            })
        
        # Current year candles (January)
        for i in range(30):
            day = 1 + i
            mock_candles.append({
                'timestamp': datetime(current_year, 1, min(day, 28), tzinfo=UTC),
                'open': 41000 + i * 50,
                'high': 41500 + i * 50,
                'low': 40500 + i * 50,
                'close': 41200 + i * 50,
                'volume': 1000000
            })
        
        # Compute levels
        provider._compute_levels(mock_candles)
        
        # Should have computed at least yearly close
        assert LevelType.YEARLY_CLOSE in provider._levels
        assert provider._levels[LevelType.YEARLY_CLOSE].price > 0
    
    def test_get_level_context_blocking(self, provider):
        """Test that levels within blocking distance trigger block"""
        # Manually set a level
        provider._levels[LevelType.WEEKLY_CLOSE] = AnchorLevel(
            level_type=LevelType.WEEKLY_CLOSE,
            price=100050.0,  # Very close above
            timestamp=datetime.now(UTC)
        )
        
        current_price = 100000.0
        atr_5m = 200.0  # Distance is 50 / 200 = 0.25 ATR (less than 0.3 threshold)
        
        context = provider.get_level_context(
            current_price=current_price,
            direction='LONG',
            atr_5m=atr_5m
        )
        
        assert context.blocked is True
        assert 'too close' in context.block_reason.lower()
    
    def test_get_level_context_confluence(self, provider):
        """Test that levels within confluence distance add bonus"""
        # Set a support level below
        provider._levels[LevelType.MONTHLY_CLOSE] = AnchorLevel(
            level_type=LevelType.MONTHLY_CLOSE,
            price=99500.0,  # 500 below
            timestamp=datetime.now(UTC)
        )
        
        current_price = 100000.0
        atr_5m = 400.0  # Distance is 500 / 400 = 1.25 ATR (within 1.5 threshold)
        
        context = provider.get_level_context(
            current_price=current_price,
            direction='LONG',  # Near support is good for longs
            atr_5m=atr_5m
        )
        
        assert context.blocked is False
        assert context.confluence_bonus > 0
        assert 'support' in context.confluence_reason.lower()
    
    def test_get_level_context_tp_adjustment(self, provider):
        """Test TP adjustment when level is between entry and TP"""
        # Set a resistance level
        provider._levels[LevelType.YEARLY_CLOSE] = AnchorLevel(
            level_type=LevelType.YEARLY_CLOSE,
            price=101000.0,  # Between entry and TP
            timestamp=datetime.now(UTC)
        )
        
        current_price = 100000.0
        planned_tp = 102000.0  # TP is beyond the level
        atr_5m = 400.0
        
        context = provider.get_level_context(
            current_price=current_price,
            direction='LONG',
            atr_5m=atr_5m,
            planned_tp=planned_tp
        )
        
        # Should suggest adjusting TP before the level
        assert context.suggested_tp is not None
        assert context.suggested_tp < 101000.0  # Before resistance
        assert context.suggested_tp > current_price  # Still profitable
        assert 'adjusted' in context.tp_adjustment_reason.lower()
    
    def test_get_level_context_short_direction(self, provider):
        """Test level context for SHORT trades"""
        # Set a support level
        provider._levels[LevelType.WEEKLY_OPEN] = AnchorLevel(
            level_type=LevelType.WEEKLY_OPEN,
            price=99800.0,  # 200 below
            timestamp=datetime.now(UTC)
        )
        
        current_price = 100000.0
        atr_5m = 400.0
        
        # For SHORT, support below should trigger blocking if too close
        provider._levels[LevelType.WEEKLY_OPEN].price = 99950.0  # Very close
        
        context = provider.get_level_context(
            current_price=current_price,
            direction='SHORT',
            atr_5m=atr_5m
        )
        
        assert context.blocked is True
        assert 'support' in context.block_reason.lower()
    
    def test_format_levels_for_notification(self, provider):
        """Test notification formatting"""
        # Add some levels
        provider._levels = {
            LevelType.YEARLY_CLOSE: AnchorLevel(
                LevelType.YEARLY_CLOSE, 95000.0, datetime.now(UTC), "Year 2024 Close"
            ),
            LevelType.MONTHLY_CLOSE: AnchorLevel(
                LevelType.MONTHLY_CLOSE, 98000.0, datetime.now(UTC), "December Close"
            ),
            LevelType.WEEKLY_CLOSE: AnchorLevel(
                LevelType.WEEKLY_CLOSE, 100500.0, datetime.now(UTC), "Week 2 Close"
            ),
        }
        
        current_price = 99000.0
        formatted = provider.format_levels_for_notification(current_price)
        
        assert "Important Levels" in formatted
        assert "95,000" in formatted or "95000" in formatted
        assert "Y-1" in formatted or "Weekly" in formatted.lower() or "Monthly" in formatted.lower()


class TestImportantLevelsIntegration:
    """Integration tests with SignalEngine"""
    
    def test_singleton_provider(self):
        """Test that get_important_levels_provider returns singleton"""
        # Reset singleton for clean test
        import engine.important_levels as il
        il._important_levels_instance = None
        
        provider1 = get_important_levels_provider('BTC/USD')
        provider2 = get_important_levels_provider('BTC/USD')
        
        assert provider1 is provider2
    
    def test_initialize_with_mock_api(self):
        """Test initialization with mocked API response"""
        import asyncio
        
        provider = ImportantLevelsProvider(symbol='BTC/USD')
        
        # Create mock candle data
        now = datetime.now(UTC)
        mock_candles = []
        
        # Generate 60 days of mock data
        for i in range(60):
            date = now - timedelta(days=60-i)
            mock_candles.append({
                'timestamp': date,
                'open': 90000 + i * 100,
                'high': 91000 + i * 100,
                'low': 89000 + i * 100,
                'close': 90500 + i * 100,
                'volume': 1000000
            })
        
        # Patch the fetch method
        async def mock_fetch(*args, **kwargs):
            return mock_candles
        
        provider._fetch_daily_candles = mock_fetch
        
        # Run the async initialization
        success = asyncio.run(provider.initialize())
        
        # Should succeed and have levels computed
        assert success is True
        assert provider.is_initialized is True
        assert len(provider._levels) > 0


# Run tests if executed directly
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
