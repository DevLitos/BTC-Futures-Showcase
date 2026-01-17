"""
Tests for HTFOverextensionExtractor

Tests the fade mode for overextended moves with momentum decay.
"""

import pytest
from datetime import datetime, UTC, timedelta

from modes.htf_overextension_extractor import (
    HTFOverextensionExtractor,
    HTFContext,
    LTFContext,
    TradeResult,
    OverextensionTradeIntent,
    OverextensionConfig,
    build_htf_context,
    build_ltf_context,
    get_htf_overextension_extractor,
    reset_htf_overextension_extractor,
)


@pytest.fixture
def extractor():
    """Fresh extractor for each test"""
    reset_htf_overextension_extractor()
    return get_htf_overextension_extractor()


@pytest.fixture
def overextended_long_htf():
    """HTF context: LONG bias, overextended, momentum decay"""
    return HTFContext(
        bias='LONG',
        close=96000,
        ema50=95000,
        atr=400,  # 2.5 ATR from EMA50
        rsi=72,
        rsi_prev=78,
        rsi_slope=-0.43,
        range_expanding=True,
        close_weak=True,
        signature='LONG_95000_72'
    )


@pytest.fixture
def overextended_short_htf():
    """HTF context: SHORT bias, overextended, momentum decay"""
    return HTFContext(
        bias='SHORT',
        close=94000,
        ema50=95000,
        atr=400,  # 2.5 ATR from EMA50
        rsi=28,
        rsi_prev=22,
        rsi_slope=0.43,  # RSI bouncing = decay in SHORT
        range_expanding=True,
        close_weak=True,
        signature='SHORT_95000_28'
    )


@pytest.fixture
def valid_ltf_swept_high():
    """LTF context: swept high, closed back inside, absorption"""
    return LTFContext(
        close=95980,
        high=96025,
        low=95960,
        open=96000,
        volume=150,
        recent_swing_high=96020,
        recent_swing_low=95900,
        swept_high=True,
        close_back_inside_range=True,
        volume_spike=True,
        delta_ratio=0.55,
        price_progress_ticks=8,
        entry_price=95980,
        atr=80,
        tick_size=1.0,
    )


@pytest.fixture
def valid_ltf_swept_low():
    """LTF context: swept low, closed back inside, absorption"""
    return LTFContext(
        close=94020,
        high=94040,
        low=93975,
        open=94000,
        volume=150,
        recent_swing_high=94100,
        recent_swing_low=93980,
        swept_low=True,
        close_back_inside_range=True,
        volume_spike=True,
        delta_ratio=0.55,
        price_progress_ticks=8,
        entry_price=94020,
        atr=80,
        tick_size=1.0,
    )


class TestContextGate:
    """Tests for evaluate_context (HTF gate)"""
    
    def test_rejects_neutral_bias(self, extractor):
        ctx = HTFContext(bias='NEUTRAL')
        assert extractor.evaluate_context(ctx) is False
        assert 'neutral_bias' in extractor.get_last_rejection()
    
    def test_rejects_not_overextended(self, extractor):
        ctx = HTFContext(
            bias='LONG',
            close=95100,  # Only 0.25 ATR from EMA50
            ema50=95000,
            atr=400,
            rsi_slope=-0.2,
        )
        assert extractor.evaluate_context(ctx) is False
        assert 'not_overextended' in extractor.get_last_rejection()
    
    def test_rejects_no_momentum_decay(self, extractor):
        ctx = HTFContext(
            bias='LONG',
            close=96000,
            ema50=95000,
            atr=400,
            rsi_slope=0.1,  # Positive slope = no decay
            delta_divergence='NONE',
            range_expanding=False,
        )
        assert extractor.evaluate_context(ctx) is False
        assert 'no_momentum_decay' in extractor.get_last_rejection()
    
    def test_accepts_valid_overextended_long(self, extractor, overextended_long_htf):
        assert extractor.evaluate_context(overextended_long_htf) is True
    
    def test_accepts_valid_overextended_short(self, extractor, overextended_short_htf):
        assert extractor.evaluate_context(overextended_short_htf) is True
    
    def test_rejects_too_extended_parabolic(self, extractor):
        ctx = HTFContext(
            bias='LONG',
            close=98000,
            ema50=95000,  # 7.5 ATR from EMA50 = parabolic
            atr=400,
            rsi_slope=-0.3,
        )
        assert extractor.evaluate_context(ctx) is False


