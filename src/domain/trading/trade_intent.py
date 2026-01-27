"""
Trade Intent - Separation of Signal Generation from Execution

This module implements a clean separation between:
1. INTENT: What the strategy wants to do (idealized)
2. EXECUTION: What actually happens (realistic)

DESIGN PHILOSOPHY:
- Strategy generates TradeIntent (what we want)
- Execution layer translates to OrderIntent (how to achieve it)
- Position Store tracks what actually happened

This separation enables:
- Clean backtesting (intent + simulated execution)
- Execution algorithm optimization
- Clear performance attribution
- Risk management at the intent level

USAGE:
    from src.domain.trading.trade_intent import TradeIntentGenerator, get_trade_intent_generator
    
    generator = get_trade_intent_generator()
    
    # Generate intent from strategy signal
    intent = generator.create_entry_intent(
        symbol='BTC/USD:USD',
        side='LONG',
        confidence=0.85,
        edge_score=2.5,
        risk_amount=100.0,  # $100 risk
        entry_price=95000.0,
        sl_price=94500.0,
        tp_price=96500.0,
        reason='BOS confirmed with pullback to OB'
    )
    
    # Validate intent
    if generator.validate_intent(intent):
        # Pass to execution layer
        orders = execution_layer.execute_intent(intent)
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Optional, Dict, List, Tuple
from enum import Enum
import uuid
import logging

from src.bootstrap.runtime import get_runtime_config, RuntimeMode
from src.domain.trading.entities import Side, TradeIntent
from src.domain.trading.cost.cost_model import get_cost_model
from src.infrastructure.storage.parquet.position_store import get_position_store

logger = logging.getLogger(__name__)


# =============================================================================
# INTENT TYPES
# =============================================================================

class IntentType(Enum):
    """Type of trade intent."""
    ENTRY = "ENTRY"           # Open new position
    EXIT = "EXIT"             # Close existing position
    SCALE_IN = "SCALE_IN"     # Add to position
    SCALE_OUT = "SCALE_OUT"   # Reduce position
    FLIP = "FLIP"             # Close and reverse
    ADJUST_SL = "ADJUST_SL"   # Modify stop loss
    ADJUST_TP = "ADJUST_TP"   # Modify take profit


class IntentStatus(Enum):
    """Status of trade intent."""
    PENDING = "PENDING"       # Created, not yet validated
    VALIDATED = "VALIDATED"   # Passed validation
    REJECTED = "REJECTED"     # Failed validation
    EXECUTING = "EXECUTING"   # Being executed
    COMPLETED = "COMPLETED"   # Fully executed
    CANCELLED = "CANCELLED"   # Cancelled before execution
    FAILED = "FAILED"         # Execution failed


class RejectionReason(Enum):
    """Reasons for intent rejection."""
    NONE = ""
    RISK_LIMIT = "risk_limit_exceeded"
    POSITION_EXISTS = "position_already_exists"
    NO_POSITION = "no_position_to_exit"
    INVALID_SL = "invalid_stop_loss"
    INVALID_TP = "invalid_take_profit"
    COST_TOO_HIGH = "cost_exceeds_edge"
    BELOW_MIN_SIZE = "below_minimum_size"
    DAILY_LOSS_LIMIT = "daily_loss_limit_hit"
    CONSECUTIVE_LOSSES = "consecutive_loss_limit"
    MAX_POSITIONS = "max_positions_reached"
    INVALID_PRICE = "invalid_price_level"
    REGIME_MISMATCH = "regime_not_suitable"


# =============================================================================
# TRADE INTENT (IMMUTABLE)
# =============================================================================

@dataclass(frozen=True)
class TradeIntentRecord:
    """
    Complete trade intent record.
    
    This is an IMMUTABLE record of what the strategy intended to do.
    Once created, it cannot be modified - create a new intent instead.
    """
    # Identification
    intent_id: str
    created_at: datetime
    
    # Core intent
    intent_type: IntentType
    symbol: str
    side: str  # 'LONG' or 'SHORT' (position direction)
    
    # Sizing
    risk_amount_usd: float  # How much we're willing to risk
    position_size: float    # Calculated position size
    notional_usd: float     # Position notional value
    
    # Prices
    entry_price: float
    sl_price: float
    tp_price: Optional[float] = None
    
    # Quality metrics
    confidence: float = 0.0  # 0-1 strategy confidence
    edge_score: float = 0.0  # Strategy-specific edge metric
    expected_rr: float = 0.0 # Expected reward/risk ratio
    
    # Context
    reason: str = ""
    regime: str = ""
    session: str = ""
    
    # Validation
    status: IntentStatus = IntentStatus.PENDING
    rejection_reason: RejectionReason = RejectionReason.NONE
    rejection_details: str = ""
    
    # Cost estimation
    estimated_cost_usd: float = 0.0
    estimated_cost_bps: float = 0.0
    breakeven_distance: float = 0.0
    
    @property
    def sl_distance(self) -> float:
        """Distance to stop loss in price."""
        if self.side == 'LONG':
            return self.entry_price - self.sl_price
        else:
            return self.sl_price - self.entry_price
    
    @property
    def tp_distance(self) -> Optional[float]:
        """Distance to take profit in price."""
        if not self.tp_price:
            return None
        if self.side == 'LONG':
            return self.tp_price - self.entry_price
        else:
            return self.entry_price - self.tp_price
    
    @property
    def risk_reward_ratio(self) -> float:
        """Actual risk/reward ratio."""
        tp_dist = self.tp_distance
        if not tp_dist or self.sl_distance <= 0:
            return 0.0
        return tp_dist / self.sl_distance
    
    def to_dict(self) -> Dict:
        return {
            'intent_id': self.intent_id,
            'created_at': self.created_at.isoformat(),
            'intent_type': self.intent_type.value,
            'symbol': self.symbol,
            'side': self.side,
            'risk_amount_usd': round(self.risk_amount_usd, 2),
            'position_size': self.position_size,
            'notional_usd': round(self.notional_usd, 2),
            'entry_price': round(self.entry_price, 2),
            'sl_price': round(self.sl_price, 2),
            'tp_price': round(self.tp_price, 2) if self.tp_price else None,
            'sl_distance': round(self.sl_distance, 2),
            'risk_reward_ratio': round(self.risk_reward_ratio, 2),
            'confidence': round(self.confidence, 3),
            'edge_score': round(self.edge_score, 3),
            'status': self.status.value,
            'rejection_reason': self.rejection_reason.value,
            'reason': self.reason,
            'estimated_cost_usd': round(self.estimated_cost_usd, 4),
            'estimated_cost_bps': round(self.estimated_cost_bps, 2),
        }


# =============================================================================
# INTENT GENERATOR
# =============================================================================

class TradeIntentGenerator:
    """
    Generates and validates trade intents.
    
    This is the bridge between strategy signals and execution.
    All trading logic should flow through intent generation.
    """
    
    def __init__(
        self,
        min_rr_ratio: float = 1.5,
        max_cost_bps: float = 30.0,  # Max 30bps in costs
        max_risk_pct: float = 1.0,   # Max 1% risk per trade
        min_confidence: float = 0.5,  # Min confidence to trade
        position_store=None,
        cost_model=None
    ):
        self.min_rr_ratio = min_rr_ratio
        self.max_cost_bps = max_cost_bps
        self.max_risk_pct = max_risk_pct
        self.min_confidence = min_confidence
        
        self._position_store = position_store or get_position_store()
        self._cost_model = cost_model or get_cost_model()
        
        # Intent history (bounded to prevent memory leaks)
        self._intent_history: List[TradeIntentRecord] = []
        self._max_intent_history = 500  # Keep last 500 intents
        self._active_intents: Dict[str, TradeIntentRecord] = {}
        
        logger.info("TradeIntentGenerator initialized")
    
    # =========================================================================
    # INTENT CREATION
    # =========================================================================
    
    def create_entry_intent(
        self,
        symbol: str,
        side: str,  # 'LONG' or 'SHORT'
        entry_price: float,
        sl_price: float,
        risk_amount: float,
        confidence: float = 0.7,
        edge_score: float = 0.0,
        tp_price: Optional[float] = None,
        reason: str = "",
        regime: str = "",
        session: str = ""
    ) -> TradeIntentRecord:
        """
        Create an entry intent.
        
        Calculates position size from risk amount and SL distance.
        Estimates costs and validates the intent.
        """
        intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        
        # Validate SL direction
        if side == 'LONG' and sl_price >= entry_price:
            return self._create_rejected_intent(
                intent_id, IntentType.ENTRY, symbol, side,
                RejectionReason.INVALID_SL,
                f"LONG SL must be below entry: {sl_price} >= {entry_price}"
            )
        
        if side == 'SHORT' and sl_price <= entry_price:
            return self._create_rejected_intent(
                intent_id, IntentType.ENTRY, symbol, side,
                RejectionReason.INVALID_SL,
                f"SHORT SL must be above entry: {sl_price} <= {entry_price}"
            )
        
        # Validate TP direction
        if tp_price:
            if side == 'LONG' and tp_price <= entry_price:
                return self._create_rejected_intent(
                    intent_id, IntentType.ENTRY, symbol, side,
                    RejectionReason.INVALID_TP,
                    f"LONG TP must be above entry: {tp_price} <= {entry_price}"
                )
            if side == 'SHORT' and tp_price >= entry_price:
                return self._create_rejected_intent(
                    intent_id, IntentType.ENTRY, symbol, side,
                    RejectionReason.INVALID_TP,
                    f"SHORT TP must be below entry: {tp_price} >= {entry_price}"
                )
        
        # Calculate SL distance and position size
        sl_distance = abs(entry_price - sl_price)
        position_size = risk_amount / sl_distance
        notional_usd = position_size * entry_price
        
        # Estimate costs
        cost_estimate = self._cost_model.estimate_costs(
            side=side,
            size_usd=notional_usd,
            entry_price=entry_price,
            is_maker_entry=True,
            is_maker_exit=False,  # SL is usually taker
        )
        
        # Calculate expected RR
        expected_rr = 0.0
        if tp_price:
            tp_distance = abs(tp_price - entry_price)
            expected_rr = tp_distance / sl_distance if sl_distance > 0 else 0
        
        intent = TradeIntentRecord(
            intent_id=intent_id,
            created_at=datetime.now(UTC),
            intent_type=IntentType.ENTRY,
            symbol=symbol,
            side=side,
            risk_amount_usd=risk_amount,
            position_size=position_size,
            notional_usd=notional_usd,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            confidence=confidence,
            edge_score=edge_score,
            expected_rr=expected_rr,
            reason=reason,
            regime=regime,
            session=session,
            estimated_cost_usd=cost_estimate.total_usd,
            estimated_cost_bps=cost_estimate.total_bps,
            breakeven_distance=self._cost_model.calculate_breakeven_distance(
                size_usd=notional_usd,
                entry_price=entry_price
            )
        )
        
        # Validate the intent
        validated_intent = self.validate_intent(intent)
        
        # Track (with bounded history)
        self._intent_history.append(validated_intent)
        if len(self._intent_history) > self._max_intent_history:
            self._intent_history = self._intent_history[-self._max_intent_history:]
        if validated_intent.status == IntentStatus.VALIDATED:
            self._active_intents[intent_id] = validated_intent
        
        return validated_intent
    
    def create_exit_intent(
        self,
        symbol: str,
        exit_price: float,
        reason: str = "",
        exit_type: str = "manual"  # 'sl', 'tp', 'manual', 'emergency'
    ) -> TradeIntentRecord:
        """Create an exit intent for existing position."""
        intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        
        # Check if position exists
        pos = self._position_store.get_position(symbol)
        if not pos or pos.is_flat:
            return self._create_rejected_intent(
                intent_id, IntentType.EXIT, symbol, 'FLAT',
                RejectionReason.NO_POSITION,
                f"No position to exit for {symbol}"
            )
        
        intent = TradeIntentRecord(
            intent_id=intent_id,
            created_at=datetime.now(UTC),
            intent_type=IntentType.EXIT,
            symbol=symbol,
            side=pos.side,
            risk_amount_usd=0,
            position_size=pos.size,
            notional_usd=pos.size * exit_price,
            entry_price=pos.entry_price,
            sl_price=0,
            tp_price=None,
            reason=f"{exit_type}: {reason}",
            status=IntentStatus.VALIDATED,  # Exits always valid
        )
        
        self._intent_history.append(intent)
        if len(self._intent_history) > self._max_intent_history:
            self._intent_history = self._intent_history[-self._max_intent_history:]
        self._active_intents[intent_id] = intent
        
        logger.info(f"Exit intent created: {intent_id} | {symbol} {pos.side} {pos.size}")
        return intent
    
    def _create_rejected_intent(
        self,
        intent_id: str,
        intent_type: IntentType,
        symbol: str,
        side: str,
        reason: RejectionReason,
        details: str
    ) -> TradeIntentRecord:
        """Create a rejected intent record."""
        intent = TradeIntentRecord(
            intent_id=intent_id,
            created_at=datetime.now(UTC),
            intent_type=intent_type,
            symbol=symbol,
            side=side,
            risk_amount_usd=0,
            position_size=0,
            notional_usd=0,
            entry_price=0,
            sl_price=0,
            status=IntentStatus.REJECTED,
            rejection_reason=reason,
            rejection_details=details
        )
        
        logger.warning(f"Intent rejected: {intent_id} | {reason.value} | {details}")
        return intent
    
    # =========================================================================
    # VALIDATION
    # =========================================================================
    
    def validate_intent(self, intent: TradeIntentRecord) -> TradeIntentRecord:
        """
        Validate a trade intent against all rules.
        
        Returns a new intent record with updated status.
        """
        if intent.status != IntentStatus.PENDING:
            return intent
        
        # Run all validation checks
        rejection_reason, details = self._run_validations(intent)
        
        if rejection_reason != RejectionReason.NONE:
            # Create rejected copy
            return TradeIntentRecord(
                intent_id=intent.intent_id,
                created_at=intent.created_at,
                intent_type=intent.intent_type,
                symbol=intent.symbol,
                side=intent.side,
                risk_amount_usd=intent.risk_amount_usd,
                position_size=intent.position_size,
                notional_usd=intent.notional_usd,
                entry_price=intent.entry_price,
                sl_price=intent.sl_price,
                tp_price=intent.tp_price,
                confidence=intent.confidence,
                edge_score=intent.edge_score,
                expected_rr=intent.expected_rr,
                reason=intent.reason,
                regime=intent.regime,
                session=intent.session,
                status=IntentStatus.REJECTED,
                rejection_reason=rejection_reason,
                rejection_details=details,
                estimated_cost_usd=intent.estimated_cost_usd,
                estimated_cost_bps=intent.estimated_cost_bps,
                breakeven_distance=intent.breakeven_distance
            )
        
        # Valid - create validated copy
        return TradeIntentRecord(
            intent_id=intent.intent_id,
            created_at=intent.created_at,
            intent_type=intent.intent_type,
            symbol=intent.symbol,
            side=intent.side,
            risk_amount_usd=intent.risk_amount_usd,
            position_size=intent.position_size,
            notional_usd=intent.notional_usd,
            entry_price=intent.entry_price,
            sl_price=intent.sl_price,
            tp_price=intent.tp_price,
            confidence=intent.confidence,
            edge_score=intent.edge_score,
            expected_rr=intent.expected_rr,
            reason=intent.reason,
            regime=intent.regime,
            session=intent.session,
            status=IntentStatus.VALIDATED,
            rejection_reason=RejectionReason.NONE,
            rejection_details="",
            estimated_cost_usd=intent.estimated_cost_usd,
            estimated_cost_bps=intent.estimated_cost_bps,
            breakeven_distance=intent.breakeven_distance
        )
    
    def _run_validations(self, intent: TradeIntentRecord) -> Tuple[RejectionReason, str]:
        """Run all validation checks on intent."""
        
        # 1. Check existing position
        if intent.intent_type == IntentType.ENTRY:
            pos = self._position_store.get_position(intent.symbol)
            if pos and not pos.is_flat:
                return RejectionReason.POSITION_EXISTS, f"Already in {pos.side} position"
        
        # 2. Check confidence threshold
        if intent.confidence < self.min_confidence:
            return RejectionReason.REGIME_MISMATCH, f"Confidence {intent.confidence:.2f} < {self.min_confidence}"
        
        # 3. Check risk/reward ratio
        if intent.tp_price and intent.expected_rr < self.min_rr_ratio:
            return RejectionReason.COST_TOO_HIGH, f"R:R {intent.expected_rr:.2f} < {self.min_rr_ratio}"
        
        # 4. Check cost vs edge
        if intent.estimated_cost_bps > self.max_cost_bps:
            return RejectionReason.COST_TOO_HIGH, f"Cost {intent.estimated_cost_bps:.1f}bps > {self.max_cost_bps}bps"
        
        # 5. Check if cost eats into profit too much
        if intent.tp_price:
            potential_profit = intent.position_size * abs(intent.tp_price - intent.entry_price)
            if intent.estimated_cost_usd > potential_profit * 0.3:  # Cost > 30% of potential profit
                return RejectionReason.COST_TOO_HIGH, f"Cost ${intent.estimated_cost_usd:.2f} > 30% of potential profit"
        
        # 6. Check breakeven viability
        sl_distance = intent.sl_distance
        if intent.breakeven_distance > sl_distance * 0.5:
            return RejectionReason.COST_TOO_HIGH, f"Breakeven {intent.breakeven_distance:.2f} > 50% of SL distance"
        
        # All checks passed
        return RejectionReason.NONE, ""
    
    # =========================================================================
    # INTENT LIFECYCLE
    # =========================================================================
    
    def mark_executing(self, intent_id: str) -> None:
        """Mark intent as being executed."""
        if intent_id in self._active_intents:
            # Note: We can't modify frozen dataclass, so we just log the state change
            logger.info(f"Intent executing: {intent_id}")
    
    def mark_completed(self, intent_id: str, actual_entry_price: float = 0.0) -> None:
        """Mark intent as completed."""
        if intent_id in self._active_intents:
            del self._active_intents[intent_id]
            logger.info(f"Intent completed: {intent_id}")
    
    def mark_cancelled(self, intent_id: str, reason: str = "") -> None:
        """Mark intent as cancelled."""
        if intent_id in self._active_intents:
            del self._active_intents[intent_id]
            logger.info(f"Intent cancelled: {intent_id} | {reason}")
    
    def mark_failed(self, intent_id: str, reason: str = "") -> None:
        """Mark intent as failed."""
        if intent_id in self._active_intents:
            del self._active_intents[intent_id]
            logger.warning(f"Intent failed: {intent_id} | {reason}")
    
    # =========================================================================
    # QUERIES
    # =========================================================================
    
    def get_active_intent(self, symbol: str) -> Optional[TradeIntentRecord]:
        """Get active intent for symbol."""
        for intent in self._active_intents.values():
            if intent.symbol == symbol:
                return intent
        return None
    
    def get_intent_history(self, last_n: int = 50) -> List[Dict]:
        """Get recent intent history."""
        return [i.to_dict() for i in self._intent_history[-last_n:]]
    
    def get_rejection_stats(self, last_n: int = 100) -> Dict:
        """Get statistics on intent rejections."""
        recent = self._intent_history[-last_n:]
        
        rejected = [i for i in recent if i.status == IntentStatus.REJECTED]
        
        if not rejected:
            return {'rejected_count': 0, 'total': len(recent)}
        
        # Count by reason
        by_reason = {}
        for intent in rejected:
            reason = intent.rejection_reason.value
            by_reason[reason] = by_reason.get(reason, 0) + 1
        
        return {
            'rejected_count': len(rejected),
            'total': len(recent),
            'rejection_rate': len(rejected) / len(recent) * 100,
            'by_reason': by_reason
        }


# =============================================================================
# SINGLETON
# =============================================================================

_trade_intent_generator: Optional[TradeIntentGenerator] = None


def get_trade_intent_generator(**kwargs) -> TradeIntentGenerator:
    """Get the global trade intent generator."""
    global _trade_intent_generator
    if _trade_intent_generator is None:
        _trade_intent_generator = TradeIntentGenerator(**kwargs)
    return _trade_intent_generator


def reset_trade_intent_generator() -> None:
    """Reset trade intent generator. Only for testing."""
    global _trade_intent_generator
    _trade_intent_generator = None
