"""
System Health Monitor - CAPA 5

Tracks system health metrics (NOT PnL):
- trades/day
- % entries by score bucket
- fills vs cancels rate
- SL hit rate by regime
- TP hit rate by funding bucket

Health degradation triggers alerts, not losses.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class HealthMetrics:
    """Current system health metrics"""
    trades_today: int = 0
    trades_this_session: int = 0
    
    # Score distribution
    score_bucket_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # e.g., {'5': 10, '6': 8, '7': 5, '8+': 2}
    
    # Fill rate
    orders_placed: int = 0
    orders_filled: int = 0
    orders_canceled: int = 0
    orders_rejected: int = 0
    
    # Exit distribution by regime
    sl_hits_by_regime: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tp_hits_by_regime: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Exit distribution by funding bucket
    sl_hits_by_funding: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tp_hits_by_funding: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Latency tracking (ONLY pure MARKET orders - not conversions)
    avg_fill_latency_ms: float = 0.0
    max_fill_latency_ms: float = 0.0
    
    # 🔥 SEPARATED: LIMIT→MARKET conversions (damage control, NOT execution quality)
    # These are INFORMATIONAL ONLY - never trigger kill switch
    limit_to_market_conversions: int = 0
    limit_to_market_avg_time_ms: float = 0.0
    
    # Integrity issues
    integrity_errors_count: int = 0
    side_mismatches: int = 0
    qty_mismatches: int = 0
    
    # Timestamp
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))


class SystemHealthMonitor:
    """
    Monitors system health independently from PnL.
    
    PHILOSOPHY:
    - Health = operational quality, not profitability
    - Degrading health = stop trading, even if "winning"
    - Good health + bad PnL = edge issue, keep collecting data
    - Bad health = system issue, stop immediately
    """
    
    def __init__(self):
        self.metrics = HealthMetrics()
        self.session_start = datetime.now(UTC)
        self.day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Thresholds for health alerts
        self.MIN_FILL_RATE = 0.7  # Alert if < 70% fills
        self.MAX_INTEGRITY_ERRORS = 3  # Alert if > 3 integrity errors
        self.MAX_LATENCY_MS = 30000  # Alert if any fill > 30 seconds
        
        # History for trend detection
        self._fill_latencies: List[float] = []  # ONLY pure MARKET orders
        self._conversion_times: List[float] = []  # LIMIT→MARKET (separate, informational)
        
        # Auto-heal by TIME, not by fills
        # Kill switch was a deadlock: required fills to heal but couldn't fill while dead
        self._last_latency_problem: Optional[datetime] = None
        self.LATENCY_HEAL_MINUTES = 5  # Auto-heal after 5 minutes without NEW problems
        
        logger.info("SystemHealthMonitor initialized")
    
    def record_trade_entry(self, side: str = 'unknown', size: float = 0.0, score: int = 0, 
                           regime: str = 'unknown', funding_bucket: str = 'normal'):
        """Record a trade entry
        
        Args:
            side: 'LONG' or 'SHORT'
            size: Position size in contracts
            score: Entry score (0-9)
            regime: Market regime
            funding_bucket: Funding rate bucket
        """
        self.metrics.trades_today += 1
        self.metrics.trades_this_session += 1
        
        # Ensure score is int for comparison
        score_int = int(score) if score is not None else 0
        
        # Score bucket
        if score_int >= 8:
            bucket = '8+'
        else:
            bucket = str(score_int)
        self.metrics.score_bucket_counts[bucket] += 1
        
        self.metrics.last_update = datetime.now(UTC)
        logger.debug(f"Health: Entry recorded | score={score} regime={regime} funding={funding_bucket}")
    
    def record_order_placed(self):
        """Record order placement"""
        self.metrics.orders_placed += 1
    
    def record_order_filled(self, latency_ms: float = 0, order_type: str = 'market', was_converted: bool = False):
        """Record successful fill
        
        Args:
            latency_ms: Time from order placement to fill
            order_type: 'market' or 'limit' - only PURE market orders affect health
            was_converted: True if LIMIT→MARKET conversion (damage control path)
        
        CRITICAL DESIGN (Jan 23, 2026):
        - LIMIT orders waiting = normal trading, IGNORED
        - LIMIT→MARKET = damage control decision, SEPARATE METRICS (informational only)
        - Pure MARKET = actual execution quality, TRACKED for health
        
        This separation prevents:
        1. Contaminating execution stats with risk decisions
        2. Training bot to shut down during volatility
        3. False positives from normal order flow
        """
        self.metrics.orders_filled += 1
        
        if latency_ms <= 0:
            return
        
        # =====================================================================
        # PATH 1: LIMIT orders (waiting for price) - IGNORED completely
        # =====================================================================
        if order_type.lower() == 'limit':
            # Normal: limit orders wait for price, this is NOT latency
            if latency_ms > 60000:
                logger.debug(f"Health: LIMIT filled after {latency_ms/1000:.0f}s (normal price wait)")
            return
        
        # =====================================================================
        # PATH 2: LIMIT→MARKET conversions - SEPARATE METRICS (never kills)
        # This is a RISK DECISION, not execution quality
        # =====================================================================
        if was_converted:
            self.metrics.limit_to_market_conversions += 1
            self._conversion_times.append(latency_ms)
            self.metrics.limit_to_market_avg_time_ms = sum(self._conversion_times) / len(self._conversion_times)
            logger.info(f"Health: LIMIT→MARKET conversion #{self.metrics.limit_to_market_conversions} "
                       f"took {latency_ms/1000:.1f}s (damage control - not affecting health score)")
            return  # NEVER affects health status
        
        # =====================================================================
        # PATH 3: Pure MARKET orders - THIS is execution quality
        # =====================================================================
        LATENCY_CAP_MS = 30000  # 30s cap for extreme API issues
        effective_latency = min(latency_ms, LATENCY_CAP_MS)
        
        if latency_ms > LATENCY_CAP_MS:
            logger.warning(f"Health: Capping extreme MARKET latency {latency_ms:.0f}ms → {LATENCY_CAP_MS}ms")
        
        self._fill_latencies.append(effective_latency)
        self.metrics.max_fill_latency_ms = max(self.metrics.max_fill_latency_ms, effective_latency)
        self.metrics.avg_fill_latency_ms = sum(self._fill_latencies) / len(self._fill_latencies)
        
        # Track if this is a problem (for time-based auto-heal)
        if effective_latency > self.MAX_LATENCY_MS:
            self._last_latency_problem = datetime.now(UTC)
            logger.warning(f"Health: High MARKET latency {effective_latency:.0f}ms recorded")
    
    def record_order_canceled(self):
        """Record order cancellation"""
        self.metrics.orders_canceled += 1
    
    def record_order_rejected(self):
        """Record order rejection"""
        self.metrics.orders_rejected += 1
    
    def record_exit(self, exit_type: str, regime: str = 'unknown', funding_bucket: str = 'normal'):
        """
        Record trade exit
        
        Args:
            exit_type: 'sl' or 'tp'
            regime: Market regime at exit
            funding_bucket: Funding bucket at exit
        """
        if exit_type.lower() == 'sl':
            self.metrics.sl_hits_by_regime[regime] += 1
            self.metrics.sl_hits_by_funding[funding_bucket] += 1
        elif exit_type.lower() == 'tp':
            self.metrics.tp_hits_by_regime[regime] += 1
            self.metrics.tp_hits_by_funding[funding_bucket] += 1
    
    def record_integrity_error(self, error_type: str = 'unknown', is_critical: bool = True):
        """Record integrity validation error
        
        Args:
            error_type: Description of the error
            is_critical: True for side mismatch (blocks trading), False for qty mismatch (partial fill)
        """
        self.metrics.integrity_errors_count += 1
        
        if 'side' in error_type.lower():
            self.metrics.side_mismatches += 1
            logger.critical(f"🚨 HEALTH ALERT: SIDE_MISMATCH detected - this is critical")
        elif 'qty' in error_type.lower():
            self.metrics.qty_mismatches += 1
            if not is_critical:
                # Partial fills are not critical - just informational
                logger.info(f"Health: Partial fill recorded | type={error_type}")
                return  # Don't log as warning for non-critical
        
        logger.warning(f"Health: Integrity error recorded | type={error_type} total={self.metrics.integrity_errors_count}")
    
    def get_fill_rate(self) -> float:
        """Get fill rate (fills / placed)"""
        if self.metrics.orders_placed == 0:
            return 1.0
        return self.metrics.orders_filled / self.metrics.orders_placed
    
    def get_health_status(self) -> Dict:
        """
        Get current health status with alerts
        
        Returns dict with:
        - is_healthy: bool
        - alerts: list of alert strings
        - metrics: current metrics
        """
        alerts = []
        is_healthy = True
        
        # Check fill rate
        fill_rate = self.get_fill_rate()
        if self.metrics.orders_placed >= 3 and fill_rate < self.MIN_FILL_RATE:
            alerts.append(f"LOW_FILL_RATE: {fill_rate:.0%} < {self.MIN_FILL_RATE:.0%}")
            is_healthy = False
        
        # Check integrity errors
        if self.metrics.integrity_errors_count > self.MAX_INTEGRITY_ERRORS:
            alerts.append(f"INTEGRITY_ERRORS: {self.metrics.integrity_errors_count} > {self.MAX_INTEGRITY_ERRORS}")
            is_healthy = False
        
        # Check latency - TIME-BASED auto-heal
        # Auto-heal by TIME, not by fills
        # Old design was a deadlock: needed fills to heal but couldn't fill while dead

        # New design:
        # - Problem detected → record timestamp
        # - If no NEW problems for LATENCY_HEAL_MINUTES → auto-healed
        # - Kill switch depends on SYSTEM state, not MARKET activity
        
        latency_is_problem = False
        if self._last_latency_problem:
            minutes_since_problem = (datetime.now(UTC) - self._last_latency_problem).total_seconds() / 60
            
            if minutes_since_problem < self.LATENCY_HEAL_MINUTES:
                # Still in problem window
                latency_is_problem = True
                alerts.append(f"HIGH_LATENCY: problem {minutes_since_problem:.1f}min ago (heals in {self.LATENCY_HEAL_MINUTES - minutes_since_problem:.1f}min)")
            else:
                # Auto-healed by time!
                old_max = self.metrics.max_fill_latency_ms
                if old_max > self.MAX_LATENCY_MS:
                    logger.info(f"Health: ✅ AUTO-HEALED by time ({self.LATENCY_HEAL_MINUTES}min without problems)")
                    self.metrics.max_fill_latency_ms = 0  # Reset
                    self._last_latency_problem = None
        
        if latency_is_problem:
            is_healthy = False
        
        # Check for side mismatches (critical) - qty mismatches are not critical
        if self.metrics.side_mismatches > 0:
            alerts.append(f"SIDE_MISMATCH: {self.metrics.side_mismatches} detected")
            is_healthy = False
            logger.critical(f"🚨 HEALTH ALERT: SIDE_MISMATCH: {self.metrics.side_mismatches} detected")
        
        return {
            'is_healthy': is_healthy,
            'alerts': alerts,
            'fill_rate': fill_rate,
            'trades_today': self.metrics.trades_today,
            'trades_session': self.metrics.trades_this_session,
            'orders_placed': self.metrics.orders_placed,
            'orders_filled': self.metrics.orders_filled,
            'avg_latency_ms': self.metrics.avg_fill_latency_ms,
            'integrity_errors': self.metrics.integrity_errors_count,
            'side_mismatches': self.metrics.side_mismatches,
            'qty_mismatches': self.metrics.qty_mismatches,
            'score_distribution': dict(self.metrics.score_bucket_counts),
            'last_update': self.metrics.last_update.isoformat()
        }
    
    def should_stop_trading(self) -> tuple[bool, str]:
        """
        CAPA 5: Kill switch based on health, not PnL
        
        Returns: (should_stop, reason)
        """
        status = self.get_health_status()
        
        if not status['is_healthy']:
            # Log all alerts
            for alert in status['alerts']:
                logger.critical(f"🚨 HEALTH ALERT: {alert}")
            
            return True, f"Health degraded: {', '.join(status['alerts'])}"
        
        return False, ""
    
    def reset_session(self):
        """Reset session metrics (called on session change)"""
        self.metrics.trades_this_session = 0
        self.session_start = datetime.now(UTC)
        logger.info("Health: Session metrics reset")
    
    def reset_daily(self):
        """Reset daily metrics (called at midnight)"""
        logger.info(f"Health: Daily reset | trades={self.metrics.trades_today} fills={self.metrics.orders_filled}/{self.metrics.orders_placed} "
                   f"conversions={self.metrics.limit_to_market_conversions}")
        
        self.metrics = HealthMetrics()
        self._fill_latencies = []
        self._conversion_times = []
        self._last_latency_problem = None
        self.day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    
    def log_summary(self):
        """Log current health summary"""
        status = self.get_health_status()
        
        health_emoji = "✅" if status['is_healthy'] else "🚨"
        
        logger.info(f"{health_emoji} HEALTH SUMMARY:")
        logger.info(f"   Trades: {status['trades_today']} today / {status['trades_session']} session")
        logger.info(f"   Fill rate: {status['fill_rate']:.0%} ({status['orders_filled']}/{status['orders_placed']})")
        logger.info(f"   Latency: {status['avg_latency_ms']:.0f}ms avg")
        logger.info(f"   Integrity: {status['integrity_errors']} errors")
        
        if status['alerts']:
            for alert in status['alerts']:
                logger.warning(f"   ⚠️ {alert}")
