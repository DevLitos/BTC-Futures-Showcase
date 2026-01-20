"""
Signal Engine for BTC Perpetual Trading System
Implements complete strategy logic with HTF, MTF, LTF filters
Integrated with Kraken WebSocket, REST execution, and external data

NOTE: Position sizing now uses centralized PositionSizer from core_utils.
Edge and state multipliers are coordinated through state_types module.
"""

import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, UTC, timedelta
from prometheus_client import Counter
import logging

from src.engine.state_machine import StateMachine, BotState
from src.engine.filters import (
    KillConditionsFilter, BlackSwanFilter, HTFFilter,
    LTFTriggerFilter
    # MTFStructureFilter removed (FIX #1: Enable earlier entries)
)
from src.engine.risk_engine import RiskEngine
from src.engine.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE  # 🔒 FIX: Centralized size rounding + min size
from src.engine.indicators import (
    calculate_ema, calculate_atr, calculate_adx, calculate_rsi, calculate_bollinger_bands
)
from src.engine.config import HTFFilters, RiskManagement, OperatingMode  # For logging thresholds + MODE
from src.engine.advanced_filters import (
    check_funding_rate_safe,
    should_exit_on_funding,
    check_session_quality,
    check_htf_divergence,
    calculate_vpoc,
    check_vpoc_validity,
    check_btc_trend
)
from src.engine.edge_regime_classifier import EdgeRegimeClassifier
from src.engine.edge_enforcement import EdgeEnforcement
from src.engine.edge_validator import ContextValidity
from src.engine.core_utils import PositionSizer, get_position_sizer
from src.engine.time_provider import get_time_provider, TimeProvider
from src.engine.error_tracker import get_error_tracker, ErrorSeverity
from src.engine.system_health import SystemHealthMonitor
from src.engine.exchange_reconciler import ExchangeReconciler, ReconcilerState
from src.engine.missed_opportunity_tracker import MissedOpportunityTracker
from src.engine.regime_detector import RegimeDetector, MarketRegime, calculate_chase_atr
from src.engine.important_levels import (
    ImportantLevelsProvider, get_important_levels_provider,
    initialize_important_levels, LevelContext, LevelType
)
from src.engine.location_filter import LocationFilter, get_location_filter, LocationContext
from src.engine.pullback_short_engine import (
    PullbackShortEngine, get_pullback_short_engine,
    PullbackShortSignal, TradeType
)
from src.engine.config import PullbackShortConfig
from src.engine.micro_trend_filter import MicroTrendFilter, get_micro_trend_filter, MicroTrendState
from src.engine.filter_pipeline import EntryFilterPipeline, get_entry_filter_pipeline
from src.monitoring.metrics import (
    update_pullback_short_signal, update_pullback_short_entry,
    update_pullback_short_exit, update_pullback_short_status
)

# NEW IMPORTS: Slippage, Spread, Trade Logger, Dynamic Configs (Jan 16, 2026)
from src.engine.slippage_model import (
    SlippageModel, get_slippage_model, calculate_worst_case_rr, OrderType
)
from src.engine.spread_filter import (
    SpreadFilter, get_spread_filter, check_spread_for_entry, SpreadCheckResult
)
from src.engine.trade_logger import TradeLogger, get_trade_logger
from src.engine.config import (
    DynamicScoreThreshold, SessionTradeLimits, VolatilityRegime,
    LossStreakConfig, LocationFilterOverride, MicroTrendADXConfig,
    get_session_name
)

# NEW ARCHITECTURE: Position Store as Single Source of Truth
from src.core import RuntimeMode, get_runtime_config
from src.accounting import get_position_store, TrackedPosition

logger = logging.getLogger(__name__)


