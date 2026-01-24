"""
Execution Analytics - Quality Tracking for Trade Execution

This module tracks execution quality metrics:
- Slippage (expected vs actual)
- Latency (order sent to fill)
- Fill type distribution (maker/taker)
- Rejection/cancel rates

CRITICAL: In LIVE mode, this provides real data for:
- Cost model calibration
- Strategy performance attribution
- Execution algorithm tuning

USAGE:
    from src.accounting.execution_analytics import get_execution_analytics
    
    analytics = get_execution_analytics()
    
    # Start tracking an execution
    tracker = analytics.start_execution(
        order_id='pending123',
        symbol='BTC/USD:USD',
        side='buy',
        size=0.01,
        expected_price=95000.0,
        order_type='limit',
        purpose='entry'
    )
    
    # When order is placed
    analytics.record_order_sent(tracker.execution_id, exchange_order_id='abc123')
    
    # When fill arrives
    analytics.record_fill(
        tracker.execution_id,
        fill_price=95002.50,
        fill_size=0.01,
        fee=0.95,
        fill_type='MAKER'
    )
    
    # Get statistics
    stats = analytics.get_stats()
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, List, Tuple
from collections import deque
from enum import Enum
import numpy as np
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# EXECUTION RECORD
# =============================================================================

class ExecutionResult(Enum):
    """Possible outcomes of an execution attempt."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass
class ExecutionTracker:
    """
    Tracks a single execution attempt from intent to fill.
    """
    execution_id: str
    order_id: str  # Exchange order ID (set when placed)
    symbol: str
    side: str  # 'buy' or 'sell'
    size: float
    order_type: str
    purpose: str  # 'entry', 'sl', 'tp', 'exit'
    
    # Expected values (at time of decision)
    expected_price: float
    expected_spread_bps: float = 0.0
    expected_volatility: float = 0.0
    
    # Timing
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent_at: Optional[datetime] = None
    ack_at: Optional[datetime] = None  # Exchange acknowledgment
    first_fill_at: Optional[datetime] = None
    last_fill_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Actual values
    result: ExecutionResult = ExecutionResult.PENDING
    fill_price: float = 0.0  # VWAP if multiple fills
    filled_size: float = 0.0
    total_fee: float = 0.0
    fill_type: str = ""  # MAKER, TAKER, MIXED
    fill_count: int = 0
    
    # Derived metrics (calculated on completion)
    slippage_bps: float = 0.0  # Positive = worse than expected
    latency_to_ack_ms: float = 0.0
    latency_to_fill_ms: float = 0.0
    total_cost_usd: float = 0.0
    
    # Metadata
    rejection_reason: str = ""
    cancel_reason: str = ""
    
    @property
    def is_complete(self) -> bool:
        return self.result not in (ExecutionResult.PENDING, ExecutionResult.PARTIAL)
    
    def calculate_metrics(self) -> None:
        """Calculate derived metrics after completion."""
        # Slippage
        if self.expected_price > 0 and self.fill_price > 0:
            if self.side == 'buy':
                self.slippage_bps = ((self.fill_price - self.expected_price) / 
                                     self.expected_price) * 10000
            else:  # sell
                self.slippage_bps = ((self.expected_price - self.fill_price) / 
                                     self.expected_price) * 10000
        
        # Latency
        if self.sent_at and self.ack_at:
            self.latency_to_ack_ms = (self.ack_at - self.sent_at).total_seconds() * 1000
        
        if self.sent_at and self.first_fill_at:
            self.latency_to_fill_ms = (self.first_fill_at - self.sent_at).total_seconds() * 1000
        
        # Total cost
        slippage_cost = self.filled_size * self.fill_price * (self.slippage_bps / 10000)
        self.total_cost_usd = self.total_fee + max(0, slippage_cost)
    
    def to_dict(self) -> Dict:
        return {
            'execution_id': self.execution_id,
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side,
            'size': self.size,
            'order_type': self.order_type,
            'purpose': self.purpose,
            'expected_price': round(self.expected_price, 2),
            'fill_price': round(self.fill_price, 2),
            'filled_size': self.filled_size,
            'result': self.result.value,
            'slippage_bps': round(self.slippage_bps, 2),
            'latency_to_ack_ms': round(self.latency_to_ack_ms, 2),
            'latency_to_fill_ms': round(self.latency_to_fill_ms, 2),
            'total_fee': round(self.total_fee, 4),
            'total_cost_usd': round(self.total_cost_usd, 4),
            'fill_type': self.fill_type,
            'fill_count': self.fill_count,
            'created_at': self.created_at.isoformat(),
        }


