"""
Missed Opportunity Tracker - Track trades we didn't take

"You need to know: How many good trades did I NOT take?"

This module logs every blocked entry with:
- Hypothetical entry price
- Block reason (filter, HARD_LOCK, etc.)
- What happened to price after (missed edge calculation)

Purpose:
- Tune filters by seeing their false positive rate
- Detect if system is too conservative
- Quantify opportunity cost of safety measures
"""

import logging
from datetime import datetime, UTC, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from collections import deque
import json

logger = logging.getLogger(__name__)

# Module-level constants for trade classification
# Minimum R threshold to consider a blocked trade as a "missed winner"
# A trade that only reaches 0.5R MFE before hitting 0.4R MAE is marginal at best
MIN_R_FOR_WINNER = 1.0  # Must reach at least 1R favorable excursion


@dataclass
class MissedOpportunity:
    """A trade signal that was blocked"""
    timestamp: datetime
    signal_direction: str  # 'LONG' or 'SHORT'
    hypothetical_entry: float
    block_reason: str  # What filter/condition blocked it
    block_category: str  # Category: 'filter', 'hard_lock', 'penalty', 'funding', 'session'
    signal_score: float = 0.0  # The score that would have triggered
    atr_at_signal: float = 0.0  # ATR for R calculation
    
    # Filled post-hoc
    price_5min_later: Optional[float] = None
    price_15min_later: Optional[float] = None
    price_1h_later: Optional[float] = None
    max_favorable_excursion: float = 0.0  # MFE in price
    max_adverse_excursion: float = 0.0  # MAE in price
    hypothetical_r: float = 0.0  # If we had taken it, what R would we have gotten?
    resolved: bool = False  # True after post-hoc analysis done
    
    @property
    def was_good_trade(self) -> bool:
        """
        Would this have been a winning trade?
        
        Criteria:
        1. MFE > MAE (favorable excursion exceeds adverse)
        2. MFE >= MIN_R_FOR_WINNER * ATR (must reach meaningful profit)
        
        A trade with MFE=10 MAE=9 is technically MFE>MAE but marginal.
        We require at least MIN_R_FOR_WINNER (1R) favorable excursion.
        """
        if not self.resolved:
            return False
        
        # Basic check: MFE must exceed MAE
        if self.max_favorable_excursion <= self.max_adverse_excursion:
            return False
        
        # Must reach at least MIN_R_FOR_WINNER in favorable direction
        if self.atr_at_signal > 0:
            mfe_r = self.max_favorable_excursion / self.atr_at_signal
            if mfe_r < MIN_R_FOR_WINNER:
                return False
        
        return True
    
    @property
    def hypothetical_r_at_mfe(self) -> float:
        """What R would we have reached at MFE?"""
        if self.atr_at_signal <= 0:
            return 0.0
        return self.max_favorable_excursion / self.atr_at_signal
    
    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'direction': self.signal_direction,
            'entry': self.hypothetical_entry,
            'block_reason': self.block_reason,
            'block_category': self.block_category,
            'score': self.signal_score,
            'atr': self.atr_at_signal,
            'price_5m': self.price_5min_later,
            'price_15m': self.price_15min_later,
            'price_1h': self.price_1h_later,
            'mfe': self.max_favorable_excursion,
            'mae': self.max_adverse_excursion,
            'hypothetical_r': self.hypothetical_r,
            'was_good': self.was_good_trade,
            'resolved': self.resolved
        }