def _ts_to_int(ts) -> int:
    """Convert any timestamp (pd.Timestamp, int, float) to int (epoch ms)."""
    if ts is None:
        return 0
    if hasattr(ts, 'value'):
        # pd.Timestamp.value is in nanoseconds, convert to ms
        return int(ts.value // 1_000_000)
    if hasattr(ts, 'timestamp'):
        # datetime object
        return int(ts.timestamp() * 1000)
    return int(ts) if ts else 0


SIGNALS_GENERATED = Counter(
    'trading_signals_generated_total',
    'Total trading signals generated',
    ['symbol', 'direction']
)

# Configuration
ENTRY_TIMEOUT_MINUTES = 3  # 3 candles at 1m = 3 minutes
POSITION_CHECK_INTERVAL = 5  # Check position every 5 seconds


def _create_safe_task(coro, name: str = "anonymous"):
    """
    Create an async task with error handling.
    Prevents fire-and-forget tasks from silently losing errors.
    """
    async def wrapped():
        try:
            return await coro
        except asyncio.CancelledError:
            pass  # Normal shutdown, don't log as error
        except Exception as e:
            logger.error(f"Async task '{name}' failed: {e}")
    
    return asyncio.create_task(wrapped(), name=name)





class SignalEngine:
    """
    Main trading engine implementing the complete strategy
    Orchestrates all filters, state machine, risk management, and execution
    
    Integrated components:
    - market_data: SynchronizedMultiTimeframeBuffer (candle history)
    - order_book: OrderBookManager (depth, imbalance, spread)
    - execution: RESTExecutionEngine (CCXT orders)
    - external_data: ExternalDataManager (funding, macro events)
    - trade_logger: TradeLogger (persistence)
    """
    
    def __init__(self, market_data, execution, external_data, trade_logger, output_manager=None, enforcement_mode: str = 'forward'):
        """
        Initialize SignalEngine.
        
        Args:
            market_data: SynchronizedMultiTimeframeBuffer
            execution: Execution engine (real or mock)
            external_data: External data manager
            trade_logger: Trade logger
            output_manager: Output manager for notifications (Telegram)
            enforcement_mode: Edge enforcement mode ('backtest', 'forward', 'live')
        """
        self.market_data = market_data
        self.execution = execution
        self.external_data = external_data
        self.trade_logger = trade_logger
        self.output_manager = output_manager
        
        # Store enforcement mode for edge enforcement initialization
        self._enforcement_mode = enforcement_mode
        self._is_backtest = enforcement_mode == 'backtest'
        
        # =====================================================================
        # NEW ARCHITECTURE: Position Store Integration
        # In LIVE mode, position_store is the SINGLE SOURCE OF TRUTH
        # =====================================================================
        self._position_store = get_position_store()
        try:
            self._runtime_config = get_runtime_config()
        except RuntimeError:
            # Runtime not initialized yet (backtest mode)
            self._runtime_config = None
        
        # Core components
        self.state_machine = StateMachine(backtest_mode=self._is_backtest)
        self.risk_engine = RiskEngine(capital=0.0)  # Will be updated from exchange
        
        # =====================================================================
        # EXCHANGE RECONCILER - Single Source of Truth (Jan 12, 2026)
        # The reconciler is the ONLY component that derives state from exchange.
        # All other subsystems LISTEN to state but never write to it.
        # =====================================================================
        self.reconciler = ExchangeReconciler(
            execution_engine=execution,
            backtest_mode=self._is_backtest
        )
        
        # Symbol (set during initialization)
        self.symbol = None
        
        # Filters - ALL use market_time, backtest_mode flag for relaxed validation
        self.kill_filter = KillConditionsFilter(self.state_machine, capital=0.0, risk_engine=self.risk_engine, backtest_mode=self._is_backtest)
        self.black_swan_filter = BlackSwanFilter()
        self.htf_filter = HTFFilter(backtest_mode=self._is_backtest)
        # self.mtf_filter = MTFStructureFilter()  # REMOVED (FIX #1: Enable earlier entries)
        self.ltf_filter = LTFTriggerFilter(backtest_mode=self._is_backtest)
        
        # Location filter - macro context veto (prevents buying tops in RANGE)
        self.location_filter = get_location_filter()
        self._location_context: LocationContext = None

        # =====================================================================
        # MICRO TREND FILTER - Prevents LONGs during micro-bearish trends
        # Detects when 5m structure is bearish even if 1h is bullish
        # =====================================================================
        self.micro_trend_filter = get_micro_trend_filter(hard_block_enabled=True)
        self._micro_trend_state: MicroTrendState = None

        # =====================================================================
        # ENTRY FILTER PIPELINE (Jan 15, 2026) - Unified filter evaluation
        # Combines: BB Width, Volatility Shock, Macro Trend, Post-Sweep, Cost, Overtrade
        # =====================================================================
        self.filter_pipeline = get_entry_filter_pipeline(
            bb_width_enabled=True,
            volatility_shock_enabled=True,
            macro_trend_enabled=True,
            post_sweep_enabled=True,
            cost_filter_enabled=True,
            overtrade_enabled=True
        )

        # =====================================================================
        # PULLBACK SHORT ENGINE - Tactical shorts in bullish markets (Jan 14, 2026)
        # Captures corrections (e.g., 97k → 92k) within bullish HTF cycles
        # =====================================================================
        self.pullback_short_engine = get_pullback_short_engine()
        self._current_trade_type: TradeType = TradeType.LONG_MAIN  # Default
        
        # Regime classifier for volume-aware filtering
        self.regime_classifier = EdgeRegimeClassifier()
        
        # Edge enforcement - pre-trade validation & degradation monitoring
        # Uses Activity Regime model (not session-based) for 24/7 crypto markets
        self.edge_enforcement = EdgeEnforcement(mode=self._enforcement_mode)
        
        # Centralized position sizer for consistent sizing calculations
        self._position_sizer = get_position_sizer(
            risk_pct=0.01,  # 1% risk per trade
            max_position_pct=0.10,  # Max 10% of capital
            leverage=10
        )
        
        # Edge context (updated periodically)
        self._edge_context: ContextValidity = None
        self._last_context_update = None
        self._context_update_interval = 300  # Update every 5 minutes
        
        # Trade tracking for edge enforcement
        self._trade_counter = 0
        self._current_trade_entry_time = None
        self._current_trade_entry_price = 0.0
        self._current_trade_mfe = 0.0
        self._current_trade_mae = 0.0
        self._reached_tp1 = False
        self._reached_tp2 = False
        self._is_be_stop = False
        
        # Trade tracking
        self.current_bias: str = None  # 'LONG' or 'SHORT'
        self.impulse_high: float = 0.0
        self.impulse_low: float = 0.0
        self.pending_order_id: str = None
        self.entry_order_time: datetime = None
        
        # STATE MACHINE para evitar busy loops
        self.setup_state = None  # IDLE, WAIT_LTF, EXECUTING
        self.setup_start_time = None  # Cuándo entró en WAIT_LTF
        self.ltf_wait_timeout = 5 * 60  # 5 minutos (300s) = ~5 candles 1m
        self.setup_frozen_htf = False  # Freeze HTF evaluation
        # self.setup_frozen_mtf = False  # REMOVED (no MTF filter)
        self.last_setup_discard_time = None  # Cuándo se descartó el último setup
        self.setup_discard_cooldown = 10  # Cooldown reducido: solo 10s para evitar thrashing
        # Position management flags
        self._stops_set = False  # Flag para evitar intentar establecer SL/TP múltiples veces
        self._sync_lock = asyncio.Lock()  # 🔥 AUDIT FIX: Proper async lock for sync operations
        self._syncing_position = False  # Track if sync is in progress
        
        # =====================================================================
        # MISSING ATTRIBUTE INITIALIZATION (🔥 AUDIT FIX)
        # =====================================================================
        self._last_entry_data = None  # Entry data for current trade
        self._last_exchange_sync = None  # Last time we synced with exchange
        self._consecutive_post_only_rejects = 0  # Track post-only rejections
        self._recent_trade_prices = []  # Recent trade prices for analysis
        self._last_log_time = None  # Rate limiting for logs
        self._last_capital_warning = None  # Rate limiting for capital warnings
        
        # =====================================================================
        # INDICATOR CACHE - Avoid recalculating heavy indicators every tick
        # Only recalculate when candle timestamp changes (Jan 20, 2026)
        # =====================================================================
        self._indicator_cache = {
            'last_candle_ts_1m': 0,
            'last_candle_ts_5m': 0,
            'last_candle_ts_1h': 0,
            'df_1m': None,
            'df_5m': None,
            'df_1h': None,
            'adx_1h': None,
            'atr_1m': None,
            'atr_ratio': None,
            'ema20_5m': None,
            'rsi_1m': None,
            'bb_width': None,
            'bb_avg': None,
            'vpoc': None,
        }
        
        # =====================================================================
        # LOSS STREAK COOLDOWN - Protection against chop regimes (IMPROVED Jan 15, 2026)
        # 
        # PROBLEM: 3 losses before cooldown = too much damage already done
        # SOLUTION: Tiered cooldown system:
        #   - 2 consecutive losses → 30 min cooldown (micro trend likely against us)
        #   - 3 consecutive losses → 60 min cooldown (clear chop regime)
        # =====================================================================
        self._consecutive_losses = 0  # Tracks consecutive SL hits
        self._cooldown_until = None  # Timestamp when cooldown ends
        self._cooldown_minutes = 60  # Cooldown duration after 3 consecutive losses
        self._cooldown_minutes_tier1 = 30  # Shorter cooldown after 2 losses (NEW)
        self._max_consecutive_losses = 2  # Trigger first cooldown after 2 losses (was 3)
        
        # =====================================================================
        # REJECTION COOLDOWN - Prevents rapid reevaluation after filter blocks
        # Only activated for TECHNICAL errors, not filter rejections
        # =====================================================================
        self._rejection_cooldown_until: datetime = None  # When rejection cooldown ends
        self._rejection_cooldown_candles = 2  # Number of candles to wait after rejection
        self._last_rejection_reason: str = None  # Why was the setup rejected
        self._last_evaluated_candle_ts: int = 0  # Timestamp of last evaluated LTF candle
        
        # =====================================================================
        # MOMENTUM QUALITY - For position sizing, not hard veto
        # =====================================================================
        self._last_momentum_quality: str = "MODERATE"  # STRONG, MODERATE, WEAK
        self._last_momentum_atr: float = 100.0  # Last ATR value
        
        # =====================================================================
        # ENTRY SCORE SYSTEM - Score > Gates architecture
        # 4 blocks × 0-2 points + synergy bonus = max 9
        # Uses MODE from OperatingMode for thresholds
        # Only Structure=0 blocks entry (the only real gate)
        # =====================================================================
        self._entry_score: int = 0
        self._score_breakdown: dict = {
            'structure': 0,  # HTF alignment + 5m structure (0-2)
            'timing': 0,     # BOS at close + reclaim quality (0-2)
            'momentum': 0,   # ADX + slope (0-2)
            'context': 0,    # Session + volatility (0-2)
            'synergy': 0     # HTF clean + BOS confirmed = +1 (non-linear bonus)
        }
        # Threshold from OperatingMode - NEVER change based on losses
        self._entry_threshold: int = OperatingMode.get_score_threshold()
        
        # =====================================================================
        # TRADE TAGS - All filters become tags, not blockers
        # This is where edges are discovered
        # =====================================================================
        self._current_trade_tags: dict = {}  # Reset per trade
        
        # =====================================================================
        # DISCOVERY METRICS - Track edge by segment
        # =====================================================================
        self._score_distribution: dict = {
            '5-6': {'trades': 0, 'wins': 0, 'pnl': 0.0},
            '7-8': {'trades': 0, 'wins': 0, 'pnl': 0.0},
            '9+': {'trades': 0, 'wins': 0, 'pnl': 0.0}
        }
        
        # Evaluation counter (para debug - verificar que evalúa continuamente)
        self._idle_eval_count = 0
        
        # Control
        self.is_running = False
        self._system_halted = False  # CRITICAL: Set to True if close fails with position still open
        
        # Log state tracking (para evitar spam)
        self._last_htf_state = None
        self._last_ltf_state = None
        
        # BOS tracking - edge-triggered (solo loguear UNA vez por nivel)
        self._last_bos_level: float = None  # Nivel donde se detectó BOS
        self._last_bos_direction: str = None  # 'LONG' o 'SHORT'
        
        # =====================================================================
        # HTF CACHE - Solo recalcular cuando cierra nueva vela 1H
        # =====================================================================
        self._htf_cache_ts: int = 0  # Timestamp de la última vela 1h evaluada
        self._htf_cache_passed: bool = False  # Resultado cacheado
        self._htf_cache_bias: str = None  # 'LONG', 'SHORT', None
        self._htf_cache_reason: str = ""  # Razón del resultado
        
        # =====================================================================
        # SYSTEM HEALTH MONITOR - Independent of PnL
        # =====================================================================
        self.health_monitor: SystemHealthMonitor = None  # Initialized in initialize()
        
        # =====================================================================
        # MISSED OPPORTUNITY TRACKER - Track blocked signals for edge analysis
        # =====================================================================
        self.missed_tracker = MissedOpportunityTracker()
        
        # =====================================================================
        # REGIME DETECTOR - Context-aware market state (Jan 12, 2026)
        # Anti-chasing becomes relative to regime, not absolute
        # =====================================================================
        self.regime_detector = RegimeDetector()
        self._current_regime: MarketRegime = MarketRegime.RANGE  # Default conservative
        
        # =====================================================================
        # IMPORTANT LEVELS PROVIDER - Anchor levels for S/R (Jan 13, 2026)
        # Year/Quarter/Month/Week closes act as psychological S/R
        # Used for: blocking near levels, confluence bonus, TP adjustment
        # =====================================================================
        self.important_levels: ImportantLevelsProvider = None  # Initialized async in initialize()
        self._last_level_context: LevelContext = None  # Last computed level context
        self._last_levels_update: datetime = None  # Last time levels were refreshed
        
        # =====================================================================
        # NEW: SPREAD FILTER - Real-time spread monitoring (Jan 16, 2026)
        # =====================================================================
        self.spread_filter: SpreadFilter = get_spread_filter()
        
        # =====================================================================
        # NEW: SLIPPAGE MODEL - Worst-case RR calculation (Jan 16, 2026)
        # =====================================================================
        self.slippage_model: SlippageModel = get_slippage_model()
        
        # =====================================================================
        # 🔥 INDICATOR CACHE BY TIMEFRAME (Jan 20, 2026)
        # =====================================================================
        # NEW: TRADE LOGGER - Persistent trade logging with tags (Jan 16, 2026)
        # =====================================================================
        self.trade_logger: TradeLogger = get_trade_logger()
        self._current_trade_id: str = None  # Active trade ID for logging
        
        # =====================================================================
        # NEW: SESSION TRADE COUNTER - Limit trades per session (Jan 16, 2026)
        # =====================================================================
        self._session_trade_counts: dict = {}  # {session_name: count}
        self._last_session: str = None
        self._daily_trade_count: int = 0
        self._last_trade_date: str = None
        self._at_session_limit: bool = False  # Track if at session limit
        
        # =====================================================================
        # NEW: VOLATILITY REGIME TRACKING (Jan 16, 2026)
        # =====================================================================
        self._current_volatility_regime: str = "NORMAL"
        self._last_atr_ratio: float = 1.0
        
        # =====================================================================
        # NEW: LAST SENT SL TRACKING for exchange updates (Jan 16, 2026)
        # =====================================================================
        self._last_sent_sl: float = 0.0
        
        logger.info("Signal Engine initialized")
    
    # =========================================================================
    # 🔥 ABSOLUTE LAW: EXCHANGE IS THE ONLY SOURCE OF TRUTH
    # =========================================================================
    
    async def _enforce_exchange_state_truth(self):
        """
        THE CARDINAL RULE: System state is DERIVED from exchange, not DECIDED by system.
        
        if exchange_position_size > min_size:
            system_state = MANAGE
        else:
            system_state = IDLE
        
        This method runs EVERY tick and OVERRIDES any local state that disagrees.
        No other code path should decide state transitions based on local tracking.
        """
        MIN_POSITION_SIZE = 0.00001  # Minimum BTC position to consider "open"
        
        # Rate limit: Check exchange every 5 seconds max to avoid API spam
        now = self.market_data.get_market_time()
        if not hasattr(self, '_last_exchange_state_check'):
            self._last_exchange_state_check = now
        
        time_since_check = (now - self._last_exchange_state_check).total_seconds()
        if time_since_check < 5:
            return
        
        self._last_exchange_state_check = now
        
        # 🔒 FIX (Jan 14, 2026): Skip enforcement during HARD_LOCK (exit in progress)
        # The reconciler handles state during exits - don't interfere
        if self.reconciler and self.reconciler.is_hard_locked:
            logger.debug("Skipping exchange state enforcement: HARD_LOCK active")
            return
        
        try:
            # 🔥 GET EXCHANGE TRUTH
            exchange_position = await self.execution.get_position(self.symbol)
            
            # CRITICAL: Check if this was a timeout/error (NOT "no position")
            if exchange_position and exchange_position.get('_error'):
                # Exchange unavailable - DO NOT CHANGE STATE
                # Keep current state until exchange is reachable
                if not hasattr(self, '_last_exchange_unavailable_log') or \
                   (self.market_data.get_market_time() - self._last_exchange_unavailable_log).total_seconds() > 30:
                    logger.warning(f"⚠️ Exchange unavailable - maintaining current state (not forcing IDLE)")
                    self._last_exchange_unavailable_log = self.market_data.get_market_time()
                return  # CRITICAL: Don't change state on timeout!
            
            exchange_size = abs(float(exchange_position.get('contracts', 0) or 0)) if exchange_position else 0.0
            
            current_state = self.state_machine.state
            
            # ===== ENFORCE STATE BASED ON EXCHANGE =====
            if exchange_size > MIN_POSITION_SIZE:
                # EXCHANGE HAS POSITION → WE MUST BE IN MANAGE (or WAIT_FILL transitioning)
                # Reset "no position" counter since we have one
                self._consecutive_no_position_checks = 0
                
                if current_state == BotState.IDLE:
                    logger.critical(f"🔄 STATE OVERRIDE: IDLE but exchange has {exchange_size:.6f} → forcing MANAGE")
                    
                    # Sync local position from exchange if we don't have one
                    candles_5m = self.market_data.get_candles('5m')
                    atr_5m = self._calculate_atr_5m(pd.DataFrame(candles_5m)) if candles_5m else None
                    
                    if not self.risk_engine.has_open_position():
                        await self.risk_engine.update_from_exchange_position(exchange_position, atr=atr_5m)
                    
                    self.state_machine.transition_to(BotState.MANAGE, "Exchange has position - enforced")
                    
                    # =========================================================================
                    # 🔥 CRITICAL FIX (Jan 17, 2026): Place emergency SL/TP for orphan position
                    # When we recover a position that has no protection, we MUST place SL/TP
                    # This prevents catastrophic losses on positions filled after bot went IDLE
                    # =========================================================================
                    try:
                        # Check if position has SL/TP protection
                        has_protection = await self._check_position_has_stops()
                        
                        if not has_protection:
                            logger.critical("🚨 ORPHAN POSITION HAS NO SL/TP - PLACING EMERGENCY PROTECTION!")
                            
                            # Get position details
                            entry_price = float(exchange_position.get('entryPrice', 0) or exchange_position.get('avgPrice', 0))
                            if entry_price <= 0:
                                # Fallback to current price if no entry available
                                ticker = await self.execution.get_ticker(self.symbol)
                                entry_price = ticker.get('last', 0) if ticker else 0
                            
                            position_side = exchange_position.get('side', '').lower()
                            if position_side not in ('long', 'short'):
                                contracts = float(exchange_position.get('contracts', 0))
                                position_side = 'long' if contracts > 0 else 'short'
                            
                            # Calculate emergency SL (2.5x ATR from entry, or 3% if no ATR)
                            if atr_5m and atr_5m > 0:
                                emergency_sl_distance = atr_5m * 2.5
                            else:
                                emergency_sl_distance = entry_price * 0.03  # 3% emergency SL
                            
                            if position_side == 'long':
                                emergency_sl = entry_price - emergency_sl_distance
                                emergency_tp = entry_price + (emergency_sl_distance * 2.0)  # 2R TP
                            else:
                                emergency_sl = entry_price + emergency_sl_distance
                                emergency_tp = entry_price - (emergency_sl_distance * 2.0)
                            
                            logger.critical(f"🔒 Emergency SL: {emergency_sl:.2f} | TP: {emergency_tp:.2f} | Entry: {entry_price:.2f}")
                            
                            # Place the stops
                            stops_placed = await self.execution.set_position_stops(
                                symbol=self.symbol,
                                stop_loss=emergency_sl,
                                take_profit=emergency_tp
                            )
                            
                            if stops_placed:
                                logger.info("✅ Emergency SL/TP placed successfully!")
                                self._stops_set = True
                                # Store for potential retry
                                self._last_entry_data = {
                                    'stop_loss': emergency_sl,
                                    'take_profit': emergency_tp,
                                    'emergency_recovery': True
                                }
                                if self.risk_engine.position:
                                    self.risk_engine.position.mark_exchange_sl_active()
                            else:
                                logger.critical("❌ FAILED TO PLACE EMERGENCY SL/TP - POSITION AT EXTREME RISK!")
                                self._stops_set = False
                                if self.risk_engine.position:
                                    self.risk_engine.position.mark_exchange_sl_inactive("Emergency placement failed")
                        else:
                            logger.info("✅ Orphan position already has SL/TP protection")
                            self._stops_set = True
                            
                    except Exception as emergency_e:
                        logger.critical(f"❌ Emergency SL/TP placement error: {emergency_e}")
                
                # Also sync local position size if it differs
                local_pos = self.risk_engine.position
                if local_pos and abs(local_pos.size - exchange_size) > MIN_POSITION_SIZE:
                    logger.warning(f"🔄 Size sync: local={local_pos.size:.6f} → exchange={exchange_size:.6f}")
                    local_pos.size = exchange_size
                    
            else:
                # EXCHANGE HAS NO POSITION
                # 🔒 FIX: Require 3 consecutive "no position" checks before forcing IDLE
                # This prevents race conditions after partial closes
                if not hasattr(self, '_consecutive_no_position_checks'):
                    self._consecutive_no_position_checks = 0
                
                self._consecutive_no_position_checks += 1
                
                if current_state == BotState.MANAGE:
                    if self._consecutive_no_position_checks < 3:
                        # Wait for more confirmations before forcing IDLE
                        logger.warning(f"⚠️ Exchange shows no position ({self._consecutive_no_position_checks}/3) - waiting for confirmation")
                        return
                    
                    # 🔒 FIX (Jan 14, 2026): Force sync position_store before final decision
                    # This ensures phantom positions are cleared
                    try:
                        if self.execution and hasattr(self.execution, 'exchange'):
                            await self._position_store.sync_from_exchange(self.execution.exchange)
                        
                        # NOW check if position_store still has a position (shouldn't after sync)
                        ps_pos = self._position_store.get_position(self.symbol)
                        if ps_pos and not ps_pos.is_flat and ps_pos.size > MIN_POSITION_SIZE:
                            # This is weird - sync should have cleared it. Log but proceed anyway
                            logger.error(f"🚨 position_store still has {ps_pos.side} {ps_pos.size:.6f} after sync - forcing clear")
                            ps_pos.size = 0
                            ps_pos.side = 'FLAT'
                    except Exception as ps_e:
                        logger.debug(f"Position store sync failed: {ps_e}")
                    
                    logger.critical(f"🔄 STATE OVERRIDE: MANAGE but exchange has no position (confirmed 3x) → forcing IDLE")
                    
                    # Clear local position
                    if self.risk_engine.has_open_position():
                        logger.warning("⚠️ Clearing orphan local position")
                        self.risk_engine.position = None
                    
                    self._reset_trade_state("Exchange has no position - enforced")
                    self.state_machine.transition_to(BotState.IDLE, "No exchange position - enforced")
                    self._consecutive_no_position_checks = 0
                
                # If in WAIT_FILL too long with no position, also reset
                elif current_state == BotState.WAIT_FILL:
                    if hasattr(self, 'entry_order_time') and self.entry_order_time:
                        wait_time = (now - self.entry_order_time).total_seconds()
                        if wait_time > 180:  # 3 minutes max
                            logger.warning(f"🔄 WAIT_FILL timeout ({wait_time:.0f}s) with no exchange position")
                            
                            # =========================================================================
                            # 🔥 CRITICAL FIX (Jan 17, 2026): Cancel order AND VERIFY before going IDLE
                            # If cancel fails and order still exists, do NOT go IDLE
                            # This prevents orders from filling later without SL/TP protection
                            # =========================================================================
                            order_cancelled = False
                            order_still_open = False
                            
                            if hasattr(self, 'pending_order_id') and self.pending_order_id:
                                try:
                                    # STEP 1: Attempt to cancel
                                    cancel_success = await self.execution.cancel_order(
                                        self.pending_order_id,
                                        self.symbol
                                    )
                                    
                                    if cancel_success:
                                        logger.info(f"✅ Cancelled pending order {self.pending_order_id[:12]}...")
                                        order_cancelled = True
                                    else:
                                        # STEP 2: Cancel failed - verify order status
                                        logger.warning(f"⚠️ Cancel returned false - verifying order status...")
                                        await asyncio.sleep(0.5)  # Let exchange update
                                        
                                        try:
                                            order_status = await self.execution.get_order_status(
                                                self.pending_order_id,
                                                self.symbol
                                            )
                                            
                                            if order_status:
                                                status = order_status.get('status', '').lower()
                                                if status in ('open', 'pending', 'new'):
                                                    order_still_open = True
                                                    logger.critical(f"🚨 ORDER STILL OPEN! Status: {status} - NOT going IDLE!")
                                                elif status in ('canceled', 'cancelled', 'expired', 'rejected'):
                                                    order_cancelled = True
                                                    logger.info(f"✅ Order was already cancelled/expired: {status}")
                                                elif status in ('filled', 'closed'):
                                                    logger.critical(f"🚨 ORDER FILLED! Status: {status} - checking position...")
                                                    # Re-fetch position - order filled!
                                                    await asyncio.sleep(0.5)
                                                    new_pos = await self.execution.get_position(self.symbol)
                                                    if new_pos and abs(float(new_pos.get('contracts', 0) or 0)) > MIN_POSITION_SIZE:
                                                        logger.critical("🔄 Position now exists - transitioning to MANAGE")
                                                        # STATE OVERRIDE will handle SL/TP placement
                                                        return  # Exit early, let next cycle handle
                                        except Exception as status_e:
                                            logger.error(f"Error checking order status: {status_e}")
                                            # Assume order may still be open
                                            order_still_open = True
                                            
                                except Exception as cancel_e:
                                    logger.error(f"Error cancelling order on timeout: {cancel_e}")
                                    # Assume order may still be open
                                    order_still_open = True
                            else:
                                # No pending order ID - safe to go IDLE
                                order_cancelled = True
                            
                            # STEP 3: Only go IDLE if we're sure order is cancelled/filled
                            if order_still_open:
                                logger.critical("🔒 NOT going IDLE - order may still fill! Will check again next cycle.")
                                # Don't reset trade state - keep trying
                                return
                            
                            self._reset_trade_state("WAIT_FILL timeout - no position")
                            self.state_machine.transition_to(BotState.IDLE, "WAIT_FILL timeout - enforced")
                            self._consecutive_no_position_checks = 0
                    
        except Exception as e:
            # On error, don't crash - just log and continue
            # 🔒 FIX: Include stack trace for debugging
            logger.error(f"Error in exchange state enforcement: {e}", exc_info=True)
    
    def _on_reconciler_state_change(self, public_state, snapshot):
        """
        Callback when ExchangeReconciler detects a state change.
        
        🔥 FIX (Jan 14, 2026): React immediately to position closes detected by reconciler.
        This handles TP/SL fills on exchange that the bot didn't initiate.
        """
        # 🔥 FIX (Jan 20, 2026): Import PublicState, not ReconcilerState
        # The callback receives PublicState enum values
        from src.engine.exchange_reconciler import PublicState
        
        # If reconciler says IDLE and we're in MANAGE, the position was closed externally
        if public_state == PublicState.IDLE and self.state_machine.state == BotState.MANAGE:
            logger.critical("📤 RECONCILER DETECTED: Position closed on exchange (TP/SL hit)")
            
            # Record trade close with estimated PnL
            if self.risk_engine.has_open_position():
                position = self.risk_engine.position
                # Try to get last known price for PnL estimate
                candles = self.market_data.get_candles('1m')
                current_price = candles[-1]['close'] if candles and len(candles) > 0 else position.entry_price
                pnl_estimate = position.calculate_pnl(current_price) if hasattr(position, 'calculate_pnl') else 0
                
                # Calculate R realized for notification
                initial_risk = abs(position.entry_price - position.initial_stop_loss)
                r_realized = pnl_estimate / (initial_risk * position.size) if initial_risk > 0 and position.size > 0 else 0
                
                # Determine exit reason (TP or SL)
                if position.direction == 'LONG':
                    exit_type = 'EXCHANGE_TAKE_PROFIT' if current_price > position.entry_price else 'EXCHANGE_STOP_LOSS'
                else:
                    exit_type = 'EXCHANGE_TAKE_PROFIT' if current_price < position.entry_price else 'EXCHANGE_STOP_LOSS'
                
                # Log trade close - 🔥 FIX: Use proper exit_type so losses are counted correctly
                self.state_machine.record_trade_close(pnl_estimate, pnl_estimate > 0, exit_reason=exit_type)
                self.state_machine.update_daily_pnl(pnl_estimate)
                
                logger.info(f"✅ Trade closed by exchange TP/SL - Estimated PnL: ${pnl_estimate:.2f}")
                
                # 🔥 FIX (Jan 14, 2026): Send Telegram notification for exchange TP/SL closes
                try:
                    from datetime import datetime, UTC
                    _create_safe_task(self.output_manager.send_exit_notification({
                        'symbol': self.symbol,
                        'reason': exit_type,  # Already has EXCHANGE_ prefix
                        'pnl': pnl_estimate,
                        'r_realized': r_realized,
                        'exit_price': current_price,
                        'entry_price': position.entry_price,
                        'direction': position.direction,
                        'timestamp': datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')
                    }), "send_exit_notification_exchange_tpsl")
                    logger.info("📱 Telegram notification sent for exchange TP/SL close")
                except Exception as e:
                    logger.error(f"Failed to send Telegram notification for TP/SL: {e}")
            
            # Clear local state
            self.risk_engine.position = None
            self._reset_trade_state("Exchange TP/SL hit - reconciler detected")
            self.state_machine.transition_to(BotState.IDLE, "Exchange TP/SL filled")
            self._consecutive_no_position_checks = 0
            
            # Also clear position_store
            try:
                ps_pos = self._position_store.get_position(self.symbol)
                if ps_pos:
                    ps_pos.size = 0
                    ps_pos.side = 'FLAT'
            except Exception as ps_err:
                logger.debug(f"Position store cleanup skipped: {ps_err}")
    
    def _reset_trade_state(self, reason: str = "State reset"):
        """Limpia COMPLETAMENTE el estado de trading - CRITICAL para prevenir órdenes múltiples"""
        logger.info(f"🔄 Resetting trade state: {reason}")
        
        # 🔥 FIX (Jan 20, 2026): Clear position_store on state reset
        # This ensures position tracking is always in sync after exits
        try:
            if hasattr(self, '_position_store') and self._position_store:
                ps_pos = self._position_store.get_position(self.symbol)
                if ps_pos and ps_pos.size > 0:
                    ps_pos.size = 0
                    ps_pos.side = 'FLAT'
                    logger.debug(f"Position store cleared during reset: {reason}")
        except Exception as ps_err:
            logger.debug(f"Position store cleanup skipped: {ps_err}")
        
        # Limpia tracking de orden
        self.pending_order_id = None
        self.entry_order_time = None
        
        # Limpia datos de entrada
        self._last_entry_data = None
        
        # Limpia flags de posición
        self._stops_set = False
        
        # Limpia bias y setup
        self.current_bias = None
        self.impulse_high = 0.0
        self.impulse_low = 0.0
        
        # Limpia estado de setup
        self.setup_state = None
        self.setup_start_time = None
        self.setup_frozen_htf = False
        
        # Reset log state tracking (para mostrar nuevo setup)
        self._last_htf_state = None
        self._last_ltf_state = None
        
        # Reset BOS tracking (nueva posición = nuevo contexto de estructura)
        self._last_bos_level = None
        self._last_bos_direction = None
        
    def _init_health_monitor(self):
        """Initialize health monitor (called after risk engine capital is set)"""
        if not hasattr(self, 'health_monitor') or self.health_monitor is None:
            self.health_monitor = SystemHealthMonitor()
            logger.info("SystemHealthMonitor initialized")
    
    def _set_rejection_cooldown(self, reason: str, candles: int = None):
        """
        Set a cooldown after a filter rejection (NOT a full reset).
        This prevents the tight loop of evaluate->reject->reset->evaluate.
        
        Args:
            reason: Why the setup was rejected
            candles: Number of candles to wait (default: self._rejection_cooldown_candles)
        """
        if candles is None:
            candles = self._rejection_cooldown_candles
        
        # Calculate cooldown end time (candles * 60 seconds per 1m candle)
        cooldown_seconds = candles * 60
        market_time = self.market_data.get_market_time()
        self._rejection_cooldown_until = market_time + timedelta(seconds=cooldown_seconds)
        self._last_rejection_reason = reason
        
        # Log only once (not on every tick)
        logger.info(f"⏸️ Rejection cooldown: {reason} | Wait {candles} candles until {self._rejection_cooldown_until.strftime('%H:%M:%S')}")
    
    def _is_in_rejection_cooldown(self) -> tuple[bool, str]:
        """Check if we're in a rejection cooldown period."""
        if self._rejection_cooldown_until is None:
            return False, ""
        
        market_time = self.market_data.get_market_time()
        if market_time < self._rejection_cooldown_until:
            remaining = (self._rejection_cooldown_until - market_time).total_seconds()
            return True, f"Rejection cooldown active ({remaining:.0f}s remaining): {self._last_rejection_reason}"
        
        # Cooldown expired
        self._rejection_cooldown_until = None
        self._last_rejection_reason = None
        return False, ""
    
    def _get_current_candle_timestamp(self) -> int:
        """Get the timestamp of the most recent closed 1m candle."""
        candles_1m = self.market_data.get_candles('1m')
        if candles_1m and len(candles_1m) > 0:
            # Get timestamp of last closed candle
            last_candle = candles_1m[-1]
            return _ts_to_int(last_candle.get('timestamp', 0))
        return 0
    
    def _is_new_candle(self) -> bool:
        """Check if a new 1m candle has closed since last evaluation."""
        current_ts = self._get_current_candle_timestamp()
        if current_ts > self._last_evaluated_candle_ts:
            return True
        return False
    
    def _mark_candle_evaluated(self):
        """Mark the current candle as evaluated."""
        self._last_evaluated_candle_ts = self._get_current_candle_timestamp()
    
    async def _reconcile_orders(self):
        """
        Reconciliar órdenes con exchange (elimina órdenes fantasma)
        Llamar después de reconexiones de WebSocket o timeouts
        """
        try:
            # Consultar órdenes reales en exchange
            real_orders = await self.execution.get_open_orders(self.symbol)
            
            # Limpiar tracking local
            self.execution.open_orders.clear()
            
            # Re-sincronizar solo órdenes que existen
            if real_orders:
                for order in real_orders:
                    order_id = order.get('id')
                    self.execution.open_orders[order_id] = order
                    logger.debug(f"Reconciled order: {order_id}")
            else:
                logger.debug("No open orders on exchange - state clean")
                
        except Exception as e:
            logger.error(f"Error reconciling orders: {e}")
        
        # Limpia estado de setup
        self.setup_state = None
        self.setup_start_time = None
        self.setup_frozen_htf = False
    
    async def initialize(self, symbol: str = 'BTC/USDT:USDT', leverage: int = 10):
        """Initialize signal engine - fetch balance and configure leverage"""
        logger.info("Initializing Signal Engine...")
        
        # Store symbol
        self.symbol = symbol
        
        # =====================================================================
        # 🔒 INITIALIZE RECONCILER with symbol (Jan 12, 2026)
        # =====================================================================
        self.reconciler.set_symbol(symbol)
        
        # 🔥 FIX (Jan 14, 2026): Register as listener to react to position closes
        self.reconciler.register_listener(self._on_reconciler_state_change)
        
        # 1. Set leverage on exchange
        leverage_set = await self.execution.set_leverage(leverage, symbol)
        if leverage_set:
            logger.info(f"✓ Leverage configured: {leverage}x")
        else:
            logger.warning(f"✗ Could not set leverage (exchange may not support it)")
        
        # 2. Fetch account balance using dedicated method
        logger.info("Fetching account balance from exchange...")
        
        # Try new method first (preferred)
        capital = await self.execution.get_available_capital()
        
        if capital > 0:
            self.risk_engine.capital = capital
            logger.info(f"✅ Capital loaded via get_available_capital: ${self.risk_engine.capital:.2f}")
        else:
            # Fallback to old method
            logger.warning("Trying fallback balance fetch...")
            balance = await self.execution.get_account_balance()
            if balance:
                updated = await self.risk_engine.update_capital_from_balance(balance)
                if updated:
                    logger.info(f"✓ Capital loaded via update_capital_from_balance: ${self.risk_engine.capital:.2f}")
                else:
                    logger.error("✗ Failed to update capital from balance")
            else:
                logger.error("✗ Failed to fetch account balance")
        
        # 3. Verify minimum capital
        if self.risk_engine.capital < 10:
            logger.warning(f"⚠ Insufficient capital: ${self.risk_engine.capital:.2f} (minimum: $10) - Trading might be restricted")
        
        # 🔥 DAILY DRAWDOWN: Initialize daily equity at startup
        self.state_machine.initialize_daily_equity(self.risk_engine.capital)
        
        # 4. Initial order reconciliation
        logger.info("Performing initial order reconciliation...")
        await self._reconcile_orders()
        
        # 5. Initialize system health monitor
        self._init_health_monitor()
        
        # 6. Initialize important levels (anchor levels for S/R)
        logger.info("Initializing important anchor levels...")
        try:
            self.important_levels = get_important_levels_provider(symbol)
            if await self.important_levels.initialize():
                self._last_levels_update = datetime.now(UTC)
                logger.info("✅ Important levels initialized")
            else:
                logger.warning("⚠️ Important levels failed to initialize - continuing without")
        except Exception as e:
            logger.warning(f"⚠️ Important levels initialization error: {e} - continuing without")
            self.important_levels = None
            
        logger.info(f"✅ Signal Engine initialized and ready | Capital: ${self.risk_engine.capital:.2f}")
    
    async def start(self):
        """Start the signal engine main loop"""
        logger.info("Starting Signal Engine...")
        self.is_running = True
        
        # Main loop
        await self._main_loop()
        
        logger.info("Signal Engine stopped")
    
    async def stop(self):
        """Stop the signal engine"""
        logger.info("Stopping Signal Engine...")
        self.is_running = False
        
        # Cancel pending orders
        if self.pending_order_id:
            try:
                await self.execution.cancel_order(self.pending_order_id, self.symbol)
            except Exception as e:
                logger.error(f"Error cancelling pending order: {e}")
        
        # Close position if open
        if self.risk_engine.has_open_position():
            try:
                await self.emergency_close_all()
            except Exception as e:
                logger.error(f"Error in emergency close: {e}")
    
    # ===== MAIN LOOP =====
    
    async def _main_loop(self):
        """Main trading loop - process ticks every second"""
        last_reconcile_time = datetime.now(UTC)
        last_position_sync_time = datetime.now(UTC)
        
        while self.is_running:
            # ═══════════════════════════════════════════════════════════════
            # 🔴 CRITICAL SAFETY CHECK: System halted state
            # If we failed to close a position, DO NOT continue trading
            # ═══════════════════════════════════════════════════════════════
            if self._system_halted:
                logger.critical("🔴 SYSTEM HALTED - Trading loop suspended")
                logger.critical("🔴 Manual intervention required to close exchange position")
                await asyncio.sleep(60)  # Sleep longer when halted
                continue
            
            try:
                # ═══════════════════════════════════════════════════════════════
                # 🔒 NEW: Position Store Sync (LIVE mode - every 30s)
                # This ensures we always have accurate position state from exchange
                # FIX #4 (Jan 13, 2026): Enhanced with auto-correction
                # ═══════════════════════════════════════════════════════════════
                if self._runtime_config and self._runtime_config.mode == RuntimeMode.LIVE:
                    time_since_sync = (datetime.now(UTC) - last_position_sync_time).total_seconds()
                    if time_since_sync > 30:
                        await self._periodic_reconciliation()
                        last_position_sync_time = datetime.now(UTC)
                
                # Periodic order reconciliation (every 60s)
                time_since_reconcile = (datetime.now(UTC) - last_reconcile_time).total_seconds()
                if time_since_reconcile > 60:
                    # Use enhanced reconciliation
                    if hasattr(self.execution, 'auto_reconcile_orders'):
                        await self.execution.auto_reconcile_orders(self.symbol)
                    else:
                        await self._reconcile_orders()
                    last_reconcile_time = datetime.now(UTC)
                
                tick_start = datetime.now(UTC)
                await self._process_tick()
                tick_duration = (datetime.now(UTC) - tick_start).total_seconds()
                # Only warn if tick takes more than 4 seconds (reconciler API calls take ~2s every 5s)
                if tick_duration > 4.0:
                    logger.warning(f"⏱️ SLOW TICK: {tick_duration:.2f}s")
                await asyncio.sleep(1)
                
                # Record success to reset error counter
                error_tracker = get_error_tracker()
                if error_tracker:
                    error_tracker.record_success("main_loop")
                
                # 📊 CAPA 5: Health-based kill switch (every ~60s)
                if self.health_monitor and int(datetime.now(UTC).timestamp()) % 60 == 0:
                    should_stop, reason = self.health_monitor.should_stop_trading()
                    if should_stop:
                        logger.critical(f"🚨 HEALTH KILL SWITCH: {reason}")
                        self.is_running = False
                        if self.output_manager:
                            await self.output_manager.send_critical_alert(f"KILL SWITCH: {reason}")
                        break
                    
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                
                # Track error for kill switch
                error_tracker = get_error_tracker()
                if error_tracker:
                    should_stop = await error_tracker.record_error(
                        source="main_loop",
                        message=str(e),
                        severity=ErrorSeverity.ERROR
                    )
                    if should_stop:
                        logger.critical("🚨 ERROR KILL SWITCH - Stopping trading")
                        self.is_running = False
                        break
                
                await asyncio.sleep(1)
    
    async def _process_tick(self):
        """Process one tick - implements strategy logic"""
        
        # UPDATE MARKET TIME in state machine (critical for cooldown/observation)
        market_time = self.market_data.get_market_time()
        self.state_machine.set_market_time(market_time)
        
        # =========================================================================
        # 📊 MISSED OPPORTUNITY TRACKER: Update pending with current price
        # =========================================================================
        current_price_for_tracker = self.market_data.get_current_price()
        if current_price_for_tracker and hasattr(self, 'missed_tracker'):
            self.missed_tracker.update_pending(current_price_for_tracker, market_time)
        
        # =========================================================================
        # 🔄 RECONCILER: Poll exchange every 5 seconds (not every tick!)
        # Rate-limited to avoid API spam that causes 4-second ticks
        # =========================================================================
        if not self._is_backtest:
            now = datetime.now(UTC)
            if not hasattr(self, '_last_reconcile_time'):
                self._last_reconcile_time = now
            time_since_reconcile = (now - self._last_reconcile_time).total_seconds()
            if time_since_reconcile >= 5:
                try:
                    await self.reconciler.reconcile()
                    self._last_reconcile_time = now
                except Exception as e:
                    logger.error(f"Reconciler error: {e}")
        
        # NOTE: _enforce_exchange_state_truth() removed - reconciler already handles this
        # and calling both caused redundant API calls (4 second ticks)
        
        # Check if we have sufficient data
        has_data = self.market_data.has_complete_data()
        
        if not has_data:
            # Log state of data buffers (pero no cada iteración para evitar spam)
            if not hasattr(self, '_last_incomplete_log'):
                self._last_incomplete_log = market_time
            
            time_since_log = (market_time - self._last_incomplete_log).total_seconds()
            if time_since_log > 10:  # Log cada 10 segundos si falta data
                status = self.market_data.get_candle_completion_status()
                logger.warning(f"⏳ Waiting for sufficient data: 1m={status['1m']}/20, "
                             f"5m={status['5m']}/50, 1h={status['1h']}/50")
                self._last_incomplete_log = market_time
            return
        
        # Get current price (REAL-TIME, not just closed candle)
        current_price = self.market_data.get_current_price()
        
        # Get candles for indicators (ATR, etc)
        candles_1m = self.market_data.get_candles('1m')
        if not candles_1m or len(candles_1m) < 14:
            return
        
        # Calculate ATR on CLOSED candles (standard practice)
        highs = np.array([c['high'] for c in candles_1m])
        lows = np.array([c['low'] for c in candles_1m])
        closes = np.array([c['close'] for c in candles_1m])
        
        # Calculate ATR 1m
        atr_1m = calculate_atr(highs, lows, closes, 14)[-1]
        
        # ===== BLOQUE 0: KILL CONDITIONS =====
        should_kill, kill_reason = self.kill_filter.check(current_price, atr_1m, market_time)
        if should_kill:
            logger.critical(f"KILL CONDITION MET: {kill_reason}")
            await self.emergency_close_all()
            self.is_running = False
            return
        
        # Check black swan (IMPROVED: pass full candle data for wick detection)
        price_change = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0
        last_candle = candles_1m[-1] if candles_1m else {}
        is_black_swan, bs_reason = self.black_swan_filter.check(
            price_change=price_change,
            atr=atr_1m,
            market_time=market_time,
            candle_high=last_candle.get('high'),
            candle_low=last_candle.get('low'),
            candle_open=last_candle.get('open'),
            candle_close=last_candle.get('close')
        )
        
        if is_black_swan:
            logger.warning(f"🦢 BLACK SWAN: {bs_reason}")
            if self.pending_order_id:
                try:
                    await self.execution.cancel_order(self.pending_order_id, self.symbol)
                except Exception as e:
                    logger.error(f"Error cancelling pending order during black swan: {e}")
            
            # Limpieza completa del estado
            self._reset_trade_state("Black swan event")
            self.state_machine.start_cooldown(8)  # Reduced from 30 to 8 min
            return
        
        # Log defensive mode if active (rate-limited to avoid spam)
        if self.black_swan_filter.is_defensive(market_time):
            if not hasattr(self, '_last_defensive_log') or \
               (market_time - self._last_defensive_log).total_seconds() >= 60:
                logger.info(f"⚠️ Defensive mode active - size multiplier: {self.black_swan_filter.get_size_multiplier(market_time)}")
                self._last_defensive_log = market_time
        
        # Update heartbeat with market time
        self.kill_filter.update_heartbeat(market_time)
        
        # ===== EDGE ENFORCEMENT UPDATES =====
        # Update edge context periodically
        await self._update_edge_context()
        # Update regime classifier with market data
        await self._update_regime_classifier(current_price)
        
        # ===== STATE MACHINE LOGIC =====
        
        if self.state_machine.state == BotState.IDLE:
            await self._handle_idle_state(current_price, atr_1m, market_time)
        
        elif self.state_machine.state == BotState.WAIT_FILL:
            await self._handle_wait_fill_state(market_time)
        
        elif self.state_machine.state == BotState.MANAGE:
            await self._handle_manage_state(current_price, atr_1m, market_time)
        
        elif self.state_machine.state == BotState.OBSERVATION:
            await self._handle_observation_state(current_price, atr_1m, market_time)
    
    # ===== OBSERVATION STATE - MONITOR ONLY =====
    
    async def _handle_observation_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """
        OBSERVATION state: Monitor market without trading
        Intenta recuperarse cuando las métricas mejoran
        """
        # Intentar salir de observation mode
        self.state_machine.try_exit_observation_mode()
        
        # Si aún estamos en observation, solo log estado
        if self.state_machine.state == BotState.OBSERVATION:
            elapsed = (market_time - self.state_machine.observation_entry_time).total_seconds() / 60 if self.state_machine.observation_entry_time else 0
            logger.debug(f"👁️ OBSERVATION: {elapsed:.0f}min elapsed | Trades: {self.state_machine.trades_in_observation} | Wins: {self.state_machine.wins_in_observation}")
        
    # ===== IDLE STATE - LOOK FOR SETUPS =====
    
    async def _handle_idle_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """IDLE state: look for trade setups with proper state management"""
        
        # PROTECCIÓN: Si estamos en OBSERVATION, no entrar en trades nuevas
        if self.state_machine.state == BotState.OBSERVATION:
            logger.debug("In OBSERVATION state, skipping entry evaluation")
            return
        
        # =====================================================================
        # 🔒 REJECTION COOLDOWN CHECK - Prevents tight loop after filter blocks
        # =====================================================================
        in_cooldown, cooldown_reason = self._is_in_rejection_cooldown()
        if in_cooldown:
            # Log only every 30 ticks to avoid spam
            self._idle_eval_count += 1
            if self._idle_eval_count % 30 == 0:
                logger.debug(f"⏸️ {cooldown_reason}")
            return
        
        # =====================================================================
        # 🕐 CANDLE GATE - Only evaluate on NEW closed candle (not every tick)
        # =====================================================================
        # EXCEPTION: If in WAIT_LTF state, check on every tick for faster trigger
        if self.setup_state != "WAIT_LTF":
            if not self._is_new_candle():
                # Same candle, no need to re-evaluate the full setup
                # Just update tick counter silently
                self._idle_eval_count += 1
                if self._idle_eval_count % 60 == 0:  # Log every minute (60 ticks)
                    logger.debug(f"📊 IDLE: Waiting for new candle | Tick #{self._idle_eval_count} | Price: ${current_price:.2f}")
                return
            # New candle - mark as evaluated and proceed
            self._mark_candle_evaluated()
        
        # Contador de evaluaciones (log cada 30 iteraciones para verificar actividad)
        self._idle_eval_count += 1
        if self._idle_eval_count % 30 == 0:
            logger.info(f"📊 IDLE: Evaluated {self._idle_eval_count} ticks | Price: ${current_price:.2f} | ATR: {atr_1m:.2f}")
        
        # 🔒 CRITICAL: Verificar que NO hay orden pendiente o posición abierta
        if self.pending_order_id:
            logger.critical(f"⛔ IDLE STATE ABORT: Pending order {self.pending_order_id} exists - transitioning to WAIT_FILL")
            self.state_machine.transition_to(BotState.WAIT_FILL, "Recovered from IDLE with pending order")
            return
        
        if self.risk_engine.has_open_position():
            logger.critical(f"⛔ IDLE STATE ABORT: Open position exists - transitioning to MANAGE")
            self.state_machine.transition_to(BotState.MANAGE, "Recovered from IDLE with open position")
            return
        
        # 🔥 PERIODIC EXCHANGE SYNC: Verificar cada 30 segundos si hay posición en exchange
        if self._last_exchange_sync is None:
            self._last_exchange_sync = market_time
        
        time_since_sync = (market_time - self._last_exchange_sync).total_seconds()
        if time_since_sync > 30:  # Sync cada 30s
            # 🔥 AUDIT FIX: Use proper async lock with timeout to prevent deadlock
            try:
                async with asyncio.timeout(10):  # 10 second timeout on lock
                    async with self._sync_lock:
                        try:
                            self._syncing_position = True
                            
                            exchange_has_position = await self.execution.has_open_position(self.symbol)
                            if exchange_has_position:
                                logger.critical("🔄 IDLE but exchange has position - syncing state...")
                                
                                # Get ATR for proper SL calculation
                                candles_5m = self.market_data.get_candles('5m')
                                atr_5m = self._calculate_atr_5m(pd.DataFrame(candles_5m)) if candles_5m else None
                                
                                # Intentar recuperar y sincronizar posición
                                exchange_position = await self.execution.get_position(self.symbol)
                                if exchange_position:
                                    await self.risk_engine.update_from_exchange_position(exchange_position, atr=atr_5m)
                                
                                # 🔒 FIX: Don't set flag here - let finally handle it
                                self.state_machine.transition_to(BotState.MANAGE, "Synced with exchange position")
                                return
                                
                            self._last_exchange_sync = market_time
                            
                        except Exception as e:
                            logger.error(f"Error during exchange sync in IDLE: {e}")
                        finally:
                            self._syncing_position = False
            except asyncio.TimeoutError:
                logger.warning("Exchange sync lock acquisition timeout - will retry next cycle")
        
        # ===== CHECK IF IN WAIT_LTF STATE =====
        if self.setup_state == "WAIT_LTF":
            elapsed = (market_time - self.setup_start_time).total_seconds()
            
            # 1. Check timeout
            if elapsed > self.ltf_wait_timeout:
                logger.warning(f"WAIT_LTF timeout after {elapsed:.0f}s - Discarding setup")
                self.setup_state = "IDLE"
                self.setup_frozen_htf = False
                self.current_bias = None
                # NO establecer cooldown - permitir re-evaluación inmediata
                return
            
            # 2. Check invalidation conditions
            # EMA50 break = setup invalid
            candles_5m = self.market_data.get_candles('5m')
            if candles_5m and len(candles_5m) >= 20:
                df_5m = pd.DataFrame(candles_5m)
                closes_5m = df_5m['close'].values
                ema_50 = calculate_ema(closes_5m, 50)[-1]
                
                if self.current_bias == "LONG" and current_price < ema_50:
                    logger.warning(f"Setup invalidated: Price broke EMA50 down during WAIT_LTF")
                    self.setup_state = "IDLE"
                    self.setup_frozen_htf = False
                    self.current_bias = None
                    # NO cooldown - permitir detección inmediata si precio vuelve a subir
                    return
                
                if self.current_bias == "SHORT" and current_price > ema_50:
                    logger.warning(f"Setup invalidated: Price broke EMA50 up during WAIT_LTF")
                    self.setup_state = "IDLE"
                    self.setup_frozen_htf = False
                    self.current_bias = None
                    # NO cooldown - permitir detección inmediata si precio vuelve a bajar
                    return
            
            # 3. Try LTF trigger
            logger.debug(f"WAIT_LTF state: Checking LTF trigger ({elapsed:.0f}s/{self.ltf_wait_timeout}s)")
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 20:
                df_1m = pd.DataFrame(candles_1m)
                
                # Get regime info for volume-aware filtering
                regime_value = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else None
                regime_conf = self.regime_classifier.regime_confidence
                
                ltf_passed, ltf_reason = self.ltf_filter.check(
                    df_1m,
                    self.current_bias,
                    {},
                    regime=regime_value,
                    regime_confidence=regime_conf
                )
                
                if ltf_passed:
                    logger.info(f"✅ LTF TRIGGER FIRED - Executing {self.current_bias}")
                    self.setup_state = "EXECUTING"
                    # Proceed to entry execution
                    await self._execute_entry(current_price, atr_1m, market_time)
                    return
                else:
                    logger.debug(f"LTF not ready yet: {ltf_reason}")
                    return  # Stay in WAIT_LTF
            
            return  # Stay in WAIT_LTF, waiting for LTF trigger
        
        # ===== IDLE STATE - FULL EVALUATION =====
        # NO cooldown - los filtros ya previenen thrashing
        # Si las condiciones vuelven a ser válidas, el bot debe detectarlo inmediatamente
        
        # ===== 🔥 DAILY DRAWDOWN CHECK - BEFORE ANYTHING ELSE =====
        is_halted, halt_reason = self.state_machine.check_daily_halt()
        if is_halted:
            if self._idle_eval_count % 1000 == 0:  # Log sparingly
                logger.critical(f"🛑 DAILY HALT ACTIVE: {halt_reason}")
            return
        
        # ===== EXCHANGE AVAILABILITY CHECK =====
        if hasattr(self.execution, 'is_exchange_available') and not self.execution.is_exchange_available:
            if self._idle_eval_count % 100 == 0:
                logger.warning("⚠️ Exchange unavailable - skipping signal evaluation")
            return
        
        # ===== IDLE STATE - FULL EVALUATION =====
        # Check if can trade
        can_trade, reason = self.state_machine.can_trade()
        if not can_trade:
            # Log at INFO level occasionally (every 100 evals) for diagnosis
            if self._idle_eval_count % 100 == 0:
                logger.info(f"⚠️ Cannot trade: {reason}")
            return
        
        # Refresh balance before looking for trade (every ~5 minutes in IDLE)
        if not hasattr(self, '_last_balance_check'):
            self._last_balance_check = market_time
        
        if (market_time - self._last_balance_check).total_seconds() > 300:  # 5 min
            try:
                balance = await self.execution.get_account_balance()
                if balance:
                    await self.risk_engine.update_capital_from_balance(balance)
                self._last_balance_check = market_time
            except Exception as e:
                logger.warning(f"Balance check error: {e}")
        
        # Check external data (funding, macro)
        ext_can_trade, ext_reason = self.external_data.can_trade()
        if not ext_can_trade:
            logger.debug(f"External data blocks trading: {ext_reason}")
            return
        
        # ===== BLOQUE 1: HTF FILTER (1h) - CON CACHÉ INTRA-BAR =====
        candles_1h = self.market_data.get_candles('1h')
        if not candles_1h or len(candles_1h) < 50:
            return
        
        # Convert to DataFrame for filters
        df_1h = pd.DataFrame(candles_1h)
        
        # HTF Cache: Solo recalcular cuando cierra vela nueva 1h
        last_1h_ts = _ts_to_int(candles_1h[-1].get('timestamp', 0))
        if last_1h_ts != self._htf_cache_ts:
            # Nueva vela 1h cerrada → recalcular
            htf_passed, bias, htf_reason = self.htf_filter.check(
                df_1h,
                current_price,
                correlation_data=None
            )
            # Guardar en caché
            self._htf_cache_ts = last_1h_ts
            self._htf_cache_passed = htf_passed
            self._htf_cache_bias = bias
            self._htf_cache_reason = htf_reason
            logger.debug(f"HTF cache updated: ts={last_1h_ts}, passed={htf_passed}, bias={bias}")
        else:
            # Usar caché
            htf_passed = self._htf_cache_passed
            bias = self._htf_cache_bias
            htf_reason = self._htf_cache_reason
        
        # IMPROVED: Allow counter-trend trading in defensive mode
        # When market is volatile, both directions can present opportunities
        allow_countertrend = False
        if not htf_passed and HTFFilters.ALLOW_COUNTERTREND_DEFENSIVE:
            if self.black_swan_filter.is_defensive(market_time):
                # In defensive mode, allow trading against HTF with reduced size
                # Derive counter-trend bias from EMA relationship
                ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                
                if ema_50 > ema_200:
                    counter_bias = 'SHORT'  # Counter to bullish HTF
                else:
                    counter_bias = 'LONG'   # Counter to bearish HTF
                
                logger.info(f"⚠️ HTF rejected but DEFENSIVE MODE active - allowing counter-trend {counter_bias}")
                allow_countertrend = True
                bias = counter_bias
                htf_passed = True  # Override for this trade
        
        if not htf_passed:
            # HTF filter rechazado - loguear cada 5 minutos (cada vela 5m cerrada)
            if not hasattr(self, '_last_htf_reject_log_time'):
                self._last_htf_reject_log_time = market_time
            
            seconds_since_log = (market_time - self._last_htf_reject_log_time).total_seconds()
            
            # Log cada 5 minutos (300 segundos) o si cambió de razón
            should_log = (seconds_since_log >= 300) or (self._last_htf_state != 'rejected')
            
            if should_log:
                logger.info(f"⛔ HTF filter rejected: {htf_reason}")
                
                # Show detailed HTF values
                ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                adx_vals, _ = calculate_adx(df_1h['high'].values, df_1h['low'].values, df_1h['close'].values, 14)
                adx = adx_vals[-1] if len(adx_vals) > 0 else 0
                adx_slope = (adx_vals[-1] - adx_vals[-2]) if len(adx_vals) > 1 else 0
                
                logger.info(f"   📊 Price: ${current_price:.2f} | EMA50: ${ema_50:.2f} | EMA200: ${ema_200:.2f}")
                logger.info(f"   📈 ADX: {adx:.2f} | Slope: {adx_slope:.4f}")
                
                self._last_htf_reject_log_time = market_time
                self._last_htf_state = 'rejected'
            return
        
        self.current_bias = bias
        bias_label = f'{bias} (counter-trend)' if allow_countertrend else bias
        if self._last_htf_state != f'passed_{bias}':
            logger.info(f"✅ HTF filter passed - Bias: {bias_label}")
            self._last_htf_state = f'passed_{bias}'
        
        # ===== BLOQUE 1.5: LOCATION FILTER (MACRO VETO) =====
        # Check if location in market structure allows this trade direction
        # This is a HARD VETO that prevents buying tops in RANGE regime
        
        closes_1h = df_1h['close'].values
        highs_1h = df_1h['high'].values
        lows_1h = df_1h['low'].values
        volumes_1h = df_1h['volume'].values if 'volume' in df_1h.columns else np.ones(len(closes_1h))
        
        # Get ATR and ADX for location context
        atr_1h_vals = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
        atr_1h = atr_1h_vals[-1] if len(atr_1h_vals) > 0 else current_price * 0.01
        
        adx_1h_vals, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
        adx_1h = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0
        
        # Get current regime for location filter
        regime_for_location = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else "RANGE"
        
        # Calculate location context (cached by 1h bar close)
        location_cache_key = _ts_to_int(df_1h.iloc[-1].get('timestamp', 0))
        location_allowed, location_reason, self._location_context = self.location_filter.check(
            closes_1h=closes_1h,
            highs_1h=highs_1h,
            lows_1h=lows_1h,
            volumes_1h=volumes_1h,
            current_price=current_price,
            atr_1h=atr_1h,
            adx_1h=adx_1h,
            bias=bias,
            regime=regime_for_location,
            cache_key=location_cache_key
        )
        
        # Store location context in trade tags
        if self._location_context:
            self._current_trade_tags['location_pct_60d'] = self._location_context.percentile_60d
            self._current_trade_tags['location_headroom_atr'] = (
                self._location_context.headroom_to_high_atr if bias == 'LONG' 
                else self._location_context.headroom_to_low_atr
            )
            self._current_trade_tags['location_in_extreme'] = (
                self._location_context.in_extreme_high or self._location_context.in_extreme_low
            )
            self._current_trade_tags['location_is_breakout'] = (
                self._location_context.is_breakout_up or self._location_context.is_breakout_down
            )
        
        if not location_allowed:
            # =====================================================================
            # LOCATION FILTER BREAKOUT OVERRIDE (Jan 16, 2026)
            # Allow entry if HTF ADX is strong AND we have a valid BOS (breakout)
            # This prevents blocking genuine breakouts that look like "extreme" locations
            # =====================================================================
            breakout_override = False
            
            if self._location_context:
                is_breakout = self._location_context.is_breakout_up or self._location_context.is_breakout_down
                htf_adx_strong = adx_1h >= LocationFilterOverride.MIN_HTF_ADX
                
                # Check for BOS (Break of Structure) if required
                has_bos = True  # Default
                if LocationFilterOverride.REQUIRE_BOS:
                    # BOS = price breaking recent swing high/low
                    if bias == 'LONG' and self._location_context.is_breakout_up:
                        # Breaking above recent highs
                        has_bos = current_price > np.max(highs_1h[-5:]) if len(highs_1h) >= 5 else False
                    elif bias == 'SHORT' and self._location_context.is_breakout_down:
                        # Breaking below recent lows
                        has_bos = current_price < np.min(lows_1h[-5:]) if len(lows_1h) >= 5 else False
                
                if is_breakout and htf_adx_strong and has_bos:
                    breakout_override = True
                    self._current_trade_tags['location_breakout_override'] = True
                    self._current_trade_tags['breakout_adx'] = adx_1h
                    logger.info(f"🚀 LOCATION OVERRIDE: Breakout with ADX={adx_1h:.1f} (threshold={LocationFilterOverride.MIN_HTF_ADX})")
            
            if not breakout_override:
                # Log location veto (once per bias change)
                if not hasattr(self, '_last_location_state') or self._last_location_state != f'vetoed_{bias}':
                    logger.warning(f"📍 LOCATION VETO: {location_reason}")
                    if self._location_context:
                        logger.info(self.location_filter.get_location_summary(self._location_context))
                    self._last_location_state = f'vetoed_{bias}'
                
                # =====================================================================
                # PULLBACK SHORT ENGINE EVALUATION (Jan 14, 2026)
                # When LONG is blocked by location (price at extreme high), check if
                # pullback short conditions are met for tactical correction capture
                # =====================================================================
                if bias == 'LONG' and PullbackShortConfig.ENABLED:
                    await self._evaluate_pullback_short(
                        current_price=current_price,
                        df_1h=df_1h,
                        candles_5m=self.market_data.get_candles('5m'),
                        htf_bias=bias,
                        regime=regime_for_location,
                        market_time=market_time,
                        veto_reason=location_reason
                    )
                
                return
        else:
            self._last_location_state = f'allowed_{bias}'
            # Log location context on first pass or regime change
            if self._location_context and self._idle_eval_count % 60 == 0:
                logger.debug(self.location_filter.get_location_summary(self._location_context))
        
        # ===== BLOQUE 2: SKIPPED - MTF FILTER REMOVED =====
        # OPTIMIZATION FIX #1 (Jan 3, 2026):
        # Removed MTF (5m) Structure filter to enable EARLIER ENTRIES
        # Benefits:
        #   - Eliminates 10-15 minute delay
        #   - Increases R from 0.6R → 1.0-1.2R naturally
        #   - Adds 15-25% more trades
        # Trade-off: +5-10% more false entries (mitigated by tighter LTF filter)
        # 
        # BEFORE: HTF (1h) → MTF (5m) → LTF (1m) = 3 filters, slow entry
        # NOW:    HTF (1h) → LTF (1m) = 2 filters, fast entry
        
        # Find impulse high/low from last 10 1m candles (for reference only)
        candles_1m_for_impulse = self.market_data.get_candles('1m')
        if candles_1m_for_impulse and len(candles_1m_for_impulse) >= 10:
            self.impulse_high = max([c['high'] for c in candles_1m_for_impulse[-10:]])
            self.impulse_low = min([c['low'] for c in candles_1m_for_impulse[-10:]])
        else:
            self.impulse_high = current_price
            self.impulse_low = current_price
        
        # Log impulse only on state changes (not every evaluation)
        logger.debug(f"⚡ BLOQUE 2 SKIPPED (MTF removed) | Impulse High: ${self.impulse_high:.2f} | Low: ${self.impulse_low:.2f}")
        
        # ===== BLOQUE 2.5: MICRO TREND FILTER (5m) - NEW (Jan 15, 2026) =====
        # PROBLEM: Bot enters LONGs during micro-bearish trends within macro-bullish,
        # leading to consecutive SL hits that erode gains.
        # SOLUTION: Detect micro-trend (5m structure) and block/penalize opposing entries.
        #
        # Workflow:
        # 1. Check if micro-trend opposes HTF bias
        # 2. If HARD BLOCK: veto entry completely
        # 3. If SOFT BLOCK: apply score penalty (tracked in _micro_trend_state)
        # 4. Score penalty applied in _calculate_entry_score via score_adjustment
        
        candles_5m_list = self.market_data.get_candles('5m')
        candles_1m_list = self.market_data.get_candles('1m')
        
        if candles_5m_list and len(candles_5m_list) >= 60:
            df_5m_micro = pd.DataFrame(candles_5m_list)
            df_1m_micro = pd.DataFrame(candles_1m_list) if candles_1m_list and len(candles_1m_list) >= 30 else None
            
            micro_allowed, micro_reason, self._micro_trend_state = self.micro_trend_filter.check(
                df_5m=df_5m_micro,
                df_1m=df_1m_micro,
                current_price=current_price,
                htf_bias=bias,
                market_time=market_time
            )
            
            # Store micro trend state in trade tags for analysis
            if self._micro_trend_state:
                self._current_trade_tags['micro_trend'] = self._micro_trend_state.micro_trend
                self._current_trade_tags['micro_trend_strength'] = self._micro_trend_state.strength
                self._current_trade_tags['micro_ema20_5m'] = self._micro_trend_state.ema20_5m
                self._current_trade_tags['micro_ema50_5m'] = self._micro_trend_state.ema50_5m
                self._current_trade_tags['micro_rsi_5m'] = self._micro_trend_state.rsi_5m
                self._current_trade_tags['micro_score_adj'] = self._micro_trend_state.score_adjustment
                self._current_trade_tags['micro_has_ll'] = self._micro_trend_state.has_lower_low
            
            if not micro_allowed:
                # =====================================================================
                # MICRO TREND ADX CONDITIONING (Jan 16, 2026)
                # Only apply hard block if HTF trend is WEAK (low ADX)
                # In STRONG trends (ADX >= 30), micro-corrections are normal - allow entry
                # In WEAK trends (ADX < 20), micro-trend signals are more meaningful - block
                # =====================================================================
                adx_override = False
                
                if adx_1h >= MicroTrendADXConfig.STRONG_TREND_ADX:
                    # Strong HTF trend - override micro block, just apply penalty
                    adx_override = True
                    self._current_trade_tags['micro_adx_override'] = True
                    self._current_trade_tags['micro_htf_adx'] = adx_1h
                    logger.info(f"⚡ MICRO TREND override: HTF ADX={adx_1h:.1f} >= {MicroTrendADXConfig.STRONG_TREND_ADX} (strong trend)")
                    # Apply a score penalty instead of hard block
                    if self._micro_trend_state:
                        self._micro_trend_state.score_adjustment = -1.0  # Penalty instead of block
                elif adx_1h < MicroTrendADXConfig.WEAK_TREND_ADX:
                    # Weak HTF trend - micro signals are significant, apply full block
                    logger.info(f"⛔ MICRO TREND: HTF ADX={adx_1h:.1f} < {MicroTrendADXConfig.WEAK_TREND_ADX} (weak trend, full block)")
                
                if not adx_override:
                    # Log micro trend veto (once per state change)
                    if not hasattr(self, '_last_micro_state') or self._last_micro_state != f'vetoed_{bias}':
                        logger.warning(f"⛔ MICRO TREND VETO: {micro_reason}")
                        logger.info(f"   📊 5m Structure: EMA20={self._micro_trend_state.ema20_5m:.0f} | EMA50={self._micro_trend_state.ema50_5m:.0f} | RSI={self._micro_trend_state.rsi_5m:.1f}")
                        logger.info(f"   📉 Signals: LL={self._micro_trend_state.has_lower_low} | LH={self._micro_trend_state.has_lower_high} | BelowEMA20={self._micro_trend_state.is_below_ema20}")
                        self._last_micro_state = f'vetoed_{bias}'
                    return
            else:
                self._last_micro_state = f'allowed_{bias}'
                if self._micro_trend_state and self._micro_trend_state.score_adjustment != 0:
                    logger.debug(f"📊 MICRO TREND: {self._micro_trend_state.micro_trend} (strength={self._micro_trend_state.strength:.0%}, score_adj={self._micro_trend_state.score_adjustment:+.1f})")
        else:
            # Not enough data - allow but log
            self._micro_trend_state = None
            logger.debug("⚠️ MICRO TREND: Insufficient 5m data - skipping filter")
        
        # ===== BLOQUE 2.6: EMA DISTANCE GUARD (Jan 15, 2026) =====
        # PROBLEM: Bot enters LONGs when price is significantly below EMA20/EMA50 on 5m,
        # which indicates strong bearish micro-structure. MicroTrendFilter hysteresis 
        # may not catch fast moves.
        # SOLUTION: Hard block if price is >1% below EMA20 for LONG (or >1% above for SHORT)
        # This is a FAIL-SAFE to prevent entries against obvious micro-structure.
        #
        # Why 1%? On BTC at 95000, 1% = $950. If we're $950+ below EMA20 (5m),
        # we're clearly in a pullback/correction - not a good LONG entry point.
        
        EMA_DISTANCE_BLOCK_PCT = 0.01  # 1% threshold
        EMA_DISTANCE_WARNING_PCT = 0.005  # 0.5% warning (apply score penalty)
        
        if 'df_5m_micro' in dir() and df_5m_micro is not None and len(df_5m_micro) >= 50:
            closes_5m = df_5m_micro['close'].values
            ema20_5m_val = calculate_ema(closes_5m, 20)[-1]
            ema50_5m_val = calculate_ema(closes_5m, 50)[-1]
            
            if not np.isnan(ema20_5m_val) and not np.isnan(ema50_5m_val) and ema20_5m_val > 0:
                if bias == 'LONG':
                    # For LONG: Block if price is significantly BELOW EMAs
                    distance_to_ema20 = (current_price - ema20_5m_val) / ema20_5m_val
                    distance_to_ema50 = (current_price - ema50_5m_val) / ema50_5m_val
                    
                    # HARD BLOCK: Price >1% below EMA20 AND below EMA50
                    if distance_to_ema20 < -EMA_DISTANCE_BLOCK_PCT and distance_to_ema50 < 0:
                        logger.warning(
                            f"⛔ EMA DISTANCE VETO: LONG blocked - price {current_price:.0f} is "
                            f"{abs(distance_to_ema20)*100:.1f}% below EMA20({ema20_5m_val:.0f}) "
                            f"and {abs(distance_to_ema50)*100:.1f}% below EMA50({ema50_5m_val:.0f})"
                        )
                        self._current_trade_tags['ema_distance_veto'] = True
                        self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                        self._current_trade_tags['ema50_distance_pct'] = distance_to_ema50 * 100
                        self._set_rejection_cooldown("EMA distance veto - price too far below EMAs", candles=5)
                        return
                    
                    # WARNING: Price 0.5-1% below EMA20 - apply extra penalty (handled in micro_trend)
                    elif distance_to_ema20 < -EMA_DISTANCE_WARNING_PCT:
                        logger.info(
                            f"⚠️ EMA DISTANCE WARNING: LONG entry {abs(distance_to_ema20)*100:.1f}% "
                            f"below EMA20 - may face headwind"
                        )
                        self._current_trade_tags['ema_distance_warning'] = True
                        self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                
                elif bias == 'SHORT':
                    # For SHORT: Block if price is significantly ABOVE EMAs
                    distance_to_ema20 = (current_price - ema20_5m_val) / ema20_5m_val
                    distance_to_ema50 = (current_price - ema50_5m_val) / ema50_5m_val
                    
                    # HARD BLOCK: Price >1% above EMA20 AND above EMA50
                    if distance_to_ema20 > EMA_DISTANCE_BLOCK_PCT and distance_to_ema50 > 0:
                        logger.warning(
                            f"⛔ EMA DISTANCE VETO: SHORT blocked - price {current_price:.0f} is "
                            f"{distance_to_ema20*100:.1f}% above EMA20({ema20_5m_val:.0f}) "
                            f"and {distance_to_ema50*100:.1f}% above EMA50({ema50_5m_val:.0f})"
                        )
                        self._current_trade_tags['ema_distance_veto'] = True
                        self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                        self._current_trade_tags['ema50_distance_pct'] = distance_to_ema50 * 100
                        self._set_rejection_cooldown("EMA distance veto - price too far above EMAs", candles=5)
                        return
        
        # ===== BLOQUE 2.7: UNIFIED FILTER PIPELINE (Jan 15, 2026) =====
        # Runs all new filters: BB Width, Volatility Shock, Macro Trend, 
        # Post-Sweep, Cost, Overtrade
        # NOTE: System doesn't have 15m candles, using 5m as proxy for intermediate TF
        df_15m_proxy = df_5m_micro if 'df_5m_micro' in dir() and df_5m_micro is not None else None
        
        # Calculate spread from order book if available
        spread = None
        if hasattr(self, 'order_book') and self.order_book:
            bid_price, ask_price, spread_pct = self.order_book.get_spread()
            if bid_price and ask_price:
                # Update spread filter with bid/ask for rolling window analysis
                self.spread_filter.update_spread(bid_price, ask_price, current_price)
                spread = spread_pct  # Use the percentage for other calculations
        
        filter_decision = self.filter_pipeline.evaluate(
            df_1m=df_1m_micro if 'df_1m_micro' in dir() else None,
            df_5m=df_5m_micro if 'df_5m_micro' in dir() else None,
            df_15m=df_15m_proxy,
            df_1h=df_1h,
            current_price=current_price,
            bias=bias,
            atr=atr_1m,
            spread=spread,
            market_time=market_time
        )
        
        # Store filter pipeline tags
        if filter_decision.tags:
            for key, value in filter_decision.tags.items():
                self._current_trade_tags[f'fp_{key}'] = value
        
        if not filter_decision.allow:
            # Log which filter blocked
            blocker = filter_decision.tags.get('blocked_by', 'unknown')
            if not hasattr(self, '_last_fp_block') or self._last_fp_block != f'{blocker}_{bias}':
                logger.warning(f"⛔ FILTER PIPELINE BLOCK [{blocker}]: {filter_decision.reasons}")
                self._last_fp_block = f'{blocker}_{bias}'
            return
        else:
            self._last_fp_block = None
            if filter_decision.score_adjust != 0:
                logger.debug(f"📊 FILTER PIPELINE: score_adj={filter_decision.score_adjust:+.1f}")
        
        # ===== BLOQUE 3: LTF TRIGGER (1m) =====
        logger.debug("Evaluating LTF trigger (1m)...")
        candles_1m = self.market_data.get_candles('1m')
        df_1m = pd.DataFrame(candles_1m)
        
        # Get regime info for volume-aware filtering
        regime_value = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else None
        regime_conf = self.regime_classifier.regime_confidence
        
        ltf_passed, ltf_reason = self.ltf_filter.check(
            df_1m,
            bias,
            {},  # Order book would go here but we'll use execution for real book
            regime=regime_value,
            regime_confidence=regime_conf
        )
        
        # ===== STORE LTF INFO AS TAGS (regardless of pass/fail) =====
        ltf_info = self.ltf_filter.get_last_ltf_info()
        if ltf_info:
            self._current_trade_tags['ltf_bos'] = ltf_info.get('bos_confirmed', False)
            self._current_trade_tags['ltf_ema_reclaim'] = ltf_info.get('ema_reclaim', False)
            self._current_trade_tags['ltf_slope_positive'] = ltf_info.get('slope_positive', False)
            self._current_trade_tags['ltf_confirmations'] = ltf_info.get('confirmations', 0)
            self._current_trade_tags['ltf_ema_slope'] = ltf_info.get('ema_slope', 0.0)
            # Volume tags (never blocks, only info)
            self._current_trade_tags['rel_vol'] = ltf_info.get('rel_vol', 1.0)
            self._current_trade_tags['volume_bucket'] = ltf_info.get('volume_bucket', 'unknown')
        
        if not ltf_passed:
            if self._last_ltf_state != f'rejected_{ltf_reason}':
                logger.info(f"⛔ LTF filter rejected: {ltf_reason}")
                
                # Show detailed LTF values
                ema_9_ltf = df_1m['close'].ewm(span=9, adjust=False).mean().iloc[-1]
                ema_20_ltf = df_1m['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                
                logger.info(f"   📊 Price: ${current_price:.2f} | EMA9: ${ema_9_ltf:.2f} | EMA20: ${ema_20_ltf:.2f}")
                logger.info(f"   🎯 Bias: {bias} | Impulse High: ${self.impulse_high:.2f} | Low: ${self.impulse_low:.2f}")
                
                self._last_ltf_state = f'rejected_{ltf_reason}'
            return
        
        if self._last_ltf_state != 'passed':
            logger.info(f"✅ LTF filter passed")
            self._last_ltf_state = 'passed'
        
        # ===== ALL FILTERS PASSED - PRE-CHECK EDGE CONTEXT =====
        # Note: LTF is "score-based" not "gate-based" so we show actual confirmations
        ltf_confs = self._current_trade_tags.get('ltf_confirmations', '?')
        
        # Quick edge context check BEFORE showing "SETUP COMPLETE"
        # This prevents confusing logs where setup appears complete but is then blocked
        if self._edge_context and not self._edge_context.is_valid():
            logger.info(f"🎯 FILTERS PASSED - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3) | ⚠️ Edge context pending validation")
        else:
            logger.info(f"🎯 SETUP COMPLETE - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3)")
        self._current_trade_type = TradeType.LONG_MAIN if bias == 'LONG' else TradeType.SHORT_STRUCTURAL
        try:
            await self._execute_entry(current_price, atr_1m, market_time)
        except Exception as e:
            logger.error(f"⛔ _execute_entry EXCEPTION: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _evaluate_pullback_short(
        self,
        current_price: float,
        df_1h: pd.DataFrame,
        candles_5m: list,
        htf_bias: str,
        regime: str,
        market_time: datetime,
        veto_reason: str = ""
    ):
        """
        Evaluate and potentially execute a pullback short signal.
        
        This is called when LONG is blocked by location filter (at extreme high).
        The pullback short engine checks if correction capture conditions are met.
        
        Args:
            current_price: Current market price
            df_1h: 1H candle DataFrame
            candles_5m: 5M candle list
            htf_bias: Current HTF bias ('LONG')
            regime: Current market regime
            market_time: Current market time
            veto_reason: Why the main signal was vetoed
        """
        # Check if pullback shorts are enabled (both config and settings)
        try:
            from src.core.settings import get_settings
            settings = get_settings()
            if not settings.enable_short_pullback:
                logger.debug("[PULLBACK] Disabled in settings")
                return
            
            if settings.is_live_mode and not settings.short_pullback_live_allowed:
                logger.debug("[PULLBACK] Not allowed in LIVE mode")
                return
        except Exception as e:
            logger.debug(f"[PULLBACK] Could not check settings: {e}")
            if not PullbackShortConfig.ENABLED:
                return
        
        # Prepare 5m DataFrame
        if not candles_5m or len(candles_5m) < 20:
            logger.debug("[PULLBACK] Insufficient 5m data")
            return
        
        df_5m = pd.DataFrame(candles_5m)
        
        # Get 1m data (optional for fine triggers)
        candles_1m = self.market_data.get_candles('1m')
        df_1m = pd.DataFrame(candles_1m) if candles_1m and len(candles_1m) >= 20 else None
        
        # Check for integrity errors
        has_integrity_errors = False
        if self.risk_engine.has_open_position() and hasattr(self.risk_engine.position, 'integrity_errors'):
            has_integrity_errors = len(self.risk_engine.position.integrity_errors) > 0
        
        # Check for macro events
        has_macro_event = self.external_data.has_high_impact_event() if hasattr(self.external_data, 'has_high_impact_event') else False
        
        # Evaluate pullback short signal
        signal = self.pullback_short_engine.evaluate(
            current_price=current_price,
            df_1h=df_1h,
            df_5m=df_5m,
            df_1m=df_1m,
            htf_bias=htf_bias,
            regime=regime,
            market_time=market_time,
            has_integrity_errors=has_integrity_errors,
            has_macro_event=has_macro_event
        )
        
        if not signal.valid:
            # Log block reason (throttled to avoid spam)
            if not hasattr(self, '_last_pullback_block_log') or \
               (market_time - self._last_pullback_block_log).total_seconds() > 60:
                logger.info(f"[PULLBACK] Signal blocked: {signal.block_reason}")
                self._last_pullback_block_log = market_time
            
            # 📊 METRICS: Track blocked pullback signals
            update_pullback_short_signal(self.symbol, blocked=True, reason=signal.block_reason or "unknown")
            return
        
        # 📊 METRICS: Track valid pullback signal
        update_pullback_short_signal(self.symbol, blocked=False)
        
        # =====================================================================
        # VALID PULLBACK SHORT SIGNAL - EXECUTE
        # =====================================================================
        logger.info(
            f"🎯 PULLBACK SHORT SIGNAL VALID | "
            f"Entry={signal.entry_price:.2f} | SL={signal.stop_loss:.2f} | TP1={signal.tp1_price:.2f}"
        )
        
        # Set trade type
        self._current_trade_type = TradeType.SHORT_PULLBACK
        self.current_bias = 'SHORT'
        
        # Store signal in tags
        self._current_trade_tags['trade_type'] = 'SHORT_PULLBACK'
        self._current_trade_tags['pullback_ext_z'] = signal.extension_z
        self._current_trade_tags['pullback_trigger'] = signal.trigger_type
        self._current_trade_tags['pullback_swing_high'] = signal.swing_high
        self._current_trade_tags['pullback_activity_score'] = signal.activity_score
        self._current_trade_tags['pullback_size_factor'] = signal.size_factor
        self._current_trade_tags['veto_reason_original'] = veto_reason
        
        # Copy signal tags
        for key, value in signal.tags.items():
            self._current_trade_tags[f'pullback_{key}'] = value
        
        # Calculate ATR for entry
        atr_1m = self._calculate_atr_1m(df_1m) if df_1m is not None else 100.0
        
        # Execute entry with pullback-specific parameters
        await self._execute_pullback_short_entry(
            signal=signal,
            atr_1m=atr_1m,
            market_time=market_time
        )
    
    async def _execute_pullback_short_entry(
        self,
        signal: PullbackShortSignal,
        atr_1m: float,
        market_time: datetime
    ):
        """
        Execute a pullback short entry with specific risk parameters.
        
        This is separate from _execute_entry to maintain distinct risk management.
        """
        # ==================================================================
        # 🔒 SAME SAFETY CHECKS AS MAIN ENTRY
        # ==================================================================
        
        if self.pending_order_id:
            logger.critical(f"⛔ PULLBACK ABORTED: Pending order exists")
            return
        
        if self.risk_engine.has_open_position():
            logger.critical("⛔ PULLBACK ABORTED: Position already open")
            return
        
        if self.state_machine.state != BotState.IDLE:
            logger.critical(f"⛔ PULLBACK ABORTED: State is {self.state_machine.state.name}")
            return
        
        # ==================================================================
        # POSITION SIZING - Reduced for pullback shorts
        # ==================================================================
        
        capital = self.risk_engine.capital
        if capital <= 0:
            logger.error("⛔ PULLBACK ABORTED: No capital available")
            return
        
        # Calculate base risk
        stop_distance = abs(signal.stop_loss - signal.entry_price)
        if stop_distance <= 0:
            logger.error("⛔ PULLBACK ABORTED: Invalid stop distance")
            return
        
        # Risk amount (reduced for pullback shorts)
        risk_pct = OperatingMode.get_risk_pct() / 100.0
        risk_pct_adjusted = risk_pct * signal.size_factor  # Apply pullback reduction
        risk_amount = capital * risk_pct_adjusted
        
        # Position size
        qty = risk_amount / stop_distance
        
        # Apply notional cap
        max_notional = min(PullbackShortConfig.MAX_NOTIONAL_USD, self.risk_engine.capital * 0.3)
        max_qty = max_notional / signal.entry_price
        qty = min(qty, max_qty)
        
        # Round to exchange precision
        qty = round_size_to_exchange(qty)
        
        if qty < KRAKEN_BTC_MIN_SIZE:
            logger.warning(f"⛔ PULLBACK ABORTED: Size {qty} below minimum {KRAKEN_BTC_MIN_SIZE}")
            return
        
        # ==================================================================
        # PLACE ORDER - RETEST (LIMIT) or MARKET mode
        # ==================================================================
        
        entry_mode = signal.entry_mode
        order_type = 'limit' if entry_mode == 'RETEST' else 'market'
        order_price = signal.retest_level if entry_mode == 'RETEST' else None
        
        logger.info(
            f"📉 PULLBACK SHORT ENTRY | Mode={entry_mode} | "
            f"Size={qty:.6f} BTC @ ${signal.entry_price:.2f} | "
            f"SL=${signal.stop_loss:.2f} | TP1=${signal.tp1_price:.2f} | "
            f"Risk=${risk_amount:.2f} ({risk_pct_adjusted*100:.2f}%)"
        )
        
        # Transition to wait_fill
        self.state_machine.transition_to(BotState.WAIT_FILL, f"Pullback short entry ({entry_mode})")
        
        try:
            # Use correct method based on order type
            if order_type == 'limit':
                order = await self.execution.place_limit_order(
                    symbol=self.symbol,
                    side='sell',
                    amount=qty,
                    price=order_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.tp1_price
                )
            else:
                order = await self.execution.place_market_order(
                    symbol=self.symbol,
                    side='sell',
                    amount=qty,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.tp1_price
                )
            
            if order and order.get('id'):
                self.pending_order_id = order['id']
                self.entry_order_time = market_time
                
                # Store entry data with all pullback-specific fields
                self._last_entry_data = {
                    'bias': 'SHORT',
                    'trade_type': TradeType.SHORT_PULLBACK,
                    'entry_price': signal.entry_price,
                    'position_size': qty,
                    'stop_loss': signal.stop_loss,
                    'tp1_price': signal.tp1_price,
                    'tp2_price': signal.tp2_price,
                    'size': qty,
                    'swing_high': signal.swing_high,
                    'confirm_level': signal.confirm_level,
                    'extension_z': signal.extension_z,
                    'trigger_type': signal.trigger_type,
                    'entry_mode': entry_mode,
                    'retest_level': signal.retest_level,
                    'order_type': order_type.upper(),
                    'atr_1m': atr_1m,  # Needed for position creation
                    'activity_score': signal.activity_score
                }
                
                # 📊 METRICS: Track pullback short entry
                update_pullback_short_entry(self.symbol, extension_z=signal.extension_z)
                
                logger.info(f"✅ PULLBACK SHORT ORDER PLACED ({order_type.upper()}): {order['id']}")
            else:
                logger.error("⛔ PULLBACK ORDER FAILED: No order ID returned")
                self.state_machine.transition_to(BotState.IDLE, "Pullback order failed")
                
        except Exception as e:
            logger.error(f"⛔ PULLBACK ORDER EXCEPTION: {e}")
            self.state_machine.transition_to(BotState.IDLE, f"Pullback order exception: {e}")
    
    def _calculate_atr_1m(self, df_1m: pd.DataFrame) -> float:
        """Calculate ATR from 1m data"""
        if df_1m is None or len(df_1m) < 14:
            return 100.0
        try:
            atr_vals = calculate_atr(
                df_1m['high'].values,
                df_1m['low'].values,
                df_1m['close'].values,
                14
            )
            return atr_vals[-1] if len(atr_vals) > 0 and not np.isnan(atr_vals[-1]) else 100.0
        except Exception as e:
            logger.debug(f"ATR calculation failed (using default 100.0): {e}")
            return 100.0

    async def _execute_entry(self, current_price: float, atr_1m: float, market_time: datetime):
        """Execute entry with strict safety checks"""
        # ==================================================================
        # 🔒 FINAL GATEKEEPER - STRICT CONCURRENCY CHECK
        # ==================================================================
        
        # 1. Check internal pending order
        if self.pending_order_id:
            logger.critical(f"⛔ EXECUTION ABORTED: Pending order {self.pending_order_id} already exists")
            self.state_machine.transition_to(BotState.WAIT_FILL, "Recovered pending order state")
            return

        # 2. Check risk engine position
        if self.risk_engine.has_open_position():
            logger.critical("⛔ EXECUTION ABORTED: Risk Engine reports open position")
            self.state_machine.transition_to(BotState.MANAGE, "Recovered open position state")
            return

        # 3. Check execution engine open orders (Double Check)
        # NOTE: Exclude SL/TP orders (protective orders on existing positions)
        if hasattr(self.execution, 'open_orders') and self.execution.open_orders:
            # Filter out protective orders (stop-loss, take-profit types)
            non_protective_orders = {
                oid: order for oid, order in self.execution.open_orders.items()
                if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
            }
            
            if non_protective_orders:
                logger.critical(f"⛔ EXECUTION ABORTED: Execution Engine reports {len(non_protective_orders)} open entry orders")
                logger.info("🔄 Attempting order reconciliation...")
                
                # Intentar reconciliar automáticamente
                await self._reconcile_orders()
                
                # Verificar nuevamente después de reconciliación (re-filter)
                non_protective_orders_after = {
                    oid: order for oid, order in self.execution.open_orders.items()
                    if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                }
                
                if non_protective_orders_after:
                    logger.critical(f"⛔ Still {len(non_protective_orders_after)} entry orders after reconciliation - aborting")
                    return
                else:
                    logger.info("✅ Orders cleaned - proceeding with entry")
            else:
                logger.debug(f"ℹ️ {len(self.execution.open_orders)} protective orders (SL/TP) detected - OK to proceed")
            
        # 4. Check State Machine (Must be IDLE to enter)
        if self.state_machine.state != BotState.IDLE:
             logger.critical(f"⛔ EXECUTION ABORTED: State is {self.state_machine.state.name}, not IDLE")
             return

        # 5. STRICT EXCHANGE CHECK (Network call - ONLY FOR LIVE)
        # In backtest mode, skip this check entirely - MockExchange handles it
        if not self._is_backtest:
            try:
                logger.info("🔒 Performing STRICT exchange check before order placement...")
                
                # Check open orders - but EXCLUDE protective orders (SL/TP)
                open_orders = await self.execution.get_open_orders(self.symbol)
                if open_orders and len(open_orders) > 0:
                    # Filter out protective orders
                    non_protective_orders = [
                        order for order in open_orders
                        if order.get('type') not in ['stop', 'stop-loss', 'take-profit', 'stop_loss']
                    ]
                    
                    if non_protective_orders:
                        logger.critical(f"⛔ EXECUTION ABORTED: Exchange reports {len(non_protective_orders)} open entry orders")
                        return
                    else:
                        logger.debug(f"ℹ️ Exchange has {len(open_orders)} protective orders (SL/TP) - OK to proceed")

                # Check position
                position = await self.execution.get_position(self.symbol)
                
                # CRITICAL: Check if API returned an error (not "no position")
                if position and position.get('_error'):
                    logger.error(f"⛔ EXECUTION ABORTED: Cannot verify position state (API error)")
                    return
                
                if position and abs(float(position.get('contracts', 0))) > 0.00001:
                    logger.critical(f"⛔ EXECUTION ABORTED: Exchange reports open position: {position.get('contracts')} contracts")
                    self.state_machine.transition_to(BotState.MANAGE, "Recovered open position from exchange")
                    
                    # Get ATR for proper SL calculation
                    candles_5m = self.market_data.get_candles('5m')
                    atr_5m = self._calculate_atr_5m(pd.DataFrame(candles_5m)) if candles_5m else None
                    await self.risk_engine.update_from_exchange_position(position, atr=atr_5m)
                    return
                    
            except Exception as e:
                 logger.error(f"Error during strict exchange check: {e}")
                 # If check fails, we abort for safety
                 logger.critical("⛔ EXECUTION ABORTED: Failed to verify exchange state")
                 return
        else:
            logger.debug("📋 [BACKTEST] Skipping STRICT exchange check")

        # ==================================================================
        # 🛡️ EDGE ENFORCEMENT - PRE-TRADE VALIDATION
        # ==================================================================
        
        # Update edge context if needed
        await self._update_edge_context()
        
        if self._edge_context is not None:
            # Calculate current drawdown from RiskEngine (source of truth for equity/drawdown)
            current_drawdown = self.risk_engine.get_drawdown_pct() / 100.0 if hasattr(self.risk_engine, 'get_drawdown_pct') else 0.0
            
            # Validate pre-trade with edge enforcement (simplified method)
            can_trade, blockers = self.edge_enforcement.validate_pre_trade_simple(
                context=self._edge_context,
                current_drawdown=current_drawdown
            )
            
            if not can_trade:
                logger.warning(f"⛔ EDGE ENFORCEMENT BLOCKED TRADE: {blockers}")
                # Track missed opportunity
                current_price = self.market_data.get_current_price() or 0.0
                atr = self._last_momentum_atr if hasattr(self, '_last_momentum_atr') else 100.0
                self.missed_tracker.log_blocked_signal(
                    direction=self.current_bias or 'UNKNOWN',
                    price=current_price,
                    block_reason=f"Edge enforcement: {', '.join(blockers)}",
                    block_category='edge_enforcement',
                    score=self._entry_score,
                    atr=atr
                )
                return
            
            # Apply position size multiplier from edge enforcement
            edge_size_multiplier = self.edge_enforcement.get_position_size_multiplier()
            if edge_size_multiplier < 1.0:
                logger.info(f"📉 Edge enforcement reducing position size to {edge_size_multiplier*100:.0f}%")
        else:
            edge_size_multiplier = 1.0
            logger.debug("Edge context not available - using full size")
        
        # ==================================================================
        
        bias = self.current_bias
        
        # Get market time for cooldown checks
        market_time = self.market_data.get_market_time()
        
        # Get 5m data for support/resistance
        candles_5m = self.market_data.get_candles('5m')
        if not candles_5m or len(candles_5m) < 20:
            logger.warning("Not enough 5m data for entry execution")
            return
        df_5m = pd.DataFrame(candles_5m)
        
        # Get 1m data for volatility check
        candles_1m = self.market_data.get_candles('1m')
        if not candles_1m or len(candles_1m) < 200:
            logger.debug("Not enough 1m data for volatility filter")
            df_1m = pd.DataFrame(candles_1m) if candles_1m else None
        else:
            df_1m = pd.DataFrame(candles_1m)
        
        # ==================================================================
        # 🚫 GLOBAL GUARDS - REJECT BEFORE WASTING COMPUTE ON SCORING
        # ==================================================================
        
        # ==================================================================
        # 🔥 GUARD 0: EARLY LOCATION FILTER (Jan 14, 2026)
        # "Location decides if you play. Scoring decides quality."
        # 
        # Rule: If REGIME = RANGE and headroom < 0.75 ATR → INVALIDATE SETUP
        # This is BEFORE scoring to avoid wasting CPU on doomed setups.
        # A human trader doesn't even evaluate a long at the ceiling.
        # ==================================================================
        
        # Get ATR for headroom calculations
        atr_for_headroom = self._last_momentum_atr or 235.0  # Default fallback
        
        # Check 1: Anchor levels (YTD high/low) - most important constraint
        # These are the real ceilings/floors that collapse RR
        if hasattr(self, 'important_levels') and self.important_levels is not None:
            try:
                anchor_levels = self.important_levels.get_relevant_levels(current_price, bias)
                
                if anchor_levels:
                    if bias == 'LONG':
                        # For LONG, check distance to nearest resistance above
                        nearest_resistance = None
                        for level in anchor_levels:
                            if level.price > current_price:
                                if nearest_resistance is None or level.price < nearest_resistance:
                                    nearest_resistance = level.price
                        
                        if nearest_resistance is not None:
                            headroom_to_resistance = nearest_resistance - current_price
                            headroom_atr = headroom_to_resistance / atr_for_headroom if atr_for_headroom > 0 else 0
                            
                            # FIX: In RANGE, if < 0.75 ATR to major resistance, skip
                            regime_check = self._location_context.regime.upper() if hasattr(self, '_location_context') and self._location_context else "RANGE"
                            if regime_check in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']:
                                if headroom_atr < 0.75:
                                    logger.warning(
                                        f"🚫 EARLY ANCHOR VETO: LONG with only {headroom_atr:.2f} ATR "
                                        f"to resistance @ ${nearest_resistance:.0f} in {regime_check} → SETUP INVALIDATED"
                                    )
                                    self._set_rejection_cooldown("Anchor veto - too close to resistance", candles=5)
                                    return
                                    
                    elif bias == 'SHORT':
                        # For SHORT, check distance to nearest support below
                        nearest_support = None
                        for level in anchor_levels:
                            if level.price < current_price:
                                if nearest_support is None or level.price > nearest_support:
                                    nearest_support = level.price
                        
                        if nearest_support is not None:
                            headroom_to_support = current_price - nearest_support
                            headroom_atr = headroom_to_support / atr_for_headroom if atr_for_headroom > 0 else 0
                            
                            regime_check = self._location_context.regime.upper() if hasattr(self, '_location_context') and self._location_context else "RANGE"
                            if regime_check in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']:
                                if headroom_atr < 0.75:
                                    logger.warning(
                                        f"🚫 EARLY ANCHOR VETO: SHORT with only {headroom_atr:.2f} ATR "
                                        f"to support @ ${nearest_support:.0f} in {regime_check} → SETUP INVALIDATED"
                                    )
                                    self._set_rejection_cooldown("Anchor veto - too close to support", candles=5)
                                    return
            except Exception as e:
                logger.debug(f"Anchor level check skipped: {e}")
        
        # Check 2: 60d range location context (secondary check)
        if hasattr(self, '_location_context') and self._location_context is not None:
            location_ctx = self._location_context
            regime_for_early_filter = location_ctx.regime.upper() if hasattr(location_ctx, 'regime') else "RANGE"
            
            # Only apply in RANGE regime (trend regimes can trade at extremes)
            if regime_for_early_filter in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']:
                # Check headroom for the intended direction
                if bias == 'LONG':
                    headroom_atr = location_ctx.headroom_to_high_atr
                    headroom_ok = headroom_atr >= 0.75
                    
                    if not headroom_ok:
                        logger.warning(
                            f"🚫 EARLY LOCATION VETO: LONG in {regime_for_early_filter} with only "
                            f"{headroom_atr:.2f} ATR headroom (need ≥0.75) → SETUP INVALIDATED"
                        )
                        # Track as early veto (not a scoring miss)
                        self.missed_tracker.log_blocked_signal(
                            direction=bias,
                            price=current_price,
                            block_reason=f"Early location veto: {headroom_atr:.2f} ATR headroom in RANGE",
                            block_category='location',
                            score=0,  # Not scored
                            atr=self._last_momentum_atr
                        )
                        self._set_rejection_cooldown("Location veto - ceiling in RANGE", candles=3)
                        return
                        
                elif bias == 'SHORT':
                    headroom_atr = location_ctx.headroom_to_low_atr
                    headroom_ok = headroom_atr >= 0.75
                    
                    if not headroom_ok:
                        logger.warning(
                            f"🚫 EARLY LOCATION VETO: SHORT in {regime_for_early_filter} with only "
                            f"{headroom_atr:.2f} ATR headroom (need ≥0.75) → SETUP INVALIDATED"
                        )
                        self.missed_tracker.log_blocked_signal(
                            direction=bias,
                            price=current_price,
                            block_reason=f"Early location veto: {headroom_atr:.2f} ATR headroom in RANGE",
                            block_category='location',
                            score=0,
                            atr=self._last_momentum_atr
                        )
                        self._set_rejection_cooldown("Location veto - floor in RANGE", candles=3)
                        return
        
        # GUARD 1: ADX coherence - HTF trending but LTF dead = false signal
        candles_1h = self.market_data.get_candles('1h')
        if candles_1h and len(candles_1h) >= 20:
            df_1h_temp = pd.DataFrame(candles_1h)
            adx_1h, _ = calculate_adx(df_1h_temp['high'].values, df_1h_temp['low'].values, df_1h_temp['close'].values, 14)
            adx_5m, _ = calculate_adx(df_5m['high'].values, df_5m['low'].values, df_5m['close'].values, 14)
            
            if len(adx_1h) > 0 and len(adx_5m) > 0:
                htf_adx = adx_1h[-1]
                ltf_adx = adx_5m[-1]
                
                # HTF shows trend but LTF is dead → wait for LTF to catch up
                if htf_adx > 20 and ltf_adx < 12:
                    logger.warning(f"🚫 ADX DIVERGENCE: HTF ADX={htf_adx:.1f} trending but LTF ADX={ltf_adx:.1f} dead → skip")
                    return
        
        # GUARD 2: Consecutive POST_ONLY rejections → market too fast for limit orders
        if hasattr(self, '_consecutive_post_only_rejects'):
            if self._consecutive_post_only_rejects >= 3:
                logger.warning(f"🚫 MARKET TOO FAST: {self._consecutive_post_only_rejects} consecutive POST_ONLY rejections → cooldown")
                self._consecutive_post_only_rejects = 0
                self.state_machine.start_cooldown(5)  # 5 min cooldown
                return
        
        # GUARD 3: Check TP multiplier is valid (sanity check)
        if RiskManagement.TP1_RISK_MULTIPLE < OperatingMode.RR_HARD_FLOOR:
            logger.error(f"🚫 CONFIG ERROR: TP1_RISK_MULTIPLE ({RiskManagement.TP1_RISK_MULTIPLE}) < RR_HARD_FLOOR ({OperatingMode.RR_HARD_FLOOR})")
            return
        
        # GUARD 4: Funding rate check (CRITICAL - prevents instant exit after entry)
        # FIX: check_funding_rate_safe was imported but NEVER USED - causing immediate exits
        if not self._is_backtest:
            try:
                funding_ok, funding_reason = await check_funding_rate_safe(
                    self.external_data,
                    self.symbol,
                    bias
                )
                if not funding_ok:
                    logger.warning(f"🚫 FUNDING UNSAFE: {funding_reason} → skip entry")
                    return
            except Exception as e:
                logger.warning(f"Could not check funding rate: {e} - proceeding with caution")
        
        # ==================================================================
        # GUARD 5: Spread Filter (blocks excessive spread, penalizes elevated)
        # ==================================================================
        session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
        spread_result = self.spread_filter.check_entry_allowed(session_name)
        
        if not spread_result.allowed:
            logger.warning(f"🚫 SPREAD BLOCK: {spread_result.reason} → skip entry")
            self._current_trade_tags['skip_reason'] = 'spread_too_wide'
            self._current_trade_tags['spread_block_reason'] = spread_result.reason
            self.missed_tracker.log_blocked_signal(
                direction=bias,
                price=current_price,
                block_reason=f"Spread filter: {spread_result.reason}",
                block_category='spread',
                score=0,  # Not yet calculated
                atr=self._last_momentum_atr
            )
            self._set_rejection_cooldown("Spread too wide", candles=1)
            return
        
        # Store spread penalty for score adjustment later
        _spread_score_penalty = spread_result.score_penalty
        _spread_size_factor = spread_result.size_factor
        if spread_result.score_penalty > 0:
            self._current_trade_tags['spread_penalty'] = spread_result.score_penalty
            logger.info(f"⚠️ SPREAD ELEVATED: penalty={spread_result.score_penalty}pts, size_mult={spread_result.size_factor:.2f}")
        
        # ==================================================================
        # GUARD 6: Session Trade Limits (prevents overtrading)
        # ==================================================================
        # Update session if changed
        if session_name != self._last_session:
            self._last_session = session_name
            # Don't reset counts - they persist per named session
        
        current_session_trades = self._session_trade_counts.get(session_name, 0)
        session_limit = SessionTradeLimits.MAX_TRADES_PER_SESSION.get(session_name, 5)
        
        # Check if at limit (override check happens after score calculation)
        self._at_session_limit = current_session_trades >= session_limit
        if self._at_session_limit:
            logger.debug(f"📊 Session {session_name}: {current_session_trades}/{session_limit} trades (at limit)")
        
        # ==================================================================
        # GUARD 7: Volatility Regime Classification
        # ==================================================================
        # Calculate ATR ratio for regime detection
        atr_1m_local = self._calculate_atr_1m(df_1m) if len(df_1m) >= 14 else None
        atr_1h_data = self.market_data.get_candles('1h')
        if atr_1h_data and len(atr_1h_data) >= 14:
            df_1h_atr = pd.DataFrame(atr_1h_data)
            atr_1h = self._calculate_atr_generic(df_1h_atr, 14)
        else:
            atr_1h = atr_1m_local * 10 if atr_1m_local else None
        
        if atr_1m_local and atr_1h:
            atr_ratio = atr_1m_local / (atr_1h / 60)  # Normalize 1H to per-minute
            self._last_atr_ratio = atr_ratio
            self._current_volatility_regime = VolatilityRegime.from_atr_ratio(atr_ratio)
            logger.debug(f"📊 Volatility regime: {self._current_volatility_regime.name} (ATR ratio={atr_ratio:.2f})")
        else:
            self._current_volatility_regime = VolatilityRegime.NORMAL
        
        # ==================================================================
        # 🎯 SCORE-BASED ENTRY SYSTEM (Score > Gates)
        # 4 blocks × 0-2 points + synergy = max 9
        # Dynamic threshold based on regime, session, and mode
        # Only Structure=0 blocks entry (the only real gate)
        # ==================================================================
        
        # Get dynamic threshold based on regime, session, and mode
        # Uses DynamicScoreThreshold config for regime-aware thresholds
        is_discovery = OperatingMode.is_discovery()
        
        # Detect market regime from current state (RANGE or TREND)
        # Use ADX and ATR expansion to determine regime
        current_adx_early = self._score_breakdown.get('adx_values', np.array([20.0]))[-1] if hasattr(self, '_score_breakdown') else 20.0
        is_trend_regime = current_adx_early > 25 or self._current_volatility_regime in [VolatilityRegime.HIGH, VolatilityRegime.SPIKE]
        regime_for_threshold = 'TREND' if is_trend_regime else 'RANGE'
        
        # Get base threshold from dynamic config
        base_threshold = DynamicScoreThreshold.get_threshold(
            regime=regime_for_threshold,
            session=session_name,
            is_discovery=is_discovery
        )
        
        # CAPA 4: Reduce threshold if activity is too low (prevent "no trades" failure)
        daily_trades = self.state_machine.daily_trades if hasattr(self.state_machine, 'daily_trades') else 0
        hours_elapsed = (market_time - market_time.replace(hour=0, minute=0, second=0)).total_seconds() / 3600
        
        # Calculate expected vs actual trades
        expected_trades = max(1, int(hours_elapsed / 8) * OperatingMode.MIN_TRADES_PER_SESSION)
        activity_deficit = expected_trades - daily_trades
        
        # If we're significantly below expected activity, relax threshold
        if activity_deficit >= 2 and hours_elapsed >= 4:
            threshold_reduction = min(OperatingMode.LOW_ACTIVITY_SCORE_REDUCTION, activity_deficit)
            self._entry_threshold = max(3, base_threshold - threshold_reduction)  # Never go below 3
            if self._entry_threshold < base_threshold:
                logger.info(f"📊 Low activity ({daily_trades}/{expected_trades} trades) → threshold {base_threshold} → {self._entry_threshold}")
                self._current_trade_tags['low_activity_relaxed'] = True
        else:
            self._entry_threshold = base_threshold
        
        # Log the regime-aware threshold
        logger.debug(f"📊 Dynamic threshold: {self._entry_threshold} (regime={regime_for_threshold}, session={session_name}, discovery={is_discovery})")
        
        # Get 1h data for structure scoring
        candles_1h = self.market_data.get_candles('1h')
        if not candles_1h or len(candles_1h) < 50:
            logger.warning("Not enough 1h data for entry scoring")
            return
        df_1h = pd.DataFrame(candles_1h)
        
        # Calculate entry score (also populates self._current_trade_tags)
        score, breakdown, structure_blocks = self._calculate_entry_score(
            df_1h, df_5m, df_1m, bias, current_price, market_time
        )
        
        # Apply spread penalty to score (from GUARD 5)
        if _spread_score_penalty > 0:
            original_score = score
            score = max(0, score - _spread_score_penalty)
            logger.info(f"📊 Score adjusted for spread: {original_score} → {score} (penalty={_spread_score_penalty})")
            self._current_trade_tags['spread_adjusted_score'] = score
        
        # Store for later use
        self._entry_score = score
        self._score_breakdown = breakdown
        
        # Log score breakdown (includes synergy)
        synergy = breakdown.get('synergy', 0)
        logger.info(f"📊 ENTRY SCORE: {score}/9 [MODE={OperatingMode.MODE}] | Str:{breakdown['structure']} Tim:{breakdown['timing']} Mom:{breakdown['momentum']} Ctx:{breakdown['context']} Syn:{synergy}")
        
        # THE ONLY GATE: Structure = 0 (corrupt data, systemic risk, invalid regime)
        if structure_blocks:
            logger.warning(f"⛔ STRUCTURE GATE: Score blocked by structure ({breakdown['structure']}/2)")
            # Track missed opportunity
            self.missed_tracker.log_blocked_signal(
                direction=bias,
                price=current_price,
                block_reason=f"Structure gate ({breakdown['structure']}/2)",
                block_category='filter',
                score=score,
                atr=self._last_momentum_atr
            )
            # Wait 2 candles for structure to potentially change
            self._set_rejection_cooldown("Structure against entry", candles=2)
            return
        
        # =====================================================================
        # GRADUATED ENTRY SYSTEM (Jan 20, 2026)
        # Replaces binary threshold with graduated sizing:
        # - score < 4.5: Block (floor breach)
        # - 4.5 <= score < 5.5: Probe entry (0.25x size)
        # - 5.5 <= score < 6.5: Reduced entry (0.5x size)
        # - score >= 6.5: Full entry (1.0x size)
        # =====================================================================
        from src.engine.config import EntryGateConfig
        
        # Get score-based size multiplier
        score_size_multiplier = EntryGateConfig.get_score_size_multiplier(score)
        self._current_trade_tags['score_size_multiplier'] = score_size_multiplier
        
        # Check if score is below absolute floor
        if score_size_multiplier == 0.0:
            logger.info(f"📉 Score {score:.1f} < floor {EntryGateConfig.SCORE_BLOCK_FLOOR} - waiting for better setup")
            # Log tags for analysis even on rejected entries
            if OperatingMode.is_discovery():
                logger.debug(f"🏷️ Rejected trade tags: {self._current_trade_tags}")
            return
        
        # Determine entry type
        is_probe_entry = score_size_multiplier <= 0.3
        is_reduced_entry = 0.3 < score_size_multiplier < 1.0
        
        if is_probe_entry:
            logger.info(f"🔬 PROBE ENTRY: Score {score:.1f} in probe zone (4.5-5.5) - entering with {score_size_multiplier:.0%} size")
            self._current_trade_tags['entry_type'] = 'probe'
        elif is_reduced_entry:
            logger.info(f"📊 REDUCED ENTRY: Score {score:.1f} in reduced zone (5.5-6.5) - entering with {score_size_multiplier:.0%} size")
            self._current_trade_tags['entry_type'] = 'reduced'
        else:
            logger.info(f"✅ FULL ENTRY: Score {score:.1f} >= 6.5 - entering with full size")
            self._current_trade_tags['entry_type'] = 'full'
        
        # =========================================================================
        # SESSION TRADE LIMIT CHECK (after score pass, allows override for high scores)
        # =========================================================================
        if self._at_session_limit:
            # Check if score qualifies for override
            if score >= SessionTradeLimits.OVERRIDE_IF_SCORE_GTE:
                logger.info(f"📊 Session limit override: score {score} >= {SessionTradeLimits.OVERRIDE_IF_SCORE_GTE} (trades in {session_name}: {current_session_trades}/{session_limit})")
                self._current_trade_tags['session_limit_override'] = True
            else:
                logger.warning(f"🚫 SESSION LIMIT: {current_session_trades}/{session_limit} trades in {session_name}, score {score} < override threshold {SessionTradeLimits.OVERRIDE_IF_SCORE_GTE}")
                self._current_trade_tags['skip_reason'] = 'session_limit'
                self.missed_tracker.log_blocked_signal(
                    direction=bias,
                    price=current_price,
                    block_reason=f"Session limit {current_session_trades}/{session_limit} in {session_name}",
                    block_category='session_limit',
                    score=score,
                    atr=self._last_momentum_atr
                )
                # No cooldown - limits reset per session
                return
        
        # =========================================================================
        # IMPORTANT LEVELS CHECK (Jan 13, 2026) - Anchor level proximity
        # Check if entry is blocked by nearby psychological S/R levels
        # Also provides confluence bonus and TP adjustment suggestions
        # =========================================================================
        level_context = None
        if self.important_levels and self.important_levels.is_initialized:
            try:
                # Refresh levels if needed (once per hour max)
                await self.important_levels.update_if_needed()
                
                # Get level context for current price and direction
                level_context = self.important_levels.get_level_context(
                    current_price=current_price,
                    direction=bias,
                    atr_5m=self._calculate_atr_5m(df_5m) if len(df_5m) >= 14 else atr_1m * 2
                )
                self._last_level_context = level_context
                
                # Store level info in tags
                if level_context.nearest_above:
                    self._current_trade_tags['level_above'] = level_context.nearest_above.price
                    self._current_trade_tags['level_above_type'] = level_context.nearest_above.level_type.value
                    self._current_trade_tags['level_above_atr'] = level_context.distance_above_atr
                if level_context.nearest_below:
                    self._current_trade_tags['level_below'] = level_context.nearest_below.price
                    self._current_trade_tags['level_below_type'] = level_context.nearest_below.level_type.value
                    self._current_trade_tags['level_below_atr'] = level_context.distance_below_atr
                
                # Log level context
                if level_context.nearest_above or level_context.nearest_below:
                    above_info = f"↑ ${level_context.nearest_above.price:,.0f} ({level_context.distance_above_atr:.2f} ATR)" if level_context.nearest_above else "↑ None"
                    below_info = f"↓ ${level_context.nearest_below.price:,.0f} ({level_context.distance_below_atr:.2f} ATR)" if level_context.nearest_below else "↓ None"
                    logger.info(f"📊 ANCHOR LEVELS: {above_info} | {below_info}")
                
                # Check if blocked by proximity to level in our direction
                if level_context.blocked:
                    logger.warning(f"⛔ ANCHOR LEVEL BLOCK: {level_context.block_reason}")
                    self._current_trade_tags['skip_reason'] = 'anchor_level_too_close'
                    self._current_trade_tags['level_block_reason'] = level_context.block_reason
                    
                    # Track as missed opportunity
                    self.missed_tracker.log_blocked_signal(
                        direction=bias,
                        price=current_price,
                        block_reason=f"Anchor level: {level_context.block_reason}",
                        block_category='anchor_level',
                        score=score,
                        atr=self._last_momentum_atr
                    )
                    # Short cooldown - levels don't change, but price might
                    self._set_rejection_cooldown("Anchor level proximity", candles=3)
                    return
                
                # Apply confluence bonus if applicable
                if level_context.confluence_bonus > 0:
                    score += level_context.confluence_bonus
                    self._current_trade_tags['level_confluence'] = True
                    self._current_trade_tags['level_confluence_bonus'] = level_context.confluence_bonus
                    logger.info(f"📈 LEVEL CONFLUENCE: +{level_context.confluence_bonus} pts | {level_context.confluence_reason}")
                    
            except Exception as e:
                logger.warning(f"Error checking important levels: {e} - continuing without")
        
        # =========================================================================
        # REGIME DETECTION (Jan 12, 2026) - Context-aware market state
        # Anti-chasing behavior changes based on regime
        # =========================================================================
        from src.engine.config import OrderExecution
        
        # Detect current regime using ADX, ATR expansion, etc.
        adx_values = self._score_breakdown.get('adx_values', np.array([20.0]))
        current_adx = adx_values[-1] if len(adx_values) > 0 else 20.0
        
        # Calculate ATR expansion (current vs average)
        atr_5m = self._calculate_atr_5m(df_5m) if len(df_5m) >= 14 else atr_1m * 2
        atr_avg = np.mean([c['high'] - c['low'] for c in self.market_data.get_candles('5m')[-20:]]) if len(df_5m) >= 20 else atr_5m
        
        # Detect regime
        has_bos = self._score_breakdown.get('timing', 0) >= 1  # BOS contributes to timing
        
        # Get actual spread from order book if available
        spread_pct = 0.0
        if hasattr(self, 'order_book') and self.order_book:
            bid_price, ask_price, spread_val = self.order_book.get_spread()
            if bid_price and ask_price and bid_price > 0:
                mid_price = (bid_price + ask_price) / 2
                spread_pct = ((ask_price - bid_price) / mid_price) * 100 if mid_price > 0 else 0.0
        
        self._current_regime = self.regime_detector.detect(
            adx=current_adx,
            adx_values=adx_values,
            atr_current=atr_5m,
            atr_avg=atr_avg,
            has_bos=has_bos,
            spread_pct=spread_pct,
            volume_spike=False
        )
        
        regime_config = self.regime_detector.get_config(self._current_regime)
        self._current_trade_tags['regime'] = self._current_regime.value
        logger.info(f"📊 REGIME: {self._current_regime.value} | {regime_config.description}")
        
        # =========================================================================
        # ANTI-CHASING AS SCORE PENALTY (Jan 12, 2026)
        # ❌ Old: gate that blocks trades
        # ✅ New: score penalty relative to regime
        # "Chasing is not wrong, it's more expensive"
        # =========================================================================
        chase_atr = 0.0
        chase_penalty = 0.0
        
        if OrderExecution.ANTI_CHASING_ENABLED:
            # Calculate chase distance (how many ATRs have we moved since signal)
            chase_atr = self._calculate_chase_atr(df_1m, atr_1m, bias)
            
            # Check if regime says to block entirely (only CHAOS or extreme chase)
            should_block, block_reason = self.regime_detector.should_block_trade(chase_atr, self._current_regime)
            
            if should_block:
                logger.warning(f"⛔ REGIME BLOCK: {block_reason}")
                self.missed_tracker.log_blocked_signal(
                    direction=bias,
                    price=current_price,
                    block_reason=f"Regime block: {block_reason}",
                    block_category='regime',
                    score=score,
                    atr=self._last_momentum_atr
                )
                self._current_trade_tags['skip_reason'] = 'regime_block'
                return
            
            # Calculate regime-aware chase penalty
            chase_penalty = self.regime_detector.get_chase_penalty(chase_atr, self._current_regime)
            
            # Check momentum context (if positive, reduce penalty)
            adx_rising = len(adx_values) >= 3 and adx_values[-1] > adx_values[-3]
            candle_expansion = atr_5m > atr_avg * 1.1
            
            # Check VWAP reclaim: was below N candles ago, now above
            vwap_reclaim = False
            try:
                if len(df_5m) >= 5:
                    from src.engine.indicators import calculate_vwap
                    vwap_series = calculate_vwap(df_5m)
                    if len(vwap_series) >= 5:
                        closes_5m = df_5m['close'].values
                        # For LONG: was below VWAP in last 5 candles, now above
                        # For SHORT: was above VWAP in last 5 candles, now below
                        if bias == 'LONG':
                            was_below = any(closes_5m[i] < vwap_series.iloc[i] for i in range(-5, -1))
                            now_above = closes_5m[-1] > vwap_series.iloc[-1]
                            vwap_reclaim = was_below and now_above
                        else:  # SHORT
                            was_above = any(closes_5m[i] > vwap_series.iloc[i] for i in range(-5, -1))
                            now_below = closes_5m[-1] < vwap_series.iloc[-1]
                            vwap_reclaim = was_above and now_below
            except Exception as e:
                logger.debug(f"VWAP reclaim check failed: {e}")
            
            momentum_context = self.regime_detector.get_momentum_context(
                has_bos=has_bos,
                adx_rising=adx_rising,
                candle_expansion=candle_expansion,
                vwap_reclaim=vwap_reclaim
            )
            
            if momentum_context and chase_penalty > 0:
                # Momentum context reduces penalty by half
                chase_penalty *= 0.5
                logger.info(f"📊 Momentum context positive → chase penalty halved")
            
            # Apply penalty to score
            if chase_penalty > 0:
                adjusted_score = score - chase_penalty
                logger.info(f"📊 Chase penalty: {chase_atr:.2f} ATR → -{chase_penalty:.1f} pts → adjusted score {adjusted_score:.1f}")
                self._current_trade_tags['chase_atr'] = chase_atr
                self._current_trade_tags['chase_penalty'] = chase_penalty
                
                # Re-check threshold after penalty
                if adjusted_score < self._entry_threshold:
                    logger.warning(f"⚠️ Score after chase penalty ({adjusted_score:.1f}) < threshold ({self._entry_threshold}) - skipping")
                    self.missed_tracker.log_blocked_signal(
                        direction=bias,
                        price=current_price,
                        block_reason=f"Chase penalty: {chase_atr:.1f} ATR, score {score:.1f}→{adjusted_score:.1f}",
                        block_category='chase_penalty',
                        score=score,
                        atr=self._last_momentum_atr
                    )
                    self._current_trade_tags['near_miss'] = True
                    self._current_trade_tags['near_miss_reason'] = 'chase_penalty'
                    return
                
                # Update score with penalty applied
                score = adjusted_score
        
        # =========================================================================
        # REGIME-BASED SIZING (replaces simple score sizing)
        # =========================================================================
        if OperatingMode.is_discovery():
            # Fixed 0.8x sizing in discovery - no momentum adjustments
            score_size_multiplier = 0.8
            logger.info(f"📊 DISCOVERY mode: fixed sizing 0.8x")
        else:
            # Get score-based size multiplier (score 5→0.8x, 6→1.0x, 7+→1.2x)
            base_size = self._get_score_size_multiplier(score)
            # Apply regime multiplier
            score_size_multiplier = base_size * regime_config.size_multiplier
            logger.info(f"📊 Regime sizing: {base_size:.1f}x × {regime_config.size_multiplier:.1f}x = {score_size_multiplier:.2f}x")
        
        # DUPLICATE ZONE - Still check this (it's about avoiding revenge trading)
        # This is TECHNICAL protection, not quality filtering
        dup_ok, dup_reason = self._check_not_duplicate_zone(current_price)
        if not dup_ok:
            logger.warning(f"⛔ DUPLICATE BLOCKED: {dup_reason}")
            self._set_rejection_cooldown(dup_reason, candles=3)
            return
        
        # LOSS COOLDOWN - Still check this (it's about protecting from tilt)
        cooldown_ok, cooldown_reason = self._check_loss_cooldown(market_time)
        if not cooldown_ok:
            logger.warning(f"⛔ {cooldown_reason}")
            self._reset_trade_state("Loss cooldown active")
            return
        
        # FIX: Use ATR from 5m timeframe for SL (more stable, survives noise)
        atr_5m = self._calculate_atr_5m(df_5m)
        if atr_5m > 0:
            atr_for_sl = atr_5m
            logger.info(f"📊 Using ATR 5m for SL: ${atr_for_sl:.2f}")
        else:
            atr_for_sl = atr_1m * 2.5
            logger.warning(f"⚠️ ATR 5m not available, using scaled 1m: ${atr_for_sl:.2f}")
        
        # 🔥 REGIME-BASED SL ATR MULTIPLIER
        # RANGE = 1.5x (tighter, quick scalps)
        # TREND = 2.5x (wider, let winners run)
        regime_sl_atr_mult = regime_config.sl_atr_multiplier
        logger.info(f"📊 Regime SL: {regime_sl_atr_mult}x ATR ({self._current_regime.value})")
        
        # Calculate ATR ratio for dynamic SL sizing (volatility adjustment)
        atr_ratio_for_sl = None
        if len(df_5m) >= 14:
            # Calculate ATR using existing function
            atr_values = calculate_atr(df_5m['high'].values, df_5m['low'].values, df_5m['close'].values, 14)
            if len(atr_values) >= 10:
                current_atr = atr_values[-1]
                avg_atr = sum(atr_values[-10:]) / 10
                if avg_atr > 0:
                    atr_ratio_for_sl = current_atr / avg_atr
                    logger.debug(f"ATR ratio for SL: {atr_ratio_for_sl:.2f} (current: {current_atr:.2f}, avg: {avg_atr:.2f})")
        
        support_resistance = self._find_support_resistance(bias, df_5m)
        stop_loss = self.risk_engine.calculate_stop_loss(
            current_price,
            bias,
            support_resistance,
            atr_for_sl,
            atr_ratio=atr_ratio_for_sl,
            regime_atr_mult=regime_sl_atr_mult  # Pass regime-based multiplier
        )
        
        # Calculate take profit
        risk = abs(current_price - stop_loss)
        tp_multiplier = RiskManagement.TP1_RISK_MULTIPLE
        
        if bias == 'LONG':
            take_profit = current_price + (risk * tp_multiplier)
        else:
            take_profit = current_price - (risk * tp_multiplier)
        
        # =========================================================================
        # TP ADJUSTMENT FOR ANCHOR LEVELS (Jan 13, 2026)
        # If an important level is between entry and TP, adjust TP to just before it
        # This avoids rejections at psychological levels
        # =========================================================================
        original_tp = take_profit
        if level_context and self.important_levels:
            # Get updated level context with planned TP
            adjusted_context = self.important_levels.get_level_context(
                current_price=current_price,
                direction=bias,
                atr_5m=atr_for_sl,
                planned_tp=take_profit
            )
            
            if adjusted_context.suggested_tp is not None:
                take_profit = adjusted_context.suggested_tp
                self._current_trade_tags['tp_adjusted_for_level'] = True
                self._current_trade_tags['tp_original'] = original_tp
                self._current_trade_tags['tp_adjustment_reason'] = adjusted_context.tp_adjustment_reason
                logger.info(f"📊 TP ADJUSTED: {adjusted_context.tp_adjustment_reason}")
                
                # Recalculate effective RR after adjustment
                new_risk_reward = abs(take_profit - current_price) / risk if risk > 0 else 0
                tp_multiplier = new_risk_reward
                logger.info(f"📊 Effective RR after adjustment: {new_risk_reward:.2f}R")
        
        logger.info(f"Levels: Entry={current_price:.2f} | SL={stop_loss:.2f} | TP={take_profit:.2f} | Risk={risk:.2f} | RR={tp_multiplier:.2f}R")
        
        # =========================================================================
        # RR SCORE ADJUSTMENT - Apply RR penalty/bonus to score (Jan 9, 2026)
        # This happens AFTER SL/TP calculation when we know actual RR
        # =========================================================================
        original_score = score
        score, rr_adjustment_reason = self._apply_rr_score_adjustment(score, tp_multiplier)
        
        if rr_adjustment_reason:
            logger.info(f"📊 Score adjusted: {original_score} → {score} ({rr_adjustment_reason})")
            self._current_trade_tags['rr_adjustment'] = rr_adjustment_reason
        
        # Re-check if score still passes threshold after RR adjustment
        if score < self._entry_threshold:
            logger.warning(f"⛔ Score {score:.1f} < threshold {self._entry_threshold} AFTER RR adjustment - SKIP ENTRY")
            self._current_trade_tags['skip_reason'] = 'score_below_threshold_after_rr_adjustment'
            return
        
        # ===== RR HARD FLOOR CHECK - REGIME AWARE (Jan 14, 2026) =====
        # Different regimes have different mathematical minimums:
        # - RANGE: High winrate mean-reversion can work with 0.5R+
        # - TREND: Need 0.9R+ because momentum premium
        # Use regime_config.min_rr as PRIMARY floor
        current_regime_config = self.regime_detector.get_config(self._current_regime)
        regime_min_rr = current_regime_config.min_rr
        
        # Absolute floor (applies to ALL regimes)
        absolute_floor = getattr(OperatingMode, 'RR_HARD_FLOOR', 0.4)
        
        # Use the higher of: regime-specific floor or absolute floor
        effective_floor = max(regime_min_rr, absolute_floor)
        
        self._current_trade_tags['rr_regime_floor'] = regime_min_rr
        self._current_trade_tags['rr_effective_floor'] = effective_floor
        
        if tp_multiplier < effective_floor:
            logger.warning(f"🚫 RR BELOW FLOOR: {tp_multiplier:.2f}R < {effective_floor:.2f}R ({self._current_regime.value}) → REJECTED")
            self._current_trade_tags['skip_reason'] = 'rr_below_floor'
            return
        
        # Log acceptance for transparency
        if tp_multiplier < 1.0:
            logger.info(f"✅ RR {tp_multiplier:.2f}R ACCEPTED for {self._current_regime.value} (floor={effective_floor:.2f}R)")
        
        # =========================================================================
        # WORST-CASE RR VALIDATION (using slippage model)
        # Calculate realistic RR accounting for:
        # - Entry slippage (LIMIT order - actual bot behavior)
        # - SL slippage (market order in adverse conditions)
        # - TP slippage (taker if aggressive exit)
        # If worst-case RR < regime floor, reject the trade
        #
        # UPDATE Jan 20, 2026:
        # Changed from MARKET to LIMIT because bot uses post-only limit orders.
        # This gives a realistic estimate rather than overly pessimistic worst-case.
        # =========================================================================
        from src.engine.slippage_model import OrderType
        
        # Get volatility ratio for slippage calculation
        volatility_ratio = self._last_atr_ratio if hasattr(self, '_last_atr_ratio') else 1.0
        
        worst_case_rr = self.slippage_model.calculate_worst_case_rr(
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=bias,
            order_type=OrderType.LIMIT,  # Bot uses limit orders for entry
            volatility_ratio=volatility_ratio
        )
        
        self._current_trade_tags['rr_ideal'] = tp_multiplier
        self._current_trade_tags['rr_worst_case'] = worst_case_rr
        self._current_trade_tags['rr_slippage_impact'] = tp_multiplier - worst_case_rr
        
        logger.info(f"📊 RR Analysis: Ideal={tp_multiplier:.2f}R → Worst-case={worst_case_rr:.2f}R (slippage={tp_multiplier - worst_case_rr:.2f}R)")
        
        # =========================================================================
        # RR-BASED SIZING (Jan 20, 2026 Update)
        # Replaces binary floor with graduated sizing:
        # - rr_net < 0.6: Block (mathematical roulette)
        # - 0.6 <= rr_net < 1.0: Probe entry (0.25x size)
        # - 1.0 <= rr_net < 1.2: Reduced entry (0.5x size)
        # - rr_net >= 1.2: Full entry (1.0x size)
        # Note: RANGE regime uses lower thresholds due to higher winrate
        # =========================================================================
        current_regime_str = self._current_regime.value if self._current_regime else "NORMAL"
        rr_size_multiplier = EntryGateConfig.get_rr_size_multiplier(worst_case_rr, current_regime_str)
        self._current_trade_tags['rr_size_multiplier'] = rr_size_multiplier
        
        if rr_size_multiplier == 0.0:
            logger.warning(f"🚫 WORST-CASE RR BELOW FLOOR: {worst_case_rr:.2f}R < {effective_floor:.2f}R (ideal was {tp_multiplier:.2f}R) → REJECTED")
            self._current_trade_tags['skip_reason'] = 'worst_case_rr_below_floor'
            self.missed_tracker.log_blocked_signal(
                direction=bias,
                price=current_price,
                block_reason=f"Worst-case RR {worst_case_rr:.2f}R < floor {effective_floor:.2f}R",
                block_category='slippage',
                score=score,
                atr=self._last_momentum_atr
            )
            return
        
        # Log RR sizing decision
        if rr_size_multiplier < 1.0:
            rr_zone = 'probe' if rr_size_multiplier <= 0.3 else 'reduced'
            logger.info(f"📊 RR SIZING: Worst-case {worst_case_rr:.2f}R → {rr_zone} ({rr_size_multiplier:.0%})")
            self._current_trade_tags['rr_zone'] = rr_zone
        
        # Tag the configured values for analysis
        self._current_trade_tags['rr_configured'] = tp_multiplier
        
        # 🔥 REFRESH CAPITAL BEFORE POSITION SIZING
        # This ensures we use ACTUAL free margin, not stale capital value
        try:
            fresh_capital = await self.execution.get_available_capital()
            if fresh_capital > 0:
                self.risk_engine.capital = fresh_capital
                logger.debug(f"Refreshed capital: ${fresh_capital:.2f} free")
            else:
                logger.error("⛔ Failed to refresh capital - aborting entry")
                # 🔥 FIX #6: Add cooldown after capital refresh failure to avoid rapid retries
                self.state_machine.start_cooldown(5)  # 5 minute cooldown
                self._reset_trade_state("Capital refresh failed")
                return
        except Exception as e:
            logger.error(f"Error refreshing capital: {e}")
            # 🔥 FIX #6: Add cooldown on exception too
            self.state_machine.start_cooldown(5)
            self._reset_trade_state("Capital refresh error")
            return
        
        # ==================================================================
        # CENTRALIZED POSITION SIZING (Using PositionSizer + Score + Funding)
        # Enhanced with: Loss streak, Session quality, Day of week, Funding direction
        # ==================================================================
        
        # Get state multiplier from operational state machine
        state_multiplier = self.state_machine.get_size_multiplier()
        
        # Get black swan defensive multiplier
        black_swan_multiplier = self.black_swan_filter.get_size_multiplier(market_time)
        
        # Initialize funding_sizing_factor (will be updated in TIER 2 section if not backtest)
        funding_sizing_factor = 1.0
        
        # SCORE-BASED SIZING: Now using graduated sizing from EntryGateConfig
        # score_size_multiplier already calculated above
        combined_defensive = black_swan_multiplier * score_size_multiplier
        
        # =========================================================================
        # RR-BASED SIZING (Jan 20, 2026) - Apply RR-based size reduction
        # rr_size_multiplier already calculated above after worst-case RR check
        # =========================================================================
        combined_defensive *= rr_size_multiplier
        if rr_size_multiplier < 1.0:
            logger.debug(f"📊 RR sizing applied: {rr_size_multiplier:.0%}")
        
        # =========================================================================
        # LTF CONFIRMATION SIZING (Jan 20, 2026) - Size based on LTF confirmations
        # Never require 3/3 confirmations (anti-paralysis)
        # 2/3+ = full, 1/3 = 0.5x, 0/3 = 0.25x
        # =========================================================================
        ltf_confirmations = self._current_trade_tags.get('ltf_confirmations', 2)
        ltf_size_multiplier = EntryGateConfig.get_ltf_size_multiplier(ltf_confirmations, total=3)
        combined_defensive *= ltf_size_multiplier
        self._current_trade_tags['ltf_size_multiplier'] = ltf_size_multiplier
        if ltf_size_multiplier < 1.0:
            logger.info(f"📊 LTF sizing: {ltf_confirmations}/3 confirmations → {ltf_size_multiplier:.0%}")
        
        # =========================================================================
        # LOSS STREAK SIZING (Jan 9, 2026) - Reduce size after consecutive losses
        # Progressive: 0 losses=100%, 1=80%, 2=60%, 3+=50%
        # =========================================================================
        loss_streak_multiplier = OperatingMode.get_loss_sizing_multiplier(self._consecutive_losses)
        if self._consecutive_losses > 0:
            logger.info(f"📉 Loss streak sizing: {self._consecutive_losses} losses → {loss_streak_multiplier:.0%}")
            self._current_trade_tags['loss_streak_size_factor'] = loss_streak_multiplier
        combined_defensive *= loss_streak_multiplier
        
        # =========================================================================
        # SESSION QUALITY SIZING (Jan 9, 2026) - Reduce in low-quality sessions
        # =========================================================================
        session_name = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
        session_multiplier = OperatingMode.get_session_multiplier(session_name)
        if session_multiplier < 1.0:
            logger.info(f"📊 Session sizing: {session_name} → {session_multiplier:.0%}")
        self._current_trade_tags['session_quality'] = session_name
        self._current_trade_tags['session_size_factor'] = session_multiplier
        combined_defensive *= session_multiplier
        
        # =========================================================================
        # DAY OF WEEK SIZING (Jan 13, 2026) - Reduce on Friday/Weekend
        # =========================================================================
        day_multiplier = OperatingMode.get_day_multiplier(market_time.weekday())
        if day_multiplier < 1.0:
            day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            logger.info(f"📊 Day sizing: {day_names[market_time.weekday()]} → {day_multiplier:.0%}")
        self._current_trade_tags['day_of_week'] = market_time.weekday()
        self._current_trade_tags['day_size_factor'] = day_multiplier
        combined_defensive *= day_multiplier
        
        # =========================================================================
        # SPREAD SIZE REDUCTION (Jan 16, 2026) - Reduce size when spread is elevated
        # =========================================================================
        if _spread_size_factor < 1.0:
            logger.info(f"📊 Spread sizing: elevated spread → {_spread_size_factor:.0%}")
            self._current_trade_tags['spread_size_factor'] = _spread_size_factor
            combined_defensive *= _spread_size_factor
        
        # =========================================================================
        # FUNDING SIZING - Moved to TIER 2 section (lines ~1980) to avoid duplication
        # The funding_sizing_factor is retrieved from external_data.funding.get_funding_info()
        # which provides comprehensive funding data including directional scoring
        # =========================================================================
        # NOTE: funding_sizing_factor will be set later in the TIER 2 section
        # and applied via combined_defensive there
        
        # Calculate position size using centralized sizer
        position_size, size_breakdown = self._position_sizer.calculate_position_size(
            capital=self.risk_engine.capital,
            entry_price=current_price,
            stop_loss=stop_loss,
            state_multiplier=state_multiplier,
            edge_multiplier=edge_size_multiplier,
            defensive_multiplier=combined_defensive,
            leverage=10
        )
        
        # =====================================================================
        # 🔒 FIX (Jan 13, 2026): PRE-ROUND SIZE TO EXCHANGE PRECISION
        # This ensures local state uses the SAME size that will be submitted.
        # Prevents desync between intended_size and submitted_size.
        # =====================================================================
        intended_size = position_size  # Store original for logging
        position_size = round_size_to_exchange(position_size)
        
        if position_size != intended_size:
            logger.info(f"📊 Size pre-rounded for local tracking: {intended_size:.8f} → {position_size:.8f}")
        
        # Log sizing breakdown with ALL factors
        logger.info(f"📊 Position sizing: base={size_breakdown['base']:.6f} × state={size_breakdown['state']:.2f} × edge={size_breakdown['edge']:.2f} × score={score_size_multiplier:.2f} × rr={rr_size_multiplier:.2f} × ltf={ltf_size_multiplier:.2f} × losses={loss_streak_multiplier:.2f} × session={session_multiplier:.2f} × day={day_multiplier:.2f} = {position_size:.6f}")
        
        if size_breakdown.get('blocked_by'):
            logger.warning(f"⛔ Sizing blocked by: {size_breakdown['blocked_by']}")
        
        if position_size <= 0:
            logger.warning("Position size is zero - skipping entry")
            return

        # ==================================================================
        # TIER 2 VALIDATION - Score > Gates Architecture
        # 
        # PHILOSOPHY: After score >= threshold, only TRUE SYSTEMIC RISK blocks.
        # Funding rate is NOT a blocker - it's a tag + sizing factor.
        #
        # NOTHING BLOCKS after score passes (except corrupt data)
        # Everything becomes a TAG for edge discovery.
        # ==================================================================

        # Get 1H data for metrics
        candles_1h = self.market_data.get_candles('1h')
        df_1h = pd.DataFrame(candles_1h) if candles_1h else None

        if not self._is_backtest:
            # ═══════════════════════════════════════════════════════════════
            # FUNDING RATE - TAG + SIZING + DIRECTIONAL SCORE (NOT A BLOCKER)
            # Funding affects sizing in PRODUCTION, but NEVER blocks entry
            # DIRECTIONAL: shorts can BENEFIT from high positive funding
            # ═══════════════════════════════════════════════════════════════
            funding_info = self.external_data.funding.get_funding_info(bias=bias)
            
            # Store as tags for analysis
            self._current_trade_tags['funding_rate'] = funding_info['rate_pct']
            self._current_trade_tags['funding_bucket'] = funding_info['bucket']
            self._current_trade_tags['funding_direction'] = funding_info['direction']
            self._current_trade_tags['funding_sizing_factor'] = funding_info['sizing_factor']
            
            # Log funding status (INFO for normal, WARNING for elevated+)
            # Bucket names from external_data: 'normal', 'elevated', 'high', 'critical', 'extreme'
            if funding_info['bucket'] in ['high', 'critical', 'extreme']:
                logger.warning(f"⚠️ Funding {funding_info['bucket'].upper()}: {funding_info['rate_pct']:.4f}% | sizing ×{funding_info['sizing_factor']:.2f}")
            elif funding_info['bucket'] == 'elevated':
                logger.info(f"📊 Funding elevated: {funding_info['rate_pct']:.4f}% | sizing ×{funding_info['sizing_factor']:.2f}")
            
            # Apply DIRECTIONAL score adjustment (can be positive for favorable funding!)
            funding_score_adj = funding_info['score_penalty']
            if funding_score_adj != 0:
                self._entry_score += funding_score_adj
                direction_note = "receiving" if funding_score_adj > 0 else "paying"
                logger.info(f"💰 Score adjusted by {funding_score_adj:+.2f} for funding ({direction_note}, new score: {self._entry_score})")
            
            # ═══════════════════════════════════════════════════════════════
            # TAGS ONLY - Collect data, don't block
            # In DISCOVERY mode, we ENTER regardless and track outcomes
            # ═══════════════════════════════════════════════════════════════
            
            # TAG: Session quality (ADX + volatility)
            adx_1h = 0.0
            volatility_bps = 0.0
            if df_1h is not None and len(df_1h) >= 20:
                try:
                    adx_vals, _ = calculate_adx(df_1h['high'].values, df_1h['low'].values, df_1h['close'].values, 14)
                    adx_1h = float(adx_vals[-1]) if len(adx_vals) > 0 else 0.0
                    
                    last = df_1h.iloc[-1]
                    close = float(last.get('close', 0) or 0)
                    high = float(last.get('high', 0) or 0)
                    low = float(last.get('low', 0) or 0)
                    volatility_bps = ((high - low) / close * 10000.0) if close > 0 else 0.0
                except Exception as e:
                    logger.debug(f"Session metrics calc failed: {e}")
            
            # 🔒 FIX: Store ADX 1h in tags for consistency with HTF filter
            self._current_trade_tags['adx_1h'] = float(adx_1h)
            
            session_ok, session_reason = check_session_quality(adx_1h, volatility_bps)
            self._current_trade_tags['session_quality'] = 'good' if session_ok else 'poor'
            self._current_trade_tags['session_reason'] = session_reason
            if not session_ok:
                logger.info(f"🏷️ TAG: Session quality poor: {session_reason} (entering anyway)")
            
            # TAG: HTF divergence
            if df_1h is not None:
                divergence_detected, divergence_reason = check_htf_divergence(df_1h, bias)
                self._current_trade_tags['htf_divergence'] = divergence_detected
                if divergence_detected:
                    logger.info(f"🏷️ TAG: HTF divergence detected: {divergence_reason} (entering anyway)")
            
            # TAG: VPOC validity
            if df_1h is not None:
                vpoc = calculate_vpoc(df_1h)
                vpoc_ok, vpoc_reason = check_vpoc_validity(current_price, vpoc, bias)
                self._current_trade_tags['vpoc_valid'] = vpoc_ok
                self._current_trade_tags['vpoc_price'] = vpoc
                if not vpoc_ok:
                    logger.info(f"🏷️ TAG: VPOC invalid: {vpoc_reason} (entering anyway)")
            
            # TAG: BTC trend correlation
            btc_ok, btc_reason = await check_btc_trend(self.external_data, bias)
            self._current_trade_tags['btc_aligned'] = btc_ok
            if not btc_ok:
                logger.info(f"🏷️ TAG: BTC trend misaligned: {btc_reason} (entering anyway)")
            
            # ═══════════════════════════════════════════════════════════════
            # CAPA 3: AGGRESSIVE TAGGING FOR EDGE DISCOVERY
            # These tags NEVER block - they enable post-trade analysis
            # ═══════════════════════════════════════════════════════════════
            
            # TAG: VWAP distance (in ATRs)
            try:
                vwap = self.market_data.indicators.get('5m', {}).get('vwap', 0.0)
                if vwap and atr_1m > 0:
                    vwap_distance = abs(current_price - vwap)
                    vwap_distance_atr = vwap_distance / atr_1m
                    self._current_trade_tags['vwap_distance_atr'] = round(vwap_distance_atr, 2)
                    self._current_trade_tags['vwap_side'] = 'above' if current_price > vwap else 'below'
            except Exception as e:
                logger.debug(f"VWAP tag failed: {e}")
            
            # TAG: Funding z-score (relative to historical, not absolute)
            try:
                funding_rate = funding_info.get('rate_pct', 0.0)
                # Historical median funding ~0.01% (10 bps)
                # Historical std ~0.05% (50 bps)
                funding_median = 0.01
                funding_std = 0.05
                funding_zscore = (abs(funding_rate) - funding_median) / funding_std if funding_std > 0 else 0.0
                self._current_trade_tags['funding_zscore'] = round(funding_zscore, 2)
            except Exception as e:
                logger.debug(f"Funding z-score tag failed: {e}")
            
            # TAG: Entry timestamp for latency tracking (order->fill)
            self._current_trade_tags['entry_timestamp_ms'] = int(market_time.timestamp() * 1000)
            
            # In PRODUCTION mode, these tags COULD block (but not in DISCOVERY)
            if not OperatingMode.is_discovery():
                # Production: Apply stricter gates if configured
                # For now, we still enter but log the concern
                pass
        else:
            logger.debug("📋 [BACKTEST] Skipping TIER2 validation")
        
        # ===== FIX #2: DETERMINE ORDER TYPE BASED ON MOMENTUM =====
        # Calculate price velocity from last 2 candles (1m)
        candles_1m = self.market_data.get_candles('1m')
        use_market_order = False
        price_velocity = 0.0
        
        if candles_1m and len(candles_1m) >= 2:
            # Last 2 candles (1m)
            last_close = candles_1m[-1]['close']
            prev_close = candles_1m[-2]['close']
            
            # Price change
            price_change = abs(last_close - prev_close)
            
            # ATR(1m)
            closes_1m = [c['close'] for c in candles_1m[-14:]]
            highs_1m = [c['high'] for c in candles_1m[-14:]]
            lows_1m = [c['low'] for c in candles_1m[-14:]]
            
            if len(closes_1m) >= 14:
                atr_1m_vals = calculate_atr(
                    np.array(highs_1m, dtype=float),
                    np.array(lows_1m, dtype=float),
                    np.array(closes_1m, dtype=float),
                    14
                )
                atr_1m_current = atr_1m_vals[-1] if len(atr_1m_vals) > 0 else atr_1m
                
                # Velocity = price change / ATR
                price_velocity = price_change / atr_1m_current if atr_1m_current > 0 else 0
                
                # Use market if velocity > 2x ATR
                from src.engine.config import OrderExecution
                
                # SKIP if momentum too extreme IN DEAD MARKET (chasing in range)
                # Only skip if: velocity > 3.5 ATR AND ADX < 25 (no trend to ride)
                is_chasing_in_range = (
                    price_velocity > OrderExecution.MOMENTUM_MAX_VELOCITY and
                    adx_1h < OrderExecution.MOMENTUM_MAX_VELOCITY_ADX_THRESHOLD
                )
                if is_chasing_in_range:
                    logger.warning(f"🚫 CHASING IN DEAD MARKET: velocity={price_velocity:.2f}ATR > {OrderExecution.MOMENTUM_MAX_VELOCITY}ATR + ADX={adx_1h:.1f} < {OrderExecution.MOMENTUM_MAX_VELOCITY_ADX_THRESHOLD} → SKIP")
                    self._current_trade_tags['skip_reason'] = 'chasing_in_range'
                    self._current_trade_tags['velocity_at_skip'] = price_velocity
                    self._current_trade_tags['adx_at_skip'] = adx_1h
                    return  # Do not enter - wait for trend or pullback
                
                use_market_order = (
                    OrderExecution.USE_MARKET_ON_MOMENTUM and 
                    price_velocity > OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER
                )
                
                if use_market_order:
                    logger.info(f"⚡ HIGH MOMENTUM: velocity={price_velocity:.2f}ATR > {OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER}ATR → Using MARKET order")
                else:
                    logger.debug(f"Normal momentum: velocity={price_velocity:.2f}ATR < {OrderExecution.MOMENTUM_VELOCITY_MULTIPLIER}ATR → Using LIMIT order")
        
        # ===== PLACE ORDER (MARKET or LIMIT based on momentum) =====
        try:
            if use_market_order:
                # Market order for momentum
                logger.info(f"🚀 Placing MARKET order | {bias} {position_size} @ market | Velocity: {price_velocity:.2f}ATR")
                order_result = await self.execution.place_market_order(
                    symbol=self.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=position_size,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
            else:
                # Limit post-only order (default)
                logger.info(f"📊 Placing LIMIT post-only | {bias} {position_size} @ {current_price:.2f} | Velocity: {price_velocity:.2f}ATR")
                order_result = await self.execution.place_limit_order(
                    symbol=self.symbol,
                    side='buy' if bias == 'LONG' else 'sell',
                    amount=position_size,
                    price=current_price,
                    stop_loss=stop_loss,      # Stored for later placement
                    take_profit=take_profit   # Stored for later placement
                )
            
            if order_result:
                self.pending_order_id = order_result.get('id')
                self.entry_order_time = market_time  # Use market_time, not datetime.now()
                
                # =====================================================================
                # 🔒 NOTIFY RECONCILER - Enter HARD_LOCK to prevent premature reactions
                # During HARD_LOCK: funding/penalty/exits are DISABLED
                # =====================================================================
                self.reconciler.notify_order_placed(self.pending_order_id)
                
                # Send Telegram notification with account value
                if self.output_manager:
                    try:
                        rec = {
                            'symbol': self.symbol,
                            'direction': bias,
                            'status': 'OPENING',
                            'entry_price': current_price,
                            'stop_loss_price': stop_loss,
                            'signal_timestamp': market_time.isoformat(),
                            'account_value': self.risk_engine.capital,
                            'score': score
                        }
                        _create_safe_task(self.output_manager.send_recommendation(rec), "send_recommendation")
                    except Exception as e:
                        logger.error(f"Failed to send Telegram notification: {e}")
                
                # Store entry data for position creation upon fill
                # Include trade tags for DISCOVERY analysis
                self._last_entry_data = {
                    'bias': bias,
                    'entry_price': current_price,
                    'position_size': position_size,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,  # Guardar TP para establecer después
                    'atr_1m': atr_1m,
                    'order_type': 'MARKET' if use_market_order else 'LIMIT',
                    # Trade type for position tracking
                    'trade_type': self._current_trade_type or (TradeType.LONG_MAIN if bias == 'LONG' else TradeType.SHORT_STRUCTURAL),
                    # DISCOVERY: Trade tags for segmented analysis
                    'tags': self._current_trade_tags.copy(),
                    'score': score,
                    'score_breakdown': breakdown.copy()
                }
                
                # Log tags for discovery tracking
                if OperatingMode.is_discovery():
                    logger.info(f"🏷️ Trade tags: score={score} | {self._current_trade_tags}")
                
                # =====================================================================
                # 🔒 FIX (Jan 13, 2026): INSTANT FILL ONLY IN BACKTEST
                # In LIVE, we MUST wait for fill confirmation via WAIT_FILL state.
                # Market orders may return status='closed' immediately from exchange API,
                # but this is NOT the same as a confirmed fill with correct size/price.
                # 
                # CRITICAL: In LIVE, always go to WAIT_FILL to:
                # 1. Verify position exists on exchange
                # 2. Get actual fill price (may differ from limit price)
                # 3. Get actual fill size (may be partial or rounded)
                # =====================================================================
                order_status = order_result.get('status', '')
                is_instant_fill = order_status in ('filled', 'closed') or order_result.get('filled', 0) > 0
                
                if self._is_backtest and is_instant_fill:
                    # BACKTEST ONLY: Skip WAIT_FILL since mock execution fills instantly
                    logger.info(f"✅ INSTANT FILL (backtest): {self.pending_order_id} - skipping WAIT_FILL")
                    
                    # Create position directly - same logic as _handle_wait_fill_state
                    fill_price = order_result.get('average') or order_result.get('price') or current_price
                    fill_size = order_result.get('filled', position_size)  # Use actual filled size
                    
                    self.risk_engine.create_position(
                        symbol=self.symbol,
                        direction=bias,
                        entry_price=fill_price,
                        position_size=fill_size,
                        stop_loss=stop_loss,
                        atr_1m=atr_1m,
                        market_time=market_time,
                        trade_type=self._current_trade_type or (TradeType.LONG_MAIN if bias == 'LONG' else TradeType.SHORT_STRUCTURAL)
                    )
                    
                    self.state_machine.record_trade_open(self.pending_order_id)
                    
                    # =========================================================================
                    # UPDATE SESSION TRADE COUNT & INITIALIZE TRADE LOGGER
                    # =========================================================================
                    session_for_count = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
                    self._session_trade_counts[session_for_count] = self._session_trade_counts.get(session_for_count, 0) + 1
                    logger.debug(f"📊 Session {session_for_count}: trade count → {self._session_trade_counts[session_for_count]}")
                    
                    # Start trade logging (generates trade_id for this trade)
                    # Wrapped in try/except to prevent logging errors from blocking state transition
                    try:
                        # Determine trade type for logging
                        trade_type_for_log = TradeType.SHORT_STRUCTURAL if bias == 'SHORT' else TradeType.LONG_MAIN
                        if hasattr(self, '_current_trade_type') and self._current_trade_type:
                            trade_type_for_log = self._current_trade_type
                        trade_type_name = trade_type_for_log.value if hasattr(trade_type_for_log, 'value') else str(trade_type_for_log)
                        
                        self._current_trade_id = self.trade_logger.start_trade(
                            symbol=self.symbol,
                            direction=bias,
                            entry_price=fill_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            position_size=fill_size,
                            entry_score=score,
                            entry_tags=list(self._current_trade_tags.keys()) if isinstance(self._current_trade_tags, dict) else [],
                            regime=self._current_volatility_regime.name,
                            volatility_regime=self._current_volatility_regime.name,
                            session=session_for_count,
                            trade_type=trade_type_name,
                            score_breakdown=breakdown.copy(),
                            metadata={'worst_case_rr': worst_case_rr if 'worst_case_rr' in dir() else None}
                        )
                    except Exception as log_err:
                        logger.error(f"TradeLogger.start_trade() failed (backtest): {log_err} - continuing without trade logging")
                        self._current_trade_id = None
                    
                    # Clear pending state
                    self.pending_order_id = None
                    self.entry_order_time = None
                    self._last_entry_data = None
                    
                    # Record trade price for duplicate zone filter
                    self._record_trade_price(fill_price)
                    
                    # Go directly to MANAGE
                    self.state_machine.transition_to(BotState.MANAGE, "Instant fill (backtest)")
                    
                    SIGNALS_GENERATED.labels(symbol=self.symbol, direction=bias).inc()
                    logger.info(f"🎯 Position opened: {bias} {fill_size} @ {fill_price:.2f}")
                else:
                    # NON-BACKTEST MODE: Always go through WAIT_FILL to verify position on exchange
                    # Even if exchange API returns 'filled', we must verify:
                    # - Position actually exists
                    # - Actual fill price (may differ from order price)
                    # - Actual fill size (may be partial or rounded)
                    if is_instant_fill and not self._is_backtest:
                        mode_name = self._runtime_config.mode.value if self._runtime_config else "UNKNOWN"
                        logger.info(f"⏳ Order returned filled status, but {mode_name} mode (instant_fill=False) requires verification via WAIT_FILL")
                    
                    self.state_machine.start_entry_timer()
                    self.state_machine.transition_to(BotState.WAIT_FILL, "Order placed")
                    
                    SIGNALS_GENERATED.labels(symbol=self.symbol, direction=bias).inc()
                    logger.info(f"Entry order placed: {self.pending_order_id}")
            else:
                # FIX (Jan 3, 2026): Reduced cooldown from 60 to 5 minutes
                logger.warning("⚠️ EXECUTION FAILED - Cooldown 5m (was 60m before fix)")
                self._reset_trade_state("Order placement failed")
                self.state_machine.start_cooldown(5)  # 5 minutes cooldown (reduced from 60)
        
        except Exception as e:
            # FIX (Jan 3, 2026): Reduced cooldown from 60 to 5 minutes
            logger.error(f"Error placing order: {e}", exc_info=True)
            logger.warning("⚠️ Order exception - Cooldown 5m (was 60m before fix)")
            self._reset_trade_state("Order exception")
            self.state_machine.start_cooldown(5)  # 5 minutes cooldown (reduced from 60)
    
    # ===== WAIT_FILL STATE - MONITOR ORDER =====
    
    async def _handle_wait_fill_state(self, market_time: datetime):
        """WAIT_FILL state: monitor pending order"""
        
        if not self.pending_order_id:
            # 🔒 LIMPIEZA si no hay orden pendiente
            self._reset_trade_state("WAIT_FILL with no pending order")
            self.state_machine.transition_to(BotState.IDLE, "No pending order")
            return
        
        # =====================================================================
        # 🔒 FIX (Jan 13, 2026): BACKTEST-ONLY instant fill from fills list
        # This path should ONLY be used in backtest mode.
        # In LIVE, we verify fills through the proper poll_fill_status flow below.
        # =====================================================================
        if self._is_backtest and hasattr(self.execution, 'fills'):
            for fill in self.execution.fills:
                if fill.get('order_id') == self.pending_order_id:
                    logger.info(f"✅ Order {self.pending_order_id} already filled (backtest) - processing immediately")
                    
                    if hasattr(self, '_last_entry_data') and self._last_entry_data:
                        entry_data = self._last_entry_data
                        fill_size = fill.get('amount', entry_data['position_size'])
                        fill_price = fill.get('price', entry_data['entry_price'])
                        
                        self.risk_engine.create_position(
                            symbol=self.symbol,
                            direction=entry_data['bias'],
                            entry_price=fill_price,
                            position_size=fill_size,
                            stop_loss=entry_data['stop_loss'],
                            atr_1m=entry_data['atr_1m'],
                            market_time=market_time,
                            trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL)
                        )
                        
                        # Record trade price for duplicate zone filter
                        self._record_trade_price(fill_price)
                        
                        self.state_machine.record_trade_open(self.pending_order_id)
                        self._reset_trade_state("Instant fill processed")
                        self.state_machine.transition_to(BotState.MANAGE, "Fill detected (backtest)")
                        logger.info(f"🎯 Position opened from WAIT_FILL: {entry_data['bias']} {fill_size} @ {fill_price:.2f}")
                        return
        
        # Check order timeout (3 minutes = 3 candles) - USE MARKET TIME
        if self.entry_order_time:
            elapsed = (market_time - self.entry_order_time).total_seconds()
            
            # ================================================================
            # 🚀 LIMIT → MARKET FALLBACK (FIX Jan 12, 2026)
            # If price escapes before fill, cancel LIMIT and send MARKET
            # This prevents missing momentum moves when using limit orders
            # 
            # VELOCITY CHECK ADDED: Only convert if candle shows momentum
            # (body_size > 0.25 ATR) to avoid converting in choppy ranges
            # ================================================================
            if elapsed > 2.0 and hasattr(self, '_last_entry_data') and self._last_entry_data:
                entry_data = self._last_entry_data
                order_type = entry_data.get('order_type', 'LIMIT')
                
                # Only apply to LIMIT orders
                if order_type == 'LIMIT':
                    entry_price = entry_data.get('entry_price', 0)
                    bias = entry_data.get('bias', '')
                    atr = entry_data.get('atr_1m', 100)  # Default ATR if not available
                    
                    # Get current price
                    current_price = self.market_data.last_price
                    
                    if current_price and entry_price and atr:
                        # Calculate how far price has moved away from limit
                        if bias == 'LONG':
                            price_escape = current_price - entry_price  # Positive = price moved up (escaped)
                        else:  # SHORT
                            price_escape = entry_price - current_price  # Positive = price moved down (escaped)
                        
                        escape_atr = price_escape / atr if atr > 0 else 0
                        
                        # ============================================================
                        # VELOCITY CHECK: Only convert if candle shows real momentum
                        # This prevents paying taker fees in choppy/ranging markets
                        # ============================================================
                        has_velocity = False
                        VELOCITY_THRESHOLD_ATR = 0.25  # Candle body must be > 0.25 ATR
                        
                        candles_1m = self.market_data.get_candles('1m')
                        if candles_1m and len(candles_1m) > 0:
                            last_candle = candles_1m[-1]
                            candle_open = float(last_candle.get('open', 0))
                            candle_close = float(last_candle.get('close', 0))
                            candle_body = abs(candle_close - candle_open)
                            body_atr_ratio = candle_body / atr if atr > 0 else 0
                            
                            # Check if candle body shows momentum
                            if body_atr_ratio > VELOCITY_THRESHOLD_ATR:
                                has_velocity = True
                                logger.debug(f"📊 Velocity check PASSED: body={candle_body:.0f} ({body_atr_ratio:.2f} ATR)")
                            else:
                                logger.debug(f"📊 Velocity check FAILED: body={candle_body:.0f} ({body_atr_ratio:.2f} ATR) < {VELOCITY_THRESHOLD_ATR}")
                        
                        # If price escaped > 0.15 ATR in favorable direction AND has velocity
                        ESCAPE_THRESHOLD_ATR = 0.15
                        if escape_atr > ESCAPE_THRESHOLD_ATR and has_velocity:
                            logger.warning(f"🚀 LIMIT→MARKET FALLBACK: Price escaped {escape_atr:.2f} ATR | {bias} @ {entry_price:.0f} → Current: {current_price:.0f}")
                            
                            # Cancel the limit order
                            try:
                                cancel_success = await self.execution.cancel_order(self.pending_order_id, self.symbol)
                                if cancel_success:
                                    logger.info(f"✅ Limit order {self.pending_order_id} cancelled for market conversion")
                                    
                                    # Clean up
                                    if self.pending_order_id in self.execution.open_orders:
                                        self.execution.open_orders.pop(self.pending_order_id, None)
                                    
                                    # Place MARKET order instead
                                    position_size = entry_data.get('position_size', 0)
                                    stop_loss = entry_data.get('stop_loss', 0)
                                    take_profit = entry_data.get('take_profit', 0)
                                    
                                    logger.info(f"📈 Placing MARKET order: {bias} {position_size:.6f} @ market (~{current_price:.0f})")
                                    
                                    market_result = await self.execution.place_market_order(
                                        symbol=self.symbol,
                                        side='buy' if bias == 'LONG' else 'sell',
                                        amount=position_size,
                                        stop_loss=stop_loss,
                                        take_profit=take_profit
                                    )
                                    
                                    if market_result and market_result.get('id'):
                                        self.pending_order_id = market_result['id']
                                        entry_data['order_type'] = 'MARKET'  # Update for tracking
                                        entry_data['entry_price'] = current_price  # Update expected price
                                        self.entry_order_time = market_time  # Reset timer
                                        logger.info(f"✅ Market order placed: {self.pending_order_id}")
                                        return  # Will process fill on next tick
                                    else:
                                        logger.error("❌ Failed to place market order after limit cancel")
                                        self._reset_trade_state("Market order failed after limit cancel")
                                        self.state_machine.transition_to(BotState.IDLE, "Market order failed")
                                        return
                                else:
                                    logger.warning("⚠️ Could not cancel limit order for market conversion")
                            except Exception as e:
                                logger.error(f"Error during LIMIT→MARKET conversion: {e}")
            
            if elapsed > ENTRY_TIMEOUT_MINUTES * 60:
                logger.warning(f"Entry timeout after {elapsed:.0f}s - attempting cancel")
                
                # ================================================================
                # FIX #3 (Jan 13, 2026): Enhanced timeout cleanup with late fill detection
                # ================================================================
                
                # Step 1: Check if order was ALREADY filled before we cancel
                pre_cancel_filled = False
                try:
                    fill_status = await self.execution.poll_fill_status(
                        self.pending_order_id, self.symbol
                    )
                    if fill_status.get('filled'):
                        logger.warning("⚠️ Order appears FILLED during timeout check - processing fill instead")
                        pre_cancel_filled = True
                        # Don't return - let the normal fill flow handle it below
                except Exception as e:
                    logger.debug(f"Pre-cancel fill check error: {e}")
                
                if not pre_cancel_filled:
                    # Step 2: Cancel the order
                    cancel_success = False
                    try:
                        cancel_success = await self.execution.cancel_order(
                            self.pending_order_id,
                            self.symbol
                        )
                        # 📊 CAPA 5: Record cancel in health monitor
                        if self.health_monitor:
                            self.health_monitor.record_order_canceled()  # No reason param
                    except Exception as e:
                        logger.error(f"Error cancelling order: {e}")
                    
                    # Step 3: Wait briefly and check for LATE FILL (race condition)
                    await asyncio.sleep(0.5)
                    
                    try:
                        exchange_has_position = await self.execution.has_open_position(self.symbol)
                        if exchange_has_position:
                            logger.critical("🚨 LATE FILL DETECTED: Order filled after timeout!")
                            logger.critical("🔄 Closing orphan position created by late fill...")
                            
                            # Get position details
                            exchange_pos = await self.execution.get_position(self.symbol)
                            if exchange_pos:
                                size = abs(float(exchange_pos.get('contracts', 0) or 0))
                                side = exchange_pos.get('side', '').lower()
                                
                                if size >= KRAKEN_BTC_MIN_SIZE:
                                    close_side = 'sell' if side == 'long' else 'buy'
                                    await self.execution.close_position_market(
                                        symbol=self.symbol,
                                        side=close_side,
                                        amount=size,
                                        reason="LATE_FILL"
                                    )
                                    logger.info(f"✅ Late fill position closed: {size:.6f} {side}")
                                    
                                    # Notify
                                    if self.output_manager:
                                        await self.output_manager.send_critical_alert(
                                            f"🚨 LATE FILL: Closed orphan {side} {size:.6f} (order filled after timeout)"
                                        )
                                elif size > 0:
                                    logger.warning(f"⚠️ Late fill size {size:.6f} < min - considered closed")
                    except Exception as e:
                        logger.error(f"Error checking late fill: {e}")
                    
                    # Step 4: Clean up local state
                    if self.pending_order_id in self.execution.open_orders:
                        self.execution.open_orders.pop(self.pending_order_id, None)
                        logger.debug(f"Cleaned order {self.pending_order_id} from local tracking")
                    
                    # Step 5: Reconcile to ensure clean state
                    await self._reconcile_orders()
                    
                    # Step 6: Complete cleanup
                    self._reset_trade_state("Order timeout (enhanced)")
                    self.state_machine.transition_to(BotState.IDLE, "Entry timeout")
                    return
        
        # Check if order was filled
        try:
            fill_status = await self.execution.poll_fill_status(
                self.pending_order_id,
                self.symbol
            )
            
            # Handle rejected order (POST_ONLY that crossed book)
            if fill_status.get('rejected'):
                logger.warning(f"🚫 Order {self.pending_order_id} was REJECTED by exchange (likely POST_ONLY crossed book)")
                
                # 📊 CAPA 5: Record rejection in health monitor
                if self.health_monitor:
                    self.health_monitor.record_order_rejected()  # No reason param
                
                # Track consecutive POST_ONLY rejections for guard
                if not hasattr(self, '_consecutive_post_only_rejects'):
                    self._consecutive_post_only_rejects = 0
                self._consecutive_post_only_rejects += 1
                logger.debug(f"POST_ONLY rejection count: {self._consecutive_post_only_rejects}")
                
                self._reset_trade_state("Order rejected by exchange")
                self.state_machine.transition_to(BotState.IDLE, "Order rejected")
                return
            
            # Reset rejection counter on successful fill
            self._consecutive_post_only_rejects = 0
            
            if fill_status.get('filled'):
                # Order filled - VERIFICAR POSICIÓN REAL antes de crear
                logger.info(f"✅ Entry order FILLED - verifying position on exchange...")
                
                # 📊 CAPA 3: Capture order→fill latency for tags
                fill_latency_ms = 0
                if hasattr(self, '_last_entry_data') and self._last_entry_data:
                    entry_ts = self._last_entry_data.get('tags', {}).get('entry_timestamp_ms', 0)
                    if entry_ts:
                        fill_ts = int(market_time.timestamp() * 1000)
                        fill_latency_ms = fill_ts - entry_ts
                        self._last_entry_data['tags']['order_fill_latency_ms'] = fill_latency_ms
                        if fill_latency_ms > 5000:  # > 5 seconds
                            logger.info(f"🏷️ TAG: Order→fill latency: {fill_latency_ms}ms (slow)")
                
                # 📊 CAPA 5: Record fill in health monitor
                if self.health_monitor:
                    self.health_monitor.record_order_filled(latency_ms=fill_latency_ms)
                
                # CRITICAL: Verificar que hay contracts en exchange
                await asyncio.sleep(0.5)  # Dar tiempo a que exchange actualice
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                
                if not exchange_has_position:
                    logger.error("⚠️ Order filled but NO position on exchange - possible partial fill or error")
                    self._reset_trade_state("Fill reported but no position")
                    self.state_machine.transition_to(BotState.IDLE, "No position after fill")
                    return
                
                logger.info("✅ Position confirmed on exchange - creating local tracking")
                
                # 🔒 CRITICAL: Validate side consistency before proceeding
                # This prevents SL/TP placement with wrong side (wouldNotReducePosition error)
                if hasattr(self, '_last_entry_data') and self._last_entry_data:
                    expected_side = self._last_entry_data.get('bias', '').lower()  # 'long' or 'short'
                    position_data = await self.execution.get_position(self.symbol)
                    if position_data:
                        exchange_side = position_data.get('side', '').lower()
                        if exchange_side and expected_side and exchange_side != expected_side:
                            logger.critical(f"🚨 SIDE MISMATCH: System={expected_side.upper()} vs Exchange={exchange_side.upper()}")
                            logger.critical("⚠️ Correcting system side to match exchange reality")
                            self._last_entry_data['bias'] = exchange_side.upper()
                        elif exchange_side:
                            logger.info(f"✅ Side validated: System={expected_side.upper()} == Exchange={exchange_side.upper()}")
                
                # Limpiar de open_orders
                if self.pending_order_id in self.execution.open_orders:
                    self.execution.open_orders.pop(self.pending_order_id, None)
                
                # Create Position object for risk management
                # 🔥 FIX #4: Use ACTUAL fill price/size from exchange, not intended values
                if hasattr(self, '_last_entry_data') and self._last_entry_data:
                    entry_data = self._last_entry_data
                    
                    # Get actual fill data from exchange position
                    actual_price = entry_data['entry_price']  # Fallback
                    actual_size = entry_data['position_size']  # Fallback
                    
                    if position_data:
                        # Use exchange-reported values if available
                        exchange_price = float(position_data.get('entryPrice', 0) or 0)
                        exchange_qty = abs(float(position_data.get('contracts', 0) or 0))
                        
                        if exchange_price > 0:
                            if abs(exchange_price - actual_price) > 0.01:
                                logger.info(f"🔄 Using actual fill price: {actual_price:.2f} → {exchange_price:.2f}")
                            actual_price = exchange_price
                        
                        if exchange_qty > 0:
                            if abs(exchange_qty - actual_size) > 0.00001:
                                logger.info(f"🔄 Using actual fill size: {actual_size:.6f} → {exchange_qty:.6f}")
                            actual_size = exchange_qty
                    
                    self.risk_engine.create_position(
                        symbol=self.symbol,
                        direction=entry_data['bias'],
                        entry_price=actual_price,
                        position_size=actual_size,
                        stop_loss=entry_data['stop_loss'],
                        atr_1m=entry_data['atr_1m'],
                        market_time=market_time,  # Use market time, not datetime.now()
                        trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL)
                    )
                    trade_type_str = entry_data.get('trade_type', TradeType.LONG_MAIN)
                    trade_type_name = trade_type_str.value if hasattr(trade_type_str, 'value') else str(trade_type_str)
                    logger.info(f"Position created: {entry_data['bias']} {actual_size:.6f} @ {actual_price:.2f} [{trade_type_name}]")
                    
                    # =========================================================================
                    # UPDATE SESSION TRADE COUNT & INITIALIZE TRADE LOGGER (WAIT_FILL path)
                    # =========================================================================
                    session_for_count = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
                    self._session_trade_counts[session_for_count] = self._session_trade_counts.get(session_for_count, 0) + 1
                    logger.debug(f"📊 Session {session_for_count}: trade count → {self._session_trade_counts[session_for_count]}")
                    
                    # Start trade logging (generates trade_id for this trade)
                    # Wrapped in try/except to prevent logging errors from blocking state transition
                    trade_tags = entry_data.get('tags', {})
                    trade_score_breakdown = entry_data.get('score_breakdown', {})
                    try:
                        # Determine trade type for logging
                        trade_type_for_log = entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL)
                        trade_type_name = trade_type_for_log.value if hasattr(trade_type_for_log, 'value') else str(trade_type_for_log)
                        
                        self._current_trade_id = self.trade_logger.start_trade(
                            symbol=self.symbol,
                            direction=entry_data['bias'],
                            entry_price=actual_price,
                            stop_loss=entry_data['stop_loss'],
                            take_profit=entry_data.get('take_profit'),
                            position_size=actual_size,
                            entry_score=entry_data.get('score', 0),
                            entry_tags=list(trade_tags.keys()) if isinstance(trade_tags, dict) else [],
                            regime=trade_tags.get('regime', 'UNKNOWN'),
                            volatility_regime=trade_tags.get('volatility_regime', 'NORMAL'),
                            session=session_for_count,
                            trade_type=trade_type_name,
                            score_breakdown=trade_score_breakdown,
                            metadata={'worst_case_rr': trade_tags.get('rr_worst_case')}
                        )
                    except Exception as log_err:
                        logger.error(f"TradeLogger.start_trade() failed: {log_err} - continuing without trade logging")
                        self._current_trade_id = None
                    
                    # 📊 CAPA 5: Record trade entry in health monitor
                    if self.health_monitor:
                        self.health_monitor.record_trade_entry(
                            side=entry_data['bias'],
                            size=entry_data['position_size'],
                            score=self._entry_score
                        )
                    # 🔒 CAPA 1: Validate position integrity against exchange
                    if position_data and self.risk_engine.position:
                        exchange_side = position_data.get('side', '').lower()
                        exchange_qty = abs(float(position_data.get('contracts', 0)))
                        
                        integrity_ok = self.risk_engine.position.validate_integrity(
                            exchange_side=exchange_side,
                            exchange_qty=exchange_qty,
                            qty_tolerance=0.02  # 2% tolerance for partial fills
                        )
                        
                        if not integrity_ok:
                            # Check what kind of error it was
                            pos = self.risk_engine.position
                            has_side_mismatch = any('SIDE MISMATCH' in e for e in pos.integrity_errors)
                            has_qty_mismatch = any('QTY MISMATCH' in e for e in pos.integrity_errors)
                            
                            if has_side_mismatch:
                                # Real side mismatch - critical, block SL/TP
                                logger.critical("🚨 SIDE MISMATCH - SL/TP placement BLOCKED")
                                if self.health_monitor:
                                    self.health_monitor.record_integrity_error(
                                        f"side_mismatch: system={entry_data['bias']} vs exchange={exchange_side}"
                                    )
                                self._stops_set = True  # Prevent stop placement
                            elif has_qty_mismatch:
                                # Partial fill - size already adjusted, safe to place SL/TP
                                logger.warning("⚠️ Partial fill detected - size adjusted, proceeding with SL/TP")
                                if self.health_monitor:
                                    self.health_monitor.record_integrity_error(
                                        f"qty_mismatch: expected={pos.expected_qty:.6f} vs exchange={exchange_qty:.6f}",
                                        is_critical=False
                                    )
                    
                    # 🔥 ESTABLECER SL/TP EN LA POSICIÓN ABIERTA (UNA SOLA VEZ)
                    if not self._stops_set:
                        logger.info("📍 Setting SL/TP on open position...")
                        
                        # 🔥 FIX #3: Retry SL placement with EXPONENTIAL BACKOFF
                        max_sl_retries = 3
                        stops_set = False
                        base_delay = 0.5  # Start at 500ms
                        
                        for attempt in range(max_sl_retries):
                            stops_set = await self.execution.set_position_stops(
                                symbol=self.symbol,
                                stop_loss=entry_data['stop_loss'],
                                take_profit=entry_data.get('take_profit')  # TP si existe
                            )
                            
                            if stops_set:
                                logger.info(f"✅ SL/TP successfully set on position (attempt {attempt + 1})")
                                self._stops_set = True
                                # Mark reduce-only validated in position
                                if self.risk_engine.position:
                                    self.risk_engine.position.mark_reduce_only_validated()
                                    # 🔥 CRITICAL FIX: Mark that exchange has the SL
                                    # This prevents local SL checks from triggering double close
                                    self.risk_engine.position.mark_exchange_sl_active()
                                break
                            else:
                                logger.warning(f"⚠️ SL/TP placement attempt {attempt + 1}/{max_sl_retries} failed")
                                if attempt < max_sl_retries - 1:
                                    # Exponential backoff: 0.5s, 1s, 2s
                                    delay = base_delay * (2 ** attempt)
                                    logger.info(f"⏳ Waiting {delay:.1f}s before retry...")
                                    await asyncio.sleep(delay)
                        
                        if not stops_set:
                            # 🔥 CRITICAL: All retries failed - Mark SL as inactive
                            logger.critical("🚨 CRITICAL: SL/TP placement FAILED after 3 retries - FORCE CLOSING position!")
                            if self.risk_engine.position:
                                self.risk_engine.position.mark_exchange_sl_inactive("All placement attempts failed")
                            if self.health_monitor:
                                self.health_monitor.record_integrity_error(
                                    "SL_PLACEMENT_FAILED: Force closing unprotected position",
                                    is_critical=True
                                )
                            await self.emergency_close_all()
                            return
                    
                else:
                    # 🔥 AUDIT FIX: entry_data does not exist here - this is an error path
                    logger.error("No entry data available - Fill without entry tracking")
                    # No entry data - error en tracking
                    self.pending_order_id = None
                    self.state_machine.transition_to(BotState.IDLE, "Fill without entry data")
                    return
                
                # Limpia pending_order_id PERO mantiene _last_entry_data para tracking
                self.pending_order_id = None
                self.state_machine.transition_to(BotState.MANAGE, "Order filled")
            else:
                # Still pending - stay in WAIT_FILL
                pass
        
        except Exception as e:
            logger.error(f"Error checking fill status: {e}")
    
    # ===== MANAGE STATE - RUN POSITION =====
    
    async def _handle_manage_state(self, current_price: float, atr_1m: float, market_time: datetime):
        """MANAGE state: run open position"""
        
        # =====================================================================
        # 🔒 HARD_LOCK CHECK - No subsystem can act during transition periods
        # The reconciler enforces this to prevent premature reactions
        # =====================================================================
        can_act, block_reason = self.reconciler.can_subsystems_act()
        if not can_act:
            logger.debug(f"🔒 Subsystems BLOCKED: {block_reason}")
            return
        
        # 🔥 FIX #2: Skip if sync operation is in progress
        if getattr(self, '_syncing_position', False):
            logger.debug("Position sync in progress - skipping MANAGE operations")
            return
        
        # 🔥 OPTIMIZATION (Jan 20, 2026): Use reconciler snapshot instead of API call
        # This was calling has_open_position() EVERY tick = 1+ second per tick!
        # The reconciler already has the position state from its last poll.
        snapshot = self.reconciler.get_snapshot()
        exchange_has_position = snapshot.has_position if snapshot else True  # Assume has position if no snapshot
        
        if not exchange_has_position:
            logger.warning("⚠️ MANAGE state but NO position on exchange (reconciler snapshot)")
            
            # Verificar si tenemos posición local
            if self.risk_engine.position:
                logger.critical("🔄 Position closed on exchange but exists locally - syncing...")
                
                # Calcular PnL aproximado (no sabemos precio exacto de cierre)
                position = self.risk_engine.position
                estimated_pnl = 0  # No podemos calcular exacto
                
                # Registrar cierre (SYNC event, NOT a real loss)
                self.state_machine.record_trade_close(estimated_pnl, False, exit_reason='RECONCILE_CLOSE')
                
                # Limpiar posición local
                self.risk_engine.position = None
            else:
                logger.warning("No local position either - likely only pending orders exist")
            
            # Cancelar todas las órdenes pendientes (SL/TP sin posición)
            logger.info("Cancelling all pending orders (no position to protect)")
            cancelled = await self.execution.cancel_all_orders(self.symbol)
            if cancelled > 0:
                logger.info(f"Cancelled {cancelled} pending orders")
            
            # Limpieza completa y volver a IDLE
            self._reset_trade_state("No position on exchange")
            self.state_machine.transition_to(BotState.IDLE, "No position")
            return
        
        # 🔥 RETRY: Intentar establecer SL/TP si no se estableció en fill
        if not self._stops_set and hasattr(self, '_last_entry_data') and self._last_entry_data:
            logger.warning("🔄 RETRY: Attempting to set SL/TP (not set during fill)")
            try:
                stops_set = await self.execution.set_position_stops(
                    symbol=self.symbol,
                    stop_loss=self._last_entry_data['stop_loss'],
                    take_profit=self._last_entry_data.get('take_profit')
                )
                
                if stops_set:
                    # VERIFICAR que realmente se colocaron en exchange
                    await asyncio.sleep(0.5)
                    verification = await self.execution.verify_stops_on_exchange(self.symbol)
                    
                    if verification['has_sl']:
                        logger.info("✅ SL/TP successfully set AND verified on exchange")
                        self._stops_set = True
                        # 🔥 CRITICAL: Mark exchange SL as active
                        if self.risk_engine.position:
                            self.risk_engine.position.mark_exchange_sl_active()
                    else:
                        logger.critical("❌ SL/TP claimed success but NOT found on exchange - UNPROTECTED!")
                        # Mark SL as inactive - local check will handle closure
                        if self.risk_engine.position:
                            self.risk_engine.position.mark_exchange_sl_inactive("Not found on exchange after placement")
                        # Intentar nuevamente con parámetros ajustados
                        logger.info("Retrying with different parameters...")
                        await asyncio.sleep(1)
                        retry2 = await self.execution.set_position_stops(
                            symbol=self.symbol,
                            stop_loss=self._last_entry_data['stop_loss'],
                            take_profit=self._last_entry_data.get('take_profit')
                        )
                        if retry2:
                            self._stops_set = True
                            if self.risk_engine.position:
                                self.risk_engine.position.mark_exchange_sl_active()
                else:
                    logger.error("❌ Failed to set SL/TP on retry - position still at risk!")
                    if self.risk_engine.position:
                        self.risk_engine.position.mark_exchange_sl_inactive("All retry attempts failed")
            except Exception as e:
                logger.error(f"Error setting stops on retry: {e}")
        
        # Verificar que tenemos posición local
        position = self.risk_engine.position
        if not position:
            # 🔒 LIMPIEZA COMPLETA si no hay posición local
            logger.warning("MANAGE state but no local position - transitioning to IDLE")
            self._reset_trade_state("MANAGE state with no position")
            self.state_machine.transition_to(BotState.IDLE, "No position")
            return

        # Track MFE/MAE for edge enforcement
        if position.entry_price > 0:
            if position.direction == 'LONG':
                excursion = (current_price - position.entry_price) / position.entry_price
            else:  # SHORT
                excursion = (position.entry_price - current_price) / position.entry_price
            
            # Update MFE (Max Favorable Excursion)
            if excursion > self._current_trade_mfe:
                self._current_trade_mfe = excursion
            # Update MAE (Max Adverse Excursion) - negative excursion
            if excursion < 0 and abs(excursion) > self._current_trade_mae:
                self._current_trade_mae = abs(excursion)

        # ==================================================================
        # TIER 2 INTEGRATION (FIX #6): Funding-based forced exit
        # TIER 2 INTEGRATION (FIX #13): Time-based forced exit
        # ==================================================================

        try:
            # FIX #6: exit on extreme funding (LONG only)
            # IMPROVED (Jan 12): Use REAL time until next funding settlement
            time_to_funding_min = None
            try:
                if hasattr(self.external_data, 'funding'):
                    time_to_funding_min = self.external_data.funding.get_time_to_next_funding_minutes()
            except Exception as e:
                logger.debug(f"Could not get time to funding: {e}")
            
            exit_funding, funding_exit_reason = await should_exit_on_funding(
                self.external_data,
                self.symbol,
                position.direction,
                unrealized_pnl=position.unrealized_pnl,
                time_to_funding_min=time_to_funding_min
            )
            if exit_funding:
                logger.critical(f"🚨 FORCED EXIT (Funding): {funding_exit_reason}")
                # 🔐 FIX #2: Hard-lock during exit to prevent state corruption
                self.reconciler.notify_exit_started("FUNDING_EXIT")
                
                # 🔒 FIX (Jan 14, 2026): Get real size from exchange
                close_size = position.size
                try:
                    exchange_pos = await self.execution.get_position(self.symbol)
                    if exchange_pos:
                        close_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                except Exception as e:
                    logger.debug(f"Could not get exchange position for FUNDING_EXIT (using local size): {e}")
                
                if close_size < KRAKEN_BTC_MIN_SIZE:
                    logger.warning(f"⚠️ FUNDING_EXIT skipped: size {close_size:.6f} < min")
                    return
                
                await self.execution.close_position_market(
                    symbol=self.symbol,
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=close_size,
                    reason="FUNDING_EXIT"
                )
                pnl_funding = position.unrealized_pnl
                self.state_machine.record_trade_close(pnl_funding, pnl_funding > 0, exit_reason='FUNDING_EXIT')
                # 🔥 DAILY DRAWDOWN: Track funding exit PnL
                self.state_machine.update_daily_pnl(pnl_funding)
                
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': position.unrealized_pnl,
                    'reason': 'FUNDING_EXIT',
                    'r': position.calculate_r_realized(current_price),
                    'mfe_r': position.calculate_mfe_r(),
                    'mae_r': position.calculate_mae_r(),
                    'direction': position.direction,
                    'regime': position.regime
                })
                
                if self.output_manager:
                    # Get fresh capital for notification
                    try:
                        fresh_capital = await self.execution.get_available_capital()
                        if fresh_capital > 0:
                            self.risk_engine.capital = fresh_capital
                    except Exception as e:
                        logger.debug(f"Could not refresh capital for funding exit notification: {e}")
                    
                    _create_safe_task(self.output_manager.send_exit_notification({
                        'symbol': self.symbol,
                        'reason': 'FUNDING_EXIT',
                        'pnl': position.unrealized_pnl,
                        'r_realized': position.calculate_r_realized(current_price),
                        'exit_price': current_price,
                        'timestamp': market_time.isoformat(),
                        'account_value': self.risk_engine.capital
                    }), "funding_exit_notification")
                
                self.risk_engine.position = None
                self._reset_trade_state(f"Trade closed: FUNDING_EXIT")
                self.state_machine.start_cooldown()
                self.state_machine.transition_to(BotState.IDLE, "Trade closed: FUNDING_EXIT")
                return

            # FIX #13: time-based exit (stagnation-based, not clock-based)
            # REDESIGNED (Jan 4, 2026): Pass current ATR for stagnation check
            current_atr = position.atr_1m  # Use stored ATR, can be updated
            exit_time, time_exit_reason = position.should_force_exit_by_time(market_time, current_atr)
            if exit_time:
                logger.critical(f"🚨 FORCED EXIT (Time): {time_exit_reason}")
                # 🔐 FIX #2: Hard-lock during exit to prevent state corruption
                self.reconciler.notify_exit_started("TIME_EXIT")
                
                # 🔒 FIX (Jan 14, 2026): Get real size from exchange
                close_size = position.size
                try:
                    exchange_pos = await self.execution.get_position(self.symbol)
                    if exchange_pos:
                        close_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                except Exception as e:
                    logger.debug(f"Could not get exchange position for TIME_EXIT (using local size): {e}")
                
                if close_size < KRAKEN_BTC_MIN_SIZE:
                    logger.warning(f"⚠️ TIME_EXIT skipped: size {close_size:.6f} < min")
                    return
                
                await self.execution.close_position_market(
                    symbol=self.symbol,
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=close_size,
                    reason="TIME_EXIT"
                )
                pnl_time = position.unrealized_pnl
                self.state_machine.record_trade_close(pnl_time, pnl_time > 0, exit_reason='TIME_EXIT')
                # 🔥 DAILY DRAWDOWN: Track time exit PnL
                self.state_machine.update_daily_pnl(pnl_time)
                
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': position.unrealized_pnl,
                    'reason': 'TIME_BASED_EXIT',
                    'r': position.calculate_r_realized(current_price),
                    'mfe_r': position.calculate_mfe_r(),
                    'mae_r': position.calculate_mae_r(),
                    'direction': position.direction,
                    'regime': position.regime
                })
                
                if self.output_manager:
                    # Get fresh capital for notification
                    try:
                        fresh_capital = await self.execution.get_available_capital()
                        if fresh_capital > 0:
                            self.risk_engine.capital = fresh_capital
                    except Exception as e:
                        logger.debug(f"Could not refresh capital for time exit notification: {e}")
                    
                    _create_safe_task(self.output_manager.send_exit_notification({
                        'symbol': self.symbol,
                        'reason': f'TIME_EXIT ({time_exit_reason})',
                        'pnl': position.unrealized_pnl,
                        'r_realized': position.calculate_r_realized(current_price),
                        'exit_price': current_price,
                        'timestamp': market_time.isoformat(),
                        'account_value': self.risk_engine.capital
                    }), "time_exit_notification")
                
                self.risk_engine.position = None
                self._reset_trade_state(f"Trade closed: TIME_BASED_EXIT")
                self.state_machine.start_cooldown()
                self.state_machine.transition_to(BotState.IDLE, "Trade closed: TIME_BASED_EXIT")
                return

        except Exception as e:
            logger.error(f"Error during forced-exit checks: {e}")
        
        # =====================================================================
        # OPTIMIZED (Jan 20, 2026): Use cached indicators system
        # Only recalculate when new candle arrives, not every tick
        # This dramatically reduces CPU usage in MANAGE state
        # =====================================================================
        candles_1m = self.market_data.get_candles('1m')
        candles_5m = self.market_data.get_candles('5m')
        candles_1h = self.market_data.get_candles('1h')
        
        # Get cached indicators (will recalculate only if candle changed)
        cached = self._get_cached_indicators(candles_1m, candles_5m, candles_1h)
        
        # Extract cached values
        current_adx = cached.get('adx_1h')
        atr_ratio = cached.get('atr_ratio')
        ema20_5m = cached.get('ema20_5m')
        rsi_1m = cached.get('rsi_1m')
        bb_width = cached.get('bb_width')
        bb_avg = cached.get('bb_avg')
        vpoc_price = cached.get('vpoc')
        df_1m = cached.get('df_1m')
        
        # Get volume from cached DataFrame (fast - no new DataFrame creation)
        if df_1m is not None and len(df_1m) > 0:
            current_volume = df_1m['volume'].iloc[-1]
            avg_volume = df_1m['volume'].mean()
        else:
            current_volume = 0
            avg_volume = 0
        
        # Find TP2 target
        tp2_target = self._find_tp2_target()
        
        # Check for BOS against position
        has_bos_against = self._check_bos_against_position(position.direction, candles_5m)
        
        # 🔥 FIX #3: Block logical exits when exchange is unavailable
        # Only emergency close with confirmed position is allowed
        if not self.reconciler.is_exchange_available():
            if self.reconciler.is_degraded:
                logger.warning("⚠️ DEGRADED MODE: Skipping logical exit checks - exchange unavailable")
            return  # Don't make any decisions based on stale data
        
        # =====================================================================
        # 🔒 FIX (Jan 13, 2026): SYNC SIZE FROM RECONCILER SNAPSHOT
        # OPTIMIZED (Jan 20, 2026): Use cached snapshot instead of API call
        # The reconciler already polls exchange every 5 seconds
        # =====================================================================
        if not self._is_backtest:
            try:
                snapshot = self.reconciler.get_snapshot()
                if snapshot and snapshot.has_position:
                    exchange_size = snapshot.position_size
                    if exchange_size > 0 and abs(exchange_size - position.size) > 0.00001:
                        logger.info(f"🔄 Pre-manage size sync: local={position.size:.6f} → snapshot={exchange_size:.6f}")
                        # 🔒 FIX: Use thread-safe sync method
                        await self.risk_engine.sync_position_size(exchange_size, "pre-manage")
            except Exception as e:
                logger.warning(f"Could not sync size before manage: {e}")
        
        # Manage position (con datos de estructura para SMART BE + TIME_INVALIDATION híbrido)
        exit_action = await self.risk_engine.manage_position(
            current_price,
            atr_1m,
            current_volume,
            avg_volume,
            tp2_target=tp2_target,
            ema20_5m=ema20_5m,
            has_bos_against=has_bos_against,
            market_time=market_time,
            adx=current_adx,           # For hybrid TIME_INVALIDATION
            atr_ratio=atr_ratio,       # For volatility check
            rsi_1m=rsi_1m,             # For momentum exit
            vpoc=vpoc_price,           # For VPOC resistance exit
            bb_width=bb_width,         # For volatility squeeze
            bb_avg=bb_avg              # For volatility squeeze comparison
        )
        
        if exit_action:
            action = exit_action.get('action')

            # Execute exchange action to match local risk engine decisions
            try:
                if action == 'PARTIAL_CLOSE':
                    close_size = float(exit_action.get('size', 0) or 0)
                    
                    # 🔒 FIX (Jan 14, 2026): Validate minimum order size before executing
                    if close_size < KRAKEN_BTC_MIN_SIZE:
                        logger.warning(f"⚠️ PARTIAL_CLOSE blocked: size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                        return
                    
                    if close_size > 0:
                        # 🔐 FIX #2: Hard-lock during PARTIAL close (uses timeout, not position=0)
                        self.reconciler.notify_partial_close("SMART_PARTIAL_CLOSE")
                        await self.execution.close_position_market(
                            symbol=self.symbol,
                            side='sell' if position.direction == 'LONG' else 'buy',
                            amount=close_size,
                            reason="PARTIAL"
                        )
                        
                        # 🔥 CRITICAL: Sync local size with exchange after partial close
                        await asyncio.sleep(0.5)  # Wait for exchange to update
                        try:
                            exchange_pos = await self.execution.get_position(self.symbol)
                            if exchange_pos:
                                exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                                if exchange_size > 0 and abs(exchange_size - position.size) > 0.00001:
                                    logger.warning(f"🔄 Size sync after TP1: local={position.size:.6f} → exchange={exchange_size:.6f}")
                                    position.size = exchange_size
                            else:
                                # No position on exchange - full close happened
                                logger.critical("⚠️ No position on exchange after partial close - was fully closed")
                                position.size = 0
                        except Exception as sync_e:
                            logger.error(f"Error syncing size after partial: {sync_e}")

                    # Mark TP1 reached for edge enforcement
                    self._reached_tp1 = True
                    
                    await self.trade_logger.log_event('tp1_hit', {
                        'entry': position.entry_price,
                        'price': current_price,
                        'size_closed': close_size,
                        'pnl_realized': exit_action.get('pnl', 0),
                        'reason': exit_action.get('reason', 'TP1')
                    })
                    # Stay in MANAGE with reduced local size
                    return

                if action == 'ADD_POSITION':
                    # SCALE INTO WINNER: Trade has proven itself (MFE >= 0.6R)
                    add_size = float(exit_action.get('add_size', 0))
                    if add_size > 0:
                        # =====================================================================
                        # 🔒 FIX (Jan 13, 2026): VERIFY POSITION BEFORE SCALE-IN
                        # Ensure we have a confirmed position with correct size before adding
                        # =====================================================================
                        
                        # Verify position exists on exchange and get actual size
                        exchange_pos = await self.execution.get_position(self.symbol)
                        if not exchange_pos:
                            logger.warning("⚠️ ADD_POSITION blocked: No position confirmed on exchange")
                            return
                        
                        exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                        if exchange_size < 0.00001:
                            logger.warning("⚠️ ADD_POSITION blocked: Exchange position size is zero")
                            return
                        
                        # Sync local size if different
                        if abs(exchange_size - position.size) > 0.00001:
                            logger.warning(f"🔄 Size sync before ADD: local={position.size:.6f} → exchange={exchange_size:.6f}")
                            position.size = exchange_size
                        
                        # Round add_size to exchange precision
                        add_size = round_size_to_exchange(add_size)
                        if add_size <= 0:
                            logger.warning("⚠️ ADD_POSITION blocked: add_size rounds to zero")
                            return
                        
                        # Place additional order at market
                        side = 'buy' if position.direction == 'LONG' else 'sell'
                        logger.info(f"💪 ADDING TO WINNER: {add_size:.6f} {side}")
                        try:
                            add_order = await self.execution.place_market_order(
                                symbol=self.symbol,
                                side=side,
                                amount=add_size
                            )
                            if add_order:
                                # Wait for fill confirmation and get actual filled size
                                await asyncio.sleep(0.5)  # Brief wait for exchange to update
                                
                                # Re-fetch position to get actual new size
                                new_exchange_pos = await self.execution.get_position(self.symbol)
                                if new_exchange_pos:
                                    new_exchange_size = abs(float(new_exchange_pos.get('contracts', 0) or 0))
                                    actual_add = new_exchange_size - exchange_size
                                    
                                    if actual_add > 0:
                                        position.size = new_exchange_size
                                        logger.info(f"✅ Position scaled: added {actual_add:.6f}, new size = {position.size:.6f}")
                                    else:
                                        logger.warning(f"⚠️ Scale-in order may not have filled: expected +{add_size:.6f}, got +{actual_add:.6f}")
                                        position.size = new_exchange_size  # Use exchange truth anyway
                                else:
                                    # Fallback: assume fill succeeded
                                    position.size = exchange_size + add_size
                                    logger.warning(f"⚠️ Could not verify scale-in - assuming filled: {position.size:.6f}")
                                
                                await self.trade_logger.log_event('add_to_winner', {
                                    'entry': position.entry_price,
                                    'price': current_price,
                                    'add_size': add_size,
                                    'new_total_size': position.size,
                                    'reason': exit_action.get('reason', 'ADD_POSITION')
                                })
                        except Exception as e:
                            logger.error(f"Failed to add to winner: {e}")
                    # Stay in MANAGE
                    return

                if action == 'FULL_CLOSE':
                    # � FIX #2: Hard-lock during exit to prevent state corruption
                    exit_reason = exit_action.get('reason', 'FULL_CLOSE')
                    self.reconciler.notify_exit_started(exit_reason)
                    
                    # �🔥 CRITICAL: Get REAL size from exchange before closing
                    try:
                        exchange_pos = await self.execution.get_position(self.symbol)
                        if exchange_pos:
                            exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                            if exchange_size > 0:
                                # Use exchange size, not local size
                                if abs(exchange_size - position.size) > 0.00001:
                                    logger.warning(f"🔄 Size correction for FULL_CLOSE: local={position.size:.6f} → exchange={exchange_size:.6f}")
                                close_size = exchange_size
                            else:
                                logger.warning("⚠️ No position on exchange for FULL_CLOSE - already closed")
                                close_size = 0
                        else:
                            logger.warning("⚠️ Could not get exchange position - using local size")
                            close_size = float(exit_action.get('size', 0) or position.size)
                    except Exception as e:
                        logger.error(f"Error getting exchange size: {e} - using local size")
                        close_size = float(exit_action.get('size', 0) or position.size)
                    
                    if close_size > 0:
                        await self.execution.close_position_market(
                            symbol=self.symbol,
                            side='sell' if position.direction == 'LONG' else 'buy',
                            amount=close_size,
                            reason="FULL_CLOSE"
                        )
                    
                    # Check if TP2 was reached
                    reason = exit_action.get('reason', '')
                    if 'TP2' in reason or 'tp2' in reason:
                        self._reached_tp2 = True
                    if 'BE' in reason or 'breakeven' in reason.lower():
                        self._is_be_stop = True
                    
                    # 🔥 CRITICAL: Verify position is actually closed after FULL_CLOSE
                    await asyncio.sleep(0.5)
                    try:
                        still_has_position = await self.execution.has_open_position(self.symbol)
                        if still_has_position:
                            logger.critical("⚠️ FULL_CLOSE executed but position still exists! Emergency close...")
                            # Force close remaining
                            exchange_pos = await self.execution.get_position(self.symbol)
                            if exchange_pos:
                                remaining = abs(float(exchange_pos.get('contracts', 0) or 0))
                                # 🔥 FIX #5: Check remaining > MIN_SIZE before emergency close
                                # to prevent double close from creating reverse position
                                min_size = 0.00001  # Minimum BTC size
                                if remaining > min_size:
                                    await self.execution.close_position_market(
                                        symbol=self.symbol,
                                        side='sell' if position.direction == 'LONG' else 'buy',
                                        amount=remaining,
                                        reason="RESIDUAL"
                                    )
                                    logger.info(f"✅ Emergency closed remaining {remaining:.6f}")
                                else:
                                    logger.info(f"✓ Remaining size {remaining:.8f} below minimum - position effectively closed")
                    except Exception as verify_e:
                        logger.error(f"Error verifying close: {verify_e}")
                        
            except Exception as e:
                logger.error(f"Error executing exchange close for {action}: {e}")

            # Record full-close only
            pnl = exit_action.get('pnl', 0)
            r_realized = position.calculate_r_realized(current_price) if position else 0
            
            # Refresh account value after trade
            try:
                fresh_capital = await self.execution.get_available_capital()
                if fresh_capital > 0:
                    self.risk_engine.capital = fresh_capital
            except Exception as e:
                logger.warning(f"Could not refresh capital for notification: {e}")
            
            if self.output_manager:
                _create_safe_task(self.output_manager.send_exit_notification({
                    'symbol': self.symbol,
                    'reason': exit_action.get('reason', 'FULL_CLOSE'),
                    'pnl': pnl,
                    'r_realized': r_realized,
                    'exit_price': current_price,
                    'timestamp': market_time.isoformat(),
                    'account_value': self.risk_engine.capital
                }), "full_close_notification")
            
            is_win = pnl > 0
            exit_reason = exit_action.get('reason', 'unknown')

            self.state_machine.record_trade_close(pnl, is_win, exit_reason=exit_reason)
            
            # =========================================================================
            # COMPLETE TRADE LOGGING (persist trade with exit data)
            # =========================================================================
            if self._current_trade_id and self.trade_logger:
                mfe_r = position.calculate_mfe_r() if position else 0
                mae_r = position.calculate_mae_r() if position else 0
                
                try:
                    self.trade_logger.complete_trade(
                        trade_id=self._current_trade_id,
                        exit_price=current_price,
                        exit_reason=exit_reason,
                        pnl_usd=pnl,
                        pnl_r=r_realized,
                        mfe_r=mfe_r,
                        mae_r=mae_r
                    )
                    logger.debug(f"📊 Trade {self._current_trade_id} logged: exit={exit_reason}, pnl=${pnl:.2f}")
                except Exception as log_err:
                    logger.error(f"TradeLogger.complete_trade() failed: {log_err}")
                self._current_trade_id = None  # Reset for next trade
            
            # =====================================================================
            # PULLBACK SHORT TRACKING - Record result for limits/consecutive losses
            # =====================================================================
            if self._current_trade_type == TradeType.SHORT_PULLBACK:
                self.pullback_short_engine.record_trade_result(pnl, is_win, market_time)
                logger.info(f"[PULLBACK] Trade result recorded: {'WIN' if is_win else 'LOSS'} ${pnl:.2f}")
                
                # 📊 METRICS: Track pullback short exit
                mfe_r = position.calculate_mfe_r()
                mae_r = position.calculate_mae_r()
                update_pullback_short_exit(self.symbol, pnl=pnl, is_win=is_win, mfe_r=mfe_r, mae_r=mae_r)
                
                # Update status metrics
                stats = self.pullback_short_engine.get_daily_stats()
                update_pullback_short_status(
                    self.symbol,
                    daily_count=stats.get('total_trades', 0),
                    consecutive_losses=stats.get('consecutive_losses', 0),
                    cumulative_pnl=stats.get('cumulative_pnl', 0.0)
                )
            
            # 🔥 DAILY DRAWDOWN: Update after each trade close
            if self.state_machine.update_daily_pnl(pnl):
                # Daily halt triggered - close any remaining position and cancel orders
                logger.critical("🛑 Daily drawdown limit reached - EMERGENCY CLOSE + trading halted for today")
                await self._emergency_daily_halt_close()
            
            # DISCOVERY: Update score distribution metrics
            if hasattr(self, '_entry_score') and self._entry_score > 0:
                self._update_score_metrics(self._entry_score, pnl, is_win)
            
            # Record for loss cooldown tracking
            self._record_trade_result(exit_reason, pnl, market_time)
            
            # Record for filter pipeline (overtrade filter zone tracking)
            if hasattr(self, 'filter_pipeline') and self._current_trade_entry_time:
                self.filter_pipeline.record_trade_result(
                    entry_price=self._current_trade_entry_price or position.entry_price,
                    exit_price=current_price,
                    entry_time=self._current_trade_entry_time,
                    exit_time=market_time,
                    direction=self._current_trade_direction or 'LONG',
                    pnl=pnl
                )
            
            # Record trade for edge enforcement degradation monitoring
            if self._current_trade_entry_time:
                self._record_trade_for_edge_enforcement(
                    entry_price=self._current_trade_entry_price,
                    exit_price=current_price,
                    entry_time=self._current_trade_entry_time,
                    exit_time=market_time,
                    reached_tp1=self._reached_tp1,
                    reached_tp2=self._reached_tp2,
                    is_be_stop=self._is_be_stop
                )

            await self.trade_logger.log_event('trade_closed', {
                'entry': exit_action.get('entry_price', position.entry_price),
                'exit': current_price,
                'size': exit_action.get('size', position.size),
                'pnl': pnl,
                'reason': exit_action.get('reason', 'unknown'),
                'r': position.calculate_r_realized(current_price),
                'mfe_r': position.calculate_mfe_r(),
                'mae_r': position.calculate_mae_r(),
                'direction': position.direction,
                'regime': position.regime
            })

            self._reset_trade_state(f"Trade closed: {exit_action.get('reason')}")

            # SMART COOLDOWN: Based on exit reason and loss size
            pnl_r = position.calculate_r_realized(current_price) if position else 0
            base_cooldown = OperatingMode.get_smart_cooldown(exit_reason, pnl_r)
            
            # Check if signal still aligned with HTF - reduce cooldown if so
            final_cooldown = base_cooldown
            try:
                if self.current_bias and position:
                    # If we exited and HTF bias still agrees with trade direction → reduce cooldown
                    if self.current_bias == position.direction:
                        final_cooldown = int(base_cooldown * OperatingMode.COOLDOWN_ALIGNED_REDUCTION)
                        logger.info(f"📊 HTF still aligned ({self.current_bias}) - cooldown reduced: {base_cooldown}→{final_cooldown}min")
            except Exception as e:
                logger.warning(f"Could not check alignment for cooldown: {e}")
            
            logger.info(f"⏱️ Smart cooldown: {final_cooldown}min (reason={exit_reason}, pnl_r={pnl_r:.2f})")
            self.state_machine.start_cooldown(final_cooldown)
            self.state_machine.transition_to(BotState.IDLE, f"Trade closed: {exit_action.get('reason')}")
    
    # ===== HELPERS =====
    
    async def _open_position_from_order(self, fill_status: dict):
        """Open position after order fill"""
        try:
            fill_price = fill_status.get('price', 0)
            fill_size = fill_status.get('amount', 0)
            
            # Calculate stop loss and ATR
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 14:
                df_1m = pd.DataFrame(candles_1m)
                highs = df_1m['high'].values
                lows = df_1m['low'].values
                closes = df_1m['close'].values
                atr_1m = calculate_atr(highs, lows, closes, 14)[-1]
            else:
                logger.warning("Not enough 1m data for ATR, using default")
                atr_1m = fill_price * 0.01  # 1% as fallback
            
            # Find support/resistance
            candles_5m = self.market_data.get_candles('5m')
            df_5m = pd.DataFrame(candles_5m) if candles_5m else pd.DataFrame()
            support_resistance = self._find_support_resistance(self.current_bias, df_5m)
            
            # Calculate ATR ratio for dynamic SL sizing
            atr_ratio_for_sl = None
            if len(df_5m) >= 14:
                atr_values = calculate_atr(df_5m['high'].values, df_5m['low'].values, df_5m['close'].values, 14)
                if len(atr_values) >= 10:
                    current_atr = atr_values[-1]
                    avg_atr = sum(atr_values[-10:]) / 10
                    if avg_atr > 0:
                        atr_ratio_for_sl = current_atr / avg_atr
            
            # Calculate stop loss
            stop_loss = self.risk_engine.calculate_stop_loss(
                fill_price,
                self.current_bias,
                support_resistance,
                atr_1m,
                atr_ratio=atr_ratio_for_sl
            )
            
            # Open position in risk engine (use market_time for consistency)
            market_time = self.market_data.get_market_time()
            position = await self.risk_engine.open_position(
                symbol=self.symbol,
                direction=self.current_bias,
                entry_price=fill_price,
                stop_loss=stop_loss,
                atr_1m=atr_1m,
                hour=market_time.hour if market_time else 12,
                minute=market_time.minute if market_time else 0
            )
            
            if position:
                # Log trade open
                await self.trade_logger.log_event('trade_opened', {
                    'symbol': self.symbol,
                    'direction': self.current_bias,
                    'entry': fill_price,
                    'size': fill_size,
                    'stop_loss': stop_loss
                })
                
                # Initialize edge enforcement tracking
                self._current_trade_entry_time = market_time
                self._current_trade_entry_price = fill_price
                self._current_trade_mfe = 0.0
                self._current_trade_mae = 0.0
                self._reached_tp1 = False
                self._reached_tp2 = False
                self._is_be_stop = False
                
                # Transition to MANAGE
                self.state_machine.transition_to(BotState.MANAGE, "Position opened")
                logger.debug(f"Position opened: {self.current_bias} {fill_size} @ {fill_price:.2f}")
            else:
                logger.error("Failed to open position in risk engine")
                self.state_machine.transition_to(BotState.IDLE, "Failed to open position")
                
        except Exception as e:
            logger.error(f"Error opening position from order: {e}", exc_info=True)
            self.state_machine.transition_to(BotState.IDLE, "Error opening position")
    
    def _find_support_resistance(self, bias: str, df_5m: pd.DataFrame) -> float:
        """Find technical support/resistance level based on swing structure
        
        EDGE FIX: Use proper swing lookback (10 candles = 50min) not just 5,
        and this is just the BASE level - ATR buffer added in calculate_stop_loss
        """
        # Use 10 candles (50min) for more meaningful structure
        lookback = min(10, len(df_5m))
        
        if bias == 'LONG':
            # Recent swing low (HL)
            return df_5m['low'].iloc[-lookback:].min()
        else:
            # Recent swing high (LH)
            return df_5m['high'].iloc[-lookback:].max()
    
    # =========================================================================
    # INDICATOR CACHE SYSTEM (Jan 20, 2026)
    # Only recalculate indicators when new candle CLOSES (not just new candle starts)
    # This dramatically reduces CPU usage during tick processing
    # Key insight: Use candles[-2] (last CLOSED candle) as the cache key
    # =========================================================================
    def _get_cached_indicators(self, candles_1m: list, candles_5m: list, candles_1h: list) -> dict:
        """
        Get indicators with caching - only recalculate when candle CLOSES.
        
        Uses candles[-2] (last CLOSED candle) as the cache key, not candles[-1] (forming).
        Returns dict with: adx_1h, atr_1m, atr_ratio, ema20_5m, rsi_1m, bb_width, bb_avg, vpoc
        All values may be None if insufficient data.
        """
        cache = self._indicator_cache
        needs_1m_update = False
        needs_5m_update = False
        needs_1h_update = False
        
        # Check if LAST CLOSED candle changed (candles[-2] is the last closed one)
        # When a new candle closes, candles[-2] gets a new timestamp
        if candles_1m and len(candles_1m) >= 2:
            closed_ts_1m = candles_1m[-2].get('timestamp', 0)  # LAST CLOSED candle
            if closed_ts_1m != cache['last_candle_ts_1m']:
                cache['last_candle_ts_1m'] = closed_ts_1m
                cache['df_1m'] = pd.DataFrame(candles_1m)
                needs_1m_update = True
        
        if candles_5m and len(candles_5m) >= 2:
            closed_ts_5m = candles_5m[-2].get('timestamp', 0)  # LAST CLOSED candle
            if closed_ts_5m != cache['last_candle_ts_5m']:
                cache['last_candle_ts_5m'] = closed_ts_5m
                cache['df_5m'] = pd.DataFrame(candles_5m)
                needs_5m_update = True
        
        if candles_1h and len(candles_1h) >= 2:
            closed_ts_1h = candles_1h[-2].get('timestamp', 0)  # LAST CLOSED candle
            if closed_ts_1h != cache['last_candle_ts_1h']:
                cache['last_candle_ts_1h'] = closed_ts_1h
                cache['df_1h'] = pd.DataFrame(candles_1h)
                needs_1h_update = True
        
        # Update 1h indicators (ADX) - only when new 1h candle CLOSES
        if needs_1h_update and cache['df_1h'] is not None and len(cache['df_1h']) >= 20:
            try:
                df_1h = cache['df_1h']
                adx_arr, _ = calculate_adx(df_1h['high'].values, df_1h['low'].values, df_1h['close'].values)
                if len(adx_arr) > 0 and not np.isnan(adx_arr[-1]):
                    cache['adx_1h'] = float(adx_arr[-1])
            except Exception as e:
                logger.debug(f"ADX 1h calculation failed: {e}")
        
        # Update 1m indicators (ATR, RSI, BB, VPOC) - only when new 1m candle
        if needs_1m_update and cache['df_1m'] is not None:
            df_1m = cache['df_1m']
            
            # ATR and ratio
            if len(df_1m) >= 20:
                try:
                    atr_values = calculate_atr(df_1m['high'].values, df_1m['low'].values, df_1m['close'].values)
                    if len(atr_values) >= 10:
                        cache['atr_1m'] = float(atr_values[-1])
                        avg_atr = float(np.mean(atr_values[-10:]))
                        cache['atr_ratio'] = cache['atr_1m'] / avg_atr if avg_atr > 0 else 1.0
                except Exception as e:
                    logger.debug(f"ATR/ratio calculation failed: {e}")
            
            # RSI
            if len(df_1m) >= 14:
                try:
                    closes = df_1m['close'].values
                    rsi_values = calculate_rsi(closes, 14)
                    if len(rsi_values) > 0:
                        cache['rsi_1m'] = float(rsi_values[-1])
                except Exception as e:
                    logger.debug(f"RSI calculation failed: {e}")
            
            # Bollinger Bands
            if len(df_1m) >= 20:
                try:
                    closes = df_1m['close'].values
                    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, period=20, std_dev=2)
                    if len(bb_upper) > 0 and len(bb_lower) > 0:
                        cache['bb_width'] = float(bb_upper[-1] - bb_lower[-1])
                        if len(bb_upper) >= 20:
                            bb_widths = bb_upper[-20:] - bb_lower[-20:]
                            cache['bb_avg'] = float(np.mean(bb_widths))
                        else:
                            cache['bb_avg'] = cache['bb_width']
                except Exception as e:
                    logger.debug(f"Bollinger Bands calculation failed: {e}")
            
            # VPOC - use cached df_1m (already a DataFrame)
            if cache['df_1m'] is not None and len(cache['df_1m']) >= 50:
                try:
                    from src.engine.advanced_filters import calculate_vpoc
                    cache['vpoc'] = calculate_vpoc(cache['df_1m'].tail(100) if len(cache['df_1m']) >= 100 else cache['df_1m'])
                except Exception as e:
                    logger.debug(f"VPOC calculation failed: {e}")
                    cache['vpoc'] = None
        
        # Update 5m indicators (EMA20) - only when new 5m candle
        if needs_5m_update and cache['df_5m'] is not None and len(cache['df_5m']) >= 20:
            try:
                df_5m = cache['df_5m']
                closes_5m = df_5m['close'].values
                ema_20 = calculate_ema(closes_5m, 20)
                if len(ema_20) > 0:
                    cache['ema20_5m'] = float(ema_20[-1])
            except Exception as e:
                logger.debug(f"EMA20 5m calculation failed: {e}")
        
        return {
            'adx_1h': cache.get('adx_1h'),
            'atr_ratio': cache.get('atr_ratio'),
            'ema20_5m': cache.get('ema20_5m'),
            'rsi_1m': cache.get('rsi_1m'),
            'bb_width': cache.get('bb_width'),
            'bb_avg': cache.get('bb_avg'),
            'vpoc': cache.get('vpoc'),
            'df_1m': cache.get('df_1m'),
            'df_5m': cache.get('df_5m'),
            'df_1h': cache.get('df_1h'),
        }

    def _calculate_atr_5m(self, df_5m: pd.DataFrame) -> float:
        """Calculate ATR from 5m timeframe for more stable SL sizing"""
        if len(df_5m) < 14:
            return 0.0
        
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        closes = df_5m['close'].values
        
        tr = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr.append(max(hl, hc, lc))
        
        if len(tr) < 14:
            return sum(tr) / len(tr) if tr else 0.0
        
        # Simple moving average of TR (last 14 periods)
        return sum(tr[-14:]) / 14
    
    def _calculate_atr_generic(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate ATR for any timeframe with configurable period"""
        if len(df) < period:
            return 0.0
        
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        
        tr = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr.append(max(hl, hc, lc))
        
        if len(tr) < period:
            return sum(tr) / len(tr) if tr else 0.0
        
        return sum(tr[-period:]) / period
    
    async def _check_position_has_stops(self) -> bool:
        """
        🔒 FIX (Jan 17, 2026): Check if current position has SL/TP protection on exchange
        
        Returns True if at least SL exists, False otherwise.
        Used by STATE OVERRIDE to detect orphan positions without protection.
        """
        if not self.execution:
            return False
        
        try:
            verification = await self.execution.verify_stops_on_exchange(self.symbol)
            return verification.get('has_sl', False)
        except Exception as e:
            logger.error(f"Error checking stops: {e}")
            return False  # Assume no protection on error (safer)
    
    def _check_atr_regime_allows_entry(self, df_5m: pd.DataFrame) -> tuple[bool, str]:
        """
        ATR REGIME FILTER: Block entries when ATR is already expanded
        
        We want to enter during CONTRACTION, not EXPANSION.
        If ATR is above 65th percentile of recent ATR, the move already happened.
        
        Returns: (can_enter, reason)
        """
        if len(df_5m) < 50:
            return True, "Not enough data for ATR regime"
        
        # Calculate ATR for each candle
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        closes = df_5m['close'].values
        
        atrs = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            atrs.append(max(hl, hc, lc))
        
        if len(atrs) < 30:
            return True, "Not enough ATR history"
        
        current_atr = atrs[-1]
        # Use last 50 candles for percentile (250 min = ~4 hours)
        lookback = min(50, len(atrs))
        recent_atrs = atrs[-lookback:]
        
        # Calculate 50th percentile (median) - more strict, want low ATR environments
        sorted_atrs = sorted(recent_atrs)
        p50_idx = int(len(sorted_atrs) * 0.50)
        atr_p50 = sorted_atrs[p50_idx]
        
        if current_atr > atr_p50:
            return False, f"ATR regime HIGH: {current_atr:.2f} > P50 {atr_p50:.2f} (move already happened)"
        
        return True, f"ATR regime OK: {current_atr:.2f} <= P50 {atr_p50:.2f}"
    
    def _check_volatility_minimum(self, df_1m: pd.DataFrame) -> tuple[bool, str]:
        """
        VOLATILITY MINIMUM FILTER: Block trades in low volatility regimes
        
        Problem in P3 (Dec 15-22): ATR was low, market in micro-range/chop.
        Low ATR → small SL → appears "cheap" → overtrading → stops accumulate
        
        Solution: Compare current ATR to its own 200-period SMA.
        If atr_ratio < 0.75, we're in a low-vol regime where sweeps don't expand.
        
        atr_ratio = atr_current / sma(atr, 200)
        
        Returns: (can_enter, reason)
        """
        if len(df_1m) < 200:
            return True, "Not enough data for volatility filter"
        
        # Calculate ATR for each candle
        highs = df_1m['high'].values
        lows = df_1m['low'].values
        closes = df_1m['close'].values
        
        atrs = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            atrs.append(max(hl, hc, lc))
        
        if len(atrs) < 200:
            return True, "Not enough ATR samples"
        
        # Current ATR (last 14 periods average)
        current_atr = sum(atrs[-14:]) / 14
        
        # SMA of ATR over last 200 periods
        atr_sma_200 = sum(atrs[-200:]) / 200
        
        if atr_sma_200 <= 0:
            return True, "ATR SMA is zero"
        
        atr_ratio = current_atr / atr_sma_200
        
        # Threshold: 0.75 = 25% below normal volatility
        MIN_VOL_RATIO = 0.75
        
        if atr_ratio < MIN_VOL_RATIO:
            return False, f"LOW VOL REGIME: ATR ratio {atr_ratio:.2f} < {MIN_VOL_RATIO} (chop zone)"
        
        return True, f"Volatility OK: ATR ratio {atr_ratio:.2f} >= {MIN_VOL_RATIO}"
    
    def _check_loss_cooldown(self, market_time: datetime) -> tuple[bool, str]:
        """
        LOSS STREAK COOLDOWN: Block trades after consecutive losses
        
        After 3 consecutive SL hits, enter 90-minute cooldown.
        This protects against chop regimes where stops cluster.
        
        IMPROVED (Jan 15, 2026): Smart cooldown reset via MicroTrendFilter
        - Can exit cooldown early if micro trend becomes bullish for N bars
        - Still respects minimum cooldown (5 min safety)
        
        Returns: (can_enter, reason)
        """
        # Check if we're still in cooldown
        if self._cooldown_until is not None:
            if market_time < self._cooldown_until:
                remaining = (self._cooldown_until - market_time).total_seconds() / 60
                
                # SMART COOLDOWN RESET (Jan 15, 2026)
                # Check if micro trend allows early exit (after minimum 5 min)
                min_cooldown_passed = remaining < (self._cooldown_minutes - 5)  # At least 5 min in cooldown
                if min_cooldown_passed and self.micro_trend_filter:
                    should_reset, reset_reason = self.micro_trend_filter.should_reset_cooldown(
                        min_bullish_bars=3, min_strength=0.5
                    )
                    if should_reset:
                        logger.info(f"🔄 SMART COOLDOWN RESET: {reset_reason}")
                        logger.info(f"   Exiting cooldown early ({remaining:.0f} min remaining → 0)")
                        self._cooldown_until = None
                        self._consecutive_losses = 0
                        return True, f"Smart reset: {reset_reason}"
                
                return False, f"LOSS COOLDOWN: {remaining:.0f} min remaining ({self._consecutive_losses} consecutive losses)"
            else:
                # Cooldown expired, reset
                logger.info("✅ Loss cooldown expired, trading resumed")
                self._cooldown_until = None
                self._consecutive_losses = 0
        
        return True, f"No cooldown active (streak: {self._consecutive_losses})"
    
    def _record_trade_result(self, exit_reason: str, pnl: float, market_time: datetime):
        """
        Record trade result for cooldown tracking.
        Called when a trade closes.
        
        IMPROVED (Jan 16, 2026): Weighted loss streak by exit type
        - STOP_LOSS: Full weight (1.0) - indicates filter failure
        - TIME_INVALIDATION: Partial weight (0.5) - market didn't move
        - TRAILING_STOP: Small weight (0.25) - partial success
        - TP1_FULL/TP2: Zero weight - wins reset streak
        """
        # 📊 CAPA 5: Record exit in health monitor
        if self.health_monitor:
            exit_type = 'sl' if exit_reason == 'STOP_LOSS' or pnl < 0 else 'tp'
            self.health_monitor.record_exit(exit_type=exit_type)
        
        if pnl < 0:
            # Get weight for this exit type from LossStreakConfig
            exit_weight = LossStreakConfig.EXIT_TYPE_WEIGHTS.get(exit_reason, LossStreakConfig.DEFAULT_WEIGHT)
            
            # Add weighted loss to streak
            old_streak = self._consecutive_losses
            self._consecutive_losses += exit_weight
            
            # Track exit type distribution
            if not hasattr(self, '_loss_exit_types'):
                self._loss_exit_types = {}
            self._loss_exit_types[exit_reason] = self._loss_exit_types.get(exit_reason, 0) + 1
            
            logger.info(f"📉 Loss recorded: {exit_reason} (weight={exit_weight:.2f}) → streak: {old_streak:.2f} → {self._consecutive_losses:.2f}")
            
            # TIERED COOLDOWN SYSTEM - now uses weighted threshold
            effective_losses = int(self._consecutive_losses)  # Round down for threshold comparison
            
            if effective_losses >= 3:
                # 3+ weighted losses = longer cooldown (60 min)
                cooldown_mins = self._cooldown_minutes
                self._cooldown_until = market_time + timedelta(minutes=cooldown_mins)
                logger.warning(f"🚫 TIER 2 COOLDOWN: {self._consecutive_losses:.1f} weighted losses → {cooldown_mins} min pause")
            elif effective_losses >= self._max_consecutive_losses:
                # 2 weighted losses = shorter cooldown (30 min)
                cooldown_mins = self._cooldown_minutes_tier1
                self._cooldown_until = market_time + timedelta(minutes=cooldown_mins)
                logger.warning(f"⚠️ TIER 1 COOLDOWN: {self._consecutive_losses:.1f} weighted losses → {cooldown_mins} min pause")
        else:
            # Win or breakeven resets the streak
            if self._consecutive_losses > 0:
                logger.info(f"✅ Win/BE recorded: resetting loss streak from {self._consecutive_losses:.1f}")
            self._consecutive_losses = 0
    
    def _check_expected_r(self, current_price: float, sl_distance: float, bias: str, df_1h: pd.DataFrame) -> tuple[bool, str]:
        """
        EXPECTED R FILTER: Only enter if potential reward >= 1.5R
        
        This is THE critical filter that P3 needs.
        
        Problem: System enters setups that can only produce 1R max.
        With SL 2-2.5 ATR, if market can only give 1R, we're mathematically doomed.
        
        Solution: Calculate distance to next HTF level (resistance/support).
        If expected_move / sl_distance < 1.5 → NO trade.
        
        This kills trades in the middle of ranges where there's no asymmetry.
        
        Returns: (can_enter, reason)
        """
        if len(df_1h) < 20:
            return True, "Not enough HTF data for Expected R"
        
        if sl_distance <= 0:
            return True, "Invalid SL distance"
        
        highs = df_1h['high'].values
        lows = df_1h['low'].values
        
        # Find recent swing high and swing low (last 20 1h candles = 20 hours)
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        range_size = recent_high - recent_low
        
        if range_size <= 0:
            return True, "No valid range"
        
        # Calculate position in range (0 = at low, 1 = at high)
        position_in_range = (current_price - recent_low) / range_size
        
        if bias == 'LONG':
            # For LONG: target is recent high (or higher)
            # Distance to target = recent_high - current_price
            distance_to_target = recent_high - current_price
            
            # If we're already near the top (>70% of range), very little upside
            if position_in_range > 0.70:
                return False, f"LONG near range top: {position_in_range:.0%} (no upside room)"
            
        else:  # SHORT
            # For SHORT: target is recent low (or lower)
            # Distance to target = current_price - recent_low
            distance_to_target = current_price - recent_low
            
            # If we're already near the bottom (<30% of range), very little downside
            if position_in_range < 0.30:
                return False, f"SHORT near range bottom: {position_in_range:.0%} (no downside room)"
        
        # Calculate Expected R
        expected_r = distance_to_target / sl_distance
        
        # Minimum threshold: 1.5R
        MIN_EXPECTED_R = 1.5
        
        if expected_r < MIN_EXPECTED_R:
            return False, f"Expected R too low: {expected_r:.2f}R < {MIN_EXPECTED_R}R (target ${distance_to_target:.0f} vs SL ${sl_distance:.0f})"
        
        return True, f"Expected R OK: {expected_r:.2f}R >= {MIN_EXPECTED_R}R (pos {position_in_range:.0%} in range)"
    
    def _calculate_chase_atr(self, df_1m: pd.DataFrame, atr_1m: float, bias: str) -> float:
        """
        Calculate how many ATRs price has moved in the signal direction recently.
        
        This measures "chasing" - entering after the move already happened.
        
        Returns:
            Chase distance in ATRs (higher = more chasing)
        """
        from src.engine.config import OrderExecution
        
        if len(df_1m) < 5 or atr_1m <= 0:
            return 0.0
        
        lookback = OrderExecution.ANTI_CHASING_LOOKBACK  # Default 2-3
        
        # Calculate total directional move in last N candles
        if bias == 'LONG':
            # For LONG, chasing = price went up
            move = df_1m['close'].iloc[-1] - df_1m['open'].iloc[-lookback]
        else:
            # For SHORT, chasing = price went down  
            move = df_1m['open'].iloc[-lookback] - df_1m['close'].iloc[-1]
        
        # Negative move means price moved against our direction (good for entry)
        if move <= 0:
            return 0.0
        
        # 🔒 FIX: Prevent division by zero
        if atr_1m <= 0:
            return 0.0
        
        return move / atr_1m
    
    def _check_not_post_impulse(self, df_1m: pd.DataFrame, atr_1m: float) -> tuple[bool, str]:
        """
        ANTI-IMPULSE FILTER: Block entries after impulsive candles
        
        If recent candles are > 1.2 ATR, we're entering AFTER the move.
        We want pullback/absorption, not chasing.
        
        Returns: (can_enter, reason)
        """
        from src.engine.config import OrderExecution
        
        if len(df_1m) < 5:
            return True, "Not enough 1m data"
        
        if atr_1m <= 0:
            return True, "Invalid ATR"
        
        # Use config values for threshold and lookback
        impulse_threshold = OrderExecution.ANTI_CHASING_ATR_THRESHOLD  # Default 1.2
        lookback = OrderExecution.ANTI_CHASING_LOOKBACK  # Default 3
        
        for i in range(-lookback, 0):
            candle_range = abs(df_1m['close'].iloc[i] - df_1m['open'].iloc[i])
            if candle_range > atr_1m * impulse_threshold:
                return False, f"Post-impulse: Candle {i} range {candle_range:.2f} > {atr_1m * impulse_threshold:.2f} ({impulse_threshold}x ATR)"
        
        return True, "No recent impulse detected"
    
    def _check_sweep_occurred(self, df_5m: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """
        SWEEP FILTER: Only enter if there was a liquidity sweep recently
        
        A sweep occurs when:
        - For LONG: Price went below recent lows (swept stops) then recovered
        - For SHORT: Price went above recent highs (swept stops) then recovered
        
        This captures the "stop hunt before expansion" pattern that precedes
        real moves, filtering out entries without prior liquidity grab.
        
        Returns: (sweep_occurred, reason)
        """
        if len(df_5m) < 15:
            return True, "Not enough data for sweep detection"
        
        # Look at last 10 candles for sweep patterns
        lookback = min(10, len(df_5m) - 5)
        
        # Get the range we're analyzing (excluding last 2 candles = current action)
        recent = df_5m.iloc[-lookback-2:-2]
        current = df_5m.iloc[-2:]
        
        if len(recent) < 5:
            return True, "Not enough history"
        
        # Find key levels to sweep
        if bias == 'LONG':
            # For LONG: We want to see a sweep below recent lows, then recovery
            recent_low = recent['low'].min()
            
            # Check if any of last 5 candles wicked below that low then closed above
            for i in range(-5, 0):
                candle = df_5m.iloc[i]
                low_wick = candle['low']
                close = candle['close']
                open_price = candle['open']
                
                # Sweep pattern: wick below recent low, close above it
                if low_wick < recent_low and close > recent_low:
                    # Additional: body should be bullish or at least not strongly bearish
                    if close >= open_price * 0.998:  # Allow small bearish body (0.2%)
                        return True, f"SWEEP detected: Low wick {low_wick:.2f} < {recent_low:.2f}, close {close:.2f} recovered"
            
            return False, f"No LONG sweep: Recent low {recent_low:.2f} not swept and recovered"
            
        else:  # SHORT
            # For SHORT: We want to see a sweep above recent highs, then rejection
            recent_high = recent['high'].max()
            
            # Check if any of last 5 candles wicked above that high then closed below
            for i in range(-5, 0):
                candle = df_5m.iloc[i]
                high_wick = candle['high']
                close = candle['close']
                open_price = candle['open']
                
                # Sweep pattern: wick above recent high, close below it
                if high_wick > recent_high and close < recent_high:
                    # Additional: body should be bearish or at least not strongly bullish
                    if close <= open_price * 1.002:  # Allow small bullish body (0.2%)
                        return True, f"SWEEP detected: High wick {high_wick:.2f} > {recent_high:.2f}, close {close:.2f} rejected"
            
            return False, f"No SHORT sweep: Recent high {recent_high:.2f} not swept and rejected"
    
    # =========================================================================
    # ENTRY SCORE SYSTEM - Score > Gates architecture
    # 4 blocks × 0-2 points = max 8. Threshold: 5
    # Only Structure=0 blocks entry (the only real gate)
    # =========================================================================
    
    def _calculate_entry_score(self, df_1h: pd.DataFrame, df_5m: pd.DataFrame, 
                                df_1m: pd.DataFrame, bias: str, current_price: float,
                                market_time: datetime) -> tuple[int, dict, bool]:
        """
        Calculate entry score using 4 blocks.
        Returns: (total_score, breakdown_dict, structure_blocks)
        
        If structure_blocks=True, DO NOT ENTER regardless of score.
        This is the ONLY gate.
        """
        breakdown = {
            'structure': 0,
            'timing': 0,
            'momentum': 0,
            'context': 0,
            'synergy': 0  # Non-linear bonus
        }
        
        # Reset trade tags for this evaluation
        self._current_trade_tags = {
            'score': 0,
            'mode': OperatingMode.MODE,
            'htf_aligned': False,
            'ltf_clean': False,
            'bos_confirmed': False,
            'ema_reclaim': False,
            'adx': 0.0,
            'adx_slope': 0.0,
            'rsi': 0.0,
            'atr': 0.0,
            'session': 'unknown',
            'volatility_ratio': 0.0
        }
        
        # 1️⃣ STRUCTURE (0-2) - THE ONLY GATE
        structure_score, structure_blocks, htf_clean = self._score_structure(df_1h, df_5m, bias, current_price)
        breakdown['structure'] = structure_score
        self._current_trade_tags['htf_aligned'] = structure_score > 0
        self._current_trade_tags['ltf_clean'] = structure_score == 2
        
        if structure_blocks:
            # Structure = 0 is the only hard gate
            return 0, breakdown, True
        
        # 2️⃣ TIMING (0-2)
        timing_score, bos_confirmed = self._score_timing(df_1m, bias)
        
        # SEMANTIC COHERENCE: Timing capped to 1 if Structure = 0
        # OB touch + divergence but no CHoCH/BOS = reversal noise, not continuation
        # (Note: If Structure = 0 we already blocked above, but this future-proofs
        #  any refactoring that allows Structure = 0 without blocking)
        if breakdown['structure'] == 0:
            timing_score = min(timing_score, 1)
            logger.debug(f"⚠️ Timing capped to 1 (Structure=0 → coherencia semántica)")
        
        breakdown['timing'] = timing_score
        self._current_trade_tags['bos_confirmed'] = bos_confirmed
        
        # 3️⃣ MOMENTUM (0-2)
        breakdown['momentum'] = self._score_momentum(df_5m, bias)
        
        # MOMENTUM COHERENCE CHECK (Jan 9, 2026):
        # If momentum = 0 AND structure < 2, this is a low quality setup
        # Momentum 0 in weak structure = noise, not signal
        if breakdown['momentum'] == 0 and breakdown['structure'] < 2:
            logger.warning(f"⚠️ Weak momentum (0) with partial structure ({breakdown['structure']}) - low quality setup")
            # Reduce structure score to cap total
            breakdown['structure'] = max(0, breakdown['structure'] - 1)
        
        # 4️⃣ CONTEXT (0-2)
        breakdown['context'] = self._score_context(df_5m, market_time)
        
        # 5️⃣ SYNERGY BONUS (0-1) - Non-linear interaction
        # HTF clean + BOS confirmed together = more valuable than separately
        if htf_clean and bos_confirmed:
            breakdown['synergy'] = 1
            logger.debug("✨ Synergy bonus: HTF clean + BOS confirmed")
        
        total = sum(breakdown.values())
        
        # 6️⃣ MICRO TREND ADJUSTMENT (Jan 15, 2026)
        # If micro-trend opposes HTF bias, penalize score
        # If micro-trend aligns with HTF bias, add bonus
        micro_adjustment = 0.0
        if hasattr(self, '_micro_trend_state') and self._micro_trend_state is not None:
            micro_adjustment = self._micro_trend_state.score_adjustment
            if micro_adjustment != 0:
                breakdown['micro_trend_adj'] = micro_adjustment
                total = max(0, total + micro_adjustment)
                if micro_adjustment < 0:
                    logger.warning(f"📉 Micro trend penalty: {micro_adjustment:+.1f} (micro={self._micro_trend_state.micro_trend}, strength={self._micro_trend_state.strength:.0%})")
                else:
                    logger.info(f"📈 Micro trend bonus: {micro_adjustment:+.1f} (micro={self._micro_trend_state.micro_trend})")
        
        # 6.5️⃣ FILTER PIPELINE ADJUSTMENT (Jan 15, 2026)
        # Apply score adjustments from unified filter pipeline
        # (BB Width, Volatility Shock, Macro Trend, Post-Sweep, Cost, Overtrade)
        # 
        # PHASE A FIX (Jan 18, 2026): Implement PENALTY HIERARCHY
        # Only apply 1 penalty from CONTEXT + 1 penalty from EXECUTION
        # This prevents "death by 1000 cuts" where multiple soft penalties kill score
        
        fp_adjustment = 0.0
        if hasattr(self, 'filter_pipeline'):
            last_decision = self.filter_pipeline.get_last_decision()
            
            if last_decision and last_decision.score_adjust != 0:
                # PHASE A: Apply penalty hierarchy
                fp_adjustment = self._apply_penalty_hierarchy(
                    last_decision, 
                    micro_adjustment,
                    breakdown
                )
                breakdown['filter_pipeline_adj'] = fp_adjustment
                total = max(0, total + fp_adjustment)
                if fp_adjustment < 0:
                    logger.warning(f"📉 Filter pipeline penalty (hierarchical): {fp_adjustment:+.1f}")
                else:
                    logger.info(f"📈 Filter pipeline bonus: {fp_adjustment:+.1f}")
        
        # 7️⃣ RR PENALTY - Applied BEFORE final score (Jan 9, 2026)
        # Low RR trades should be penalized in the scoring system
        # This is calculated later in _execute_entry but we store a flag
        # The actual penalty is applied when RR is known
        # NOTE: RR penalty is applied in _apply_rr_score_adjustment() after SL/TP calculation
        
        self._current_trade_tags['score'] = total
        
        return total, breakdown, False
    
    def _apply_penalty_hierarchy(
        self, 
        pipeline_decision: 'FilterDecision', 
        micro_adjustment: float,
        breakdown: dict
    ) -> float:
        """
        PHASE A: Apply penalty hierarchy to prevent excessive penalty stacking.
        
        Penalty Categories:
        - CONTEXT: {regime, volatility, micro_trend, correlation, macro_trend}
        - EXECUTION: {cost, spread, timing, overtrade}
        
        Rule: Only apply WORST penalty from CONTEXT + WORST from EXECUTION
        The rest become INFO ONLY (logged but not applied to score)
        
        Returns: Total hierarchical adjustment
        """
        # Get individual filter decisions from pipeline
        filter_decisions = self.filter_pipeline.get_filter_decisions() if hasattr(self, 'filter_pipeline') else {}
        
        # Categorize penalties
        context_penalties = {}  # regime_source -> penalty
        execution_penalties = {}
        
        # CONTEXT filters: affect market conditions
        CONTEXT_FILTERS = {'volatility_shock', 'bb_width', 'macro_trend', 'btc_correlation', 'post_sweep'}
        # EXECUTION filters: affect trade mechanics
        EXECUTION_FILTERS = {'cost', 'overtrade'}
        
        for source, decision in filter_decisions.items():
            if decision.score_adjust < 0:  # Only track penalties
                if source in CONTEXT_FILTERS:
                    context_penalties[source] = decision.score_adjust
                elif source in EXECUTION_FILTERS:
                    execution_penalties[source] = decision.score_adjust
        
        # Add micro_trend to context penalties if present
        if micro_adjustment < 0:
            context_penalties['micro_trend'] = micro_adjustment
        
        # Pick WORST penalty from each category
        worst_context = 0.0
        worst_context_source = None
        if context_penalties:
            worst_context_source = min(context_penalties, key=context_penalties.get)
            worst_context = context_penalties[worst_context_source]
        
        worst_execution = 0.0
        worst_execution_source = None
        if execution_penalties:
            worst_execution_source = min(execution_penalties, key=execution_penalties.get)
            worst_execution = execution_penalties[worst_execution_source]
        
        # Calculate total hierarchical penalty
        total_hierarchical = worst_context + worst_execution
        
        # Track which penalties were applied vs ignored
        breakdown['penalty_hierarchy'] = {
            'context_penalties': context_penalties,
            'execution_penalties': execution_penalties,
            'worst_context': (worst_context_source, worst_context),
            'worst_execution': (worst_execution_source, worst_execution),
            'total_before_hierarchy': pipeline_decision.score_adjust + micro_adjustment,
            'total_after_hierarchy': total_hierarchical
        }
        
        # Log if hierarchy saved us from excessive penalties
        original_total = sum(context_penalties.values()) + sum(execution_penalties.values())
        if abs(original_total) > abs(total_hierarchical) + 0.1:
            logger.info(
                f"🛡️ PENALTY HIERARCHY: Reduced {original_total:+.1f} → {total_hierarchical:+.1f} "
                f"| CONTEXT: {worst_context_source}={worst_context:+.1f} "
                f"| EXECUTION: {worst_execution_source}={worst_execution:+.1f}"
            )
            self._current_trade_tags['penalty_hierarchy_saved'] = abs(original_total - total_hierarchical)
        
        # Store for tracking
        self._current_trade_tags['worst_context_penalty'] = worst_context_source
        self._current_trade_tags['worst_execution_penalty'] = worst_execution_source
        
        # Return only the hierarchical adjustment (micro_adjustment already applied above)
        # Since micro_adjustment is already in context_penalties, we return total minus micro
        return total_hierarchical - micro_adjustment if micro_adjustment < 0 else total_hierarchical

    def _apply_rr_score_adjustment(self, score: int, rr: float) -> tuple[float, str]:
        """
        Apply RR-based score adjustment with PROPORTIONAL penalty.
        
        RR < 1.0 → Proportional penalty based on distance from ideal
        RR >= 1.5 → Add bonus point (rewards high RR)
        
        RANGE regime gets lighter penalty (high winrate compensates)
        TREND regime gets full penalty (need RR for math)
        
        Returns: (adjusted_score, adjustment_reason)
        """
        adjustment = 0.0
        reason = ""
        
        # Get regime-specific multiplier for penalty
        regime_multiplier = 1.0  # Default (TREND)
        if hasattr(self, '_current_regime'):
            from src.core.types import MarketRegime
            if self._current_regime in (MarketRegime.RANGE,):
                regime_multiplier = getattr(OperatingMode, 'RR_PENALTY_RANGE_MULTIPLIER', 0.5)
        
        # RR below threshold → PROPORTIONAL penalty (not fixed)
        if rr < OperatingMode.RR_PENALTY_THRESHOLD:
            # Scale penalty: RR=0.5 → full penalty, RR=1.0 → 0 penalty
            # penalty = max_penalty * (1 - rr) / 0.5 (clamped)
            rr_gap = OperatingMode.RR_PENALTY_THRESHOLD - rr
            max_penalty = getattr(OperatingMode, 'RR_PENALTY_MAX_POINTS', 2)
            
            # Proportional: RR=0.5 gets full penalty, RR=0.9 gets 20% penalty
            penalty_ratio = min(1.0, rr_gap / 0.5)  # Normalize to 0-1
            base_penalty = max_penalty * penalty_ratio
            
            # Apply regime multiplier (RANGE = 50% penalty)
            adjustment = -base_penalty * regime_multiplier
            
            regime_note = f" (RANGE: {regime_multiplier:.0%} penalty)" if regime_multiplier < 1.0 else ""
            reason = f"RR penalty: {rr:.2f}R < {OperatingMode.RR_PENALTY_THRESHOLD}R → {adjustment:.1f}{regime_note}"
            logger.warning(f"📉 {reason}")
        # RR excellent → bonus
        elif rr >= OperatingMode.RR_EXCELLENT_THRESHOLD:
            adjustment = OperatingMode.RR_EXCELLENT_BONUS
            reason = f"RR bonus: {rr:.2f}R >= {OperatingMode.RR_EXCELLENT_THRESHOLD}R → +{OperatingMode.RR_EXCELLENT_BONUS}"
            logger.info(f"📈 {reason}")
        
        adjusted_score = max(0, score + adjustment)
        return adjusted_score, reason
    
    def _score_structure(self, df_1h: pd.DataFrame, df_5m: pd.DataFrame, 
                         bias: str, current_price: float) -> tuple[int, bool, bool]:
        """
        STRUCTURE SCORE (0-2)
        +2 → HTF aligned + 5m structure clean
        +1 → HTF ok but LTF structure partial
        0 → Against structure → BLOCKS ENTRY
        
        Returns: (score, blocks_entry, htf_is_clean)
        """
        if len(df_1h) < 50 or len(df_5m) < 20:
            return 1, False, False  # Not enough data, allow with partial score
        
        # HTF alignment check
        closes_1h = df_1h['close'].values
        ema50_1h = calculate_ema(closes_1h, 50)
        
        if np.isnan(ema50_1h[-1]):
            return 1, False, False
        
        htf_aligned = False
        if bias == 'LONG':
            htf_aligned = current_price > ema50_1h[-1]
        else:
            htf_aligned = current_price < ema50_1h[-1]
        
        if not htf_aligned:
            # Against HTF → Structure = 0 → BLOCKS
            logger.warning(f"⛔ STRUCTURE GATE: {bias} against HTF (Price vs EMA50_1h)")
            return 0, True, False
        
        # 5m structure check (price vs EMA20)
        closes_5m = df_5m['close'].values
        ema20_5m = calculate_ema(closes_5m, 20)
        
        if np.isnan(ema20_5m[-1]):
            return 1, False, htf_aligned
        
        ltf_clean = False
        if bias == 'LONG':
            # Clean structure: price above EMA20 or just reclaiming
            ltf_clean = current_price >= ema20_5m[-1] * 0.998  # Within 0.2%
        else:
            ltf_clean = current_price <= ema20_5m[-1] * 1.002
        
        if htf_aligned and ltf_clean:
            return 2, False, True  # Perfect alignment, HTF is clean
        elif htf_aligned:
            return 1, False, False  # HTF ok, LTF partial
        
        return 0, True, False  # Should not reach here
    
    def _score_timing(self, df_1m: pd.DataFrame, bias: str) -> tuple[int, bool]:
        """
        TIMING SCORE (0-2)
        +2 → BOS confirmed at CLOSE + clean reclaim
        +1 → BOS + late timing
        0 → Signal intra-candle / ambiguous
        
        Returns: (score, bos_confirmed)
        """
        if len(df_1m) < 10:
            return 1, False  # Not enough data
        
        # Check if last CLOSED candle confirmed BOS
        # (not current forming candle)
        last_closed = df_1m.iloc[-2] if len(df_1m) >= 2 else df_1m.iloc[-1]
        prev_candles = df_1m.iloc[-7:-2] if len(df_1m) >= 7 else df_1m.iloc[:-1]
        
        if len(prev_candles) < 3:
            return 1, False
        
        recent_high = prev_candles['high'].max()
        recent_low = prev_candles['low'].min()
        
        bos_confirmed = False
        clean_close = False
        
        if bias == 'LONG':
            # BOS: Close above recent high (not just wick)
            bos_confirmed = last_closed['close'] > recent_high
            # Clean: Close near high of candle (strong)
            candle_range = last_closed['high'] - last_closed['low']
            if candle_range > 0:
                close_position = (last_closed['close'] - last_closed['low']) / candle_range
                clean_close = close_position > 0.6  # Closed in upper 40%
        else:
            # BOS: Close below recent low
            bos_confirmed = last_closed['close'] < recent_low
            candle_range = last_closed['high'] - last_closed['low']
            if candle_range > 0:
                close_position = (last_closed['high'] - last_closed['close']) / candle_range
                clean_close = close_position > 0.6  # Closed in lower 40%
        
        if bos_confirmed and clean_close:
            return 2, True  # Perfect timing, BOS confirmed
        elif bos_confirmed:
            return 1, True  # BOS confirmed but timing not perfect
        
        # Check if current candle is forming BOS (intra-candle)
        current = df_1m.iloc[-1]
        intra_bos = False
        if bias == 'LONG':
            intra_bos = current['high'] > recent_high
        else:
            intra_bos = current['low'] < recent_low
        
        if intra_bos:
            return 1, False  # Potential BOS forming, not confirmed
        
        return 0, False  # No clear timing signal
    
    def _score_momentum(self, df_5m: pd.DataFrame, bias: str) -> int:
        """
        MOMENTUM SCORE (0-2)
        +2 → ADX HTF strong + slope favorable + volume OK
        +1 → Neutral momentum
        0 → Weak momentum (but does NOT block!)
        
        CAPA 4: Degradation penalties (weights, not gates):
        - Volume low → -0.5 (tracked as penalty, applied via rounding)
        - ADX slope declining fast → -0.25
        
        These penalties are reflected in the final score through rounding,
        but NEVER block in DISCOVERY mode.
        """
        if len(df_5m) < 30:
            return 1
        
        closes = df_5m['close'].values
        highs = df_5m['high'].values
        lows = df_5m['low'].values
        
        adx, _ = calculate_adx(highs, lows, closes, 14)
        
        if np.isnan(adx[-1]):
            return 1
        
        adx_val = adx[-1]
        
        # ADX slope (momentum direction)
        adx_slope = 0.0
        if len(adx) >= 5 and not np.isnan(adx[-5]):
            adx_slope = adx[-1] - adx[-5]
        
        # Store in trade tags for analysis
        # NOTE: This is ADX from 5m timeframe (LTF momentum)
        self._current_trade_tags['adx_5m'] = float(adx_val)
        self._current_trade_tags['adx'] = float(adx_val)  # Keep for backwards compat
        self._current_trade_tags['adx_slope'] = float(adx_slope)
        
        # Store ADX array for regime detection (last 10 values)
        adx_values = adx[-10:] if len(adx) >= 10 else adx
        # Filter out NaN values
        adx_values = adx_values[~np.isnan(adx_values)]
        self._score_breakdown['adx_values'] = adx_values
        
        # ADX slope bucket for analysis
        if adx_slope < -0.5:
            self._current_trade_tags['adx_slope_bucket'] = 'declining_fast'
        elif adx_slope < -0.15:
            self._current_trade_tags['adx_slope_bucket'] = 'declining'
        elif adx_slope < 0.15:
            self._current_trade_tags['adx_slope_bucket'] = 'neutral'
        else:
            self._current_trade_tags['adx_slope_bucket'] = 'rising'
        
        # Also calculate and store RSI for tags (even though it doesn't affect score)
        rsi = calculate_rsi(closes, 14)
        if not np.isnan(rsi[-1]):
            self._current_trade_tags['rsi'] = float(rsi[-1])
        
        # Base score based on ADX strength and slope
        base_score = 0.0
        if adx_val > 25 and adx_slope > 0:
            base_score = 2.0  # Strong trend, strengthening
        elif adx_val > 20 or (adx_val > 15 and adx_slope > 0):
            base_score = 1.0  # Moderate trend
        else:
            base_score = 0.0  # Weak (but doesn't block!)
        
        # ===== CAPA 4: Degradation penalties (weights, not gates) =====
        penalty = 0.0
        
        # Volume penalty (from LTF info stored in tags)
        rel_vol = self._current_trade_tags.get('rel_vol', 1.0)
        if rel_vol < 0.6:  # Low volume
            penalty += 0.5
            self._current_trade_tags['volume_penalty'] = 0.5
            logger.debug(f"📊 Volume penalty: -0.5 (rel_vol={rel_vol:.2f})")
        
        # ADX slope penalty (fast decline = momentum dying)
        if adx_slope < -0.5:
            penalty += 0.25
            self._current_trade_tags['slope_penalty'] = 0.25
            logger.debug(f"📊 Slope penalty: -0.25 (slope={adx_slope:.2f})")
        
        # Apply penalty and round to integer
        final_score = max(0, base_score - penalty)
        return int(round(final_score))
    
    def _score_context(self, df_5m: pd.DataFrame, market_time: datetime) -> int:
        """
        CONTEXT SCORE (0-2)
        +1 → Session London/NY
        +1 → Volatility healthy (ATR fast ≈ slow)
        
        Enhanced with session quality tracking (Jan 9, 2026)
        """
        score = 0
        
        # Session check using config helper
        hour = market_time.hour
        minute = market_time.minute
        day_of_week = market_time.weekday()
        session_name = get_session_name(hour, minute, day_of_week)
        
        # Best sessions get point
        if session_name in ['LONDON', 'NY', 'LONDON_NY_OVERLAP']:
            score += 1
            self._current_trade_tags['session'] = 'active'
        elif session_name == 'ASIA':
            self._current_trade_tags['session'] = 'asia'
        else:
            self._current_trade_tags['session'] = 'quiet'
        
        # Store detailed session info
        self._current_trade_tags['session_quality'] = session_name
        
        # Check for session quality based on volatility and ADX
        session_quality_reason = ""
        
        # Volatility check
        if len(df_5m) >= 50:
            highs = df_5m['high'].values
            lows = df_5m['low'].values
            closes = df_5m['close'].values
            
            atr_fast = calculate_atr(highs, lows, closes, 14)
            atr_slow = calculate_atr(highs, lows, closes, 42)
            
            if not np.isnan(atr_fast[-1]) and not np.isnan(atr_slow[-1]):
                # Healthy: ATR fast within 70-130% of slow
                ratio = atr_fast[-1] / atr_slow[-1] if atr_slow[-1] > 0 else 1.0
                self._current_trade_tags['volatility_ratio'] = float(ratio)
                self._current_trade_tags['atr'] = float(atr_fast[-1])
                if 0.7 <= ratio <= 1.5:
                    score += 1
        
        return score
    
    def _get_score_size_multiplier(self, score: int) -> float:
        """
        Score-based position sizing multiplier.
        score=5 → 0.8x
        score=6 → 1.0x
        score>=7 → 1.2x
        """
        if score >= 7:
            return 1.2
        elif score >= 6:
            return 1.0
        else:  # score = 5
            return 0.8
    
    def _update_score_metrics(self, score: int, pnl: float, is_win: bool):
        """
        Update DISCOVERY metrics for edge segmentation.
        Track expectancy by score bucket to find real edge.
        """
        # Determine bucket
        if score >= 9:
            bucket = '9+'
        elif score >= 7:
            bucket = '7-8'
        else:
            bucket = '5-6'
        
        # Update metrics
        self._score_distribution[bucket]['trades'] += 1
        self._score_distribution[bucket]['pnl'] += pnl
        if is_win:
            self._score_distribution[bucket]['wins'] += 1
        
        # Log distribution for analysis
        logger.info(f"📊 SCORE METRICS | Bucket {bucket}: trades={self._score_distribution[bucket]['trades']}, " +
                    f"wins={self._score_distribution[bucket]['wins']}, " +
                    f"pnl={self._score_distribution[bucket]['pnl']:.2f}")
        
        # Calculate and log expectancy per bucket
        for b, data in self._score_distribution.items():
            if data['trades'] > 0:
                exp = data['pnl'] / data['trades']
                wr = (data['wins'] / data['trades']) * 100
                logger.debug(f"   {b}: expectancy={exp:.2f}, winrate={wr:.1f}%")
    
    def _check_session_filter(self, market_time: datetime) -> tuple[bool, str]:
        """
        SESSION FILTER: Disabled for now to see real edge with more trades
        BTC trades 24/7 - let's see what happens without session restriction
        """
        hour = market_time.hour
        # Allow all hours for now to get more trades
        return True, f"Session OK: 24/7 trading ({hour}:00 UTC)"
    
    def _check_trend_filter(self, df: pd.DataFrame, bias: str, current_price: float) -> tuple[bool, str]:
        """
        TREND FILTER (OBLIGATORIO): Solo operar en dirección de tendencia clara
        
        LONG: Price > EMA200 AND EMA50 > EMA200
        SHORT: Price < EMA200 AND EMA50 < EMA200
        """
        if len(df) < 200:
            return True, "Not enough data for trend filter"
        
        closes = df['close'].values
        ema50 = calculate_ema(closes, 50)
        ema200 = calculate_ema(closes, 200)
        
        if np.isnan(ema50[-1]) or np.isnan(ema200[-1]):
            return True, "EMA values not ready"
        
        allow_long = current_price > ema200[-1] and ema50[-1] > ema200[-1]
        allow_short = current_price < ema200[-1] and ema50[-1] < ema200[-1]
        
        if bias == 'LONG':
            if allow_long:
                return True, f"Trend OK: LONG aligned (P>{ema200[-1]:.0f}, EMA50>{ema200[-1]:.0f})"
            return False, f"LONG against trend: P={current_price:.0f}, EMA50={ema50[-1]:.0f}, EMA200={ema200[-1]:.0f}"
        else:
            if allow_short:
                return True, f"Trend OK: SHORT aligned (P<{ema200[-1]:.0f}, EMA50<{ema200[-1]:.0f})"
            return False, f"SHORT against trend: P={current_price:.0f}, EMA50={ema50[-1]:.0f}, EMA200={ema200[-1]:.0f}"
    
    def _check_momentum_filter(self, df: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """
        MOMENTUM FILTER: RSI slope + ADX for trend confirmation
        
        FIXED LOGIC (trend-following):
        - LONG: RSI rising (slope > 0) OR RSI < 70 (not overbought)
        - SHORT: RSI falling (slope < 0) OR RSI > 30 (not oversold)
        
        RSI LEVEL ALONE IS NOT A SIGNAL!
        - High RSI = good for SHORT (overbought, expect reversal/continuation down)
        - Low RSI = good for LONG (oversold, expect reversal/continuation up)
        
        ADX > 20 confirms trending market (optional, not a hard veto)
        
        This filter now ALWAYS passes but returns momentum quality for sizing.
        """
        if len(df) < 30:
            return True, "Not enough data for momentum"
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        volumes = df['volume'].values if 'volume' in df.columns else None
        
        # Skip momentum check if recent candles have zero volume (ghost data)
        if volumes is not None and len(volumes) >= 3:
            recent_vol = volumes[-3:]
            if np.sum(recent_vol) == 0:
                return True, "Momentum check skipped: zero volume candles (ghost data)"
        
        rsi = calculate_rsi(closes, 14)
        adx, _ = calculate_adx(highs, lows, closes, 14)
        
        if np.isnan(rsi[-1]) or np.isnan(adx[-1]):
            return True, "RSI/ADX not ready"
        
        rsi_val = rsi[-1]
        adx_val = adx[-1]
        
        # Calculate RSI slope (momentum direction)
        rsi_slope = 0.0
        if len(rsi) >= 5 and not np.isnan(rsi[-5]):
            rsi_slope = rsi[-1] - rsi[-5]  # 5-period change
        
        # Calculate ATR for volatility adjustment
        atr = calculate_atr(highs, lows, closes, 14)
        atr_val = atr[-1] if not np.isnan(atr[-1]) else 100
        
        # Normalize ATR (rough BTC typical range 50-500)
        atr_normalized = min(max((atr_val - 50) / 450, 0), 1)  # 0 to 1
        
        # Build momentum assessment (NOT a hard veto anymore)
        momentum_quality = "STRONG"
        issues = []
        
        # ADX check (soft, not hard veto)
        if adx_val < 15:
            momentum_quality = "WEAK"
            issues.append(f"low ADX={adx_val:.1f}")
        elif adx_val < 20:
            momentum_quality = "MODERATE"
        
        if bias == 'LONG':
            # LONG: Want RSI rising OR not overbought (< 75)
            # High RSI with rising slope = strong uptrend, still valid
            if rsi_slope < -5:  # RSI falling significantly
                if rsi_val > 60:  # Falling from high = bearish
                    momentum_quality = "WEAK"
                    issues.append(f"RSI falling from high ({rsi_val:.1f}, slope={rsi_slope:.1f})")
            # Extremely overbought + no momentum = caution
            if rsi_val > 80 and rsi_slope <= 0:
                momentum_quality = "WEAK"
                issues.append(f"RSI extreme overbought={rsi_val:.1f}")
        else:  # SHORT
            # SHORT: Want RSI falling OR overbought (> 55 is GOOD for shorts!)
            # HIGH RSI = OVERBOUGHT = GOOD SHORT OPPORTUNITY
            if rsi_val > 65:  # Overbought = GOOD for short
                if rsi_slope < 0:  # And falling = even better
                    momentum_quality = "STRONG"
                elif rsi_slope > 5:  # Rising strongly = wait
                    momentum_quality = "MODERATE"
                    issues.append(f"RSI still rising ({rsi_val:.1f}, slope={rsi_slope:.1f})")
                # Else: high RSI, neutral slope = still good short
            elif rsi_val > 55:  # Mildly overbought
                if rsi_slope < 0:  # Falling = good
                    momentum_quality = "STRONG"
                else:  # Rising or flat = moderate
                    momentum_quality = "MODERATE"
            elif rsi_val < 35 and rsi_slope < -3:  # Already oversold and still falling
                momentum_quality = "WEAK"
                issues.append(f"RSI already oversold={rsi_val:.1f}")
        
        # NEVER BLOCK - momentum is for SIZING, not VETO
        # Store momentum quality for position sizing
        self._last_momentum_quality = momentum_quality
        self._last_momentum_atr = atr_val
        
        if issues:
            return True, f"Momentum {momentum_quality}: RSI={rsi_val:.1f} slope={rsi_slope:.1f} ADX={adx_val:.1f} ({', '.join(issues)})"
        return True, f"Momentum {momentum_quality}: RSI={rsi_val:.1f} slope={rsi_slope:.1f} ADX={adx_val:.1f}"
    
    def _check_volatility_expansion(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        VOLATILITY FILTER: ATR fast vs ATR slow
        
        Solo operar cuando BTC se mueve, no en compresión
        
        ATR(14) >= ATR(42) * 0.9
        """
        if len(df) < 50:
            return True, "Not enough data for volatility"
        
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        
        atr_fast = calculate_atr(highs, lows, closes, 14)
        atr_slow = calculate_atr(highs, lows, closes, 42)
        
        if np.isnan(atr_fast[-1]) or np.isnan(atr_slow[-1]):
            return True, "ATR not ready"
        
        threshold = atr_slow[-1] * 0.9
        
        if atr_fast[-1] >= threshold:
            return True, f"Volatility OK: ATR14={atr_fast[-1]:.2f} >= {threshold:.2f} (90% of ATR42)"
        
        return False, f"Low volatility: ATR14={atr_fast[-1]:.2f} < {threshold:.2f}"
    
    def _check_not_compressed(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        COMPRESSION FILTER: Disabled for now to get more trades
        """
        return True, "Compression filter disabled"
    
    def _check_anti_reverso(self, df: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """
        ANTI-REVERSO FILTER: Evita cuchillos (engulfing + rejection wicks)
        
        Bloquea entrada si:
        - Engulfing bearish detectado para LONG
        - Engulfing bullish detectado para SHORT
        - Rejection wick > 60% del rango
        """
        if len(df) < 3:
            return True, "Not enough data for reversal detection"
        
        last3 = df.iloc[-3:]
        
        # Candle properties
        open1, close1 = last3.iloc[-2]['open'], last3.iloc[-2]['close']
        open2, close2 = last3.iloc[-1]['open'], last3.iloc[-1]['close']
        high2, low2 = last3.iloc[-1]['high'], last3.iloc[-1]['low']
        
        candle_range = high2 - low2
        if candle_range <= 0:
            return True, "No range"
        
        # Engulfing patterns
        bearish_engulf = (close2 < open2 and  # Current bearish
                         close2 < open1 and   # Close below prev open
                         open2 > close1)      # Open above prev close
        
        bullish_engulf = (close2 > open2 and  # Current bullish
                         close2 > open1 and   # Close above prev open
                         open2 < close1)      # Open below prev close
        
        # Rejection wicks (upper wick for LONG rejection, lower for SHORT)
        upper_wick = high2 - max(open2, close2)
        lower_wick = min(open2, close2) - low2
        
        long_reject = upper_wick > candle_range * 0.6
        short_reject = lower_wick > candle_range * 0.6
        
        if bias == 'LONG':
            if bearish_engulf:
                return False, "Bearish engulfing detected - LONG blocked"
            if long_reject:
                return False, f"Long rejection wick ({upper_wick/candle_range*100:.0f}% of range)"
            return True, "No reversal patterns for LONG"
        else:
            if bullish_engulf:
                return False, "Bullish engulfing detected - SHORT blocked"
            if short_reject:
                return False, f"Short rejection wick ({lower_wick/candle_range*100:.0f}% of range)"
            return True, "No reversal patterns for SHORT"
    
    def _check_not_duplicate_zone(self, current_price: float) -> tuple[bool, str]:
        """
        DUPLICATE ZONE FILTER: Disabled for now to get more trades
        """
        return True, "Duplicate zone filter disabled"
    
    def _check_volume_confirmation(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        DELTA VOLUME FILTER: Entry needs INTENTION (directional volume)
        
        No RSI, no MACD, no pollas.
        BTC moves with intention or doesn't move.
        
        Delta = Buy Volume - Sell Volume (approximated from candle structure)
        If |delta| < threshold → NO TRADE (no clear direction)
        """
        if len(df) < 25 or 'volume' not in df.columns:
            return True, "Not enough data for volume filter"
        
        # Calculate delta volume (approximation using candle body vs range)
        # Bullish candle: close > open → delta positive
        # Bearish candle: close < open → delta negative
        # Delta strength = how much of the range is body (conviction)
        
        volumes = df['volume'].values
        opens = df['open'].values
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # Current candle delta
        current_vol = volumes[-1]
        current_range = highs[-1] - lows[-1]
        current_body = closes[-1] - opens[-1]  # Positive = bullish, negative = bearish
        
        if current_range <= 0:
            return True, "Zero range candle"
        
        # Delta = volume * (body / range) * sign
        # This gives us directional volume
        body_ratio = abs(current_body) / current_range
        delta = current_vol * body_ratio * (1 if current_body > 0 else -1)
        
        # Average volume for normalization
        avg_vol_20 = np.mean(volumes[-21:-1])
        if avg_vol_20 <= 0:
            return True, "Zero average volume"
        
        # Normalized delta (as ratio of average volume)
        norm_delta = abs(delta) / avg_vol_20
        
        # Threshold: need at least 0.3x average volume as clear delta
        # This filters out indecision candles (dojis, spinning tops)
        DELTA_THRESHOLD = 0.3
        
        if norm_delta >= DELTA_THRESHOLD:
            direction = "BUY" if delta > 0 else "SELL"
            return True, f"Delta OK: {norm_delta:.2f}x avg ({direction})"
        
        return False, f"No intention: delta {norm_delta:.2f}x < {DELTA_THRESHOLD}x (indecision)"
    
    def _record_trade_price(self, entry_price: float):
        """Record trade price for duplicate zone detection"""
        if not hasattr(self, '_recent_trade_prices'):
            self._recent_trade_prices = []
        self._recent_trade_prices.append(entry_price)
        # Keep only last 5
        self._recent_trade_prices = self._recent_trade_prices[-5:]
    
    def _find_tp2_target(self) -> float:
        """
        Find TP2 target based on POSITION DIRECTION (not current_bias!)
        
        For LONG: TP2 = highest high of recent 5m candles (upside target)
        For SHORT: TP2 = lowest low of recent 5m candles (downside target)
        """
        candles_5m = self.market_data.get_candles('5m')
        if not candles_5m:
            return 0
        
        df_5m = pd.DataFrame(candles_5m)
        
        # CRITICAL: Use position direction, not current_bias
        # current_bias can be different after HTF flip
        position = self.risk_engine.position
        if position:
            direction = position.direction
        else:
            # Fallback to current_bias if no position (should not happen)
            direction = self.current_bias
        
        if direction == 'LONG':
            return df_5m['high'].max()
        else:  # SHORT
            return df_5m['low'].min()
    
    def _check_bos_against_position(self, direction: str, candles_5m: list) -> bool:
        """
        Detecta Break of Structure (BOS) en contra de la posición
        
        BOS LONG (en contra de posición LONG):
        - Precio hace nuevo Lower Low (rompe último soporte)
        
        BOS SHORT (en contra de posición SHORT):
        - Precio hace nuevo Higher High (rompe última resistencia)
        
        Returns: True si hay BOS en contra
        """
        if not candles_5m or len(candles_5m) < 10:
            return False
        
        df = pd.DataFrame(candles_5m)
        
        # Solo mirar últimas 10 velas para BOS reciente
        recent = df.tail(10)
        
        if direction == 'LONG':
            # Para LONG: BOS = Lower Low (rompe mínimo anterior)
            # Buscar si el low más reciente es menor que todos los anteriores
            lows = recent['low'].values
            if len(lows) < 3:
                return False
            
            # Si el último low es menor que el mínimo de los 5 anteriores = BOS
            recent_low = lows[-1]
            previous_lows = lows[-6:-1]  # 5 velas anteriores
            if len(previous_lows) > 0:
                min_previous = min(previous_lows)
                if recent_low < min_previous:
                    # Edge-triggered: solo loguear si es un NUEVO nivel de BOS
                    if self._last_bos_level != recent_low or self._last_bos_direction != 'LONG':
                        logger.info(f"📉 BOS detectado (LONG): LL @ {recent_low:.2f} < {min_previous:.2f}")
                        self._last_bos_level = recent_low
                        self._last_bos_direction = 'LONG'
                    return True
        
        else:  # SHORT
            # Para SHORT: BOS = Higher High (rompe máximo anterior)
            highs = recent['high'].values
            if len(highs) < 3:
                return False
            
            recent_high = highs[-1]
            previous_highs = highs[-6:-1]
            if len(previous_highs) > 0:
                max_previous = max(previous_highs)
                if recent_high > max_previous:
                    # Edge-triggered: solo loguear si es un NUEVO nivel de BOS
                    if self._last_bos_level != recent_high or self._last_bos_direction != 'SHORT':
                        logger.info(f"📈 BOS detectado (SHORT): HH @ {recent_high:.2f} > {max_previous:.2f}")
                        self._last_bos_level = recent_high
                        self._last_bos_direction = 'SHORT'
                    return True
        
        return False
    
    async def emergency_close_all(self):
        """Emergency close all positions (market order)"""
        logger.critical("EMERGENCY POSITION CLOSE")
        
        # 🔐 FIX #2: Hard-lock during emergency exit
        self.reconciler.notify_exit_started("EMERGENCY_CLOSE")
        
        try:
            # 🔥 FIX #8: Cancel all pending orders FIRST to avoid SL/TP triggering during close
            try:
                await self.execution.cancel_all_orders(self.symbol)
                logger.info("✅ All pending orders cancelled before emergency close")
            except Exception as cancel_err:
                logger.warning(f"⚠️ Could not cancel orders: {cancel_err}")
            
            position = self.risk_engine.position
            if position:
                # Get current price for PnL calculation
                current_price = 0.0
                candles_1m = self.market_data.get_candles('1m')
                if candles_1m and len(candles_1m) > 0:
                    current_price = candles_1m[-1]['close']
                
                # 🔒 FIX (Jan 14, 2026): Get real size from exchange for emergency close
                close_size = position.size
                try:
                    exchange_pos = await self.execution.get_position(self.symbol)
                    if exchange_pos:
                        close_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                except Exception as e:
                    logger.debug(f"Could not get exchange position for emergency close (using local size): {e}")
                
                if close_size < KRAKEN_BTC_MIN_SIZE:
                    logger.warning(f"⚠️ Emergency close skipped: size {close_size:.6f} < min - position effectively closed")
                    self.risk_engine.position = None
                    self._reset_trade_state("Position too small to close")
                    return
                
                # Calculate PnL
                if position.direction == 'LONG':
                    pnl = (current_price - position.entry_price) * close_size
                else:
                    pnl = (position.entry_price - current_price) * close_size
                
                # Market close via REST
                close_result = await self.execution.close_position_market(
                    symbol=self.symbol,
                    side='sell' if position.direction == 'LONG' else 'buy',
                    amount=close_size,
                    reason="EMERGENCY"
                )
                
                # ═══════════════════════════════════════════════════════════════
                # 🔒 CRITICAL SAFETY CHECK: Verify close was successful
                # If exchange still has position, HALT - do NOT continue
                # ═══════════════════════════════════════════════════════════════
                if close_result is None:
                    logger.critical("🚨 CRITICAL: Emergency close FAILED - checking exchange position...")
                    await asyncio.sleep(1)  # Brief wait for exchange to update
                    
                    exchange_has_position = await self.execution.has_open_position(self.symbol)
                    if exchange_has_position:
                        logger.critical("🔴🔴🔴 HALT: LOCAL CLOSED BUT EXCHANGE STILL HAS POSITION 🔴🔴🔴")
                        logger.critical("🔴 SYSTEM HALTED - MANUAL INTERVENTION REQUIRED 🔴")
                        logger.critical(f"🔴 Position: {position.direction} {position.size} @ {position.entry_price}")
                        
                        # Set halted flag to prevent any further trading
                        self._system_halted = True
                        self.is_running = False
                        
                        # DO NOT reset trade state - keep position info for manual recovery
                        return
                
                # Record close in state machine
                self.state_machine.record_trade_close(pnl, pnl > 0, exit_reason='EMERGENCY_CLOSE')
                
                # Log to trade logger for backtest results
                await self.trade_logger.log_event('trade_closed', {
                    'entry': position.entry_price,
                    'exit': current_price,
                    'size': position.size,
                    'pnl': pnl,
                    'direction': position.direction,
                    'reason': 'EMERGENCY_CLOSE',
                    'r': position.calculate_r_realized(current_price),
                    'mfe_r': position.calculate_mfe_r(),
                    'mae_r': position.calculate_mae_r(),
                    'regime': position.regime
                })
                
                self.risk_engine.position = None
                
                logger.critical(f"Emergency close completed - PnL: {pnl:.2f}")
            
            # 🔒 LIMPIEZA COMPLETA independientemente de si había posición
            self._reset_trade_state("Emergency close")
        
        except Exception as e:
            logger.error(f"Error in emergency close: {e}", exc_info=True)
            # Check if we're in a dangerous state
            try:
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                if exchange_has_position:
                    logger.critical("🔴🔴🔴 HALT: Exception during close but exchange has position 🔴🔴🔴")
                    self._system_halted = True
                    self.is_running = False
                    return
            except Exception as verify_err:
                logger.error(f"Error verifying position after emergency close exception: {verify_err}")
            # Limpia de todas formas
            self._reset_trade_state("Emergency close error")

    async def _emergency_daily_halt_close(self):
        """
        Emergency close triggered by daily drawdown limit.
        
        FIX #1 (Jan 13, 2026): When daily loss limit is hit:
        1. Cancel ALL pending orders
        2. Close ANY open position at market
        3. Set system to halt state
        4. Log clearly for audit
        """
        logger.critical("🛑🛑🛑 DAILY HALT CLOSE - Closing all positions and cancelling orders 🛑🛑🛑")
        
        try:
            # Step 1: Cancel ALL pending orders
            try:
                await self.execution.cancel_all_open_orders(self.symbol)
                logger.info("✅ All pending orders cancelled for daily halt")
            except Exception as e:
                logger.error(f"Error cancelling orders during daily halt: {e}")
            
            # Step 2: Close any open position
            try:
                exchange_pos = await self.execution.get_position(self.symbol)
                if exchange_pos:
                    size = abs(float(exchange_pos.get('contracts', 0) or 0))
                    side = exchange_pos.get('side', '').lower()
                    
                    if size >= KRAKEN_BTC_MIN_SIZE:
                        close_side = 'sell' if side == 'long' else 'buy'
                        logger.critical(f"🛑 Closing {side} position of {size:.6f} at market")
                        
                        await self.execution.close_position_market(
                            symbol=self.symbol,
                            side=close_side,
                            amount=size,
                            reason="DAILY_HALT"
                        )
                        logger.critical(f"✅ Daily halt position closed: {size:.6f} {side}")
                    elif size > 0:
                        logger.warning(f"⚠️ Daily halt: Position {size:.6f} < min - considered closed")
                else:
                    logger.info("No open position to close for daily halt")
            except Exception as e:
                logger.critical(f"🔴 CRITICAL: Failed to close position during daily halt: {e}")
            
            # Step 3: Reset local state
            self._reset_trade_state("Daily halt emergency close")
            
            # Step 4: Clear risk engine position
            if self.risk_engine.position:
                self.risk_engine.position = None
            
            logger.critical("🛑 Daily halt close completed - trading halted until next day")
            
        except Exception as e:
            logger.critical(f"🔴🔴🔴 CRITICAL ERROR in daily halt close: {e}")
    
    # NOTE: _open_position_from_order REMOVED - duplicated and unused
    # Position opening logic is in _handle_wait_fill_state after fill confirmation
    
    def get_status(self) -> dict:
        """Get engine status"""
        # Get position from PositionStore (source of truth in LIVE mode)
        position_store_pos = None
        if self._runtime_config and self._runtime_config.mode == RuntimeMode.LIVE:
            ps_pos = self._position_store.get_position(self.symbol) if self.symbol else None
            if ps_pos and not ps_pos.is_flat:
                position_store_pos = {
                    'side': ps_pos.side,
                    'size': ps_pos.size,
                    'entry_price': ps_pos.entry_price,
                    'source': 'exchange'
                }
        
        status = {
            'state': str(self.state_machine.state),
            'current_bias': self.current_bias,
            'has_position': self.risk_engine.has_open_position(),
            'position_store': position_store_pos,  # NEW: Source of truth
            'pending_order': self.pending_order_id is not None,
            'daily_trades': self.state_machine.daily_trades,
            'edge_state': self.edge_enforcement.state_machine.current_state.value if self.edge_enforcement else 'N/A',
            'regime': self.regime_classifier.current_regime.value if self.regime_classifier else 'N/A'
        }
        
        # Add health monitor status
        if self.health_monitor:
            health = self.health_monitor.get_health_status()
            status['health'] = health
            should_stop, reason = self.health_monitor.should_stop_trading()
            status['health_alert'] = reason if should_stop else None
        
        # Add important levels status
        if self.important_levels and self.important_levels.is_initialized:
            status['important_levels'] = self.important_levels.get_status()
        
        return status
    
    def get_important_levels_info(self, current_price: float = None) -> str:
        """
        Get formatted important levels info for Telegram/log notifications.
        
        Args:
            current_price: Current market price (uses last_price if not provided)
            
        Returns:
            Formatted string with level information
        """
        if not self.important_levels or not self.important_levels.is_initialized:
            return "⚠️ Important levels not available"
        
        price = current_price or self.market_data.get_current_price() or 0
        if price <= 0:
            return "⚠️ No price available for level calculation"
        
        return self.important_levels.format_levels_for_notification(price)
    
    # ===== EDGE ENFORCEMENT INTEGRATION =====
    
    async def _update_edge_context(self):
        """Update edge context for pre-trade validation"""
        # Use market_time for consistency (live and backtest)
        market_time = self.market_data.get_market_time()
        if market_time is None:
            return
        
        # Only update periodically
        if self._last_context_update and (market_time - self._last_context_update).total_seconds() < self._context_update_interval:
            return
        
        try:
            candles_1h = self.market_data.get_candles('1h')
            if not candles_1h or len(candles_1h) < 60:
                return
            
            df_1h = pd.DataFrame(candles_1h)
            highs = df_1h['high'].values
            lows = df_1h['low'].values
            closes = df_1h['close'].values
            volumes = df_1h['volume'].values
            
            # Calculate ATR and history for Activity Regime analysis
            atr_values = calculate_atr(highs, lows, closes, 14)
            atr_current = atr_values[-1] if len(atr_values) > 0 else 0
            
            # Macro event check (simplified - no macro detected by default)
            is_macro_event_day = False
            if hasattr(self.external_data, 'macro') and self.external_data.macro:
                is_macro_event_day = self.external_data.macro.is_in_quiet_zone()
            
            # Update context via edge enforcement using new Activity Regime model
            # The new validate_context uses Activity Regime instead of session-based logic
            self._edge_context = self.edge_enforcement.edge_validator.validate_context(
                atr_current=atr_current,
                atr_history=atr_values,
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes,
                is_macro_event_day=is_macro_event_day,
                candle_minutes=60  # 1h candles
            )
            
            self._last_context_update = market_time
            
            if self._edge_context and not self._edge_context.is_valid():
                logger.warning(f"⚠️ Edge context invalid: {self._edge_context.invalid_reasons()}")
            else:
                logger.debug(f"✅ Edge context valid - Score: {self._edge_context.edge_score:.2f} ({self._edge_context.edge_regime})")
            
        except Exception as e:
            logger.error(f"Error updating edge context: {e}")
    
    async def _update_regime_classifier(self, current_price: float):
        """Update regime classifier with market data"""
        try:
            candles_1h = self.market_data.get_candles('1h')
            if not candles_1h or len(candles_1h) < 30:
                return
            
            df_1h = pd.DataFrame(candles_1h)
            highs = df_1h['high'].values
            lows = df_1h['low'].values
            closes = df_1h['close'].values
            volumes = df_1h['volume'].values
            
            # Calculate metrics for regime classification
            atr_values = calculate_atr(highs, lows, closes, 14)
            atr_5d = (atr_values[-1] - atr_values[-5]) / atr_values[-5] if atr_values[-5] > 0 else 0
            atr_20d = (atr_values[-1] - atr_values[-20]) / atr_values[-20] if len(atr_values) >= 20 and atr_values[-20] > 0 else 0
            
            # Session metrics
            session_range = highs[-1] - lows[-1]
            session_realized = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0
            
            # Expansion count
            avg_atr = np.mean(atr_values[-20:]) if len(atr_values) >= 20 else atr_values[-1]
            expansion_sizes = [(highs[i] - lows[i]) / avg_atr for i in range(-5, 0) if avg_atr > 0]
            expansion_count = sum(1 for s in expansion_sizes if s > 1.0)
            
            # Volume quality (simplified)
            vol_quality = 0.6  # Default moderate
            if len(volumes) >= 20:
                recent_vol = np.mean(volumes[-5:])
                avg_vol = np.mean(volumes[-20:])
                vol_quality = min(recent_vol / avg_vol, 1.0) if avg_vol > 0 else 0.5
            
            # Breakout success rate (simplified - use 0.6 default)
            breakout_success = 0.6
            
            # Price action consistency (simplified)
            price_consistency = 0.5
            if len(closes) >= 10:
                changes = np.diff(closes[-10:])
                same_direction = sum(1 for i in range(len(changes)-1) if changes[i] * changes[i+1] > 0)
                price_consistency = same_direction / (len(changes) - 1) if len(changes) > 1 else 0.5
            
            # Classify regime
            regime, confidence = self.regime_classifier.classify_regime(
                expansion_count=expansion_count,
                expansion_sizes=expansion_sizes,
                breakout_success_rate=breakout_success,
                atr_trend_5d=atr_5d,
                atr_trend_20d=atr_20d,
                session_range=session_range,
                session_realized_move=session_realized,
                volume_profile_quality=vol_quality,
                price_action_consistency=price_consistency,
                order_flow_imbalance_std=0.2  # Default stable
            )
            
            logger.debug(f"📊 Regime: {regime.value} (confidence: {confidence:.2f})")
            
        except Exception as e:
            logger.error(f"Error updating regime classifier: {e}")
    
    def _record_trade_for_edge_enforcement(self, 
                                           entry_price: float, 
                                           exit_price: float,
                                           entry_time: datetime,
                                           exit_time: datetime,
                                           reached_tp1: bool,
                                           reached_tp2: bool,
                                           is_be_stop: bool):
        """Record completed trade in edge enforcement for degradation monitoring"""
        try:
            self._trade_counter += 1
            
            # Calculate MFE/MAE from position tracking if available
            mfe = self._current_trade_mfe
            mae = self._current_trade_mae
            
            self.edge_enforcement.record_completed_trade(
                trade_id=self._trade_counter,
                entry_price=entry_price,
                exit_price=exit_price,
                entry_time=entry_time,
                exit_time=exit_time,
                mfe=mfe,
                mae=mae,
                reached_tp1=reached_tp1,
                reached_tp2=reached_tp2,
                is_be_stop=is_be_stop
            )
            
            # Reset tracking
            self._current_trade_mfe = 0.0
            self._current_trade_mae = 0.0
            self._current_trade_entry_time = None
            
            logger.info(f"📝 Trade #{self._trade_counter} recorded in edge enforcement")
            
        except Exception as e:
            logger.error(f"Error recording trade for edge enforcement: {e}")

    async def _periodic_reconciliation(self):
        """
        FIX #4 (Jan 13, 2026): Periodic reconciliation with exchange.
        
        Runs every 30s to ensure local state matches exchange reality.
        Handles:
        1. Position size drift (partial liquidations, etc.)
        2. Orphan positions (position exists but no local tracking)
        3. Ghost positions (local tracking but no exchange position)
        4. Stale orders
        """
        try:
            logger.debug("🔄 Periodic reconciliation starting...")
            
            # Step 1: Sync position store
            sync_result = await self._position_store.sync_from_exchange(self.execution.exchange)
            
            # Step 2: Check for discrepancies and handle them
            if sync_result.get('discrepancies'):
                for d in sync_result['discrepancies']:
                    disc_type = d.get('type', 'unknown')
                    
                    if disc_type == 'size_mismatch':
                        # Exchange has different size than we think
                        local_size = d.get('local_size', 0)
                        exchange_size = d.get('exchange_size', 0)
                        
                        logger.warning(f"⚠️ SIZE MISMATCH: local={local_size:.6f} vs exchange={exchange_size:.6f}")
                        
                        # Update local position to match exchange
                        if self.risk_engine.position:
                            old_size = self.risk_engine.position.size
                            self.risk_engine.position.size = exchange_size
                            logger.info(f"🔄 Auto-corrected position size: {old_size:.6f} → {exchange_size:.6f}")
                    
                    elif disc_type == 'orphan_position':
                        # Exchange has position, we don't track it
                        logger.critical(f"🚨 ORPHAN POSITION DETECTED: {d}")
                        
                        # In production, we might want to adopt or close it
                        # For now, just log critically
                        if self.output_manager:
                            await self.output_manager.send_critical_alert(
                                f"🚨 ORPHAN POSITION: {d.get('exchange_side')} {d.get('exchange_size')} @ {d.get('exchange_entry')}"
                            )
                    
                    elif disc_type == 'ghost_position':
                        # We think we have position, exchange doesn't
                        logger.warning(f"⚠️ GHOST POSITION: Local thinks we have position but exchange has none")
                        
                        # Clear local state
                        if self.risk_engine.position:
                            logger.warning("🔄 Clearing ghost position from local state")
                            self.risk_engine.position = None
                            self._reset_trade_state("Ghost position cleared")
                            self.state_machine.transition_to(BotState.IDLE, "Ghost position - synced with exchange")
                    
                    elif disc_type == 'phantom_position_cleared':
                        # 🔥 FIX (Jan 14, 2026): Position was closed by exchange TP/SL
                        # This is the MAIN case for TP/SL notifications!
                        logger.critical(f"📤 PHANTOM POSITION CLEARED: Position closed by exchange (likely TP/SL)")
                        
                        local_size = d.get('local_size', 0)
                        local_side = d.get('local_side', 'UNKNOWN')
                        symbol = d.get('symbol', self.symbol)
                        
                        # Try to estimate PnL (we don't have exact exit price)
                        pnl_estimate = 0.0
                        r_realized = 0.0
                        exit_type = "EXCHANGE_TP_SL"
                        exit_price = 0.0
                        entry_price = 0.0  # 🔥 FIX: Track entry for notification
                        
                        # Get last known price
                        candles = self.market_data.get_candles('1m')
                        if candles and len(candles) > 0:
                            exit_price = candles[-1]['close']
                        
                        # If we still have position info in risk_engine, calculate PnL
                        if self.risk_engine.position:
                            pos = self.risk_engine.position
                            entry_price = getattr(pos, 'entry_price', 0.0)  # 🔥 FIX: Get entry price
                            if hasattr(pos, 'calculate_pnl') and exit_price > 0:
                                pnl_estimate = pos.calculate_pnl(exit_price)
                            if hasattr(pos, 'entry_price') and hasattr(pos, 'initial_stop_loss'):
                                initial_risk = abs(pos.entry_price - pos.initial_stop_loss)
                                if initial_risk > 0 and pos.size > 0:
                                    r_realized = pnl_estimate / (initial_risk * pos.size)
                                # Determine if TP or SL based on price
                                if pos.direction == 'LONG':
                                    exit_type = 'EXCHANGE_TAKE_PROFIT' if exit_price > pos.entry_price else 'EXCHANGE_STOP_LOSS'
                                else:
                                    exit_type = 'EXCHANGE_TAKE_PROFIT' if exit_price < pos.entry_price else 'EXCHANGE_STOP_LOSS'
                        
                        # Record trade close
                        self.state_machine.record_trade_close(pnl_estimate, pnl_estimate > 0, exit_reason=exit_type)
                        
                        # 🔥 SEND TELEGRAM NOTIFICATION
                        if self.output_manager:
                            try:
                                from datetime import datetime, UTC
                                _create_safe_task(self.output_manager.send_exit_notification({
                                    'symbol': symbol,
                                    'reason': exit_type,
                                    'pnl': pnl_estimate,
                                    'r_realized': r_realized,
                                    'entry_price': entry_price,  # 🔥 FIX: Include entry price
                                    'exit_price': exit_price,
                                    'direction': local_side,
                                    'size': local_size,
                                    'timestamp': datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')
                                }), "send_phantom_close_notification")
                                logger.info("📱 Telegram notification sent for phantom position close")
                            except Exception as e:
                                logger.error(f"Failed to send phantom close notification: {e}")
                        
                        # Clear local state
                        self.risk_engine.position = None
                        self._reset_trade_state("Phantom position cleared by exchange")
                        self.state_machine.transition_to(BotState.IDLE, "Exchange closed position (TP/SL)")
                    
                    else:
                        logger.warning(f"⚠️ Unknown discrepancy: {d}")
            
            # Step 3: Verify local risk engine matches position store
            pos_store_pos = self._position_store.get_position(self.symbol) if self.symbol else None
            local_pos = self.risk_engine.position
            
            if pos_store_pos and not pos_store_pos.is_flat:
                # Exchange has position
                if not local_pos:
                    # We should have local tracking but don't
                    logger.warning("⚠️ Position store has position but risk engine doesn't")
                    # The startup recovery or next candle will handle this
                else:
                    # Verify size matches
                    if abs(pos_store_pos.size - local_pos.size) > 0.00001:
                        logger.warning(f"🔄 Size correction: local={local_pos.size:.6f} → store={pos_store_pos.size:.6f}")
                        local_pos.size = pos_store_pos.size
            else:
                # Exchange has no position
                if local_pos:
                    logger.warning("⚠️ Risk engine has position but exchange doesn't - clearing")
                    self.risk_engine.position = None
                    if self.state_machine.state == BotState.MANAGE:
                        self._reset_trade_state("Periodic reconciliation - no exchange position")
                        self.state_machine.transition_to(BotState.IDLE, "No exchange position")
            
            # =========================================================================
            # Step 4: ZOMBIE ORDER REAPER (Jan 17, 2026)
            # Use centralized cleanup_zombie_orders method from position_store
            # This handles orders that are NOT in our tracking and NOT protective (SL/TP)
            # =========================================================================
            try:
                zombie_result = await self._position_store.cleanup_zombie_orders(
                    exchange=self.execution.exchange,
                    symbol=self.symbol,
                    max_age_minutes=5  # Cancel orders older than 5 min that we don't track
                )
                
                if zombie_result.get('cancelled', 0) > 0:
                    logger.warning(f"🧟 Zombie cleanup: {zombie_result['cancelled']} orders cancelled")
                    # Send notification for zombie orders
                    if self.output_manager and zombie_result.get('zombie_orders'):
                        for zombie in zombie_result['zombie_orders']:
                            if zombie.get('cancelled'):
                                _create_safe_task(
                                    self.output_manager.send_critical_alert(
                                        f"🧟 ZOMBIE ORDER CANCELLED: {zombie.get('side')} @ ${zombie.get('price', 0):.2f} "
                                        f"(age: {zombie.get('age_minutes', 0):.1f}min)"
                                    ),
                                    "zombie_alert"
                                )
            except Exception as e:
                logger.debug(f"Error in zombie order cleanup: {e}")
            
            logger.debug("✅ Periodic reconciliation complete")
            
        except Exception as e:
            logger.error(f"Periodic reconciliation error: {e}")

