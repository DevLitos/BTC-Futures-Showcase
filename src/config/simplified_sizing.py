"""
SIMPLIFIED SIZING SYSTEM (Jan 27, 2026)
=======================================

PHILOSOPHY CHANGE:
- OLD: 16 multipliers × filtering = tiny positions always (fear-based)
- NEW: 5 multipliers × hard blocks = bet big when edge exists

This is NOT about reducing damage. This is about EXPLOITING EDGE.

A winning system:
- Finds FEW situations where it's worth betting STRONG

Your old system:
- Finds MANY situations where it tries to bet SMALL

5 CORE MULTIPLIERS (everything else becomes HARD BLOCK or 1.0):
1. base_risk        - 1% of capital, period
2. rr_bucket        - RR < 0.7 = BLOCK, 0.7-1.0 = 0.5x, ≥1.0 = 1.0x
3. regime           - RANGE = 0.7x, TREND = 1.0x
4. loss_streak      - 0-1 loss = 1.0x, 2+ = 0.5x
5. session          - good = 1.0x, moderate = 0.8x, poor = BLOCK

HARD BLOCKS (NO SIZING REDUCTION - JUST BLOCK):
- session_quality == 'poor' → BLOCK (no trade)
- worst_case_rr < 0.7 → BLOCK (no trade)
- score_structure == 0 → BLOCK (no trade)
- chase > 2.5 ATR → BLOCK (no trade)
- edge_state != ACTIVE → BLOCK (no trade)

REMOVED (set to 1.0):
- score_mult        → score decides BLOCK or not, not sizing
- ltf_mult          → part of HARD BLOCK decision
- day_mult          → noise
- spread_mult       → noise (use hard block if spread > threshold)
- countertrend_mult → use regime instead
- anticipation_mult → probe entries are eliminated
- volatility_mult   → noise (covered by regime)
- gate_cap          → eliminated
- range_mult        → covered by regime_mult
- daily_regime_mult → covered by regime_mult
- location_cap      → eliminated (HARD BLOCK at extremes)

SIMPLIFIED EXITS (30-day test):
- SL structural (no change)
- TP fixed: 1.5R in trend, 1.0R in range
- Trailing: ONLY after 1R profit
- WTP: DISABLED (this is the painful but correct decision)
- TIME_INVALIDATION: Keep but simplify to 60 min flat

Author: FideiRatioCapital
Date: January 27, 2026
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Tuple, Optional, Dict, Any, TYPE_CHECKING
import logging

# Avoid circular imports - only for type hints
if TYPE_CHECKING:
    from src.domain.trading.edge_state_manager import LiquidityEvent

logger = logging.getLogger(__name__)


class SimplifiedSizingMode(Enum):
    """Sizing mode - toggle between old (complex) and new (simplified)"""
    LEGACY = "legacy"       # Old 16-multiplier system
    SIMPLIFIED = "simplified"  # New 5-multiplier system


# =============================================================================
# CONFIGURATION FLAG - TOGGLE THIS TO SWITCH MODES
# =============================================================================
SIZING_MODE = SimplifiedSizingMode.SIMPLIFIED  # 🔥 Change to LEGACY to revert


# =============================================================================
# SIMPLIFIED SIZING RESULT
# =============================================================================
@dataclass
class SimplifiedSizingResult:
    """Result of simplified sizing calculation"""
    should_trade: bool = True
    block_reason: Optional[str] = None
    
    # The 5 core multipliers (+ edge mode + adaptive filters)
    base_risk_pct: float = 0.01  # 1% always
    rr_mult: float = 1.0
    regime_mult: float = 1.0
    loss_streak_mult: float = 1.0
    session_mult: float = 1.0
    edge_mult: float = 1.0  # Edge mode multiplier (INSTITUTIONAL/INTRADAY)
    toxic_hour_mult: float = 1.0  # Toxic hour multiplier
    toxic_combo_mult: float = 1.0  # 🔥 NEW: Toxic combo multiplier (REDUCE mode)
    momentum_mult: float = 1.0  # Momentum predictor multiplier
    
    # Final
    combined_multiplier: float = 1.0
    final_size: float = 0.0
    
    # Edge mode info
    edge_mode: str = 'INSTITUTIONAL'  # 🔥 NEW: Track which mode
    
    # 🔥 NEW (Jan 28): PROVISIONAL edge support
    is_provisional: bool = False      # True if this is a provisional/starter position
    provisional_reason: str = ''      # Why it's provisional (timing_zero, low_r_no_liquidity, etc)
    # PASO 6 — Elimina micro-TPs: provisional no puede tener TP corto
    provisional_tp_r: float = 1.0
    provisional_time_stop_min: int = 5  # Time stop in minutes
    
    # Debug info - use field(default_factory=dict) for mutable default
    tags: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# RR BUCKET CONFIGURATION
# =============================================================================
class RRBucket:
    """
    Simplified RR sizing.
    
    OLD: Complex curve with 0.25x, 0.5x, 0.75x...
    NEW: 3 buckets only
    """
    BLOCK_THRESHOLD = 0.70       # Below this = HARD BLOCK
    REDUCED_THRESHOLD = 1.00     # Below this = 0.5x
    # Above 1.0R = 1.0x
    
    @classmethod
    def get_multiplier(cls, worst_case_rr: float) -> Tuple[float, bool]:
        """
        Returns (multiplier, should_block)
        """
        if worst_case_rr < cls.BLOCK_THRESHOLD:
            return 0.0, True  # HARD BLOCK
        elif worst_case_rr < cls.REDUCED_THRESHOLD:
            return 0.5, False  # Reduced but trade
        else:
            return 1.0, False  # Full size


# =============================================================================
# REGIME MULTIPLIER
# =============================================================================
class RegimeMultiplier:
    """
    Simplified regime sizing.
    
    OLD: Different mults for TRANSITION, RANGE_DAY, daily_regime_config...
    NEW: 2 states - TREND or RANGE
    
    🔄 REFACTORED Feb 04: Uses ADXThresholds from trading_config
    """
    TREND = 1.0
    RANGE = 0.7  # ADX < ADXThresholds.RANGE (18)
    
    @classmethod
    def get_multiplier(cls, adx: Optional[float], regime_str: str = "NORMAL") -> float:
        """Get regime multiplier based on ADX.
        
        Uses centralized ADXThresholds for consistency.
        """
        from src.config.trading_config import ADXThresholds
        
        if adx is not None and adx < ADXThresholds.RANGE:
            return cls.RANGE
        if regime_str.upper() in ('RANGE', 'RANGING'):
            return cls.RANGE
        return cls.TREND


# =============================================================================
# LOSS STREAK MULTIPLIER
# =============================================================================
class LossStreakMultiplier:
    """
    🔥 ENHANCED (Feb 1, 2026): Graduated loss streak handling.
    
    OLD: Binary - normal or 0.5x after 2 losses
    NEW: Progressive reduction to prevent revenge trading
    
    - 0-1 losses: 1.0x (normal)
    - 2 losses: 0.6x 
    - 3 losses: 0.4x
    - 4 losses: 0.25x (minimal position)
    - 5+ losses: 0.1x (token size - effectively just observing)
    """
    NORMAL = 1.0
    AFTER_2_LOSSES = 0.6
    AFTER_3_LOSSES = 0.4
    AFTER_4_LOSSES = 0.25
    AFTER_5_LOSSES = 0.1  # Token size - system is clearly in bad environment
    
    @classmethod
    def get_multiplier(cls, consecutive_losses: int) -> float:
        if consecutive_losses >= 5:
            return cls.AFTER_5_LOSSES
        if consecutive_losses >= 4:
            return cls.AFTER_4_LOSSES
        if consecutive_losses >= 3:
            return cls.AFTER_3_LOSSES
        if consecutive_losses >= 2:
            return cls.AFTER_2_LOSSES
        return cls.NORMAL


# =============================================================================
# LOSS STREAK COOLDOWN (Feb 2, 2026)
# 🔄 DEPRECATED Feb 04: Use CooldownManager instead
# Kept for backward compatibility - delegates to CooldownManager
# =============================================================================
class LossStreakCooldown:
    """
    Time-based cooldown after consecutive losses.
    
    ⚠️ DEPRECATED: Use src.application.live.cooldown_manager.CooldownManager instead.
    This class is kept for backward compatibility and delegates to CooldownManager.
    """
    
    @classmethod
    def get_cooldown_minutes(cls, consecutive_losses: int) -> int:
        """Get required cooldown time in minutes.
        
        DEPRECATED: Use CooldownManager.activate_loss_streak_cooldown() instead.
        """
        from src.config.trading_config import RiskManagement
        
        if not RiskManagement.LOSS_STREAK_COOLDOWN_ENABLED:
            return 0
        
        if consecutive_losses >= 5:
            return RiskManagement.LOSS_STREAK_5_COOLDOWN_MIN
        if consecutive_losses >= 4:
            return RiskManagement.LOSS_STREAK_4_COOLDOWN_MIN
        if consecutive_losses >= 3:
            return RiskManagement.LOSS_STREAK_3_COOLDOWN_MIN
        return 0  # No cooldown
    
    @classmethod
    def is_in_cooldown(cls, consecutive_losses: int, last_trade_time: Optional[datetime], current_time: datetime) -> Tuple[bool, int]:
        """
        Check if we're still in cooldown period.
        
        DEPRECATED: Use CooldownManager.check_can_trade() instead.
        """
        # Delegate to CooldownManager for consistent behavior
        try:
            from src.application.live.cooldown_manager import get_cooldown_manager
            mgr = get_cooldown_manager()
            mgr.activate_loss_streak_cooldown(consecutive_losses, current_time, last_trade_time)
            result = mgr.check_can_trade(current_time)
            if not result.can_trade:
                return True, result.remaining_seconds
            return False, 0
        except ImportError:
            # Fallback to legacy logic if CooldownManager not available
            from src.config.trading_config import RiskManagement
            
            if not RiskManagement.LOSS_STREAK_COOLDOWN_ENABLED:
                return False, 0
            
            if last_trade_time is None:
                return False, 0
            
            cooldown_minutes = cls.get_cooldown_minutes(consecutive_losses)
            if cooldown_minutes == 0:
                return False, 0
            
            elapsed = (current_time - last_trade_time).total_seconds()
            required = cooldown_minutes * 60
            
            if elapsed < required:
                remaining = int(required - elapsed)
                return True, remaining
            
            return False, 0


# =============================================================================
# TOXIC HOUR MULTIPLIER (Feb 2, 2026)
# =============================================================================
class ToxicHourMultiplier:
    """
    Reduce sizing during historically toxic hours.
    
    ⚠️ ADAPTIVE: Uses config from RiskManagement for easy recalibration.
    Based on Jan 20 - Feb 1, 2026 analysis but should be monitored.
    
    Config (in trading_config.py):
    - TOXIC_HOURS_ENABLED: bool
    - TOXIC_HOURS_SET: set of UTC hours
    - TOXIC_HOURS_MULT: sizing multiplier (default 0.5)
    """
    
    @classmethod
    def get_multiplier(cls, hour_utc: int) -> float:
        """Get multiplier for current UTC hour."""
        from src.config.trading_config import RiskManagement
        
        if not RiskManagement.TOXIC_HOURS_ENABLED:
            return 1.0
        
        if hour_utc in RiskManagement.TOXIC_HOURS_SET:
            mult = RiskManagement.TOXIC_HOURS_MULT
            logger.info(f"⚠️ TOXIC HOUR: {hour_utc:02d}:00 UTC → {mult}x sizing")
            return mult
        return 1.0
    
    @classmethod
    def from_datetime(cls, dt: datetime) -> float:
        """Get multiplier from datetime object."""
        return cls.get_multiplier(dt.hour)


# =============================================================================
# TOXIC COMBINATION HANDLER (Feb 2, 2026)
# =============================================================================
class ToxicCombinationBlock:
    """
    Handle session+direction combinations with historical negative edge.
    
    ⚠️ ADAPTIVE: Uses config from RiskManagement for easy recalibration.
    
    MODES:
    - 'BLOCK': No trade allowed (original behavior)
    - 'REDUCE': Reduce sizing but still trade (more adaptive)
    
    Config (in trading_config.py):
    - TOXIC_COMBOS_ENABLED: bool
    - TOXIC_COMBOS_MODE: 'BLOCK' or 'REDUCE'
    - TOXIC_COMBOS_MULT: sizing multiplier if MODE='REDUCE'
    - TOXIC_COMBOS_SET: set of (session, direction) tuples
    """
    
    @classmethod
    def is_blocked(cls, session_name: str, direction: str) -> Tuple[bool, str]:
        """
        Check if session+direction combination should be blocked.
        
        Returns:
            (is_blocked, reason)
        """
        from src.config.trading_config import RiskManagement
        
        if not RiskManagement.TOXIC_COMBOS_ENABLED:
            return False, ""
        
        # Normalize inputs
        session_upper = session_name.upper()
        direction_upper = direction.upper()
        combo = (session_upper, direction_upper)
        
        if combo in RiskManagement.TOXIC_COMBOS_SET:
            mode = RiskManagement.TOXIC_COMBOS_MODE
            
            if mode == 'BLOCK':
                reason = f"🛑 TOXIC COMBO BLOCK: {session_upper} + {direction_upper} (historical negative edge)"
                logger.warning(reason)
                return True, reason
            else:
                # REDUCE mode - don't block, just log (reduction handled in get_multiplier)
                logger.info(f"⚠️ TOXIC COMBO: {session_upper} + {direction_upper} → reduced sizing")
                return False, ""
        
        return False, ""
    
    @classmethod
    def get_multiplier(cls, session_name: str, direction: str) -> float:
        """
        Get sizing multiplier for session+direction combo.
        
        Returns 1.0 for normal combos, TOXIC_COMBOS_MULT for toxic combos in REDUCE mode.
        """
        from src.config.trading_config import RiskManagement
        
        if not RiskManagement.TOXIC_COMBOS_ENABLED:
            return 1.0
        
        session_upper = session_name.upper()
        direction_upper = direction.upper()
        combo = (session_upper, direction_upper)
        
        if combo in RiskManagement.TOXIC_COMBOS_SET:
            if RiskManagement.TOXIC_COMBOS_MODE == 'REDUCE':
                return RiskManagement.TOXIC_COMBOS_MULT
        
        return 1.0


# =============================================================================
# MOMENTUM SIZING MULTIPLIER (Feb 2, 2026)
# =============================================================================
class MomentumSizingMultiplier:
    """
    Adjust sizing based on momentum score as a predictor.
    
    ⚠️ ADAPTIVE: Uses config from RiskManagement for easy recalibration.
    
    Config (in trading_config.py):
    - MOMENTUM_SIZING_ENABLED: bool
    - MOMENTUM_LOW_THRESHOLD: below this = low momentum
    - MOMENTUM_HIGH_THRESHOLD: above this = high momentum
    - MOMENTUM_LOW_MULT: sizing mult for low momentum
    - MOMENTUM_HIGH_MULT: sizing mult for high momentum
    """
    
    @classmethod
    def get_multiplier(cls, momentum_score: float) -> float:
        """
        Get sizing multiplier based on momentum score.
        
        Args:
            momentum_score: 0-2 momentum score from entry_scorer
            
        Returns:
            Sizing multiplier
        """
        from src.config.trading_config import RiskManagement
        
        if not RiskManagement.MOMENTUM_SIZING_ENABLED:
            return 1.0
        
        if momentum_score >= RiskManagement.MOMENTUM_HIGH_THRESHOLD:
            mult = RiskManagement.MOMENTUM_HIGH_MULT
            logger.debug(f"📈 MOMENTUM BOOST: score={momentum_score:.2f} >= {RiskManagement.MOMENTUM_HIGH_THRESHOLD} → {mult}x sizing")
            return mult
        elif momentum_score < RiskManagement.MOMENTUM_LOW_THRESHOLD:
            mult = RiskManagement.MOMENTUM_LOW_MULT
            logger.info(f"📉 MOMENTUM WEAK: score={momentum_score:.2f} < {RiskManagement.MOMENTUM_LOW_THRESHOLD} → {mult}x sizing")
            return mult
        return 1.0


# =============================================================================
# SESSION MULTIPLIER
# =============================================================================
class SessionMultiplier:
    """
    Simplified session handling.
    
    OLD: 'poor' sessions had reduced size
    NEW: 'poor' sessions = HARD BLOCK (in HardBlockValidator)
         Only good/moderate trade
    """
    GOOD = 1.0      # LONDON, NY_OVERLAP, NY
    MODERATE = 0.8  # ASIA
    # POOR = HARD BLOCK (not a multiplier)
    
    @classmethod
    def get_multiplier(cls, session_quality: str) -> Tuple[float, bool]:
        """
        Returns (multiplier, should_block)
        
        🔄 REFACTORED Feb 04: Uses SessionQuality.is_quality_blocked for consistency.
        """
        from src.config.trading_config import SessionQuality
        
        if SessionQuality.is_quality_blocked(session_quality):
            return 0.0, True  # HARD BLOCK
        
        mult = SessionQuality.get_quality_size_mult(session_quality)
        return mult, False


# =============================================================================
# SIMPLIFIED SIZER - THE MAIN CLASS
# =============================================================================
class SimplifiedSizer:
    """
    5-multiplier sizing system.
    
    Use this instead of GraduatedSizer when SIZING_MODE == SIMPLIFIED
    
    🔥 NEW (Jan 28, 2026): Added edge_mode multiplier
    - INSTITUTIONAL: 1.0x - 1.5x (high conviction)
    - INTRADAY: 0.15x - 0.30x (low conviction, high frequency)
    
    🔥 NEW (Feb 2, 2026): Added toxic hour multiplier and combo blocks
    """
    
    @staticmethod
    def calculate(
        capital: float,
        entry_price: float,
        stop_loss: float,
        worst_case_rr: float,
        regime_str: str,
        adx: Optional[float],
        consecutive_losses: int,
        session_quality: str,
        # 🔥 NEW: Edge mode parameters
        edge_mode: str = 'INSTITUTIONAL',  # 'INSTITUTIONAL' or 'INTRADAY'
        edge_mode_multiplier: float = 1.0,  # From EdgeStateManager
        # 🔥 NEW (Jan 28): Liquidity event for R-validation
        liquidity_event: Optional[Any] = None,  # LiquidityEvent from edge_state_manager
        # 🔥 NEW (Feb 2, 2026): Toxic combo and hour parameters
        session_name: str = '',  # For toxic combo check (e.g., 'ASIA', 'LONDON')
        direction: str = '',     # For toxic combo check ('LONG' or 'SHORT')
        market_time: Optional[datetime] = None,  # For toxic hour check
        last_trade_time: Optional[datetime] = None,  # For loss streak cooldown
        # 🔥 NEW (Feb 2, 2026): Momentum predictor
        momentum_score: float = 1.0,  # 0-2 score from entry_scorer
    ) -> SimplifiedSizingResult:
        """
        Calculate position size using simplified 5-multiplier system.
        
        🔥 CRITICAL (Jan 28, 2026): INTRADAY R < 0.3 REQUIRES liquidity_event
        Sin sweep/rejection → NO TRADE. Sin negociación.
        
        🔥 NEW (Feb 2, 2026): 
        - Blocks toxic session+direction combos
        - Reduces sizing 50% during toxic hours (0, 4, 21, 23 UTC)
        - Enforces cooldown after 3+ consecutive losses
        
        Args:
            capital: Trading capital in USD
            entry_price: Entry price
            stop_loss: Stop loss price
            worst_case_rr: Worst-case risk-reward ratio
            regime_str: Current regime ('TREND', 'RANGE', etc)
            adx: ADX value (or None)
            consecutive_losses: Number of consecutive losses
            session_quality: 'good', 'moderate', or 'poor'
            edge_mode: 'INSTITUTIONAL' or 'INTRADAY'
            edge_mode_multiplier: From EdgeStateManager
            liquidity_event: Optional LiquidityEvent for R-validation (INTRADAY)
            session_name: Session name for toxic combo check
            direction: Direction for toxic combo check
            market_time: Current time for toxic hour check
            last_trade_time: Time of last trade exit (for cooldown)
        
        Returns:
            SimplifiedSizingResult with final size and breakdown
        """
        result = SimplifiedSizingResult()
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): LOSS STREAK COOLDOWN
        # Enforce waiting period after 3+ consecutive losses
        # =====================================================================
        if consecutive_losses >= 3 and market_time is not None:
            in_cooldown, remaining = LossStreakCooldown.is_in_cooldown(
                consecutive_losses, last_trade_time, market_time
            )
            if in_cooldown:
                result.should_trade = False
                result.block_reason = (
                    f"⏳ LOSS STREAK COOLDOWN: {consecutive_losses} losses → "
                    f"wait {remaining}s more"
                )
                result.tags['loss_streak_cooldown'] = True
                result.tags['cooldown_remaining_s'] = remaining
                return result
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): TOXIC COMBINATION HANDLER
        # In BLOCK mode: blocks trade entirely
        # In REDUCE mode: applies sizing multiplier (more adaptive)
        # =====================================================================
        if session_name and direction:
            is_toxic, toxic_reason = ToxicCombinationBlock.is_blocked(session_name, direction)
            if is_toxic:
                result.should_trade = False
                result.block_reason = toxic_reason
                result.tags['toxic_combo_blocked'] = True
                result.tags['toxic_combo'] = f"{session_name}_{direction}"
                return result
            
            # Apply REDUCE multiplier if in REDUCE mode
            result.toxic_combo_mult = ToxicCombinationBlock.get_multiplier(session_name, direction)
            if result.toxic_combo_mult < 1.0:
                result.tags['toxic_combo_reduced'] = True
                result.tags['toxic_combo'] = f"{session_name}_{direction}"
                result.tags['toxic_combo_mult'] = result.toxic_combo_mult
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): TOXIC HOUR MULTIPLIER
        # Reduce size 50% during hours 0, 4, 21, 23 UTC
        # =====================================================================
        if market_time is not None:
            result.toxic_hour_mult = ToxicHourMultiplier.from_datetime(market_time)
            result.tags['toxic_hour_mult'] = result.toxic_hour_mult
            result.tags['entry_hour_utc'] = market_time.hour
        
        # =====================================================================
        # 🔥 NEW (Feb 2, 2026): MOMENTUM SIZING MULTIPLIER
        # Boost high momentum (>=1.5) by 1.2x, reduce low (<0.5) by 0.5x
        # =====================================================================
        result.momentum_mult = MomentumSizingMultiplier.get_multiplier(momentum_score)
        result.tags['momentum_score'] = momentum_score
        result.tags['momentum_mult'] = result.momentum_mult
        
        # =====================================================================
        # 🔥 INTRADAY vs INSTITUTIONAL: Filtros diferentes
        # INTRADAY tiene sus propios blocks para no asfixiarse
        # 
        # INTRADAY THRESHOLDS (hardcoded to avoid circular import):
        # - RR_BLOCK_THRESHOLD = 0.25R (vs 0.70R institutional)
        # - RR_REDUCED_THRESHOLD = 0.40R
        # - ADX_LOW_SIZE_MULT = 0.8x
        # - SESSION_POOR_SIZE_MULT = 0.5x
        # =====================================================================
        is_intraday = edge_mode.upper() == 'INTRADAY'
        
        # INTRADAY specific thresholds (from IntradayEdgeCriteria)
        INTRADAY_RR_BLOCK = 0.15      # Minimum RR absoluto (antes 0.25)
        INTRADAY_RR_REDUCED = 0.40    # Full size above this
        INTRADAY_ADX_LOW_MULT = 0.8   # Size mult if ADX < 15
        INTRADAY_SESSION_POOR_MULT = 0.5  # Size mult in poor session
        
        # =====================================================================
        # 🔥 CAMBIO CLAVE #3 (Jan 28): R < 0.30 NO BLOQUEA, DEGRADA A PROVISIONAL
        # 
        # ANTES: R < 0.30 sin liquidity → NO TRADE
        # AHORA: R < 0.30 sin liquidity → PROVISIONAL (size 0.10x)
        # 
        # ❗ IMPORTANTE:
        # - R < 0.25 sigue bloqueado (mínimo absoluto)
        # - Liquidity sigue siendo obligatoria para size grande
        # - Sin liquidity = starter position con TP corto
        # =====================================================================
        INTRADAY_LIQUIDITY_REQUIRED_BELOW_R = 0.30
        INTRADAY_R_HARD_FLOOR = 0.25  # 🔥 Mínimo absoluto - NO negociable
        PROVISIONAL_SIZE_MULT = 0.10  # Size para R bajo sin liquidity
        
        if is_intraday and worst_case_rr < INTRADAY_LIQUIDITY_REQUIRED_BELOW_R:
            # R bajo: ¿Hay liquidity event válido?
            has_valid_liquidity = False
            event_type = 'NONE'
            event_confidence = 0.0
            
            if liquidity_event is not None:
                if hasattr(liquidity_event, 'is_valid'):
                    has_valid_liquidity = liquidity_event.is_valid()
                    if hasattr(liquidity_event, 'event_type'):
                        event_type = getattr(liquidity_event.event_type, 'value', str(liquidity_event.event_type))
                    if hasattr(liquidity_event, 'confidence'):
                        event_confidence = liquidity_event.confidence
            
            # 🔥 HARD FLOOR: R < 0.25 sigue bloqueado SIN EXCEPCIONES
            if worst_case_rr < INTRADAY_R_HARD_FLOOR:
                result.should_trade = False
                result.block_reason = (
                    f"🚫 R={worst_case_rr:.2f} < {INTRADAY_R_HARD_FLOOR} HARD FLOOR. "
                    f"Mínimo absoluto no negociable."
                )
                result.rr_mult = 0.0
                logger.warning(f"🚫 INTRADAY R HARD FLOOR: {result.block_reason}")
                return result
            
            if not has_valid_liquidity:
                # 🔥 NUEVO: NO bloquear, DEGRADAR a PROVISIONAL
                result.is_provisional = True
                result.provisional_reason = f'low_r_no_liquidity (R={worst_case_rr:.2f})'
                # PASO 6 — Elimina micro-TPs
                result.provisional_tp_r = max(result.provisional_tp_r, 1.0)
                result.provisional_time_stop_min = 3  # Time stop corto
                result.edge_mult = PROVISIONAL_SIZE_MULT  # Forzar size 0.10x
                result.tags['provisional'] = True
                result.tags['provisional_reason'] = 'low_r_no_liquidity'
                logger.info(
                    f"⚠️ PROVISIONAL: R={worst_case_rr:.2f} sin liquidity → "
                    f"starter position {PROVISIONAL_SIZE_MULT}x, TP={result.provisional_tp_r}R"
                )
            else:
                logger.info(
                    f"✅ INTRADAY R={worst_case_rr:.2f} PERMITIDO con {event_type} "
                    f"(conf: {event_confidence:.2f})"
                )
                result.tags['liquidity_event'] = event_type
                result.tags['liquidity_confidence'] = event_confidence
        
        # 1. RR BUCKET (can BLOCK) - DIFERENTES THRESHOLDS
        if is_intraday:
            # INTRADAY: threshold más bajo (0.25R min)
            if worst_case_rr < INTRADAY_RR_BLOCK:
                result.should_trade = False
                result.block_reason = f"INTRADAY RR {worst_case_rr:.2f} < {INTRADAY_RR_BLOCK}"
                result.rr_mult = 0.0
                logger.warning(f"🚫 INTRADAY BLOCK: {result.block_reason}")
                return result
            elif worst_case_rr < INTRADAY_RR_REDUCED:
                rr_mult = 0.7  # Reduced size
            else:
                rr_mult = 1.0
            rr_block = False
        else:
            # INSTITUTIONAL: usar thresholds normales
            rr_mult, rr_block = RRBucket.get_multiplier(worst_case_rr)
            if rr_block:
                result.should_trade = False
                result.block_reason = f"RR {worst_case_rr:.2f} < {RRBucket.BLOCK_THRESHOLD}"
                result.rr_mult = 0.0
                logger.warning(f"🚫 SIMPLIFIED BLOCK: {result.block_reason}")
                return result
        result.rr_mult = rr_mult
        result.tags['rr_mult'] = rr_mult
        result.tags['rr_bucket'] = '1.0+' if rr_mult == 1.0 else ('intraday' if is_intraday else '0.7-1.0')
        
        # 2. REGIME - INTRADAY NO bloquea por ADX
        if is_intraday:
            # INTRADAY: no bloquea por ADX bajo, solo reduce levemente
            if adx is not None and adx < 15:
                result.regime_mult = INTRADAY_ADX_LOW_MULT  # 0.8x
            else:
                result.regime_mult = 1.0
            result.tags['regime'] = 'INTRADAY_ANY'
        else:
            # INSTITUTIONAL: usar regime multiplier normal
            result.regime_mult = RegimeMultiplier.get_multiplier(adx, regime_str)
            result.tags['regime'] = 'RANGE' if result.regime_mult < 1.0 else 'TREND'
        result.tags['regime_mult'] = result.regime_mult
        
        # 3. LOSS STREAK - Aplica a ambos
        result.loss_streak_mult = LossStreakMultiplier.get_multiplier(consecutive_losses)
        result.tags['loss_streak_mult'] = result.loss_streak_mult
        result.tags['consecutive_losses'] = consecutive_losses
        
        # 4. SESSION - INTRADAY NO bloquea por poor session
        # 🔄 REFACTORED Feb 04: Use SessionQuality centralized class
        from src.config.trading_config import SessionQuality
        
        if is_intraday:
            # INTRADAY: no bloquea, solo reduce size  
            if SessionQuality.is_quality_blocked(session_quality):
                session_mult = INTRADAY_SESSION_POOR_MULT  # 0.5x
                session_block = False
                logger.info(f"⚡ INTRADAY: Poor session - size reduced to 50%")
            else:
                # Use centralized multiplier map
                session_mult = SessionQuality.get_quality_size_mult(session_quality)
                session_block = False
        else:
            # INSTITUTIONAL: poor session = BLOCK
            session_mult, session_block = SessionMultiplier.get_multiplier(session_quality)
            if session_block:
                result.should_trade = False
                result.block_reason = f"Session quality '{session_quality}' blocked"
                result.session_mult = 0.0
                logger.warning(f"🚫 SIMPLIFIED BLOCK: {result.block_reason}")
                return result
        result.session_mult = session_mult
        result.tags['session_mult'] = session_mult
        result.tags['session_quality'] = session_quality
        
        # 5. EDGE MODE MULTIPLIER (NEW Jan 28, 2026)
        # INSTITUTIONAL: 1.0x - 1.5x
        # INTRADAY: 0.15x - 0.30x
        # 🔥 PROVISIONAL: 0.10x (ya seteado arriba, NO sobrescribir)
        if not result.is_provisional:
            # Solo sobrescribir si NO es provisional
            result.edge_mult = edge_mode_multiplier
        # else: result.edge_mult ya fue forzado a PROVISIONAL_SIZE_MULT (0.10x) arriba
        
        result.edge_mode = edge_mode
        result.tags['edge_mode'] = edge_mode
        result.tags['edge_mode_mult'] = result.edge_mult
        if result.is_provisional:
            result.tags['edge_mode_provisional'] = True
        
        # 6. COMBINED MULTIPLIER (includes all adaptive filters)
        result.combined_multiplier = (
            result.rr_mult *
            result.regime_mult *
            result.loss_streak_mult *
            result.session_mult *
            result.edge_mult *          # Edge mode multiplier
            result.toxic_hour_mult *    # Toxic hour multiplier
            result.toxic_combo_mult *   # Toxic combo multiplier (REDUCE mode)
            result.momentum_mult        # Momentum predictor multiplier
        )
        
        # Calculate base size from risk
        risk_per_trade = capital * result.base_risk_pct  # 1% of capital
        risk_in_price = abs(entry_price - stop_loss)
        
        if risk_in_price <= 0:
            result.should_trade = False
            result.block_reason = "Invalid risk (SL == entry)"
            return result
        
        base_size_btc = risk_per_trade / risk_in_price
        
        # Apply combined multiplier
        result.final_size = base_size_btc * result.combined_multiplier
        
        # Minimum size check (Kraken)
        KRAKEN_MIN = 0.0001
        if result.final_size < KRAKEN_MIN:
            result.final_size = KRAKEN_MIN
            result.tags['min_size_applied'] = True
        
        # Log the calculation
        toxic_hour_log = f" × TOXIC_HR={result.toxic_hour_mult:.2f}" if result.toxic_hour_mult < 1.0 else ""
        toxic_combo_log = f" × TOXIC_COMBO={result.toxic_combo_mult:.2f}" if result.toxic_combo_mult < 1.0 else ""
        momentum_log = f" × MOM={result.momentum_mult:.2f}" if result.momentum_mult != 1.0 else ""
        logger.info(
            f"📊 SIMPLIFIED SIZING [{edge_mode}]: "
            f"RR={result.rr_mult:.2f} × "
            f"REG={result.regime_mult:.2f} × "
            f"LOSS={result.loss_streak_mult:.2f} × "
            f"SESS={result.session_mult:.2f} × "
            f"EDGE={result.edge_mult:.2f}"
            f"{toxic_hour_log}{toxic_combo_log}{momentum_log} = "
            f"{result.combined_multiplier:.2f} → "
            f"{result.final_size:.6f} BTC"
        )
        
        return result


# =============================================================================
# SIMPLIFIED EXIT CONFIG
# =============================================================================
class SimplifiedExitConfig:
    """
    Simplified exit configuration for 30-day test.
    
    CHANGES FROM LEGACY:
    - WTP: DISABLED
    - TP: Fixed based on regime (not dynamic)
    - Trailing: Only after 1R profit
    - TIME_INVALIDATION: Simplified to 60 min flat
    
    🔥 NEW (Jan 28, 2026): BREAKOUT edge uses 2.0R minimum
    BREAKOUT has lower winrate but needs bigger R to pay for failures.
    
    🔥 NEW (Jan 28, 2026): INTRADAY edge uses smaller TP (0.3-0.8R)
    INTRADAY is high frequency, small wins.
    """
    
    # WTP - DISABLED for 30 days to see true edge
    WORST_TRADE_PREVENTION_ENABLED = False  # 🔥 THIS IS THE PAINFUL DECISION
    
    # Take Profit - Fixed by regime (INSTITUTIONAL)
    TP_R_TREND = 1.5    # 1.5R in trending markets
    TP_R_RANGE = 1.0    # 1.0R in ranging markets
    
    # 🔥 BREAKOUT edge - higher TP target obligatorio
    TP_R_BREAKOUT = 2.0       # BREAKOUT MUST target 2.0R minimum (low winrate)
    TP_R_BREAKOUT_IDEAL = 3.0 # Ideal target for BREAKOUT
    
    # 🔥 DISPLACEMENT edge (Jan 30, 2026) - Momentum plays need room to run
    TP_R_DISPLACEMENT = 2.0        # DISPLACEMENT targets 2.0R (institutional momentum)
    TP_R_DISPLACEMENT_IDEAL = 2.5  # Ideal 2.5R for strong displacement
    
    # 🔥 INTRADAY edge - Must be >= RR floor (0.7R) to be viable
    # Jan 28, 2026: Changed from 0.5R to 1.0R - 0.5R was ALWAYS blocked by RR floor
    TP_R_INTRADAY = 1.0       # Same as RANGE (must pass RR floor)
    TP_R_INTRADAY_MIN = 0.8   # Minimum 0.8R (above 0.7R floor)
    TP_R_INTRADAY_MAX = 1.0   # Maximum 1.0R (keep conservative)
    SL_R_INTRADAY_MAX = 0.5   # SL técnico: máximo 0.5R
    
    # Trailing - Only after profit
    TRAILING_ACTIVATION_R = 1.0  # Only trail after 1R profit
    TRAILING_ATR_MULT = 1.5      # Trail at 1.5 ATR
    
    # Time Invalidation - Simplified
    TIME_INVALIDATION_MINUTES = 60  # 60 min flat (INSTITUTIONAL)
    TIME_INVALIDATION_INTRADAY_MIN = 5  # 5 min for INTRADAY
    TIME_INVALIDATION_MAX_LOSS_R = -0.20  # 🔥 FIX: -0.20R (was -0.50R!)
    
    # Stop Loss - No change (structural)
    SL_STRUCTURAL = True  # Use structural SL always
    
    @classmethod
    def get_tp_r(cls, regime_str: str, adx: Optional[float] = None, edge_type: Optional[str] = None, edge_mode: Optional[str] = None) -> float:
        """
        Get TP target in R based on regime, edge type, and edge mode.
        
        🔥 FIX (Jan 28, 2026): TP basado PRINCIPALMENTE en régimen de mercado
        - El edge_type es informativo pero no debe cambiar el TP
        - Evita inconsistencia donde mismo contexto da diferente TP
        
        Regla simple:
        - RANGE regime → 1.0R (mean reversion)
        - TREND regime → 1.5R (momentum)
        - BREAKOUT edge → 2.0R (expansion play)
        - DISPLACEMENT edge → 2.0R (institutional momentum)
        
        IMPORTANTE: edge_type=None vs edge_type=INTRADAY NO debe cambiar TP
        """
        # BREAKOUT edge overrides everything - requires high RR to justify low winrate
        if edge_type and edge_type.upper() == 'BREAKOUT':
            logger.info(f"📊 BREAKOUT edge detected - using minimum TP {cls.TP_R_BREAKOUT}R")
            return cls.TP_R_BREAKOUT
        
        # 🔥 DISPLACEMENT edge (Jan 30, 2026) - institutional momentum needs room
        if edge_type and edge_type.upper() == 'DISPLACEMENT':
            logger.info(f"📊 DISPLACEMENT edge detected - using minimum TP {cls.TP_R_DISPLACEMENT}R")
            return cls.TP_R_DISPLACEMENT
        
        # 🔥 FIX: REGIME is primary, NOT edge_type
        # This prevents TP inconsistency between ticks
        # 🔄 REFACTORED Feb 04: Use ADXThresholds
        from src.config.trading_config import ADXThresholds
        if regime_str.upper() in ('RANGE', 'RANGING') or (adx is not None and adx < ADXThresholds.RANGE):
            # RANGE mode - conservative TP
            return cls.TP_R_RANGE  # 1.0R
        
        # TREND mode - larger TP
        return cls.TP_R_TREND  # 1.5R
    
    @classmethod
    def get_sl_r_max(cls, edge_mode: Optional[str] = None) -> float:
        """Get max SL in R for edge mode"""
        if edge_mode and edge_mode.upper() == 'INTRADAY':
            return cls.SL_R_INTRADAY_MAX  # 0.4R max for INTRADAY
        return 1.0  # 1R for INSTITUTIONAL
    
    @classmethod
    def calculate_intraday_sl(
        cls,
        entry_price: float,
        direction: str,
        last_swing_high: float,
        last_swing_low: float,
        atr: float,
        capital: float,  # Trading capital
        risk_pct: float = 0.01  # 1% risk per trade
    ) -> Tuple[float, float]:
        """
        🔥 Calculate TECHNICAL SL for INTRADAY (not fixed 0.4R).
        
        SL = último micro swing ± buffer ATR
        Cap máximo 0.5R
        
        Args:
            entry_price: Entry price
            direction: 'LONG' or 'SHORT'
            last_swing_high: Last micro swing high (from LTF)
            last_swing_low: Last micro swing low (from LTF)
            atr: Current ATR (1m or 5m)
            capital: Trading capital for R calculation
            risk_pct: Risk per trade (default 1%)
        
        Returns:
            (sl_price, sl_in_r) - Stop loss price and its R value
        """
        from src.domain.trading.edge_state_manager import IntradayEdgeCriteria
        
        # Calculate what 1R means in price terms
        # 1R = risk_pct * capital / position_size
        # For simplicity, 1R distance = price * risk_pct (approximation for fixed % risk)
        one_r_distance = entry_price * risk_pct  # ~$1000 for $100k entry with 1% risk
        
        buffer = atr * IntradayEdgeCriteria.SL_SWING_BUFFER_ATR  # 0.15 ATR buffer
        
        if direction.upper() == 'LONG':
            # LONG: SL below last swing low - buffer
            technical_sl = last_swing_low - buffer
            sl_distance = entry_price - technical_sl
        else:
            # SHORT: SL above last swing high + buffer  
            technical_sl = last_swing_high + buffer
            sl_distance = technical_sl - entry_price
        
        # Calculate R value of this SL
        sl_in_r = sl_distance / one_r_distance if one_r_distance > 0 else 999
        
        # Apply caps
        if sl_in_r > IntradayEdgeCriteria.SL_R_CAP:  # 0.5R max
            # Cap the SL
            capped_distance = IntradayEdgeCriteria.SL_R_CAP * one_r_distance
            if direction.upper() == 'LONG':
                technical_sl = entry_price - capped_distance
            else:
                technical_sl = entry_price + capped_distance
            old_r = sl_in_r
            sl_in_r = IntradayEdgeCriteria.SL_R_CAP
            logger.info(f"⚡ INTRADAY SL capped at {IntradayEdgeCriteria.SL_R_CAP}R (was {old_r:.2f}R)")
        
        if sl_in_r < IntradayEdgeCriteria.SL_R_MIN:  # 0.15R min
            # SL too tight - expand to minimum
            min_distance = IntradayEdgeCriteria.SL_R_MIN * one_r_distance
            if direction.upper() == 'LONG':
                technical_sl = entry_price - min_distance
            else:
                technical_sl = entry_price + min_distance
            sl_in_r = IntradayEdgeCriteria.SL_R_MIN
            logger.info(f"⚡ INTRADAY SL expanded to {IntradayEdgeCriteria.SL_R_MIN}R (was too tight)")
        
        logger.info(
            f"⚡ INTRADAY TECHNICAL SL: {direction} | "
            f"Swing: {last_swing_low if direction=='LONG' else last_swing_high:.2f} | "
            f"SL: {technical_sl:.2f} | R: {sl_in_r:.2f}"
        )
        
        return technical_sl, sl_in_r
    
    @classmethod
    def get_time_invalidation(cls, edge_mode: Optional[str] = None) -> int:
        """Get time invalidation in minutes for edge mode"""
        if edge_mode and edge_mode.upper() == 'INTRADAY':
            return cls.TIME_INVALIDATION_INTRADAY_MIN  # 5 min
        return cls.TIME_INVALIDATION_MINUTES  # 60 min
    
    @classmethod
    def should_trail(cls, current_r: float) -> bool:
        """Check if we should start trailing"""
        return current_r >= cls.TRAILING_ACTIVATION_R


# =============================================================================
# FEATURE FLAGS
# =============================================================================
class SimplifiedFeatureFlags:
    """
    Feature flags to toggle simplified vs legacy behavior.
    
    Set these to gradually migrate to simplified system.
    """
    
    # Sizing
    USE_SIMPLIFIED_SIZING = True    # Use 5-mult instead of 16-mult
    
    # Exits
    DISABLE_WTP = True              # Disable WORST_TRADE_PREVENTION
    USE_FIXED_TP = True             # Use fixed TP (1.5R/1.0R) instead of dynamic
    SIMPLIFIED_TRAILING = True      # Trail only after 1R
    SIMPLIFIED_TIME_INVALIDATION = True  # 60 min flat
    
    # Edge
    STRICT_EDGE_MODE = True         # Only trade when edge_state == ACTIVE
    MAX_TRADES_PER_EDGE = 2         # Max 2 attempts per BOS event
    
    @classmethod
    def is_simplified_mode(cls) -> bool:
        """Check if running in simplified mode"""
        return SIZING_MODE == SimplifiedSizingMode.SIMPLIFIED


# =============================================================================
# MIGRATION HELPER
# =============================================================================
def get_sizer():
    """
    Get the appropriate sizer based on current mode.
    
    Usage:
        from src.config.simplified_sizing import get_sizer
        sizer = get_sizer()
        result = sizer.calculate(...)
    """
    if SimplifiedFeatureFlags.is_simplified_mode():
        return SimplifiedSizer
    else:
        # Import legacy sizer
        from src.application.live.entry_validators import GraduatedSizer
        return GraduatedSizer


# =============================================================================
# LOGGING FOR MODE CHANGE
# =============================================================================
logger.info(f"🔧 SIZING MODE: {SIZING_MODE.value.upper()}")
if SIZING_MODE == SimplifiedSizingMode.SIMPLIFIED:
    logger.info("   → 5 multipliers: RR, REGIME, LOSS_STREAK, SESSION + base_risk")
    logger.info("   → WTP: DISABLED")
    logger.info("   → TP: Fixed (1.5R trend / 1.0R range)")
    logger.info("   → Trailing: Only after 1R")