class MissedOpportunityTracker:
    """
    Track missed trading opportunities for filter tuning.
    
    Usage:
        tracker = MissedOpportunityTracker()
        
        # When a signal is blocked:
        tracker.log_blocked_signal(
            direction='LONG',
            price=98000.0,
            block_reason='Consecutive losses penalty',
            block_category='penalty',
            score=8.5,
            atr=150.0
        )
        
        # On every tick, update with new price:
        tracker.update_pending(current_price=98500.0)
        
        # Get statistics:
        stats = tracker.get_statistics()
    """
    
    # How long to track a missed opportunity
    TRACKING_DURATION_MINUTES = 60
    
    # Maximum pending opportunities to track
    MAX_PENDING = 100
    
    def __init__(self, persist_path: Optional[str] = None):
        """
        Initialize tracker.
        
        Args:
            persist_path: Optional path to persist completed opportunities
        """
        self._pending: deque[MissedOpportunity] = deque(maxlen=self.MAX_PENDING)
        self._completed: List[MissedOpportunity] = []
        self._persist_path = persist_path
        
        # Statistics
        self._total_blocked = 0
        self._good_trades_missed = 0
        self._bad_trades_avoided = 0
        
        # Block reason statistics
        self._block_counts: Dict[str, int] = {}
        self._block_good_missed: Dict[str, int] = {}
        
        logger.info("MissedOpportunityTracker initialized")
    
    def log_blocked_signal(
        self,
        direction: str | None,
        price: float,
        block_reason: str,
        block_category: str,
        score: float = 0.0,
        atr: float = 0.0
    ):
        """
        Log a signal that was blocked.
        
        Args:
            direction: 'LONG' or 'SHORT' (or None if unknown)
            price: Current price (hypothetical entry)
            block_reason: Human-readable reason for block
            block_category: Category (filter, hard_lock, penalty, funding, session)
            score: The signal score
            atr: Current ATR for R calculations
        """
        opp = MissedOpportunity(
            timestamp=datetime.now(UTC),
            signal_direction=(direction or 'UNKNOWN').upper(),
            hypothetical_entry=price,
            block_reason=block_reason,
            block_category=block_category,
            signal_score=score,
            atr_at_signal=atr
        )
        
        self._pending.append(opp)
        self._total_blocked += 1
        
        # Track by reason
        self._block_counts[block_reason] = self._block_counts.get(block_reason, 0) + 1
        
        logger.info(f"📊 MISSED: {direction} @ {price:.2f} blocked by {block_category}: {block_reason}")
    
    def update_pending(self, current_price: float, current_time: datetime | None = None):
        """
        Update all pending opportunities with new price.
        
        Should be called on every tick to track price movement.
        """
        if current_time is None:
            current_time = datetime.now(UTC)
        
        resolved_indices = []
        
        for i, opp in enumerate(self._pending):
            if opp.resolved:
                continue
            
            age_minutes = (current_time - opp.timestamp).total_seconds() / 60.0
            
            # Update MFE/MAE
            if opp.signal_direction == 'LONG':
                excursion = current_price - opp.hypothetical_entry
            else:
                excursion = opp.hypothetical_entry - current_price
            
            if excursion > 0:
                opp.max_favorable_excursion = max(opp.max_favorable_excursion, excursion)
            else:
                opp.max_adverse_excursion = max(opp.max_adverse_excursion, abs(excursion))
            
            # Record price at specific intervals
            if opp.price_5min_later is None and age_minutes >= 5:
                opp.price_5min_later = current_price
                
            if opp.price_15min_later is None and age_minutes >= 15:
                opp.price_15min_later = current_price
                
            if opp.price_1h_later is None and age_minutes >= 60:
                opp.price_1h_later = current_price
            
            # Resolve after tracking duration
            if age_minutes >= self.TRACKING_DURATION_MINUTES:
                self._resolve_opportunity(opp)
                resolved_indices.append(i)
        
        # Move resolved to completed and REMOVE from pending
        # We must iterate in reverse to avoid index shifting issues
        for i in sorted(resolved_indices, reverse=True):
            if i < len(self._pending):
                completed = self._pending[i]
                self._completed.append(completed)
        
        # Remove resolved items from deque (rebuild without them)
        if resolved_indices:
            resolved_set = set(resolved_indices)
            # Create new deque without resolved items
            new_pending = deque(maxlen=self.MAX_PENDING)
            for i, opp in enumerate(self._pending):
                if i not in resolved_set:
                    new_pending.append(opp)
            self._pending = new_pending
    
    def _resolve_opportunity(self, opp: MissedOpportunity):
        """Finalize tracking for an opportunity"""
        opp.resolved = True
        
        # Calculate hypothetical R
        if opp.atr_at_signal > 0:
            # Use MFE as potential profit (optimistic)
            # This shows what we COULD have gotten
            opp.hypothetical_r = opp.max_favorable_excursion / opp.atr_at_signal
        
        # Format timestamp for clarity (show when signal was originally blocked)
        signal_time_str = opp.timestamp.strftime('%H:%M:%S')
        
        # Calculate best/worst prices reached for context
        if opp.signal_direction == 'SHORT':
            best_price = opp.hypothetical_entry - opp.max_favorable_excursion
            worst_price = opp.hypothetical_entry + opp.max_adverse_excursion
        else:  # LONG
            best_price = opp.hypothetical_entry + opp.max_favorable_excursion
            worst_price = opp.hypothetical_entry - opp.max_adverse_excursion
        
        # Update statistics
        if opp.was_good_trade:
            self._good_trades_missed += 1
            # Track by reason
            self._block_good_missed[opp.block_reason] = \
                self._block_good_missed.get(opp.block_reason, 0) + 1
            logger.warning(
                f"❌ MISSED WINNER [{signal_time_str}]: {opp.signal_direction} entry={opp.hypothetical_entry:.2f} "
                f"→ best={best_price:.2f} (MFE {opp.hypothetical_r_at_mfe:.2f}R) worst={worst_price:.2f} "
                f"| Blocked by: {opp.block_reason}"
            )
        else:
            self._bad_trades_avoided += 1
            logger.info(
                f"✅ DODGED LOSER [{signal_time_str}]: {opp.signal_direction} entry={opp.hypothetical_entry:.2f} "
                f"→ best={best_price:.2f} worst={worst_price:.2f} (MAE > MFE) "
                f"| Blocked by: {opp.block_reason}"
            )
        
        # Persist if configured
        if self._persist_path:
            self._persist_opportunity(opp)
    
    def _persist_opportunity(self, opp: MissedOpportunity):
        """Append opportunity to file"""
        if self._persist_path is None:
            return
        try:
            with open(self._persist_path, 'a') as f:
                f.write(json.dumps(opp.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to persist missed opportunity: {e}")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive missed opportunity statistics"""
        total_resolved = self._good_trades_missed + self._bad_trades_avoided
        
        # Calculate filter effectiveness
        filter_stats = {}
        for reason in self._block_counts:
            total = self._block_counts[reason]
            good_missed = self._block_good_missed.get(reason, 0)
            bad_avoided = total - good_missed  # This is approximate
            
            filter_stats[reason] = {
                'total_blocks': total,
                'good_trades_missed': good_missed,
                'false_positive_rate': good_missed / total if total > 0 else 0.0
            }
        
        return {
            'total_blocked': self._total_blocked,
            'pending_tracking': len(self._pending),
            'resolved': total_resolved,
            'good_trades_missed': self._good_trades_missed,
            'bad_trades_avoided': self._bad_trades_avoided,
            'miss_rate': self._good_trades_missed / total_resolved if total_resolved > 0 else 0.0,
            'filter_effectiveness': filter_stats
        }
    
    def get_worst_filters(self, top_n: int = 5) -> List[Dict]:
        """
        Get filters with highest false positive rate (blocking good trades).
        
        Use this to tune filters that are too aggressive.
        """
        filter_stats = []
        
        for reason in self._block_counts:
            total = self._block_counts[reason]
            good_missed = self._block_good_missed.get(reason, 0)
            
            if total >= 5:  # Need minimum sample
                filter_stats.append({
                    'filter': reason,
                    'total_blocks': total,
                    'good_missed': good_missed,
                    'false_positive_rate': good_missed / total
                })
        
        # Sort by false positive rate (highest first)
        filter_stats.sort(key=lambda x: x['false_positive_rate'], reverse=True)
        
        return filter_stats[:top_n]
    
    def log_summary(self):
        """Log a summary of missed opportunities"""
        stats = self.get_statistics()
        
        logger.info("=" * 50)
        logger.info("📊 MISSED OPPORTUNITY SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Total signals blocked: {stats['total_blocked']}")
        logger.info(f"Currently tracking: {stats['pending_tracking']}")
        logger.info(f"Resolved: {stats['resolved']}")
        logger.info(f"  ❌ Good trades missed: {stats['good_trades_missed']}")
        logger.info(f"  ✅ Bad trades avoided: {stats['bad_trades_avoided']}")
        logger.info(f"Miss rate: {stats['miss_rate']:.1%}")
        
        # Show worst filters
        worst = self.get_worst_filters(3)
        if worst:
            logger.info("\n⚠️ FILTERS WITH HIGHEST FALSE POSITIVE RATE:")
            for f in worst:
                logger.info(f"  {f['filter']}: {f['false_positive_rate']:.0%} ({f['good_missed']}/{f['total_blocks']} good trades blocked)")
