"""
Exchange Reconciler - Single Source of Truth for Position State

The reconciler is the ONLY component that can change the trading state.
All other subsystems (funding, penalty, exits, R-tracking) LISTEN to state
but NEVER write to it.

Architecture:
    Exchange → Reconciler → StateMachine → Everything else

Key principles:
1. Position exists ONLY if exchange returns an open position
2. HARD_LOCK state prevents premature reactions during async gaps
3. State is DERIVED from exchange, not inferred from order flow
"""

import asyncio
import logging
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ReconcilerState(Enum):
    """
    INTERNAL states as seen by the reconciler.
    
    ⚠️ ORDER_PENDING and HARD_LOCK are INTERNAL ONLY.
    External subsystems should use get_public_state() which only returns:
    - PublicState.IDLE (no confirmed position)
    - PublicState.POSITION_OPEN (confirmed position on exchange)
    """
    IDLE = "IDLE"                    # No orders, no position
    ORDER_PENDING = "ORDER_PENDING"  # Order exists but not filled (INTERNAL)
    HARD_LOCK = "HARD_LOCK"          # Transition period (INTERNAL)
    POSITION_OPEN = "POSITION_OPEN"  # Confirmed position on exchange


class PublicState(Enum):
    """
    PUBLIC states visible to subsystems.
    
    The reconciler exposes ONLY these two states externally.
    This prevents subsystems from inferring intent from ORDER_PENDING
    and acting on anticipated rather than confirmed state.
    """
    IDLE = "IDLE"                    # No confirmed position (safe to enter)
    POSITION_OPEN = "POSITION_OPEN"  # Confirmed position (can manage/exit)


class HardLockEvent(Enum):
    """
    Events that trigger HARD_LOCK.
    
    Each event type has specific conditions for release:
    - ORDER_PLACED: Released when position confirms or order disappears
    - CANCEL_SENT: Released when cancel confirms
    - SL_TP_SET: Released when SL/TP confirmed on exchange
    - EXIT_PENDING: Released when position_size = 0 confirmed
    - PARTIAL_CLOSE: Released when position size changes (not necessarily to 0)
    """
    ORDER_PLACED = "ORDER_PLACED"    # Entry order placed
    CANCEL_SENT = "CANCEL_SENT"      # Cancel request sent
    SL_TP_SET = "SL_TP_SET"          # SL/TP being set
    EXIT_PENDING = "EXIT_PENDING"    # Exit in progress - CRITICAL
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # Partial close - releases when size changes
    MANUAL = "MANUAL"                # Manual lock (fallback)
    

@dataclass
class ExchangeSnapshot:
    """Immutable snapshot of exchange state at a point in time"""
    timestamp: datetime
    has_position: bool
    position_side: Optional[str] = None  # 'long' or 'short'
    position_size: float = 0.0
    position_entry_price: float = 0.0
    position_unrealized_pnl: float = 0.0
    open_orders: list = field(default_factory=list)
    pending_entry_orders: list = field(default_factory=list)  # Non-SL/TP orders
    
    @property
    def has_pending_entry(self) -> bool:
        return len(self.pending_entry_orders) > 0