class TestSetupDetection:
    """Tests for evaluate_setup (LTF trigger)"""
    
    def test_rejects_no_sweep(self, extractor, overextended_long_htf):
        extractor.evaluate_context(overextended_long_htf)
        
        ltf = LTFContext(
            swept_high=False,  # No sweep
            swept_low=False,
            close_back_inside_range=True,
            volume_spike=True,
            delta_ratio=0.5,
            price_progress_ticks=10,
            recent_swing_high=96020,
            entry_price=95980,
            tick_size=1.0,
        )
        assert extractor.evaluate_setup(ltf, overextended_long_htf) is None
        assert 'no_failed_continuation' in extractor.get_last_rejection()
    
    def test_rejects_no_close_back_inside(self, extractor, overextended_long_htf):
        extractor.evaluate_context(overextended_long_htf)
        
        ltf = LTFContext(
            swept_high=True,
            close_back_inside_range=False,  # Still extended
            volume_spike=True,
            delta_ratio=0.5,
            price_progress_ticks=10,
            recent_swing_high=96020,
            entry_price=96050,
            tick_size=1.0,
        )
        assert extractor.evaluate_setup(ltf, overextended_long_htf) is None
    
    def test_rejects_no_absorption(self, extractor, overextended_long_htf):
        extractor.evaluate_context(overextended_long_htf)
        
        ltf = LTFContext(
            swept_high=True,
            close_back_inside_range=True,
            volume_spike=True,
            delta_ratio=0.1,  # Low delta = no absorption
            price_progress_ticks=50,  # High progress = not absorption
            recent_swing_high=96020,
            entry_price=95980,
            tick_size=1.0,
        )
        assert extractor.evaluate_setup(ltf, overextended_long_htf) is None
        assert 'no_absorption' in extractor.get_last_rejection()
    
    def test_rejects_stop_too_wide(self, extractor, overextended_long_htf):
        extractor.evaluate_context(overextended_long_htf)
        
        ltf = LTFContext(
            swept_high=True,
            close_back_inside_range=True,
            volume_spike=True,
            delta_ratio=0.5,
            price_progress_ticks=10,
            recent_swing_high=96100,  # Too far from entry
            entry_price=95980,
            tick_size=1.0,
        )
        assert extractor.evaluate_setup(ltf, overextended_long_htf) is None
        rejection = extractor.get_last_rejection()
        # Stop is invalid because risk is too high
        assert 'invalid_stop' in rejection or 'stop_too_wide' in rejection
    
    def test_accepts_valid_short_fade(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        
        assert intent is not None
        assert intent.side == 'SHORT'  # Fade the LONG
        assert intent.entry == 95980
        assert intent.stop > intent.entry  # Stop above for SHORT
        assert intent.target < intent.entry  # Target below for SHORT
    
    def test_accepts_valid_long_fade(self, extractor, overextended_short_htf, valid_ltf_swept_low):
        extractor.evaluate_context(overextended_short_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_low, overextended_short_htf)
        
        assert intent is not None
        assert intent.side == 'LONG'  # Fade the SHORT
        assert intent.entry == 94020
        assert intent.stop < intent.entry  # Stop below for LONG
        assert intent.target > intent.entry  # Target above for LONG


class TestLocking:
    """Tests for lock after loss"""
    
    def test_locks_after_loss(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        # Take a trade
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        extractor.on_trade_opened(intent)
        
        # Simulate loss
        result = TradeResult(
            entry_price=intent.entry,
            exit_price=intent.stop,
            side=intent.side,
            r_multiple=-1.0,
            htf_signature=overextended_long_htf.signature
        )
        extractor.on_trade_closed(result)
        
        # Same context should be locked
        assert extractor.evaluate_context(overextended_long_htf) is False
        assert 'cooldown_active' in extractor.get_last_rejection()
    
    def test_unlocks_with_new_context(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        # Take a losing trade
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        extractor.on_trade_opened(intent)
        extractor.on_trade_closed(TradeResult(
            entry_price=intent.entry,
            exit_price=intent.stop,
            side=intent.side,
            r_multiple=-1.0,
            htf_signature=overextended_long_htf.signature
        ))
        
        # Clear cooldown manually for test
        extractor._cooldown_until = None
        
        # Different signature should unlock
        new_htf = HTFContext(
            bias='LONG',
            close=97000,
            ema50=95500,
            atr=380,
            rsi_slope=-0.4,
            signature='LONG_95500_70'  # Different!
        )
        
        assert extractor.evaluate_context(new_htf) is True
    
    def test_no_lock_after_win(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        # Take a winning trade
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        extractor.on_trade_opened(intent)
        extractor.on_trade_closed(TradeResult(
            entry_price=intent.entry,
            exit_price=intent.target,
            side=intent.side,
            r_multiple=1.0,
            htf_signature=overextended_long_htf.signature
        ))
        
        # Same context should NOT be locked
        assert extractor.evaluate_context(overextended_long_htf) is True


class TestStats:
    """Tests for statistics tracking"""
    
    def test_tracks_wins_and_losses(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        extractor.on_trade_opened(intent)
        extractor.on_trade_closed(TradeResult(
            entry_price=intent.entry,
            exit_price=intent.target,
            side=intent.side,
            r_multiple=1.0,
            htf_signature=overextended_long_htf.signature
        ))
        
        stats = extractor.get_stats()
        assert stats['wins'] == 1
        assert stats['losses'] == 0
        assert stats['total_r'] == 1.0
        assert stats['trades_taken'] == 1


class TestTradeIntent:
    """Tests for trade intent structure"""
    
    def test_intent_has_correct_structure(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        
        assert intent.mode == 'HTF_OVEREXTENSION'
        assert intent.target_r == 1.0
        assert intent.risk_ticks > 0
        assert intent.reward_ticks > 0
        assert intent.htf_signature == overextended_long_htf.signature
    
    def test_intent_to_dict(self, extractor, overextended_long_htf, valid_ltf_swept_high):
        extractor.evaluate_context(overextended_long_htf)
        intent = extractor.evaluate_setup(valid_ltf_swept_high, overextended_long_htf)
        
        d = intent.to_dict()
        assert 'side' in d
        assert 'entry' in d
        assert 'stop' in d
        assert 'target' in d
        assert 'mode' in d