# =============================================================================
# EXECUTION ANALYTICS
# =============================================================================

class ExecutionAnalytics:
    """
    Tracks and analyzes execution quality across all trades.
    
    Provides:
    - Real-time execution tracking
    - Aggregate statistics
    - Cost model calibration data
    - Performance attribution
    """
    
    def __init__(self, max_history: int = 1000):
        self._active_executions: Dict[str, ExecutionTracker] = {}
        self._completed_executions: deque = deque(maxlen=max_history)
        
        # Aggregate counters
        self._total_executions = 0
        self._total_filled = 0
        self._total_rejected = 0
        self._total_canceled = 0
        self._total_timed_out = 0
        
        # Aggregate metrics
        self._total_volume_usd = 0.0
        self._total_fees_paid = 0.0
        self._total_slippage_usd = 0.0
        
        # By purpose tracking
        self._by_purpose: Dict[str, Dict] = {}
        
        logger.info("ExecutionAnalytics initialized")
    
    # =========================================================================
    # EXECUTION TRACKING
    # =========================================================================
    
    def start_execution(
        self,
        order_id: str,
        symbol: str,
        side: str,
        size: float,
        expected_price: float,
        order_type: str = 'limit',
        purpose: str = 'entry',
        expected_spread_bps: float = 0.0,
        expected_volatility: float = 0.0
    ) -> ExecutionTracker:
        """
        Start tracking a new execution.
        
        Call this when creating an order intent, BEFORE sending to exchange.
        """
        import uuid
        execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        
        tracker = ExecutionTracker(
            execution_id=execution_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            purpose=purpose,
            expected_price=expected_price,
            expected_spread_bps=expected_spread_bps,
            expected_volatility=expected_volatility,
        )
        
        self._active_executions[execution_id] = tracker
        self._total_executions += 1
        
        logger.debug(f"Execution started: {execution_id} | {symbol} {side} {size}")
        return tracker
    
    def record_order_sent(
        self,
        execution_id: str,
        exchange_order_id: str | None = None
    ) -> None:
        """Record that order was sent to exchange."""
        tracker = self._active_executions.get(execution_id)
        if not tracker:
            logger.warning(f"Unknown execution: {execution_id}")
            return
        
        tracker.sent_at = datetime.now(UTC)
        if exchange_order_id:
            tracker.order_id = exchange_order_id
    
    def record_order_ack(self, execution_id: str) -> None:
        """Record that exchange acknowledged the order."""
        tracker = self._active_executions.get(execution_id)
        if tracker:
            tracker.ack_at = datetime.now(UTC)
    
    def record_fill(
        self,
        execution_id: str,
        fill_price: float,
        fill_size: float,
        fee: float = 0.0,
        fill_type: str = 'UNKNOWN'
    ) -> None:
        """
        Record a fill for an execution.
        
        Can be called multiple times for partial fills.
        """
        tracker = self._active_executions.get(execution_id)
        if not tracker:
            logger.warning(f"Fill for unknown execution: {execution_id}")
            return
        
        now = datetime.now(UTC)
        
        if tracker.fill_count == 0:
            tracker.first_fill_at = now
        tracker.last_fill_at = now
        
        # Update VWAP
        if tracker.filled_size > 0:
            total_value = tracker.fill_price * tracker.filled_size + fill_price * fill_size
            tracker.filled_size += fill_size
            tracker.fill_price = total_value / tracker.filled_size
        else:
            tracker.fill_price = fill_price
            tracker.filled_size = fill_size
        
        tracker.total_fee += fee
        tracker.fill_count += 1
        
        # Update fill type
        if tracker.fill_type == "":
            tracker.fill_type = fill_type
        elif tracker.fill_type != fill_type:
            tracker.fill_type = "MIXED"
        
        # Check if complete
        if tracker.filled_size >= tracker.size - 1e-9:
            self._complete_execution(tracker, ExecutionResult.FILLED)
        else:
            tracker.result = ExecutionResult.PARTIAL
        
        logger.debug(
            f"Fill recorded: {execution_id} | {fill_size}@{fill_price} | "
            f"total={tracker.filled_size}/{tracker.size}"
        )
    
    def record_rejection(self, execution_id: str, reason: str = "") -> None:
        """Record that order was rejected."""
        tracker = self._active_executions.get(execution_id)
        if tracker:
            tracker.rejection_reason = reason
            self._complete_execution(tracker, ExecutionResult.REJECTED)
            self._total_rejected += 1
            logger.warning(f"Execution rejected: {execution_id} | {reason}")
    
    def record_cancel(self, execution_id: str, reason: str = "") -> None:
        """Record that order was canceled."""
        tracker = self._active_executions.get(execution_id)
        if tracker:
            tracker.cancel_reason = reason
            self._complete_execution(tracker, ExecutionResult.CANCELED)
            self._total_canceled += 1
            logger.debug(f"Execution canceled: {execution_id} | {reason}")
    
    def record_timeout(self, execution_id: str) -> None:
        """Record that order timed out."""
        tracker = self._active_executions.get(execution_id)
        if tracker:
            self._complete_execution(tracker, ExecutionResult.TIMEOUT)
            self._total_timed_out += 1
            logger.warning(f"Execution timeout: {execution_id}")
    
    def _complete_execution(
        self,
        tracker: ExecutionTracker,
        result: ExecutionResult
    ) -> None:
        """Complete an execution and move to history."""
        tracker.result = result
        tracker.completed_at = datetime.now(UTC)
        tracker.calculate_metrics()
        
        # Update aggregates
        if result == ExecutionResult.FILLED:
            self._total_filled += 1
            self._total_volume_usd += tracker.filled_size * tracker.fill_price
            self._total_fees_paid += tracker.total_fee
            
            slippage_cost = (tracker.filled_size * tracker.fill_price * 
                           tracker.slippage_bps / 10000)
            self._total_slippage_usd += max(0, slippage_cost)
        
        # Track by purpose
        purpose = tracker.purpose
        if purpose not in self._by_purpose:
            self._by_purpose[purpose] = {
                'count': 0, 'filled': 0, 'total_slippage_bps': 0,
                'total_latency_ms': 0, 'maker_count': 0, 'taker_count': 0
            }
        
        stats = self._by_purpose[purpose]
        stats['count'] += 1
        
        if result == ExecutionResult.FILLED:
            stats['filled'] += 1
            stats['total_slippage_bps'] += tracker.slippage_bps
            stats['total_latency_ms'] += tracker.latency_to_fill_ms
            
            if tracker.fill_type == 'MAKER':
                stats['maker_count'] += 1
            elif tracker.fill_type == 'TAKER':
                stats['taker_count'] += 1
        
        # Move to history
        self._completed_executions.append(tracker)
        self._active_executions.pop(tracker.execution_id, None)
        
        logger.info(
            f"📊 Execution complete: {tracker.execution_id} | "
            f"result={result.value} | slip={tracker.slippage_bps:.1f}bps | "
            f"latency={tracker.latency_to_fill_ms:.0f}ms"
        )
    
    # =========================================================================
    # STATISTICS
    # =========================================================================
    
    def get_stats(self, last_n: int = 100) -> Dict:
        """
        Get comprehensive execution statistics.
        
        Returns metrics useful for:
        - Cost model calibration
        - Performance analysis
        - Execution improvement
        """
        recent = list(self._completed_executions)[-last_n:]
        filled = [e for e in recent if e.result == ExecutionResult.FILLED]
        
        if not filled:
            return {
                'total_executions': self._total_executions,
                'sample_size': 0,
                'message': 'No filled executions to analyze'
            }
        
        # Slippage stats
        slippages = [e.slippage_bps for e in filled]
        entry_slippages = [e.slippage_bps for e in filled if e.purpose == 'entry']
        exit_slippages = [e.slippage_bps for e in filled if e.purpose in ('sl', 'tp', 'exit')]
        
        # Latency stats
        latencies = [e.latency_to_fill_ms for e in filled if e.latency_to_fill_ms > 0]
        
        # Fill type distribution
        makers = sum(1 for e in filled if e.fill_type == 'MAKER')
        takers = sum(1 for e in filled if e.fill_type == 'TAKER')
        
        return {
            'total_executions': self._total_executions,
            'sample_size': len(recent),
            'filled_count': len(filled),
            'fill_rate_pct': len(filled) / len(recent) * 100 if recent else 0,
            
            'slippage': {
                'mean_bps': np.mean(slippages) if slippages else 0,
                'std_bps': np.std(slippages) if len(slippages) > 1 else 0,
                'p50_bps': np.percentile(slippages, 50) if slippages else 0,
                'p90_bps': np.percentile(slippages, 90) if slippages else 0,
                'p99_bps': np.percentile(slippages, 99) if slippages else 0,
                'entry_mean_bps': np.mean(entry_slippages) if entry_slippages else 0,
                'exit_mean_bps': np.mean(exit_slippages) if exit_slippages else 0,
            },
            
            'latency_ms': {
                'mean': np.mean(latencies) if latencies else 0,
                'p50': np.percentile(latencies, 50) if latencies else 0,
                'p90': np.percentile(latencies, 90) if latencies else 0,
                'p99': np.percentile(latencies, 99) if latencies else 0,
            },
            
            'fill_types': {
                'maker_pct': makers / len(filled) * 100 if filled else 0,
                'taker_pct': takers / len(filled) * 100 if filled else 0,
            },
            
            'costs': {
                'total_volume_usd': self._total_volume_usd,
                'total_fees_usd': self._total_fees_paid,
                'total_slippage_usd': self._total_slippage_usd,
                'avg_fee_per_trade': self._total_fees_paid / len(filled) if filled else 0,
            },
            
            'failures': {
                'rejected': self._total_rejected,
                'canceled': self._total_canceled,
                'timed_out': self._total_timed_out,
            },
            
            'by_purpose': self._by_purpose,
        }
    
    def get_slippage_by_conditions(self, last_n: int = 200) -> Dict:
        """
        Analyze slippage by different market conditions.
        
        Useful for improving execution algorithms.
        """
        recent = list(self._completed_executions)[-last_n:]
        filled = [e for e in recent if e.result == ExecutionResult.FILLED]
        
        if not filled:
            return {'message': 'No data'}
        
        # Group by order type
        by_order_type: Dict[str, List[float]] = {}
        for e in filled:
            ot = e.order_type
            if ot not in by_order_type:
                by_order_type[ot] = []
            by_order_type[ot].append(e.slippage_bps)
        
        # Group by volatility bucket (if tracked)
        by_volatility: Dict[str, List[float]] = {'low': [], 'medium': [], 'high': []}
        for e in filled:
            if e.expected_volatility <= 0:
                continue
            if e.expected_volatility < 30:
                by_volatility['low'].append(e.slippage_bps)
            elif e.expected_volatility < 70:
                by_volatility['medium'].append(e.slippage_bps)
            else:
                by_volatility['high'].append(e.slippage_bps)
        
        return {
            'by_order_type': {
                k: {'mean': np.mean(v), 'count': len(v)}
                for k, v in by_order_type.items()
            },
            'by_volatility': {
                k: {'mean': np.mean(v) if v else 0, 'count': len(v)}
                for k, v in by_volatility.items()
            }
        }
    
    def get_recent_executions(self, n: int = 20) -> List[Dict]:
        """Get recent executions for debugging."""
        return [e.to_dict() for e in list(self._completed_executions)[-n:]]
    
    def get_active_executions(self) -> List[Dict]:
        """Get currently active executions."""
        return [e.to_dict() for e in self._active_executions.values()]
    
    # =========================================================================
    # CALIBRATION DATA
    # =========================================================================
    
    def get_calibration_data(self, last_n: int = 100) -> Dict:
        """
        Get data for cost model calibration.
        
        Returns empirical values to update the cost model.
        """
        recent = list(self._completed_executions)[-last_n:]
        filled = [e for e in recent if e.result == ExecutionResult.FILLED]
        
        if len(filled) < 10:
            return {'sufficient_data': False, 'sample_size': len(filled)}
        
        # Slippage calibration
        slippages = [e.slippage_bps for e in filled]
        entry_slippages = [e.slippage_bps for e in filled if e.purpose == 'entry']
        sl_slippages = [e.slippage_bps for e in filled if e.purpose == 'sl']
        
        # Latency
        latencies = [e.latency_to_fill_ms for e in filled if e.latency_to_fill_ms > 0]
        
        return {
            'sufficient_data': True,
            'sample_size': len(filled),
            'slippage': {
                'overall_mean_bps': np.mean(slippages),
                'overall_std_bps': np.std(slippages),
                'entry_mean_bps': np.mean(entry_slippages) if entry_slippages else 0,
                'sl_mean_bps': np.mean(sl_slippages) if sl_slippages else 0,
            },
            'latency_ms': {
                'mean': np.mean(latencies) if latencies else 0,
                'p95': np.percentile(latencies, 95) if latencies else 0,
            },
            'maker_rate': sum(1 for e in filled if e.fill_type == 'MAKER') / len(filled),
        }


# =============================================================================
# SINGLETON
# =============================================================================

_execution_analytics: Optional[ExecutionAnalytics] = None


def get_execution_analytics() -> ExecutionAnalytics:
    """Get the global execution analytics instance."""
    global _execution_analytics
    if _execution_analytics is None:
        _execution_analytics = ExecutionAnalytics()
    return _execution_analytics


def reset_execution_analytics() -> None:
    """Reset execution analytics. Only for testing."""
    global _execution_analytics
    _execution_analytics = None