class ExchangeReconciler:
    """
    Exchange-driven state machine reconciler.
    
    This is the SINGLE SOURCE OF TRUTH for position state.
    
    The reconciler:
    1. Polls exchange for positions and orders
    2. Derives the correct state
    3. Enforces HARD_LOCK during transitions
    4. Broadcasts state changes to listeners
    
    NO OTHER COMPONENT should directly change trading state.
    """
    
    # Configuration
    POLL_INTERVAL_MS = 500  # Poll exchange every 500ms in live
    # Increased from 1.5s to 15s to give exchange time to respond
    # The 1.5s timeout was too short and caused premature lock releases
    HARD_LOCK_DURATION_MS = 15000  # 15 seconds of protection (was 1.5s)
    BACKTEST_POLL_INTERVAL_MS = 0  # Instant in backtest
    
    def __init__(self, execution_engine, backtest_mode: bool = False):
        """
        Initialize the reconciler.
        
        Args:
            execution_engine: The execution engine with exchange access
            backtest_mode: If True, skip actual exchange calls
        """
        self.execution = execution_engine
        self.backtest_mode = backtest_mode
        
        # Current derived state
        self._state = ReconcilerState.IDLE
        self._previous_state = ReconcilerState.IDLE
        
        # Last confirmed exchange snapshot
        self._last_snapshot: Optional[ExchangeSnapshot] = None
        self._last_poll_time: Optional[datetime] = None
        
        # HARD_LOCK tracking - now EVENT-BASED
        self._hard_lock_until: Optional[datetime] = None
        self._hard_lock_reason: str = ""
        self._hard_lock_event: Optional[HardLockEvent] = None
        self._hard_lock_started: Optional[datetime] = None
        
        # Position tracking (exchange-confirmed only)
        self._confirmed_position_id: Optional[str] = None
        self._confirmed_position_side: Optional[str] = None
        self._confirmed_position_size: float = 0.0
        self._confirmed_entry_price: float = 0.0
        
        # Order tracking
        self._tracked_order_id: Optional[str] = None
        self._order_placed_time: Optional[datetime] = None
        
        # State change callbacks
        self._state_listeners: list = []
        
        # Statistics
        self._reconcile_count = 0
        self._state_changes = 0
        self._hard_locks_triggered = 0
        
        # Exchange availability tracking
        self._consecutive_failures = 0
        self._last_successful_poll: Optional[datetime] = None
        self._exchange_available = True
        self._degraded_mode = False
        # Thresholds for degradation
        self.MAX_CONSECUTIVE_FAILURES = 3  # Enter degraded mode after 3 failures
        self.STALE_DATA_THRESHOLD_SEC = 10  # Data older than 10s = stale
        
        # Symbol to track
        self._symbol: Optional[str] = None
        
    def set_symbol(self, symbol: str):
        """Set the symbol to track"""
        self._symbol = symbol
        
    def register_listener(self, callback):
        """
        Register a callback to be notified of state changes.
        
        Callback signature: callback(new_state: ReconcilerState, snapshot: ExchangeSnapshot)
        """
        self._state_listeners.append(callback)
        
    @property
    def state(self) -> ReconcilerState:
        """Current reconciler state (INTERNAL - prefer get_public_state())"""
        return self._state
    
    def get_public_state(self) -> PublicState:
        """
        Get PUBLIC state for external subsystems.
        
        ⚠️ THIS IS THE ONLY METHOD SUBSYSTEMS SHOULD USE TO CHECK STATE.
        
        Returns ONLY:
        - PublicState.IDLE: No confirmed position (can try to enter)
        - PublicState.POSITION_OPEN: Position confirmed on exchange (can manage)
        
        ORDER_PENDING and HARD_LOCK are NEVER exposed.
        This prevents subsystems from acting on anticipated state.
        """
        if self._state == ReconcilerState.POSITION_OPEN:
            return PublicState.POSITION_OPEN
        else:
            # IDLE, ORDER_PENDING, HARD_LOCK → all appear as IDLE
            return PublicState.IDLE
    
    def get_snapshot(self) -> Optional['ExchangeSnapshot']:
        """
        Get the last known exchange snapshot.
        
        🔥 OPTIMIZATION (Jan 20, 2026): Use this instead of making new API calls.
        The snapshot is updated every reconcile cycle (every 5 seconds).
        
        Returns:
            ExchangeSnapshot with position/order state, or None if not yet polled
        """
        return self._last_snapshot
    
    @property
    def is_hard_locked(self) -> bool:
        """
        Check if we're in HARD_LOCK state.
        
        HARD_LOCK can end by:
        1. Event-based: Position confirms, order disappears
        2. Time-based: Fallback timeout (prevents forever locks)
        
        PRIORITY: Timeout is checked FIRST to ensure we never get stuck.
        
        This property now ONLY checks state without side effects.
        The actual release happens in _check_and_release_hard_lock() which should
        be called once per reconcile cycle, not on every property access.
        """
        if self._hard_lock_event is None:
            return False
        
        # Check if we SHOULD release (without actually releasing)
        # Timeout check
        if self._hard_lock_until is not None:
            now = datetime.now(UTC)
            if now >= self._hard_lock_until:
                return False  # Will be released on next reconcile
            
        # Event-based check
        if self._should_release_hard_lock():
            return False  # Will be released on next reconcile
        
        return True
    
    def _check_and_release_hard_lock(self) -> bool:
        """
        Check if HARD_LOCK should be released and release it if so.
        
        This method has side effects and should be called
        ONCE per reconcile cycle, not from property accessors.
        
        Returns: True if lock was released, False otherwise
        """
        if self._hard_lock_event is None:
            return False
        
        # Time-based fallback - CHECK FIRST (prevents infinite locks)
        if self._hard_lock_until is not None:
            now = datetime.now(UTC)
            if now >= self._hard_lock_until:
                elapsed_ms = (now - self._hard_lock_started).total_seconds() * 1000 if self._hard_lock_started else 0
                logger.warning(f"⏰ HARD_LOCK timeout after {elapsed_ms:.0f}ms (event={self._hard_lock_event.value if self._hard_lock_event else 'None'})")
                self._release_hard_lock("Timeout expired")
                return True
            
        # Event-based release check
        if self._should_release_hard_lock():
            self._release_hard_lock("Event completed")
            return True
        
        return False
    
    def _should_release_hard_lock(self) -> bool:
        """
        Check if the HARD_LOCK event has completed.
        
        Event-based release conditions:
        - ORDER_PLACED: Position confirmed OR order no longer exists
        - CANCEL_SENT: Order no longer exists
        - SL_TP_SET: SL/TP orders exist on exchange
        - EXIT_PENDING: Position is now flat (size = 0)
        """
        if self._hard_lock_event is None:
            return True
            
        snapshot = self._last_snapshot
        if snapshot is None:
            return False  # Wait for first poll
        
        if self._hard_lock_event == HardLockEvent.ORDER_PLACED:
            # Release if: position confirmed OR order gone (filled/cancelled)
            return snapshot.has_position or not snapshot.has_pending_entry
        
        if self._hard_lock_event == HardLockEvent.CANCEL_SENT:
            # Release if: no more pending entry orders
            return not snapshot.has_pending_entry
        
        if self._hard_lock_event == HardLockEvent.SL_TP_SET:
            # Release if: SL/TP orders exist (check for stop orders)
            has_stop_orders = any(
                o.get('type') in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                for o in (snapshot.open_orders or [])
            )
            return has_stop_orders
        
        # =====================================================================
        # 🔒 EXIT_PENDING should release when position is flat
        # This ensures we don't wait for timeout when exit already succeeded
        # =====================================================================
        if self._hard_lock_event == HardLockEvent.EXIT_PENDING:
            # Release if: position is now flat (no position on exchange)
            return not snapshot.has_position
        
        # =====================================================================
        # 🔒 PARTIAL_CLOSE should release when size changes
        # For partial closes, position won't be 0, so we release when size differs
        # =====================================================================
        if self._hard_lock_event == HardLockEvent.PARTIAL_CLOSE:
            # Release if: position size has changed from expected
            # We use timeout as the release mechanism since we can't easily track
            # the expected new size here. The 5s timeout is sufficient.
            return False  # Let timeout handle it
        
        # MANUAL: only timeout
        return False
    
    def _release_hard_lock(self, reason: str):
        """Release HARD_LOCK state"""
        if self._hard_lock_event is not None and self._hard_lock_started is not None:
            duration_ms = (datetime.now(UTC) - self._hard_lock_started).total_seconds() * 1000
            logger.info(f"🔓 HARD_LOCK released ({duration_ms:.0f}ms): {reason}")
        
        self._hard_lock_event = None
        self._hard_lock_until = None
        self._hard_lock_reason = ""
        self._hard_lock_started = None
    
    @property
    def has_confirmed_position(self) -> bool:
        """True only if exchange has confirmed an open position"""
        return self._confirmed_position_size > 0
    
    @property
    def confirmed_position(self) -> Dict[str, Any]:
        """Get confirmed position details"""
        if not self.has_confirmed_position:
            return {}
        return {
            'side': self._confirmed_position_side,
            'size': self._confirmed_position_size,
            'entry_price': self._confirmed_entry_price,
        }
    
    def can_subsystems_act(self) -> Tuple[bool, str]:
        """
        Check if subsystems (funding, penalty, exits) can act.
        
        Returns:
            (can_act: bool, reason: str)
        
        Subsystems can ONLY act if:
        1. Not in HARD_LOCK
        2. State is POSITION_OPEN (confirmed by exchange)
        """
        # Block ALL actions when exchange is unavailable
        if self._degraded_mode:
            return False, "DEGRADED_MODE: Exchange connection unstable - all trading blocked"
        
        if self.is_hard_locked and self._hard_lock_event is not None:
            if self._hard_lock_until is not None:
                remaining_ms = (self._hard_lock_until - datetime.now(UTC)).total_seconds() * 1000
                return False, f"HARD_LOCK[{self._hard_lock_event.value}] ({remaining_ms:.0f}ms max): {self._hard_lock_reason}"
            return False, f"HARD_LOCK[{self._hard_lock_event.value}]: {self._hard_lock_reason}"
        
        if self._state != ReconcilerState.POSITION_OPEN:
            return False, f"No confirmed position (state={self._state.value})"
        
        return True, ""
    
    def is_exchange_available(self) -> bool:
        """
        Check if exchange data is reliable enough for trading decisions.
        
        Returns False if:
        - Too many consecutive poll failures
        - Data is stale (older than threshold)
        
        When False: NO logical exits (SL check, trailing, etc.)
        Only emergency close with confirmed position is allowed.
        """
        if self._degraded_mode:
            return False
            
        # Check for stale data
        if self._last_successful_poll is not None:
            staleness = (datetime.now(UTC) - self._last_successful_poll).total_seconds()
            if staleness > self.STALE_DATA_THRESHOLD_SEC:
                logger.warning(f"⚠️ Exchange data stale: {staleness:.1f}s since last successful poll")
                return False
        
        return self._exchange_available
    
    @property
    def is_degraded(self) -> bool:
        """Check if we're in degraded mode (exchange unavailable)"""
        return self._degraded_mode
    
    def enter_hard_lock(self, reason: str, event: HardLockEvent = HardLockEvent.MANUAL, 
                        max_duration_ms: int | None = None):
        """
        Enter HARD_LOCK state for a specific event.
        
        EVENT-BASED lock release:
        - The lock is released when the event completes (position confirms, etc.)
        - max_duration_ms is a FALLBACK timeout to prevent infinite locks
        
        During HARD_LOCK:
        - Funding check: DISABLED
        - Penalty tracking: DISABLED  
        - Exit logic: DISABLED
        - R-tracking: DISABLED
        
        Only the reconciler continues polling to detect actual state.
        """
        if max_duration_ms is None:
            max_duration_ms = self.HARD_LOCK_DURATION_MS
        
        now = datetime.now(UTC)
        self._hard_lock_event = event
        self._hard_lock_started = now
        self._hard_lock_until = now + timedelta(milliseconds=max_duration_ms)
        self._hard_lock_reason = reason
        self._hard_locks_triggered += 1
        
        # DEBUG level - HARD_LOCK is normal operation, not a warning
        logger.debug(f"HARD_LOCK[{event.value}] ({max_duration_ms}ms max): {reason}")
        
        # Transition to HARD_LOCK state
        self._transition_to(ReconcilerState.HARD_LOCK)
    
    def notify_order_placed(self, order_id: str):
        """
        Called when an entry order is placed.
        
        Enters EVENT-BASED HARD_LOCK:
        - Released when: position confirms OR order disappears
        - Fallback: timeout after HARD_LOCK_DURATION_MS
        """
        self._tracked_order_id = order_id
        self._order_placed_time = datetime.now(UTC)
        
        # Event-based HARD_LOCK - released when position confirms
        # Increased timeout from 5s to 15s
        # Kraken API has significant lag (~60s sometimes) between order fill and position appearing
        # The 15s timeout allows for normal API delays while not blocking indefinitely
        self.enter_hard_lock(
            reason=f"Order placed: {order_id[:8]}...",
            event=HardLockEvent.ORDER_PLACED,
            max_duration_ms=15000  # 15s fallback - Kraken can be slow
        )
        
        logger.info(f"📋 Reconciler tracking order: {order_id[:12]}...")
    
    def notify_partial_close(self, reason: str = "Partial close"):
        """
        Called when a PARTIAL close is being executed.
        
        Unlike EXIT_PENDING, position will NOT be 0 after.
        Uses PARTIAL_CLOSE event which releases by timeout.
        
        CRITICAL: During PARTIAL_CLOSE hard-lock:
        - NO state enforcement runs
        - Local size sync happens after exit
        """
        self.enter_hard_lock(
            reason=reason,
            event=HardLockEvent.PARTIAL_CLOSE,
            max_duration_ms=5000  # 5s fallback for partial close
        )
        logger.info(f"🔒 PARTIAL CLOSE HARD_LOCK started: {reason}")
    
    def notify_exit_started(self, reason: str = "Position closing"):
        """
        Called when FULL exit is being executed.
        
        CRITICAL: During EXIT_PENDING hard-lock:
        - NO penalty can be applied
        - NO cooldown can start
        - NO state transition to IDLE
        
        Released ONLY when position_size = 0 confirmed by exchange.
        """
        # Increased from 5s to 10s for exit operations
        self.enter_hard_lock(
            reason=reason,
            event=HardLockEvent.EXIT_PENDING,
            max_duration_ms=10000  # 10s fallback for exit - needs time to confirm flat
        )
        logger.info(f"🔒 EXIT HARD_LOCK started: {reason}")

    def notify_exit_completed(self) -> None:
        """
        Called when exit is confirmed complete.
        
        Releases the EXIT_PENDING hard-lock, allowing:
        - Penalty application (if warranted)
        - Cooldown to start
        - State transition to IDLE
        """
        if self._hard_lock_event == HardLockEvent.EXIT_PENDING:
            logger.info("🔓 EXIT HARD_LOCK released: Exit completed")
            self._release_hard_lock("Exit completed")
    
    async def reconcile(self) -> ExchangeSnapshot | None:
        """
        Poll exchange and derive state.
        
        This is the ONLY method that changes state.
        
        Returns:
            ExchangeSnapshot with current exchange state, or None if no symbol set
        """
        if not self._symbol:
            logger.warning("Reconciler: No symbol set")
            return None
            
        self._reconcile_count += 1
        now = datetime.now(UTC)
        
        # In backtest, state is managed synchronously
        if self.backtest_mode:
            return self._last_snapshot
        
        try:
            # ============================================================
            # STEP 1: Fetch actual exchange state (THE SOURCE OF TRUTH)
            # ============================================================
            
            # Get positions
            position_data = await self.execution.get_position(self._symbol)
            
            # =================================================================
            # 🔒 API timeout/error must NOT be treated as "no position"

            # ROOT CAUSE: get_position() returns {'_timeout': True} or {'_error': ...}
            # on failure. This dict is truthy but has contracts=0, so the reconciler
            # interprets it as "position closed" → false POSITION_OPEN→IDLE transition
            # → records phantom PnL → state corruption.

            # FIX: If position_data contains error/timeout sentinels, skip this
            # reconcile cycle entirely and return last known good snapshot.
            # =================================================================
            if isinstance(position_data, dict) and ('_error' in position_data or '_timeout' in position_data):
                error_type = 'timeout' if position_data.get('_timeout') else position_data.get('_error', 'unknown')
                logger.warning(
                    f"🔒 API returned error/timeout ({error_type}) - "
                    f"SKIPPING reconcile to prevent false position close. "
                    f"Current state: {self._state.name}"
                )
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    if not self._degraded_mode:
                        logger.critical(f"🚨 DEGRADED MODE: {self._consecutive_failures} consecutive exchange failures")
                        self._degraded_mode = True
                        self._exchange_available = False
                return self._last_snapshot
            
            # Get open orders (filter out SL/TP)
            open_orders = await self.execution.get_open_orders(self._symbol)
            pending_entry_orders = [
                o for o in (open_orders or [])
                if o.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
            ]
            
            # Parse position
            has_position = False
            position_size = 0.0
            position_side = None
            entry_price = 0.0
            unrealized_pnl = 0.0
            
            if position_data:
                contracts = float(position_data.get('contracts', 0) or 0)
                if contracts != 0:
                    has_position = True
                    position_size = abs(contracts)
                    position_side = position_data.get('side', '').lower()
                    entry_price = float(position_data.get('entryPrice', 0) or 0)
                    unrealized_pnl = float(position_data.get('unrealizedPnl', 0) or 0)
            
            # Create snapshot
            snapshot = ExchangeSnapshot(
                timestamp=now,
                has_position=has_position,
                position_side=position_side,
                position_size=position_size,
                position_entry_price=entry_price,
                position_unrealized_pnl=unrealized_pnl,
                open_orders=open_orders or [],
                pending_entry_orders=pending_entry_orders
            )
            
            self._last_snapshot = snapshot
            self._last_poll_time = now
            
            # ============================================================
            # STEP 1.5: Check and release HARD_LOCK if conditions met
            # Call this once per reconcile, not in property
            # ============================================================
            self._check_and_release_hard_lock()
            
            # ============================================================
            # STEP 2: Derive state from snapshot
            # ============================================================
            
            new_state = self._derive_state(snapshot)
            
            # ============================================================
            # STEP 3: Handle state transition
            # ============================================================
            
            if new_state != self._state:
                self._transition_to(new_state, snapshot)
            
            # ============================================================
            # STEP 4: Update confirmed position tracking
            # ============================================================
            
            if has_position:
                self._confirmed_position_side = position_side
                self._confirmed_position_size = position_size
                self._confirmed_entry_price = entry_price
            else:
                self._confirmed_position_side = None
                self._confirmed_position_size = 0.0
                self._confirmed_entry_price = 0.0
            
            # Mark exchange as available on successful poll
            self._consecutive_failures = 0
            self._last_successful_poll = now
            if self._degraded_mode:
                logger.info("✅ Exchange connection restored - exiting degraded mode")
                self._degraded_mode = False
            self._exchange_available = True
            
            return snapshot
            
        except Exception as e:
            # Track consecutive failures for degradation
            self._consecutive_failures += 1
            logger.error(f"Reconciler error ({self._consecutive_failures}/{self.MAX_CONSECUTIVE_FAILURES}): {e}")
            
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                if not self._degraded_mode:
                    logger.critical(f"🚨 DEGRADED MODE: {self._consecutive_failures} consecutive exchange failures - blocking all trading decisions")
                    self._degraded_mode = True
                    self._exchange_available = False
            
            return self._last_snapshot
    
    def _derive_state(self, snapshot: ExchangeSnapshot) -> ReconcilerState:
        """
        Derive state purely from exchange snapshot.
        
        This is deterministic logic - same snapshot = same state.
        """
        # Check if HARD_LOCK is still active
        if self.is_hard_locked:
            return ReconcilerState.HARD_LOCK
        
        # State derivation priority:
        # 1. Position exists → POSITION_OPEN
        # 2. Entry order pending → ORDER_PENDING
        # 3. Nothing → IDLE
        
        if snapshot.has_position:
            return ReconcilerState.POSITION_OPEN
        
        if snapshot.has_pending_entry:
            return ReconcilerState.ORDER_PENDING
        
        return ReconcilerState.IDLE
    
    def _transition_to(self, new_state: ReconcilerState, snapshot: ExchangeSnapshot | None = None):
        """Transition to new state and notify listeners"""
        old_state = self._state
        self._previous_state = old_state
        self._state = new_state
        self._state_changes += 1
        
        # Log transition with REASON for clarity
        emoji = {
            ReconcilerState.IDLE: "⚪",
            ReconcilerState.ORDER_PENDING: "🟡",
            ReconcilerState.HARD_LOCK: "🔒",
            ReconcilerState.POSITION_OPEN: "🟢"
        }
        
        # Build reason string for better debugging
        reason = ""
        if snapshot:
            if new_state == ReconcilerState.POSITION_OPEN and snapshot.has_position:
                reason = f" (exchange confirmed: {snapshot.position_side} {snapshot.position_size:.6f} @ {snapshot.position_entry_price:.2f})"
            elif new_state == ReconcilerState.IDLE and not snapshot.has_position:
                reason = " (no position on exchange)"
            elif new_state == ReconcilerState.ORDER_PENDING and snapshot.has_pending_entry:
                reason = f" (entry order pending)"
        
        # Log state transitions - INFO for position changes, DEBUG for others
        if new_state == ReconcilerState.POSITION_OPEN or (old_state == ReconcilerState.POSITION_OPEN and new_state == ReconcilerState.IDLE):
            logger.info(f"{emoji.get(new_state, '❓')} Reconciler: {old_state.value} → {new_state.value}{reason}")
        else:
            logger.debug(f"Reconciler: {old_state.value} → {new_state.value}{reason}")
        
        # Clear HARD_LOCK when transitioning out (via _release_hard_lock for proper cleanup)
        if old_state == ReconcilerState.HARD_LOCK and new_state != ReconcilerState.HARD_LOCK:
            if self._hard_lock_event is not None:
                self._release_hard_lock("State transition")
        
        # Notify listeners with PUBLIC state only
        public_state = self.get_public_state()
        for callback in self._state_listeners:
            try:
                callback(public_state, snapshot)
            except Exception as e:
                logger.error(f"Listener error: {e}")
    

